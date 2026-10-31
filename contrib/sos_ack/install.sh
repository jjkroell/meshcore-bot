#!/bin/bash
# Installs (or updates) the SosAck plugin into a meshcore-bot install and
# restarts the bot. Run with sudo:
#
#   sudo bash contrib/sos_ack/install.sh [/opt/meshcore-bot]
#
# The plugin file is always refreshed. The [SosAck] settings are added to
# local/config.ini only if that section is missing, so edits made there are
# kept; change the reply or channels in local/config.ini, then rerun.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BOT=${1:-/opt/meshcore-bot}
OWNER=$(stat -c %U:%G "$BOT")

install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 644 "$HERE/sos_ack_service.py" "$BOT/local/service_plugins/sos_ack_service.py"
if ! grep -q '^\[SosAck\]' "$BOT/local/config.ini" 2>/dev/null; then
	[ -f "$BOT/local/config.ini" ] && cp -p "$BOT/local/config.ini" "$BOT/local/config.ini.bak-$(date +%F-%H%M)"
	{ [ -s "$BOT/local/config.ini" ] && echo; cat "$HERE/config.ini.sosack"; } >> "$BOT/local/config.ini"
	chown "$OWNER" "$BOT/local/config.ini"
	echo "Added [SosAck] to $BOT/local/config.ini"
fi
sed -n '/^\[SosAck\]/,/^\[/p' "$BOT/local/config.ini" | grep -v '^\[[^S]'

# #sos must not be a monitored channel, or every bot command answers there.
if sed -n '/^\[Channels\]/,/^\[/p' "$BOT/config.ini" | grep -qiE '^\s*monitor_channels\s*=.*#?sos\s*(,|$)'; then
	echo "WARNING: #sos is in [Channels] monitor_channels in $BOT/config.ini — remove it (the plugin doesn't need it)."
fi

systemctl restart meshcore-bot
sleep 20
grep -a "SosAck" "$BOT/meshcore_bot.log" | tail -2 || true
systemctl is-active meshcore-bot
