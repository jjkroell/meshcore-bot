"""
#sos acknowledgement for meshcore-bot (local service plugin).

When someone sends a message on #sos containing one of Ridgeline's emergency
words, reply on #sos so they know their call was heard by the bot.

The trigger is the same as Ridgeline's (internal/store/sos.go sosAlertRe and
web/src/lib/sos.svelte.ts ALERT_RE): sos, emergency, mayday, 911, threat or
life as a whole word ("#sos", the channel's own tag, doesn't count), or
"need help" / "needs help" / "help me". A message that opens with an
@-mention is a reply, not a new call, and gets no reply. Keep the three in step.

This listens to the radio's channel messages directly, like the Discord bridge
does, so it works without #sos in [Channels] monitor_channels. Leave #sos OUT
of monitor_channels: otherwise every bot command (help, test, hello, ...)
answers on the emergency channel too. #sos only has to be one of the radio's
channels.

Config (local/config.ini):

    [SosAck]
    enabled = true
    channel = #sos              (or a list: #sos, #sos-test)
    reply = @[{sender}] *auto reply*  Email sent to Ridgeline alert users. Please reply with your location.
    sender_cooldown_seconds = 60
    max_age_minutes = 15
"""

from __future__ import annotations

import copy
import re
import time
from typing import Any

from meshcore import EventType

from modules.service_plugins.base_service import BaseServicePlugin

# Python's re has lookbehind, so this is ALERT_RE (the TypeScript form).
ALERT_RE = re.compile(
    r"(?<![#\w])(sos|emergency|mayday|911|threat|life)(?!\w)|\bneeds? help\b|\bhelp me\b",
    re.IGNORECASE,
)

# A message opening with an @-mention ("@[Dave] on my way") is a reply in a
# conversation, not a new call for help (Ridgeline's sosReplyRe / REPLY_RE).
REPLY_RE = re.compile(r"^\s*@\[")

DEFAULT_REPLY = "@[{sender}] *auto reply*  Email sent to Ridgeline alert users. Please reply with your location."

# The bot's limit for a channel message, "<bot name>: " included.
MAX_CHANNEL_BYTES = 150

# A sender clock before this is unset, so its message age is unknown.
PLAUSIBLE_CLOCK = 1_700_000_000  # Nov 2023


def has_alert_word(text: str) -> bool:
    """Whether text contains an emergency word at all."""
    return bool(ALERT_RE.search(text))


def is_alert(text: str) -> bool:
    """Whether a #sos message is a call for help (Ridgeline's rule): an
    emergency word, and not a reply opening with an @-mention."""
    return has_alert_word(text) and not REPLY_RE.match(text)


def split_sender(text: str) -> tuple[str, str]:
    """Channel text arrives as "Name: message"."""
    if ":" in text and not text.startswith("http"):
        name, body = text.split(":", 1)
        return name.strip(), body.strip()
    return "", text.strip()


class SosAckService(BaseServicePlugin):
    config_section = "SosAck"
    name = "sos_ack"
    description = "Replies on #sos when an emergency word is heard, so the sender knows it got out"

    def __init__(self, bot: Any):
        super().__init__(bot)
        cfg = bot.config
        sec = self.config_section
        self.enabled = cfg.getboolean(sec, "enabled", fallback=False)
        # One channel or a comma-separated list (e.g. "#sos, #sos-test").
        raw = cfg.get(sec, "channel", fallback="#sos")
        self.channels = {c.strip().lstrip("#").lower() for c in raw.split(",") if c.strip()}
        self.reply = cfg.get(sec, "reply", fallback=DEFAULT_REPLY).strip()
        self.cooldown = cfg.getfloat(sec, "sender_cooldown_seconds", fallback=60.0)
        self.max_age = cfg.getfloat(sec, "max_age_minutes", fallback=15.0) * 60
        self._last_ack: dict[str, float] = {}

    # --- lifecycle ---

    async def start(self) -> None:
        # Some bot versions call start() twice; subscribe only once.
        if not self.enabled or self._running:
            return
        if has_alert_word(self._render("Tester", None)):
            # The reply would itself look like an emergency to Ridgeline (and to
            # this service), alerting every monitor. Refuse to run with it.
            self.logger.error("SosAck: the reply template contains an emergency word; not starting")
            return
        if not getattr(self.bot, "meshcore", None):
            self.logger.error("SosAck: meshcore not available; not starting")
            return
        self.bot.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)
        self._running = True
        names = ", ".join("#" + c for c in sorted(self.channels))
        self.logger.info(f"SosAck: acknowledging emergency words on {names}")

    async def on_transport_reconnected(self) -> None:
        if self._running and getattr(self.bot, "meshcore", None):
            self.bot.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)

    async def stop(self) -> None:
        self._running = False

    def _flood_scope(self) -> str | None:
        # Newer bots have get_mesh_flood_scope(); before it, None = the
        # [Channels] default.
        get = getattr(super(), "get_mesh_flood_scope", None)
        return get() if callable(get) else None

    # --- messages ---

    def _same_channel(self, name: str) -> bool:
        return name.strip().lstrip("#").lower() in self.channels

    def _render(self, sender: str, hops: int | None) -> str:
        """The reply, shortened to fit one channel message: the radio sends
        "<bot name>: <text>" in at most MAX_CHANNEL_BYTES, so a long sender name
        loses the hop count first, then the mention."""
        text = self._format(sender, hops)
        bot_name = self.bot.config.get("Bot", "bot_name", fallback="the bot")
        room = MAX_CHANNEL_BYTES - len(bot_name.encode()) - 2
        if len(text.encode()) > room:
            text = self._format(sender, None)
        if len(text.encode()) > room:
            text = self._format("", None)
        return text

    def _format(self, sender: str, hops: int | None) -> str:
        bot_name = self.bot.config.get("Bot", "bot_name", fallback="the bot")
        if hops is None:
            hops_text = ""
        elif hops == 0:
            hops_text = " (direct)"
        else:
            hops_text = f" ({hops} hop{'s' if hops != 1 else ''})"
        fields = dict(bot=bot_name, hops=hops if hops is not None else "?", hops_text=hops_text)
        text = self.reply.format(sender=sender, **fields) if sender else ""
        if not sender or has_alert_word(text):
            # No name, or one like "SOS Dave" that would make the reply an
            # alert: leave the mention out rather than raise a false one.
            text = self.reply.replace("@[{sender}] ", "").replace("{sender}", "").format(sender="", **fields)
        return text

    async def _on_channel_message(self, event, metadata=None) -> None:
        if not self._running:
            return
        try:
            payload = copy.deepcopy(getattr(event, "payload", None)) or {}
            channel_name = self.bot.channel_manager.get_channel_name(payload.get("channel_idx", 0))
            if not self._same_channel(channel_name):
                return
            sender, body = split_sender(payload.get("text", ""))
            if not is_alert(body):
                return

            # Messages the radio queued while the bot was down arrive on start;
            # an old call shouldn't get a "received" as if it were new.
            sent_at = payload.get("sender_timestamp")
            if isinstance(sent_at, (int, float)) and sent_at > PLAUSIBLE_CLOCK and time.time() - sent_at > self.max_age:
                self.logger.info(f"SosAck: not acknowledging an old message from {sender or 'unknown'}")
                return

            key = sender.lower() or "?"
            now = time.monotonic()
            if now - self._last_ack.get(key, -1e9) < self.cooldown:
                return
            self._last_ack[key] = now

            # The raw path_len byte: low 6 bits = hops, high 2 = bytes per hop;
            # 0xFF = not flooded (unknown here).
            path_len = payload.get("path_len")
            hops = path_len & 63 if isinstance(path_len, int) and 0 <= path_len < 0xFF else None
            text = self._render(sender, hops)
            self.logger.info(f"SosAck: emergency word from {sender or 'unknown'}; replying on {channel_name}")
            ok = await self.bot.command_manager.send_channel_message(
                channel_name, text, skip_user_rate_limit=True, scope=self._flood_scope()
            )
            if not ok:
                self.logger.warning("SosAck: reply was not sent")
                self._last_ack.pop(key, None)  # let the next message try again
        except Exception as e:  # never take the bot down over an ack
            self.logger.error(f"SosAck: error handling channel message: {e}")
