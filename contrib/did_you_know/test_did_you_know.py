"""Run from the repo root: python -m pytest --no-cov contrib/did_you_know/test_did_you_know.py"""
import asyncio
import configparser
import datetime
import importlib.util
import re
import types
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("dyk", HERE / "did_you_know_service.py")
dyk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dyk)
dyk.TIPS_FILE = HERE / "did_you_know_tips.txt"

spec = importlib.util.spec_from_file_location("dyk_cmd", HERE / "dyk_command.py")
cmd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmd)
cmd.TIPS_FILE = HERE / "did_you_know_tips.txt"


class Log:
    def __init__(self): self.warnings = []
    def info(self, *a): pass
    def error(self, *a): pass
    def warning(self, m): self.warnings.append(m)


class Cmd:
    def __init__(self, enabled=True, admin=False, dm=False, channels=None):
        self.enabled, self.admin, self.requires_dm, self.channels = enabled, admin, dm, channels
    def _derive_config_section_name(self): return "X"
    def get_config_value(self, *a, **k): return self.enabled
    def requires_admin_access(self): return self.admin
    def is_channel_allowed(self, m): return self.channels is None or m.channel in self.channels


def make(commands, sent=None, ok=True):
    cfg = configparser.ConfigParser()
    cfg.read_string("[DidYouKnow]\nenabled=true\nchannel=#kod-bot\ntime=12:00\n")
    async def send(ch, text, **kw):
        sent.append((ch, text, kw))
        return ok
    manager = types.SimpleNamespace(commands=commands, send_channel_message=send,
                                    _is_channel_trigger_allowed=lambda n, m: True)
    return dyk.DidYouKnowService(types.SimpleNamespace(config=cfg, logger=Log(), command_manager=manager))


def test_every_tip_fits_and_names_a_real_command():
    names = {"dyk"}
    for f in (HERE.parent.parent / "modules" / "commands").glob("*_command.py"):
        names |= set(re.findall(r'^    name = "(\w+)"', f.read_text(), re.M))
    tips = dyk.parse_tips(dyk.TIPS_FILE.read_text(encoding="utf-8"))
    assert len(tips) > 30
    for name, tip in tips:
        assert name in names, name
        assert len((dyk.PREFIX + tip).encode()) <= dyk.MAX_BYTES, tip


def test_parse_skips_comments_and_keeps_colons_in_text():
    assert dyk.parse_tips("# no\n\nwx: a: b\nbroken line\n") == [("wx", "a: b")]


def test_one_tip_a_day_covers_them_all_and_ignores_restarts():
    tips = ["a", "b", "c"]
    start = datetime.date(2026, 11, 7)
    days = [dyk.pick_tip(tips, start + datetime.timedelta(days=i)) for i in range(3)]
    assert sorted(days) == tips
    assert dyk.pick_tip(tips, start) == dyk.pick_tip(list(tips), start)
    assert dyk.pick_tip([], start) is None


def test_next_send():
    at = lambda h, m=0: datetime.datetime(2026, 11, 7, h, m)
    assert dyk.next_send(at(9), 12, 0) == at(12)
    assert dyk.next_send(at(12), 12, 0) == at(12) + datetime.timedelta(days=1)
    assert dyk.next_send(at(15), 12, 0) == at(12) + datetime.timedelta(days=1)


def test_tips_only_for_commands_people_can_use():
    s = make({
        "ping": Cmd(), "wx": Cmd(enabled=False), "tide": Cmd(admin=True),
        "fire": Cmd(channels=["#other"]), "advert": Cmd(dm=True, channels=["#other"]),
    })
    text = " ".join(s.available_tips())
    assert "ping - replies" in text and "advert - DM" in text
    for gone in ("wx ", "tide ", "fire "):
        assert gone not in text
    assert "aqi" not in text  # command not loaded


def test_send_goes_to_the_channel_with_the_prefix():
    sent = []
    s = make({"ping": Cmd()}, sent)
    assert asyncio.run(s.send_tip(datetime.date(2026, 11, 7)))
    ch, text, kw = sent[0]
    assert ch == "#kod-bot" and text.startswith("Did you know? ping") and kw["skip_user_rate_limit"]


def test_no_usable_tips_sends_nothing():
    sent = []
    s = make({}, sent)
    assert not asyncio.run(s.send_tip(datetime.date(2026, 11, 7)))
    assert not sent


def test_start_twice_runs_one_loop():
    async def go():
        s = make({})
        await s.start()
        first = s._task
        await s.start()
        assert s._task is first
        await s.stop()
        assert s._task is None
    asyncio.run(go())


def make_command(commands, limit=130):
    c = cmd.DykCommand.__new__(cmd.DykCommand)
    c.name = "dyk"
    c._last = None
    c.bot = types.SimpleNamespace(command_manager=types.SimpleNamespace(
        commands=commands, _is_channel_trigger_allowed=lambda n, m: True))
    c.get_max_message_length = lambda m: limit
    return c


MSG = types.SimpleNamespace(channel="#kod-bot", is_dm=False)


def test_dyk_only_offers_usable_tips():
    c = make_command({"ping": Cmd(), "wx": Cmd(enabled=False), "dyk": Cmd()})
    for _ in range(5):
        assert "ping - replies" in c.pick_tip(MSG)


def test_dyk_never_repeats_back_to_back():
    c = make_command({"ping": Cmd(), "moon": Cmd(), "sun": Cmd()})
    picks = [c.pick_tip(MSG) for _ in range(50)]
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert len(set(picks)) == 3


def test_dyk_skips_tips_too_long_for_the_senders_name():
    c = make_command({"ping": Cmd(), "trace": Cmd()}, limit=100)
    for _ in range(10):
        assert len(c.pick_tip(MSG).encode()) <= 100


def test_dyk_with_nothing_usable():
    assert make_command({}).pick_tip(MSG) is None
