"""Run from the repo root: python -m pytest --no-cov contrib/public_alerts/test_public_alerts.py

fixtures/ holds real alerts from the NAAD feed. real_urgent() rewrites the real
test message into the shape of a live alert, since none was active to capture.
"""
import asyncio
import configparser
import importlib.util
import json

import pytest
from dataclasses import replace
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("pa", HERE / "public_alerts_service.py")
pa = importlib.util.module_from_spec(spec)
sys.modules["pa"] = pa
spec.loader.exec_module(pa)

TEST_CAP = (HERE / "fixtures/test_message.xml").read_bytes()
WIND_CAP = (HERE / "fixtures/wind_chilcotin.xml").read_bytes()


def real_urgent(area="Nanaimo", code="5921007", status="Actual", broadcast="Yes"):
    x = TEST_CAP.decode()
    x = x.replace("<status>Test</status>", f"<status>{status}</status>")
    x = x.replace("testMessage", "tsunami").replace("<event>Test Message</event>", "<event>tsunami</event>")
    x = x.replace("This is a Test Message and is NOT Intended for distribution to the Public", "Tsunami warning")
    x = x.replace("<value>No</value>", f"<value>{broadcast}</value>", 1)
    x = x.replace("Colville Lake", area).replace("6101012", code)
    return x.encode()


class Log:
    def __init__(self): self.lines = []
    def info(self, m): pass
    def warning(self, m): self.lines.append(m)
    def error(self, m): self.lines.append(m)


class DB:
    def __init__(self): self.data = {}
    def get_metadata(self, k): return self.data.get(k)
    def set_metadata(self, k, v): self.data[k] = v


def make(extra="", db=None, ok=True):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(
        "[PublicAlerts]\nenabled=true\ngroup_minutes=0\n"
        f"route1_channel=#swbc-alerts\nroute1_area_codes={pa.DEFAULT_AREA_CODES}\n"
        f"route1_area_names={pa.DEFAULT_AREA_NAMES}\n"
        "source1_name=a\nsource1_type=naad\nsource1_url=http://a/\n" + extra
    )
    sent = []
    async def one(ch, text, **kw):
        sent.append((ch, [text]))
        return ok
    async def many(ch, chunks, **kw):
        sent.append((ch, chunks))
        return ok
    manager = types.SimpleNamespace(send_channel_message=one, send_channel_messages_chunked=many)
    bot = types.SimpleNamespace(config=cfg, logger=Log(), command_manager=manager, db_manager=db or DB())
    return pa.PublicAlertsService(bot), sent


def serve(service, feed, files):
    """Stand in for the network: feed is a list of (id, link), files maps link to bytes."""
    service._read_source = lambda source: [(k, l, None) for k, l in feed]
    service._fetch = lambda url: files[url]


def fresh(alert):
    alert.sent = time.time()
    alert.expires = time.time() + 3600
    return alert


def test_real_test_message_is_recognised():
    a = pa.parse_cap(TEST_CAP)
    assert a.status == "Test" and a.is_test


def test_real_wind_warning_parses():
    a = pa.parse_cap(WIND_CAP)
    assert a.status == "Actual" and not a.is_test and a.level == 1 and a.from_eccc
    assert a.title == "Yellow wind warning" and a.hazard == "yellow wind warning"
    assert a.areas == ["Chilcotin", "100 Mile"]


def test_english_block_is_used():
    assert "vent" not in pa.parse_cap(WIND_CAP).hazard


def passes(s, alert, now=None):
    """True if any section would post this alert."""
    now = now or time.time()
    return s.real_and_current(alert, now) and any(s.for_route(alert, r) for r in s.routes())


def test_tests_never_pass_whatever_the_settings():
    s, _ = make("route1_min_level=statement\nroute2_channel=#t\nroute2_area_names=Colville\n"
                "route2_min_level=statement\n")
    assert not passes(s, fresh(pa.parse_cap(TEST_CAP)))
    # A test flagged only by its status, or only by its wording, is still a test.
    assert not passes(s, fresh(pa.parse_cap(real_urgent(status="Test"))))
    worded = pa.parse_cap(TEST_CAP.replace(b"<status>Test</status>", b"<status>Actual</status>"))
    assert not passes(s, fresh(worded))


def test_area_codes_for_alert_ready():
    s, _ = make()
    assert passes(s, fresh(pa.parse_cap(real_urgent())))
    assert not passes(s, fresh(pa.parse_cap(real_urgent("Kamloops", "5933042"))))


def test_weather_ignores_codes_so_neighbouring_regions_stay_out():
    a = pa.Alert(key="x", title="t", hazard="h", from_eccc=True, areas=["Central Coast - coastal sections"],
                 area_codes={"Central Coast - coastal sections": ["5943012"]})
    assert pa.matching_areas(a, ["5943"], ["Metro Vancouver"]) == []


def test_only_local_regions_are_named():
    a = pa.Alert(key="x", title="t", hazard="h", from_eccc=True, areas=["Chilcotin", "Greater Victoria"])
    assert pa.matching_areas(a, [], ["Greater Victoria"]) == ["Greater Victoria"]


def test_urgent_bc_alert_with_no_location_codes_is_kept():
    a = pa.Alert(key="x", title="t", hazard="h", level=0, areas=["British Columbia"], area_codes={"British Columbia": []})
    assert pa.matching_areas(a, ["5915"], ["Metro Vancouver"]) == ["British Columbia"]


def test_first_read_posts_nothing_then_new_alerts_post():
    s, sent = make()
    feed = [("old", "http://x/old")]
    files = {"http://x/old": real_urgent(), "http://x/new": real_urgent()}
    serve(s, feed, files)
    asyncio.run(s.poll())
    assert not sent
    feed.append(("new", "http://x/new"))
    original = pa.parse_cap
    pa.parse_cap = lambda d, k="": fresh(original(d, k))
    try:
        asyncio.run(s.poll())
    finally:
        pa.parse_cap = original
    assert len(sent) == 1 and sent[0][0] == "#swbc-alerts"
    assert sent[0][1][0].startswith("ALERT: Tsunami warning - Nanaimo")


CH = "#swbc-alerts"


def eccc(area, name="wind warning", what="Gusts to 90 km/h.", stamp=None):
    return pa.Alert(key=f"{name}{area}{stamp}", title=name.capitalize(), hazard=name, areas=[area],
                    from_eccc=True, what=what, sent=time.time(), expires=time.time() + 3600)


def test_regions_merge_and_updates_stay_quiet():
    s, sent = make()
    now = time.time()
    s._queue(CH, eccc("Greater Victoria"), now)
    s._queue(CH, eccc("Metro Vancouver - southwest including Richmond"), now)
    asyncio.run(s._send_ready(now))
    assert sent[0][1] == ["Wind warning: Greater Victoria, Metro Vancouver. Gusts to 90 km/h."]
    s._queue(CH, eccc("Greater Victoria", stamp="continued"), now)
    asyncio.run(s._send_ready(now))
    assert len(sent) == 1
    s._queue(CH, eccc("Sunshine Coast - Gibsons"), now)
    asyncio.run(s._send_ready(now))
    assert len(sent) == 2 and "Sunshine Coast" in sent[1][1][0]


def test_weather_waits_to_group_but_urgent_does_not():
    s, sent = make()
    s.bot.config.set("PublicAlerts", "group_minutes", "5")
    now = time.time()
    s._queue(CH, eccc("Greater Victoria"), now)
    s._queue(CH, fresh(pa.parse_cap(real_urgent())), now)
    asyncio.run(s._send_ready(now))
    assert len(sent) == 1 and sent[0][1][0].startswith("ALERT")
    asyncio.run(s._send_ready(now + 301))
    assert len(sent) == 2


def test_failed_send_is_tried_again():
    s, sent = make(ok=False)
    now = time.time()
    s._queue(CH, fresh(pa.parse_cap(real_urgent())), now)
    asyncio.run(s._send_ready(now))
    asyncio.run(s._send_ready(now))
    assert len(sent) == 2 and len(s._urgent) == 1


def test_min_level():
    s, _ = make("route1_min_level=urgent\n")
    assert not passes(s, eccc("Greater Victoria"))


def test_ended_weather_is_skipped_and_old_alerts_too():
    s, _ = make()
    gone = eccc("Greater Victoria")
    gone.ended = True
    assert not passes(s, gone)
    old = eccc("Greater Victoria")
    old.sent = time.time() - 7200
    assert not passes(s, old)


def test_sos_is_refused():
    s, sent = make()
    s.bot.config.set("PublicAlerts", "route1_channel", "#SOS")
    assert s.routes() == []
    assert not asyncio.run(s._send("#SOS", ["x"]))
    assert not sent


def test_channel_change_applies_without_restart():
    s, sent = make()
    s.bot.config.set("PublicAlerts", "route1_channel", "#other")
    assert s.routes()[0]["channel"] == "#other"


def test_messages_fit():
    long = eccc("Greater Victoria", what="word " * 60)
    assert len(pa.format_group(long.title, long.areas, long.what).encode()) <= pa.MAX_BYTES
    a = fresh(pa.parse_cap(real_urgent()))
    a.instruction = "é " * 200
    assert all(len(m.encode()) <= pa.MAX_BYTES for m in pa.format_urgent(a))


def test_sources_skip_disabled_and_keep_order():
    s, _ = make("source2_type=eccc\nsource2_url=http://b/\nsource2_enabled=false\n"
                "source3_type=eccc\nsource3_url=http://c/?a=1,2\n")
    assert [x["url"] for x in s.sources()] == ["http://a/", "http://c/?a=1,2"]


def test_state_survives_a_restart():
    db = DB()
    s, _ = make(db=db)
    s._mark_seen("k")
    s._primed.add("http://a/")
    s._posted["wind warning|X"] = time.time() + 100
    s._save_state()
    assert json.loads(db.data[pa.STATE_KEY])["seen"] == ["k"]
    again, _ = make(db=db)
    assert "k" in again._seen_set and "http://a/" in again._primed and again._posted


def test_eccc_api_shape():
    data = json.dumps({"features": [{"properties": {
        "alert_code": "WDW", "alert_type": "warning", "alert_name_en": "wind warning",
        "feature_name_en": "Greater Victoria", "feature_id": "f", "status_en": "continued",
        "alert_text_en": "Damaging.\n\nWhat:\nGusts of 90 km/h.\n", "publication_datetime": "2026-11-06T12:24:10.161Z",
        "expiration_datetime": "2026-11-07T04:24:10.161Z",
    }}]}).encode()
    a = pa.parse_eccc(data)[0]
    assert a.hazard == "wind warning" and a.what == "Gusts to 90 km/h." and a.level == 1 and a.status == "Actual"


def test_doctype_is_refused():
    try:
        pa.parse_cap(b"<?xml version='1.0'?><!DOCTYPE x><alert/>")
    except ValueError:
        return
    raise AssertionError


SALT_SPRING = "route2_channel=#saltspring\nroute2_area_codes=5917027\nroute2_min_level=warning\n"


def gulf_islands_wind():
    return pa.Alert(key="w", title="Wind warning", hazard="wind warning", from_eccc=True, what="Gusts.",
                    areas=["Southern Gulf Islands", "Greater Victoria"],
                    area_codes={"Southern Gulf Islands": ["5917027", "5917029"], "Greater Victoria": ["5917005"]},
                    sent=time.time(), expires=time.time() + 3600)


def test_one_island_section_gets_only_its_own_region():
    s, _ = make(SALT_SPRING)
    wide, island = s.routes()
    assert s.for_route(gulf_islands_wind(), wide).areas == ["Southern Gulf Islands", "Greater Victoria"]
    assert s.for_route(gulf_islands_wind(), island).areas == ["Southern Gulf Islands"]


def test_alert_ready_for_another_area_skips_the_island():
    s, _ = make(SALT_SPRING)
    wide, island = s.routes()
    nanaimo = fresh(pa.parse_cap(real_urgent()))
    assert s.for_route(nanaimo, wide) and not s.for_route(nanaimo, island)
    here = fresh(pa.parse_cap(real_urgent("Salt Spring Island", "5917027")))
    assert s.for_route(here, wide) and s.for_route(here, island)


def test_both_sections_post_and_track_separately():
    s, sent = make(SALT_SPRING)
    now = time.time()
    for route in s.routes():
        s._queue(route["channel"], s.for_route(gulf_islands_wind(), route), now)
    asyncio.run(s._send_ready(now))
    assert sorted(ch for ch, _ in sent) == ["#saltspring", "#swbc-alerts"]
    for route in s.routes():
        s._queue(route["channel"], s.for_route(gulf_islands_wind(), route), now)
    asyncio.run(s._send_ready(now))
    assert len(sent) == 2


def test_section_levels_are_separate():
    s, _ = make(SALT_SPRING.replace("min_level=warning", "min_level=urgent"))
    wide, island = s.routes()
    assert s.for_route(gulf_islands_wind(), wide) and not s.for_route(gulf_islands_wind(), island)


def test_switched_off_or_empty_sections_are_skipped():
    s, _ = make("route2_channel=#a\nroute2_area_codes=5917027\nroute2_enabled=false\nroute3_channel=#b\n")
    assert [r["channel"] for r in s.routes()] == ["#swbc-alerts"]


def test_division_code_does_not_pull_in_neighbouring_weather():
    a = pa.Alert(key="x", title="t", hazard="h", from_eccc=True, areas=["Central Coast"],
                 area_codes={"Central Coast": ["5943012"]})
    assert pa.matching_areas(a, ["5943"], []) == []
    assert pa.matching_areas(a, ["5943012"], []) == ["Central Coast"]


FIX = HERE / "fixtures"
HEALTH = (
    "route4_channel=#ssi\nroute4_health_authority=island-health\n"
    "route4_health_locations=Salt Spring Island\nroute4_health_boil_water=true\n"
    "route4_health_do_not_consume=true\nroute4_health_lifted=true\n"
)
LIST_PAGE = (FIX / "health_list.html").read_text(errors="ignore")


def health_service(extra=HEALTH, page=LIST_PAGE):
    s, sent = make(extra)
    state = {"page": page}
    def fetch(url):
        if url.endswith("public-notices"):
            return state["page"].encode()
        return (FIX / "health_boil.html").read_bytes()
    s._fetch = fetch
    s._read_source = lambda source: []
    return s, sent, state


def run(s, offset=0.0):
    now = time.time() + offset
    asyncio.run(s._poll_health(s.routes(), now))
    asyncio.run(s._send_ready(now))


def row(permit, name, location="Salt Spring Island", start="11/06/2026"):
    return (f'<div class="w-row"><div class="w-col w-col-6"><h4 class="establishment-list-name">'
            f'<a href="/island-health/water-drinking-facility/?permitID={permit}" class="hyperlink1">{name}</a>'
            f'</h4></div><div class="w-col w-col-4">{location}</div><div class="w-col w-col-2">{start}</div></div>')


def test_real_list_parses():
    rows = pa.parse_health_list(LIST_PAGE)
    assert len(rows) == 56
    assert sum(r["location"] == "Salt Spring Island" for r in rows) == 6
    assert all(r["permit"] and r["name"] and r["start"] for r in rows)


def test_real_facility_pages_give_the_type():
    read = lambda f: pa.parse_health_type((FIX / f).read_text(errors="ignore"))
    assert read("health_boil.html") == "Boil Water Notice"
    assert read("health_dnc.html") == "Do Not Consume"
    assert read("health_quality.html") == "Water Quality Notice"


def test_existing_notices_are_never_posted():
    s, sent, _ = health_service()
    run(s)
    assert not sent and len(s._health["island-health"]) == 56


def test_new_notice_posts_only_to_the_following_channel():
    s, sent, state = health_service()
    run(s)
    state["page"] = LIST_PAGE + row("NEW1", "Ganges Water")
    run(s, 3700)
    assert sent == [("#ssi", ["Boil Water Notice: Ganges Water, Salt Spring Island. Check with your water supplier."])]


def test_other_locations_are_ignored():
    s, sent, state = health_service()
    run(s)
    state["page"] = LIST_PAGE + row("NEW2", "Pender Water", "Pender Island")
    run(s, 3700)
    assert not sent


def test_unticked_types_are_skipped():
    s, sent, state = health_service(HEALTH.replace("route4_health_boil_water=true\n", ""))
    run(s)
    state["page"] = LIST_PAGE + row("NEW3", "Ganges Water")
    run(s, 3700)
    assert not sent


def test_nothing_ticked_follows_nothing():
    s, sent, state = health_service(
        "route4_channel=#ssi\nroute4_health_authority=island-health\nroute4_health_locations=Salt Spring Island\n"
    )
    run(s)
    state["page"] = LIST_PAGE + row("NEW4", "Ganges Water")
    run(s, 3700)
    assert not sent


def test_lifted_notice_posts_once():
    s, sent, state = health_service()
    state["page"] = LIST_PAGE + row("GONE", "Ganges Water")
    run(s)
    state["page"] = LIST_PAGE
    run(s, 3700)
    assert sent == [("#ssi", ["No longer listed: Boil Water Notice for Ganges Water, Salt Spring Island. "
                              "Confirm with your supplier."])]
    run(s, 7400)
    assert len(sent) == 1


def test_broken_page_does_not_look_like_everything_lifted():
    s, sent, state = health_service()
    run(s)
    state["page"] = "<html>new layout</html>"
    run(s, 3700)
    assert not sent and len(s._health["island-health"]) == 56
    assert any("layout" in line for line in s.bot.logger.lines)


def test_page_is_only_read_on_the_interval():
    s, _, _ = health_service()
    calls = []
    original = s._fetch
    s._fetch = lambda url: (calls.append(url), original(url))[1]
    run(s)
    list_reads = lambda: sum(u.endswith("public-notices") for u in calls)
    assert list_reads() == 1
    run(s, 60)
    assert list_reads() == 1
    run(s, 3700)
    assert list_reads() == 2


def test_failed_detail_read_is_retried():
    s, sent, state = health_service()
    run(s)
    good = s._fetch
    def broken(url):
        if "NEW5" in url:
            raise OSError("down")
        return good(url)
    s._fetch = broken
    state["page"] = LIST_PAGE + row("NEW5", "Ganges Water")
    run(s, 3700)
    assert not sent
    s._fetch = good
    run(s, 7400)
    assert len(sent) == 1


def test_health_only_section_is_valid_and_gets_no_public_alerts():
    s, _, _ = health_service()
    route = s.routes()[-1]
    assert route["channel"] == "#ssi" and route["health_authority"] == "island-health"
    assert s.for_route(fresh(pa.parse_cap(real_urgent())), route) is None


def test_health_notice_fits():
    text = pa.format_health({"name": "x" * 200, "location": "Salt Spring Island", "type": "Boil Water Notice"})
    assert len(text.encode()) <= pa.MAX_BYTES


def test_failed_detail_read_on_first_read_stays_in_the_baseline():
    s, sent, _ = health_service()
    good = s._fetch
    def broken(url):
        if url.endswith("public-notices"):
            return good(url)
        raise OSError("down")
    s._fetch = broken
    run(s)
    s._fetch = good
    run(s, 3700)
    assert not sent


def test_longest_real_lifted_message_is_not_cut():
    text = pa.format_health({"name": "Salt Spring Island Kombucha Water System",
                             "location": "Salt Spring Island", "type": "Boil Water Notice"}, lifted=True)
    assert text.endswith("Confirm with your supplier.")


GULF = (FIX / "wind_yellow_gulf_islands.xml").read_bytes()


def test_real_yellow_warning_keeps_colour_and_timing():
    a = pa.parse_cap(GULF, "k")
    assert a.colour == "yellow" and a.title == "Yellow wind warning" and a.level == 1
    line = pa.format_group(a.title, ["Southern Gulf Islands"], a.what)
    assert line == "Yellow wind warning: Southern Gulf Islands. When: This evening. Winds 70 km/h gusting to 90."


def colour_route(colour):
    extra = f"route4_channel=#x\nroute4_area_names=Southern Gulf Islands\n"
    if colour:
        extra += f"route4_min_colour={colour}\n"
    s, _ = make(extra)
    return s, s.routes()[-1]


def coloured(colour, level=1):
    return pa.Alert(key="c", title="t", hazard=f"{colour} wind warning".strip(), colour=colour, level=level,
                    from_eccc=True, areas=["Southern Gulf Islands"])


def test_section_without_the_setting_gets_every_colour():
    s, route = colour_route(None)
    assert all(s.for_route(coloured(c), route) for c in pa.COLOURS)


def test_orange_section_skips_yellow():
    s, route = colour_route("orange")
    assert s.for_route(coloured("yellow"), route) is None
    assert s.for_route(coloured("orange"), route) and s.for_route(coloured("red"), route)


def test_red_section_takes_only_red():
    s, route = colour_route("red")
    assert s.for_route(coloured("orange"), route) is None and s.for_route(coloured("red"), route)


def test_emergency_alerts_ignore_the_colour_setting():
    s, route = colour_route("red")
    assert s.for_route(coloured("yellow", level=0), route)
    assert s.for_route(coloured("", level=0), route)


def test_alerts_with_no_colour_ignore_the_colour_setting():
    s, route = colour_route("red")
    assert s.for_route(coloured(""), route)


def test_warning_raised_to_orange_posts_again():
    s, sent = make("route4_channel=#x\nroute4_area_names=Southern Gulf Islands\n")
    now = time.time()
    s._queue("#x", coloured("yellow"), now)
    s._posted["#x|yellow wind warning|Southern Gulf Islands"] = now + 100
    s._pending.clear()
    s._queue("#x", coloured("orange"), now)
    assert ("#x", "orange wind warning") in s._pending


def test_grouped_areas_with_different_timing_show_no_timing():
    s, _ = make("route4_channel=#x\nroute4_area_names=A,B\n")
    now = time.time()
    first = replace(coloured("yellow"), areas=["A"], what="When: Tonight. Windy.")
    second = replace(coloured("yellow"), areas=["B"], what="When: Sunday. Windy.")
    s._queue("#x", first, now)
    s._queue("#x", second, now)
    assert s._pending[("#x", "yellow wind warning")]["what"] == ""


def test_api_alerts_match_the_feed_name():
    data = json.dumps({"features": [{"properties": {
        "alert_code": "WDW", "alert_type": "warning", "alert_name_en": "wind warning",
        "risk_colour_en": "orange", "feature_name_en": "X", "feature_id": "1", "status_en": "issued",
    }}]}).encode()
    a = pa.parse_eccc(data)[0]
    assert a.hazard == "orange wind warning" and a.colour == "orange"


@pytest.mark.parametrize("text,expected", [
    ("Strong west winds with gusts to 90 km/h are expected.", "Gusts to 90 km/h."),
    ("Southwesterly winds gusting to 90 km/h tonight.", "Gusts to 90 km/h."),
    ("Strong winds gusting up to 110 km/h continue.", "Gusts to 110 km/h."),
    ("Maximum wind gusts: southeasterly up to 90 km/h.", "Gusts to 90 km/h."),
    ("What: Southwest winds gusting up to 90 kilometres per hour.", "Gusts to 90 km/h."),
    ("Strong winds gusting between 90 and 100 km/h will continue.", "Gusts 90 to 100 km/h."),
    ("A strong push of southwest wind with gusts ranging from 80-100 km/h.", "Gusts 80 to 100 km/h."),
    ("Southwest winds gusting to 90 to 100 kilometres per hour.", "Gusts 90 to 100 km/h."),
    ("southeasterly winds 80 gusting to 100 km/h will develop", "Winds 80 km/h gusting to 100."),
    ("gusting to 90 km/h. Later, winds 70 km/h gusting to 90 will develop", "Winds 70 km/h gusting to 90."),
])
def test_clear_wind_speeds_are_read(text, expected):
    assert pa.wind_speeds(text) == expected


@pytest.mark.parametrize("text", [
    "Maximum wind gusts: 80 km/h, except 110 to 120 in areas prone to enhanced southeasterlies.",
    "Gusts to 90 km/h, locally 120 km/h near the strait.",
    "Wind gusts of 100 km/h were reported at Slave Lake earlier this morning.",
    "Wind gusts of 100 km/h have been reported at Slave Lake.",
    "Southeast gusts to 90 km/h this evening. Southwest gusts to 100 km/h overnight.",
    "Winds of 60 to 70 km/h gusting to 100.",
    "Damaging winds are expected.",
    "###",
    "",
])
def test_unclear_wind_speeds_give_nothing(text):
    assert pa.wind_speeds(text) == ""


def test_unclear_speeds_keep_the_alert_wording():
    text = "Damaging winds are expected.\nGusts to 90 km/h, except 120 in the strait."
    assert pa._what_line(text, wind=True) == "Damaging winds are expected."


def test_speeds_are_only_for_wind_alerts():
    text = "Blizzard conditions expected.\nGusts to 90 km/h."
    assert pa._what_line(text) == "Blizzard conditions expected."


# --- several bots on one channel ---

PEERS = "peer_bots=Bot A, Bot B\nrole=backup1\n"


def fire(s, now):
    asyncio.run(s._send_ready(now))


def urgent_on(s, now):
    alert = fresh(pa.parse_cap(real_urgent()))
    s._queue(CH, alert, now)
    return alert


def test_roles_set_the_wait():
    assert make("peer_bots=A\nrole=primary\n")[0]._wait() == 0
    assert make("peer_bots=A\nrole=backup1\n")[0]._wait() == 120
    assert make("peer_bots=A\nrole=backup2\n")[0]._wait() == 240


def test_the_gap_follows_how_often_bots_check():
    assert make("peer_bots=A\nrole=backup1\npoll_seconds=300\n")[0]._wait() == 360


def test_a_role_we_do_not_know_posts_rather_than_stays_quiet():
    assert make("peer_bots=A\nrole=nonsense\n")[0]._wait() == 0


def test_no_other_bots_means_no_wait():
    s, sent = make("role=backup1\n")
    now = time.time()
    urgent_on(s, now)
    fire(s, now)
    assert len(sent) == 1


def test_urgent_waits_then_posts_if_nothing_is_heard():
    s, sent = make(PEERS)
    now = time.time()
    urgent_on(s, now)
    fire(s, now + 60)
    assert sent == [] and len(s._urgent) == 1
    fire(s, now + 120)
    assert len(sent) == 1 and s._urgent == []


def test_urgent_stays_quiet_if_another_bot_posted_it():
    s, sent = make(PEERS)
    now = time.time()
    alert = urgent_on(s, now)
    s._hear(CH, "Bot A: " + pa.format_urgent(alert)[0], now + 5)
    fire(s, now + 120)
    assert sent == [] and s._urgent == []


def test_heard_before_this_bot_saw_the_alert_counts():
    s, sent = make(PEERS)
    now = time.time()
    alert = fresh(pa.parse_cap(real_urgent()))
    s._hear(CH, "Bot A: " + pa.format_urgent(replace(alert, areas=alert.areas))[0], now + 1)
    s._queue(CH, alert, now + 30)
    fire(s, now + 150)
    assert sent == []


def test_only_named_bots_are_listened_to():
    s, sent = make(PEERS)
    now = time.time()
    alert = urgent_on(s, now)
    s._hear(CH, "Somebody: " + pa.format_urgent(alert)[0], now + 5)
    fire(s, now + 120)
    assert len(sent) == 1


def test_a_post_on_another_channel_does_not_count():
    s, sent = make(PEERS)
    now = time.time()
    alert = urgent_on(s, now)
    s._hear("#elsewhere", "Bot A: " + pa.format_urgent(alert)[0], now + 5)
    fire(s, now + 120)
    assert len(sent) == 1


def test_names_and_channels_ignore_case():
    s, sent = make(PEERS)
    now = time.time()
    alert = urgent_on(s, now)
    s._hear(CH.upper(), "BOT a: " + pa.format_urgent(alert)[0], now + 5)
    fire(s, now + 120)
    assert sent == []


def test_a_repeated_update_is_not_silenced_by_the_earlier_post():
    s, sent = make(PEERS)
    now = time.time()
    alert = fresh(pa.parse_cap(real_urgent()))
    s._hear(CH, "Bot A: " + pa.format_urgent(alert)[0], now - 1800)
    s._queue(CH, alert, now)
    fire(s, now + 120)
    assert len(sent) == 1


def group_on(s, now, *areas):
    for area in areas:
        s._queue(CH, fresh(eccc_alert(area)), now)


def eccc_alert(area):
    return pa.Alert(key=area, title="Yellow wind warning", hazard="yellow wind warning", areas=[area],
                    from_eccc=True, what="Winds 70 km/h.", colour="yellow")


def test_weather_heard_is_marked_posted_so_it_stays_quiet_later():
    s, sent = make(PEERS)
    now = time.time()
    group_on(s, now, "Greater Victoria")
    s._hear(CH, "Bot A: Yellow wind warning: Greater Victoria. Winds 70 km/h.", now + 5)
    fire(s, now + 120)
    assert sent == [] and s._pending == {}
    group_on(s, now + 200, "Greater Victoria")
    assert s._pending == {}


def test_weather_posts_only_the_areas_nobody_covered():
    s, sent = make(PEERS)
    now = time.time()
    group_on(s, now, "Greater Victoria", "Southern Gulf Islands")
    s._hear(CH, "Bot A: Yellow wind warning: Greater Victoria. Winds 70 km/h.", now + 5)
    fire(s, now + 120)
    assert len(sent) == 1
    assert "Southern Gulf Islands" in sent[0][1][0] and "Greater Victoria" not in sent[0][1][0]


def test_a_different_hazard_is_not_covered():
    s, sent = make(PEERS)
    now = time.time()
    group_on(s, now, "Greater Victoria")
    s._hear(CH, "Bot A: Orange rain warning: Greater Victoria.", now + 5)
    fire(s, now + 120)
    assert len(sent) == 1


def test_water_notice_heard_from_another_bot_is_not_repeated():
    s, sent = make(PEERS)
    now = time.time()
    s._outbox.append((CH, "Boil Water Notice: Test Water, Salt Spring Island.", now))
    s._hear(CH, "Bot A: Boil Water Notice: Test Water, Salt Spring Island.", now - 3000)
    fire(s, now + 120)
    assert sent == [] and s._outbox == []


def test_water_notice_waits_and_posts_if_nothing_is_heard():
    s, sent = make(PEERS)
    now = time.time()
    s._outbox.append((CH, "Boil Water Notice: Test Water, Salt Spring Island.", now))
    fire(s, now)
    assert sent == []
    fire(s, now + 120)
    assert len(sent) == 1


def test_heard_posts_survive_a_restart_and_old_ones_are_dropped():
    db = DB()
    s, _ = make(PEERS, db=db)
    now = time.time()
    s._hear(CH, "Bot A: old", now - pa.HEARD_KEEP_SECONDS - 10)
    s._hear(CH, "Bot A: new", now)
    s._save_state()
    s2, _ = make(PEERS, db=db)
    assert s2._heard_since(CH, 0) == ["new"]


def test_two_bots_post_once_and_the_second_covers_a_dead_first():
    """Both bots, wired together the way the channel would."""
    def pair(first_alive):
        a, a_sent = make("peer_bots=Bot B\nrole=primary\n")
        b, b_sent = make("peer_bots=Bot A\nrole=backup1\n")
        now = time.time()
        alert = fresh(pa.parse_cap(real_urgent()))
        b._queue(CH, alert, now)
        if first_alive:
            a._queue(CH, alert, now + 20)
            fire(a, now + 20)
            for channel, messages in a_sent:
                for message in messages:
                    b._hear(channel, f"Bot A: {message}", now + 40)
        fire(b, now + 120)
        return len(a_sent), len(b_sent)
    assert pair(first_alive=True) == (1, 0)
    assert pair(first_alive=False) == (0, 1)


def test_channel_events_reach_the_listener_and_bad_ones_are_harmless():
    s, _ = make(PEERS)
    s.bot.channel_manager = types.SimpleNamespace(get_channel_name=lambda n: CH)
    event = types.SimpleNamespace(payload={"channel_idx": 3, "text": "Bot A: hello"})
    asyncio.run(s._on_channel_message(event))
    assert s._heard_since(CH, 0) == ["hello"]
    asyncio.run(s._on_channel_message(types.SimpleNamespace()))
    assert s.bot.logger.lines
