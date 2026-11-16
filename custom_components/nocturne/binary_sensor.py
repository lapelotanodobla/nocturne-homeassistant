"""Binary sensor platform for Nocturne."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from nocturne_py import StateSpan

from . import NocturneConfigEntry
from .coordinator import DeviceCoordinator, GlucoseCoordinator
from .device import HUB, PUMP, device_info


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=dt_util.UTC)


class NocturneTempTargetSensor(CoordinatorEntity[GlucoseCoordinator], BinarySensorEntity):
    """On while the newest temporary target span covers the present.

    Its end is planned ahead, so the state is decided against the clock at each refresh
    rather than by an open-ended span.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "temp_target"
    _attr_icon = "mdi:run"

    def __init__(self, coordinator: GlucoseCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_temp_target"
        self._attr_device_info = device_info(entry_id, HUB)

    @property
    def _span(self) -> StateSpan | None:
        return (self.coordinator.data or {}).get("temp_target")

    @property
    def is_on(self) -> bool:
        span = self._span
        if span is None or span.start_timestamp is None:
            return False
        now = dt_util.utcnow()
        end = _aware(span.end_timestamp)
        return _aware(span.start_timestamp) <= now and (end is None or now < end)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        span = self._span
        if span is None or not self.is_on:
            return {}
        metadata = span.metadata or {}
        return {
            "started": _aware(span.start_timestamp),
            "ends": _aware(span.end_timestamp),
            "target_low": metadata.get("targetBottom"),
            "target_high": metadata.get("targetTop"),
            "units": metadata.get("units"),
        }


class NocturnePumpSuspendedSensor(CoordinatorEntity[DeviceCoordinator], BinarySensorEntity):
    """On while the latest pump snapshot reports insulin delivery suspended."""

    _attr_has_entity_name = True
    _attr_translation_key = "pump_suspended"
    _attr_icon = "mdi:pause-octagon-outline"

    def __init__(self, coordinator: DeviceCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_pump_suspended"
        self._attr_device_info = device_info(entry_id, PUMP, coordinator.data)

    @property
    def _suspended(self) -> bool | None:
        pump = (self.coordinator.data or {}).get("pump")
        return pump.suspended if pump is not None else None

    @property
    def available(self) -> bool:
        return super().available and self._suspended is not None

    @property
    def is_on(self) -> bool | None:
        return self._suspended


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NocturneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the temporary target and pump suspended sensors."""
    data = entry.runtime_data
    async_add_entities(
        [
            NocturneTempTargetSensor(data.glucose_coordinator, entry.entry_id),
            NocturnePumpSuspendedSensor(data.device_coordinator, entry.entry_id),
        ]
    )
