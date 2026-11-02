#!/usr/bin/env python3
"""
Daily clock-sync job for MeshCore repeaters.

Logs into each configured repeater over the mesh (via a pyMC companion
identity's TCP frame-protocol port) and sends `clock sync`, which makes the
remote node adopt this box's (NTP-synced) clock. Config format is documented
in scripts/clock_sync.yaml.example.

Run standalone (not part of the `repeater` package) so it has no dependency
on the openhop-repeater daemon's own venv or release pipeline - it only
needs the `meshcore` client library and PyYAML, installed in its own venv.
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from meshcore import EventType, MeshCore

import peer_parse  # next to this script
import schedule  # the web page's schedule (also holds the peer skip window)

logger = logging.getLogger("clock_sync")

DEFAULT_LOGIN_TIMEOUT = 20
DEFAULT_REPLY_TIMEOUT = 20
DEFAULT_DELAY_BETWEEN_NODES = 30
DEFAULT_CHANNEL_SCAN_LIMIT = 20
DEFAULT_STATE_FILE = "/opt/clock_sync/state.json"
DEFAULT_BOT_DB = "/opt/meshcore-bot/meshcore_bot.db"
DEFAULT_CLKREBOOT_MIN_HOURS = 12  # must be well under the ~48h run cadence
DEFAULT_CLKREBOOT_RETRY_DELAY = 30  # seconds to let a node reboot before retrying `clock sync`
CHAN_MSG_MAX_BYTES = 120  # UTF-8 bytes; the radio prepends "<our name>: " inside a ~160-byte limit
DEFAULT_PEER_SKIP_HOURS = 24


class Outcome:
    SUCCESS = "SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    ALREADY_CURRENT = "ALREADY_CURRENT"  # flagged, auto-clkreboot suppressed (see detail)
    AUTO_REBOOTED = "AUTO_REBOOTED"  # clkreboot issued automatically
    CMD_ERROR = "CMD_ERROR"
    SEND_FAILED = "SEND_FAILED"
    CONTACT_NOT_FOUND = "CONTACT_NOT_FOUND"
    SKIPPED_PEER = "SKIPPED_PEER"  # another time-sync bot reported this node synced recently


@dataclass
class SyncResult:
    node: str
    outcome: str
    detail: str = ""
    offset: Optional[float] = None  # node clock minus ours at login, seconds (+ = node ahead)


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("repeaters"):
        raise ValueError(f"No 'repeaters' list in {path}")
    if not cfg.get("admin_password"):
        raise ValueError(f"No 'admin_password' in {path}")
    return cfg


def load_state(path: str) -> dict:
    """Per-node last-known-good sync times, keyed by pubkey_prefix.

    Used in place of an actual clock-skew reading (the firmware has no way to
    read a remote node's RTC - only set it, see clock_sync.yaml.example) and
    to gate auto-clkreboot against retriggering on a node that only just
    succeeded (see _handle_already_current).
    """
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def find_contact(mc: MeshCore, node_cfg: dict) -> Optional[dict]:
    """Match strictly by pubkey_prefix when one is configured - never fall
    back to name for that entry. Several names on this mesh are shared by
    multiple distinct contacts (decoy/retired/other-operator nodes with the
    same display name); matching by name whenever it happened to come first
    in mc.contacts silently targeted the wrong physical node for 4 of 8
    configured repeaters (confirmed 2026-07-20: Rathtrevor, Cokley 4, KU
    East, and L'ville Lookout all resolved to a same-named decoy instead of
    the pubkey_prefix actually configured for them - which is very likely
    why those four showed persistent LOGIN_TIMEOUT rather than a real
    reachability problem). Name-only matching remains for entries that don't
    configure a pubkey_prefix at all.
    """
    prefix = node_cfg.get("pubkey_prefix")
    if prefix:
        for contact in mc.contacts.values():
            if contact["public_key"].lower().startswith(prefix.lower()):
                return contact
        return None

    name = node_cfg.get("name")
    if name:
        for contact in mc.contacts.values():
            if contact.get("adv_name") == name:
                return contact
    return None


def _flood_contact(rec: dict) -> dict:
    """A contact record in the shape meshcore's add_contact wants, set to flood
    (the run resets the path anyway; a stored direct path may be stale)."""
    name = str(rec.get("adv_name") or "")
    raw = name.encode()[:31]
    while raw:
        try:
            name = raw.decode()
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    else:
        name = ""
    return {
        "public_key": str(rec["public_key"]).lower(),
        "type": int(rec.get("type") or 2),
        "flags": int(rec.get("flags") or 0),
        "out_path_len": -1,
        "out_path": "",
        "out_path_hash_mode": 0,
        "adv_name": name,
        "last_advert": int(rec.get("last_advert") or 0),
        "adv_lat": float(rec.get("adv_lat") or 0.0),
        "adv_lon": float(rec.get("adv_lon") or 0.0),
    }


def lookup_contact_record(bot_db: str, key: str, name: str) -> tuple[Optional[dict], str]:
    """Find a repeater the companion has lost, so it can be re-added.

    meshcore-bot (auto_manage_contacts = bot) purges repeaters from the
    companion when its contact list fills, and its database keeps each one's
    full contact record (repeater_contacts.contact_data). Look the configured
    key up there - a 6-hex prefix is enough if it matches exactly one
    repeater. A full 64-hex key in the config works even for a repeater the
    bot has never heard (re-added by key and name alone).

    Returns (contact record, where it came from) or (None, why not)."""
    key = key.lower()
    full = len(key) == 64
    records: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{bot_db}?immutable=1", uri=True, timeout=5)
        try:
            for pk, data, rname, lat, lon in con.execute(
                "SELECT public_key, contact_data, name, latitude, longitude FROM repeater_contacts "
                "WHERE public_key LIKE ?", (key + "%",)
            ):
                rec = None
                if data:
                    try:
                        rec = json.loads(data)
                    except ValueError:
                        rec = None
                if not isinstance(rec, dict) or not rec.get("public_key"):
                    rec = {"public_key": pk, "adv_name": rname, "adv_lat": lat, "adv_lon": lon}
                records[pk.lower()] = rec
            for pk, rname, lat, lon in con.execute(
                "SELECT public_key, name, latitude, longitude FROM complete_contact_tracking "
                "WHERE public_key LIKE ? AND (role = 'repeater' OR device_type IN ('Repeater', 'repeater', '2'))",
                (key + "%",),
            ):
                records.setdefault(pk.lower(), {"public_key": pk, "adv_name": rname, "adv_lat": lat, "adv_lon": lon})
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.warning("Bot database %s unreadable: %s", bot_db, e)
        if not full:
            return None, f"couldn't read the bot's database ({e})"

    if len(records) > 1:
        return None, f"prefix {key} matches {len(records)} repeaters in the bot's database - use a longer key"
    if records:
        rec = next(iter(records.values()))
        return _flood_contact(rec), "the bot's database"
    if full:
        return _flood_contact({"public_key": key, "adv_name": name}), "the full key in the config"
    return None, "not in the bot's database either - enter its full public key"


async def restore_contact(mc: MeshCore, node_cfg: dict, bot_db: str) -> tuple[Optional[dict], str]:
    """Re-add a configured repeater the companion no longer has."""
    key = str(node_cfg.get("pubkey_prefix") or "").strip()
    if not key:
        return None, "no pubkey_prefix to re-add it by"
    rec, source = lookup_contact_record(bot_db, key, node_cfg.get("name") or "")
    if rec is None:
        return None, source
    res = await mc.commands.add_contact(rec)
    if res is None or res.type == EventType.ERROR:
        detail = getattr(res, "payload", None)
        return None, f"re-adding it failed ({detail}) - the radio's contact list may be full"
    mc.contacts[rec["public_key"]] = rec
    logger.info("%s: re-added to the companion's contacts from %s (%s)",
                node_cfg.get("name") or key, source, rec["public_key"][:12])
    return rec, source


def load_peer_successes(bot_db: str, channel: str, peers: list[str], since_ts: float) -> list[dict]:
    """Every node a peer bot reported synced OK on `channel` since `since_ts`
    ({ts, sender, key, note}, see peer_parse). Read from meshcore-bot's
    message_stats (it logs every channel message it hears, kept ~7 days); the
    bot is stopped while this script runs, so the file is quiet."""
    # mode=ro sees the WAL (root can create its -shm); immutable is the fallback.
    for mode in ("mode=ro", "immutable=1"):
        uri = f"file:{bot_db}?{mode}"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                rows = con.execute(
                    "SELECT COALESCE(CAST(strftime('%s', created_at) AS INTEGER), timestamp), sender_id, channel, content "
                    "FROM message_stats WHERE is_dm = 0 AND timestamp >= ? ORDER BY id DESC",
                    (int(since_ts) - 86400,),  # sender clocks drift; filter on receive time below
                ).fetchall()
            finally:
                con.close()
            index = peer_parse.name_index(uri)
        except sqlite3.Error as e:
            logger.warning("Couldn't read peer bot messages from %s (%s): %s", bot_db, mode, e)
            continue
        rows = [r for r in rows if (r[0] or 0) >= since_ts]
        return peer_parse.peer_successes(rows, peers, channel, index)
    return []


def format_post(label: str, key: str, outcome: str, detail: str = "") -> str:
    """Our #time-sync line, in the shared format both bots parse:
    `<name> [<first 3 bytes of the key, 6 hex>]: <OUTCOME>[ - detail]`, kept under
    CHAN_MSG_MAX_BYTES by trimming the detail, then the name - never the key."""
    key = (key or "")[:6]
    tail = f" [{key}]: {outcome}" if key else f": {outcome}"
    name = label
    while name and len((name + tail).encode()) > CHAN_MSG_MAX_BYTES:
        name = name[:-1]
    msg = (name.rstrip() or "?") + tail
    if detail:
        extra = f" {detail}" if detail.startswith("(") else f" - {detail}"
        room = CHAN_MSG_MAX_BYTES - len(msg.encode())
        if len(extra.encode()) > room:
            raw = extra.encode()[: max(0, room - 3)]
            extra = raw.decode("utf-8", "ignore") + "..." if room > 6 else ""
        msg += extra
    return msg


async def find_or_create_channel(mc: MeshCore, name: str, scan_limit: int) -> Optional[int]:
    """Return the slot index of channel `name` on the connected companion,
    creating it in the first free slot if it isn't there.

    Channels are local, per-companion-identity config (not mesh-wide), so
    this has to query the actual device rather than trust a cache - matches
    the exact-then-stripped-# convention meshcore-bot's channel_manager uses.

    The create path is a safety net for companion swaps: this job has already
    outlived a TCP companion (openHop, removed 2026-08-31), a USB Heltec
    HT-n5262, and now a USB RAK4631, and each swap starts with a bare channel
    table. set_channel() derives the key from the exact name string
    (sha256(name)[:16] for a '#'-prefixed hashtag channel), so anything else
    monitoring '#kod-timesync' re-syncs automatically as long as the
    configured summary_channel keeps its leading '#'.
    """
    target = name.lstrip("#").lower()
    first_free = None
    for idx in range(scan_limit):
        event = await mc.commands.get_channel(idx)
        if event is None or event.type == EventType.ERROR:
            break
        channel_name = (event.payload.get("channel_name") or "").strip()
        if channel_name.lstrip("#").lower() == target:
            return idx
        # Never reuse slot 0 (the well-known public channel) even if it reads
        # blank on some firmware.
        if first_free is None and idx > 0 and not channel_name:
            first_free = idx

    if first_free is None:
        logger.warning(
            "summary_channel '%s' not found and no free slot in the first %d - "
            "per-node updates won't be posted",
            name,
            scan_limit,
        )
        return None

    result = await mc.commands.set_channel(first_free, name)
    if result is None or result.type == EventType.ERROR:
        logger.warning(
            "summary_channel '%s' not found and set_channel(slot %d) failed (%s) - "
            "per-node updates won't be posted",
            name,
            first_free,
            result.payload if result is not None else "no response",
        )
        return None
    logger.info("summary_channel '%s' was missing - created it in slot %d", name, first_free)
    return first_free


async def _handle_already_current(
    mc: MeshCore,
    contact: dict,
    pubkey_prefix: str,
    label: str,
    text: str,
    state: dict,
    auto_clkreboot: bool,
    clkreboot_min_hours: float,
    admin_password: str,
    login_timeout: float,
    reply_timeout: float,
    clkreboot_retry_delay: float,
) -> SyncResult:
    """`clock sync` was rejected as backwards. Decide whether this is a
    genuinely stuck RTC (auto-issue `clkreboot`, the only fix - see module
    docstring context in clock_sync.yaml.example) or almost certainly just a
    retest too soon after a real success (mesh transit delay vs. an
    already-correct clock rejects the retry) - proven to happen in testing,
    so it's not a hypothetical edge case.
    """
    if not auto_clkreboot:
        return SyncResult(label, Outcome.ALREADY_CURRENT, text)

    entry = state.get(pubkey_prefix)
    hours_since_success = (
        (time.time() - entry["last_success_ts"]) / 3600
        if entry and "last_success_ts" in entry
        else None
    )
    if hours_since_success is not None and hours_since_success < clkreboot_min_hours:
        return SyncResult(
            label,
            Outcome.ALREADY_CURRENT,
            f"{text} - synced OK {hours_since_success:.1f}h ago, skipping auto-clkreboot",
        )

    reboot_send = await mc.commands.send_cmd(contact, "clkreboot")
    if reboot_send is None or reboot_send.type == EventType.ERROR:
        return SyncResult(label, Outcome.ALREADY_CURRENT, f"{text} - clkreboot send failed")

    last_ok = f"{hours_since_success:.1f}h ago" if hours_since_success is not None else "never"

    # clkreboot resets the RTC to the firmware baseline (15 May 2024) and
    # reboots the node - a fresh `clock sync` right after should apply
    # cleanly (now is well ahead of that baseline) instead of being rejected
    # as backwards again, so it's worth retrying once in this same run rather
    # than leaving the node on a ~2024 clock until the next scheduled run in
    # ~2 days. No real measured reboot time yet (this path's first-ever live
    # trigger against a genuinely stuck node was 2026-08-02), hence a fixed
    # settle delay rather than any smarter backoff/polling.
    await asyncio.sleep(clkreboot_retry_delay)
    retry_outcome, retry_detail = await _login_and_sync(
        mc, contact, pubkey_prefix, admin_password, login_timeout, reply_timeout
    )
    if retry_outcome == Outcome.SUCCESS:
        state[pubkey_prefix] = {"name": label, "last_success_ts": time.time()}
        return SyncResult(
            label,
            Outcome.SUCCESS,
            f"{text} - issued clkreboot (last OK: {last_ok}), resynced "
            f"{clkreboot_retry_delay:.0f}s after reboot ({retry_detail})",
        )

    return SyncResult(
        label,
        Outcome.AUTO_REBOOTED,
        f"{text} - issued clkreboot (last OK: {last_ok}), resync retry after "
        f"{clkreboot_retry_delay:.0f}s failed: {retry_outcome} - {retry_detail}",
    )


async def _login_and_sync(
    mc: MeshCore,
    contact: dict,
    pubkey_prefix: str,
    admin_password: str,
    login_timeout: float,
    reply_timeout: float,
    meas: Optional[dict] = None,
) -> tuple[str, str]:
    """Log in and send a single `clock sync`. Returns (outcome, detail).

    meas, if given, gets "offset": how far the node's clock was off before the
    sync. The login reply carries the node's own clock (server_timestamp,
    companion firmware >= 1.10, meshcore lib >= 2.3.17); it was stamped
    somewhere between our request going out and the reply coming back, so
    compare it with the midpoint - that cancels most of the mesh transit time.

    outcome is one of Outcome.SUCCESS / LOGIN_FAILED / LOGIN_TIMEOUT /
    SEND_FAILED / CMD_ERROR, or the sentinel "BACKWARDS" for a "clock cannot
    go backwards" rejection - callers decide what that means (sync_one's
    first attempt escalates to clkreboot; the post-reboot retry below does
    not recurse into another clkreboot on a second "BACKWARDS").

    Split out of sync_one() so the post-clkreboot retry can reuse the exact
    same login/send/interpret logic instead of drifting out of sync with it.
    """
    # send_login_sync() only ever waits for LOGIN_SUCCESS, so an explicit
    # LOGIN_FAILED (bad password) reads identically to plain silence. Send the
    # raw request ourselves and race both reply types so the two are visible
    # as different outcomes in the log. Call _send_login_raw() rather than the
    # public send_login(), which is otherwise identical but logs a
    # "consider using send_login_sync" WARNING on every single call - once per
    # node per run, it was the bulk of the log's noise.
    t_sent = time.time()
    send_result = await mc.commands._send_login_raw(contact, admin_password)
    if send_result is None or send_result.type == EventType.ERROR:
        detail = str(send_result.payload) if send_result else "no send confirmation"
        return Outcome.SEND_FAILED, f"login request not sent: {detail}"

    # Not filtered by pubkey_prefix: older firmware omits that field from the
    # LOGIN_SUCCESS/LOGIN_FAILED payload entirely, which would make the filter
    # never match. Safe to leave unfiltered because nodes are synced one at a
    # time - only one login can be outstanding on this connection at once.
    success_task = asyncio.ensure_future(mc.wait_for_event(EventType.LOGIN_SUCCESS))
    failed_task = asyncio.ensure_future(mc.wait_for_event(EventType.LOGIN_FAILED))
    done, pending = await asyncio.wait(
        {success_task, failed_task}, timeout=login_timeout, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()

    if success_task in done and success_task.result() is not None:
        server_ts = (success_task.result().payload or {}).get("server_timestamp")
        if meas is not None and isinstance(server_ts, int) and server_ts > 0:
            meas["offset"] = server_ts - (t_sent + time.time()) / 2
    elif failed_task in done and failed_task.result() is not None:
        return Outcome.LOGIN_FAILED, "explicit rejection (bad admin password)"
    else:
        return Outcome.LOGIN_TIMEOUT, "no login response within timeout"

    send_result = await mc.commands.send_cmd(contact, "clock sync")
    if send_result is None or send_result.type == EventType.ERROR:
        detail = str(send_result.payload) if send_result else "no send confirmation"
        return Outcome.SEND_FAILED, detail

    reply = await mc.wait_for_event(
        EventType.CONTACT_MSG_RECV,
        attribute_filters={"pubkey_prefix": pubkey_prefix},
        timeout=reply_timeout,
    )
    if reply is None:
        # Confirmed against a real node: firmware only sends a text reply for
        # an ERR condition - a successful `clock sync` is silent. Login just
        # proved a real round trip to this node, so silence here means the
        # command applied cleanly, not that the node is unreachable.
        return Outcome.SUCCESS, "no explicit ack (normal - firmware is silent on success)"

    text = (reply.payload.get("text") or "").strip()
    if "clock cannot go backwards" in text.lower():
        return "BACKWARDS", text
    if text.upper().startswith("ERR"):
        return Outcome.CMD_ERROR, text
    return Outcome.SUCCESS, text


async def sync_one(
    mc: MeshCore,
    node_cfg: dict,
    admin_password: str,
    login_timeout: float,
    reply_timeout: float,
    reset_path: bool = True,
    state: Optional[dict] = None,
    auto_clkreboot: bool = True,
    clkreboot_min_hours: float = DEFAULT_CLKREBOOT_MIN_HOURS,
    clkreboot_retry_delay: float = DEFAULT_CLKREBOOT_RETRY_DELAY,
    restore_missing: bool = True,
    bot_db: str = DEFAULT_BOT_DB,
) -> SyncResult:
    label = node_cfg.get("name") or node_cfg.get("pubkey_prefix") or "?"
    state = state if state is not None else {}

    contact = find_contact(mc, node_cfg)
    restored = False
    if contact is None:
        if not restore_missing:
            return SyncResult(label, Outcome.CONTACT_NOT_FOUND, "not in companion's contact list")
        contact, why = await restore_contact(mc, node_cfg, bot_db)
        if contact is None:
            return SyncResult(label, Outcome.CONTACT_NOT_FOUND, f"not in companion's contact list; {why}")
        restored = True

    result = await _sync_found(
        mc, contact, label, admin_password, login_timeout, reply_timeout, reset_path, state,
        auto_clkreboot, clkreboot_min_hours, clkreboot_retry_delay,
    )
    if restored:
        result.detail = f"{result.detail} (contact re-added)" if result.detail else "contact re-added"
    return result


async def _sync_found(
    mc: MeshCore,
    contact: dict,
    label: str,
    admin_password: str,
    login_timeout: float,
    reply_timeout: float,
    reset_path: bool,
    state: dict,
    auto_clkreboot: bool,
    clkreboot_min_hours: float,
    clkreboot_retry_delay: float,
) -> SyncResult:

    pubkey_prefix = contact["public_key"][:12]  # 6 bytes hex, matches reader.py attributes

    if reset_path:
        # This job only touches each node once a day, so a cached direct path
        # (learned from whatever last happened to route successfully) is more
        # likely than usual to have gone stale - a repeater moved, changed
        # frequency, or the mesh topology shifted. Flood once rather than
        # trust a possibly-dead cached hop.
        await mc.commands.reset_path(contact)

    meas: dict = {}
    outcome, detail = await _login_and_sync(
        mc, contact, pubkey_prefix, admin_password, login_timeout, reply_timeout, meas
    )
    result = await _finish(
        mc, contact, pubkey_prefix, label, outcome, detail, state, auto_clkreboot, clkreboot_min_hours,
        admin_password, login_timeout, reply_timeout, clkreboot_retry_delay,
    )
    offset = meas.get("offset")
    if offset is not None:
        result.offset = offset
        result.detail = f"{peer_parse.fmt_offset(offset)} ({'ahead' if offset > 0 else 'behind'}) - {result.detail}"
        if result.outcome == Outcome.SUCCESS and pubkey_prefix in state:
            state[pubkey_prefix]["last_offset_s"] = round(offset, 1)
    return result


async def _finish(
    mc, contact, pubkey_prefix, label, outcome, detail, state, auto_clkreboot, clkreboot_min_hours,
    admin_password, login_timeout, reply_timeout, clkreboot_retry_delay,
) -> SyncResult:
    if outcome == "BACKWARDS":
        return await _handle_already_current(
            mc,
            contact,
            pubkey_prefix,
            label,
            detail,
            state,
            auto_clkreboot,
            clkreboot_min_hours,
            admin_password,
            login_timeout,
            reply_timeout,
            clkreboot_retry_delay,
        )

    if outcome == Outcome.SUCCESS:
        state[pubkey_prefix] = {"name": label, "last_success_ts": time.time()}
    return SyncResult(label, outcome, detail)


async def run(cfg: dict) -> list[SyncResult]:
    companion = cfg.get("companion", {})
    transport = str(companion.get("transport", "tcp")).lower()
    host = companion.get("host", "127.0.0.1")
    port = companion.get("port", 5001)
    serial_port = companion.get("serial_port", "/dev/ttyACM0")
    baudrate = companion.get("baudrate", 115200)
    login_timeout = cfg.get("login_timeout_seconds", DEFAULT_LOGIN_TIMEOUT)
    reply_timeout = cfg.get("reply_timeout_seconds", DEFAULT_REPLY_TIMEOUT)
    delay = cfg.get("delay_between_nodes_seconds", DEFAULT_DELAY_BETWEEN_NODES)
    admin_password = cfg["admin_password"]
    reset_path = cfg.get("reset_path_before_sync", True)
    summary_channel = cfg.get("summary_channel")
    channel_scan_limit = cfg.get("channel_scan_limit", DEFAULT_CHANNEL_SCAN_LIMIT)
    auto_clkreboot = cfg.get("auto_clkreboot", True)
    clkreboot_min_hours = cfg.get("clkreboot_min_hours_since_success", DEFAULT_CLKREBOOT_MIN_HOURS)
    clkreboot_retry_delay = cfg.get("clkreboot_retry_delay_seconds", DEFAULT_CLKREBOOT_RETRY_DELAY)
    state_file = cfg.get("state_file", DEFAULT_STATE_FILE)
    # meshcore-bot purges repeaters from the companion when its contact list
    # fills; put a configured one back (from the bot's database) when it's missing.
    restore_missing = cfg.get("restore_missing_contacts", True)
    bot_db = cfg.get("bot_db", DEFAULT_BOT_DB)
    # Another time-sync bot on the same channel: skip a node it reported synced
    # within peer_skip_hours (not on manual --only runs: those mean "do it now").
    peers = cfg.get("peer_bots") or []
    if isinstance(peers, str):
        peers = [peers]
    peer_skip_hours = float(cfg.get("peer_skip_hours", DEFAULT_PEER_SKIP_HOURS))
    peer_successes: list = []
    if peers and summary_channel and not cfg.get("_manual") and peer_skip_hours > 0:
        peer_successes = load_peer_successes(bot_db, summary_channel, peers, time.time() - peer_skip_hours * 3600)
        logger.info("Peer bots %s: %d node success(es) on %s in the last %g h",
                    ", ".join(peers), len(peer_successes), summary_channel, peer_skip_hours)
    state = load_state(state_file)

    if transport == "serial":
        # openHop is gone; the companion radio is a USB device that
        # meshcore-bot normally owns. The cron wrapper stops the bot first so
        # this run has exclusive access to the serial port, then restarts it.
        logger.info("Connecting to companion via serial %s @ %d baud", serial_port, baudrate)
        mc = await MeshCore.create_serial(serial_port, baudrate, default_timeout=10)
        if mc is None:
            raise ConnectionError(f"Could not connect to companion on {serial_port}")
    else:
        logger.info("Connecting to companion at %s:%s", host, port)
        mc = await MeshCore.create_tcp(host, port, default_timeout=10)
        if mc is None:
            raise ConnectionError(f"Could not connect to companion at {host}:{port}")

    results: list[SyncResult] = []
    try:
        await mc.ensure_contacts()
        # Replies to admin CLI commands (e.g. the "clock sync" ack/error text)
        # arrive as queued messages, not pushed events - the device sends a
        # MESSAGES_WAITING notice and we have to pull them with get_msg().
        # Without this, wait_for_event(CONTACT_MSG_RECV, ...) below never
        # fires even when the remote node replied (verified: a real sync
        # succeeded - node's RTC updated on reboot - while this script still
        # logged NO_REPLY before this fix).
        await mc.start_auto_message_fetching()

        channel_idx = None
        if summary_channel:
            channel_idx = await find_or_create_channel(mc, summary_channel, channel_scan_limit)

        nodes = cfg["repeaters"]
        for i, node_cfg in enumerate(nodes):
            if peer_successes:
                contact = find_contact(mc, node_cfg)
                key = contact["public_key"] if contact else str(node_cfg.get("pubkey_prefix") or "")
                hit = peer_parse.latest_for(peer_successes, key)
                if hit:
                    ts = hit["ts"]
                    result = SyncResult(
                        node_cfg.get("name") or key or "?", Outcome.SKIPPED_PEER,
                        f"synced by {hit['sender']} at {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}, "
                        f"{(time.time() - ts) / 3600:.1f} h ago" + (f", {hit['note']}" if hit["note"] else ""),
                    )
                    results.append(result)
                    logger.info("%s: %s (%s)", result.node, result.outcome, result.detail)
                    continue  # nothing sent, so no delay before the next node
            node_password = node_cfg.get("admin_password", admin_password)
            result = await sync_one(
                mc,
                node_cfg,
                node_password,
                login_timeout,
                reply_timeout,
                reset_path,
                state,
                auto_clkreboot,
                clkreboot_min_hours,
                clkreboot_retry_delay,
                restore_missing,
                bot_db,
            )
            results.append(result)
            logger.info("%s: %s (%s)", result.node, result.outcome, result.detail)
            save_state(state_file, state)  # after every node, not just at the end - durable across crashes

            if channel_idx is not None:
                contact = find_contact(mc, node_cfg)
                key = (contact["public_key"] if contact else str(node_cfg.get("pubkey_prefix") or "")).lower()
                detail = result.detail if result.outcome in (Outcome.ALREADY_CURRENT, Outcome.AUTO_REBOOTED) else ""
                if result.offset is not None:  # `Name [key]: SUCCESS (5s off)`, as PMD-TimeSync does
                    detail = f"({peer_parse.fmt_offset(result.offset)})" + (f" {detail.split(' - ', 1)[-1]}" if detail else "")
                await mc.commands.send_chan_msg(channel_idx, format_post(result.node, key, result.outcome, detail))

            if i < len(nodes) - 1:
                await asyncio.sleep(delay)
    finally:
        await mc.disconnect()

    return results


async def ensure_channel(cfg: dict) -> int:
    name = cfg.get("summary_channel")
    if not name:
        logger.error("No summary_channel in the config")
        return 1
    companion = cfg.get("companion", {})
    if str(companion.get("transport", "tcp")).lower() == "serial":
        mc = await MeshCore.create_serial(companion.get("serial_port", "/dev/ttyACM0"),
                                          companion.get("baudrate", 115200), default_timeout=10)
    else:
        mc = await MeshCore.create_tcp(companion.get("host", "127.0.0.1"), companion.get("port", 5001), default_timeout=10)
    if mc is None:
        logger.error("Could not connect to the companion")
        return 1
    try:
        idx = await find_or_create_channel(mc, name, cfg.get("channel_scan_limit", DEFAULT_CHANNEL_SCAN_LIMIT))
    finally:
        await mc.disconnect()
    if idx is None:
        return 1
    logger.info("summary_channel '%s' is in slot %d", name, idx)
    return 0


def configure_logging(log_file: Optional[str]) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,  # `meshcore` calls basicConfig() on import, which would
        # otherwise make this call a no-op and silently drop the FileHandler
    )


def select_nodes(nodes: list[dict], wanted: list[str]) -> list[dict]:
    """The configured nodes named by --only, in config order. Each value must
    match at least one node, by name (case-insensitive) or as a prefix of its
    pubkey_prefix, so a typo stops the run instead of silently syncing nothing."""
    picked: list[dict] = []
    for w in wanted:
        w_name, w_key = w.strip().lower(), w.strip().lower().replace(" ", "")
        hits = [
            n for n in nodes
            if str(n.get("name") or "").strip().lower() == w_name
            or (w_key and str(n.get("pubkey_prefix") or "").lower().startswith(w_key))
            or (len(w_key) >= 6 and str(n.get("pubkey_prefix") or "") and w_key.startswith(str(n.get("pubkey_prefix")).lower()))
        ]
        if not hits:
            names = ", ".join(str(n.get("name")) for n in nodes)
            raise SystemExit(f"--only {w!r} matches no configured node (have: {names})")
        picked.extend(h for h in hits if not any(h is p for p in picked))
    return [n for n in nodes if any(n is p for p in picked)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="/etc/clock_sync/clock_sync.yaml", help="Path to config YAML"
    )
    parser.add_argument("--log-file", default=None, help="Optional log file path")
    parser.add_argument(
        "--only", action="append", default=[], metavar="NODE",
        help="Sync only this node: its name (case-insensitive) or the start of its "
             "pubkey_prefix. Repeat for several. Default: every configured node.",
    )
    parser.add_argument(
        "--ensure-channel", action="store_true",
        help="Only make sure summary_channel is on the companion (adds it to a free slot), then exit.",
    )
    args = parser.parse_args()

    configure_logging(args.log_file)

    cfg = load_config(args.config)
    sched, from_file = schedule.load()
    if from_file:  # set on the web page; beats the config file's value
        cfg["peer_skip_hours"] = sched["peer_skip_hours"]
    if args.ensure_channel:
        sys.exit(asyncio.run(ensure_channel(cfg)))
    if args.only:
        cfg["_manual"] = True
        cfg["repeaters"] = select_nodes(cfg["repeaters"], args.only)
        logger.info("Manual run of %d node(s): %s", len(cfg["repeaters"]),
                    ", ".join(n.get("name") or n.get("pubkey_prefix") or "?" for n in cfg["repeaters"]))
    started = time.time()
    results = asyncio.run(run(cfg))
    elapsed = time.time() - started

    skipped = [r for r in results if r.outcome == Outcome.SKIPPED_PEER]
    ok = sum(1 for r in results if r.outcome in (Outcome.SUCCESS, Outcome.SKIPPED_PEER))
    rebooted = [r for r in results if r.outcome == Outcome.AUTO_REBOOTED]
    backwards = [r for r in results if r.outcome == Outcome.ALREADY_CURRENT]
    failed = [r for r in results if r.outcome not in (Outcome.SUCCESS, Outcome.SKIPPED_PEER)]

    logger.info("Done in %.1fs: %d/%d succeeded", elapsed, ok, len(results))
    if skipped:
        logger.info("%d node(s) skipped - a peer bot synced them recently: %s",
                    len(skipped), ", ".join(r.node for r in skipped))
    if rebooted:
        logger.warning(
            "%d node(s) had a stuck forward clock - issued 'clkreboot' automatically: %s",
            len(rebooted),
            ", ".join(r.node for r in rebooted),
        )
    if backwards:
        logger.error(
            "%d node(s) rejected clock sync as backwards but auto-clkreboot was suppressed "
            "(recent success on record - likely a retest, not real drift): %s",
            len(backwards),
            ", ".join(r.node for r in backwards),
        )
    if failed:
        for r in failed:
            logger.warning("FAILED %s: %s - %s", r.node, r.outcome, r.detail)
        sys.exit(1)


if __name__ == "__main__":
    main()
