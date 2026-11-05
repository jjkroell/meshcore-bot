#!/usr/bin/env python3
"""
Bots command for the MeshCore Bot
Lists the mesh's other bot and test channels, busiest first
"""

import asyncio

from ..clients.channel_directory import get_directory, is_bot_channel, parse_exclude_list
from ..models import MeshMessage
from .base_command import BaseCommand

MESSAGE_LIMIT = 130


class BotsCommand(BaseCommand):
    """Handles the bots command."""

    # Plugin metadata
    name = "bots"
    keywords = ['bots', 'bot']
    description = "Lists other bot and test channels"
    category = "basic"

    # Documentation
    short_description = "Lists other bot and test channels"
    usage = "bots"
    examples = ["bots"]

    def __init__(self, bot):
        """Initialize the bots command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.bots_enabled = self.get_config_value('Bots_Command', 'enabled', fallback=True, value_type='bool')
        self.directory_url = self.get_config_value(
            'Channels_Command', 'directory_url', fallback='', value_type='str'
        ).strip()
        self.exclude = parse_exclude_list(self.get_config_value(
            'Channels_Command', 'directory_exclude', fallback='', value_type='str'
        ))

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message."""
        if not self.bots_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return "Lists other bot and test channels, busiest first."

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Match only the bare word, so chat like "bot is down" gets no reply."""
        keywords = {keyword.lower() for keyword in self.keywords}
        return self._cleaned_content_matches(message, lambda content: content in keywords)

    def _other_bot_channels(self, names: list[str]) -> list[str]:
        """Pick the bot and test channels that aren't this bot's own or excluded."""
        own = {channel.lstrip('#').lower() for channel in self.bot.command_manager.monitor_channels}
        return [
            f"#{name}" for name in names
            if is_bot_channel(name) and name.lower() not in own and name.lower() not in self.exclude
        ]

    def _split_messages(self, channels: list[str]) -> list[str]:
        """Split the list into messages short enough for one packet."""
        messages: list[str] = []
        line = "Bot/test channels: "
        empty = True
        for channel in channels:
            addition = channel if empty else f", {channel}"
            if not empty and len((line + addition).encode('utf-8')) > MESSAGE_LIMIT:
                messages.append(line)
                line, addition = "", channel
            line += addition
            empty = False
        messages.append(line)
        return messages

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the bots command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if the reply was sent.
        """
        names: list[str] = []
        if self.directory_url:
            directory = get_directory(self.directory_url, self.logger)
            names = await asyncio.to_thread(directory.names)
        channels = self._other_bot_channels(names)
        if not channels:
            return await self.send_response(message, "No other bot or test channels found.")
        return await self.send_response_chunked(
            message, self._split_messages(channels), skip_user_rate_limit_first=False
        )
