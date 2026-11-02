#!/bin/bash
# "Ns off" like PMD-TimeSync:  sudo bash install-offset.sh
#  - meshcore 2.3.7 -> 2.3.17 in /opt/clock_sync/venv (2.3.7 drops the node's
#    clock from the login reply). meshcore-bot has its own venv: untouched.
#  - clock_sync.py measures how far each node's clock was off at login, logs
#    it, stores it in state.json and posts `Name [key]: SUCCESS (5s off)`
#  - the page shows it after "Last synced by"
# Roll back the library: sudo /opt/clock_sync/venv/bin/pip install meshcore==2.3.7
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
/opt/clock_sync/venv/bin/pip freeze > "/root/clock_sync-venv-freeze.before-offset-$STAMP"
cp -a /opt/clock_sync/clock_sync.py "/opt/clock_sync/clock_sync.py.before-offset-$STAMP"
cp -a /opt/clock_sync/peer_parse.py "/opt/clock_sync/peer_parse.py.before-offset-$STAMP"
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-offset-$STAMP"

/opt/clock_sync/venv/bin/pip install -q --disable-pip-version-check "meshcore==2.3.17"
install -m 664 clock_sync/clock_sync.py /opt/clock_sync/clock_sync.py
install -m 644 clock_sync/peer_parse.py /opt/clock_sync/peer_parse.py
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
systemctl restart clock-sync-web
sleep 3
/opt/clock_sync/venv/bin/python -I -c "
import sys; sys.path.insert(0, '/opt/clock_sync')
import meshcore, clock_sync, peer_parse, importlib.metadata as m
print('meshcore', m.version('meshcore'), '- clock_sync imports OK')"
systemctl is-active clock-sync-web
echo "Done. Test it: on the page, press Run on one node - its result should start with 'Ns off (ahead/behind)'."
