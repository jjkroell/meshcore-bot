#!/bin/bash
# Adds the web page's "Run now" buttons:  sudo bash install-run-button.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
if systemctl is-active -q clock-sync-manual 2>/dev/null; then echo "a manual clock sync is running - wait for it to finish"; exit 1; fi

cp -a /opt/clock_sync/run-clock-sync.sh /opt/clock_sync/run-clock-sync.sh.before-lock
install -m 775 clock_sync/run-clock-sync.sh /opt/clock_sync/run-clock-sync.sh
install -m 755 runner/clock-sync-run-request.sh /opt/clock_sync_web/clock-sync-run-request.sh
install -m 644 runner/clock-sync-run.path runner/clock-sync-run.service /etc/systemd/system/
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
rm -f /etc/clock_sync/run-request
systemctl daemon-reload
systemctl enable --now clock-sync-run.path
systemctl restart clock-sync-web
sleep 2
systemctl is-active clock-sync-run.path clock-sync-web
echo "Done. Reload the page: 'Run all now' at the top, 'Run' on each node."
