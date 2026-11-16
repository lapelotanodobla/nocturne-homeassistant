"""Sensors for a pump that runs its own algorithm and leaves no APS snapshot (CareLink, Tandem)."""

from __future__ import annotations

from datetime import timedelta

import pytest
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

    async def test_no_active_basal_rate_without_an_aps(self, reauth_hass, stubs):
        entry = make_entry(expires_in=3600)
        stubs.clients[entry.data["instance_url"]] = _client()
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


def _dose(start, minutes, rate):
    return TempBasal(rate=rate, start_timestamp=start, end_timestamp=start + timedelta(minutes=minutes))


class TestAutoBasalLastHour:
    """Sum of the pump's own auto-basal doses, never a provisional rate."""

    async def _state(self, hass, stubs, pump, basals):
        entry = make_entry(expires_in=3600)
        client = _client()
        client.get_latest_pump_snapshot.return_value = pump
        client.get_temp_basals_since.return_value = basals
        stubs.clients[entry.data["instance_url"]] = client
        await set_up(hass, entry)
        return hass.states.get("sensor.nocturne_pump_auto_basal_last_hour"), client

    async def test_sums_the_doses_in_the_hour_up_to_the_snapshot(self, reauth_hass, stubs):
        as_of = dt_util.utcnow() - timedelta(minutes=2)
        state, _ = await self._state(
            reauth_hass,
            stubs,
            PumpSnapshot(pump_mode="Automatic", timestamp=as_of),
            [
                # Provisional 5-minute rate on the newest dose still gives its real 0.025 U.
                _dose(as_of - timedelta(minutes=5), 5, 0.3),
                _dose(as_of - timedelta(minutes=10), 5, 0.9),
                # A dose followed by 20 minutes of nothing: Nocturne spreads it to 0.075 U/h.
                _dose(as_of - timedelta(minutes=30), 20, 0.075),
                _dose(as_of - timedelta(minutes=70), 5, 0.9),
            ],
        )
        assert state.state == "0.125"
        assert state.attributes["as_of"] == as_of

    async def test_zero_when_the_pump_gave_nothing(self, reauth_hass, stubs):
        as_of = dt_util.utcnow() - timedelta(minutes=2)
        state, _ = await self._state(
            reauth_hass,
            stubs,
            PumpSnapshot(pump_mode="Automatic", timestamp=as_of),
            [_dose(as_of - timedelta(minutes=90), 5, 0.9)],
        )
        assert float(state.state) == 0

    @pytest.mark.parametrize(
        ("mode", "age"),
        [
            ("Automatic", timedelta(minutes=20)),
            ("Manual", timedelta(minutes=2)),
            (None, timedelta(minutes=2)),
        ],
        ids=["stale pump data", "manual mode", "placeholder snapshot"],
    )
    async def test_unavailable_unless_current_and_automatic(self, reauth_hass, stubs, mode, age):
        as_of = dt_util.utcnow() - age
        state, _ = await self._state(
            reauth_hass,
            stubs,
            PumpSnapshot(pump_mode=mode, timestamp=as_of),
            [_dose(as_of - timedelta(minutes=5), 5, 0.3)],
        )
        assert state.state == STATE_UNAVAILABLE

    async def test_a_dose_of_unknown_size_makes_the_total_unknown(self, reauth_hass, stubs):
        as_of = dt_util.utcnow() - timedelta(minutes=2)
        state, _ = await self._state(
            reauth_hass,
            stubs,
            PumpSnapshot(pump_mode="Automatic", timestamp=as_of),
            [
                TempBasal(rate=0.3, start_timestamp=as_of - timedelta(minutes=5)),
                _dose(as_of - timedelta(minutes=10), 5, 0.9),
            ],
        )
        assert state.state == STATE_UNAVAILABLE

    async def test_fetches_two_hours_of_doses(self, reauth_hass, stubs):
        _, client = await self._state(
            reauth_hass, stubs, PumpSnapshot(pump_mode="Automatic", timestamp=dt_util.utcnow()), []
        )
        start = client.get_temp_basals_since.call_args.args[0]
        assert timedelta(minutes=119) < dt_util.utcnow() - start < timedelta(minutes=121)
