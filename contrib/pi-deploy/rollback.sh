#!/bin/bash
# Put a deploy.sh backup back:  sudo bash rollback.sh /opt/meshcore-bot.before-<time>
# Restores everything as it was at that moment - code, config, database and
# venv - so anything the bot logged since is lost (the current install is kept
# beside it as /opt/meshcore-bot.rolled-back-<time>).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
BACKUP="${1:-}"
DEST=/opt/meshcore-bot
[ -n "$BACKUP" ] && [ -f "$BACKUP/meshcore_bot.py" ] || {
    echo "usage: sudo bash rollback.sh /opt/meshcore-bot.before-<time>"; ls -d $DEST.before-* 2>/dev/null; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }
STAMP=$(date +%Y%m%d-%H%M%S)
systemctl stop meshcore-bot
mv "$DEST" "$DEST.rolled-back-$STAMP"
cp -a "$BACKUP" "$DEST"
systemctl start meshcore-bot
sleep 15
systemctl --no-pager --lines=10 status meshcore-bot | head -15
echo "Rolled back to $BACKUP (the install you left is $DEST.rolled-back-$STAMP)."
