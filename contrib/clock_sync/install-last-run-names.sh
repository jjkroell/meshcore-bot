#!/bin/bash
# Last run column: bot name on line 2 (highlighted), Nov-04 dates, no "contact re-added":
#   sudo bash install-last-run-names.sh
# Roll back: the clock_sync_web.py.before-last-run-names-* copy in /opt/clock_sync_web/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-last-run-names-$STAMP"
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
systemctl restart clock-sync-web
systemctl is-active clock-sync-web
echo "Done. Reload the page."
