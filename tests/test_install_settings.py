"""install.sh's answers land in config.ini with comments kept and the password hashed."""

import configparser
import importlib.util
from pathlib import Path

import bcrypt

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_settings", ROOT / "scripts/install_settings.py")
install_settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_settings)

ANSWERS = {
    "MB_CONNECTION": "serial", "MB_SERIAL_PORT": "/dev/serial/by-id/usb-radio", "MB_HOSTNAME": "",
    "MB_TCP_PORT": "", "MB_BLE_NAME": "", "MB_BOT_NAME": "TestBot", "MB_LATITUDE": "48.85",
    "MB_LONGITUDE": "-123.5", "MB_CHANNEL": "#mybot", "MB_PASSWORD": "testpass123",
}


def apply(tmp_path, answers):
    path = tmp_path / "config.ini"
    path.write_text((ROOT / "config.ini.example").read_text())
    install_settings.update_ini_values(str(path), install_settings.settings(answers))
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    return path.read_text(), parser


def test_answers_are_written_and_comments_kept(tmp_path):
    text, cfg = apply(tmp_path, ANSWERS)
    assert cfg["Connection"]["serial_port"] == "/dev/serial/by-id/usb-radio"
    assert cfg["Bot"]["bot_name"] == "TestBot"
    assert cfg["Bot"]["bot_latitude"] == "48.85"
    assert cfg["Channels"]["monitor_channels"] == "#mybot"
    assert cfg["Web_Viewer"]["enabled"] == "true" and cfg["Web_Viewer"]["auto_start"] == "true"
    assert "# Enable or disable the web data viewer" in text


def test_password_is_stored_hashed(tmp_path):
    text, cfg = apply(tmp_path, ANSWERS)
    stored = cfg["Web_Viewer"]["web_viewer_password"]
    assert "testpass123" not in text
    assert bcrypt.checkpw(b"testpass123", stored.encode())


def test_network_radio(tmp_path):
    _, cfg = apply(tmp_path, {**ANSWERS, "MB_CONNECTION": "tcp", "MB_HOSTNAME": "10.0.0.5", "MB_TCP_PORT": "5000"})
    assert cfg["Connection"]["connection_type"] == "tcp"
    assert cfg["Connection"]["hostname"] == "10.0.0.5"
