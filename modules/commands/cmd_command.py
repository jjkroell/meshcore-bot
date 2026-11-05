#!/usr/bin/env python3
"""
Cmd command for the MeshCore Bot
Lists available commands in a compact, comma-separated format for LoRa
"""

from typing import Any, Optional

from ..models import MeshMessage
from .base_command import BaseCommand


# Commands whose plugin name isn't what people type (or isn't a trigger at all).
DISPLAY_NAMES = {
    'announcements': 'announce',
    'catfact': 'meow',
    'dogfact': 'woof',
    'ferry': 'ferries',
    'hacker': 'sudo',
}


class CmdCommand(BaseCommand):
    """Handles the cmd command"""

    # Plugin metadata
    name = "cmd"
    keywords = ['cmd', 'cmds', 'command', 'commands']
    description = "Lists available commands in compact format"
    category = "basic"

    # Documentation
    short_description = "Lists available commands"
    usage = "cmd"
    examples = ["cmd"]

    def __init__(self, bot):
        """Initialize the cmd command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.cmd_enabled = self.get_config_value('Cmd_Command', 'enabled', fallback=True, value_type='bool')
        self.cmd_reference_url = self.get_config_value(
            'Cmd_Command', 'cmd_reference_url', fallback='', value_type='str'
        ).strip()

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.cmd_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        """Get help text for the cmd command.

        Returns:
            str: The help text for this command.
        """
        return "Lists commands in compact format."

    def _is_command_valid_for_channel(self, cmd_name: str, cmd_instance: Any, message: Optional[MeshMessage]) -> bool:
        """Return True if this command is valid in the message's channel context."""
        if message is None:
            return True
        if hasattr(cmd_instance, 'is_channel_allowed') and callable(cmd_instance.is_channel_allowed):
            if not cmd_instance.is_channel_allowed(message):
                return False
        if hasattr(self.bot.command_manager, '_is_channel_trigger_allowed'):
            if not self.bot.command_manager._is_channel_trigger_allowed(cmd_name, message):
                return False
        return True

    def _is_usable_by_sender(self, cmd_instance: Any, message: Optional[MeshMessage]) -> bool:
        """Return False for commands this sender would only get refused by."""
        if message is None:
            return True
        if getattr(cmd_instance, 'requires_dm', False) is True and not message.is_dm:
            return False
        requires_admin = getattr(cmd_instance, 'requires_admin_access', None)
        if callable(requires_admin) and requires_admin() is True:
            return cmd_instance._check_admin_access(message) is True
        return True

    def _get_command_names(self, message: Optional[MeshMessage] = None) -> list[str]:
        """Get the names to show for every available command, most important first.

        When message is provided, only includes commands this sender can use here
        (respects per-command channel overrides, channel_keywords, DM-only and
        admin-only commands).

        Args:
            message: Optional message for context filtering.

        Returns:
            list[str]: Names as a user would type them.
        """
        # Define priority order - most important/commonly used commands first
        priority_commands = [
            'test', 'ping', 'help', 'hello', 'cmd', 'advert',
            'wx', 'aqi', 'sun', 'moon', 'solar', 'hfcond', 'satpass',
            'prefix', 'path', 'sports', 'dice', 'roll', 'stats', 'channels', 'bots'
        ]

        # Get all command names (only those available in this context)
        all_commands = []

        # Include plugin commands that are valid for this channel
        for cmd_name, cmd_instance in self.bot.command_manager.commands.items():
            # Skip system commands without keywords (like greeter)
            if hasattr(cmd_instance, 'keywords') and cmd_instance.keywords:
                section_name = cmd_instance._derive_config_section_name()
                if not cmd_instance.get_config_value(section_name, 'enabled', fallback=True, value_type='bool'):
                    continue
                if not self._is_command_valid_for_channel(cmd_name, cmd_instance, message):
                    continue
                if not self._is_usable_by_sender(cmd_instance, message):
                    continue
                all_commands.append(DISPLAY_NAMES.get(cmd_name, cmd_name))

        # Include config keywords that aren't handled by plugins (and are allowed in channel)
        for keyword in self.bot.command_manager.keywords:
            # Check if this keyword is already handled by a plugin
            is_plugin_keyword = any(
                keyword.lower() in [k.lower() for k in cmd.keywords]
                for cmd in self.bot.command_manager.commands.values()
            )
            if not is_plugin_keyword:
                if message is not None and hasattr(self.bot.command_manager, '_is_channel_trigger_allowed'):
                    if not self.bot.command_manager._is_channel_trigger_allowed(keyword, message):
                        continue
                all_commands.append(keyword)

        # Remove duplicates and sort
        all_commands = sorted(set(all_commands))

        # Prioritize: put priority commands first, then others
        prioritized = []
        remaining = []

        for cmd in all_commands:
            if cmd in priority_commands:
                prioritized.append(cmd)
            else:
                remaining.append(cmd)

        # Sort priority commands by their order in priority_commands list
        prioritized = sorted(prioritized, key=lambda x: priority_commands.index(x) if x in priority_commands else 999)

        # Combine: priority first, then others
        return prioritized + sorted(remaining)

    def _chunk_commands(self, command_names: list[str], max_length: int) -> list[str]:
        """Split the full command list into messages of at most max_length bytes."""
        chunks: list[str] = []
        line = "Avail cmds: "
        empty = True
        for name in command_names:
            addition = name if empty else f", {name}"
            if not empty and len((line + addition + ",").encode('utf-8')) > max_length:
                chunks.append(line + ",")
                line, addition = "", name
            line += addition
            empty = False
        chunks.append(line)
        return chunks

    def _get_commands_list(self, message: Optional[MeshMessage] = None, max_length: Optional[int] = None) -> str:
        """Get a compact list of available commands, prioritizing important ones.

        Args:
            message: Optional message for context filtering. When provided, only
                commands this sender can use here are included.
            max_length: Maximum length for the command list (None = no limit).

        Returns:
            str: Comma-separated list of commands, truncated if necessary.
        """
        command_names = self._get_command_names(message)

        # Build the list, respecting max_length if provided
        if max_length is None:
            return ', '.join(command_names)

        # Build list within length limit
        result: list[str] = []
        prefix = "Avail cmds: "
        current_length = len(prefix)

        for cmd in command_names:
            # Calculate length if we add this command: ", cmd" or "cmd" (first one)
            test_length = current_length + len(', ') + len(cmd) if result else current_length + len(cmd)

            if test_length <= max_length:
                if result:
                    result.append(cmd)
                    current_length += len(', ') + len(cmd)
                else:
                    result.append(cmd)
                    current_length += len(cmd)
            else:
                # Can't fit this command, add count of remaining
                remaining_count = len(command_names) - len(result)
                if remaining_count > 0:
                    suffix = f" ({remaining_count} more)"
                    if current_length + len(suffix) <= max_length:
                        result.append(suffix)
                break

        return prefix + ', '.join(result)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the cmd command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            if self.cmd_reference_url:
                return await self.send_response(message, f"Full command reference: {self.cmd_reference_url}")

            # Check if user has defined a custom cmd keyword response in config
            # Use the already-loaded keywords dict (quotes are already stripped)
            cmd_keyword = self.bot.command_manager.keywords.get('cmd')
            if cmd_keyword:
                # User has defined a custom response, use it with formatting
                response = self.bot.command_manager.format_keyword_response(cmd_keyword, message)
                return await self.send_response(message, response)

            # Fallback to dynamic command list if no custom keyword is defined
            # Get max message length to ensure we fit within limits
            max_length = self.get_max_message_length(message)
            chunks = self._chunk_commands(self._get_command_names(message), max_length)
            if not message.is_dm and message.sender_id:
                # Address it ourselves with the list on its own line; same bytes as
                # the "@[name] " send_channel_message would otherwise add.
                chunks[0] = f"@[{message.sender_id}]\n{chunks[0]}"
            if len(chunks) == 1:
                return await self.send_response(message, chunks[0])
            return await self.send_response_chunked(message, chunks, skip_user_rate_limit_first=False)
        except Exception as e:
            self.logger.error(f"Error executing cmd command: {e}")
            error_msg = self.translate('errors.execution_error', command='cmd', error=str(e))
            return await self.send_response(message, error_msg)
