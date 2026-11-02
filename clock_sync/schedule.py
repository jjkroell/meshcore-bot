"""When the automatic clock sync runs - edited on the web page.

/etc/clock_sync/schedule.json:
    {"days": ["mon", "wed", "fri"], "time": "04:00", "peer_skip_hours": 24, "paused": false}

Read by clock-sync-scheduler.py (root, every minute: is a slot due?), by the
web page (show/edit it, "next run"), and by clock_sync.py (peer_skip_hours).
Times are local (the Pi's timezone).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Optional

SCHEDULE_FILE = "/etc/clock_sync/schedule.json"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # datetime.weekday() order
DEFAULT = {"days": ["mon", "wed", "fri"], "time": "04:00", "peer_skip_hours": 24, "paused": False}
GRACE_MINUTES = 30  # a slot missed by up to this long (Pi rebooting, radio busy) still runs


def validate(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ValueError("schedule must be an object")
    days = d.get("days", DEFAULT["days"])
    if not isinstance(days, list) or not all(isinstance(x, str) and x.lower() in DAYS for x in days):
        raise ValueError("days must be a list of mon..sun")
    days = [x for x in DAYS if x in {y.lower() for y in days}]
    t = str(d.get("time", DEFAULT["time"])).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("time must be HH:MM (24 h)")
    t = f"{int(m.group(1)):02d}:{m.group(2)}"
    try:
        hours = float(d.get("peer_skip_hours", DEFAULT["peer_skip_hours"]))
    except (TypeError, ValueError):
        raise ValueError("skip window must be a number of hours")
    if not 0 <= hours <= 168:
        raise ValueError("skip window must be 0-168 hours (0 = never skip)")
    paused = d.get("paused", False)
    if not isinstance(paused, bool):
        raise ValueError("paused must be true or false")
    return {"days": days, "time": t, "peer_skip_hours": int(hours) if hours == int(hours) else hours,
            "paused": paused}


def load(path: Optional[str] = None) -> tuple[dict, bool]:
    """(schedule, came from the file). A missing or broken file gives the default."""
    try:
        with open(path or SCHEDULE_FILE, encoding="utf-8") as f:
            return validate(json.load(f)), True
    except (OSError, ValueError):
        return dict(DEFAULT), False


def save(sched: dict, path: Optional[str] = None) -> dict:
    sched = validate(sched)
    path = path or SCHEDULE_FILE
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".schedule.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sched, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return sched


def _slot_on(day: datetime, sched: dict) -> datetime:
    hh, mm = map(int, sched["time"].split(":"))
    return day.replace(hour=hh, minute=mm, second=0, microsecond=0)


def next_run(sched: dict, now: Optional[datetime] = None) -> Optional[datetime]:
    """The next slot strictly after now; None if paused or no days picked."""
    now = now or datetime.now()
    if sched["paused"] or not sched["days"]:
        return None
    for i in range(8):
        day = now + timedelta(days=i)
        if DAYS[day.weekday()] in sched["days"]:
            slot = _slot_on(day, sched)
            if slot > now:
                return slot
    return None


def due_slot(sched: dict, now: Optional[datetime] = None) -> Optional[datetime]:
    """The slot that should be running now: today's, if today is picked and
    now is 0..GRACE_MINUTES past it. The caller remembers which slots it ran."""
    now = now or datetime.now()
    if sched["paused"] or DAYS[now.weekday()] not in sched["days"]:
        return None
    slot = _slot_on(now, sched)
    return slot if timedelta(0) <= now - slot <= timedelta(minutes=GRACE_MINUTES) else None
