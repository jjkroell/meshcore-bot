"""
Public alerts for meshcore-bot (local service plugin).

Polls one or more alert sources and posts new alerts to mesh channels. Each
channel is its own section with its own areas and alert level, so one channel
can cover the whole southwest and another a single island. Settings are on the
web viewer's Plugins page and are re-read on every poll, so changing them needs
no restart; turning the service on or off does.

Each section can also follow a health authority's drinking water notices for
named locations, with its own choice of notice types. Island Health has no
feed, so its web page is read about once an hour; if the page's layout changes,
this stops working and the log says so. New notices only: what's already listed
when a section starts following a location is never posted.

Source types:
    naad   Atom feed whose entries link to CAP alert files. This is the
           NAAD feed that carries Alert Ready, Environment Canada and
           earthquake early warning alerts.
    eccc   Environment Canada weather alerts API (GeoJSON).

What gets posted:
    - Only real alerts: anything with a status other than Actual, or marked
      as a test in any way, is dropped first and can't be turned back on.
    - Urgent alerts (Alert Ready's broadcast or wireless flags, or any alert
      not from Environment Canada) go out at once, in up to two messages.
    - Environment Canada alerts are held for a few minutes so regions issued
      separately can be merged into one line, and a region is posted once per
      hazard until that alert lapses, so "continued" updates stay quiet.

Several bots can share the job, so an area keeps its alerts if the bot nearest
to it loses power or internet. Each gets the same sections, the bots' radio
names and its own role. The primary posts at once. A backup holds each post for
a wait set by its role and stays quiet if it hears another bot post it first.
Lost packets can mean an
occasional duplicate, which is better than a missed alert. Only the named bots
are listened to, so nobody else can silence an alert by typing its wording.

This is a convenience, not a warning system. It depends on the internet, the
bot and the mesh all working.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import requests
from meshcore import EventType

from modules.service_plugins.base_service import BaseServicePlugin

CAP_NS = {"c": "urn:oasis:names:tc:emergency:cap:1.2"}
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
STATE_KEY = "public_alerts_state"
MAX_BYTES = 130  # safe for a channel message whatever the radio's name is
MAX_SEEN = 3000
HEARD_KEEP_SECONDS = 24 * 3600  # how long other bots' posts are remembered
MAX_HEARD = 300
ROLES = {"primary": 0, "backup1": 1, "backup2": 2}  # place in line
MESH_ALLOWANCE_SECONDS = 60  # time for a post to cross the mesh
HTTP_TIMEOUT = 10
USER_AGENT = "meshcore-bot-public-alerts"
# Posting here would look like a call for help to Ridgeline and email its users.
FORBIDDEN_CHANNELS = {"sos", "sos-test"}

HEALTH_GROUP = "Drinking water notices"
HEALTH_AUTHORITIES = {
    "island-health": {
        "label": "Island Health",
        "url": "https://inspections.myhealthdepartment.com/island-health/public-notices",
        "base": "https://inspections.myhealthdepartment.com",
    },
}
# Fewer rows than this on a successful read means the page is wrong, not that
# the notices were lifted.
MIN_HEALTH_ROWS = 5
HEALTH_ROW = re.compile(
    r'<h4 class="establishment-list-name[^"]*">\s*<a href="([^"]+permitID=([^"&]+)[^"]*)"[^>]*>(.*?)</a>\s*</h4>'
    r'\s*</div>\s*<div class="w-col w-col-4">\s*(.*?)\s*</div>\s*<div class="w-col w-col-2">\s*(.*?)\s*</div>',
    re.S,
)
HEALTH_TYPE = re.compile(r"currently under an?\s+(.{3,60}?)\s*\.", re.S)
# Section setting -> word found in the notice type.
HEALTH_TYPE_FIELDS = {
    "health_boil_water": "boil water",
    "health_water_quality": "water quality",
    "health_do_not_consume": "do not consume",
    "health_do_not_use": "do not use",
}

LEVELS = {"urgent": 0, "warning": 1, "advisory": 2, "statement": 3}
COLOURS = ("yellow", "orange", "red")  # Environment Canada's risk colours, lowest first
# How wind alerts write their speeds, on lower-cased text with "km/h" spelled that way.
WIND_UNCLEAR = re.compile(r"\b(?:except|prone|locally|higher|reported|observed|recorded|measured)\b|\d{2,3} ?(?:-|to) ?\d{2,3} (?:km/h )?gusting")
WIND_STEADY = re.compile(r"\b(\d{2,3}) (?:km/h )?gusting (?:up )?to (\d{2,3})\b")
WIND_GUST = re.compile(r"(?:gusts?|gusting|gusts:)[a-z ]{0,20}? (\d{2,3}) ?km/h")
WIND_GUST_RANGE = re.compile(r"gust(?:s|ing)?[a-z ]{0,20}? (\d{2,3}) ?(?:-|to|and) ?(\d{2,3}) ?km/h")
ALERT_TYPE_LEVEL = {"warning": 1, "watch": 1, "advisory": 2, "statement": 3}

# Census divisions: Fraser Valley, Greater Vancouver, Capital, Cowichan Valley,
# Nanaimo, Alberni-Clayoquot, Strathcona, Comox Valley, qathet, Sunshine Coast,
# Mount Waddington.
DEFAULT_AREA_CODES = "5909, 5915, 5917, 5919, 5921, 5923, 5924, 5926, 5927, 5929, 5943"
DEFAULT_AREA_NAMES = (
    "Metro Vancouver, Fraser Valley, Howe Sound, Whistler, Sea to Sky, Sunshine Coast, "
    "Southern Gulf Islands, Greater Victoria, Vancouver Island"
)


@dataclass
class Alert:
    """One alert from any source, in the form the filters work on."""

    key: str                   # unique per message, for "already seen"
    title: str                 # e.g. "Yellow wind warning"
    hazard: str                # shared across sources, e.g. "yellow wind warning"
    areas: list[str]           # area names
    area_codes: dict[str, list[str]] = field(default_factory=dict)  # area name -> location codes
    from_eccc: bool = False    # Environment Canada weather alert
    colour: str = ""           # yellow, orange or red; "" if it has none
    level: int = 1             # 0 urgent ... 3 statement
    status: str = "Actual"
    msg_type: str = "Alert"
    ended: bool = False
    is_test: bool = False
    what: str = ""             # one line of detail
    instruction: str = ""
    expires: float = 0.0       # unix time, 0 = unknown
    sent: float = 0.0          # unix time, 0 = unknown


def _colour(text: Optional[str]) -> str:
    colour = (text or "").strip().lower()
    return colour if colour in COLOURS else ""


def _parse_time(text: Optional[str]) -> float:
    if not text:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _parse_xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data[:2000]:
        raise ValueError("refusing XML with a DOCTYPE")
    return ET.fromstring(data)


def wind_speeds(text: str) -> str:
    """The forecast speeds in an alert's text, like "Winds 70 km/h gusting to 90."

    They're only written in sentences, so anything unclear gives "": no speed
    is better than a wrong one. That covers exceptions for some areas, a range
    of steady winds, figures that disagree (different periods or places), and
    speeds already measured, which aren't a forecast.
    """
    flat = " ".join((text or "").split()).lower().replace("kilometres per hour", "km/h")
    if WIND_UNCLEAR.search(flat):
        return ""
    steady = {int(a) for a, _ in WIND_STEADY.findall(flat)}
    gusts = {(int(b), int(b)) for _, b in WIND_STEADY.findall(flat)}
    gusts |= {(int(b), int(b)) for b in WIND_GUST.findall(flat)}
    gusts |= {(int(a), int(b)) for a, b in WIND_GUST_RANGE.findall(flat)}
    if len(gusts) != 1 or len(steady) > 1:
        return ""
    low, high = gusts.pop()
    if steady:
        return f"Winds {steady.pop()} km/h gusting to {high}."
    return f"Gusts to {high} km/h." if low == high else f"Gusts {low} to {high} km/h."


def _what_line(text: str, wind: bool = False) -> str:
    """The line after "What:" in an Environment Canada alert, else the first line.

    A wind alert's forecast speeds replace it when they can be read. Timing goes
    first: alerts come out hours ahead, and a long line loses its end.
    """
    match = re.search(r"What:\s*\n?\s*(.+)", text or "")
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    what = match.group(1).strip() if match else (lines[0] if lines else "")
    what = (wind_speeds(text) if wind else "") or what
    when = re.search(r"When:\s*(.+)", text or "")
    return f"When: {when.group(1).strip().rstrip('.')}. {what}" if when else what


def parse_cap(data: bytes, key: str = "") -> Optional[Alert]:
    """Turn a CAP file into an Alert, using its English block. None if there isn't one."""
    root = _parse_xml(data)
    info = next(
        (i for i in root.findall("c:info", CAP_NS)
         if (i.findtext("c:language", "", CAP_NS) or "").lower().startswith("en")),
        None,
    )
    if info is None:
        return None
    params = {
        (p.findtext("c:valueName", "", CAP_NS) or "").split(":")[-1]: (p.findtext("c:value", "", CAP_NS) or "")
        for p in info.findall("c:parameter", CAP_NS)
    }
    event = (info.findtext("c:event", "", CAP_NS) or "").strip()
    headline = (info.findtext("c:headline", "", CAP_NS) or event).strip()
    event_codes = [(v.text or "") for v in info.findall("c:eventCode/c:value", CAP_NS)]
    sender = info.findtext("c:senderName", "", CAP_NS) or ""
    status = (root.findtext("c:status", "", CAP_NS) or "").strip()
    msg_type = (root.findtext("c:msgType", "", CAP_NS) or "").strip()
    alert_type = params.get("Alert_Type", "").lower()

    urgent = (
        params.get("Broadcast_Immediately", "").lower() == "yes"
        or params.get("WirelessImmediate", "").lower() == "yes"
        or params.get("Broadcast_Intrusive", "").lower() == "yes"
        or not alert_type  # not an Environment Canada weather alert
    )
    # "yellow warning - wind" -> "yellow wind warning". The colour stays in the
    # hazard so a warning that's raised to orange posts as a new one.
    name = params.get("Alert_Name", "")
    parts = [p.strip() for p in name.split(" - ")]
    colour = _colour(params.get("Colour") or name.split(" ")[0])
    if len(parts) == 2:
        hazard = f"{colour} {parts[1]} {parts[0].split()[-1]}".strip().lower()
    else:
        hazard = (name or event or headline).lower()
    areas, area_codes = [], {}
    for area in info.findall("c:area", CAP_NS):
        desc = (area.findtext("c:areaDesc", "", CAP_NS) or "").strip()
        if not desc:
            continue
        areas.append(desc)
        area_codes[desc] = [
            geocode.findtext("c:value", "", CAP_NS) or ""
            for geocode in area.findall("c:geocode", CAP_NS)
            if (geocode.findtext("c:valueName", "", CAP_NS) or "").endswith(":Location:0.3")
        ]
    description = info.findtext("c:description", "", CAP_NS) or ""
    return Alert(
        key=key or root.findtext("c:identifier", "", CAP_NS) or "",
        title=hazard.capitalize() if alert_type else headline,
        hazard=hazard,
        areas=areas,
        area_codes=area_codes,
        from_eccc=bool(alert_type),
        colour=colour,
        level=0 if urgent else ALERT_TYPE_LEVEL.get(alert_type, 3),
        status=status,
        msg_type=msg_type,
        ended=msg_type == "Cancel" or headline.lower().endswith("ended"),
        is_test=(
            "testmessage" in " ".join(event_codes).lower()
            or "test message" in f"{event} {headline}".lower()
        ),
        what=_what_line(description, wind="wind" in hazard),
        instruction=" ".join((info.findtext("c:instruction", "", CAP_NS) or "").split()),
        expires=_parse_time(info.findtext("c:expires", "", CAP_NS)),
        sent=_parse_time(root.findtext("c:sent", "", CAP_NS)),
    )


def parse_atom(data: bytes) -> list[tuple[str, str]]:
    """Return (entry id, CAP link) pairs, oldest first."""
    root = _parse_xml(data)
    pairs = []
    for entry in root.findall("a:entry", ATOM_NS):
        link = entry.find("a:link[@rel='alternate']", ATOM_NS)
        if link is None:
            link = entry.find("a:link", ATOM_NS)
        entry_id = (entry.findtext("a:id", "", ATOM_NS) or "").rsplit("feed.atom/", 1)[-1]
        if link is not None and link.get("href") and entry_id:
            pairs.append((entry_id, link.get("href").replace("http://", "https://", 1)))
    pairs.reverse()
    return pairs


def parse_eccc(data: bytes) -> list[Alert]:
    """Environment Canada API: one Alert per alert per forecast region."""
    alerts = []
    for feature in json.loads(data).get("features", []):
        p = feature.get("properties", {})
        name = (p.get("alert_name_en") or "").strip()
        alert_type = (p.get("alert_type") or "").lower()
        region = (p.get("feature_name_en") or "").strip()
        status = (p.get("status_en") or "").lower()
        colour = _colour(p.get("risk_colour_en"))
        name = f"{colour} {name}".strip()
        alerts.append(Alert(
            key=f"eccc:{p.get('alert_code')}:{p.get('feature_id')}:{p.get('publication_datetime')}",
            title=name.capitalize(),
            hazard=name.lower(),
            areas=[region],
            from_eccc=True,
            colour=colour,
            level=ALERT_TYPE_LEVEL.get(alert_type, 3),
            ended=status in ("ended", "cancelled"),
            what=_what_line(p.get("alert_text_en") or "", wind="wind" in name.lower()),
            expires=_parse_time(p.get("expiration_datetime")),
            sent=_parse_time(p.get("publication_datetime")),
        ))
    return alerts


def matching_areas(alert: Alert, codes: list[str], names: list[str]) -> list[str]:
    """The alert's areas that match. An urgent BC alert we can't place is kept whole.

    Environment Canada regions cross census divisions (Central Coast reaches
    into Mount Waddington, Nicola into Fraser Valley), so a 4-digit division
    code would pull in neighbours. Its alerts only match on names or on a full
    7-digit subdivision code; other alerts match on any code.
    """
    prefixes = tuple(c for c in codes if len(c) == 7 or not alert.from_eccc)
    lowered = [n.lower() for n in names]
    matched = [
        area for area in alert.areas
        if (prefixes and any(c.startswith(prefixes) for c in alert.area_codes.get(area, [])))
        or any(n in area.lower() for n in lowered)
    ]
    if matched:
        return matched
    unplaced = not any(alert.area_codes.values())
    if alert.level == 0 and unplaced and "british columbia" in " ".join(alert.areas).lower():
        return alert.areas
    return []


def short_area(name: str) -> str:
    """'Metro Vancouver - southeast including Surrey' -> 'Metro Vancouver'."""
    return re.split(r" - | including ", name, maxsplit=1)[0].strip()


def fit(text: str, limit: int = MAX_BYTES) -> str:
    """Cut to limit bytes, at a word if one is close."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[: limit - 3].decode("utf-8", "ignore")
    if " " in cut[-25:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "..."


def format_group(title: str, areas: list[str], what: str) -> str:
    """One line for a hazard and the regions it covers."""
    seen: list[str] = []
    for area in areas:
        short = short_area(area)
        if short not in seen:
            seen.append(short)
    return fit(f"{title}: {', '.join(seen)}. {what}".strip())


def format_urgent(alert: Alert) -> list[str]:
    """Up to two messages: what and where, then what to do."""
    prefix = "CANCELLED: " if alert.ended else "ALERT: "
    first = fit(f"{prefix}{alert.title} - {', '.join(alert.areas)}")
    if alert.ended:
        return [first]
    second = alert.instruction or alert.what
    return [first, fit(second)] if second else [first]


def _plain(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def parse_health_list(page: str) -> list[dict[str, str]]:
    """The rows of a health authority's notice page."""
    return [
        {"link": html.unescape(link), "permit": permit, "name": _plain(name),
         "location": _plain(location), "start": _plain(start)}
        for link, permit, name, location, start in HEALTH_ROW.findall(page)
    ]


def parse_health_type(page: str) -> str:
    """The notice type on a facility page, or "" if it doesn't say."""
    match = HEALTH_TYPE.search(_plain(page))
    return match.group(1).strip() if match else ""


def format_health(notice: dict[str, str], lifted: bool = False) -> str:
    kind = notice.get("type") or "Drinking water notice"
    where = f"{notice['name']}, {notice['location']}"
    if lifted:
        return fit(f"No longer listed: {kind} for {where}. Confirm with your supplier.")
    return fit(f"{kind}: {where}. Check with your water supplier.")


class PublicAlertsService(BaseServicePlugin):
    config_section = "PublicAlerts"
    description = "Posts Alert Ready and weather alerts to channels, each with its own area"

    settings_schema = [
        {"key": "poll_seconds", "label": "Check every", "type": "int", "min": 30, "default": 60,
         "unit": "s", "help": "How often to check the sources."},
        {"key": "group_minutes", "label": "Group weather alerts for", "type": "int", "min": 0, "default": 5,
         "unit": "min", "help": "Wait this long so regions can merge into one post. Urgent alerts skip this."},
        {"key": "max_age_minutes", "label": "Ignore alerts older than", "type": "int", "min": 5, "default": 60,
         "unit": "min", "help": "Stops a backlog being posted after the bot has been down."},
        {"key": "health_poll_minutes", "label": "Check health notices every", "type": "int", "min": 15,
         "default": 60, "unit": "min", "help": "How often to read a health authority's notice page."},
        {"key": "peer_bots", "label": "Other alert bots", "type": "list", "default": "",
         "help": "Radio names of other bots posting the same alerts to the same channels, exactly as they "
                 "appear on the mesh. Leave blank if this is the only one."},
        {"key": "role", "label": "This bot's role", "type": "enum", "default": "primary", "width": "md",
         "options": [
             {"value": "primary", "label": "Primary"},
             {"value": "backup1", "label": "First backup"},
             {"value": "backup2", "label": "Second backup"},
         ],
         "help": "The primary posts first. A backup only posts if it doesn't hear the alert from another "
                 "bot. Give each bot a different role."},
    ]

    settings_repeating_blocks = [
        {
            "id": "route",
            "label": "Alert channels",
            "item_label": "Channel",
            "enabled_field": "enabled",
            "help": "Each section posts to its own channel with its own area and level. An alert goes to "
                    "every section it matches. Test messages are never posted.",
            "fields": [
                {"key": "name", "label": "Name", "type": "str", "default": "",
                 "help": "A label for you, like Salt Spring Island."},
                {"key": "channel", "label": "Channel", "type": "str", "default": "",
                 "help": "It must exist on the radio. #sos and #sos-test are refused."},
                {"key": "min_level", "label": "What to post", "type": "enum", "default": "warning", "width": "md",
                 "options": [
                     {"value": "urgent", "label": "Urgent alerts only"},
                     {"value": "warning", "label": "Urgent + warnings and watches"},
                     {"value": "advisory", "label": "Urgent + warnings, watches and advisories"},
                     {"value": "statement", "label": "Everything, including statements"},
                 ]},
                {"key": "min_colour", "label": "Lowest warning colour", "type": "enum", "default": "yellow", "width": "md",
                 "options": [
                     {"value": "yellow", "label": "Yellow, orange and red"},
                     {"value": "orange", "label": "Orange and red only"},
                     {"value": "red", "label": "Red only"},
                 ],
                 "explain": [
                     {"term": "Urgent", "text": "Emergencies sent out through Alert Ready, and anything that isn't "
                      "an Environment Canada weather alert, like an evacuation order. Posted straight away, "
                      "whatever the colour setting."},
                     {"term": "Warnings and watches", "text": "Environment Canada alerts for serious weather that "
                      "is expected or happening (a warning), or that conditions could produce (a watch)."},
                     {"term": "Advisories", "text": "Environment Canada alerts for weather that is less serious "
                      "but worth knowing about, like fog."},
                     {"term": "Statements", "text": "Early notices with no set level, like a special weather "
                      "statement or coastal flooding statement. These can be frequent."},
                     {"term": "Warning colours", "text": "Environment Canada rates its warnings yellow, orange or "
                      "red, from least to most serious. Alerts with no colour aren't affected by this setting."},
                 ]},
                {"key": "area_codes", "label": "Area codes", "type": "list", "default": [],
                 "pattern": r"\d{2,7}",
                 "help": "Statistics Canada codes. 7 digits is one area (Salt Spring Island is 5917027); "
                         "4 digits is a whole regional district, for non-weather alerts only."},
                {"key": "area_names", "label": "Area names", "type": "list", "default": [],
                 "help": "Match alerts whose area name contains one of these, like Metro Vancouver."},
                {"key": "health_authority", "label": "Health authority", "type": "enum", "default": "none",
                 "group": HEALTH_GROUP, "width": "md",
                 "options": [{"value": "none", "label": "None"}] + [
                     {"value": key, "label": value["label"]} for key, value in HEALTH_AUTHORITIES.items()
                 ]},
                {"key": "health_locations", "label": "Locations", "type": "list", "default": [],
                 "group": HEALTH_GROUP, "width": "md",
                 "help": "Exactly as the authority's list writes it, like Salt Spring Island."},
                {"key": "health_boil_water", "label": "Boil Water", "type": "bool", "default": False,
                 "group": HEALTH_GROUP, "style": "check", "heading": "Notices to post"},
                {"key": "health_water_quality", "label": "Water Quality", "type": "bool", "default": False,
                 "group": HEALTH_GROUP, "style": "check"},
                {"key": "health_do_not_consume", "label": "Do Not Consume", "type": "bool", "default": False,
                 "group": HEALTH_GROUP, "style": "check"},
                {"key": "health_do_not_use", "label": "Do Not Use", "type": "bool", "default": False,
                 "group": HEALTH_GROUP, "style": "check"},
                {"key": "health_lifted", "label": "Post when a notice is no longer listed", "type": "bool",
                 "default": False, "group": HEALTH_GROUP, "style": "check", "heading": "Also"},
            ],
        },
        {
            "id": "source",
            "label": "Alert sources",
            "item_label": "Source",
            "enabled_field": "enabled",
            "help": "Shared by every channel. Each is checked in turn, and an alert found in more than "
                    "one is posted once.",
            "fields": [
                {"key": "name", "label": "Name", "type": "str", "default": ""},
                {"key": "type", "label": "Type", "type": "enum", "default": "naad",
                 "options": [
                     {"value": "naad", "label": "NAAD / Atom feed with CAP alerts"},
                     {"value": "eccc", "label": "Environment Canada alerts API"},
                 ]},
                {"key": "url", "label": "URL", "type": "str", "default": ""},
            ],
        },
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.enabled = bot.config.getboolean(self.config_section, "enabled", fallback=False)
        self._task: Optional[asyncio.Task] = None
        self._seen: list[str] = []
        self._seen_set: set[str] = set()
        self._posted: dict[str, float] = {}  # "channel|hazard|area" -> expires
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}  # (channel, hazard) -> group waiting
        self._urgent: list[tuple[str, Alert, float]] = []  # (channel, alert, queued) waiting to send
        self._outbox: list[tuple[str, str, float]] = []  # (channel, health message, queued) waiting to send
        self._heard: list[dict[str, Any]] = []  # other bots' posts: channel, text, at
        self._subscription: Any = None
        self._health: dict[str, dict[str, dict[str, str]]] = {}  # authority -> notice key -> notice
        self._health_checked: dict[str, float] = {}
        self._primed: set[str] = set()  # sources read at least once
        self._warned: set[str] = set()
        self._load_state()

    # --- settings, re-read every poll ---

    def _get(self, key: str, fallback: str) -> str:
        return self.bot.config.get(self.config_section, key, fallback=fallback, raw=True).strip()

    def _int(self, key: str, fallback: int) -> int:
        try:
            return int(self._get(key, str(fallback)))
        except ValueError:
            return fallback

    def _flag(self, key: str) -> bool:
        return self._get(key, "false").lower() in ("true", "1", "yes", "on")

    def _blocks(self, prefix: str) -> list[int]:
        """Numbers of the enabled blocks, in order."""
        pattern = re.compile(rf"^{prefix}(\d+)_")
        numbers = {
            int(match.group(1))
            for match in map(pattern.match, self.bot.config.options(self.config_section))
            if match
        }
        return [
            n for n in sorted(numbers)
            if self._get(f"{prefix}{n}_enabled", "true").lower() in ("true", "1", "yes", "on")
        ]

    def _peers(self) -> list[str]:
        return [v.strip().lower() for v in self._get("peer_bots", "").split(",") if v.strip()]

    def _wait(self) -> int:
        """Seconds to hold each post. No other bots means nothing to wait for.

        Bots read the feed up to one check apart and posts take time to cross
        the mesh, so each place in line waits that much longer.
        """
        if not self._peers():
            return 0
        gap = max(30, self._int("poll_seconds", 60)) + MESH_ALLOWANCE_SECONDS
        return ROLES.get(self._get("role", "primary"), 0) * gap

    def sources(self) -> list[dict[str, str]]:
        return [
            {
                "name": self._get(f"source{n}_name", f"source {n}"),
                "type": self._get(f"source{n}_type", "naad"),
                "url": self._get(f"source{n}_url", ""),
            }
            for n in self._blocks("source")
            if self._get(f"source{n}_url", "")
        ]

    def routes(self) -> list[dict[str, Any]]:
        """The enabled channel sections that can actually post."""
        found = []
        for n in self._blocks("route"):
            channel = self._get(f"route{n}_channel", "")
            if channel.lstrip("#").lower() in FORBIDDEN_CHANNELS:
                if channel not in self._warned:
                    self._warned.add(channel)
                    self.logger.error(f"PublicAlerts: refusing to post to {channel}")
                continue
            codes = [v.strip() for v in self._get(f"route{n}_area_codes", "").split(",") if v.strip()]
            names = [v.strip() for v in self._get(f"route{n}_area_names", "").split(",") if v.strip()]
            authority = self._get(f"route{n}_health_authority", "none")
            locations = [
                v.strip().lower() for v in self._get(f"route{n}_health_locations", "").split(",") if v.strip()
            ]
            follows_health = authority in HEALTH_AUTHORITIES and bool(locations)
            if channel and (codes or names or follows_health):
                found.append({
                    "channel": channel,
                    "level": LEVELS.get(self._get(f"route{n}_min_level", "warning"), 1),
                    "min_colour": _colour(self._get(f"route{n}_min_colour", "yellow")) or COLOURS[0],
                    "codes": codes,
                    "names": names,
                    "health_authority": authority if follows_health else None,
                    "health_locations": locations,
                    "health_types": [
                        word for key, word in HEALTH_TYPE_FIELDS.items() if self._flag(f"route{n}_{key}")
                    ],
                    "health_lifted": self._flag(f"route{n}_health_lifted"),
                })
        return found

    # --- state ---

    def _load_state(self) -> None:
        db = getattr(self.bot, "db_manager", None)
        raw = db.get_metadata(STATE_KEY) if db else None
        if not isinstance(raw, str) or not raw:
            return
        try:
            state = json.loads(raw)
        except ValueError:
            return
        self._seen = list(state.get("seen", []))
        self._seen_set = set(self._seen)
        self._posted = dict(state.get("posted", {}))
        self._primed = set(state.get("primed", []))
        self._health = dict(state.get("health", {}))
        self._health_checked = dict(state.get("health_checked", {}))
        self._heard = list(state.get("heard", []))

    def _save_state(self) -> None:
        db = getattr(self.bot, "db_manager", None)
        if db:
            db.set_metadata(STATE_KEY, json.dumps({
                "seen": self._seen[-MAX_SEEN:],
                "posted": self._posted,
                "primed": sorted(self._primed),
                "health": self._health,
                "health_checked": self._health_checked,
                "heard": self._heard,
            }))

    def _mark_seen(self, key: str) -> None:
        self._seen.append(key)
        self._seen_set.add(key)
        if len(self._seen) > MAX_SEEN * 2:
            self._seen = self._seen[-MAX_SEEN:]
            self._seen_set = set(self._seen)

    # --- lifecycle ---

    async def start(self) -> None:
        # Some bot versions call start() twice; run one loop only.
        if not self.enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._listen()
        channels = ", ".join(route["channel"] for route in self.routes()) or "no channels"
        self.logger.info(f"PublicAlerts: started, posting to {channels}")

    async def stop(self) -> None:
        self._running = False
        meshcore = getattr(self.bot, "meshcore", None)
        if self._subscription is not None and meshcore:
            with contextlib.suppress(Exception):
                meshcore.unsubscribe(self._subscription)
        self._subscription = None
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def on_transport_reconnected(self) -> None:
        """A reconnect brings a new meshcore instance, so listen on that one."""
        if self._running:
            self._listen()

    def _listen(self) -> None:
        meshcore = getattr(self.bot, "meshcore", None)
        if meshcore:
            self._subscription = meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)

    async def _on_channel_message(self, event: Any, metadata: Any = None) -> None:
        try:
            payload = event.payload
            channel = self.bot.channel_manager.get_channel_name(payload.get("channel_idx", 0))
            self._hear(channel, payload.get("text", ""), time.time())
        except Exception as e:
            self.logger.warning(f"PublicAlerts: can't read a channel message: {e}")

    def _hear(self, channel: str, text: str, now: float) -> None:
        """Remember a post if it came from one of the named bots."""
        lowered = text.lower()
        for peer in self._peers():
            if lowered.startswith(f"{peer}:"):
                self._heard.append({
                    "channel": channel.lstrip("#").lower(),
                    "text": text[len(peer) + 1:].strip(),
                    "at": now,
                })
                self._heard = [h for h in self._heard if now - h["at"] < HEARD_KEEP_SECONDS][-MAX_HEARD:]
                return

    def _heard_since(self, channel: str, since: float) -> list[str]:
        key = channel.lstrip("#").lower()
        return [h["text"] for h in self._heard if h["channel"] == key and h["at"] >= since]

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"PublicAlerts: poll failed: {e}")
            await asyncio.sleep(max(30, self._int("poll_seconds", 60)))

    # --- fetching ---

    def _fetch(self, url: str) -> bytes:
        response = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return response.content

    def _read_source(self, source: dict[str, str]) -> list[tuple[str, Optional[str], Optional[Alert]]]:
        """Return (key, CAP link, alert) rows. NAAD rows are fetched only if unseen."""
        data = self._fetch(source["url"])
        if source["type"] == "eccc":
            return [(a.key, None, a) for a in parse_eccc(data)]
        return [(key, link, None) for key, link in parse_atom(data)]

    # --- the poll ---

    def real_and_current(self, alert: Alert, now: float) -> bool:
        """Checks that don't depend on the channel. Tests can never pass."""
        if alert.status != "Actual" or alert.is_test:
            return False
        if alert.ended and alert.level != 0:
            return False
        if alert.expires and alert.expires < now:
            return False
        return not (alert.sent and now - alert.sent > self._int("max_age_minutes", 60) * 60)

    def for_route(self, alert: Alert, route: dict[str, Any]) -> Optional[Alert]:
        """This channel's copy of the alert, naming only its areas. None if it doesn't apply."""
        if alert.level > route["level"]:
            return None
        if alert.level != 0 and alert.colour and (
            COLOURS.index(alert.colour) < COLOURS.index(route["min_colour"])
        ):
            return None
        areas = matching_areas(alert, route["codes"], route["names"])
        return replace(alert, areas=areas) if areas else None

    def _queue(self, channel: str, alert: Alert, now: float) -> None:
        new = [a for a in alert.areas if f"{channel}|{alert.hazard}|{a}" not in self._posted]
        if alert.level == 0:
            # Updates to a real emergency always go out. Environment Canada's
            # urgent alerts repeat like its others, so they're posted once.
            if new or not alert.from_eccc:
                self._urgent.append((channel, alert, now))
                for area in new:
                    self._posted[f"{channel}|{alert.hazard}|{area}"] = alert.expires or now + 12 * 3600
            return
        if not new:
            return
        group = self._pending.setdefault((channel, alert.hazard), {
            "title": alert.title, "areas": [], "what": alert.what, "first": now, "expires": 0.0,
        })
        group["areas"].extend(a for a in new if a not in group["areas"])
        # One line covers the whole group, so details that differ can't be shown.
        if group["what"] != alert.what:
            group["what"] = ""
        group["expires"] = max(group["expires"], alert.expires or now + 12 * 3600)

    async def poll(self) -> None:
        now = time.time()
        routes = self.routes()
        for source in self.sources():
            url = source["url"]
            try:
                rows = await asyncio.to_thread(self._read_source, source)
            except Exception as e:
                self.logger.warning(f"PublicAlerts: {source['name']} failed: {e}")
                continue
            # First read of a source only learns what's there, so turning the
            # service on doesn't post the whole feed.
            first_read = url not in self._primed
            for key, link, alert in rows:
                if key in self._seen_set:
                    continue
                if first_read:
                    self._mark_seen(key)
                    continue
                if alert is None:
                    try:
                        data = await asyncio.to_thread(self._fetch, link)
                        alert = parse_cap(data, key)
                    except Exception as e:
                        # Not marked seen, so it's tried again next poll.
                        self.logger.warning(f"PublicAlerts: can't read {link}: {e}")
                        continue
                self._mark_seen(key)
                if alert is None or not self.real_and_current(alert, now):
                    continue
                for route in routes:
                    copy = self.for_route(alert, route)
                    if copy is not None:
                        self._queue(route["channel"], copy, now)
            self._primed.add(url)
        await self._poll_health(routes, now)
        await self._send_ready(now)
        self._posted = {k: v for k, v in self._posted.items() if v > now}
        self._save_state()

    @staticmethod
    def _wants_notice(route: dict[str, Any], authority: str, notice: dict[str, str]) -> bool:
        """True if this section follows the notice. A type we can't read is sent, not dropped."""
        if route["health_authority"] != authority or not route["health_types"]:
            return False
        if notice["location"].lower() not in route["health_locations"]:
            return False
        kind = notice.get("type", "").lower()
        return not kind or any(word in kind for word in route["health_types"])

    async def _read_notice_type(self, base: str, row: dict[str, str]) -> Optional[str]:
        """The notice's type. None if the page couldn't be read, so it's tried next time."""
        try:
            page = await asyncio.to_thread(self._fetch, base + row["link"])
        except Exception as e:
            self.logger.warning(f"PublicAlerts: can't read {row['name']}: {e}")
            return None
        return parse_health_type(page.decode("utf-8", "ignore"))

    async def _poll_health(self, routes: list[dict[str, Any]], now: float) -> None:
        interval = max(15, self._int("health_poll_minutes", 60)) * 60
        for authority in {r["health_authority"] for r in routes if r["health_authority"]}:
            if now - self._health_checked.get(authority, 0) < interval:
                continue
            info = HEALTH_AUTHORITIES[authority]
            followers = [r for r in routes if r["health_authority"] == authority]
            followed = {place for r in followers for place in r["health_locations"]}
            try:
                data = await asyncio.to_thread(self._fetch, info["url"])
            except Exception as e:
                self.logger.warning(f"PublicAlerts: {info['label']} failed: {e}")
                continue
            self._health_checked[authority] = now
            rows = parse_health_list(data.decode("utf-8", "ignore"))
            known = self._health.get(authority)
            if len(rows) < max(MIN_HEALTH_ROWS, len(known or {}) // 2):
                self.logger.error(
                    f"PublicAlerts: {info['label']} page gave {len(rows)} notices; has its layout changed?"
                )
                continue
            current: dict[str, dict[str, str]] = {}
            for row in rows:
                key = f"{row['permit']}|{row['start']}"
                if known is not None and key in known:
                    current[key] = known[key]
                    continue
                notice = {"name": row["name"], "location": row["location"], "type": ""}
                if row["location"].lower() in followed:
                    kind = await self._read_notice_type(info["base"], row)
                    if kind is None and known is not None:
                        continue
                    notice["type"] = kind or ""
                current[key] = notice
                # First read only learns what's there.
                if known is not None:
                    for route in followers:
                        if self._wants_notice(route, authority, notice):
                            self._outbox.append((route["channel"], format_health(notice), now))
            for key, notice in (known or {}).items():
                if key not in current:
                    for route in followers:
                        if route["health_lifted"] and self._wants_notice(route, authority, notice):
                            self._outbox.append((route["channel"], format_health(notice, lifted=True), now))
            self._health[authority] = current

    async def _send(self, channel: str, messages: list[str]) -> bool:
        if channel.lstrip("#").lower() in FORBIDDEN_CHANNELS:
            self.logger.error(f"PublicAlerts: refusing to post to {channel}")
            return False
        manager = self.bot.command_manager
        if len(messages) == 1:
            return await manager.send_channel_message(channel, messages[0], skip_user_rate_limit=True)
        return await manager.send_channel_messages_chunked(channel, messages)

    async def _send_ready(self, now: float) -> None:
        wait = self._wait()
        max_age = self._int("max_age_minutes", 60) * 60

        waiting = []
        for channel, alert, queued in self._urgent:
            if now - queued < wait:
                waiting.append((channel, alert, queued))
                continue
            messages = format_urgent(alert)
            # An update can repeat an earlier post's words, so only posts made
            # since this alert was issued count.
            since = alert.sent - 60 if alert.sent else queued - max_age
            if messages[0] in self._heard_since(channel, since):
                self.logger.info(f"PublicAlerts: another bot posted {alert.title} on {channel}")
                continue
            if not await self._send(channel, messages):
                if not alert.expires or alert.expires > now:
                    waiting.append((channel, alert, queued))
        self._urgent = waiting

        unsent = []
        # The other bot may have read the page up to one check earlier.
        health_window = max(15, self._int("health_poll_minutes", 60)) * 60 + 600
        for channel, message, queued in self._outbox:
            if now - queued < wait:
                unsent.append((channel, message, queued))
                continue
            if message in self._heard_since(channel, queued - health_window):
                self.logger.info(f"PublicAlerts: another bot posted a water notice on {channel}")
                continue
            if not await self._send(channel, [message]):
                unsent.append((channel, message, queued))
        self._outbox = unsent

        hold = self._int("group_minutes", 5) * 60 + wait
        for key, group in list(self._pending.items()):
            if now - group["first"] < hold:
                continue
            channel, hazard = key
            heard = [
                text for text in self._heard_since(channel, group["first"] - max_age)
                if text.startswith(f"{group['title']}:")
            ]
            covered = [a for a in group["areas"] if any(short_area(a) in text for text in heard)]
            for area in covered:
                self._posted[f"{channel}|{hazard}|{area}"] = group["expires"]
                group["areas"].remove(area)
            if covered:
                self.logger.info(f"PublicAlerts: another bot posted {group['title']} on {channel}")
            if not group["areas"]:
                del self._pending[key]
                continue
            if await self._send(channel, [format_group(group["title"], group["areas"], group["what"])]):
                for area in group["areas"]:
                    self._posted[f"{channel}|{hazard}|{area}"] = group["expires"]
                del self._pending[key]
            elif group["expires"] < now:
                del self._pending[key]
