#!/bin/bash
# Turn off the wc / worldcup command.
#   sudo bash disable-worldcup.sh
# Roll back: the config.ini.before-worldcup-off-* copy in /opt/meshcore-bot/
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

CONFIG=${CONFIG:-/opt/meshcore-bot/config.ini}
cp -a "$CONFIG" "$CONFIG.before-worldcup-off-$(date +%Y%m%d-%H%M%S)"
python3 - "$CONFIG" <<'PY'
import re, sys
path = sys.argv[1]
lines = open(path, encoding='utf-8').read().splitlines(keepends=True)
start = next((i for i, l in enumerate(lines) if l.strip() == '[Worldcup_Command]'), None)
if start is None:
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    lines += ['\n', '[Worldcup_Command]\n', 'enabled = false\n']
else:
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('[')), len(lines))
    found = [i for i in range(start + 1, end) if re.match(r'enabled\s*[=:]', lines[i])]
    for i in found:
        lines[i] = 'enabled = false\n'
    if not found:
        lines.insert(start + 1, 'enabled = false\n')
open(path, 'w', encoding='utf-8').writelines(lines)
PY
sed -n '/^\[Worldcup_Command\]/,/^\[/p' "$CONFIG" | grep -E '^enabled'
[ -n "${CONFIG_TEST:-}" ] || systemctl restart meshcore-bot
echo "Done. wc and worldcup are off."
