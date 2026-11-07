"""
Daily "Did you know?" tip for meshcore-bot (local service plugin).

Once a day, posts one line to a channel explaining what a bot command does or
what its reply means. Tips live in local/did_you_know_tips.txt, which is read
at send time, so wording can change without a restart.

The tip is picked from the date, so restarts neither repeat nor skip one, and
no state is kept. If the bot is down at the send time, that day's tip is
skipped; it isn't sent late.

A tip is left out when its command is disabled, not allowed on the channel, or
admin-only, so nobody is pointed at a command they can't use. DM-only commands
stay in: their tips say to DM the bot.

Config (local/config.ini):

    [DidYouKnow]
    enabled = true
    channel = #kod-bot
    time = 12:00                (bot timezone, 24 hour)
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from typing import Any

from modules.models import MeshMessage
from modules.service_plugins.base_service import BaseServicePlugin
from modules.utils import get_config_timezone

PREFIX = "Did you know? "
# Channel replies are safe up to this many bytes whatever the radio's name is.
MAX_BYTES = 130
TIPS_FILE = Path(__file__).resolve().parent.parent / "did_you_know_tips.txt"


def parse_tips(text: str) -> list[tuple[str, str]]:
    """Read "command: tip" lines. Blank lines and # comments are ignored."""
    tips = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        command, sep, tip = line.partition(":")
        if sep and tip.strip():
            tips.append((command.strip().lower(), tip.strip()))
    return tips


def pick_tip(tips: list[str], day: datetime.date) -> str | None:
    """Step through the tips one per day."""
    return tips[day.toordinal() % len(tips)] if tips else None


def next_send(now: datetime.datetime, hour: int, minute: int) -> datetime.datetime:
    """The next time the clock reads hour:minute, strictly after now."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + datetime.timedelta(days=1)


class DidYouKnowService(BaseServicePlugin):
    config_section = "DidYouKnow"
    description = "Posts a daily tip explaining a bot command"

    def __init__(self, bot: Any):
        super().__init__(bot)
        cfg = bot.config
        sec = self.config_section
        self.enabled = cfg.getboolean(sec, "enabled", fallback=False)
        self.channel = cfg.get(sec, "channel", fallback="#kod-bot").strip()
        hour, minute = cfg.get(sec, "time", fallback="12:00").strip().split(":")
        self.hour, self.minute = int(hour), int(minute)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        # Some bot versions call start() twice; run one loop only.
        if not self.enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self.logger.info(f"DidYouKnow: daily tip at {self.hour:02d}:{self.minute:02d} on {self.channel}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def _now(self) -> datetime.datetime:
        tz, _ = get_config_timezone(self.bot.config, self.logger)
        return datetime.datetime.now(tz)

    async def _loop(self) -> None:
        while self._running:
            now = self._now()
            await asyncio.sleep((next_send(now, self.hour, self.minute) - now).total_seconds())
            try:
                await self.send_tip(self._now().date())
            except Exception as e:
                self.logger.error(f"DidYouKnow: send failed: {e}")
            # Never wake twice for the same minute.
            await asyncio.sleep(1)

    def _usable(self, name: str) -> bool:
        """True if someone on the channel could use this command."""
        manager = self.bot.command_manager
        command = manager.commands.get(name)
        if command is None:
            return False
        section = command._derive_config_section_name()
        if not command.get_config_value(section, "enabled", fallback=True, value_type="bool"):
            return False
        requires_admin = getattr(command, "requires_admin_access", None)
        if callable(requires_admin) and requires_admin() is True:
            return False
        if getattr(command, "requires_dm", False) is True:
            return True
        message = MeshMessage(content="", channel=self.channel)
        return command.is_channel_allowed(message) and manager._is_channel_trigger_allowed(name, message)

    def available_tips(self) -> list[str]:
        """Tips that fit in one message and point at a usable command."""
        tips = []
        for name, tip in parse_tips(TIPS_FILE.read_text(encoding="utf-8")):
            text = PREFIX + tip
            if len(text.encode("utf-8")) > MAX_BYTES:
                self.logger.warning(f"DidYouKnow: tip too long, skipped: {tip}")
            elif self._usable(name):
                tips.append(text)
        return tips

    async def send_tip(self, day: datetime.date) -> bool:
        text = pick_tip(self.available_tips(), day)
        if text is None:
            self.logger.warning("DidYouKnow: no usable tips")
            return False
        ok = await self.bot.command_manager.send_channel_message(
            self.channel, text, skip_user_rate_limit=True
        )
        if not ok:
            self.logger.warning(f"DidYouKnow: not sent to {self.channel}")
        return ok
