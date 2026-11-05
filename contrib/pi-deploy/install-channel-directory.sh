#!/bin/bash
# channels lists a Ridgeline server's channels busiest first; bots lists the bot and test ones.
#   sudo bash install-channel-directory.sh https://your.ridgeline.server
# Roll back: the *.before-channel-directory-* copies next to each file, then delete
# bots_command.py and clients/channel_directory.py.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
URL=${1:-}
case "$URL" in https://*) ;; *) echo "give the Ridgeline address, starting https://"; exit 1 ;; esac
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

BASE=/opt/meshcore-bot/modules
CONFIG=${CONFIG:-/opt/meshcore-bot/config.ini}
check() {
    if [ "$(sha256sum "$BASE/$1" | cut -d' ' -f1)" != "$2" ]; then
        echo "$BASE/$1 is not the version this change was built on - nothing changed"; exit 1
    fi
}
check commands/channels_command.py bb8f39e1962b1b3aa4bf934e326849690560ba7d2f41665bb8a8b05cf133a8d6
check commands/cmd_command.py 13bf994f0471b905f700075e1880fb3dcf7d9a46ef2fbb66df8574e8ab9d3f81
for f in channel_directory.py bots_command.py channels_command.py cmd_command.py; do
    python3 -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$f"
done

STAMP=$(date +%Y%m%d-%H%M%S)
cp -a "$CONFIG" "$CONFIG.before-channel-directory-$STAMP"
for f in channels_command.py cmd_command.py; do
    cp -a "$BASE/commands/$f" "$BASE/commands/$f.before-channel-directory-$STAMP"
done
install -o meshcore -g meshcore -m 664 channel_directory.py "$BASE/clients/channel_directory.py"
for f in bots_command.py channels_command.py cmd_command.py; do
    install -o meshcore -g meshcore -m 664 "$f" "$BASE/commands/$f"
done

python3 - "$CONFIG" "$URL" <<'PY'
import re, sys
path, url = sys.argv[1:3]
lines = open(path, encoding='utf-8').read().splitlines(keepends=True)
start = next((i for i, l in enumerate(lines) if l.strip() == '[Channels_Command]'), None)
if start is None:
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    lines += ['\n', '[Channels_Command]\n']
    start = len(lines) - 1
end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('[')), len(lines))
kept = [l for l in lines[start + 1:end] if not re.match(r'directory_url\s*[=:]', l)]
lines[start + 1:end] = [f'directory_url = {url}\n'] + kept
open(path, 'w', encoding='utf-8').writelines(lines)
PY
sed -n '/^\[Channels_Command\]/,/^\[/p' "$CONFIG" | grep -E '^directory_'
[ -n "${CONFIG_TEST:-}" ] || systemctl restart meshcore-bot
echo "Done. Give the bot about 30 seconds, then try channels and bots."
