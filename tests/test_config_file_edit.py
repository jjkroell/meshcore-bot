"""Tests for modules.config_file_edit: text edits that keep comments."""

import configparser

from modules.config_file_edit import remove_option, set_options

SAMPLE = """# Top comment
[Connection]
# how to reach the radio
connection_type = serial  ; inline note
radio_debug = false

[Scheduled_Messages]
# HHMM = channel:message[::count]
1300 = #sos:SOS TEST example::1
0 9 1 * * = #sos:SOS test message
# trailing comment

[Banned_Users]
# names
banned_users = Spammer
"""


def _write(tmp_path, text=SAMPLE):
    p = tmp_path / "config.ini"
    p.write_text(text, encoding="utf-8")
    return p


def _parse(p):
    cp = configparser.ConfigParser()
    cp.read(p, encoding="utf-8")
    return cp


def test_set_existing_keeps_comments_and_position(tmp_path):
    p = _write(tmp_path)
    set_options(str(p), "Connection", {"radio_debug": "true"})
    text = p.read_text()
    assert text == SAMPLE.replace("radio_debug = false", "radio_debug = true")


def test_set_matches_key_case_insensitively(tmp_path):
    p = _write(tmp_path)
    set_options(str(p), "Connection", {"RADIO_DEBUG": "true"})
    assert "radio_debug = true" in p.read_text()
    assert "RADIO_DEBUG" not in p.read_text()


def test_set_new_key_goes_after_last_option(tmp_path):
    p = _write(tmp_path)
    set_options(str(p), "Connection", {"radio_probe_interval_seconds": "60"})
    lines = p.read_text().splitlines()
    i = lines.index("radio_debug = false")
    assert lines[i + 1] == "radio_probe_interval_seconds = 60"
    assert "# Top comment" in lines and "# how to reach the radio" in lines


def test_set_new_section_appended(tmp_path):
    p = _write(tmp_path)
    set_options(str(p), "New_Section", {"a": "1"})
    assert p.read_text().startswith(SAMPLE)
    assert _parse(p).get("New_Section", "a") == "1"


def test_count_decrement_on_cron_key(tmp_path):
    p = _write(tmp_path, SAMPLE.replace("SOS test message\n", "SOS test message::3\n"))
    set_options(str(p), "Scheduled_Messages", {"0 9 1 * *": "#sos:SOS test message::2"})
    assert "0 9 1 * * = #sos:SOS test message::2" in p.read_text()
    assert "# HHMM = channel:message[::count]" in p.read_text()


def test_remove_option_keeps_everything_else(tmp_path):
    p = _write(tmp_path)
    assert remove_option(str(p), "Scheduled_Messages", "1300") is True
    assert p.read_text() == SAMPLE.replace("1300 = #sos:SOS TEST example::1\n", "")
    assert not _parse(p).has_option("Scheduled_Messages", "1300")


def test_remove_only_in_named_section(tmp_path):
    p = _write(tmp_path)
    assert remove_option(str(p), "Connection", "banned_users") is False
    assert remove_option(str(p), "Missing", "x") is False
    assert p.read_text() == SAMPLE


def test_remove_takes_continuation_lines(tmp_path):
    p = _write(tmp_path, "[S]\nkey = one\n  two\nother = 3\n")
    remove_option(str(p), "S", "key")
    assert p.read_text() == "[S]\nother = 3\n"


def test_keeps_file_mode(tmp_path):
    p = _write(tmp_path)
    p.chmod(0o640)
    set_options(str(p), "Connection", {"radio_debug": "true"})
    assert p.stat().st_mode & 0o777 == 0o640
