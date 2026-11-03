#!/bin/bash
# Update /opt/meshcore-bot from a copy of this repo:  sudo bash deploy.sh
#
# Run from the staged copy (contrib/pi-deploy/ inside it). It:
#   1. waits for no clock sync to be running, then stops meshcore-bot
#   2. backs up the whole install (code, config, database, venv) to
#      /opt/meshcore-bot.before-<time>  - rollback.sh puts it back
#   3. copies the repo's files over the install. Never touched: config.ini,
#      local/, logs, *.db, venv, data you added. Files the repo deleted since
#      the last deploy (listed in .deployed-files) are removed.
#   4. installs requirements.txt into the venv
#   5. starts the bot and checks it came up (database migrations run on start)
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
DEST="${DEPLOY_DEST:-/opt/meshcore-bot}"  # override only for testing
OWNER="${DEPLOY_OWNER:-meshcore}"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -f "$SRC/meshcore_bot.py" ] || { echo "$SRC doesn't look like a meshcore-bot tree"; exit 1; }
[ -d "$DEST" ] || { echo "no $DEST"; exit 1; }

# One radio user at a time: hold the clock sync's lock for the whole update.
exec 9>>"${DEPLOY_LOCK:-/run/clock_sync.lock}"
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$DEST.before-$STAMP"
echo "== stopping meshcore-bot"
systemctl stop meshcore-bot
trap 'echo "!! failed - the bot is STOPPED. Undo with: sudo bash $SRC/contrib/pi-deploy/rollback.sh $BACKUP"' ERR

echo "== backup -> $BACKUP"
cp -a "$DEST" "$BACKUP"
du -sh "$BACKUP" | cut -f1

echo "== files"
cd "$SRC"
NEW_LIST=$(mktemp)
if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$SRC" ls-files > "$NEW_LIST"
else
    [ -f "$SRC/.release-files" ] || { echo "no git and no .release-files list"; exit 1; }
    cp "$SRC/.release-files" "$NEW_LIST"
fi
# never ship these over the live install
grep -vE '^(config\.ini|local/|logs/|venv/|.*\.db)$|^local/|^logs/|^venv/' "$NEW_LIST" > "$NEW_LIST.f"
rsync -a --chown="$OWNER:$OWNER" --files-from="$NEW_LIST.f" "$SRC/" "$DEST/"
echo "copied $(wc -l < "$NEW_LIST.f") files"
# Remove what an earlier deploy put there and this tree no longer has. The
# first deploy has no record yet; the staged copy can carry the old tree's list.
if [ ! -f "$DEST/.deployed-files" ] && [ -f "$SRC/.release-previous-files" ]; then
    cp "$SRC/.release-previous-files" "$DEST/.deployed-files"
fi
if [ -f "$DEST/.deployed-files" ]; then
    removed=0
    while IFS= read -r f; do
        case "$f" in ""|/*|*..*|config.ini|local/*|logs/*|venv/*|*.db) continue;; esac
        if ! grep -qxF -- "$f" "$NEW_LIST.f" && [ -f "$DEST/$f" ]; then
            rm -f -- "$DEST/$f"; echo "   removed $f"; removed=$((removed+1))
        fi
    done < "$DEST/.deployed-files"
    echo "removed $removed file(s) the repo dropped"
fi
install -m 644 -o "$OWNER" -g "$OWNER" "$NEW_LIST.f" "$DEST/.deployed-files"
rm -f "$NEW_LIST" "$NEW_LIST.f"

echo "== python packages"
sudo -u "$OWNER" "$DEST/venv/bin/pip" install -q --disable-pip-version-check -r "$DEST/requirements.txt"
sudo -u "$OWNER" "$DEST/venv/bin/pip" list 2>/dev/null | grep -iE '^(meshcore|meshcore-cli|flask-compress|apscheduler) ' || true

echo "== starting meshcore-bot"
trap - ERR
systemctl start meshcore-bot
START=$(date '+%Y-%m-%d %H:%M:%S')
for i in $(seq 1 18); do  # up to 3 min for migrations + radio connect
    sleep 10
    systemctl is-active -q meshcore-bot || break
    journalctl -u meshcore-bot --since "$START" --no-pager -q | grep -qF 'Bot is running' && break
done
echo "-- last log lines:"
journalctl -u meshcore-bot --since "$START" --no-pager -q | tail -n 25
if ! systemctl is-active -q meshcore-bot; then
    echo "!! meshcore-bot is NOT running. Undo with: sudo bash $SRC/contrib/pi-deploy/rollback.sh $BACKUP"
    exit 1
fi
if journalctl -u meshcore-bot --since "$START" --no-pager -q | grep -qE 'Traceback|CRITICAL'; then
    echo "!! running, but the log has a Traceback/CRITICAL above - check it. Undo: sudo bash $SRC/contrib/pi-deploy/rollback.sh $BACKUP"
    exit 1
fi
echo
echo "Done: meshcore-bot is running the new code. Backup: $BACKUP"
echo "Undo: sudo bash $SRC/contrib/pi-deploy/rollback.sh $BACKUP"
