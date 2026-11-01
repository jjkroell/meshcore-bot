#!/bin/bash
# One-shot installer, run from this folder on the bot Pi:  sudo bash install.sh
#  1. meshcore-bot watchdog (restarts the bot if it hears nothing for 30 min)
#  2. persistent journal (logs survive a reboot / power cycle)
#  3. clock-sync web editor on http://<pi>:8090/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

echo "== 1. watchdog"
install -m 755 watchdog/meshcore-bot-watchdog.py /usr/local/sbin/meshcore-bot-watchdog.py
install -m 644 watchdog/meshcore-bot-watchdog.service watchdog/meshcore-bot-watchdog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now meshcore-bot-watchdog.timer
systemctl start meshcore-bot-watchdog.service   # one check now (bot is healthy: does nothing)
journalctl -u meshcore-bot-watchdog.service -n 3 --no-pager || true

echo "== 2. persistent journal"
install -d -m 755 /etc/systemd/journald.conf.d
install -m 644 watchdog/journald-persistent.conf /etc/systemd/journald.conf.d/persistent.conf
install -d -m 2755 -g systemd-journal /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
journalctl --flush
du -sh /var/log/journal

echo "== 3. clock-sync web editor"
id clocksync-web >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin clocksync-web
install -d -m 755 /opt/clock_sync_web
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
cp -a /etc/clock_sync/clock_sync.yaml /root/clock_sync.yaml.before-web-$(date +%Y%m%d-%H%M%S)
chown root:clocksync-web /etc/clock_sync /etc/clock_sync/clock_sync.yaml
chmod 770 /etc/clock_sync
chmod 660 /etc/clock_sync/clock_sync.yaml

echo "-- dry run (nothing written; secrets redacted):"
if ! sudo -u clocksync-web /opt/clock_sync/venv/bin/python /opt/clock_sync_web/clock_sync_web.py --check; then
    echo "!! the editor can't safely handle this config - NOT starting the web UI. Paste the output above to Claude."
    exit 1
fi

if [ ! -s /etc/clock_sync/web_auth.json ]; then
    echo "-- choose a password for the web page (8+ characters):"
    /opt/clock_sync/venv/bin/python /opt/clock_sync_web/clock_sync_web.py --set-password
    chown root:clocksync-web /etc/clock_sync/web_auth.json
    chmod 640 /etc/clock_sync/web_auth.json
fi

install -m 644 web/clock-sync-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now clock-sync-web
sleep 2
systemctl --no-pager --lines=3 status clock-sync-web | head -12
echo "-- login check (expect 401): $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/)"
echo
echo "Done. Open http://$(hostname -I | awk '{print $1}'):8090/ (any username, the password you just set)."
