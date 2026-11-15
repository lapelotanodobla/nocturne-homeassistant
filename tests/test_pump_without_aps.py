"""Sensors for a pump that runs its own algorithm and leaves no APS snapshot (CareLink, Tandem)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.util import dt as dt_util
from nocturne_py import PumpSnapshot, StateSpan, TempBasal
from nocturne_py.models import V4SummaryResponse

from .conftest import make_api_client, make_entry, set_up


def _client():
    client = make_api_client()
    client.get_latest_aps_snapshot.return_value = None
    return client


class TestPumpWithoutAps:
    async def test_iob_and_cob_come_from_the_summary(self, reauth_hass, stubs):
        entry = make_entry(expires_in=3600)
        client = _client()
        client.get_summary.return_value = V4SummaryResponse(iob=0.86, cob=10)
        stubs.clients[entry.data["instance_url"]] = client
        await set_up(reauth_hass, entry)

        assert reauth_hass.states.get("sensor.nocturne_insulin_on_board").state == "0.86"
        assert reauth_hass.states.get("sensor.nocturne_carbs_on_board").state == "10"

    async def test_basal_rate_is_the_running_auto_basal(self, reauth_hass, stubs):
        entry = make_entry(expires_in=3600)
        client = _client()
        now = dt_util.utcnow()
        client.get_latest_temp_basal.return_value = TempBasal(
            rate=0.9, start_timestamp=now - timedelta(minutes=10), end_timestamp=now - timedelta(minutes=5)
        )
        stubs.clients[entry.data["instance_url"]] = client
        await set_up(reauth_hass, entry)

        assert reauth_hass.states.get("sensor.nocturne_active_basal_rate").state == "0.9"

    async def test_a_stale_basal_is_not_shown_as_running(self, reauth_hass, stubs):
        entry = make_entry(expires_in=3600)
        client = _client()
        now = dt_util.utcnow()
        client.get_latest_temp_basal.return_value = TempBasal(
            rate=0.9, start_timestamp=now - timedelta(hours=2), end_timestamp=now - timedelta(hours=1)
        )
        stubs.clients[entry.data["instance_url"]] = client
        await set_up(reauth_hass, entry)

        assert reauth_hass.states.get("sensor.nocturne_active_basal_rate").state == STATE_UNAVAILABLE

    async def test_pump_mode(self, reauth_hass, stubs):
        entry = make_entry(expires_in=3600)
        client = _client()
        client.get_latest_pump_snapshot.return_value = PumpSnapshot(pump_mode="Automatic")
        stubs.clients[entry.data["instance_url"]] = client
        await set_up(reauth_hass, entry)

        assert reauth_hass.states.get("sensor.nocturne_pump_mode").state == "Automatic"


class TestTempTarget:
    async def _set_up(self, hass, stubs, span):
        entry = make_entry(expires_in=3600)
        client = _client()
        client.get_latest_temp_target.return_value = span
        stubs.clients[entry.data["instance_url"]] = client
        await set_up(hass, entry)
        return hass.states.get("binary_sensor.nocturne_temp_target")

    async def test_off_without_a_span(self, reauth_hass, stubs):
        state = await self._set_up(reauth_hass, stubs, None)
        assert state.state == STATE_OFF

    async def test_on_while_the_span_runs(self, reauth_hass, stubs):
        now = dt_util.utcnow()
        state = await self._set_up(
            reauth_hass,
            stubs,
            StateSpan(
                start_timestamp=now - timedelta(minutes=20),
                end_timestamp=now + timedelta(minutes=40),
                metadata={"targetTop": 150, "targetBottom": 150, "units": "mg/dl"},
            ),
        )
        assert state.state == STATE_ON
        assert state.attributes["target_low"] == 150
        assert state.attributes["target_high"] == 150
        assert state.attributes["ends"] == now + timedelta(minutes=40)

    async def test_off_once_the_span_has_ended(self, reauth_hass, stubs):
        now = dt_util.utcnow()
        state = await self._set_up(
            reauth_hass,
            stubs,
            StateSpan(start_timestamp=now - timedelta(hours=2), end_timestamp=now - timedelta(hours=1)),
        )
        assert state.state == STATE_OFF
        assert "ends" not in state.attributes
