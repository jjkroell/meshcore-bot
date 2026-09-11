#!/usr/bin/env bash
set -euo pipefail

# bootstrap.sh — run ONCE, over SSH, on a freshly-imaged Raspberry Pi OS
# Lite (64-bit) Zero 2W, before any USB hardware is moved over from the Pi 5.
#
# Handles: OS packages, the `meshcore` service user, cloning the bot repo,
# both venvs, the systemd unit, and a tightened memory limit for the
# Zero 2W's 512MB of RAM.
#
# Deliberately does NOT touch (all involve secrets or live data — copy these
# by hand, see the printed instructions at the end):
#   - config.ini
#   - meshcore_bot.db and data/
#   - clock_sync.py, run-clock-sync.sh, state.json (not in git)
#   - the cron.d entry
#   - moving the USB hub/radios over
#
# Usage:
#   REPO_URL=https://github.com/jjkroell/meshcore-bot ./bootstrap.sh
# (your fork — has everything: tide/ferry/fire, web viewer, catfact/dogfact,
# the 1-byte path-notice patch, and the @[{sender}] response-format examples.
# If it's private, copy ~/.git-credentials and ~/.gitconfig from the Pi 5
# first, or set up a fresh GitHub token on the Zero 2W before running this.)

REPO_URL="${REPO_URL:-}"
BOT_DIR=/opt/meshcore-bot
CLOCK_DIR=/opt/clock_sync

if [ -z "$REPO_URL" ]; then
  echo "Set REPO_URL to your meshcore-bot fork first, e.g.:" >&2
  echo "  REPO_URL=https://github.com/jjkroell/meshcore-bot $0" >&2
  echo "(Private repo? Copy ~/.git-credentials + ~/.gitconfig from the Pi 5 first, or set up a token here.)" >&2
  exit 1
fi

echo "==> Updating base system"
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-pip python3-dev sqlite3 libdbus-1-dev build-essential

echo "==> Checking piwheels is active (avoids compiling cryptography/pynacl from source on-device)"
pip config list 2>/dev/null | grep -qi piwheels \
  && echo "  piwheels OK" \
  || echo "  NOTE: piwheels not found in pip config — those installs may compile from source and be slow/memory-heavy."

echo "==> Creating meshcore service user"
if ! id meshcore &>/dev/null; then
  sudo groupadd --system meshcore
  sudo useradd --system --gid meshcore --groups dialout,tty,uucp \
    --home-dir "$BOT_DIR" --no-create-home --shell /usr/sbin/nologin meshcore
fi

echo "==> Cloning meshcore-bot into $BOT_DIR"
sudo mkdir -p "$BOT_DIR"
sudo git clone "$REPO_URL" "$BOT_DIR" 2>/dev/null || (cd "$BOT_DIR" && sudo git pull)
sudo chown -R meshcore:meshcore "$BOT_DIR"

echo "==> Building meshcore-bot venv (this is the slow step on a Zero 2W — let it run)"
sudo -u meshcore python3 -m venv "$BOT_DIR/venv"
sudo -u meshcore "$BOT_DIR/venv/bin/pip" install --upgrade pip
sudo -u meshcore "$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt"

echo "==> Setting up /var/log/meshcore-bot"
sudo mkdir -p /var/log/meshcore-bot
sudo chown meshcore:meshcore /var/log/meshcore-bot

echo "==> Installing systemd unit (not starting/enabling yet — no config.ini or radio present)"
sudo cp "$BOT_DIR/meshcore-bot.service" /etc/systemd/system/meshcore-bot.service
sudo systemctl daemon-reload

echo "==> Tightening the service's memory ceiling for the Zero 2W's 512MB total RAM"
# The Pi 5's unit has MemoryMax=512M, which is the *entire* machine's RAM here
# and leaves nothing for the OS, clock_sync, or page cache. Bot+web_viewer
# together run ~200MB RSS on the Pi 5; 350M gives headroom without letting a
# leak take down the whole board. Tune after watching real usage.
sudo mkdir -p /etc/systemd/system/meshcore-bot.service.d
cat <<'EOF' | sudo tee /etc/systemd/system/meshcore-bot.service.d/override.conf >/dev/null
[Service]
MemoryMax=350M
EOF
sudo systemctl daemon-reload

echo "==> Preparing clock_sync venv"
# clock_sync.py itself isn't in git (root-owned, /opt-only on the Pi 5), so
# there's no requirements.txt to install from. These exact versions were
# read off the Pi 5's installed venv so both boards match:
sudo mkdir -p "$CLOCK_DIR"
sudo python3 -m venv "$CLOCK_DIR/venv"
sudo "$CLOCK_DIR/venv/bin/pip" install --upgrade pip
sudo "$CLOCK_DIR/venv/bin/pip" install \
  pyserial==3.5 \
  pyserial-asyncio-fast==0.16 \
  bleak==3.0.2 \
  dbus_fast==5.0.22 \
  meshcore==2.3.7 \
  pycayennelpp==2.4.0 \
  pycryptodome==3.23.0 \
  pyyaml==6.0.3

cat <<EOF

==============================================================================
Base setup done. Still to do by hand (all involve secrets or live data, so
this script intentionally didn't touch them):

1. FROM the Pi 5 (not here), vacuum the db to shrink it, then copy the
   runtime state over (replace <zero2w-host> with this Pi's hostname/IP):

     sudo sqlite3 /opt/meshcore-bot/meshcore_bot.db "VACUUM;"
     sudo scp /opt/meshcore-bot/config.ini      <zero2w-host>:/tmp/
     sudo scp -r /opt/meshcore-bot/data         <zero2w-host>:/tmp/
     sudo scp /opt/meshcore-bot/meshcore_bot.db <zero2w-host>:/tmp/
     sudo scp /opt/clock_sync/clock_sync.py     <zero2w-host>:/tmp/
     sudo scp /opt/clock_sync/run-clock-sync.sh <zero2w-host>:/tmp/
     sudo scp /opt/clock_sync/state.json        <zero2w-host>:/tmp/

2. HERE on the Zero 2W, move them into place and fix ownership:

     sudo mv /tmp/config.ini /tmp/meshcore_bot.db $BOT_DIR/
     sudo mv /tmp/data $BOT_DIR/
     sudo chown -R meshcore:meshcore $BOT_DIR/config.ini $BOT_DIR/data $BOT_DIR/meshcore_bot.db
     sudo mv /tmp/clock_sync.py /tmp/run-clock-sync.sh /tmp/state.json $CLOCK_DIR/
     sudo chmod +x $CLOCK_DIR/run-clock-sync.sh

3. Re-create the cron entry (this is what's on the Pi 5 — matches
   /etc/cron.d/clock-sync):

     echo '0 4 1-31/2 * * root /opt/clock_sync/run-clock-sync.sh' | sudo tee /etc/cron.d/clock-sync

4. Once the RAK4631 is attached via your OTG adapter, confirm it enumerates
   the same way it does on the Pi 5:

     ls -la /dev/serial/by-id/
     # expect: usb-RAKwireless_WisCore_RAK4631_Board_015966DE670DDBAD-if00 -> ../../ttyACM0

   If config.ini references /dev/ttyACM0 directly, that's fine as long as
   it's the only serial device ever attached (true here) — no hub-induced
   renumbering risk to worry about.

5. Only after 1-4:

     sudo systemctl enable --now meshcore-bot
     journalctl -u meshcore-bot -f

   Then do a manual dry-run of run-clock-sync.sh to confirm it can see the
   RAK4631 before waiting for the next scheduled 04:00 run.
==============================================================================
EOF
