"""Tests for cron keys in [Scheduled_Messages] (e.g. "0 9 1 * *" = 09:00 on the 1st)."""

import datetime
import json
from configparser import ConfigParser
from unittest.mock import Mock

import pytest
import pytz

from modules.scheduled_message_cron import cron_spec, cron_trigger, describe_cron, normalize_cron_key
from modules.scheduler import MessageScheduler

from .test_web_viewer_app import mock_viewer  # noqa: F401  (fixture)

TZ = pytz.timezone("America/Vancouver")


class TestCronSpec:
    @pytest.mark.parametrize("key", ["0 9 1 * *", "*/15 * * * *", "0 8 * * mon", "0 0 last * *", "@monthly", "@Weekly"])
    def test_valid(self, key):
        assert cron_spec(key) is not None

    @pytest.mark.parametrize("key", ["0900", "every_6h", "0 9 1 *", "0 9 32 * *", "61 * * * *", "@sometimes", ""])
    def test_invalid(self, key):
        assert cron_spec(key) is None

    def test_normalize(self):
        assert normalize_cron_key("  0  9 1 *   * ") == "0 9 1 * *"

    def test_first_of_month_next_fire(self):
        now = TZ.localize(datetime.datetime(2026, 10, 30, 12, 0))
        nxt = cron_trigger("0 9 1 * *", TZ).get_next_fire_time(None, now)
        assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 11, 1, 9, 0)

    def test_weekday_names_are_days(self):
        # 2026-10-30 is a Friday; the next Monday is Nov 2.
        now = TZ.localize(datetime.datetime(2026, 10, 30, 12, 0))
        assert cron_trigger("0 8 * * mon", TZ).get_next_fire_time(None, now).day == 2

    @pytest.mark.parametrize("key,label", [
        ("0 9 1 * *", "Monthly on the 1st at 09:00"),
        ("30 17 22 * *", "Monthly on the 22nd at 17:30"),
        ("0 9 11 * *", "Monthly on the 11th at 09:00"),
        ("0 8 * * mon", "Every Mon at 08:00"),
        ("0 12 25 12 *", "Yearly on Dec 25 at 12:00"),
        ("@monthly", "Monthly on the 1st at 00:00"),
        ("0 0 last * *", "Monthly on the last day at 00:00"),
        ("*/15 * * * *", "*/15 * * * *"),
    ])
    def test_describe(self, key, label):
        assert describe_cron(key) == label


@pytest.fixture
def scheduler(mock_logger):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "timezone", "America/Vancouver")
    bot.config.add_section("Scheduled_Messages")
    s = MessageScheduler(bot)
    yield s
    if s._apscheduler is not None:
        s._apscheduler.shutdown(wait=False)


class TestSchedulerCronKeys:
    def test_cron_key_registered(self, scheduler):
        scheduler.bot.config.set("Scheduled_Messages", "0 9 1 * *", "#kod-bot: Monthly test")
        scheduler.setup_scheduled_messages()
        assert scheduler.scheduled_messages["0 9 1 * *"] == ("#kod-bot", "Monthly test")
        job = scheduler._apscheduler.get_job("cron_0 9 1 * *_#kod-bot")
        assert job is not None and job.func == scheduler.send_scheduled_message

    def test_count_suffix_uses_counted_sender(self, scheduler):
        scheduler.bot.config.set("Scheduled_Messages", "@monthly", "#kod-bot: Twice::2")
        scheduler.setup_scheduled_messages()
        job = scheduler._apscheduler.get_job("cron_@monthly_#kod-bot")
        assert job.func == scheduler.send_scheduled_message_counted
        assert job.args[1] == "Twice"

    def test_mixed_keys_no_warnings(self, scheduler):
        scheduler.bot.config.set("Scheduled_Messages", "0900", "general: Morning")
        scheduler.bot.config.set("Scheduled_Messages", "every_6h", "general: Ping")
        scheduler.bot.config.set("Scheduled_Messages", "0 9 1 * *", "general: Monthly")
        scheduler.setup_scheduled_messages()
        assert len(scheduler._apscheduler.get_jobs()) == 3
        warned = [str(c) for c in scheduler.logger.warning.call_args_list]
        assert not any("Invalid time format" in w for w in warned)

    def test_bad_cron_key_skipped(self, scheduler):
        scheduler.bot.config.set("Scheduled_Messages", "0 9 32 * *", "general: Never")
        scheduler.setup_scheduled_messages()
        assert scheduler.scheduled_messages == {}


HDR = {"X-Requested-With": "XMLHttpRequest"}


class TestApiCronKeys:
    def _post(self, client, **body):
        return client.post("/api/scheduled-messages", data=json.dumps(body), content_type="application/json", headers=HDR)

    def test_create_list_update_delete(self, mock_viewer):  # noqa: F811
        with mock_viewer.app.test_client() as client:
            r = self._post(client, key="0  9 1 * *", channel="#kod-bot", message="Monthly test")
            assert r.status_code == 200, r.data
            with open(mock_viewer.config_path) as f:
                assert "0 9 1 * * = #kod-bot:Monthly test" in f.read()

            msgs = client.get("/api/scheduled-messages", headers=HDR).get_json()["messages"]
            assert len(msgs) == 1
            m = msgs[0]
            assert (m["type"], m["label"], m["spec"]) == ("cron", "Monthly on the 1st at 09:00", "0 9 1 * *")
            assert m["next"] and m["next"][8:] == "01 09:00"

            r = client.put("/api/scheduled-messages/" + "0%209%201%20*%20*",
                           data=json.dumps({"key": "0 8 * * mon", "channel": "#kod-bot", "message": "Weekly", "count": 3}),
                           content_type="application/json", headers=HDR)
            assert r.status_code == 200, r.data
            msgs = client.get("/api/scheduled-messages", headers=HDR).get_json()["messages"]
            assert [(m["key"], m["count"]) for m in msgs] == [("0 8 * * mon", 3)]

            r = client.delete("/api/scheduled-messages/0%208%20*%20*%20mon", headers=HDR)
            assert r.status_code == 200, r.data
            assert client.get("/api/scheduled-messages", headers=HDR).get_json()["messages"] == []

    def test_invalid_cron_rejected(self, mock_viewer):  # noqa: F811
        with mock_viewer.app.test_client() as client:
            r = self._post(client, key="0 9 32 * *", channel="#kod-bot", message="x")
            assert r.status_code == 400
