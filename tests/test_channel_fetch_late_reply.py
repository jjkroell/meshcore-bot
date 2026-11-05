"""A late radio reply must never put a channel in the wrong slot.

The radio answers CMD_GET_CHANNEL with the slot it was asked about, but the
library hands back the next channel reply whichever slot it is for. On
2026-11-04 slot 1's reply came after the 2 s wait, while the bot was waiting on
slot 2: #ve7kod was filed under slot 2 and Public vanished from the map, so the
bot took Public traffic for #ve7kod and replied there.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.channel_manager import ChannelManager

ZERO = b"\x00" * 16
SLOTS = {0: "#kod-bot", 1: "#ve7kod", 2: "Public", 3: "", 4: "#sos", 5: "#time-sync"}


def info(idx):
    """What the radio says about a slot; slot 3 is empty, as on the live companion."""
    name = SLOTS[idx]
    secret = bytes([idx + 1]) * 16 if name else ZERO
    return {"channel_idx": idx, "channel_name": name, "channel_secret": secret}


class FakeRadio:
    """Answers in order, like the serial link. `slow` slots answer only after the
    bot's wait has expired: the reply is queued and goes to the NEXT request, with
    that request's own reply behind it, dropped as nobody is waiting by then."""

    def __init__(self, slow=(), silent=(), error=None):
        self.slow = list(slow)
        self.silent = set(silent)
        self.error = error or {}
        self.late = []
        self.calls = []

    async def get_channel(self, idx):
        self.calls.append(idx)
        if self.late:
            return SimpleNamespace(payload=self.late.pop(0))
        if idx in self.error:
            return SimpleNamespace(payload=self.error[idx])
        if idx in self.silent:
            await asyncio.Event().wait()
        if idx in self.slow:
            self.slow.remove(idx)
            self.late.append(info(idx))
            await asyncio.Event().wait()
        return SimpleNamespace(payload=info(idx))


def manager(radio):
    bot = MagicMock()
    bot.connected = True
    bot.meshcore.commands = radio
    bot.config.getfloat.return_value = 0.0
    cm = ChannelManager(bot, max_channels=6)
    cm._fetch_timeout = 0.01
    cm._read_retry_delay = 0
    cm._store_channels_in_db = Mock()
    return cm


def names(cm):
    return {i: c["channel_name"] for i, c in cm._channels_cache.items()}


GOOD = {0: "#kod-bot", 1: "#ve7kod", 2: "Public", 4: "#sos", 5: "#time-sync"}


@pytest.mark.asyncio
async def test_clean_read():
    cm = manager(FakeRadio())
    assert len(await cm.fetch_all_channels(force_refresh=True)) == 5
    assert names(cm) == GOOD


@pytest.mark.asyncio
async def test_late_reply_is_not_filed_under_the_next_slot():
    """The 2026-11-04 failure."""
    cm = manager(FakeRadio(slow=[1]))
    channels = await cm.fetch_all_channels(force_refresh=True)
    assert names(cm) == GOOD
    assert cm.get_channel_number("#ve7kod") == 1
    assert cm.get_channel_number("Public") == 2
    assert cm.get_channel_name(2) == "Public"
    assert all(i == c["channel_idx"] for i, c in cm._channels_cache.items())
    assert len(channels) == 5


@pytest.mark.asyncio
async def test_late_reply_from_the_empty_slot_is_ignored():
    cm = manager(FakeRadio(slow=[3]))
    await cm.fetch_all_channels(force_refresh=True)
    assert names(cm) == GOOD


@pytest.mark.asyncio
async def test_slot_that_never_answers_gives_no_map_at_all():
    """No partial map: Public missing is what made the bot unsafe."""
    cm = manager(FakeRadio(silent=[2]))
    assert await cm.fetch_all_channels(force_refresh=True) == []
    assert cm._cache_valid is False
    cm._store_channels_in_db.assert_not_called()


@pytest.mark.asyncio
async def test_unanswered_slot_fails_startup():
    """fetch_channels() False is what makes core.py refuse to come up."""
    cm = manager(FakeRadio(silent=[2]))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        assert await cm.fetch_channels(max_attempts=2, retry_delay=0) is False
    assert cm._cache_valid is False


@pytest.mark.asyncio
async def test_failed_refresh_keeps_the_last_good_map():
    cm = manager(FakeRadio())
    await cm.fetch_all_channels(force_refresh=True)
    cm.bot.meshcore.commands = FakeRadio(silent=[2])
    assert await cm.fetch_all_channels(force_refresh=True) == []
    assert names(cm) == GOOD


@pytest.mark.asyncio
async def test_radio_error_means_no_such_slot():
    """Past the radio's last slot it answers with an error: a real answer, not a timeout."""
    radio = FakeRadio(error={5: {"error_code": 2, "code_string": "not found"}})
    cm = manager(radio)
    assert len(await cm.fetch_all_channels(force_refresh=True)) == 4
    assert radio.calls.count(5) == 1


@pytest.mark.asyncio
async def test_library_timeout_counts_as_no_answer():
    radio = FakeRadio(error={2: {"reason": "no_event_received"}})
    cm = manager(radio)
    assert await cm.fetch_all_channels(force_refresh=True) == []
    assert radio.calls.count(2) == cm._read_attempts


@pytest.mark.asyncio
async def test_get_channel_uncached_is_checked_too():
    radio = FakeRadio()
    radio.late.append(info(1))
    cm = manager(radio)
    assert (await cm.get_channel(2, use_cache=False))["channel_name"] == "Public"
    assert names(cm) == {2: "Public"}


@pytest.mark.asyncio
async def test_get_channel_gives_none_when_the_radio_stays_silent():
    cm = manager(FakeRadio(silent=[2]))
    assert await cm.get_channel(2, use_cache=False) is None
