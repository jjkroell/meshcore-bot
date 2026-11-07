"""Tests for airplanes/ADS-B command API URL handling and fetch errors."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from modules.commands.airplanes_command import (
    DEFAULT_API_URL,
    USER_AGENT,
    AirplanesCommand,
    _response_body_snippet,
    is_deprecated_public_airplanes_live_url,
    normalize_api_url,
)


class TestNormalizeApiUrl:
    def test_adds_trailing_slash(self):
        assert normalize_api_url("https://api.adsb.lol/v2") == "https://api.adsb.lol/v2/"

    def test_keeps_trailing_slash(self):
        assert normalize_api_url("https://api.adsb.lol/v2/") == "https://api.adsb.lol/v2/"

    def test_empty_stays_empty(self):
        assert normalize_api_url("  ") == ""


class TestDeprecatedAirplanesLiveUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://api.airplanes.live/v2/",
            "https://api.airplanes.live/v2/",
            "http://api.airplanes.live/v2",
            "https://API.AIRPLANES.LIVE/v2/",
        ],
    )
    def test_detects_public_host(self, url):
        assert is_deprecated_public_airplanes_live_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            DEFAULT_API_URL,
            "http://localhost:8080/data/",
            "https://rest.api.airplanes.live/",
            "https://api.airplanes.live.example.com/v2/",
        ],
    )
    def test_ignores_other_hosts(self, url):
        assert is_deprecated_public_airplanes_live_url(url) is False


class TestResolveApiUrl:
    def test_default_is_adsb_lol(self, command_mock_bot):
        cmd = AirplanesCommand(command_mock_bot)
        assert cmd.api_url == DEFAULT_API_URL

    @pytest.mark.parametrize(
        "configured",
        [
            "http://api.airplanes.live/v2/",
            "https://api.airplanes.live/v2",
        ],
    )
    def test_remaps_legacy_airplanes_live(self, command_mock_bot, configured):
        command_mock_bot.config.add_section("Airplanes_Command")
        command_mock_bot.config.set("Airplanes_Command", "api_url", configured)
        cmd = AirplanesCommand(command_mock_bot)
        assert cmd.api_url == DEFAULT_API_URL
        command_mock_bot.logger.warning.assert_called()
        warning = " ".join(str(arg) for arg in command_mock_bot.logger.warning.call_args[0])
        assert "api.airplanes.live" in warning
        assert DEFAULT_API_URL in warning

    def test_preserves_local_readsb_url(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        command_mock_bot.config.set("Airplanes_Command", "api_url", "http://localhost:8080/data")
        cmd = AirplanesCommand(command_mock_bot)
        assert cmd.api_url == "http://localhost:8080/data/"
        command_mock_bot.logger.warning.assert_not_called()

    def test_preserves_airplanes_live_pro_host(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        command_mock_bot.config.set(
            "Airplanes_Command", "api_url", "https://rest.api.airplanes.live/"
        )
        cmd = AirplanesCommand(command_mock_bot)
        assert cmd.api_url == "https://rest.api.airplanes.live/"

    def test_empty_api_url_uses_default(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        command_mock_bot.config.set("Airplanes_Command", "api_url", "  ")
        cmd = AirplanesCommand(command_mock_bot)
        assert cmd.api_url == DEFAULT_API_URL


class TestFetchAircraftData:
    def test_sends_user_agent_and_builds_point_url(self, command_mock_bot):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {"ac": []}
        cmd = AirplanesCommand(command_mock_bot)

        with patch(
            "modules.commands.airplanes_command.requests.get",
            return_value=mock_response,
        ) as mock_get:
            data = cmd._fetch_aircraft_data(47.6, -122.3, 25)

        assert data == {"ac": []}
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert args[0] == f"{DEFAULT_API_URL}point/47.6/-122.3/25"
        assert kwargs["headers"]["User-Agent"] == USER_AGENT
        assert kwargs["headers"]["Accept"] == "application/json"
        assert kwargs["timeout"] == cmd.url_timeout

    def test_logs_http_error_status_and_sanitized_body(self, command_mock_bot):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 403
        mock_response.reason = "Forbidden"
        mock_response.text = (
            '{"error": "Please contact us at contact@airplanes.live."}\nsecret'
        )
        cmd = AirplanesCommand(command_mock_bot)

        with patch(
            "modules.commands.airplanes_command.requests.get",
            return_value=mock_response,
        ):
            assert cmd._fetch_aircraft_data(47.6, -122.3, 25) is None

        command_mock_bot.logger.warning.assert_called()
        args = command_mock_bot.logger.warning.call_args[0]
        assert args[1] == 403
        assert "api.adsb.lol" in args[2]
        snippet = args[3]
        assert "Please contact us" in snippet
        assert "\n" not in snippet

    def test_timeout_returns_none(self, command_mock_bot):
        cmd = AirplanesCommand(command_mock_bot)
        with patch(
            "modules.commands.airplanes_command.requests.get",
            side_effect=requests.exceptions.Timeout(),
        ):
            assert cmd._fetch_aircraft_data(47.6, -122.3, 25) is None
        command_mock_bot.logger.warning.assert_called()


class TestResponseBodySnippet:
    def test_strips_newlines_and_truncates(self):
        response = MagicMock()
        response.text = "line1\nline2" + ("x" * 300)
        response.reason = "Forbidden"
        snippet = _response_body_snippet(response, max_length=20)
        assert "\n" not in snippet
        assert len(snippet) <= 20
        assert snippet.startswith("line1")


class TestPlaceName:
    def _run(self, command_mock_bot, text, geocode=(49.28, -123.12, None)):
        """Run a command; return (geocode mock, fetch mock, send mock)."""
        cmd = AirplanesCommand(command_mock_bot)
        cmd.translate = MagicMock(side_effect=lambda key, **kw: f"{key} {kw}")
        cmd.send_response = AsyncMock(return_value=True)
        cmd._get_companion_location = MagicMock(return_value=(1.0, 2.0))
        cmd._fetch_aircraft_data_async = AsyncMock(return_value={"ac": []})
        message = MagicMock(content=text)
        with patch(
            "modules.commands.airplanes_command.geocode_city", new=AsyncMock(return_value=geocode)
        ) as geo:
            asyncio.run(cmd.execute(message))
        return geo, cmd._fetch_aircraft_data_async, cmd.send_response

    def test_place_is_searched(self, command_mock_bot):
        geo, fetch, _ = self._run(command_mock_bot, "airplanes vancouver")
        assert geo.await_args.args[1] == "vancouver"
        assert fetch.await_args.args[:2] == (49.28, -123.12)

    def test_multi_word_place_keeps_filters(self, command_mock_bot):
        geo, fetch, _ = self._run(command_mock_bot, "planes port alberni radius=50 highest")
        assert geo.await_args.args[1] == "port alberni"
        assert fetch.await_args.args[2] == 50

    def test_unknown_place_says_so_and_does_not_search(self, command_mock_bot):
        _, fetch, send = self._run(command_mock_bot, "airplanes nowhereville", geocode=(None, None, None))
        fetch.assert_not_awaited()
        assert "place_not_found" in send.await_args.args[1]

    @pytest.mark.parametrize("text", ["airplanes", "airplanes military", "airplanes radius=10 fastest"])
    def test_no_place_uses_your_location(self, command_mock_bot, text):
        geo, fetch, _ = self._run(command_mock_bot, text)
        geo.assert_not_awaited()
        assert fetch.await_args.args[:2] == (1.0, 2.0)

    def test_coordinates_skip_the_lookup(self, command_mock_bot):
        geo, fetch, _ = self._run(command_mock_bot, "airplanes 49.19,-123.18")
        geo.assert_not_awaited()
        assert fetch.await_args.args[:2] == (49.19, -123.18)


class TestHighestSort:
    def test_aircraft_on_the_ground_sort_last(self, command_mock_bot):
        cmd = AirplanesCommand(command_mock_bot)
        filters = cmd._parse_filters(["highest"])
        aircraft = [
            {"flight": "PARKED", "alt_baro": "ground", "lat": 49.2, "lon": -123.1},
            {"flight": "HIGH", "alt_baro": 35000, "lat": 49.2, "lon": -123.1},
            {"flight": "LOW", "alt_baro": 900, "lat": 49.2, "lon": -123.1},
        ]
        result = cmd._filter_aircraft(aircraft, filters, 49.2, -123.1)
        assert [a["flight"] for a in result] == ["HIGH", "LOW", "PARKED"]
