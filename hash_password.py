#!/usr/bin/env python3
"""Generate a bcrypt hash for use as web_viewer_password in config.ini.

Usage:
    sudo /opt/meshcore-bot/venv/bin/python3 /opt/meshcore-bot/hash_password.py
"""
import getpass, bcrypt

pw = getpass.getpass("New web viewer password: ")
confirm = getpass.getpass("Confirm password: ")
if pw != confirm:
    print("Passwords do not match.")
    raise SystemExit(1)
hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()
print("\nPaste this into config.ini under [Web_Viewer]:")
print(f"web_viewer_password = {hashed}")
