"""
dyk command for meshcore-bot (local command plugin).

Replies with a random tip from local/did_you_know_tips.txt, the same file the
daily DidYouKnow post uses. Tips for commands the sender can't use from where
they asked are left out, and so are tips too long to fit after the @[sender].
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Optional

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

PREFIX = "Did you know?\n➡️ "
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


class DykCommand(BaseCommand):
    name = "dyk"
    keywords = ["dyk"]
    description = "Sends a random tip about a bot command"
    category = "basic"
    cooldown_seconds = 3

    short_description = "Random tip about a bot command"
    usage = "dyk"
    examples = ["dyk"]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.dyk_enabled = self.get_config_value("Dyk_Command", "enabled", fallback=True, value_type="bool")
        self._last: Optional[str] = None

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.dyk_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return "dyk - sends a random tip about what a bot command does."

    def _usable(self, name: str, message: MeshMessage) -> bool:
        """True if the sender could use this command from where they asked."""
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
            return True  # the tip tells them to DM the bot
        return command.is_channel_allowed(message) and manager._is_channel_trigger_allowed(name, message)

    def pick_tip(self, message: MeshMessage) -> Optional[str]:
        limit = self.get_max_message_length(message)
        tips = [
            PREFIX + tip
            for name, tip in parse_tips(TIPS_FILE.read_text(encoding="utf-8"))
            if name != self.name
            and len((PREFIX + tip).encode("utf-8")) <= limit
            and self._usable(name, message)
        ]
        # Never the same one twice running, unless it's the only one.
        fresh = [tip for tip in tips if tip != self._last] or tips
        self._last = random.choice(fresh) if fresh else None
        return self._last

    async def execute(self, message: MeshMessage) -> bool:
        self.record_execution(message.sender_id)
        return await self.send_response(message, self.pick_tip(message) or "No tips available right now.")
