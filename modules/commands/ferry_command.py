#!/usr/bin/env python3
"""
Ferry Command - BC Ferries real-time sailing status via bcferriesapi.ca v2
"""
import asyncio
import urllib.request
import json
from ..models import MeshMessage
from .base_command import BaseCommand


# Aliases so users can type natural names
TERMINAL_ALIASES = {
    'nanaimo': 'NAN', 'departure': 'NAN', 'departure bay': 'NAN',
    'duke': 'DUK', 'duke point': 'DUK',
    'victoria': 'SWB', 'swartz': 'SWB', 'swartz bay': 'SWB',
    'tsawwassen': 'TSA', 'tsawassen': 'TSA',
    'horseshoe': 'HSB', 'horseshoe bay': 'HSB', 'vancouver': 'HSB',
    'langdale': 'LNG', 'sunshine coast': 'LNG',
    'bowen': 'BOW', 'bowen island': 'BOW',
    'fulford': 'FUL', 'salt spring': 'FUL',
    'gulf islands': 'SGI', 'gulf': 'SGI',
}

VALID_TERMINALS = {'BOW', 'DUK', 'FUL', 'HSB', 'LNG', 'NAN', 'SWB', 'TSA', 'SGI'}


class FerryCommand(BaseCommand):
    name = "ferry"
    keywords = ['ferries']
    description = "BC Ferries sailing status (usage: ferries NAN, ferries HSB NAN)"
    category = "travel"

    short_description = "BC Ferries real-time sailing status"
    usage = "ferries <terminal> or ferries <src> <dst>"
    examples = ["ferries NAN", "ferries HSB NAN", "ferries SWB TSA"]

    API_URL = "https://www.bcferriesapi.ca/v2/capacity/"

    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10

    def _fetch(self):
        req = urllib.request.Request(self.API_URL, headers={'User-Agent': 'MeshCoreBot/1.0'})
        resp = urllib.request.urlopen(req, timeout=self.url_timeout)
        data = json.loads(resp.read())
        # Index routes by (src, dst) for easy lookup
        routes = {}
        for r in data.get('routes', []):
            src = r['fromTerminalCode']
            dst = r['toTerminalCode']
            if src not in routes:
                routes[src] = {}
            routes[src][dst] = r['sailings']
        return routes

    @classmethod
    def _split_two_terminals(cls, text):
        tokens = text.split()
        for i in range(1, len(tokens)):
            src = cls._resolve_terminal(' '.join(tokens[:i]))
            dst = cls._resolve_terminal(' '.join(tokens[i:]))
            if src and dst and src != dst:
                return src, dst
        return None, None

    @staticmethod
    def _resolve_terminal(text):
        text = text.strip().upper()
        if text in VALID_TERMINALS:
            return text
        return TERMINAL_ALIASES.get(text.lower())

    @staticmethod
    def _to24(time_str):
        try:
            import datetime
            return datetime.datetime.strptime(time_str.strip(), '%I:%M %p').strftime('%H:%M')
        except ValueError:
            return time_str

    @classmethod
    def _format_sailing(cls, s):
        parts = [cls._to24(s['time'])]
        fill = s.get('fill', 0)
        car = s.get('carFill', 0)
        if fill or car:
            parts.append(f"{fill}%/{car}%car")
        if s.get('isCancelled'):
            parts.append('CANCELLED')
        return ' '.join(parts)

    @staticmethod
    def _upcoming(sailings, limit=3):
        import datetime
        now = datetime.datetime.now().strftime('%H:%M')
        result = []
        for s in sailings:
            if s.get('sailingStatus') != 'future':
                continue
            try:
                t = datetime.datetime.strptime(s['time'].strip(), '%I:%M %p').strftime('%H:%M')
                if t >= now:
                    result.append(s)
            except (ValueError, KeyError):
                result.append(s)
        return result[:limit]

    async def execute(self, message: MeshMessage) -> bool:
        try:
            content = message.content.strip()
            for kw in self.keywords:
                if content.lower().startswith(kw):
                    content = content[len(kw):].strip()
                    break

            if not content:
                await self.send_response(message,
                    "ferries <terminal> or ferries <src> <dst>\nTerms: NAN, DUK, SWB, TSA, HSB, LNG, BOW, FUL")
                return True

            data = self._fetch()

            # Two-terminal route lookup
            src_code, dst_code = self._split_two_terminals(content)
            if src_code and dst_code:
                sailings = self._upcoming(data.get(src_code, {}).get(dst_code, []))
                if sailings:
                    sailing_strs = [self._format_sailing(s) for s in sailings]
                    await self.send_response(message, f"⛴ {src_code}→{dst_code}\n" + "\n".join(sailing_strs))
                else:
                    await self.send_response(message, f"⛴ {src_code}→{dst_code}\nNo current data.")
                return True

            # Single terminal lookup
            code = self._resolve_terminal(content)
            if not code:
                await self.send_response(message, f"Unknown terminal: {content}. Try: NAN, DUK, SWB, TSA, HSB, LNG, BOW, FUL")
                return False

            if code not in data:
                await self.send_response(message, f"No data for {code}")
                return False

            route_blocks = []
            for dst, sailings in data[code].items():
                upcoming = self._upcoming(sailings)
                if upcoming:
                    sailing_strs = [self._format_sailing(s) for s in upcoming]
                    route_blocks.append((dst, sailing_strs))

            if not route_blocks:
                await self.send_response(message, f"⛴ {code}\nNo upcoming sailings.")
                return True

            total = len(route_blocks)
            chunks = []
            for i, (dst, sailing_strs) in enumerate(route_blocks):
                page = f"{i + 1}/{total}"
                chunks.append("\n".join([f"⛴ {code}→{dst} {page}"] + sailing_strs))

            if hasattr(self.bot.command_manager, '_last_response'):
                self.bot.command_manager._last_response = chunks[0]
            await asyncio.sleep(0.5)
            await self.send_response_chunked(message, chunks)
            return True

        except Exception as e:
            self.logger.error(f"Ferry command error: {e}", exc_info=True)
            await self.send_response(message, f"Error fetching ferry data: {str(e)[:60]}")
            return False

    def get_help_text(self) -> str:
        return "ferries <terminal> or ferries <src> <dst>\nTerms: NAN, DUK, SWB, TSA, HSB, LNG, BOW, FUL"
