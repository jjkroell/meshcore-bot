#!/bin/bash
# Wait longer for a login reply:  sudo bash install-login-timeout.sh [seconds]   (default 30)
# Sets login_timeout_seconds in clock_sync.yaml; nothing else in the file changes.
# Roll back: copy the /root/clock_sync.yaml.before-login-timeout-* file back over it.
set -euo pipefail
CONF=/etc/clock_sync/clock_sync.yaml
SECS=${1:-30}
[[ "$SECS" =~ ^[0-9]+$ ]] || { echo "seconds must be a whole number"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

echo "Before: $(grep -E '^login_timeout_seconds:' "$CONF" || echo 'not set (default 20)')"
BACKUP="/root/clock_sync.yaml.before-login-timeout-$(date +%Y%m%d-%H%M%S)"
cp -a "$CONF" "$BACKUP"

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
if grep -qE '^login_timeout_seconds:' "$CONF"; then
    sed -E "s/^login_timeout_seconds:.*/login_timeout_seconds: $SECS/" "$CONF" > "$TMP"
elif grep -qE '^repeaters:' "$CONF"; then
    sed -E "0,/^repeaters:/s//login_timeout_seconds: $SECS\n&/" "$CONF" > "$TMP"
else
    echo "no repeaters: section in $CONF - nothing changed"; exit 1
fi

# Check the edited copy before it replaces the real file.
/opt/clock_sync/venv/bin/python -I - "$TMP" "$SECS" "$CONF" <<'PY'
import sys, yaml
new, secs, conf = sys.argv[1], int(sys.argv[2]), sys.argv[3]
a, b = yaml.safe_load(open(new, encoding="utf-8")), yaml.safe_load(open(conf, encoding="utf-8"))
assert a.get("login_timeout_seconds") == secs, "value not set"
a.pop("login_timeout_seconds"); b.pop("login_timeout_seconds", None)
assert a == b, "something else changed"
PY

cat "$TMP" > "$CONF"   # keeps the file's owner and mode
echo "After:  $(grep -E '^login_timeout_seconds:' "$CONF")"
echo "Backup: $BACKUP"
echo "Done. Test it: on the page, press Run on NIARS Newcastle."
