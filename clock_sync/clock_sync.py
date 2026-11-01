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
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from meshcore import EventType, MeshCore

logger = logging.getLogger("clock_sync")

DEFAULT_LOGIN_TIMEOUT = 20
DEFAULT_REPLY_TIMEOUT = 20
DEFAULT_DELAY_BETWEEN_NODES = 30
DEFAULT_CHANNEL_SCAN_LIMIT = 20
DEFAULT_STATE_FILE = "/opt/clock_sync/state.json"
DEFAULT_CLKREBOOT_MIN_HOURS = 12  # must be well under the ~48h run cadence
DEFAULT_CLKREBOOT_RETRY_DELAY = 30  # seconds to let a node reboot before retrying `clock sync`
CHAN_MSG_MAX_LEN = 140  # keep comfortably inside a single-packet channel message


class Outcome:
    SUCCESS = "SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    ALREADY_CURRENT = "ALREADY_CURRENT"  # flagged, auto-clkreboot suppressed (see detail)
    AUTO_REBOOTED = "AUTO_REBOOTED"  # clkreboot issued automatically
    CMD_ERROR = "CMD_ERROR"
    SEND_FAILED = "SEND_FAILED"
    CONTACT_NOT_FOUND = "CONTACT_NOT_FOUND"


@dataclass
class SyncResult:
    node: str
    outcome: str
    detail: str = ""


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
) -> tuple[str, str]:
    """Log in and send a single `clock sync`. Returns (outcome, detail).

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
        pass  # logged in
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
) -> SyncResult:
    label = node_cfg.get("name") or node_cfg.get("pubkey_prefix") or "?"
    state = state if state is not None else {}

    contact = find_contact(mc, node_cfg)
    if contact is None:
        return SyncResult(label, Outcome.CONTACT_NOT_FOUND, "not in companion's contact list")

    pubkey_prefix = contact["public_key"][:12]  # 6 bytes hex, matches reader.py attributes

    if reset_path:
        # This job only touches each node once a day, so a cached direct path
        # (learned from whatever last happened to route successfully) is more
        # likely than usual to have gone stale - a repeater moved, changed
        # frequency, or the mesh topology shifted. Flood once rather than
        # trust a possibly-dead cached hop.
        await mc.commands.reset_path(contact)

    outcome, detail = await _login_and_sync(
        mc, contact, pubkey_prefix, admin_password, login_timeout, reply_timeout
    )

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
            )
            results.append(result)
            logger.info("%s: %s (%s)", result.node, result.outcome, result.detail)
            save_state(state_file, state)  # after every node, not just at the end - durable across crashes

            if channel_idx is not None:
                msg = f"{result.node}: {result.outcome}"
                if result.outcome in (Outcome.ALREADY_CURRENT, Outcome.AUTO_REBOOTED):
                    msg += f" - {result.detail}"
                if len(msg) > CHAN_MSG_MAX_LEN:
                    msg = msg[: CHAN_MSG_MAX_LEN - 3] + "..."
                await mc.commands.send_chan_msg(channel_idx, msg)

            if i < len(nodes) - 1:
                await asyncio.sleep(delay)
    finally:
        await mc.disconnect()

    return results


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="/etc/clock_sync/clock_sync.yaml", help="Path to config YAML"
    )
    parser.add_argument("--log-file", default=None, help="Optional log file path")
    args = parser.parse_args()

    configure_logging(args.log_file)

    cfg = load_config(args.config)
    started = time.time()
    results = asyncio.run(run(cfg))
    elapsed = time.time() - started

    ok = sum(1 for r in results if r.outcome == Outcome.SUCCESS)
    rebooted = [r for r in results if r.outcome == Outcome.AUTO_REBOOTED]
    backwards = [r for r in results if r.outcome == Outcome.ALREADY_CURRENT]
    failed = [r for r in results if r.outcome not in (Outcome.SUCCESS,)]

    logger.info("Done in %.1fs: %d/%d succeeded", elapsed, ok, len(results))
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
