"""Tests for the channel directory and the channels / bots replies built from it."""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.clients import channel_directory
from modules.clients.channel_directory import ChannelDirectory, is_bot_channel, parse_exclude_list
from modules.commands.bots_command import BotsCommand
from modules.commands.channels_command import ChannelsCommand
from tests.conftest import mock_message

DISCOVERED = {"channels": [
    {"name": name, "status": "confirmed"}
    for name in ("quiet", "busy", "kod-bot", "pmd-bot", "langley-bot", "test")
] + [{"name": "pending-one", "status": "pending"}]}
RECENT = (
    [{"channel": "busy", "messageHash": str(i)} for i in range(3)]
    + [{"channel": "busy", "messageHash": "0"}] * 5  # same message, other observers
    + [{"channel": "langley-bot", "messageHash": "a"}, {"channel": "langley-bot", "messageHash": "b"}]
    + [{"channel": "pmd-bot", "messageHash": "a"}]
    + [{"channel": "Public", "messageHash": "z"}, {"channel": None, "messageHash": "y"}]
)


def _directory(responses=None):
    directory = ChannelDirectory("https://example.test/", Mock())
    answers = responses or {"/api/channels/discovered": DISCOVERED, "/api/channels/recent": RECENT}

    def fake_get(path):
        answer = answers[path.split('?')[0]]
        if isinstance(answer, Exception):
            raise answer
        return answer

    directory._get_json = Mock(side_effect=fake_get)
    return directory


class TestDirectory:
    def test_busiest_first_counting_each_message_once(self):
        assert _directory().names() == ["busy", "langley-bot", "pmd-bot", "kod-bot", "quiet", "test"]

    def test_only_confirmed_listed(self):
        assert "pending-one" not in _directory().names()

    def test_second_call_uses_cache(self):
        directory = _directory()
        directory.names()
        calls = directory._get_json.call_count
        directory.names()
        assert directory._get_json.call_count == calls

    def test_keeps_last_list_when_fetch_fails(self):
        directory = _directory()
        first = directory.names()
        directory._next_fetch = 0
        directory._get_json = Mock(side_effect=OSError("down"))
        assert directory.names() == first

    def test_empty_when_never_fetched(self):
        assert _directory({"/api/channels/discovered": OSError("down")}).names() == []

    def test_traffic_failure_still_lists_names(self):
        directory = _directory({"/api/channels/discovered": DISCOVERED, "/api/channels/recent": OSError("x")})
        assert directory.names()[:2] == ["busy", "kod-bot"]


def test_is_bot_channel():
    assert is_bot_channel("kod-bot") and is_bot_channel("Bot")
    assert is_bot_channel("test") and is_bot_channel("Testing")
    assert not is_bot_channel("bcferries")


def test_parse_exclude_list():
    assert parse_exclude_list(" #Test, testing ,, # ") == {"test", "testing"}


def _bot(url="https://example.test", exclude=""):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "#kod-bot")
    config.add_section("Keywords")
    config.add_section("Channels_Command")
    config.set("Channels_Command", "directory_url", url)
    config.set("Channels_Command", "directory_exclude", exclude)
    config.add_section("Channels_List")
    config.set("Channels_List", "fromconfig", "Fallback")
    bot.config = config
    bot.translator = MagicMock()

    def translate(key, **kwargs):
        return "Common channels: " if key.endswith("common_channels") else key

    bot.translator.translate = Mock(side_effect=translate)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["#kod-bot"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    return bot


@pytest.fixture
def directory(monkeypatch):
    fake = _directory()
    monkeypatch.setattr(channel_directory, "_directories", {"https://example.test": fake})
    return fake


class TestChannelsReply:
    @pytest.mark.asyncio
    async def test_plain_channels_leaves_out_bot_and_test_channels(self, directory):
        bot = _bot()
        await ChannelsCommand(bot).execute(mock_message(content="channels"))
        sent = bot.command_manager.send_response.call_args.args[1]
        assert sent == "Common channels: #busy, #quiet"

    @pytest.mark.asyncio
    async def test_exclusions_apply(self, directory):
        bot = _bot(exclude="#quiet")
        await ChannelsCommand(bot).execute(mock_message(content="channels"))
        assert bot.command_manager.send_response.call_args.args[1] == "Common channels: #busy"

    @pytest.mark.asyncio
    async def test_falls_back_to_config_when_directory_has_nothing(self, monkeypatch):
        monkeypatch.setattr(
            channel_directory, "_directories",
            {"https://example.test": _directory({"/api/channels/discovered": OSError("down")})},
        )
        bot = _bot()
        await ChannelsCommand(bot).execute(mock_message(content="channels"))
        assert "#fromconfig" in bot.command_manager.send_response.call_args.args[1]

    @pytest.mark.asyncio
    async def test_no_url_uses_config(self):
        bot = _bot(url="")
        await ChannelsCommand(bot).execute(mock_message(content="channels"))
        assert "#fromconfig" in bot.command_manager.send_response.call_args.args[1]


class TestBotsReply:
    @pytest.mark.asyncio
    async def test_lists_other_bots_busiest_first(self, directory):
        bot = _bot()
        assert await BotsCommand(bot).execute(mock_message(content="bots")) is True
        chunks = bot.command_manager.send_response_chunked.call_args.args[1]
        assert chunks == ["Bot/test channels: #langley-bot, #pmd-bot, #test"]

    @pytest.mark.asyncio
    async def test_excluded_bot_left_out(self, directory):
        bot = _bot(exclude="#pmd-bot")
        await BotsCommand(bot).execute(mock_message(content="bots"))
        assert bot.command_manager.send_response_chunked.call_args.args[1] == ["Bot/test channels: #langley-bot, #test"]

    @pytest.mark.asyncio
    async def test_says_so_without_a_directory(self):
        bot = _bot(url="")
        await BotsCommand(bot).execute(mock_message(content="bot"))
        assert bot.command_manager.send_response.call_args.args[1] == "No other bot or test channels found."

    def test_long_list_splits(self):
        cmd = BotsCommand(_bot())
        channels = [f"#channel-number-{i:02d}-bot" for i in range(20)]
        messages = cmd._split_messages(channels)
        assert len(messages) > 1
        assert all(len(m.encode("utf-8")) <= 130 for m in messages)

    @pytest.mark.parametrize("content,expected", [
        ("bots", True), ("bot", True), ("Bot", True),
        ("bot is down", False), ("bots are fun", False), ("robot", False),
    ])
    def test_only_the_bare_word_triggers(self, content, expected):
        cmd = BotsCommand(_bot())
        assert cmd.matches_keyword(mock_message(content=content)) is expected


@pytest.mark.asyncio
async def test_channels_waits_a_second_between_parts(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("modules.commands.channels_command.asyncio.sleep", sleep)
    bot = _bot()
    await ChannelsCommand(bot)._send_multiple_messages(mock_message(content="channels"), ["a", "b", "c"])
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 1.0]
