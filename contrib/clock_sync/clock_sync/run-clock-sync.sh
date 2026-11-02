#!/bin/bash
# Every-other-day MeshCore repeater clock sync.
#
# openHop was removed 2026-08-31. The companion radio is a USB device owned by
# meshcore-bot, and a USB companion serves only one client at a time, so this
# wrapper stops the bot for the duration of the run to get exclusive serial
# access, then hands the radio back. The bot is restarted no matter how this
# script exits (trap on EXIT).
#
# Extra arguments go to clock_sync.py, e.g. a manual run of two nodes:
#   run-clock-sync.sh --only "KOD - Little Mountain" --only "KOD - Mt Shepherd"
set -u

LOG=/var/log/clock_sync/clock_sync.log
PY=/opt/clock_sync/venv/bin/python
SCRIPT=/opt/clock_sync/clock_sync.py
CONFIG=/etc/clock_sync/clock_sync.yaml
MAX_SECONDS=2400  # hard cap: 14 nodes * (~40s timeouts + 30s delay) worst case

mkdir -p "$(dirname "$LOG")"
log() { echo "$(date -Is) run-clock-sync: $*" >>"$LOG"; }

# One run at a time: cron, a manual run and the web page's "Run now" must
# never share the radio. The web page tests this lock to show "running".
LOCK=/run/clock_sync.lock
exec 9>>"$LOCK"
chmod 644 "$LOCK"
if ! flock -n 9; then
    log "another clock sync is already running - not starting this one"
    exit 75
fi

log "stopping meshcore-bot"
systemctl stop meshcore-bot
trap 'log "restarting meshcore-bot"; systemctl start meshcore-bot' EXIT

sleep 3  # let the kernel release /dev/ttyACM*

timeout "$MAX_SECONDS" "$PY" "$SCRIPT" --config "$CONFIG" --log-file "$LOG" "$@"
rc=$?
log "clock_sync.py exited rc=$rc"
exit "$rc"
