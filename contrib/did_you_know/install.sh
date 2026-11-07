#!/bin/bash
# Installs (or updates) the DidYouKnow plugin and dyk command into a meshcore-bot install and
# restarts the bot. Run with sudo:
#
#   sudo bash contrib/did_you_know/install.sh [/opt/meshcore-bot]
#
# The plugin is always refreshed. The tips file and the [DidYouKnow] settings
# are added only if missing, so edits made on the bot are kept.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BOT=${1:-/opt/meshcore-bot}
OWNER=$(stat -c %U:%G "$BOT")

install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 644 "$HERE/did_you_know_service.py" "$BOT/local/service_plugins/did_you_know_service.py"
install -d -o "${OWNER%:*}" -g "${OWNER#*:}" "$BOT/local/commands"
install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 644 "$HERE/dyk_command.py" "$BOT/local/commands/dyk_command.py"
if [ ! -f "$BOT/local/did_you_know_tips.txt" ]; then
	install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 664 "$HERE/did_you_know_tips.txt" "$BOT/local/did_you_know_tips.txt"
fi
if ! grep -q '^dyk:' "$BOT/local/did_you_know_tips.txt"; then
	grep '^dyk:' "$HERE/did_you_know_tips.txt" >> "$BOT/local/did_you_know_tips.txt"
fi
if ! grep -q '^\[DidYouKnow\]' "$BOT/local/config.ini" 2>/dev/null; then
	[ -f "$BOT/local/config.ini" ] && cp -p "$BOT/local/config.ini" "$BOT/local/config.ini.bak-$(date +%F-%H%M)"
	{ [ -s "$BOT/local/config.ini" ] && echo; cat "$HERE/config.ini.didyouknow"; } >> "$BOT/local/config.ini"
	chown "$OWNER" "$BOT/local/config.ini"
	echo "Added [DidYouKnow] to $BOT/local/config.ini"
fi

systemctl restart meshcore-bot
sleep 20
grep -a -E "DidYouKnow|plugin: dyk" "$BOT/meshcore_bot.log" | tail -3 || true
grep -a "  Channel [0-9]*:" "$BOT/meshcore_bot.log" | tail -6 || true
systemctl is-active meshcore-bot
