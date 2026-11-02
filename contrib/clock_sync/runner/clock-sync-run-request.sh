#!/bin/bash
# Started (as root) by clock-sync-run.path when the web page drops a run
# request. The request is one node name per line (empty file = every node);
# each becomes a separate --only argument - never evaluated as shell.
set -u
REQ=/etc/clock_sync/run-request
LOG=/var/log/clock_sync/clock_sync.log
[ -f "$REQ" ] || exit 0
mapfile -t lines < <(head -n 50 "$REQ" | cut -c1-100)
rm -f "$REQ"

args=()
names=()
for n in "${lines[@]}"; do
    n="${n%$'\r'}"
    [ -n "$n" ] || continue
    args+=(--only "$n")
    names+=("$n")
done
if [ ${#names[@]} -eq 0 ]; then what="all nodes"; else what=$(printf '%s, ' "${names[@]}"); what=${what%, }; fi
echo "$(date -Is) run-clock-sync: run requested from the web page: $what" >>"$LOG"
exec /opt/clock_sync/run-clock-sync.sh "${args[@]}"
