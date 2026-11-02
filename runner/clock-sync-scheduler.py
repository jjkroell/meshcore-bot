#!/usr/bin/env python3
"""Run the clock sync when the web page's schedule says so.

Started every minute (as root) by clock-sync-scheduler.timer. Replaces the old
/etc/cron.d/clock-sync line, which the unprivileged web page couldn't edit.
Each slot runs once: the last slot started is kept in LAST_FILE. If another
clock sync holds the radio at the slot, it tries again next minute (within
schedule.GRACE_MINUTES) instead of losing the run.
"""

import fcntl
import os
import sys
import time

sys.path.insert(0, "/opt/clock_sync")
import schedule  # noqa: E402

LAST_FILE = "/var/lib/clock_sync/last-scheduled-slot"
LOCK = "/run/clock_sync.lock"
LOG = "/var/log/clock_sync/clock_sync.log"
WRAPPER = "/opt/clock_sync/run-clock-sync.sh"


def radio_busy() -> bool:
    try:
        fd = os.open(LOCK, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def main() -> int:
    sched, _ = schedule.load()
    slot = schedule.due_slot(sched)
    if slot is None:
        return 0
    key = slot.strftime("%Y-%m-%d %H:%M")
    try:
        with open(LAST_FILE) as f:
            if f.read().strip() == key:
                return 0  # already started this one
    except OSError:
        pass
    if radio_busy():
        return 0  # a manual run has the radio; try again next minute
    os.makedirs(os.path.dirname(LAST_FILE), exist_ok=True)
    with open(LAST_FILE, "w") as f:
        f.write(key + "\n")
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} run-clock-sync: scheduled run "
                f"({slot.strftime('%a %H:%M')}; {', '.join(d.capitalize() for d in sched['days'])})\n")
    os.execv(WRAPPER, [WRAPPER])


if __name__ == "__main__":
    sys.exit(main())
