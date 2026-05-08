#!/usr/bin/env python3
"""
Ferry Command - BC Ferries real-time sailing status via bcferriesapi.ca
"""
import urllib.request
import json
from ..models import MeshMessage
from .base_command import BaseCommand


TERMINAL_NAMES = {
    'BOW': 'Bowen Island', 'DUK': 'Duke Point', 'FUL': 'Fulford Harbour',
    'HSB': 'Horseshoe Bay', 'LNG': 'Langdale', 'NAN': 'Departure Bay',
    'SWB': 'Swartz Bay', 'TSA': 'Tsawwassen', 'SGI': 'Gulf Islands',
}

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
}

# Default routes to show when no terminal specified (VI-focused)
DEFAULT_ROUTES = [('NAN', 'HSB'), ('DUK', 'TSA'), ('SWB', 'TSA')]


class FerryCommand(BaseCommand):
    name = "ferry"
    keywords = ['ferries']
    description = "BC Ferries sailing status (usage: ferries, ferries Nanaimo, ferries Swartz Bay)"
    category = "travel"

    short_description = "BC Ferries real-time sailing status"
    usage = "ferries [terminal]"
    examples = ["ferries", "ferries Nanaimo", "ferries Swartz Bay", "ferries NAN"]

    API_URL = "https://bcferriesapi.ca/api/"

    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10

    def _fetch(self):
        req = urllib.request.Request(self.API_URL, headers={'User-Agent': 'MeshCoreBot/1.0'})
        resp = urllib.request.urlopen(req, timeout=self.url_timeout)
        return json.loads(resp.read())

    @classmethod
    def _split_two_terminals(cls, text):
        """Try to parse text as two terminal codes/names. Returns (src, dst) or (None, None)."""
        tokens = text.split()
        # Try each split point: first i tokens = src, rest = dst
        for i in range(1, len(tokens)):
            src = cls._resolve_terminal(' '.join(tokens[:i]))
            dst = cls._resolve_terminal(' '.join(tokens[i:]))
            if src and dst and src != dst:
                return src, dst
        return None, None

    @staticmethod
    def _resolve_terminal(text):
        text = text.strip().upper()
        if text in TERMINAL_NAMES:
            return text
        lower = text.lower()
        return TERMINAL_ALIASES.get(lower)

    @staticmethod
    def _format_sailing(s):
        parts = [s['time']]
        fill = s.get('fill', 0)
        car = s.get('carFill', 0)
        if fill or car:
            parts.append(f"{fill}%/{car}%car")
        vessel = s.get('vesselName', '').strip()
        if vessel:
            # Shorten "Queen of X" → "Q.X", "Spirit of X" → "S.X", "Coastal X" → "C.X"
            vessel = vessel.replace('Queen of ', 'Q.').replace('Spirit of ', 'S.')
            vessel = vessel.replace('Coastal ', 'C.').replace('Salish ', 'Sl.')
            parts.append(vessel)
        if s.get('isCancelled'):
            parts.append('CANCELLED')
        return ' '.join(parts)

    async def execute(self, message: MeshMessage) -> bool:
        try:
            content = message.content.strip()
            for kw in self.keywords:
                if content.lower().startswith(kw):
                    content = content[len(kw):].strip()
                    break

            data = self._fetch()

            if not content:
                # Summary of default VI routes
                lines = []
                for src, dst in DEFAULT_ROUTES:
                    if src in data and dst in data[src]:
                        sailings = data[src][dst]['sailings']
                        # Show next 2 sailings that haven't sailed yet (fill data present = current/upcoming)
                        upcoming = sailings[:2]
                        if upcoming:
                            s1 = self._format_sailing(upcoming[0])
                            s2 = self._format_sailing(upcoming[1]) if len(upcoming) > 1 else ''
                            route = f"{TERMINAL_NAMES[src]}→{TERMINAL_NAMES[dst]}"
                            line = f"{route}: {s1}"
                            if s2:
                                line += f" | {s2}"
                            lines.append(line)
                if lines:
                    await self.send_response(message, "⛴ BC Ferries\n" + "\n".join(lines))
                else:
                    await self.send_response(message, "No ferry data available.")
                return True

            # Try to parse as two terminals (src + dst)
            src_code, dst_code = self._split_two_terminals(content)

            if src_code and dst_code:
                # Specific route lookup — also try reverse if not found
                routes_to_try = [(src_code, dst_code), (dst_code, src_code)]
                for src, dst in routes_to_try:
                    if src in data and dst in data.get(src, {}):
                        sailings = data[src][dst]['sailings'][:3]
                        route = f"{TERMINAL_NAMES.get(src, src)}→{TERMINAL_NAMES.get(dst, dst)}"
                        sailing_strs = [self._format_sailing(s) for s in sailings]
                        await self.send_response(message, f"⛴ {route}:\n" + " | ".join(sailing_strs))
                        return True
                src_name = TERMINAL_NAMES.get(src_code, src_code)
                dst_name = TERMINAL_NAMES.get(dst_code, dst_code)
                await self.send_response(message, f"No direct route between {src_name} and {dst_name}.")
                return False

            # Single terminal lookup
            code = src_code or self._resolve_terminal(content)
            if not code:
                await self.send_response(message, f"Unknown terminal: {content}. Try: Nanaimo (NAN), Duke Point (DUK), Swartz Bay (SWB), Tsawwassen (TSA), Horseshoe Bay (HSB), Langdale (LNG)")
                return False

            if code not in data:
                await self.send_response(message, f"No data for {TERMINAL_NAMES.get(code, code)}")
                return False

            lines = [f"⛴ {TERMINAL_NAMES.get(code, code)} sailings:"]
            for dst, info in data[code].items():
                sailings = info.get('sailings', [])[:3]
                dst_name = TERMINAL_NAMES.get(dst, dst)
                sailing_strs = [self._format_sailing(s) for s in sailings]
                lines.append(f"→{dst_name}: " + " | ".join(sailing_strs))

            await self.send_response(message, "\n".join(lines))
            return True

        except Exception as e:
            self.logger.error(f"Ferry command error: {e}", exc_info=True)
            await self.send_response(message, f"Error fetching ferry data: {str(e)[:60]}")
            return False

    def get_help_text(self) -> str:
        return "'ferries'=VI summary\n'ferries HSB'=term\n'ferries HSB NAN'=route\nTerms: NAN, DUK, SWB, TSA, HSB, LNG, BOW, FUL"
