#!/usr/bin/env python3
"""
Fire Command - BC Wildfire active fire status via BCWS ArcGIS API
"""
import urllib.request
import urllib.parse
import json
import time
from ..models import MeshMessage
from .base_command import BaseCommand
from ..url_shortener import shorten_url_sync


ARCGIS_URL = (
    "https://services6.arcgis.com/ubm4tcTYICKBpist/arcgis/rest/services"
    "/BCWS_ActiveFires_PublicView/FeatureServer/0/query"
)

# FIRE_CENTRE integer IDs match BC Wildfire Service fire centres
FIRE_CENTRES = {
    'coastal':   (2, ['cstl', 'coastal', 'coast', 'vi', 'island', 'vancouver island',
                      'lmd', 'lower mainland', 'mainland', 'sc', 'sunshine coast',
                      'sunshine', 'haida', 'haida gwaii']),
    'northwest': (3, ['northwest', 'nw', 'skeena', 'smithers', 'terrace', 'prince rupert']),
    'pg':        (4, ['pg', 'prince george', 'peace', 'peace river', 'fort st john', 'dawson']),
    'kamloops':  (5, ['kamloops', 'kam', 'thompson', 'okanagan', 'ok', 'nicola',
                      'merritt', 'penticton', 'kelowna', 'vernon']),
    'southeast': (6, ['southeast', 'se', 'kootenay', 'kootenays', 'koot', 'nelson',
                      'castlegar', 'cranbrook', 'boundary']),
    'cariboo':   (7, ['cariboo', 'car', 'williams lake', 'quesnel', 'clinton',
                      '100 mile', 'hundred mile']),
}

CENTRE_NAMES = {
    'coastal': 'Cstl', 'northwest': 'NW', 'pg': 'PG',
    'kamloops': 'OK', 'southeast': 'Koot', 'cariboo': 'Car',
}

STATUS_ICON = {
    'Out of Control': '🔴',
    'Being Held':     '🟡',
    'Under Control':  '🟢',
}
STATUS_ORDER = {'Out of Control': 0, 'Being Held': 1, 'Under Control': 2}


class FireCommand(BaseCommand):
    name = "fire"
    keywords = ['fire', 'fires', 'wildfire', 'wildfires']
    description = "BC active wildfires by fire centre (usage: fire coastal, fire kamloops major)"
    category = "weather"

    short_description = "BC Wildfire active fire status"
    usage = "fire <region> [major]"
    examples = ["fire coastal", "fire kamloops", "fire vi major", "fire cariboo major"]

    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10

    def _fetch(self, centre_id, ooc_only=False):
        where = f"FIRE_CENTRE={centre_id} AND FIRE_STATUS NOT IN ('Out')"
        if ooc_only:
            where = f"FIRE_CENTRE={centre_id} AND FIRE_STATUS='Out of Control'"
        params = {
            'where': where,
            'outFields': 'FIRE_NUMBER,FIRE_STATUS,CURRENT_SIZE,INCIDENT_NAME,FIRE_URL',
            'f': 'json',
            'resultRecordCount': 50,
            'orderByFields': 'CURRENT_SIZE DESC',
        }
        url = ARCGIS_URL + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'MeshCoreBot/1.0'})
        resp = urllib.request.urlopen(req, timeout=self.url_timeout)
        data = json.loads(resp.read())
        return [f['attributes'] for f in data.get('features', [])]

    @staticmethod
    def _chunk_messages(lines, max_len=200):
        chunks, current, length = [], [], 0
        for line in lines:
            needed = len(line) + (1 if current else 0)
            if current and length + needed > max_len:
                chunks.append('\n'.join(current))
                current, length = [line], len(line)
            else:
                current.append(line)
                length += needed
        if current:
            chunks.append('\n'.join(current))
        return chunks

    async def execute(self, message: MeshMessage) -> bool:
        try:
            content = message.content.strip()
            for kw in self.keywords:
                if content.lower().startswith(kw):
                    content = content[len(kw):].strip()
                    break

            tokens = content.lower().split()

            ooc_only = any(t in ('major', 'ooc') for t in tokens)
            tokens = [t for t in tokens if t not in ('major', 'ooc')]

            centre_id = None
            region_name = None
            text = ' '.join(tokens)
            for key, (cid, aliases) in FIRE_CENTRES.items():
                for alias in aliases:
                    if alias in text:
                        centre_id = cid
                        region_name = CENTRE_NAMES[key]
                        break
                if centre_id:
                    break

            if centre_id is None:
                await self.send_response(message,
                    "fire <region>\nfire <region> major\nRegions: cstl, car, ok, koot, pg, nw")
                return True

            fires = self._fetch(centre_id, ooc_only=ooc_only)

            if not fires:
                label = 'Out of Control ' if ooc_only else ''
                await self.send_response(message, f"✅ No active {label}fires in {region_name} Fire Centre.")
                return True

            fires.sort(key=lambda f: (STATUS_ORDER.get(f.get('FIRE_STATUS', ''), 3), -(f.get('CURRENT_SIZE') or 0)))

            label = 'OOC ' if ooc_only else ''
            header = f"🔥 {region_name} — {len(fires)} {label}fire{'s' if len(fires) != 1 else ''}:"

            tx_delay = self.bot.config.getint('Bot', 'tx_delay_ms', fallback=500) / 1000.0
            fire_lines = []
            for i, f in enumerate(fires[:9]):
                icon = STATUS_ICON.get(f.get('FIRE_STATUS', ''), '⚪')
                name = f.get('INCIDENT_NAME') or f.get('FIRE_NUMBER', 'Unknown')
                fire_url = f.get('FIRE_URL', '')
                if fire_url:
                    if i > 0:
                        time.sleep(tx_delay)
                    short = shorten_url_sync(fire_url, config=self.bot.config, logger=self.logger)
                else:
                    short = ''
                fire_lines.append(f"{icon} {name}: {short}" if short else f"{icon} {name}")

            if len(fires) > 9:
                fire_lines.append(f"… +{len(fires) - 9} more. bcwildfire.ca")

            # Group into chunks of 3, header on first chunk
            raw_chunks = [fire_lines[i:i + 3] for i in range(0, len(fire_lines), 3)]
            total = len(raw_chunks)
            chunks = []
            for i, batch in enumerate(raw_chunks):
                page = f"{i + 1}/{total}"
                chunks.append("\n".join([f"{header} {page}"] + batch))

            if hasattr(self.bot.command_manager, '_last_response'):
                self.bot.command_manager._last_response = chunks[0]
            await self.send_response_chunked(message, chunks)
            return True

        except Exception as e:
            self.logger.error(f"Fire command error: {e}", exc_info=True)
            await self.send_response(message, f"Error fetching fire data: {str(e)[:60]}")
            return False

    def get_help_text(self) -> str:
        return ("fire <region>\nfire <region> major\nRegions: cstl, car, ok, koot, pg, nw")
