"""Pump, CGM and uploader details: their devices, last bolus/carbs/alert, CGM status."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from nocturne_py import (
    Bolus,
    CarbIntake,
    DeviceAgeInfo,
    PumpSnapshot,
    SystemEvent,
    SystemEventType,
    TempBasal,
)
from nocturne_py.models import PaginatedResponseOfBolus as BolusPaginatedResponse
from nocturne_py.models import PaginatedResponseOfTempBasal as TempBasalPaginatedResponse

from custom_components.nocturne.api import BOLUS_PAGE_SIZE, TEMP_BASAL_PAGE_SIZE, NocturneApiClient
from custom_components.nocturne.const import DOMAIN

from .conftest import make_api_client, make_entry, set_up

STAMP = datetime(2026, 11, 16, 7, 19, 9, tzinfo=UTC)


def _cgm_status(battery=97, state="NO_ERROR_MESSAGE", in_range=True, status=None):
    connect = {"conduitSensorInRange": in_range}
    if status is not None:
        connect["sensorStatus"] = status
    return {
        "cgm": {
            "manufacturer": "Medtronic",
            "model": "Guardian (DURABLE)",
            "transmitterBattery": battery,
            "sensorState": state,
        },
        "connect": connect,
    }


async def _states(hass, stubs, **returns):
    entry = make_entry(expires_in=3600)
    client = make_api_client()
    client.get_latest_aps_snapshot.return_value = None
    for name, value in returns.items():
        getattr(client, name).return_value = value
    stubs.clients[entry.data["instance_url"]] = client
    await set_up(hass, entry)
    return entry, client


class TestDevices:
    async def test_each_battery_lives_on_its_own_device(self, reauth_hass, stubs):
        entry, _ = await _states(
            reauth_hass,
            stubs,
            get_latest_pump_snapshot=PumpSnapshot(
                manufacturer="Medtronic", model="MMT-1886", battery_percent=75
            ),
            get_cgm_status=_cgm_status(),
        )
        devices = dr.async_get(reauth_hass)
        pump = devices.async_get_device({(DOMAIN, f"{entry.entry_id}_pump")})
        cgm = devices.async_get_device({(DOMAIN, f"{entry.entry_id}_cgm")})

        assert (pump.manufacturer, pump.model) == ("Medtronic", "MMT-1886")
        assert (cgm.manufacturer, cgm.model) == ("Medtronic", "Guardian (DURABLE)")
        assert reauth_hass.states.get("sensor.nocturne_pump_battery").state == "75"
        assert reauth_hass.states.get("sensor.nocturne_cgm_battery").state == "97"
        assert reauth_hass.states.get("sensor.nocturne_uploader_battery").state == "90"


class TestCgmStatus:
    async def test_battery_and_state_while_in_range(self, reauth_hass, stubs):
        await _states(reauth_hass, stubs, get_cgm_status=_cgm_status(state="WARM_UP"))
        assert reauth_hass.states.get("sensor.nocturne_cgm_battery").state == "97"
        assert reauth_hass.states.get("sensor.nocturne_cgm_sensor_state").state == "Warming up"

    async def test_battery_is_hidden_out_of_range(self, reauth_hass, stubs):
        await _states(
            reauth_hass,
            stubs,
            get_cgm_status=_cgm_status(battery=100, state="SEARCHING_FOR_SENSOR_SIGNAL", in_range=False),
        )
        assert reauth_hass.states.get("sensor.nocturne_cgm_battery").state == STATE_UNAVAILABLE
        assert (
            reauth_hass.states.get("sensor.nocturne_cgm_sensor_state").state
            == "Searching for signal"
        )

    async def test_unknown_state_is_unavailable(self, reauth_hass, stubs):
        await _states(reauth_hass, stubs, get_cgm_status=_cgm_status(state="UNKNOWN", in_range=False))
        assert reauth_hass.states.get("sensor.nocturne_cgm_sensor_state").state == STATE_UNAVAILABLE

    async def test_unmapped_state_is_made_readable(self, reauth_hass, stubs):
        await _states(reauth_hass, stubs, get_cgm_status=_cgm_status(state="SENSOR_END_OF_LIFE"))
        assert reauth_hass.states.get("sensor.nocturne_cgm_sensor_state").state == "Sensor end of life"


class TestCgmSensorStatus:
    """Nocturne's reason for there being, or not being, a current reading."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("Ok", "OK"),
            ("WarmingUp", "Warming up"),
            ("NoSensor", "No sensor"),
            ("Connecting", "Connecting"),
            ("SensorOutOfRange", "Sensor out of range"),
            ("PumpOutOfReach", "Pump out of reach"),
        ],
    )
    async def test_status_reads_in_plain_words(self, reauth_hass, stubs, status, expected):
        await _states(reauth_hass, stubs, get_cgm_status=_cgm_status(status=status))
        assert reauth_hass.states.get("sensor.nocturne_cgm_status").state == expected

    async def test_no_sensor_is_a_state_not_unavailable(self, reauth_hass, stubs):
        await _states(
            reauth_hass,
            stubs,
            get_cgm_status=_cgm_status(battery=None, in_range=False, status="NoSensor"),
        )
        assert reauth_hass.states.get("sensor.nocturne_cgm_status").state == "No sensor"

    @pytest.mark.parametrize("status", [None, "Unknown"])
    async def test_missing_or_unknown_status_is_unavailable(self, reauth_hass, stubs, status):
        await _states(reauth_hass, stubs, get_cgm_status=_cgm_status(status=status))
        assert reauth_hass.states.get("sensor.nocturne_cgm_status").state == STATE_UNAVAILABLE


class TestSensorAge:
    async def test_no_recorded_sensor_start_is_unavailable_not_zero(self, reauth_hass, stubs):
        await _states(reauth_hass, stubs, get_sensor_age=DeviceAgeInfo(found=False, days=0))
        assert reauth_hass.states.get("sensor.nocturne_cgm_sensor_age").state == STATE_UNAVAILABLE


class TestPumpSuspended:
    @pytest.mark.parametrize(("suspended", "expected"), [(True, STATE_ON), (False, STATE_OFF), (None, STATE_UNAVAILABLE)])
    async def test_follows_the_pump_snapshot(self, reauth_hass, stubs, suspended, expected):
        await _states(reauth_hass, stubs, get_latest_pump_snapshot=PumpSnapshot(suspended=suspended))
        assert reauth_hass.states.get("binary_sensor.nocturne_pump_suspended").state == expected


class TestLastBolusAndCarbs:
    async def test_amounts_with_their_time(self, reauth_hass, stubs):
        await _states(
            reauth_hass,
            stubs,
            get_latest_manual_bolus=Bolus(insulin=3.5, timestamp=STAMP),
            get_latest_carbs=CarbIntake(carbs=15, timestamp=STAMP),
        )
        bolus = reauth_hass.states.get("sensor.nocturne_pump_last_bolus")
        carbs = reauth_hass.states.get("sensor.nocturne_last_carbs")
        assert (bolus.state, bolus.attributes["time"]) == ("3.5", STAMP)
        assert (carbs.state, carbs.attributes["time"]) == ("15", STAMP)

    async def test_next_search_starts_at_the_known_bolus_and_keeps_it(self, reauth_hass, stubs):
        entry, client = await _states(
            reauth_hass, stubs, get_latest_manual_bolus=Bolus(insulin=3.5, timestamp=STAMP)
        )
        client.get_latest_manual_bolus.return_value = None
        await entry.runtime_data.glucose_coordinator.async_refresh()
        await reauth_hass.async_block_till_done()

        client.get_latest_manual_bolus.assert_called_with(STAMP)
        assert reauth_hass.states.get("sensor.nocturne_pump_last_bolus").state == "3.5"


class TestLastPumpAlert:
    async def test_known_message_with_details(self, reauth_hass, stubs):
        await _states(
            reauth_hass,
            stubs,
            get_latest_pump_alert=SystemEvent(
                event_type=SystemEventType.ALARM,
                description="BC_SID_HIGH_SG_CHECK_BG",
                mills=int(STAMP.timestamp() * 1000),
            ),
        )
        state = reauth_hass.states.get("sensor.nocturne_pump_last_alert")
        assert state.state == "Alert on high"
        assert state.attributes["time"] == STAMP
        assert state.attributes["type"] == "Alarm"
        assert state.attributes["code"] == "BC_SID_HIGH_SG_CHECK_BG"

    async def test_unmapped_message_is_made_readable(self, reauth_hass, stubs):
        await _states(
            reauth_hass,
            stubs,
            get_latest_pump_alert=SystemEvent(description="BC_SID_SOMETHING_NEW_HAPPENED"),
        )
        assert reauth_hass.states.get("sensor.nocturne_pump_last_alert").state == "Something new happened"


@pytest.fixture
def client() -> NocturneApiClient:
    return NocturneApiClient("https://example.com", AsyncMock(return_value="test-token"))


class TestApiManualBolus:
    async def test_skips_automatic_corrections_across_pages(self, client):
        autos = [Bolus(insulin=0.2, automatic=True) for _ in range(BOLUS_PAGE_SIZE)]
        manual = Bolus(insulin=3.5, automatic=False)
        with patch("custom_components.nocturne.api.BolusApi") as api_cls:
            api_cls.return_value.bolus_get_all.side_effect = [
                BolusPaginatedResponse(data=autos),
                BolusPaginatedResponse(data=[Bolus(insulin=0.1, automatic=True), manual]),
            ]
            assert await client.get_latest_manual_bolus() is manual
            offsets = [c.kwargs["offset"] for c in api_cls.return_value.bolus_get_all.call_args_list]
        assert offsets == [0, BOLUS_PAGE_SIZE]

    async def test_none_when_only_corrections(self, client):
        with patch("custom_components.nocturne.api.BolusApi") as api_cls:
            api_cls.return_value.bolus_get_all.return_value = BolusPaginatedResponse(
                data=[Bolus(insulin=0.2, automatic=True)]
            )
            assert await client.get_latest_manual_bolus(STAMP) is None
            assert api_cls.return_value.bolus_get_all.call_args.kwargs["var_from"] == STAMP

    async def test_default_lookback_is_two_days(self, client):
        with patch("custom_components.nocturne.api.BolusApi") as api_cls:
            api_cls.return_value.bolus_get_all.return_value = BolusPaginatedResponse(data=[])
            await client.get_latest_manual_bolus()
            start = api_cls.return_value.bolus_get_all.call_args.kwargs["var_from"]
        assert timedelta(hours=47) < datetime.now(UTC) - start < timedelta(hours=49)


class TestApiTempBasals:
    async def test_returns_the_doses_since_the_start(self, client):
        doses = [TempBasal(rate=0.3), TempBasal(rate=0.9)]
        with patch("custom_components.nocturne.api.TempBasalApi") as api_cls:
            api_cls.return_value.temp_basal_get_all.return_value = TempBasalPaginatedResponse(data=doses)
            assert await client.get_temp_basals_since(STAMP) == doses
            kwargs = api_cls.return_value.temp_basal_get_all.call_args.kwargs
        assert kwargs == {"var_from": STAMP, "limit": TEMP_BASAL_PAGE_SIZE, "sort": "timestamp_desc"}


class TestApiPumpAlert:
    async def test_skips_the_generic_alarm_snapshot_copy(self, client):
        message = SystemEvent(description="BC_SID_HIGH_SG_CHECK_BG")
        with patch("custom_components.nocturne.api.SystemEventsApi") as api_cls:
            api_cls.return_value.system_events_get_system_events.return_value = [
                SystemEvent(description="ALERT"),
                SystemEvent(description=""),
                message,
            ]
            assert await client.get_latest_pump_alert() is message


def _wire_response(status: int, body: object) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.reason = "OK" if status == 200 else "Error"
    resp.data = json.dumps(body).encode()
    resp.headers = {}
    resp.getheaders.return_value = {}
    return resp


class TestApiCgmStatus:
    """Goes through the real SDK request path, since this call is not a generated method."""

    async def _get(self, client, status, body):
        captured: dict = {}

        def _request(self, method, url, headers=None, body=None, post_params=None, _request_timeout=None):
            captured.update(method=method, url=url, headers=headers)
            return _wire_response(status, response_body)

        response_body = body
        with patch("nocturne_py.rest.RESTClientObject.request", _request):
            result = await client.get_cgm_status()
        return result, captured

    async def test_reads_the_first_status_with_cgm_details(self, client):
        docs = [{"uploader": {"battery": 89}}, {**_cgm_status(), "pump": {}}]
        result, captured = await self._get(client, 200, docs)

        assert result == _cgm_status()
        assert captured["method"] == "GET"
        assert captured["url"] == "https://example.com/api/v1/devicestatus?count=5"
        assert captured["headers"]["Authorization"] == "Bearer test-token"

    async def test_none_without_cgm_details(self, client):
        result, _ = await self._get(client, 200, [{"uploader": {"battery": 89}}])
        assert result is None

    async def test_none_when_forbidden(self, client):
        result, _ = await self._get(client, 403, {"title": "Forbidden"})
        assert result is None
