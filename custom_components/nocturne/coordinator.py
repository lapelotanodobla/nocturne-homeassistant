"""Data update coordinators for Nocturne."""

from __future__ import annotations

import logging
from datetime import timedelta
from http import HTTPStatus
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from nocturne_py import ApiException, SensorGlucose

from .api import NocturneApiClient
from .const import DEVICE_UPDATE_INTERVAL_SECONDS, GLUCOSE_UPDATE_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

# The readings behind a 32-slot strip of 5-minute readings, with a slot of slack.
RECENT_GLUCOSE_WINDOW = timedelta(minutes=165)


def reading_epoch(reading: SensorGlucose) -> int | None:
    """Unix seconds of a reading, from its mills or its timestamp."""
    if reading.mills is not None:
        return reading.mills // 1000
    if reading.timestamp is not None:
        stamp = reading.timestamp
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt_util.UTC)
        return int(stamp.timestamp())
    return None


def merge_recent(existing: list[SensorGlucose], new: list[SensorGlucose]) -> list[SensorGlucose]:
    """Readings within the window, one per time, oldest first."""
    cutoff = int((dt_util.utcnow() - RECENT_GLUCOSE_WINDOW).timestamp())
    by_time: dict[int, SensorGlucose] = {}
    for reading in [*existing, *new]:
        epoch = reading_epoch(reading)
        if epoch is not None and epoch >= cutoff and reading.mgdl is not None:
            by_time[epoch] = reading
    return [by_time[epoch] for epoch in sorted(by_time)]


# Covers the hour that auto basal is summed over plus how far behind the pump's data can lag.
AUTO_BASAL_FETCH_WINDOW = timedelta(hours=2)


class NocturneCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls Nocturne and maps API and token refresh errors for both coordinators."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: NocturneApiClient,
        name: str,
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=name,
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self.client = client
        self._refresh_failed = False

    async def _async_fetch(self) -> dict[str, Any]:
        raise NotImplementedError

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._async_fetch()
        except ClientResponseError as err:
            if 400 <= err.status < 500 and err.status != HTTPStatus.TOO_MANY_REQUESTS:
                raise ConfigEntryAuthFailed("OAuth token refresh refused") from err
            raise UpdateFailed(f"Token endpoint error: {err.status}") from err
        except ApiException as err:
            if err.status == 401:
                if self._refresh_failed:
                    raise ConfigEntryAuthFailed(
                        "OAuth token expired"
                    ) from err
                self._refresh_failed = True
                raise UpdateFailed("Auth failed, will retry") from err
            raise UpdateFailed(f"API error: {err.status}") from err
        except Exception as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        self._refresh_failed = False
        return data


class GlucoseCoordinator(NocturneCoordinator):
    """Coordinator for glucose entries and APS snapshots (60s interval)."""

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, client: NocturneApiClient
    ) -> None:
        super().__init__(
            hass, config_entry, client, "Nocturne Glucose", GLUCOSE_UPDATE_INTERVAL_SECONDS
        )

    def async_handle_glucose_push(self, payload: Any) -> None:
        """Take the current reading from a SignalR glucose_reading.

        The push carries only new readings, oldest first, so the last is current.
        APS state and corrections to stored readings are not pushed, so the poll
        timer is left running: async_set_updated_data would restart it on every
        push, and a CGM that reports more often than the interval would starve it.
        """
        readings = payload.get("readings") if isinstance(payload, dict) else None
        if not readings or self.data is None:
            return
        pushed = [SensorGlucose.from_dict(reading) for reading in readings]
        self.data = {
            **self.data,
            "glucose": pushed[-1],
            "recent_glucose": merge_recent(self.data.get("recent_glucose") or [], pushed),
        }
        self.async_update_listeners()

    async def _async_fetch(self) -> dict[str, Any]:
        aps = await self.client.get_latest_aps_snapshot()
        previous_bolus = (self.data or {}).get("last_bolus")
        data = {
            "glucose": await self.client.get_latest_glucose(),
            "recent_glucose": merge_recent(
                [],
                await self.client.get_recent_glucose(dt_util.utcnow() - RECENT_GLUCOSE_WINDOW),
            ),
            "aps": aps,
            "summary": None,
            "pump": None,
            "temp_basals": None,
            "temp_target": await self.client.get_latest_temp_target(),
            # Searching from the last known manual bolus keeps the scan short when
            # automatic corrections pile up; an empty answer means no newer one.
            "last_bolus": await self.client.get_latest_manual_bolus(
                previous_bolus.timestamp if previous_bolus else None
            )
            or previous_bolus,
            "last_carbs": await self.client.get_latest_carbs(),
            "last_alert": await self.client.get_latest_pump_alert(),
        }
        if aps is None:
            # A pump that runs its own algorithm (CareLink, Tandem) leaves no APS snapshot:
            # IOB and COB come from what Nocturne computes, the basal from the pump's own
            # auto-basal doses, read against the pump snapshot that says how current they are.
            data["summary"] = await self.client.get_summary()
            data["pump"] = await self.client.get_latest_pump_snapshot()
            data["temp_basals"] = await self.client.get_temp_basals_since(
                dt_util.utcnow() - AUTO_BASAL_FETCH_WINDOW
            )
        return data


class DeviceCoordinator(NocturneCoordinator):
    """Coordinator for device status, profile, and reports (5m interval)."""

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, client: NocturneApiClient
    ) -> None:
        super().__init__(
            hass, config_entry, client, "Nocturne Device", DEVICE_UPDATE_INTERVAL_SECONDS
        )

    async def _async_fetch(self) -> dict[str, Any]:
        return {
            "pump": await self.client.get_latest_pump_snapshot(),
            "uploader": await self.client.get_latest_uploader_snapshot(),
            "profile": await self.client.get_profile_summary(),
            "daily_summary": await self.client.get_daily_summary(dt_util.now().date()),
            "sensor_age": await self.client.get_sensor_age(),
            "cgm": await self.client.get_cgm_status(),
        }
