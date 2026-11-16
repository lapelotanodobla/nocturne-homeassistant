"""Glucose delta and the recent readings behind the Sugar Matrix colour strip."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.util import dt as dt_util
from nocturne_py import SensorGlucose
from nocturne_py.models import PaginatedResponseOfSensorGlucose

from custom_components.nocturne.api import NocturneApiClient

from .conftest import make_api_client, make_entry, set_up

DELTA = "sensor.nocturne_glucose_delta"
RECENT = "sensor.nocturne_recent_glucose"


def _reading(mgdl: int, minutes_ago: float) -> SensorGlucose:
    mills = int((dt_util.utcnow() - timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return SensorGlucose(mgdl=mgdl, mills=mills)


async def _setup(hass, stubs, recent):
    entry = make_entry(expires_in=3600)
    client = make_api_client()
    client.get_recent_glucose.return_value = recent
    if recent:
        client.get_latest_glucose.return_value = recent[-1]
    stubs.clients[entry.data["instance_url"]] = client
    await set_up(hass, entry)
    return entry


class TestGlucoseDelta:
    async def test_delta_is_difference_of_two_newest(self, reauth_hass, stubs):
        await _setup(reauth_hass, stubs, [_reading(190, 5), _reading(186, 0)])
        assert reauth_hass.states.get(DELTA).state == "-4"

    async def test_delta_unavailable_across_gap(self, reauth_hass, stubs):
        await _setup(reauth_hass, stubs, [_reading(190, 20), _reading(186, 0)])
        assert reauth_hass.states.get(DELTA).state == STATE_UNAVAILABLE

    async def test_delta_unavailable_when_newest_is_old(self, reauth_hass, stubs):
        """Two old readings 5 min apart are not a current change."""
        await _setup(reauth_hass, stubs, [_reading(190, 45), _reading(186, 40)])
        assert reauth_hass.states.get(DELTA).state == STATE_UNAVAILABLE

    async def test_delta_unavailable_with_one_reading(self, reauth_hass, stubs):
        await _setup(reauth_hass, stubs, [_reading(186, 0)])
        assert reauth_hass.states.get(DELTA).state == STATE_UNAVAILABLE


class TestRecentGlucose:
    async def test_recent_glucose_attribute(self, reauth_hass, stubs):
        readings = [_reading(190, 10), _reading(188, 5), _reading(186, 0)]
        await _setup(reauth_hass, stubs, readings)

        state = reauth_hass.states.get(RECENT)
        assert state.state == "3"
        assert state.attributes["readings"] == [[r.mills // 1000, r.mgdl] for r in readings]

    async def test_push_appends_to_recent(self, reauth_hass, stubs):
        entry = await _setup(reauth_hass, stubs, [_reading(190, 10), _reading(188, 5)])
        newer = _reading(183, 0)

        entry.runtime_data.glucose_coordinator.async_handle_glucose_push(
            {"readings": [{"mgdl": newer.mgdl, "mills": newer.mills}]}
        )
        await reauth_hass.async_block_till_done()

        assert reauth_hass.states.get(RECENT).state == "3"
        assert reauth_hass.states.get(DELTA).state == "-5"

    async def test_push_dedupes_recent(self, reauth_hass, stubs):
        readings = [_reading(190, 5), _reading(188, 0)]
        entry = await _setup(reauth_hass, stubs, readings)

        entry.runtime_data.glucose_coordinator.async_handle_glucose_push(
            {"readings": [{"mgdl": 188, "mills": readings[-1].mills}]}
        )
        await reauth_hass.async_block_till_done()

        assert reauth_hass.states.get(RECENT).state == "2"

    async def test_recent_window_trims(self, reauth_hass, stubs):
        await _setup(reauth_hass, stubs, [_reading(200, 170), _reading(190, 5), _reading(188, 0)])
        assert reauth_hass.states.get(RECENT).state == "2"


@pytest.fixture
def client() -> NocturneApiClient:
    return NocturneApiClient("https://example.com", AsyncMock(return_value="test-token"))


async def test_get_recent_glucose_pages_from_since(client):
    since = dt_util.utcnow() - timedelta(minutes=165)
    newest, older = _reading(186, 0), _reading(190, 5)
    with patch("custom_components.nocturne.api.SensorGlucoseApi") as api_cls:
        api_cls.return_value.sensor_glucose_get_all.return_value = PaginatedResponseOfSensorGlucose(
            data=[newest, older]
        )
        result = await client.get_recent_glucose(since)
        kwargs = api_cls.return_value.sensor_glucose_get_all.call_args.kwargs

    assert result == [older, newest]
    assert kwargs["var_from"] == since
    assert kwargs["sort"] == "timestamp_desc"
