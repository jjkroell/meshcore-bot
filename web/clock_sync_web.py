#!/usr/bin/env python3
"""Web editor for the MeshCore repeater clock-sync list.

Edits the `repeaters:` list in /etc/clock_sync/clock_sync.yaml (read by
/opt/clock_sync/clock_sync.py from cron). Only that list is ever rewritten:
everything else in the file - admin_password, companion settings, comments -
is left byte-for-byte as it was. Entries you don't touch keep their exact
text (comments included); per-node keys the UI doesn't show (a node's own
admin_password, say) are carried over on edit. Every save is re-parsed and
checked before it replaces the file, and the previous file is kept in
backups/.

Shows, per node: the last successful sync (state.json) and the outcome of the
latest run (clock_sync.log). Suggests names and public keys from the repeaters
meshcore-bot has heard (its database, read-only).

No dependencies beyond PyYAML (run it with /opt/clock_sync/venv/bin/python).
HTTP Basic auth; the password hash lives in the auth file (see --set-password).

    clock_sync_web.py --set-password          # prompt, write the auth file
    clock_sync_web.py --check                 # dry run: what a save would change
    clock_sync_web.py [--port 8090]           # serve
"""

from __future__ import annotations

import argparse
import base64
import difflib
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

CONFIG = "/etc/clock_sync/clock_sync.yaml"
BACKUP_DIR = "/etc/clock_sync/backups"
AUTH_FILE = "/etc/clock_sync/web_auth.json"
STATE_FILE = "/opt/clock_sync/state.json"
SYNC_LOG = "/var/log/clock_sync/clock_sync.log"
BOT_DB = "/opt/meshcore-bot/meshcore_bot.db"
KEEP_BACKUPS = 30
MIN_KEY_HEX = 6  # clock_sync matches contacts by public-key prefix; 3 bytes is the house style

SECRET_KEYS = re.compile(r"(password|secret|token|key)", re.I)
HEX = re.compile(r"^[0-9a-f]+$")

_lock = threading.Lock()  # one save at a time


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# The repeaters block, as text
# --------------------------------------------------------------------------

def _is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


class RepeatersBlock:
    """The file split as: head (up to and including `repeaters:`), the list's
    leading comments, one text chunk per entry, the list's trailing comments,
    and tail (the rest of the file). Each entry chunk = its leading comment
    lines + its own lines."""

    def __init__(self, text: str):
        self.text = text
        lines = text.splitlines(keepends=True)
        start = next((i for i, l in enumerate(lines)
                      if re.match(r"^repeaters\s*:\s*(#.*)?$", l.rstrip("\r\n"))), None)
        if start is None:
            raise ConfigError("no top-level `repeaters:` line (a flow-style or nested list can't be edited here)")
        # The list runs until the next top-level key...
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[^\s#-]", lines[i])), len(lines))
        # ...minus the comments/blank lines after its last entry, which stay
        # where they are (new entries go in above them).
        while end - 1 > start and _is_blank_or_comment(lines[end - 1]):
            end -= 1
        self.head = "".join(lines[: start + 1])
        self.tail = "".join(lines[end:])
        body = lines[start + 1 : end]

        item_re = re.compile(r"^(\s*)- ")
        starts = [i for i, l in enumerate(body) if item_re.match(l)]
        if not starts:
            raise ConfigError("`repeaters:` has no `- ` entries")
        self.dash_indent = item_re.match(body[starts[0]]).group(1)
        if any(item_re.match(body[i]).group(1) != self.dash_indent for i in starts):
            raise ConfigError("`repeaters:` entries aren't all at the same indent")
        self.key_indent = self.dash_indent + "  "

        # Comments above the first entry stay with the list; comments between
        # entries belong to the entry below them (removed with it).
        self.lead = "".join(body[: starts[0]])
        bounds = []
        for n, s in enumerate(starts):
            a = s
            if n:
                while a - 1 > starts[n - 1] and _is_blank_or_comment(body[a - 1]):
                    a -= 1
            bounds.append(a)
        bounds.append(len(body))
        self.chunks = ["".join(body[bounds[n] : bounds[n + 1]]) for n in range(len(starts))]

    @staticmethod
    def split_lead(chunk: str) -> tuple[str, str]:
        lines = chunk.splitlines(keepends=True)
        i = 0
        while i < len(lines) and _is_blank_or_comment(lines[i]):
            i += 1
        return "".join(lines[:i]), "".join(lines[i:])

    def render_entry(self, entry: dict) -> str:
        out = []
        for n, (k, v) in enumerate(entry.items()):
            prefix = self.dash_indent + "- " if n == 0 else self.key_indent
            out.append(f"{prefix}{k}: {json.dumps(v, ensure_ascii=False)}\n")
        return "".join(out)

    def build(self, chunks: list[str]) -> str:
        body = self.lead + "".join(c if c.endswith("\n") else c + "\n" for c in chunks)
        return self.head + body + self.tail


# --------------------------------------------------------------------------
# Config load / save
# --------------------------------------------------------------------------

def read_config(path: str | None = None) -> tuple[str, dict, RepeatersBlock]:
    with open(path or CONFIG, encoding="utf-8") as f:
        text = f.read()
    cfg = yaml.safe_load(text) or {}
    reps = cfg.get("repeaters")
    if not isinstance(reps, list) or not all(isinstance(r, dict) for r in reps):
        raise ConfigError("`repeaters` is not a list of mappings")
    block = RepeatersBlock(text)
    if len(block.chunks) != len(reps):
        raise ConfigError(f"found {len(block.chunks)} `- ` entries in the text but {len(reps)} repeaters parsed")
    # Every chunk must parse to the entry it claims to be.
    for chunk, rep in zip(block.chunks, reps):
        _, own = RepeatersBlock.split_lead(chunk)
        dedent = "\n".join(l[len(block.dash_indent):] for l in own.splitlines())
        if (yaml.safe_load(dedent) or [None])[0] != rep:
            raise ConfigError("couldn't line up an entry's text with its parsed value")
    return text, cfg, block


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def normalise_key(key: str) -> str:
    key = (key or "").strip().lower().replace(" ", "")
    if not HEX.match(key) or len(key) < MIN_KEY_HEX or len(key) > 64:
        raise ValueError(f"public key must be {MIN_KEY_HEX}-64 hex characters")
    return key


def apply_ops(text: str, cfg: dict, block: RepeatersBlock, ops: list[dict]) -> tuple[str, list[dict]]:
    """ops: the full desired list, in order: {id: <index in file> or None, name, pubkey,
    password?: str (set this node's own admin password), clear_password?: bool (use the
    shared one again)}. A missing password keeps whatever the node has.
    Returns (new text, new repeaters list)."""
    old = cfg["repeaters"]
    seen_ids: set[int] = set()
    keys: list[tuple[str, bool]] = []  # (key, new or changed)
    chunks: list[str] = []
    new: list[dict] = []
    for op in ops:
        name = str(op.get("name", "")).strip()
        if not name or len(name) > 64 or "\n" in name:
            raise ValueError("each node needs a name (one line, up to 64 characters)")
        raw_key = str(op.get("pubkey") or "").strip().lower()
        oid = op.get("id")
        orig = None
        if oid is not None:
            if not isinstance(oid, int) or not 0 <= oid < len(old) or oid in seen_ids:
                raise ValueError("stale or invalid node id; reload the page")
            seen_ids.add(oid)
            orig = old[oid]
        # An entry left exactly as it is is kept as it is, whatever its key looks like.
        pw = op.get("password")
        if pw is not None:
            pw = str(pw)
            if not pw:
                pw = None  # an empty box means "keep"
            elif len(pw) > 64 or "\n" in pw or "\r" in pw:
                raise ValueError("admin password must be one line, up to 64 characters")
        clear_pw = bool(op.get("clear_password")) and pw is None
        if orig is not None and pw is not None and orig.get("admin_password") == pw:
            pw = None  # same as before
        if clear_pw and (orig is None or "admin_password" not in orig):
            clear_pw = False
        unchanged = (orig is not None and orig.get("name") == name and pw is None and not clear_pw
                     and str(orig.get("pubkey_prefix") or "").lower() == raw_key)
        key = raw_key if unchanged else normalise_key(raw_key)
        if key:
            keys.append((key, not unchanged))
        if unchanged:
            chunks.append(block.chunks[oid])  # untouched: keep its text exactly
            new.append(orig)
            continue
        if orig is not None:
            entry = dict(orig)
            entry["name"] = name
            entry["pubkey_prefix"] = key
            if pw is not None:
                entry["admin_password"] = pw
            elif clear_pw:
                entry.pop("admin_password", None)
            lead, _ = RepeatersBlock.split_lead(block.chunks[oid])
            chunks.append(lead + block.render_entry(entry))
            new.append(entry)
        else:
            entry = {"name": name, "pubkey_prefix": key}
            if pw is not None:
                entry["admin_password"] = pw
            chunks.append(block.render_entry(entry))
            new.append(entry)
    # Two keys where one is a prefix of the other would both match one node.
    # Only refuse it when this save introduces the clash.
    for i, (a, a_new) in enumerate(keys):
        for b, b_new in keys[i + 1:]:
            if (a_new or b_new) and (a.startswith(b) or b.startswith(a)):
                raise ValueError(f"public key {a} overlaps {b}: both would match the same node")
    if not new:
        raise ValueError("the list can't be empty (clock_sync.py refuses to run without repeaters)")
    out = block.build(chunks)

    # Check: the result parses, the list is exactly what we meant, nothing else moved.
    parsed = yaml.safe_load(out)
    if parsed.get("repeaters") != new:
        raise ConfigError("internal check failed: the rewritten list doesn't parse back as intended")
    if {k: v for k, v in parsed.items() if k != "repeaters"} != {k: v for k, v in cfg.items() if k != "repeaters"}:
        raise ConfigError("internal check failed: a setting outside `repeaters` would change")
    return out, new


def write_config(path: str, new_text: str, old_text: str) -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(BACKUP_DIR, f"clock_sync.yaml.{stamp}"), "w", encoding="utf-8") as f:
        f.write(old_text)
    os.chmod(os.path.join(BACKUP_DIR, f"clock_sync.yaml.{stamp}"), 0o640)
    backups = sorted(os.listdir(BACKUP_DIR))
    for old in backups[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".clock_sync.yaml.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, st.st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Status: state.json, the last run in clock_sync.log, the bot's database
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


RESULT_RE = re.compile(r"^(\S+ \S+) INFO clock_sync: (.+?): ([A-Z_]+) \((.*)\)\s*$")
DONE_RE = re.compile(r"^(\S+ \S+) INFO clock_sync: Done in ([\d.]+)s: (\d+)/(\d+) succeeded")


START_RE = re.compile(r"^(\S+?)[T ](\d\d:\d\d:\d\d)\S* run-clock-sync: stopping meshcore-bot")
END_MARK = " run-clock-sync: restarting meshcore-bot"


def last_run() -> dict:
    """Per-node outcome of the latest run, keyed by configured name.

    A run is everything from the wrapper's "stopping meshcore-bot" to its
    "restarting meshcore-bot". The latest FINISHED run (one with a "Done in"
    line) is the base; if a newer run is still going, or was stopped early,
    its results so far replace the base's for the nodes it has reached
    ("current": True), so the page never shows a stale result next to a
    fresh last-success time.
    """
    try:
        with open(SYNC_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    runs: list[dict] = []
    cur = None
    for l in lines:
        m = START_RE.match(l)
        if m:
            cur = {"started": f"{m.group(1)} {m.group(2)}", "nodes": {}, "done": None, "ended": False}
            runs.append(cur)
            continue
        if cur is None:
            # The tail began mid-run: collect what's there as an unnamed run.
            cur = {"started": None, "nodes": {}, "done": None, "ended": False}
            runs.append(cur)
        r = RESULT_RE.match(l)
        if r:
            cur["nodes"][r.group(2)] = {"outcome": r.group(3), "detail": r.group(4), "at": r.group(1)[:19]}
        d = DONE_RE.match(l)
        if d:
            cur["done"] = {"finished": d.group(1)[:19], "ok": int(d.group(3)), "total": int(d.group(4))}
        if END_MARK in l:
            cur["ended"] = True
    finished = next((r for r in reversed(runs) if r["done"]), None)
    out: dict = {"nodes": {}}
    if finished:
        out.update(finished["done"])
        out["nodes"] = dict(finished["nodes"])
    latest = runs[-1] if runs else None
    if latest and latest is not finished and latest["started"]:
        out["live"] = {
            "started": latest["started"],
            "state": "stopped early" if latest["ended"] else "in progress",
            "done": len(latest["nodes"]),
        }
        for name, res in latest["nodes"].items():
            out["nodes"][name] = {**res, "current": True}
    return out


def suggest(q: str, limit: int = 15) -> list[dict]:
    q = q.strip()
    if len(q) < 2:
        return []
    try:
        # immutable: never takes a lock or writes; good enough for suggestions.
        con = sqlite3.connect(f"file:{BOT_DB}?immutable=1", uri=True, timeout=2)
        like = f"%{q}%"
        rows = con.execute(
            """SELECT public_key, name, MAX(seen) FROM (
                 SELECT public_key, name, last_heard AS seen FROM complete_contact_tracking
                  WHERE (role = 'repeater' OR device_type IN ('Repeater', 'repeater', '2'))
                    AND (name LIKE ? OR public_key LIKE ?)
                 UNION ALL
                 SELECT public_key, name, last_seen FROM repeater_contacts
                  WHERE name LIKE ? OR public_key LIKE ?)
               GROUP BY public_key ORDER BY 3 DESC LIMIT ?""",
            (like, q.lower() + "%", like, q.lower() + "%", limit),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [{"pubkey": k, "name": n, "seen": str(s or "")[:16]} for k, n, s in rows if k]


def known_matches(prefix: str) -> list[str]:
    """Names of the distinct repeaters the bot has heard whose key starts with prefix."""
    try:
        con = sqlite3.connect(f"file:{BOT_DB}?immutable=1", uri=True, timeout=2)
        rows = con.execute(
            """SELECT public_key, name FROM repeater_contacts WHERE public_key LIKE ?
               UNION SELECT public_key, name FROM complete_contact_tracking
                WHERE public_key LIKE ? AND (role = 'repeater' OR device_type IN ('Repeater', 'repeater', '2'))""",
            (prefix + "%", prefix + "%"),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    names: dict[str, str] = {}
    for k, n in rows:
        names[k] = n or k[:12]
    return sorted(set(names.values())) if len(names) > 1 else []


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def hash_password(pw: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return {"salt": salt.hex(), "hash": dk.hex(), "iter": 200_000}


def check_password(pw: str, rec: dict) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(rec["salt"]), int(rec["iter"]))
    return hmac.compare_digest(dk.hex(), rec["hash"])


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "clock-sync-web"
    auth: dict = {}
    _fails: dict = {}

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.client_address[0]} {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _authed(self) -> bool:
        ip = self.client_address[0]
        fails = [t for t in self._fails.get(ip, []) if time.time() - t < 300]
        self._fails[ip] = fails
        if len(fails) >= 10:
            self._send(429, b"Too many failed logins; wait 5 minutes.\n", "text/plain")
            return False
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                if check_password(pw, self.auth):
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
            fails.append(time.time())
        self._send(401, b"Login required.\n", "text/plain",
                   {"WWW-Authenticate": 'Basic realm="Clock sync", charset="UTF-8"'})
        return False

    def do_GET(self):
        if not self._authed():
            return
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif url.path == "/api/nodes":
            try:
                text, cfg, _ = read_config()
            except (OSError, ConfigError, yaml.YAMLError) as e:
                return self._json(500, {"error": f"can't read {CONFIG}: {e}"})
            state = load_state()
            run = last_run()
            nodes = []
            for i, r in enumerate(cfg["repeaters"]):
                key = str(r.get("pubkey_prefix") or "").lower()
                # state.json is keyed by the first 12 hex of the full key.
                st = max((v for k, v in state.items() if key and (k.startswith(key) or key.startswith(k))),
                         key=lambda v: v.get("last_success_ts") or 0, default=None)
                nodes.append({
                    "id": i,
                    "name": r.get("name") or "",
                    "pubkey": key,
                    "own_password": "admin_password" in r,  # never the value itself
                    "hidden": sorted(k for k in r if k not in ("name", "pubkey_prefix", "admin_password")),
                    "last_success": (st or {}).get("last_success_ts"),
                    "last_run": run.get("nodes", {}).get(r.get("name") or key),
                })
            self._json(200, {"version": version_of(text), "nodes": nodes,
                             "run": {k: v for k, v in run.items() if k != "nodes"},
                             "config": CONFIG})
        elif url.path == "/api/suggest":
            q = parse_qs(url.query).get("q", [""])[0]
            self._json(200, {"results": suggest(q)})
        else:
            self._send(404, b"Not found\n", "text/plain")

    def do_POST(self):
        if not self._authed():
            return
        # CSRF: a cross-site form can't set this header, and a cross-site fetch
        # with it needs a preflight we never answer.
        if self.headers.get("X-Clock-Sync") != "1":
            return self._json(403, {"error": "missing request header"})
        if urlparse(self.path).path != "/api/save":
            return self._send(404, b"Not found\n", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 200_000:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            with _lock:
                text, cfg, block = read_config()
                if body.get("version") != version_of(text):
                    return self._json(409, {"error": "the config changed since you loaded it; reload and try again"})
                new_text, new = apply_ops(text, cfg, block, body.get("nodes") or [])
                if new_text == text:
                    return self._json(200, {"ok": True, "unchanged": True, "version": version_of(text)})
                write_config(CONFIG, new_text, text)
            self.log_message("saved %d repeaters", len(new))
            old_keys = {str(r.get("pubkey_prefix") or "").lower() for r in cfg["repeaters"]}
            warnings = []
            for r in new:
                k = str(r.get("pubkey_prefix") or "").lower()
                if k and k not in old_keys:
                    clash = known_matches(k)
                    if clash:
                        warnings.append(f"{k} matches {len(clash)} repeaters the bot has heard "
                                        f"({', '.join(clash[:4])}); clock_sync uses whichever is in the radio's contacts. "
                                        "A longer prefix would pin it down.")
            self._json(200, {"ok": True, "version": version_of(new_text), "warnings": warnings})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except (ConfigError, yaml.YAMLError, OSError) as e:
            self._json(500, {"error": str(e)})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clock Sync Nodes</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2128;--muted:#5f6b7a;--line:#dde2e8;--accent:#1f6feb;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c22;--ink:#e6edf3;--muted:#9aa7b4;--line:#2b333c;--accent:#4c9aff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--chip:#222a33}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}
.run{display:flex;gap:18px;flex-wrap:wrap;align-items:baseline}
.run b{font-size:18px}
table{width:100%;border-collapse:collapse}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);text-align:left;font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;background:var(--chip)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.small{font-size:12px}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:5px 11px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:default}
input{font:inherit;width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink)}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
label{display:block;font-size:13px;color:var(--muted);margin:0 0 4px}
.grid{display:grid;grid-template-columns:1.2fr 1fr 1fr auto;gap:10px;align-items:end}
.pw{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.pw input[type=password]{flex:1;min-width:140px;margin:0}.pw label{display:flex;gap:4px;align-items:center;margin:0}
.sugg{border:1px solid var(--line);border-radius:7px;margin-top:6px;max-height:240px;overflow:auto;display:none}
.sugg div{padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--line)}
.sugg div:last-child{border-bottom:0}
.sugg div:hover,.sugg div:focus{background:var(--chip);outline:none}
.actions{white-space:nowrap;text-align:right}
.msg{margin:0 0 14px;padding:9px 12px;border-radius:8px;display:none}
.msg.err{display:block;background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}
.msg.info{display:block;background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.edit input{margin-bottom:6px}
@media (max-width:900px){.grid{grid-template-columns:1fr 1fr}}
@media (max-width:700px){
 .grid{grid-template-columns:1fr}
 thead{display:none}
 table,tbody,tr,td{display:block;width:100%}
 tr{border-bottom:1px solid var(--line);padding:6px 0}
 td{border:0;padding:3px 0}
 .actions{text-align:left}
}
</style></head>
<body><main>
<h1>Clock sync nodes</h1>
<p class="sub">Repeaters the 04:00 clock sync logs into (every other day). Changes save straight to <code id="cfg"></code> and apply from the next run.</p>
<div id="msg" class="msg"></div>
<div class="card run" id="run"><span class="muted">Loading…</span></div>

<div class="card">
 <label for="q">Find a repeater the bot has heard</label>
 <input id="q" placeholder="Name or start of public key" autocomplete="off">
 <div class="sugg" id="sugg" role="listbox"></div>
 <div class="grid" style="margin-top:10px">
  <div><label for="nn">Name</label><input id="nn" placeholder="Label used in the log and channel summary"></div>
  <div><label for="nk">Public key or prefix (at least 6 hex)</label>
   <input id="nk" placeholder="e.g. 6df99f" autocomplete="off" spellcheck="false"></div>
  <div><label for="np">Admin password (blank = shared one)</label>
   <input id="np" type="password" autocomplete="new-password" placeholder="Shared password"></div>
  <div><button class="primary" id="add">Add</button></div>
 </div>
</div>

<div class="card" style="padding:4px 10px">
<table><thead><tr><th>Node</th><th>Public key</th><th>Last success</th><th>Last run</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
</div>
<p class="small muted">Keys are matched by prefix against the companion radio's contact list. When the radio has lost a repeater (meshcore-bot purges them when its contact list fills), the clock sync re-adds it from the bot's database first. That needs a prefix matching exactly one repeater the bot has heard, or the full 64-hex key. <b>CONTACT_NOT_FOUND</b> now means it couldn't, and the detail says why. Entries not changed here keep their exact text in the file, and a backup is kept on every save.</p>
</main>
<script>
let data=null, editing=null;
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function msg(t,kind){const m=$("#msg");m.textContent=t||"";m.className="msg"+(t?" "+kind:"");}
function ago(ts){if(!ts)return '<span class="muted">never</span>';const h=(Date.now()/1000-ts)/3600;
 const t=h<1?Math.round(h*60)+" min ago":h<48?Math.round(h)+" h ago":Math.round(h/24)+" days ago";
 const cls=h<=60?"ok":h<=24*7?"warn":"bad";return `<span class="${cls}">${t}</span>`;}
function outcome(r){if(!r)return '<span class="muted">—</span>';
 const cls=/^(SUCCESS|AUTO_REBOOTED)$/.test(r.outcome)?"ok":r.outcome==="ALREADY_CURRENT"?"warn":"bad";
 return `<span class="chip ${cls}">${esc(r.outcome)}</span>`+(r.current?` <span class="small muted">${esc(r.at.slice(11,16))}</span>`:"")+(r.detail?`<div class="small muted">${esc(r.detail)}</div>`:"");}
async function load(){
 const r=await fetch("/api/nodes",{cache:"no-store"});const j=await r.json();
 if(!r.ok){msg(j.error,"err");return;}
 data=j;$("#cfg").textContent=j.config;
 const run=j.run;
 $("#run").innerHTML=(run.live?`<span class="chip ${run.live.state==="in progress"?"warn":"bad"}">${run.live.state==="in progress"?"Run in progress":"Run stopped early"}</span><span>started <b>${esc(run.live.started.slice(11,16))}</b>, ${run.live.done} node${run.live.done===1?"":"s"} done${run.live.state==="in progress"?" — reload for more":""}</span><span class="muted">·</span>`:"")+
 (run.finished?`<span>Last full run <b>${esc(run.finished)}</b></span><span><b class="${run.ok===run.total?"ok":run.ok? "warn":"bad"}">${run.ok}/${run.total}</b> synced</span>`:'<span class="muted">No finished run in the log yet</span>');
 render();}
function render(){
 $("#rows").innerHTML=data.nodes.map((n,i)=>editing===i?`
 <tr class="edit"><td><input id="en" value="${esc(n.name)}"></td><td><input id="ek" value="${esc(n.pubkey)}" spellcheck="false"></td>
 <td colspan="2"><label for="ep">Admin password</label><div class="pw">
  <input id="ep" type="password" autocomplete="new-password" placeholder="${n.own_password?"Own one set — type to replace":"Shared — type to set its own"}">
  ${n.own_password?'<label class="small"><input type="checkbox" id="ec" style="width:auto"> use shared</label>':""}</div>
  ${n.hidden.length?`<div class="small muted">Kept as is: ${n.hidden.map(esc).join(", ")}</div>`:""}</td>
 <td class="actions"><button class="primary" onclick="saveEdit(${i})">Save</button> <button onclick="editing=null;render()">Cancel</button></td></tr>`:`
 <tr><td><b>${esc(n.name)}</b>${n.own_password?' <span class="chip" title="This node has its own admin password">own password</span>':""}${n.hidden.length?`<div class="small muted">+ ${n.hidden.map(esc).join(", ")}</div>`:""}</td>
 <td><code title="${esc(n.pubkey)}">${esc(n.pubkey.length>16?n.pubkey.slice(0,16)+"…":n.pubkey)}</code></td>
 <td>${ago(n.last_success)}</td><td>${outcome(n.last_run)}</td>
 <td class="actions"><button onclick="editing=${i};render()">Edit</button> <button class="danger" onclick="removeNode(${i})">Remove</button></td></tr>`).join("");}
async function save(nodes,okText){
 const r=await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json","X-Clock-Sync":"1"},
  body:JSON.stringify({version:data.version,nodes})});
 const j=await r.json().catch(()=>({error:"bad response"}));
 if(!r.ok){msg(j.error||("HTTP "+r.status),"err");if(r.status===409)await load();return false;}
 editing=null;await load();
 if(j.warnings&&j.warnings.length)msg(okText+" Note: "+j.warnings.join(" "),"err");else msg(j.unchanged?"No change.":okText,"info");return true;}
function current(){return data.nodes.map(n=>({id:n.id,name:n.name,pubkey:n.pubkey}));}
async function saveEdit(i){const ns=current();ns[i].name=$("#en").value.trim();ns[i].pubkey=$("#ek").value.trim();
 if($("#ep").value)ns[i].password=$("#ep").value;else if($("#ec")&&$("#ec").checked)ns[i].clear_password=true;
 await save(ns,`Saved ${ns[i].name}.`);}
async function removeNode(i){const n=data.nodes[i];if(!confirm(`Remove ${n.name} from the clock sync?`))return;
 const ns=current();ns.splice(i,1);await save(ns,`Removed ${n.name}.`);}
$("#add").onclick=async()=>{const name=$("#nn").value.trim(),pubkey=$("#nk").value.trim(),password=$("#np").value;
 if(!name||!pubkey){msg("Enter a name and a public key.","err");return;}
 if(await save([...current(),{id:null,name,pubkey,...(password?{password}:{})}],`Added ${name}.`)){$("#nn").value="";$("#nk").value="";$("#np").value="";$("#q").value="";}};
let t=null;
$("#q").oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{const q=$("#q").value.trim();const s=$("#sugg");
 if(q.length<2){s.style.display="none";return;}
 const j=await (await fetch("/api/suggest?q="+encodeURIComponent(q))).json();
 const listed=k=>data.nodes.some(n=>n.pubkey&&(k.startsWith(n.pubkey)||n.pubkey.startsWith(k)));
 s.innerHTML=j.results.length?j.results.map(r=>`<div tabindex="0" data-k="${esc(r.pubkey)}" data-n="${esc(r.name)}"><b>${esc(r.name)}</b>
  <code class="small muted">${esc(r.pubkey.slice(0,16))}…</code> <span class="small muted">heard ${esc(r.seen)}</span>
  ${listed(r.pubkey)?'<span class="chip warn">already listed</span>':""}</div>`).join(""):'<div class="muted">No match in the bot\'s database</div>';
 s.style.display="block";},250);};
$("#sugg").onclick=e=>{const d=e.target.closest("[data-k]");if(!d)return;$("#nk").value=d.dataset.k.slice(0,6);$("#nn").value=d.dataset.n;$("#sugg").style.display="none";};
$("#sugg").onkeydown=e=>{if(e.key==="Enter")$("#sugg").onclick(e);};
load().catch(e=>msg(String(e),"err"));
</script></body></html>
"""


def redact(text: str) -> str:
    return re.sub(r"^(\s*-?\s*[\w-]*(?:password|secret|token)[\w-]*\s*:).*$", r"\1 <redacted>", text, flags=re.I | re.M)


def main() -> int:
    global CONFIG, AUTH_FILE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--auth-file", default=AUTH_FILE)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--set-password", action="store_true", help="prompt for the web password and save its hash")
    ap.add_argument("--check", action="store_true",
                    help="dry run: parse the config, rewrite it with every entry touched, print the diff (secrets redacted)")
    a = ap.parse_args()
    CONFIG, AUTH_FILE = a.config, a.auth_file

    if a.set_password:
        pw = getpass.getpass("New web password: ")
        if len(pw) < 8 or pw != getpass.getpass("Again: "):
            print("Passwords didn't match or are shorter than 8 characters.", file=sys.stderr)
            return 1
        fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as f:
            json.dump(hash_password(pw), f)
        print(f"Saved {AUTH_FILE}")
        return 0

    if a.check:
        text, cfg, block = read_config()
        reps = cfg["repeaters"]
        print(f"OK: {len(reps)} repeaters; list at indent {len(block.dash_indent)}; "
              f"other top-level keys: {', '.join(k for k in cfg if k != 'repeaters')}")
        same, _ = apply_ops(text, cfg, block, [{"id": i, "name": r.get("name"), "pubkey": r.get("pubkey_prefix")} for i, r in enumerate(reps)])
        print("Saving with no changes leaves the file identical:", same == text)
        # Worst case: every entry edited (comments kept, entry text regenerated).
        chunks = [RepeatersBlock.split_lead(c)[0] + block.render_entry(r) for c, r in zip(block.chunks, reps)]
        rewritten = block.build(chunks)
        ok = yaml.safe_load(rewritten) == cfg
        print("Every entry regenerated still parses to the same config:", ok)
        diff = difflib.unified_diff(redact(text).splitlines(), redact(rewritten).splitlines(), "current", "all-entries-edited", lineterm="", n=1)
        print("\n".join(diff) or "(no textual difference)")
        return 0 if same == text and ok else 1

    try:
        with open(AUTH_FILE) as f:
            Handler.auth = json.load(f)
    except (OSError, ValueError):
        print(f"No password set: run with --set-password first ({AUTH_FILE}).", file=sys.stderr)
        return 1
    read_config()  # fail fast if the file can't be handled
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print(f"clock-sync-web on http://{a.bind}:{a.port}/ editing {CONFIG}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
