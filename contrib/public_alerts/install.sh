#!/bin/bash
# Installs (or updates) the PublicAlerts plugin. Run with sudo:
#
#   sudo bash contrib/public_alerts/install.sh [/opt/meshcore-bot]
#
# It installs TURNED OFF and doesn't restart the bot. Settings appear on the web
# viewer's Plugins page. To go live: add the channel to the radio, tick Enabled
# there, then restart the bot. Existing [PublicAlerts] settings are kept.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BOT=${1:-/opt/meshcore-bot}
OWNER=$(stat -c %U:%G "$BOT")

install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 644 "$HERE/public_alerts_service.py" "$BOT/local/service_plugins/public_alerts_service.py"
if ! grep -q '^\[PublicAlerts\]' "$BOT/local/config.ini" 2>/dev/null; then
	[ -f "$BOT/local/config.ini" ] && cp -p "$BOT/local/config.ini" "$BOT/local/config.ini.bak-$(date +%F-%H%M)"
	{ [ -s "$BOT/local/config.ini" ] && echo; cat "$HERE/config.ini.publicalerts"; } >> "$BOT/local/config.ini"
	chown "$OWNER" "$BOT/local/config.ini"
	echo "Added [PublicAlerts] (disabled) to $BOT/local/config.ini"
fi
# Older installs kept one channel at the top of the section: make it section 1.
if ! grep -q '^route1_channel' "$BOT/local/config.ini"; then
	cp -p "$BOT/local/config.ini" "$BOT/local/config.ini.bak-$(date +%F-%H%M)"
	OLD=$(sed -n '/^\[PublicAlerts\]/,$s/^channel = //p' "$BOT/local/config.ini" | head -1)
	sed -i '/^\[PublicAlerts\]/,${/^channel = /d;/^min_level = /d;/^area_codes = /d;/^area_names = /d}' "$BOT/local/config.ini"
	grep '^route1_' "$HERE/config.ini.publicalerts" | sed "s|^route1_channel = .*|route1_channel = ${OLD:-#swbc-alerts}|" > "$BOT/local/.route1"
	sed -i "/^\[PublicAlerts\]/r $BOT/local/.route1" "$BOT/local/config.ini"
	rm -f "$BOT/local/.route1"
	echo "Moved the channel settings into section 1"
fi
sed -n '/^\[PublicAlerts\]/,$p' "$BOT/local/config.ini" | grep -E '^(enabled|route[0-9]+_channel)'
