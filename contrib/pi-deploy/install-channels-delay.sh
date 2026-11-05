#!/bin/bash
# channels waits 1 second between the parts of its reply.
#   sudo bash install-channels-delay.sh
# Roll back: the channels_command.py.before-channels-delay-* copy in /opt/meshcore-bot/modules/commands/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

DEST=/opt/meshcore-bot/modules/commands/channels_command.py
EXPECTED=558a65cd43f273a301fbb253e24d81ce70330e761323f92a68fc84c16376b2dd
if [ "$(sha256sum "$DEST" | cut -d' ' -f1)" != "$EXPECTED" ]; then
    echo "$DEST is not the version this change was built on - nothing changed"; exit 1
fi
python3 -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" channels_command.py

cp -a "$DEST" "$DEST.before-channels-delay-$(date +%Y%m%d-%H%M%S)"
install -o meshcore -g meshcore -m 664 channels_command.py "$DEST"
systemctl restart meshcore-bot
echo "Done. Give the bot about 30 seconds, then try channels."
