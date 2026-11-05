#!/bin/bash
# Channel list editor in the web UI: shows and accepts #names, stores them without the #.
#   sudo bash install-channel-hash.sh
# Roll back: the *.before-channel-hash-* copies next to each file in /opt/meshcore-bot/modules/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

BASE=/opt/meshcore-bot/modules
check() {
    if [ "$(sha256sum "$BASE/$1" | cut -d' ' -f1)" != "$2" ]; then
        echo "$BASE/$1 is not the version this change was built on - nothing changed"; exit 1
    fi
}
check commands/channels_command.py 4554cc549db8a845741b846acbc00cb819c9a772a3ae7c41f942b0ed1595d452
check settings_schema.py cddfae6d419d46843ec4bec03acc1cbed91e6794094751f7c54d0e5dc0490aad
check web_viewer/app.py 008af9cd9daaa423a9bac29d182b5a24d792bf1973d3e4c3f123068ed31b96f2
for f in channels_command.py settings_schema.py app.py; do
    python3 -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$f"
done

STAMP=$(date +%Y%m%d-%H%M%S)
put() {
    cp -a "$BASE/$2" "$BASE/$2.before-channel-hash-$STAMP"
    install -o meshcore -g meshcore -m 664 "$1" "$BASE/$2"
}
put channels_command.py commands/channels_command.py
put settings_schema.py settings_schema.py
put app.py web_viewer/app.py
systemctl restart meshcore-bot
echo "Done. Give the bot about 30 seconds, then reload the web UI."
