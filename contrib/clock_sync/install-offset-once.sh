#!/bin/bash
# Last run column: the clock offset is shown once, not twice:
#   sudo bash install-offset-once.sh
# Roll back: the clock_sync_web.py.before-offset-once-* copy in /opt/clock_sync_web/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-offset-once-$STAMP"
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
systemctl restart clock-sync-web
systemctl is-active clock-sync-web
echo "Done. Reload the page."
