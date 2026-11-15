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
from .const import DOMAIN
from .coordinator import GlucoseCoordinator


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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Nocturne",
            "manufacturer": "Nocturne",
        }

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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NocturneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the temporary target sensor."""
    async_add_entities(
        [NocturneTempTargetSensor(entry.runtime_data.glucose_coordinator, entry.entry_id)]
    )
