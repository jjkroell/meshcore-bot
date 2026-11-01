# SosAck — auto reply on #sos

A local service plugin for meshcore-bot. When someone sends a message on
**#sos** containing one of [Ridgeline](https://ridgeline.ve7kod.ca)'s emergency
words, the bot replies on #sos so the sender knows their call got out:

> @[KOD - Jesse]
> SOS received, email sent to Ridgeline Alert users.
>
> Send your location and nature of the emergency ASAP.

A call that says it's a test (**test, tests, testing, tested** anywhere in it,
Ridgeline's own test rule) gets `test_reply` instead, because Ridgeline only
emails test calls to accounts that opted in to test messages:

> @[KOD - Jesse]
> Test received

## Trigger words

The same rule Ridgeline uses for its #sos alerts (keep them in step): **sos,
emergency, mayday, 911, threat, life** as whole words, or **need help / needs
help / help me**, anywhere in the message and in any case. `#sos` on its own
(the channel's name), "wildlife", "lifeline", "threats", "thanks for the help"
and "test" on its own don't count. Neither does a reply: a message that **starts with an
@-mention** ("@[Dave] what's your emergency?") is part of a conversation, not
a new call, so it gets no auto reply and raises no Ridgeline alert.

## Safety rules

- A reply must never set off a Ridgeline alert itself. Either it opens with
  `@[{sender}]` (Ridgeline reads a message starting with an @-mention as a
  reply, so emergency words are fine — the default does this), or it contains
  no trigger word at all. The plugin refuses to start otherwise. Without the
  leading mention, a sender name that would trigger (e.g. "SOS Sam") is left
  out.
- `\n` in `reply` / `test_reply` is a line break.
- One reply per sender per `sender_cooldown_seconds` (60), counted separately
  for tests and real calls, so a real call right after a test still gets one.
- Messages more than `max_age_minutes` (15) old — queued on the radio while
  the bot was down — get no reply.
- The reply is trimmed to fit one channel message (150 bytes with the bot's
  name): the hop count goes first, then the sender's name is shortened (the
  `@[` stays, so it's still a reply to Ridgeline).

## Install

1. Add **#sos** to the bot's radio channels (web viewer → channels). Keep it
   **out** of `[Channels] monitor_channels`: the plugin listens to the radio
   directly, and a monitored #sos would have every bot command (help, test,
   hello…) answering on the emergency channel.
2. From the bot's install directory:

   ```
   sudo bash contrib/sos_ack/install.sh
   ```

   It copies the plugin into `local/service_plugins/`, adds the `[SosAck]`
   settings to `local/config.ini` if they aren't there, and restarts the bot.
   The log should show `SosAck: acknowledging emergency words on #sos`.

To change the reply or channels, edit `[SosAck]` in `local/config.ini` and
restart the bot. `channel` takes a list (`#sos, #sos-test`) for testing.

## Test

`python3 contrib/sos_ack/test_sos_ack.py` runs without a radio (it stubs the
bot) and checks the trigger rule against Ridgeline's cases.
