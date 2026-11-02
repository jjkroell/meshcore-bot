#!/bin/bash
# Schedule editor on the web page (like PMD-TimeSync's):  sudo bash install-scheduler.sh
#  - /etc/clock_sync/schedule.json: days, time, skip window, pause - edited on the page
#  - clock-sync-scheduler.timer (root, every minute) starts the run when a slot is due
#  - REMOVES /etc/cron.d/clock-sync (the old every-other-day 04:00 line; backed up)
# Initial schedule: Mon, Wed, Fri at 04:00 (about the old cadence), skip window from the config.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/clock_sync/clock_sync.py "/opt/clock_sync/clock_sync.py.before-sched-$STAMP"
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-sched-$STAMP"
[ -f /etc/cron.d/clock-sync ] && cp -a /etc/cron.d/clock-sync "/root/cron.d-clock-sync.before-sched-$STAMP"

install -m 664 clock_sync/clock_sync.py /opt/clock_sync/clock_sync.py
install -m 644 clock_sync/schedule.py /opt/clock_sync/schedule.py
install -m 644 clock_sync/peer_parse.py /opt/clock_sync/peer_parse.py
install -m 755 runner/clock-sync-scheduler.py /opt/clock_sync/clock-sync-scheduler.py
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
install -m 644 runner/clock-sync-scheduler.service runner/clock-sync-scheduler.timer /etc/systemd/system/
install -d -m 755 /var/lib/clock_sync

if [ ! -f /etc/clock_sync/schedule.json ]; then
    /opt/clock_sync/venv/bin/python -I - <<'PY'
import sys, yaml
sys.path.insert(0, "/opt/clock_sync")
import schedule
cfg = yaml.safe_load(open("/etc/clock_sync/clock_sync.yaml")) or {}
s = schedule.save({**schedule.DEFAULT, "peer_skip_hours": cfg.get("peer_skip_hours", 24)})
print("schedule.json:", s)
PY
    chown root:clocksync-web /etc/clock_sync/schedule.json
fi

# Hand over from cron to the timer. Don't let the new timer re-run a slot the
# old cron already ran today.
rm -f /etc/cron.d/clock-sync
systemctl daemon-reload
systemctl enable --now clock-sync-scheduler.timer
systemctl restart clock-sync-web
sleep 3
systemctl is-active clock-sync-scheduler.timer clock-sync-web
/opt/clock_sync/venv/bin/python -I -c "
import sys; sys.path.insert(0, '/opt/clock_sync'); import schedule
s, f = schedule.load(); n = schedule.next_run(s)
print('schedule', s, '| next run', n.strftime('%a %b %d %H:%M') if n else 'none')"
echo "Done. The Schedule card is on the page under the Run all bar. The old cron line is gone (backup in /root)."
