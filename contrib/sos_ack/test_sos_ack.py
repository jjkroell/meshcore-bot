# Standalone test: python3 contrib/sos_ack/test_sos_ack.py (stubs the bot, needs no radio).
import sys, types, asyncio, configparser
# stub the bot's modules so the plugin imports standalone
mc = types.ModuleType("meshcore"); mc.EventType = types.SimpleNamespace(CHANNEL_MSG_RECV="chan"); sys.modules["meshcore"] = mc
for n in ["modules", "modules.service_plugins"]: sys.modules[n] = types.ModuleType(n)
bs = types.ModuleType("modules.service_plugins.base_service")
class Base:
    def __init__(self, bot): self.bot = bot; self.logger = bot.logger; self._running = False
    def get_mesh_flood_scope(self): return None
bs.BaseServicePlugin = Base; sys.modules["modules.service_plugins.base_service"] = bs
import os; sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))
import sos_ack_service as m

cases = {"SOS at Mt Seymour": True, "emergency!": True, "Need help near Squamish": True, "he needs help": True,
 "help me": True, "call 911": True, "Mayday": True, "sos": True, "threat on the trail": True,
 "life-threatening injury": True, "Life or death": True, "threats": False, "lifeline": False, "wildlife": False,
 "announce #sos on Public": False, "thanks for the help": False, "sosa": False, "test": False,
 "@[Dave] what's your emergency?": False, " @[Dave] got your SOS, on my way": False, "SOS at the lake, @[Mike] is hurt": True}
for t, want in cases.items():
    assert m.is_alert(t) == want, t

class Log:
    def info(self, *a): print("INFO", *a)
    warning = error = debug = info
sent = []
class CM:
    async def send_channel_message(self, ch, text, **kw): sent.append((ch, text)); return True
cfg = configparser.ConfigParser(); cfg.read_dict({"Bot": {"bot_name": "KOD-BOT"}, "SosAck": {"enabled": "true", "channel": "#sos, #sos-test"}})
bot = types.SimpleNamespace(config=cfg, logger=Log(), command_manager=CM(), meshcore=types.SimpleNamespace(subscribe=lambda *a: None),
    channel_manager=types.SimpleNamespace(get_channel_name=lambda i: {3: "#sos", 1: "#test", 5: "#sos-test"}[i]))
s = m.SosAckService(bot)
async def run():
    await s.start(); assert s._running
    ev = lambda idx, text, pl=2, ts=None: types.SimpleNamespace(payload={"channel_idx": idx, "text": text, "path_len": pl, "sender_timestamp": ts})
    await s._on_channel_message(ev(3, "Dave: help me, stuck at the summit"))
    await s._on_channel_message(ev(3, "Dave: SOS again"))            # cooldown
    await s._on_channel_message(ev(1, "Ann: sos"))                   # other channel
    await s._on_channel_message(ev(3, "Ann: test"))                  # not a trigger
    await s._on_channel_message(ev(3, "Bob: mayday", 0x42, 1_750_000_000))  # old -> skipped
    await s._on_channel_message(ev(3, "SOS Sam: emergency", 0x41))   # name would trigger
    await s._on_channel_message(ev(3, "Kim: 911", 0))
    await s._on_channel_message(ev(3, "Rescuer: @[Kim] what's your emergency?", 2))  # a reply: no ack
    await s._on_channel_message(ev(5, "Tess: sos test", 1))
    await s._on_channel_message(ev(3, "A Very Long Node Name 🗻🗻🗻🗻: need help", 3))
    await s._on_channel_message(ev(5, "Tess: SOS for real now", 1))  # real call after a test: own cooldown
    await s._on_channel_message(ev(3, "Mo: **TEST TEST TEST**\n\nMonthly SOS test message\n\n**TEST TEST TEST**"))
asyncio.run(run())
for x in sent: print(x)
for ch, t in sent:
    assert len(("KOD-BOT: " + t).encode()) <= 150, t
    # every reply is safe for Ridgeline: no alert word, or opens with a mention
    assert not m.is_alert(t), t
texts = [t for _, t in sent]
assert texts == [
    "@[Dave]\nSOS received, email sent to Ridgeline Alert users.\n\nSend your location and nature of the emergency ASAP.",
    "@[SOS Sam]\nSOS received, email sent to Ridgeline Alert users.\n\nSend your location and nature of the emergency ASAP.",
    "@[Kim]\nSOS received, email sent to Ridgeline Alert users.\n\nSend your location and nature of the emergency ASAP.",
    "@[Tess]\nTest received",
    "@[A Very Long Node Name 🗻🗻]\nSOS received, email sent to Ridgeline Alert users.\n\nSend your location and nature of the emergency ASAP.",
    "@[Tess]\nSOS received, email sent to Ridgeline Alert users.\n\nSend your location and nature of the emergency ASAP.",
    "@[Mo]\nTest received",
], texts

# the monthly test message, \n decoded by the scheduler
assert m.is_alert("**TEST TEST TEST**\n\nMonthly SOS test message\n\n**TEST TEST TEST**")
assert m.is_test("**TEST TEST TEST**\n\nMonthly SOS test message\n\n**TEST TEST TEST**")
assert not m.is_test("SOS contest results") and m.is_test("SOS testing")

# a very long name is trimmed, never dropped (the mention keeps it a reply)
t = s._render(s.reply, "N" * 60, None)
assert t.startswith("@[N") and len(("KOD-BOT: " + t).encode()) <= 150 and not m.is_alert(t), t
assert s._render(s.reply, "", None).startswith("@[?]\n")

# escapes from config
assert m.decode_escapes(r"a\n\nb") == "a\n\nb" and m.decode_escapes(r"a\\nb") == r"a\nb"

# a reply with an emergency word but no leading mention refuses to start
for key, bad in (("reply", "SOS heard, email sent"), ("test_reply", "Test of SOS received")):
    c2 = configparser.ConfigParser(); c2.read_dict({"Bot": {"bot_name": "KOD-BOT"}, "SosAck": {"enabled": "true", key: bad}})
    s2 = m.SosAckService(types.SimpleNamespace(**{**vars(bot), "config": c2}))
    asyncio.run(s2.start()); assert not s2._running, key
# ...and a plain one without the mention still starts
c3 = configparser.ConfigParser(); c3.read_dict({"Bot": {"bot_name": "KOD-BOT"}, "SosAck": {"enabled": "true", "reply": "Heard you, email sent"}})
s3 = m.SosAckService(types.SimpleNamespace(**{**vars(bot), "config": c3})); asyncio.run(s3.start()); assert s3._running
print("ok")
