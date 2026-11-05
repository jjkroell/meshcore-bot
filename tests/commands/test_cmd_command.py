"""Tests for modules.commands.cmd_command."""

from unittest.mock import AsyncMock

import pytest

from modules.commands.alert_command import AlertCommand
from modules.commands.cmd_command import DISPLAY_NAMES, CmdCommand
from modules.commands.ping_command import PingCommand
from modules.commands.sports_command import SportsCommand
from modules.commands.trace_command import TraceCommand
from modules.commands.wx_command import WxCommand
from tests.conftest import mock_message


class TestCmdCommand:
    """Tests for CmdCommand."""

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.command_manager.commands = {"ping": object(), "help": object()}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "false")
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_is_not_listed_when_sports_cmd_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        command_mock_bot.config.add_section("Wx_Command")
        command_mock_bot.config.set("Wx_Command", "enabled", "true")
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "enabled", "false")
        command_mock_bot.config.add_section("Trace_Command")
        command_mock_bot.config.set("Trace_Command", "enabled", "true")
        command_mock_bot.config.add_section("Alert_Command")
        command_mock_bot.config.set("Alert_Command", "enabled", "true")

        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {
            'ping': PingCommand(command_mock_bot),
            'wx': WxCommand(command_mock_bot),
            'sports': SportsCommand(command_mock_bot),
            'trace': TraceCommand(command_mock_bot),
            'alert': AlertCommand(command_mock_bot)
        }

        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        call_args = command_mock_bot.command_manager.send_response.call_args

        assert result is True
        assert 'ping' in call_args[0][1]
        assert 'wx' in call_args[0][1]
        assert 'sports' not in call_args[0][1]
        assert 'trace' in call_args[0][1]
        assert 'alert' in call_args[0][1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled_value", ["false", "no", "0", "False", "FALSE", "off"])
    async def test_is_not_listed_for_any_disabled_spelling(self, command_mock_bot, disabled_value):
        """config.ini accepts every boolean spelling configparser does, not just 'false'."""
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "enabled", disabled_value)

        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {
            'ping': PingCommand(command_mock_bot),
            'sports': SportsCommand(command_mock_bot),
        }

        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        call_args = command_mock_bot.command_manager.send_response.call_args

        assert result is True
        assert 'ping' in call_args[0][1]
        assert 'sports' not in call_args[0][1]

    @pytest.mark.asyncio
    async def test_is_listed_when_command_has_no_config_section(self, command_mock_bot):
        """A command with no [<Name>_Command] section defaults to enabled, as before."""
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")

        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {
            'ping': PingCommand(command_mock_bot),
            'sports': SportsCommand(command_mock_bot),
        }

        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        call_args = command_mock_bot.command_manager.send_response.call_args

        assert result is True
        assert 'ping' in call_args[0][1]
        assert 'sports' in call_args[0][1]

    @pytest.mark.asyncio
    async def test_execute_returns_command_list(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.command_manager.keywords = {}  # No custom cmd keyword -> use dynamic list
        mock_ping = type("MockCmd", (), {"keywords": ["ping"]})()
        mock_help = type("MockCmd", (), {"keywords": ["help"]})()
        command_mock_bot.command_manager.commands = {"ping": mock_ping, "help": mock_help}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert "ping" in response or "help" in response or "cmd" in response

    @pytest.mark.asyncio
    async def test_channel_reply_puts_list_on_line_after_mention(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.command_manager.keywords = {}
        ping = type("MockCmd", (), {
            "keywords": ["ping"],
            "_derive_config_section_name": lambda self: "Ping_Command",
            "get_config_value": lambda self, section, key, fallback=None, value_type=None: fallback,
        })()
        command_mock_bot.command_manager.commands = {"ping": ping}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=False, sender_id="Kim")
        assert await cmd.execute(msg) is True
        response = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert response.startswith("@[Kim]\nAvail cmds: ")

    @pytest.mark.asyncio
    async def test_execute_returns_reference_url_when_configured(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.set("Cmd_Command", "cmd_reference_url", "https://example.com/commands")
        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {"ping": type("MockCmd", (), {"keywords": ["ping"]})()}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)

        result = await cmd.execute(msg)

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        assert call_args[0][1] == "Full command reference: https://example.com/commands"

    @pytest.mark.asyncio
    async def test_execute_reference_url_takes_precedence_over_custom_keyword(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.set("Cmd_Command", "cmd_reference_url", "https://example.com/commands")
        command_mock_bot.command_manager.keywords = {"cmd": "Custom cmd output"}
        command_mock_bot.command_manager.commands = {"ping": type("MockCmd", (), {"keywords": ["ping"]})()}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)

        result = await cmd.execute(msg)

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        assert call_args[0][1] == "Full command reference: https://example.com/commands"

    def test_get_commands_list_truncation(self, command_mock_bot):
        """Test that _get_commands_list truncates long lists with '(N more)' suffix."""
        import re
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.command_manager.keywords = {}
        # Create 25 mock commands with long names to force truncation
        commands = {}
        for i in range(25):
            name = f"longcommandname{i:02d}"
            commands[name] = CmdCommand(command_mock_bot)
            command_mock_bot.config.add_section(name.title() + "_Command")
        command_mock_bot.command_manager.commands = commands
        cmd = CmdCommand(command_mock_bot)
        # "Avail cmds: " = 12 chars; "longcommandnameNN" = 17 chars; ", " = 2 chars
        # 3 commands fit in 67 chars; suffix " (22 more)" = 11 chars; total = 78
        # max_length=82 allows 3 commands + suffix, but not a 4th command (86)
        result = cmd._get_commands_list(max_length=82)
        # Should contain truncation indicator
        assert "more)" in result
        # Should start with prefix
        assert result.startswith("Avail cmds: ")
        # Should NOT contain doubled numbers like "(5 5 more)"
        assert not re.search(r'\(\d+ \d+ more\)', result)


class _FakeCommand:
    """Just enough of a command for the cmd listing."""

    def __init__(self, name, requires_dm=False, admin=False):
        self.name = name
        self.keywords = [name]
        self.requires_dm = requires_dm
        self.admin = admin

    def _derive_config_section_name(self):
        return f"{self.name.title()}_Command"

    def get_config_value(self, section, key, fallback=None, value_type='str'):
        return fallback

    def is_channel_allowed(self, message):
        return True

    def requires_admin_access(self):
        return self.admin

    def _check_admin_access(self, message):
        return message.sender_id == "Boss"


def _cmd_with(bot, *commands):
    bot.command_manager.keywords = {}
    bot.command_manager._is_channel_trigger_allowed = lambda name, message: True
    bot.command_manager.commands = {c.name: c for c in commands}
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    return CmdCommand(bot)


class TestCmdFullList:
    """cmd lists every usable command, over as many messages as it takes."""

    def test_uses_the_words_people_type(self, command_mock_bot):
        cmd = _cmd_with(command_mock_bot, *[_FakeCommand(n) for n in ("catfact", "dogfact", "hacker", "ferry")])
        names = cmd._get_command_names(mock_message(content="cmd"))
        assert sorted(names) == ["ferries", "meow", "sudo", "woof"]

    def test_display_names_are_real_triggers(self):
        from modules.commands.announcements_command import AnnouncementsCommand
        from modules.commands.catfact_command import CatfactCommand
        from modules.commands.dogfact_command import DogfactCommand
        from modules.commands.ferry_command import FerryCommand
        from modules.commands.hacker_command import HackerCommand
        for cls in (AnnouncementsCommand, CatfactCommand, DogfactCommand, FerryCommand, HackerCommand):
            assert DISPLAY_NAMES[cls.name] in cls.keywords

    def test_dm_only_commands_shown_in_dm_not_channel(self, command_mock_bot):
        cmd = _cmd_with(command_mock_bot, _FakeCommand("ping"), _FakeCommand("advert", requires_dm=True))
        assert cmd._get_command_names(mock_message(content="cmd")) == ["ping"]
        assert "advert" in cmd._get_command_names(mock_message(content="cmd", is_dm=True))

    def test_admin_commands_only_shown_to_admins(self, command_mock_bot):
        cmd = _cmd_with(command_mock_bot, _FakeCommand("ping"), _FakeCommand("reload", admin=True))
        assert "reload" not in cmd._get_command_names(mock_message(content="cmd", is_dm=True))
        assert "reload" in cmd._get_command_names(mock_message(content="cmd", is_dm=True, sender_id="Boss"))

    def test_chunks_hold_every_name_within_limit(self, command_mock_bot):
        cmd = _cmd_with(command_mock_bot)
        names = [f"command{i:02d}" for i in range(40)]
        chunks = cmd._chunk_commands(names, 100)
        assert len(chunks) > 1
        assert all(len(c.encode("utf-8")) <= 100 for c in chunks)
        joined = " ".join(chunks).removeprefix("Avail cmds: ")
        assert [n.strip() for n in joined.replace(",", " ").split()] == names

    def test_short_list_is_one_message(self, command_mock_bot):
        cmd = _cmd_with(command_mock_bot)
        assert cmd._chunk_commands(["ping", "wx"], 100) == ["Avail cmds: ping, wx"]

    @pytest.mark.asyncio
    async def test_long_list_goes_out_chunked_and_addressed(self, command_mock_bot):
        cmd = _cmd_with(command_mock_bot, *[_FakeCommand(f"command{i:02d}") for i in range(40)])
        assert await cmd.execute(mock_message(content="cmd")) is True
        command_mock_bot.command_manager.send_response.assert_not_called()
        sent = command_mock_bot.command_manager.send_response_chunked.call_args
        chunks = sent.args[1]
        assert len(chunks) > 1
        assert chunks[0].startswith("@[TestUser]\nAvail cmds: ")
        assert not chunks[1].startswith("@[")
        assert sent.kwargs["skip_user_rate_limit_first"] is False
        assert "command39" in chunks[-1]
