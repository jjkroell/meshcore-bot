#!/bin/bash
# channels drops the Common channels label from its reply.
#   sudo bash install-channels-nolabel.sh
# Roll back: the channels_command.py.before-channels-nolabel-* copy in /opt/meshcore-bot/modules/commands/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

DEST=/opt/meshcore-bot/modules/commands/channels_command.py
EXPECTED=ffc528c10d1b742095f8c91366f4e4fe82ab7594af5f2d74d632bff1c025cce0
if [ "$(sha256sum "$DEST" | cut -d' ' -f1)" != "$EXPECTED" ]; then
    echo "$DEST is not the version this change was built on - nothing changed"; exit 1
fi
python3 -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" channels_command.py

cp -a "$DEST" "$DEST.before-channels-nolabel-$(date +%Y%m%d-%H%M%S)"
install -o meshcore -g meshcore -m 664 channels_command.py "$DEST"
systemctl restart meshcore-bot
echo "Done. Give the bot about 30 seconds, then try channels."
