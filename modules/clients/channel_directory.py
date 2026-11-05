#!/usr/bin/env python3
"""Hashtag channel names from a Ridgeline channel directory, busiest first."""

import json
import threading
import time
import urllib.request
from typing import Any

CACHE_SECONDS = 3600
RETRY_SECONDS = 300
TIMEOUT_SECONDS = 15
TRAFFIC_WINDOW_SECONDS = 86400

_directories: dict[str, "ChannelDirectory"] = {}


def is_bot_channel(name: str) -> bool:
    """Return True for a channel for the bots reply (kod-bot, bot, test, testing, ...)."""
    lowered = name.lower()
    return 'bot' in lowered or 'test' in lowered


def parse_exclude_list(raw: str) -> set[str]:
    """Parse a comma-separated channel list into bare lower-case names."""
    return {name.strip().lstrip('#').lower() for name in raw.split(',') if name.strip().lstrip('#')}


def get_directory(url: str, logger: Any) -> "ChannelDirectory":
    """Return the directory for this URL, shared so commands share one cache."""
    if url not in _directories:
        _directories[url] = ChannelDirectory(url, logger)
    return _directories[url]


class ChannelDirectory:
    """Confirmed channel names, cached, keeping the last good list on failure."""

    def __init__(self, url: str, logger: Any):
        self.url = url.rstrip('/')
        self.logger = logger
        self._lock = threading.Lock()
        self._names: list[str] = []
        self._next_fetch = 0.0

    def names(self) -> list[str]:
        """Return channel names without the #, busiest first. Blocks while fetching."""
        with self._lock:
            now = time.monotonic()
            if now >= self._next_fetch:
                try:
                    self._names = self._fetch()
                    self._next_fetch = now + CACHE_SECONDS
                except Exception as e:
                    self.logger.warning(f"Channel directory fetch failed: {e}")
                    self._next_fetch = now + RETRY_SECONDS
            return list(self._names)

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(f"{self.url}{path}", headers={'User-Agent': 'meshcore-bot'})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)

    def _fetch(self) -> list[str]:
        channels = self._get_json('/api/channels/discovered').get('channels', [])
        names = [c['name'] for c in channels if c.get('status') == 'confirmed' and c.get('name')]
        try:
            traffic = self._traffic()
        except Exception as e:
            self.logger.warning(f"Channel traffic fetch failed, listing unranked: {e}")
            traffic = {}
        return sorted(names, key=lambda name: (-traffic.get(name.lower(), 0), name.lower()))

    def _traffic(self) -> dict[str, int]:
        """Count unique messages per channel; each is listed once per observer."""
        since = int(time.time()) - TRAFFIC_WINDOW_SECONDS
        seen: dict[str, set[str]] = {}
        for row in self._get_json(f'/api/channels/recent?since={since}'):
            channel = row.get('channel')
            if channel:
                seen.setdefault(channel.lower(), set()).add(row.get('messageHash') or '')
        return {channel: len(hashes) for channel, hashes in seen.items()}
