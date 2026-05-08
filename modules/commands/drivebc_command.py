#!/usr/bin/env python3
"""
DriveBC Command - BC road incidents and conditions via Open511 API
"""
import urllib.request
import json
from ..models import MeshMessage
from .base_command import BaseCommand
from ..url_shortener import shorten_url_sync


# Bounding boxes (lon_min, lat_min, lon_max, lat_max)
REGIONS = {
    'vi':  ('-128.5,48.2,-123.8,50.8', ['vi', 'island', 'vancouver island']),
    'lmd': ('-123.5,48.9,-122.0,49.5', ['lmd', 'lml', 'lower mainland', 'mainland']),
    'fv':  ('-122.0,49.0,-120.8,49.4', ['fv', 'fraser valley', 'fraser']),
}
VI_BBOX = REGIONS['vi'][0]

SEVERITY_ORDER = {'MAJOR': 0, 'MODERATE': 1, 'MINOR': 2, 'UNKNOWN': 3}
SEVERITY_ICON  = {'MAJOR': '🔴', 'MODERATE': '🟡', 'MINOR': '⚪', 'UNKNOWN': '⚪'}

TYPE_SHORT = {
    'CONSTRUCTION': 'Const.', 'INCIDENT': 'Incident', 'SPECIAL_EVENT': 'Event',
    'WEATHER_CONDITION': 'Weather', 'ROAD_CONDITION': 'Road cond.',
}


class DriveBCCommand(BaseCommand):
    name = "drivebc"
    keywords = ['drivebc', 'road', 'roads', 'highway']
    description = "BC road incidents on Vancouver Island (usage: drivebc, drivebc hwy19, drivebc major)"
    category = "travel"

    short_description = "DriveBC road incidents and conditions"
    usage = "drivebc [highway | major]"
    examples = ["drivebc", "drivebc major", "drivebc hwy 19", "drivebc hwy4"]

    API_BASE = "https://api.open511.gov.bc.ca/events"

    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10

    def _fetch(self, bbox=VI_BBOX, limit=100):
        url = f"{self.API_BASE}?format=json&status=ACTIVE&limit={limit}"
        if bbox:
            url += f"&bbox={bbox}"
        req = urllib.request.Request(url, headers={'User-Agent': 'MeshCoreBot/1.0', 'Accept': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=self.url_timeout)
        return json.loads(resp.read()).get('events', [])

    @staticmethod
    def _road_name(event):
        roads = event.get('roads', [])
        if roads:
            name = roads[0].get('name', '')
            frm = roads[0].get('from', '')
            return f"{name} {frm}".strip()
        return 'Unknown road'

    @staticmethod
    def _short_desc(event, max_len=80):
        desc = event.get('description', '').strip()
        if len(desc) > max_len:
            desc = desc[:max_len].rsplit(' ', 1)[0] + '…'
        return desc

    async def execute(self, message: MeshMessage) -> bool:
        try:
            content = message.content.strip()
            for kw in self.keywords:
                if content.lower().startswith(kw):
                    content = content[len(kw):].strip()
                    break

            if not content:
                await self.send_response(message,
                    "Usage: drivebc major [region] | drivebc hwy 19 | drivebc Malahat\n"
                    "Regions: vi, lmd, fv — e.g. 'drivebc major lmd'")
                return True

            tokens = content.lower().split()

            # Detect severity filter
            filter_major = any(t in ('major', 'majors', 'moderate') for t in tokens)
            tokens = [t for t in tokens if t not in ('major', 'majors', 'moderate')]

            # Detect region
            bbox = None
            region_name = 'BC'
            for key, (box, aliases) in REGIONS.items():
                # Check single tokens and pairs against aliases
                text = ' '.join(tokens)
                for alias in aliases:
                    if alias in text:
                        bbox = box
                        region_name = {'vi': 'Vancouver Island', 'lmd': 'Lower Mainland', 'fv': 'Fraser Valley'}[key]
                        tokens = text.replace(alias, '').split()
                        break
                if bbox:
                    break

            # Remaining tokens = keyword/highway filter
            keyword = ' '.join(tokens).strip().replace('hwy ', 'highway ').replace('hwy', 'highway ') or None

            # Highway number searches go province-wide unless a region was specified;
            # plain keyword searches default to VI
            is_highway_search = keyword and ('highway' in keyword or keyword.replace(' ', '').isdigit())
            if bbox is None:
                bbox = None if is_highway_search else VI_BBOX
                if bbox is None:
                    region_name = 'BC'

            events = self._fetch(bbox=bbox)

            if not events:
                await self.send_response(message, f"✅ No active road events in {region_name}.")
                return True

            if filter_major:
                events = [e for e in events if e.get('severity') in ('MAJOR', 'MODERATE')]

            if keyword:
                events = [e for e in events if keyword in (
                    e.get('roads', [{}])[0].get('name', '') + ' ' +
                    e.get('description', '')).lower()]

            # Sort: major first, then by type
            events.sort(key=lambda e: (SEVERITY_ORDER.get(e.get('severity', 'UNKNOWN'), 3),
                                       e.get('event_type', '')))

            if not events:
                await self.send_response(message, f"✅ No matching events in {region_name}.")
                return True

            # Build compact response — cap at ~5 events to stay under 200 chars per message
            scope = keyword.title() if keyword else region_name
            lines = [f"🚧 {scope} — {len(events)} event{'s' if len(events)!=1 else ''}:"]
            for e in events[:5]:
                icon = SEVERITY_ICON.get(e.get('severity', 'UNKNOWN'), '⚪')
                etype = TYPE_SHORT.get(e.get('event_type', ''), e.get('event_type', ''))
                road = self._road_name(e)
                event_url = e.get('url', '')
                short = shorten_url_sync(event_url, config=self.bot.config, logger=self.logger) if event_url else ''
                if short:
                    lines.append(f"{icon} {etype} — {road}: {short}")
                else:
                    desc = self._short_desc(e, 60)
                    lines.append(f"{icon} {etype} — {road}: {desc}")

            if len(events) > 5:
                lines.append(f"… and {len(events)-5} more. drivebc.ca")

            await self.send_response(message, "\n".join(lines))
            return True

        except Exception as e:
            self.logger.error(f"DriveBC command error: {e}", exc_info=True)
            await self.send_response(message, f"Error fetching road data: {str(e)[:60]}")
            return False

    def get_help_text(self) -> str:
        return (f"{self.description}\n"
                "Filter: 'drivebc major' for serious incidents only, "
                "'drivebc hwy 19' for a specific highway.")
