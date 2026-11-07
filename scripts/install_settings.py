#!/usr/bin/env python3
"""Write install.sh's answers into config.ini, keeping the file's comments.

The answers come in MB_ environment variables so the password never appears in
a process list. Run with the bot's virtualenv: the password is stored hashed.
"""

from __future__ import annotations

import os
import sys

import bcrypt

from modules.ini_writer import update_ini_values


def connection_settings(env: dict[str, str]) -> dict[str, str]:
    kind = env["MB_CONNECTION"]
    if kind == "tcp":
        return {"connection_type": kind, "hostname": env["MB_HOSTNAME"], "tcp_port": env["MB_TCP_PORT"]}
    if kind == "ble":
        return {"connection_type": kind, "ble_device_name": env["MB_BLE_NAME"]}
    return {"connection_type": "serial", "serial_port": env["MB_SERIAL_PORT"]}


def settings(env: dict[str, str]) -> dict[str, dict[str, str]]:
    password = bcrypt.hashpw(env["MB_PASSWORD"].encode(), bcrypt.gensalt(rounds=12)).decode()
    return {
        "Connection": connection_settings(env),
        "Bot": {
            "bot_name": env["MB_BOT_NAME"],
            "bot_latitude": env["MB_LATITUDE"],
            "bot_longitude": env["MB_LONGITUDE"],
        },
        "Channels": {"monitor_channels": env["MB_CHANNEL"]},
        "Web_Viewer": {
            "enabled": "true",
            "auto_start": "true",
            "host": "0.0.0.0",
            "web_viewer_password": password,
        },
    }


if __name__ == "__main__":
    update_ini_values(sys.argv[1], settings(dict(os.environ)))
