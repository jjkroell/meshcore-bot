#!/bin/bash
# Eye button in the page's password fields, to check what you typed:
#   sudo bash install-password-eye.sh
# Roll back: the clock_sync_web.py.before-eye-* copy in /opt/clock_sync_web/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-eye-$STAMP"
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
systemctl restart clock-sync-web
systemctl is-active clock-sync-web
echo "Done. Reload the page: the Add form and each node's Edit row have an eye in the password field."
