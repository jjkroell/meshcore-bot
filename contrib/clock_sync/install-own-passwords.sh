#!/bin/bash
# Every node uses its own admin password:  sudo bash install-own-passwords.sh
#  - each node in /etc/clock_sync/clock_sync.yaml that had no password of its
#    own gets a copy of the shared admin_password, then the shared one is
#    deleted from the file (nodes with their own keep it)
#  - clock_sync.py: no shared password; a node without one is NO_PASSWORD
#  - the page: a password is required to add a node; no "own password" chip,
#    no "use shared"; a node without one shows "no password"
# Roll back: the copies ending .before-own-passwords-<time> (config in /root,
# code next to the installed files)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
exec 9>>/run/clock_sync.lock
flock -n 9 || { echo "a clock sync is running - wait for it to finish"; exit 1; }

CFG=/etc/clock_sync/clock_sync.yaml
STAMP=$(date +%Y%m%d-%H%M%S)
cp -a "$CFG" "/root/clock_sync.yaml.before-own-passwords-$STAMP"
cp -a /opt/clock_sync/clock_sync.py "/opt/clock_sync/clock_sync.py.before-own-passwords-$STAMP"
cp -a /opt/clock_sync_web/clock_sync_web.py "/opt/clock_sync_web/clock_sync_web.py.before-own-passwords-$STAMP"

# 1. the config (with the NEW web module's careful rewrite of the repeaters list)
/opt/clock_sync/venv/bin/python -I - "$CFG" web/clock_sync_web.py <<'PY'
import importlib.util, os, re, sys, yaml
cfg_path, mod_path = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("csw", mod_path)
csw = importlib.util.module_from_spec(spec); spec.loader.exec_module(csw)

text, cfg, block = csw.read_config(cfg_path)
shared = cfg.get("admin_password")
reps = cfg["repeaters"]
missing = [r for r in reps if not r.get("admin_password")]
if shared is None:
    if missing:
        sys.exit("no shared admin_password in the config, and these nodes have none: "
                 + ", ".join(str(r.get("name")) for r in missing))
    print("Config already done: every node has its own password, no shared one.")
    sys.exit(0)
shared = str(shared)

ops = [{"id": i, "name": r.get("name") or "", "pubkey": str(r.get("pubkey_prefix") or ""),
        **({} if r.get("admin_password") else {"password": shared})} for i, r in enumerate(reps)]
out, new = csw.apply_ops(text, cfg, block, ops)

# Drop the top-level `admin_password:` line (top level = not indented).
lines = out.splitlines(keepends=True)
top = [i for i, l in enumerate(lines) if re.match(r"^admin_password\s*:", l)]
if len(top) != 1:
    sys.exit(f"expected one top-level admin_password line, found {len(top)} - nothing changed")
del lines[top[0]]
out = "".join(lines)

# Check: same settings minus the shared password, every node has one, and each
# node that had its own still has exactly that.
parsed = yaml.safe_load(out)
want = {k: v for k, v in cfg.items() if k not in ("admin_password", "repeaters")}
if {k: v for k, v in parsed.items() if k != "repeaters"} != want:
    sys.exit("check failed: another setting would change - nothing changed")
if len(parsed["repeaters"]) != len(reps):
    sys.exit("check failed: the node count would change - nothing changed")
for old, nw in zip(reps, parsed["repeaters"]):
    if not nw.get("admin_password") or {k: v for k, v in nw.items() if k != "admin_password"} != \
            {k: v for k, v in old.items() if k != "admin_password"} or \
            (old.get("admin_password") and nw["admin_password"] != old["admin_password"]):
        sys.exit(f"check failed on {old.get('name')} - nothing changed")

st = os.stat(cfg_path)
csw.write_config(cfg_path, out, text)
os.chown(cfg_path, st.st_uid, st.st_gid)  # root wrote it; the page must still read it
masked = shared[:2] + "*" * max(0, len(shared) - 2)
print(f"Config: {len(missing)} of {len(reps)} nodes given the old shared password ({masked}), "
      f"{len(reps) - len(missing)} kept their own; shared password removed.")
for r in missing:
    print("   ", r.get("name"))
PY

# 2. the code
install -m 664 clock_sync/clock_sync.py /opt/clock_sync/clock_sync.py
install -m 644 web/clock_sync_web.py /opt/clock_sync_web/clock_sync_web.py
systemctl restart clock-sync-web
sleep 3
/opt/clock_sync/venv/bin/python -I -c "
import sys; sys.path.insert(0, '/opt/clock_sync')
import clock_sync
cfg = clock_sync.load_config('$CFG')
no = [r.get('name') for r in cfg['repeaters'] if not r.get('admin_password')]
print('clock_sync loads the config:', len(cfg['repeaters']), 'nodes,', 'all have a password' if not no else 'NO PASSWORD: ' + ', '.join(no))"
ls -l "$CFG"
systemctl is-active clock-sync-web
echo "Done. Reload the page: no 'own password' chips, and Add asks for a password."
