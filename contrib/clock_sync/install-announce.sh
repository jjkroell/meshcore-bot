#!/bin/bash
# Start / finish lines on #time-sync, like PMD-TimeSync:  sudo bash install-announce.sh
#  - before the first node: `Time Sync started Wed Nov 04 04:00: 12 repeaters to
#    check, 1 skipped (synced by PMD-TimeSync in last 24h)`
#  - after the last:        `Time Sync finished Wed Nov 04 04:14: 11/12 OK,
#    1 NOT done, 1 skipped. Done until Fri 04:00`
#  - turn both off with `announce_runs: false` in clock_sync.yaml
# Roll back: the clock_sync.py.before-announce-* copy in /opt/clock_sync/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync/clock_sync.py "/opt/clock_sync/clock_sync.py.before-announce-$STAMP"
install -m 664 clock_sync/clock_sync.py /opt/clock_sync/clock_sync.py
/opt/clock_sync/venv/bin/python -I -c "
import sys; sys.path.insert(0, '/opt/clock_sync')
import clock_sync
print(clock_sync.format_start(3, 1, ['PMD-TimeSync'], 24, False))
print(clock_sync.format_finish([], clock_sync.next_run_text()))"
echo "Done. Test it: on the page, press Run on one node - #time-sync gets a 'Time Sync started' line, the node, then 'Time Sync finished'."
