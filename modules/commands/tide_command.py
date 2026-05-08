#!/usr/bin/env python3
"""
Tide Command - Canadian tide predictions via DFO IWLS API
"""
import math
import datetime
import urllib.request
import json
import threading

from ..models import MeshMessage
from .base_command import BaseCommand


class TideCommand(BaseCommand):
    name = "tide"
    keywords = ['tide', 'tides']
    description = "Get tide predictions for nearest DFO station (usage: tide, tide Nanaimo, tide Victoria BC)"
    category = "weather"

    short_description = "Canadian tide predictions from DFO"
    usage = "tide [location]"
    examples = ["tide", "tide Nanaimo", "tide Victoria BC"]

    DFO_BASE = "https://api-iwls.dfo-mpo.gc.ca/api/v1"

    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10
        self.default_lat = bot.config.getfloat('Bot', 'bot_latitude', fallback=49.314)
        self.default_lon = bot.config.getfloat('Bot', 'bot_longitude', fallback=-124.314)
        self._stations = None
        self._stations_lock = threading.Lock()

    def _load_stations(self):
        with self._stations_lock:
            if self._stations is not None:
                return self._stations
            url = f"{self.DFO_BASE}/stations?province=BC&type=TIDEGAUGE"
            try:
                req = urllib.request.urlopen(url, timeout=self.url_timeout)
                all_bc = json.loads(req.read())
            except Exception:
                # Fallback: try all Canadian stations
                req = urllib.request.urlopen(f"{self.DFO_BASE}/stations?type=TIDEGAUGE", timeout=self.url_timeout)
                all_bc = json.loads(req.read())
            # Keep only stations with wlp-hilo predictions
            self._stations = [
                s for s in all_bc
                if any(ts['code'] == 'wlp-hilo' for ts in s.get('timeSeries', []))
            ]
            self.logger.info(f"Tide: loaded {len(self._stations)} stations with predictions")
            return self._stations

    def _nearest_station(self, lat, lon):
        stations = self._load_stations()
        return min(stations, key=lambda s: math.sqrt(
            (s['latitude'] - lat) ** 2 + (s['longitude'] - lon) ** 2
        ))

    def _get_predictions(self, station_id, days=2):
        today = datetime.date.today()
        end = today + datetime.timedelta(days=days)
        url = (f"{self.DFO_BASE}/stations/{station_id}/data"
               f"?time-series-code=wlp-hilo"
               f"&from={today}T00:00:00Z"
               f"&to={end}T00:00:00Z")
        req = urllib.request.urlopen(url, timeout=self.url_timeout)
        return json.loads(req.read())

    @staticmethod
    def _hl_labels(events):
        """Determine High/Low by local max/min."""
        vals = [e['value'] for e in events]
        n = len(vals)
        labels = []
        for i in range(n):
            left = vals[i - 1] if i > 0 else vals[i + 1]
            right = vals[i + 1] if i < n - 1 else vals[i - 1]
            labels.append('H' if vals[i] >= max(left, right) else 'L')
        return labels

    @staticmethod
    def _local_time(utc_str):
        dt = datetime.datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        local = dt.astimezone()
        return local.strftime('%H:%M')

    async def execute(self, message: MeshMessage) -> bool:
        try:
            # Strip trigger word to get optional location
            content = message.content.strip()
            for kw in self.keywords:
                if content.lower().startswith(kw):
                    content = content[len(kw):].strip()
                    break

            lat, lon = self.default_lat, self.default_lon

            if content:
                from ..utils import geocode_city_sync
                glat, glon, _ = geocode_city_sync(self.bot, content)
                if glat and glon:
                    lat, lon = glat, glon
                else:
                    await self.send_response(message, f"Could not find location: {content}")
                    return False

            station = self._nearest_station(lat, lon)
            events = self._get_predictions(station['id'])

            if not events:
                await self.send_response(message, f"No tide predictions for {station['officialName']}")
                return False

            # Split into today/tomorrow
            today_str = datetime.date.today().isoformat()
            tomorrow_str = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
            today_events = [e for e in events if e['eventDate'].startswith(today_str)]
            tmrw_events  = [e for e in events if e['eventDate'].startswith(tomorrow_str)]

            def format_events(evts):
                if not evts:
                    return ""
                labels = self._hl_labels(evts)
                return " | ".join(
                    f"{lbl} {self._local_time(e['eventDate'])} {e['value']:.1f}m"
                    for e, lbl in zip(evts, labels)
                )

            date_str = datetime.date.today().strftime('%b %-d')
            tmrw_str  = (datetime.date.today() + datetime.timedelta(days=1)).strftime('%b %-d')

            lines = [f"🌊 {station['officialName']} — {date_str}"]
            today_line = format_events(today_events)
            if today_line:
                lines.append(today_line)
            tmrw_line = format_events(tmrw_events)
            if tmrw_line:
                lines.append(f"{tmrw_str}: {tmrw_line}")

            await self.send_response(message, "\n".join(lines))
            return True

        except Exception as e:
            self.logger.error(f"Tide command error: {e}", exc_info=True)
            await self.send_response(message, f"Error fetching tide data: {str(e)[:60]}")
            return False

    def get_help_text(self) -> str:
        return f"{self.description}\nDefault location: nearest DFO station to bot coordinates."
