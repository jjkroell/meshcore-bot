#!/bin/bash
# cmd lists every command the sender can use, over as many messages as it takes.
#   sudo bash install-cmd-list.sh
# Roll back: the cmd_command.py.before-cmd-list-* copy in /opt/meshcore-bot/modules/commands/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

DEST=/opt/meshcore-bot/modules/commands/cmd_command.py
EXPECTED=b171bbf40e267076d3e839851635683241c22064583ac9244e37323c49ec6ef0
if [ "$(sha256sum "$DEST" | cut -d' ' -f1)" != "$EXPECTED" ]; then
    echo "$DEST is not the version this change was built on - nothing changed"; exit 1
fi
python3 -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" cmd_command.py

cp -a "$DEST" "$DEST.before-cmd-list-$(date +%Y%m%d-%H%M%S)"
install -o meshcore -g meshcore -m 664 cmd_command.py "$DEST"
systemctl restart meshcore-bot
echo "Done. Send cmd on #kod-bot once the bot is back."
