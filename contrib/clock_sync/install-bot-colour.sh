#!/bin/bash
# Last run column: our bot name in purple instead of green:
#   sudo bash install-bot-colour.sh
# Roll back: the clock_sync_web.py.before-bot-colour-* copy in /opt/clock_sync_web/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-bot-colour-$STAMP"
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
systemctl restart clock-sync-web
systemctl is-active clock-sync-web
echo "Done. Reload the page."
