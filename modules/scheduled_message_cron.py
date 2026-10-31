"""Cron keys for [Scheduled_Messages]: a 5-field crontab or an @preset.

    0 9 1 * *  = #kod-bot:Monthly test      (09:00 on the 1st of every month)
    0 8 * * mon = #ve7kod:Weekly net        (08:00 every Monday)
    @monthly   = #ve7kod:First of the month (00:00 on the 1st)

Shared by the scheduler and the web viewer. Day-of-week numbers are
APScheduler's (0=Monday ... 6=Sunday), not crontab's; names (mon-sun) are safer.
"""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

# APScheduler's from_crontab doesn't take @presets.
CRON_PRESETS = {
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
    '@monthly': '0 0 1 * *',
    '@weekly': '0 0 * * sun',
    '@daily': '0 0 * * *',
    '@midnight': '0 0 * * *',
    '@hourly': '0 * * * *',
}

_DAYS = {'mon': 'Mon', 'tue': 'Tue', 'wed': 'Wed', 'thu': 'Thu', 'fri': 'Fri', 'sat': 'Sat', 'sun': 'Sun',
         '0': 'Mon', '1': 'Tue', '2': 'Wed', '3': 'Thu', '4': 'Fri', '5': 'Sat', '6': 'Sun'}
_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def normalize_cron_key(key: str) -> str:
    """Lowercase with single spaces, as keys are stored."""
    return ' '.join(key.lower().split())


def cron_spec(key: str) -> str | None:
    """The crontab string for a cron key, or None if the key isn't a valid one."""
    key = normalize_cron_key(key)
    spec = CRON_PRESETS.get(key, key)
    if len(spec.split()) != 5:
        return None
    try:
        CronTrigger.from_crontab(spec)
    except ValueError:
        return None
    return spec


def cron_trigger(key: str, tz=None) -> CronTrigger:
    spec = cron_spec(key)
    if spec is None:
        raise ValueError(f"invalid cron key {key!r}")
    return CronTrigger.from_crontab(spec, timezone=tz)


def _ordinal(n: int) -> str:
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


def describe_cron(key: str) -> str:
    """A short label for the common shapes ("Monthly on the 1st at 09:00");
    anything else is shown as the crontab itself."""
    spec = cron_spec(key)
    if spec is None:
        return key
    minute, hour, dom, month, dow = spec.split()
    if not (minute.isdigit() and hour.isdigit()):
        return spec
    at = f"{int(hour):02d}:{int(minute):02d}"
    if dom == '*' and month == '*' and dow == '*':
        return f"Daily at {at}"
    if dom == '*' and month == '*' and dow in _DAYS:
        return f"Every {_DAYS[dow]} at {at}"
    if dom.isdigit() and month == '*' and dow == '*':
        return f"Monthly on the {_ordinal(int(dom))} at {at}"
    if dom.isdigit() and month.isdigit() and 1 <= int(month) <= 12 and dow == '*':
        return f"Yearly on {_MONTHS[int(month) - 1]} {int(dom)} at {at}"
    if dom == 'last' and month == '*' and dow == '*':
        return f"Monthly on the last day at {at}"
    return spec
