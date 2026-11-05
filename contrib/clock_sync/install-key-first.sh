#!/bin/bash
# #time-sync node lines now open with the key:  sudo bash install-key-first.sh
#    [6df99f] Mt Benson Top S: SUCCESS (73s off)
# Was `Mt Benson Top S [6df99f]: ...`, which a command bot on the channel took
# for its `mt` command.
# Roll back: the clock_sync.py.before-key-first-* copy in /opt/clock_sync/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync/clock_sync.py "/opt/clock_sync/clock_sync.py.before-key-first-$STAMP"
install -m 664 clock_sync/clock_sync.py /opt/clock_sync/clock_sync.py
/opt/clock_sync/venv/bin/python -I -c "
import sys; sys.path.insert(0, '/opt/clock_sync')
import clock_sync
print(clock_sync.format_post('Mt Benson Top S', '6df99fabcdef', 'SUCCESS', '(73s off)'))"
echo "Done. Test it: on the page, press Run on one node - its #time-sync line starts with [key]."
