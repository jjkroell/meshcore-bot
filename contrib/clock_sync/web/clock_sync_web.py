#!/usr/bin/env python3
"""Web editor for the MeshCore repeater clock-sync list.

Edits the `repeaters:` list in /etc/clock_sync/clock_sync.yaml (read by
/opt/clock_sync/clock_sync.py from cron). Only that list is ever rewritten:
everything else in the file - companion settings, comments - is left
byte-for-byte as it was. Entries you don't touch keep their exact text
(comments included); per-node keys the UI doesn't show are carried over on
edit. Every node has its own admin_password (write-only here: the page is
only told whether one is set); there is no shared one. Every save is re-parsed and
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
import fcntl
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

# peer_parse.py lives with clock_sync.py, so the page and the run read the
# other bot's posts the same way. (../clock_sync is the source tree layout.)
for _d in ("/opt/clock_sync", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "clock_sync")):
    if os.path.exists(os.path.join(_d, "peer_parse.py")):
        sys.path.insert(0, _d)
        break
import peer_parse  # noqa: E402
import schedule  # noqa: E402

CONFIG = "/etc/clock_sync/clock_sync.yaml"
BACKUP_DIR = "/etc/clock_sync/backups"
AUTH_FILE = "/etc/clock_sync/web_auth.json"
STATE_FILE = "/opt/clock_sync/state.json"
SYNC_LOG = "/var/log/clock_sync/clock_sync.log"
BOT_DB = "/opt/meshcore-bot/meshcore_bot.db"
RUN_REQUEST = "/etc/clock_sync/run-request"  # picked up by clock-sync-run.path (root)
RUN_LOCK = "/run/clock_sync.lock"  # held by run-clock-sync.sh for the whole run
ARCHIVE_DB = "/var/lib/clock-sync-web/messages.db"  # our permanent copy of the bot's message log
ARCHIVE_EVERY = 60  # seconds
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
    password?: str (set or replace this node's admin password)}. A missing password
    keeps whatever the node has; a new or edited node must end up with one.
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
        if orig is not None and pw is not None and orig.get("admin_password") == pw:
            pw = None  # same as before
        unchanged = (orig is not None and orig.get("name") == name and pw is None
                     and str(orig.get("pubkey_prefix") or "").lower() == raw_key)
        if not unchanged and pw is None and not (orig or {}).get("admin_password"):
            raise ValueError(f"{name} needs an admin password")
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


OUTCOMES = "SUCCESS|LOGIN_FAILED|LOGIN_TIMEOUT|ALREADY_CURRENT|AUTO_REBOOTED|CMD_ERROR|SEND_FAILED|CONTACT_NOT_FOUND|SKIPPED_PEER"
RESULT_RE = re.compile(r"^(\S+ \S+) INFO clock_sync: (.+?): (" + OUTCOMES + r")(?: \((.*)\))?\s*$")
# Written on every silent success before 2026-11-02; not worth showing.
NOISE_RE = re.compile(r"\s*(?:- )?no explicit ack \(normal - firmware is silent on success\)\s*")
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
            detail = NOISE_RE.sub(" ", r.group(4) or "").strip(" -")
            detail = re.sub(r"\(\s*\)", "", detail).strip()
            cur["nodes"][r.group(2)] = {"outcome": r.group(3), "detail": detail, "at": r.group(1)[:19]}
        d = DONE_RE.match(l)
        if d:
            cur["done"] = {"finished": d.group(1)[:19], "ok": int(d.group(3)), "total": int(d.group(4))}
        if END_MARK in l:
            cur["ended"] = True
    finished = next((r for r in reversed(runs) if r["done"]), None)
    out: dict = {"nodes": {}}
    # Each node's most recent result from any run in the tail, so a manual
    # run of a few nodes doesn't blank the others.
    for r in runs:
        out["nodes"].update(r["nodes"])
    if finished:
        out.update(finished["done"])
    latest = runs[-1] if runs else None
    if latest and latest is not finished and latest["started"]:
        out["live"] = {
            "started": latest["started"],
            "state": "stopped early" if latest["ended"] else "in progress",
            "done": len(latest["nodes"]),
        }
        for name, res in latest["nodes"].items():
            out["nodes"][name] = {**res, "current": True}
    for res in out["nodes"].values():
        res.setdefault("current", False)
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


def run_in_progress() -> bool:
    """Whether run-clock-sync.sh (cron, manual or ours) holds its lock."""
    try:
        fd = os.open(RUN_LOCK, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Message archive + the other time-sync bot(s)
# --------------------------------------------------------------------------
# meshcore-bot logs every message it hears to message_stats but deletes rows
# older than 7 days. Copy them here, keyed by its row id, so nothing is lost.

def _archive_con() -> sqlite3.Connection:
    con = sqlite3.connect(ARCHIVE_DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS messages (
        bot_id INTEGER PRIMARY KEY,   -- message_stats.id in the bot's database
        received_at INTEGER,          -- when the bot heard it (unix, bot clock)
        sent_ts INTEGER,              -- the sender's own timestamp
        sender TEXT, channel TEXT, content TEXT, is_dm INTEGER,
        hops INTEGER, snr REAL, rssi INTEGER, path TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_chan_time ON messages(channel, received_at)")
    return con


def archive_once() -> int:
    con = _archive_con()
    try:
        last = con.execute("SELECT COALESCE(MAX(bot_id), 0) FROM messages").fetchone()[0]
        # immutable: the bot's DB is WAL and this user can't create its -shm, so
        # mode=ro fails. An immutable read can lag the WAL, so re-scan the last
        # 500 ids each pass; INSERT OR IGNORE keeps that free of duplicates.
        src = sqlite3.connect(f"file:{BOT_DB}?immutable=1", uri=True, timeout=5)
        try:
            rows = src.execute(
                "SELECT id, CAST(strftime('%s', created_at) AS INTEGER), timestamp, sender_id, channel, content, "
                "is_dm, hops, snr, rssi, path FROM message_stats WHERE id > ? ORDER BY id LIMIT 5000", (max(0, last - 500),)
            ).fetchall()
        finally:
            src.close()
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        return con.total_changes - before
    finally:
        con.close()


def archiver() -> None:
    while True:
        try:
            n = archive_once()
            if n:
                sys.stderr.write(f"archived {n} message(s)\n")
        except sqlite3.Error as e:
            sys.stderr.write(f"message archive: {e}\n")
        time.sleep(ARCHIVE_EVERY)


def channel_messages(channel: str, since_days: float = 60, limit: int = 2000) -> list[tuple]:
    """(received_at, sender, channel, content) on `channel`, newest first: the archive,
    topped up from the bot's database for anything not archived yet."""
    if not channel:
        return []
    chans = sorted({channel.lower(), channel.lower().lstrip("#"), "#" + channel.lower().lstrip("#")})
    q = ",".join("?" * len(chans))
    since = int(time.time() - since_days * 86400)
    seen: dict[int, tuple] = {}
    for db, sql in (
        (f"file:{ARCHIVE_DB}?mode=ro",
         f"SELECT bot_id, COALESCE(received_at, sent_ts), sender, channel, content FROM messages "
         f"WHERE is_dm = 0 AND lower(channel) IN ({q}) AND COALESCE(received_at, sent_ts) >= ? "
         f"ORDER BY bot_id DESC LIMIT {limit}"),
        (f"file:{BOT_DB}?immutable=1",
         f"SELECT id, COALESCE(CAST(strftime('%s', created_at) AS INTEGER), timestamp), sender_id, channel, content "
         f"FROM message_stats WHERE is_dm = 0 AND lower(channel) IN ({q}) AND timestamp >= ? "
         f"ORDER BY id DESC LIMIT {limit}"),
    ):
        try:
            con = sqlite3.connect(db, uri=True, timeout=3)
            try:
                for bid, ts, sender, ch, content in con.execute(sql, (*chans, since)):
                    seen.setdefault(bid, (ts or 0, sender or "", ch or "", content or ""))
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return [seen[k] for k in sorted(seen, reverse=True)]


def schedule_view(cfg: dict) -> dict:
    sched, from_file = schedule.load()
    if not from_file and "peer_skip_hours" in cfg:
        sched["peer_skip_hours"] = cfg["peer_skip_hours"]
    nxt = schedule.next_run(sched)
    return {**sched, "from_file": from_file, "next": nxt.timestamp() if nxt else None}


def request_run(names: list[str]) -> None:
    """Drop a run request for the root path unit: one node name per line,
    empty for every node."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(RUN_REQUEST), prefix=".run-request.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(n + "\n" for n in names))
    os.chmod(tmp, 0o640)
    os.replace(tmp, RUN_REQUEST)


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
            running = run_in_progress()
            if run.get("live", {}).get("state") == "in progress" and not running:
                run["live"]["state"] = "stopped early"  # no lock held: it isn't running
            channel = str(cfg.get("summary_channel") or "")
            peers = cfg.get("peer_bots") or []
            peers = [peers] if isinstance(peers, str) else [str(p) for p in peers]
            msgs = channel_messages(channel)
            try:
                index = peer_parse.name_index(f"file:{BOT_DB}?immutable=1")
            except sqlite3.Error:
                index = {}
            peer_ok = peer_parse.peer_successes(msgs, peers, channel, index)
            me = str(cfg.get("bot_name") or "this bot")
            nodes = []
            for i, r in enumerate(cfg["repeaters"]):
                key = str(r.get("pubkey_prefix") or "").lower()
                # state.json is keyed by the first 12 hex of the full key.
                st = max((v for k, v in state.items() if key and (k.startswith(key) or key.startswith(k))),
                         key=lambda v: v.get("last_success_ts") or 0, default=None)
                # Who synced it last: this bot (state.json) or a peer (its #time-sync posts).
                by = None
                if st and st.get("last_success_ts"):
                    off = st.get("last_offset_s")
                    by = {"bot": me, "ts": st["last_success_ts"], "peer": False,
                          "note": peer_parse.fmt_offset(off) if isinstance(off, (int, float)) else ""}
                peer = peer_parse.latest_for(peer_ok, key)
                if peer and (by is None or peer["ts"] > by["ts"]):
                    by = {"bot": peer["sender"], "ts": peer["ts"], "peer": True, "note": peer["note"]}
                nodes.append({
                    "id": i,
                    "name": r.get("name") or "",
                    "pubkey": key,
                    "has_password": bool(r.get("admin_password")),  # never the value itself
                    "hidden": sorted(k for k in r if k not in ("name", "pubkey_prefix", "admin_password")),
                    "last_success": by["ts"] if by else None,
                    "last_by": by,
                    "last_run": run.get("nodes", {}).get(r.get("name") or key),
                })
            self._json(200, {"version": version_of(text), "nodes": nodes,
                             "running": running, "requested": os.path.exists(RUN_REQUEST),
                             "run": {k: v for k, v in run.items() if k != "nodes"},
                             "config": CONFIG, "channel": channel, "peers": peers, "me": me,
                             "schedule": schedule_view(cfg),
                             "channel_msgs": [{"ts": ts, "sender": s, "text": c} for ts, s, _, c in msgs[:25]]})
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
        path = urlparse(self.path).path
        if path == "/api/run":
            return self._run()
        if path == "/api/schedule":
            return self._schedule()
        if path != "/api/save":
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


def _run_handler(self):
    """POST /api/run {nodes: [names]} - [] or missing = every node."""
    try:
        n = int(self.headers.get("Content-Length", "0"))
        if n > 20_000:
            raise ValueError("request too large")
        body = json.loads(self.rfile.read(n) or b"{}")
        wanted = body.get("nodes") or []
        if not isinstance(wanted, list) or not all(isinstance(x, str) for x in wanted):
            raise ValueError("nodes must be a list of names")
        with _lock:
            _, cfg, _ = read_config()
            names = {str(r.get("name") or "") for r in cfg["repeaters"]}
            unknown = [x for x in wanted if x not in names]
            if unknown:
                raise ValueError(f"not on the list: {', '.join(unknown)}; reload the page")
            if run_in_progress() or os.path.exists(RUN_REQUEST):
                return self._json(409, {"error": "a clock sync is already running or starting"})
            request_run(sorted(set(wanted), key=wanted.index))
        self.log_message("run requested: %s", ", ".join(wanted) or "all nodes")
        self._json(200, {"ok": True})
    except ValueError as e:
        self._json(400, {"error": str(e)})
    except (ConfigError, yaml.YAMLError, OSError) as e:
        self._json(500, {"error": str(e)})


Handler._run = _run_handler


def _schedule_handler(self):
    """POST /api/schedule {days, time, peer_skip_hours, paused}."""
    try:
        n = int(self.headers.get("Content-Length", "0"))
        if n > 5_000:
            raise ValueError("request too large")
        body = json.loads(self.rfile.read(n) or b"{}")
        with _lock:
            current, _ = schedule.load()  # fields not sent keep their current value
            sched = schedule.save({**current, **{k: body[k] for k in ("days", "time", "peer_skip_hours", "paused")
                                                 if k in body}})
        self.log_message("schedule saved: %s", json.dumps(sched))
        nxt = schedule.next_run(sched)
        self._json(200, {"ok": True, "schedule": {**sched, "from_file": True,
                                                    "next": nxt.timestamp() if nxt else None}})
    except ValueError as e:
        self._json(400, {"error": str(e)})
    except OSError as e:
        self._json(500, {"error": str(e)})


Handler._schedule = _schedule_handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clock Sync Nodes</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2128;--muted:#5f6b7a;--line:#dde2e8;--accent:#1f6feb;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--chip:#eef1f5;--bot:#8250df}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c22;--ink:#e6edf3;--muted:#9aa7b4;--line:#2b333c;--accent:#4c9aff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--chip:#222a33;--bot:#bc8cff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}
.run{display:flex;gap:18px;flex-wrap:wrap;align-items:baseline}
.runbar{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
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
.pw{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.pwf{position:relative;flex:1;min-width:140px}.pwf input,.edit .pwf input{margin:0;padding-right:40px}.eye{position:absolute;top:0;right:0;bottom:0;width:38px;padding:0;border:0;background:none;color:var(--muted);display:flex;align-items:center;justify-content:center}.eye:hover{color:var(--ink)}.eye:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:7px}.eye svg{width:18px;height:18px}.eye .slash{display:none}.eye[aria-pressed=true] .slash{display:inline}.pw label{display:flex;gap:4px;align-items:center;margin:0}
.sugg{border:1px solid var(--line);border-radius:7px;margin-top:6px;max-height:240px;overflow:auto;display:none}
.sugg div{padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--line)}
.sugg div:last-child{border-bottom:0}
.sugg div:hover,.sugg div:focus{background:var(--chip);outline:none}
.actions{white-space:nowrap;text-align:right}
.msg{margin:0 0 14px;padding:9px 12px;border-radius:8px;display:none}
.msg.err{display:block;background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}
.msg.info{display:block;background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.edit input{margin-bottom:6px}
.days{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.days button{min-width:52px}
.days button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.srow{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.srow input[type=time]{width:auto}.srow input[type=number]{width:80px}
.srow label{display:flex;gap:6px;align-items:center;margin:0;color:var(--ink);font-size:15px}
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
<p class="sub">Repeaters the scheduled clock sync logs into. A node another time-sync bot reported synced within the skip window is skipped on scheduled runs and Run all. Changes save straight to <code id="cfg"></code> and apply from the next run.</p>
<div id="msg" class="msg"></div>
<div class="card runbar"><div class="run" id="run"><span class="muted">Loading…</span></div>
 <button class="primary" id="runall">Run all now</button></div>

<div class="card" id="sched">
 <div><b>Schedule</b> <span class="muted" id="snext"></span></div>
 <div class="days" id="sdays"></div>
 <div class="srow">
  <label>at <input type="time" id="stime" step="60"></label>
  <label>skip if another bot synced within <input type="number" id="sskip" min="0" max="168" step="1"> h</label>
 </div>
 <div class="srow" style="margin-top:10px;justify-content:space-between">
  <label><input type="checkbox" id="spause" style="width:auto"> pause automatic runs</label>
  <button class="primary" id="ssave" disabled>Save schedule</button>
 </div>
</div>

<div class="card">
 <label for="q">Find a repeater the bot has heard</label>
 <input id="q" placeholder="Name or start of public key" autocomplete="off">
 <div class="sugg" id="sugg" role="listbox"></div>
 <div class="grid" style="margin-top:10px">
  <div><label for="nn">Name</label><input id="nn" placeholder="Label used in the log and channel summary"></div>
  <div><label for="nk">Public key or prefix (at least 6 hex)</label>
   <input id="nk" placeholder="e.g. 6df99f" autocomplete="off" spellcheck="false"></div>
  <div><label for="np">Admin password</label>
   <div class="pwf"><input id="np" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="none" placeholder="This node's admin password"></div></div>
  <div><button class="primary" id="add">Add</button></div>
 </div>
</div>

<div class="card" style="padding:4px 10px">
<table><thead><tr><th>Node</th><th>Public key</th><th>Last success</th><th>Last run</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
</div>
<div class="card" id="chan" style="display:none"><div class="runbar"><b id="chanh"></b><span class="small muted" id="chanp"></span></div>
<div id="chanl" class="small" style="margin-top:8px"></div></div>
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
function when(ts){const d=new Date(ts*1000),now=new Date();const hm=d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",hour12:false});
 return d.toDateString()===now.toDateString()?"today "+hm:d.toLocaleDateString([],{month:"short",day:"numeric"})+" "+hm;}
// Nov-04 04:06, from either the log's local time or a peer post's timestamp.
const MONTHS=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const stamp=d=>{const p=x=>String(x).padStart(2,"0");return `${MONTHS[d.getMonth()]}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;};
function outcome(r,b){
 // Whichever is newer: our last attempt, or a successful sync by a peer bot.
 const ran=r&&new Date(r.at.replace(" ","T")),useBy=b&&(!r||(b.peer&&b.ts*1000>ran));
 if(!r&&!b)return '<span class="muted">—</span>';
 const res=useBy?"SUCCESS":r.outcome;
 const cls=/^(SUCCESS|AUTO_REBOOTED|SKIPPED_PEER)$/.test(res)?"ok":res==="ALREADY_CURRENT"?"warn":"bad";
 const label=res==="SKIPPED_PEER"?"SKIPPED":res;
 const at=stamp(useBy?new Date(b.ts*1000):ran);
 const fromBy=useBy||(res==="SKIPPED_PEER"&&b),who=fromBy?b.bot:data.me;
 const line=(fromBy?b.note||"":r.detail||"").replace(/ \((ahead|behind)\)/,"").replace(/\s*\(?contact re-added\)?/,"");
 return `<span class="chip ${cls}">${esc(label)}</span> <span class="small muted">${esc(at)}</span><div class="small"><b style="color:var(--${who===data.me?"bot":"accent"})">${esc(who)}</b>${line?` <span class="muted">${esc(line)}</span>`:""}</div>`;}
async function load(){
 const r=await fetch("/api/nodes",{cache:"no-store"});const j=await r.json();
 if(!r.ok){msg(j.error,"err");return;}
 data=j;$("#cfg").textContent=j.config;
 const run=j.run;
 $("#run").innerHTML=(run.live?`<span class="chip ${run.live.state==="in progress"?"warn":"bad"}">${run.live.state==="in progress"?"Run in progress":"Run stopped early"}</span><span>started <b>${esc(run.live.started.slice(11,16))}</b>, ${run.live.done} node${run.live.done===1?"":"s"} done${run.live.state==="in progress"?" — reload for more":""}</span><span class="muted">·</span>`:"")+
 (run.finished?`<span>Last finished run <b>${esc(run.finished)}</b></span><span><b class="${run.ok===run.total?"ok":run.ok? "warn":"bad"}">${run.ok}/${run.total}</b> synced</span>`:'<span class="muted">No finished run in the log yet</span>');
 if(!schedDirty)showSched(j.schedule);
 if(j.channel){$("#chan").style.display="";$("#chanh").textContent=j.channel;
  $("#chanp").textContent=j.peers.length?"Peer bot"+(j.peers.length>1?"s":"")+": "+j.peers.join(", ")+" — their OK / SUCCESS posts count for the skip":"No peer bots configured";
  const pn=new Set(j.peers.map(p=>p.toLowerCase().replace(/[^0-9a-z]/g,"")));
  $("#chanl").innerHTML=j.channel_msgs.length?j.channel_msgs.map(m=>`<div style="padding:4px 0;border-bottom:1px solid var(--line)"><span class="muted">${esc(when(m.ts))}</span> <b style="${pn.has(m.sender.toLowerCase().replace(/[^0-9a-z]/g,""))?"color:var(--accent)":""}">${esc(m.sender)}</b>: <code>${esc(m.text)}</code></div>`).join("")+'<div class="muted" style="padding-top:6px">Our own posts don\'t show here — the bot is offline while it sends them.</div>':'<span class="muted">Nothing heard on this channel yet.</span>';}
 render();}
const EYE=`<button type="button" class="eye" aria-pressed="false" aria-label="Show password" title="Show password"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/><path class="slash" d="M4 4l16 16"/></svg></button>`;
function showPw(input,show){input.type=show?"text":"password";const b=input.parentNode.querySelector(".eye"),t=show?"Hide password":"Show password";
 b.setAttribute("aria-pressed",show);b.setAttribute("aria-label",t);b.title=t;}
document.addEventListener("click",e=>{const b=e.target.closest(".eye");if(!b)return;const i=b.parentNode.querySelector("input");showPw(i,i.type==="password");i.focus();});
$("#np").insertAdjacentHTML("afterend",EYE);
const rowKey=n=>`${n.id}|${n.name}|${n.pubkey}`;
const row=(n,i)=>editing===i?`
 <tr class="edit" data-k="${esc(rowKey(n))}"><td><input id="en" value="${esc(n.name)}"></td><td><input id="ek" value="${esc(n.pubkey)}" spellcheck="false"></td>
 <td colspan="2"><label for="ep">Admin password</label><div class="pw">
  <div class="pwf"><input id="ep" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="none" placeholder="${n.has_password?"Set — type to replace":"None set — type one"}">${EYE}</div></div>
  ${n.hidden.length?`<div class="small muted">Kept as is: ${n.hidden.map(esc).join(", ")}</div>`:""}</td>
 <td class="actions"><button class="primary" onclick="saveEdit(${i})">Save</button> <button onclick="editing=null;render()">Cancel</button></td></tr>`:`
 <tr><td><b>${esc(n.name)}</b>${n.has_password?"":' <span class="chip bad" title="No admin password set: the clock sync skips this node. Edit it to set one.">no password</span>'}${n.hidden.length?`<div class="small muted">+ ${n.hidden.map(esc).join(", ")}</div>`:""}</td>
 <td><code title="${esc(n.pubkey)}">${esc(n.pubkey.length>16?n.pubkey.slice(0,16)+"…":n.pubkey)}</code></td>
 <td>${ago(n.last_success)}</td><td>${outcome(n.last_run,n.last_by)}</td>
 <td class="actions"><button class="runone" onclick="runNow([data.nodes[${i}].name])" ${busy()?"disabled":""}>Run</button> <button onclick="editing=${i};render()">Edit</button> <button class="danger" onclick="removeNode(${i})">Remove</button></td></tr>`;
function render(){
 // A refresh mustn't touch an open Edit row: it holds what's being typed. Rebuild
 // the rows around it, unless the node it was opened on has changed.
 const box=$("#rows"),open=box.querySelector("tr.edit"),n=data.nodes[editing];
 if(open&&n&&open.dataset.k===rowKey(n)){
  [...box.children].forEach(r=>{if(r!==open)r.remove();});
  open.insertAdjacentHTML("beforebegin",data.nodes.slice(0,editing).map(row).join(""));
  open.insertAdjacentHTML("afterend",data.nodes.slice(editing+1).map((m,k)=>row(m,editing+1+k)).join(""));
 }else box.innerHTML=data.nodes.map(row).join("");
 $("#runall").disabled=busy();$("#runall").textContent=data.requested?"Starting…":data.running?"Running…":"Run all now";
 clearTimeout(poll);if(busy())poll=setTimeout(()=>load().catch(()=>{}),10000);}
let poll=null;
const DAYS=["mon","tue","wed","thu","fri","sat","sun"];let sdays=new Set(),schedDirty=false;
function showSched(s){sdays=new Set(s.days);$("#stime").value=s.time;$("#sskip").value=s.peer_skip_hours;$("#spause").checked=s.paused;
 $("#snext").textContent=s.paused?"· paused":s.next?"· next "+when(s.next):"· no days picked";schedDirty=false;$("#ssave").disabled=true;drawDays();}
function drawDays(){$("#sdays").innerHTML=DAYS.map(d=>`<button type="button" data-d="${d}" class="${sdays.has(d)?"on":""}" aria-pressed="${sdays.has(d)}">${d[0].toUpperCase()+d.slice(1)}</button>`).join("");}
function dirty(){schedDirty=true;$("#ssave").disabled=false;}
$("#sdays").onclick=e=>{const b=e.target.closest("[data-d]");if(!b)return;const d=b.dataset.d;sdays.has(d)?sdays.delete(d):sdays.add(d);drawDays();dirty();};
["#stime","#sskip","#spause"].forEach(id=>$(id).addEventListener("input",dirty));
$("#ssave").onclick=async()=>{
 const body={days:DAYS.filter(d=>sdays.has(d)),time:$("#stime").value,peer_skip_hours:Number($("#sskip").value),paused:$("#spause").checked};
 const r=await fetch("/api/schedule",{method:"POST",headers:{"Content-Type":"application/json","X-Clock-Sync":"1"},body:JSON.stringify(body)});
 const j=await r.json().catch(()=>({error:"bad response"}));
 if(!r.ok){msg(j.error||("HTTP "+r.status),"err");return;}
 showSched(j.schedule);msg(j.schedule.paused?"Schedule saved — automatic runs paused.":j.schedule.next?"Schedule saved. Next run "+when(j.schedule.next)+".":"Schedule saved — no days picked, so no automatic runs.","info");};
const busy=()=>!!(data&&(data.running||data.requested));
async function runNow(names){
 const what=names.length?names.join(", "):`all ${data.nodes.length} nodes`;
 const mins=Math.max(1,Math.round((names.length||data.nodes.length)*50/60));
 if(!confirm(`Run the clock sync now for ${what}?\n\nThe bot goes offline while it runs (about ${mins} min).`))return;
 const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json","X-Clock-Sync":"1"},body:JSON.stringify({nodes:names})});
 const j=await r.json().catch(()=>({error:"bad response"}));
 if(!r.ok){msg(j.error||("HTTP "+r.status),"err");return;}
 msg(`Clock sync started for ${what}. Results appear below as each node finishes.`,"info");
 data.requested=true;render();setTimeout(()=>load().catch(()=>{}),3000);}
$("#runall").onclick=()=>runNow([]);
async function save(nodes,okText){
 const r=await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json","X-Clock-Sync":"1"},
  body:JSON.stringify({version:data.version,nodes})});
 const j=await r.json().catch(()=>({error:"bad response"}));
 if(!r.ok){msg(j.error||("HTTP "+r.status),"err");if(r.status===409)await load();return false;}
 editing=null;await load();
 if(j.warnings&&j.warnings.length)msg(okText+" Note: "+j.warnings.join(" "),"err");else msg(j.unchanged?"No change.":okText,"info");return true;}
function current(){return data.nodes.map(n=>({id:n.id,name:n.name,pubkey:n.pubkey}));}
async function saveEdit(i){const ns=current();ns[i].name=$("#en").value.trim();ns[i].pubkey=$("#ek").value.trim();
 if($("#ep").value)ns[i].password=$("#ep").value;
 await save(ns,`Saved ${ns[i].name}.`);}
async function removeNode(i){const n=data.nodes[i];if(!confirm(`Remove ${n.name} from the clock sync?`))return;
 const ns=current();ns.splice(i,1);await save(ns,`Removed ${n.name}.`);}
$("#add").onclick=async()=>{const name=$("#nn").value.trim(),pubkey=$("#nk").value.trim(),password=$("#np").value;
 if(!name||!pubkey||!password){msg("Enter a name, a public key and the node's admin password.","err");return;}
 if(await save([...current(),{id:null,name,pubkey,password}],`Added ${name}.`)){$("#nn").value="";$("#nk").value="";$("#np").value="";showPw($("#np"),false);$("#q").value="";}};
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
    if os.path.isdir(os.path.dirname(ARCHIVE_DB)):
        threading.Thread(target=archiver, daemon=True, name="archiver").start()
    else:
        print(f"No {os.path.dirname(ARCHIVE_DB)}: message archive off", file=sys.stderr)
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print(f"clock-sync-web on http://{a.bind}:{a.port}/ editing {CONFIG}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
