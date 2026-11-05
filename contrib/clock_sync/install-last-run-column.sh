#!/bin/bash
# Last run column reads `SUCCESS 11-04 04:06 KOD-BOT`, offset on the line below:
#   sudo bash install-last-run-column.sh [bot name]   (default KOD-BOT)
# Also sets bot_name in clock_sync.yaml, which is the name the page shows for us.
# Roll back: the *.before-last-run-* copies in /opt/clock_sync_web/ and /root/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
CONF=/etc/clock_sync/clock_sync.yaml
NAME=${1:-KOD-BOT}
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bot name: letters, numbers, dot, dash, underscore only"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a "$CONF" "/root/clock_sync.yaml.before-last-run-$STAMP"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
if grep -qE '^bot_name:' "$CONF"; then
    sed -E "s/^bot_name:.*/bot_name: $NAME/" "$CONF" > "$TMP"
elif grep -qE '^repeaters:' "$CONF"; then
    sed -E "0,/^repeaters:/s//bot_name: $NAME\n&/" "$CONF" > "$TMP"
else
    echo "no repeaters: section in $CONF - nothing changed"; exit 1
fi

# Check the edited copy before it replaces the real file.
/opt/clock_sync/venv/bin/python -I - "$TMP" "$NAME" "$CONF" <<'PY'
import sys, yaml
new, name, conf = sys.argv[1:4]
a, b = yaml.safe_load(open(new, encoding="utf-8")), yaml.safe_load(open(conf, encoding="utf-8"))
assert a.get("bot_name") == name, "value not set"
a.pop("bot_name"); b.pop("bot_name", None)
assert a == b, "something else changed"
PY
cat "$TMP" > "$CONF"   # keeps the file's owner and mode

cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-last-run-$STAMP"
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
systemctl restart clock-sync-web
systemctl is-active clock-sync-web
echo "bot_name: $NAME"
echo "Done. Reload the page."
