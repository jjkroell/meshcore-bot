#!/bin/bash
# Channel read fix: a late radio reply can no longer put a channel in the wrong slot.
#   sudo bash install-channel-fix.sh
# Install it with the radio unplugged, then plug the radio in.
# Roll back: the channel_manager.py.before-channel-fix-* copy in /opt/meshcore-bot/modules/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

DEST=/opt/meshcore-bot/modules/channel_manager.py
EXPECTED=9192a4e929894f41a80cc2320c4c6a66b22b20a53cdd4bc62e0bad61272de684
if [ "$(sha256sum "$DEST" | cut -d' ' -f1)" != "$EXPECTED" ]; then
    echo "$DEST is not the version this fix was built on - nothing changed"; exit 1
fi
python3 -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" channel_manager.py

cp -a "$DEST" "$DEST.before-channel-fix-$(date +%Y%m%d-%H%M%S)"
install -o meshcore -g meshcore -m 664 channel_manager.py "$DEST"
systemctl restart meshcore-bot
echo "Done. Now plug the radio in: the bot picks it up within about 30 seconds."
