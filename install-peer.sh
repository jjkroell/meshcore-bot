#!/bin/bash
# Shared #time-sync channel with PMD-TimeSync:  sudo bash install-peer.sh
#  - clock_sync.py: posts `Name [3-byte key]: OUTCOME` to #time-sync; on the
#    scheduled run, skips a node PMD-TimeSync reported OK in the last 24 h
#  - peer_parse.py: reads the other bot's posts (used by the run and the page)
#  - web page: "Last synced by <bot> · <time>" per node, #time-sync card,
#    permanent archive of every message the bot hears (/var/lib/clock-sync-web)
#  - config: summary_channel "#time-sync", peer_bots, peer_skip_hours
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

CFG=/etc/clock_sync/clock_sync.yaml
STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync/clock_sync.py "/opt/clock_sync/clock_sync.py.before-peer-$STAMP"
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-peer-$STAMP"
cp -a /etc/systemd/system/clock-sync-web.service "/root/clock-sync-web.service.before-peer-$STAMP"
cp -a "$CFG" "/root/clock_sync.yaml.before-peer-$STAMP"

# Config: change summary_channel, add the peer settings. Only those keys may
# change - checked by parsing before and after.
/opt/clock_sync/venv/bin/python -I - "$CFG" <<'EOF'
import re, sys, yaml
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
before = yaml.safe_load(text)
want = {"summary_channel": "#time-sync", "peer_bots": ["PMD-TimeSync"], "peer_skip_hours": 24}
new = text
if re.search(r"(?m)^summary_channel\s*:", new):
    new = re.sub(r"(?m)^summary_channel\s*:.*$", 'summary_channel: "#time-sync"', new, count=1)
else:
    new = new.rstrip("\n") + '\nsummary_channel: "#time-sync"\n'
extra = ""
if "peer_bots" not in before:
    extra += 'peer_bots: ["PMD-TimeSync"]   # other time-sync bots on summary_channel\n'
if "peer_skip_hours" not in before:
    extra += "peer_skip_hours: 24           # skip a node a peer reported OK within this many hours\n"
if extra:
    new = new.rstrip("\n") + "\n\n# Shared #time-sync channel (added " + __import__("time").strftime("%Y-%m-%d") + ")\n" + extra
after = yaml.safe_load(new)
for k in set(before) | set(after):
    if k in want:
        continue
    assert before.get(k) == after.get(k), f"{k} would change - not writing"
print("summary_channel:", before.get("summary_channel"), "->", after["summary_channel"])
print("peer_bots:", after.get("peer_bots"), " peer_skip_hours:", after.get("peer_skip_hours"))
if new != text:
    with open(path, "w", encoding="utf-8") as f:  # in place: keeps owner + mode
        f.write(new)
EOF

install -m 664 clock_sync/clock_sync.py /opt/clock_sync/clock_sync.py
install -m 644 clock_sync/peer_parse.py /opt/clock_sync/peer_parse.py
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
install -m 644 web/clock-sync-web.service /etc/systemd/system/clock-sync-web.service
systemctl daemon-reload
systemctl restart clock-sync-web
sleep 3
systemctl is-active clock-sync-web
/opt/clock_sync/venv/bin/python -I -c "import sys; sys.path.insert(0, '/opt/clock_sync'); import clock_sync, peer_parse; print('clock_sync imports OK')"
journalctl -u clock-sync-web --since "-10s" --no-pager -o cat | grep -v '"GET' | tail -5
echo "Done. Reload http://192.168.102.39:8090 - 'Last synced by' under each node's last run, #time-sync card at the bottom."
echo "Backups: *.before-peer-$STAMP (in /opt/clock_sync, /opt/clock_sync_web, /root)"
