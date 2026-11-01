#!/usr/bin/env python3
"""Restart meshcore-bot when it has gone deaf.

On 2026-11-01 the bot stopped hearing the radio at 06:42 and sat there until
a power cycle at 10:14: the process stayed up (so systemd's Restart=always
never fired) while its main loop was stuck. This runs from a systemd timer
every 5 minutes and restarts the bot if:

  - the service is active, and has been for at least LIMIT minutes, and
  - the bot's log shows no received packet ("ROUTING INFO") for LIMIT minutes.

The longest normal silence over 10 days of logs was ~15-20 min, so the
default LIMIT is 30. While the 04:00 clock sync holds the radio the bot is
stopped, so it's skipped; after any (re)start it gets LIMIT minutes of grace.
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime

UNIT = "meshcore-bot"
LOG = "/opt/meshcore-bot/meshcore_bot.log"
LIMIT_MIN = float(os.environ.get("LIMIT_MIN", "30"))
MARKER = "ROUTING INFO"
TAIL_BYTES = 1_000_000

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def systemctl(*args: str) -> str:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True).stdout.strip()


def last_rx(path: str) -> datetime | None:
    """Time of the last received packet in the tail of a log file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        if MARKER in line:
            m = STAMP.match(ANSI.sub("", line))
            if m:
                return datetime.fromisoformat(m.group(1))
    return None


def main() -> int:
    if systemctl("is-active", UNIT) != "active":
        return 0  # stopped (e.g. the clock sync has the radio) or already restarting

    entered_us = int(systemctl("show", "-p", "ActiveEnterTimestampMonotonic", "--value", UNIT) or 0)
    up_min = (time.monotonic() - entered_us / 1e6) / 60
    if up_min < LIMIT_MIN:
        return 0

    rx = last_rx(LOG) or last_rx(LOG + ".1")  # just after a rotation the new file may be empty
    if rx is None:
        print(f"no '{MARKER}' line in {LOG}; bot up {up_min:.0f} min - restarting")
    else:
        quiet_min = (datetime.now() - rx).total_seconds() / 60
        if quiet_min < LIMIT_MIN:
            return 0
        print(f"no packet heard for {quiet_min:.0f} min (last {rx:%Y-%m-%d %H:%M:%S}); "
              f"bot up {up_min:.0f} min - restarting {UNIT}")
    sys.stdout.flush()
    subprocess.run(["systemctl", "restart", UNIT], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
