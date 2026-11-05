"""What another time-sync bot's #time-sync posts say it synced.

Shared by clock_sync.py (skip a node a peer synced recently) and the web page
(show who synced each node last), so the two can never disagree.

Two formats are understood:

  key format (preferred):   `<name> [<key or 3-byte prefix>]: SUCCESS` or `: OK`,
      or key first, as our own posts are: `[<key>] <name>: SUCCESS`
      one or more per message; each key's outcome is the text after it, up to
      the next key (a lone key: anywhere in the message). Exact.

  PMD-TimeSync's current format:    `4/11 Mt Benson Top N⛰: OK (5s off)`
      the repeater's advert name. Resolved to a key through the bot's
      database, and ONLY when exactly one repeater has that name: this mesh
      has several same-named nodes, and a wrong match would skip a real sync.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional

# A key: 6-64 hex in [brackets] (our posts carry the 3-byte prefix, `[ecf7f5]`),
# or 12-64 bare hex. Short bare hex is too easy to hit in ordinary words.
HEX_TOKEN = re.compile(r"(?<=\[)[0-9a-fA-F]{6,64}(?=\])|(?<![0-9a-fA-F])[0-9a-fA-F]{12,64}(?![0-9a-fA-F])")
OK_WORD = re.compile(r"(?<!NOT )\b(SUCCESS|OK)\b")  # upper case only: "ok" turns up in names
NAME_LINE = re.compile(r"^\s*(?:\d+\s*/\s*\d+\s+)?(?P<name>.+?):\s*(?:SUCCESS|OK)\b\s*(?P<note>.*)$")
NOTE = re.compile(r"^\(([^)]{1,40})\)")


def norm(name: str) -> str:
    """'PMD-TimeSync' == 'pmd - timesync 📡'; 'Mt Benson Top N⛰' == 'mt benson top n'."""
    return re.sub(r"[^0-9a-z]", "", (name or "").lower())


def parse(content: str) -> list[dict]:
    """Each node the message reports synced OK: {"key": hex} or {"name": normalised
    advert name, "label": as written}, plus "note" (e.g. "5s off") when given."""
    content = content or ""
    toks = list(HEX_TOKEN.finditer(content))
    if toks:
        out = []
        for i, m in enumerate(toks):
            seg = content if len(toks) == 1 else content[m.end():toks[i + 1].start() if i + 1 < len(toks) else len(content)]
            ok = OK_WORD.search(seg)
            if ok:
                note = NOTE.match(seg[ok.end():].strip())
                out.append({"key": m.group(0).lower(), "note": note.group(1) if note else ""})
        return out
    m = NAME_LINE.match(content)
    if not m or not norm(m.group("name")):
        return []
    note = NOTE.match(m.group("note").strip())
    return [{"name": norm(m.group("name")), "label": m.group("name").strip(), "note": note.group(1) if note else ""}]


def name_index(bot_db_uri: str) -> dict[str, set[str]]:
    """Normalised advert name -> the full keys of every repeater the bot has heard with it."""
    idx: dict[str, set[str]] = {}
    con = sqlite3.connect(bot_db_uri, uri=True, timeout=5)
    try:
        for key, name in con.execute(
            "SELECT public_key, name FROM repeater_contacts UNION "
            "SELECT public_key, name FROM complete_contact_tracking "
            "WHERE role = 'repeater' OR device_type IN ('Repeater', 'repeater', '2')"
        ):
            if key and norm(name):
                idx.setdefault(norm(name), set()).add(str(key).lower())
    finally:
        con.close()
    return idx


def resolve(entries: Iterable[dict], index: dict[str, set[str]]) -> list[dict]:
    """Give name entries a key when the name is unique; drop ambiguous/unknown names."""
    out = []
    for e in entries:
        if "key" in e:
            out.append(e)
            continue
        keys = index.get(e["name"], set())
        if len(keys) == 1:
            out.append({**e, "key": next(iter(keys))})
    return out


def key_matches(reported: str, node_key: str) -> bool:
    """A reported key and a node's key (full, or the configured 6+ hex prefix)
    are the same node when one starts with the other."""
    a, b = (reported or "").lower(), (node_key or "").lower()
    return len(a) >= 6 and len(b) >= 6 and (a.startswith(b) or b.startswith(a))


def peer_successes(rows: Iterable[tuple], peers: Iterable[str], channel: str,
                   index: dict[str, set[str]]) -> list[dict]:
    """rows: (received_at, sender, channel, content), newest first.
    Returns [{ts, sender, key, note}] for the peer bots' OK reports on channel."""
    want = {norm(p) for p in peers if norm(p)}
    chan = norm(channel)
    out = []
    for ts, sender, ch, content in rows:
        if not want or norm(sender) not in want or norm(ch) != chan:
            continue
        for e in resolve(parse(content), index):
            out.append({"ts": float(ts or 0), "sender": str(sender), "key": e["key"], "note": e.get("note", "")})
    return out


def latest_for(successes: list[dict], node_key: str) -> Optional[dict]:
    best = None
    for s in successes:
        if key_matches(s["key"], node_key) and (best is None or s["ts"] > best["ts"]):
            best = s
    return best


def fmt_offset(seconds: float) -> str:
    """How far a clock was off, PMD-TimeSync style: '5s off', '12m off', '3h off', '540d off'."""
    a = abs(seconds)
    if a < 90:
        return f"{a:.0f}s off"
    if a < 90 * 60:
        return f"{a / 60:.0f}m off"
    if a < 36 * 3600:
        return f"{a / 3600:.0f}h off"
    return f"{a / 86400:.0f}d off"

