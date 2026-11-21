"""Sensor platform for Nocturne."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from . import NocturneConfigEntry
from .coordinator import GlucoseCoordinator, reading_epoch
from .device import CGM, HUB, PUMP, UPLOADER, device_info

# The longest gap between two readings that still gives a change between them.
DELTA_MAX_GAP_SECONDS = 15 * 60
# The oldest the newest reading may be for its change to count as current.
DELTA_MAX_AGE_SECONDS = 20 * 60


def _glucose_delta(d: dict[str, Any]) -> int | None:
    """The newest reading minus the one before it, unknown across a gap or once it is old."""
    recent = d.get("recent_glucose") or []
    if len(recent) < 2:
        return None
    previous, latest = recent[-2], recent[-1]
    gap = (reading_epoch(latest) or 0) - (reading_epoch(previous) or 0)
    if not 0 < gap <= DELTA_MAX_GAP_SECONDS:
        return None
    if dt_util.utcnow().timestamp() - (reading_epoch(latest) or 0) > DELTA_MAX_AGE_SECONDS:
        return None
    return round(latest.mgdl - previous.mgdl)


# Auto basal is summed over this window, ending at the pump snapshot the doses arrived with.
AUTO_BASAL_WINDOW = timedelta(hours=1)
# A pump snapshot older than this means the pump's data has stopped arriving: CareLink
# uploads every 5 minutes and is seen up to 10 minutes apart.
PUMP_DATA_MAX_AGE = timedelta(minutes=15)


def _iob(d: dict[str, Any]) -> float | None:
    if d.get("aps"):
        return d["aps"].iob
    return d["summary"].iob if d.get("summary") else None


def _cob(d: dict[str, Any]) -> float | None:
    if d.get("aps"):
        return d["aps"].cob
    return d["summary"].cob if d.get("summary") else None


def _basal_rate(d: dict[str, Any]) -> float | None:
    return d["aps"].enacted_rate if d.get("aps") else None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt_util.UTC)


def _auto_basal_window_end(d: dict[str, Any]) -> datetime | None:
    """The time the pump's auto-basal doses are known up to, if they are current.

    Only an automatic-mode pump doses this way, and only a recent snapshot proves that
    no dose is missing from the window.
    """
    pump = d.get("pump")
    if pump is None or pump.pump_mode != "Automatic" or pump.timestamp is None:
        return None
    as_of = _utc(pump.timestamp)
    return as_of if dt_util.utcnow() - as_of <= PUMP_DATA_MAX_AGE else None


def _auto_basal_last_hour(d: dict[str, Any]) -> float | None:
    """Units the pump delivered as auto basal in the hour up to its latest snapshot.

    Nocturne stores each auto-basal dose as a rate over the time until the next dose, so
    rate times duration gives the dose back. A dose without both makes the total unknown
    rather than partial.
    """
    end = _auto_basal_window_end(d)
    if end is None or d.get("temp_basals") is None:
        return None
    start = end - AUTO_BASAL_WINDOW
    total = 0.0
    for basal in d["temp_basals"]:
        if basal.start_timestamp is None:
            return None
        if not start < _utc(basal.start_timestamp) <= end:
            continue
        if basal.rate is None or basal.end_timestamp is None:
            return None
        hours = (_utc(basal.end_timestamp) - _utc(basal.start_timestamp)).total_seconds() / 3600
        total += basal.rate * hours
    return round(total, 3)



# Readable names for the CareLink alert messages seen so far; others fall back to
# their code, made readable by _humanize.
PUMP_ALERT_MESSAGES = {
    "BC_SID_SG_APPROACH_LOW_LIMIT_CHECK_BG": "Alert before low",
    "BC_SID_LOW_SD_CHECK_BG": "Alert on low",
    "BC_MESSAGE_SG_UNDER_50_MG_DL": "Glucose under 50 mg/dL",
    "BC_SID_HIGH_SG_CHECK_BG": "Alert on high",
    "BC_SID_MOVE_PUMP_CLOSER_TO_MINILINK": "Lost sensor signal: move pump closer to transmitter",
    "BC_SID_MOVE_AWAY_FROM_ELECTR_DEVICES": "Lost sensor signal: move away from electronic devices",
    "BC_SID_BOLUS_ENTRY_TIMED_OUT": "Bolus entry timed out",
    "BC_MESSAGE_TIME_REMAINING_CHANGE_RESERVOIR": "Low reservoir",
    "BC_SID_CHECK_BG_CONSIDER_TESTING_KETONES_CHANGE_RESERVOIR": "Insulin flow blocked: check BG, consider ketones",
    "BC_SID_ENTER_BG_TO_CONTINUE_IN_SMART_GUARD": "BG required to stay in SmartGuard",
    "BC_SID_SMART_GUARD_MAXIMUM_DELIVERY": "SmartGuard maximum delivery",
    "BC_SID_SMART_GUARD_MINIMUM_DELIVERY": "SmartGuard minimum delivery",
    "BC_SID_BASAL_STARTED_SMART_GUARD": "SmartGuard exited: basal started",
    "BC_SID_REPLACE_BATTERY_SOON": "Replace pump battery soon",
    "BC_SID_INSERT_NEW_AA_BATTERY": "Insert new AA battery",
    "BC_SID_DELIVERY_STOPPED_INSERT_NEW_BATTERY": "Delivery stopped: insert new battery",
    "BC_SID_IF_NEW_SENSR_SELCT_START_NEW_ELSE_REWIND": "Sensor connected",
    "BC_SID_INSERT_NEW_SENSOR": "Change sensor",
    "BC_SID_INSERT_AND_START_NEW_SENSOR": "Insert and start a new sensor",
    "BC_SID_SENSOR_RELATED_ISSUE_INSERT_NEW": "Sensor issue: insert a new sensor",
    "BC_SID_ENTER_BG_TO_CALIBRATE_SENSOR": "Calibration required",
    "BC_SID_ENTER_BG_TO_CALIBRATE_SENSOR_SENSOR_INFO_NO_AVAILABLE": "Calibration required",
    "BC_SID_WAIT_AT_LEAST_15_MINUTES": "Calibration not accepted: wait 15 minutes",
    "BC_SID_ENSURE_CONNECTION_SECURE": "Sensor connection: check transmitter",
    "BC_SID_SENSOR_INFO_UNAVAILABLE_FOR_UP_TO_TWO_HOURS": "Sensor updating (up to 2 hours)",
    "BC_SID_UPDATING_CAN_TAKE_UP_TO_NINETY_MINUTES": "Sensor updating (up to 90 minutes)",
}

SENSOR_STATES = {
    "NO_ERROR_MESSAGE": "OK",
    "WARM_UP": "Warming up",
    "CALIBRATING": "Calibrating",
    "CALIBRATION_REQUIRED": "Calibration required",
    "WAIT_TO_CALIBRATE": "Wait to calibrate",
    "DO_NOT_CALIBRATE": "Do not calibrate",
    "CHANGE_SENSOR": "Change sensor",
    "SENSOR_DISCONNECTED": "Sensor disconnected",
    "SEARCHING_FOR_SENSOR_SIGNAL": "Searching for signal",
    "NO_SENSOR_SIGNAL": "No sensor signal",
    "NO_DATA_FROM_PUMP": "No data from pump",
    "SG_BELOW_40_MGDL": "Below 40 mg/dL",
}


def _humanize(code: str) -> str:
    for prefix in ("BC_SID_", "BC_MESSAGE_", "BC_"):
        code = code.removeprefix(prefix)
    return code.replace("_", " ").capitalize()


def _alert_message(d: dict[str, Any]) -> str | None:
    alert = d.get("last_alert")
    if alert is None or not alert.description:
        return None
    return PUMP_ALERT_MESSAGES.get(alert.description) or _humanize(alert.description)


def _alert_attributes(d: dict[str, Any]) -> dict[str, Any]:
    alert = d.get("last_alert")
    if alert is None:
        return {}
    return {
        "time": dt_util.utc_from_timestamp(alert.mills / 1000) if alert.mills else None,
        "type": alert.event_type.value if alert.event_type else None,
        "code": alert.description,
    }


def _cgm(d: dict[str, Any]) -> dict[str, Any]:
    status = d.get("cgm")
    return status["cgm"] if status else {}


def _transmitter_battery(d: dict[str, Any]) -> int | None:
    # Out of range, CareLink keeps sending the last or a placeholder battery level.
    status = d.get("cgm")
    if not status or status["connect"].get("conduitSensorInRange") is not True:
        return None
    return status["cgm"].get("transmitterBattery")


def _sensor_state(d: dict[str, Any]) -> str | None:
    state = _cgm(d).get("sensorState")
    if not state or state == "UNKNOWN":
        return None
    return SENSOR_STATES.get(state) or _humanize(state)


# Nocturne's CareLink connector reports why there is, or is not, a current reading
# (connect.sensorStatus), so a planned gap (no sensor on, warm-up) reads differently from a fault.
CGM_STATUSES = {
    "Ok": "OK",
    "WarmingUp": "Warming up",
    "NoSensor": "No sensor",
    "Connecting": "Connecting",
    "SensorOutOfRange": "Sensor out of range",
    "PumpOutOfReach": "Pump out of reach",
}


def _cgm_status(d: dict[str, Any]) -> str | None:
    status = d.get("cgm")
    if not status:
        return None
    return CGM_STATUSES.get(status["connect"].get("sensorStatus"))


def _time_attribute(d: dict[str, Any], key: str) -> dict[str, Any]:
    record = d.get(key)
    return {"time": record.timestamp} if record is not None else {}



@dataclass(frozen=True, kw_only=True)
class NocturneSensorDescription(SensorEntityDescription):
    """Describes a Nocturne sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    available_fn: Callable[[dict[str, Any]], bool]
    coordinator_key: str  # "glucose" or "device"
    device: str = HUB
    enum_options: list[str] | None = None
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


GLUCOSE_SENSORS: list[NocturneSensorDescription] = [
    NocturneSensorDescription(
        key="current_glucose",
        translation_key="current_glucose",
        device_class=SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION,
        native_unit_of_measurement="mg/dL",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:diabetes",
        coordinator_key="glucose",
        value_fn=lambda d: d["glucose"].mgdl if d.get("glucose") else None,
        available_fn=lambda d: d.get("glucose") is not None,
    ),
    NocturneSensorDescription(
        key="glucose_trend",
        translation_key="glucose_trend",
        device_class=SensorDeviceClass.ENUM,
        icon="mdi:trending-up",
        coordinator_key="glucose",
        value_fn=lambda d: d["glucose"].direction.value if d.get("glucose") and d["glucose"].direction else None,
        available_fn=lambda d: d.get("glucose") is not None and d["glucose"].direction is not None,
        enum_options=[
            "None", "DoubleUp", "SingleUp", "FortyFiveUp", "Flat",
            "FortyFiveDown", "SingleDown", "DoubleDown",
            "NotComputable", "RateOutOfRange",
        ],
    ),
    NocturneSensorDescription(
        key="glucose_delta",
        translation_key="glucose_delta",
        native_unit_of_measurement="mg/dL",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:delta",
        coordinator_key="glucose",
        value_fn=_glucose_delta,
        available_fn=lambda d: _glucose_delta(d) is not None,
    ),
    NocturneSensorDescription(
        key="iob",
        translation_key="iob",
        native_unit_of_measurement="U",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:needle",
        coordinator_key="glucose",
        value_fn=_iob,
        available_fn=lambda d: _iob(d) is not None,
    ),
    NocturneSensorDescription(
        key="cob",
        translation_key="cob",
        native_unit_of_measurement="g",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:food-apple",
        coordinator_key="glucose",
        value_fn=_cob,
        available_fn=lambda d: _cob(d) is not None,
    ),
    NocturneSensorDescription(
        key="predicted_bg",
        translation_key="predicted_bg",
        device_class=SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION,
        native_unit_of_measurement="mg/dL",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:crystal-ball",
        coordinator_key="glucose",
        value_fn=lambda d: d["aps"].eventual_bg if d.get("aps") else None,
        available_fn=lambda d: d.get("aps") is not None and d["aps"].eventual_bg is not None,
    ),
    NocturneSensorDescription(
        key="loop_status",
        translation_key="loop_status",
        device_class=SensorDeviceClass.ENUM,
        icon="mdi:sync",
        coordinator_key="glucose",
        enum_options=["enacted", "open", "unknown"],
        value_fn=lambda d: (
            "enacted" if d.get("aps") and d["aps"].enacted
            else ("open" if d.get("aps") else "unknown")
        ),
        available_fn=lambda d: d.get("aps") is not None,
    ),
    NocturneSensorDescription(
        key="active_basal_rate",
        translation_key="active_basal_rate",
        native_unit_of_measurement="U/hr",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:iv-bag",
        coordinator_key="glucose",
        value_fn=_basal_rate,
        available_fn=lambda d: _basal_rate(d) is not None,
    ),
    NocturneSensorDescription(
        key="auto_basal_last_hour",
        translation_key="auto_basal_last_hour",
        native_unit_of_measurement="U",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:iv-bag",
        coordinator_key="glucose",
        device=PUMP,
        value_fn=_auto_basal_last_hour,
        available_fn=lambda d: _auto_basal_last_hour(d) is not None,
        attributes_fn=lambda d: {"as_of": _auto_basal_window_end(d)},
    ),
    NocturneSensorDescription(
        key="last_bolus",
        translation_key="last_bolus",
        native_unit_of_measurement="U",
        suggested_display_precision=2,
        icon="mdi:needle",
        coordinator_key="glucose",
        device=PUMP,
        value_fn=lambda d: d["last_bolus"].insulin if d.get("last_bolus") else None,
        available_fn=lambda d: d.get("last_bolus") is not None and d["last_bolus"].insulin is not None,
        attributes_fn=lambda d: _time_attribute(d, "last_bolus"),
    ),
    NocturneSensorDescription(
        key="last_carbs",
        translation_key="last_carbs",
        native_unit_of_measurement="g",
        suggested_display_precision=0,
        icon="mdi:food-apple",
        coordinator_key="glucose",
        value_fn=lambda d: d["last_carbs"].carbs if d.get("last_carbs") else None,
        available_fn=lambda d: d.get("last_carbs") is not None and d["last_carbs"].carbs is not None,
        attributes_fn=lambda d: _time_attribute(d, "last_carbs"),
    ),
    NocturneSensorDescription(
        key="last_pump_alert",
        translation_key="last_pump_alert",
        icon="mdi:alert-circle-outline",
        coordinator_key="glucose",
        device=PUMP,
        value_fn=_alert_message,
        available_fn=lambda d: _alert_message(d) is not None,
        attributes_fn=_alert_attributes,
    ),
]

DEVICE_SENSORS: list[NocturneSensorDescription] = [
    NocturneSensorDescription(
        key="pump_reservoir",
        translation_key="pump_reservoir",
        native_unit_of_measurement="U",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:beaker",
        coordinator_key="device",
        device=PUMP,
        value_fn=lambda d: d["pump"].reservoir if d.get("pump") else None,
        available_fn=lambda d: d.get("pump") is not None and d["pump"].reservoir is not None,
    ),
    NocturneSensorDescription(
        key="pump_battery",
        translation_key="pump_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        coordinator_key="device",
        device=PUMP,
        value_fn=lambda d: d["pump"].battery_percent if d.get("pump") else None,
        available_fn=lambda d: d.get("pump") is not None and d["pump"].battery_percent is not None,
    ),
    NocturneSensorDescription(
        key="pump_mode",
        translation_key="pump_mode",
        icon="mdi:auto-mode",
        coordinator_key="device",
        device=PUMP,
        value_fn=lambda d: d["pump"].pump_mode if d.get("pump") else None,
        available_fn=lambda d: d.get("pump") is not None and d["pump"].pump_mode is not None,
    ),
    NocturneSensorDescription(
        key="cgm_battery",
        translation_key="cgm_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        coordinator_key="device",
        device=UPLOADER,
        value_fn=lambda d: d["uploader"].battery if d.get("uploader") else None,
        available_fn=lambda d: d.get("uploader") is not None and d["uploader"].battery is not None,
    ),
    NocturneSensorDescription(
        key="sensor_age",
        translation_key="sensor_age",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-sand",
        coordinator_key="device",
        device=CGM,
        value_fn=lambda d: d["sensor_age"].days if d.get("sensor_age") else None,
        # Without a recorded sensor start the server still answers, with found false and 0 days.
        available_fn=lambda d: d.get("sensor_age") is not None
        and d["sensor_age"].found is True
        and d["sensor_age"].days is not None,
    ),
    NocturneSensorDescription(
        key="time_in_range",
        translation_key="time_in_range",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:target",
        coordinator_key="device",
        value_fn=lambda d: d["daily_summary"].time_in_range_percent if d.get("daily_summary") else None,
        available_fn=lambda d: d.get("daily_summary") is not None and d["daily_summary"].time_in_range_percent is not None,
    ),
    NocturneSensorDescription(
        key="transmitter_battery",
        translation_key="transmitter_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        coordinator_key="device",
        device=CGM,
        value_fn=_transmitter_battery,
        available_fn=lambda d: _transmitter_battery(d) is not None,
    ),
    NocturneSensorDescription(
        key="sensor_state",
        translation_key="sensor_state",
        icon="mdi:access-point",
        coordinator_key="device",
        device=CGM,
        value_fn=_sensor_state,
        available_fn=lambda d: _sensor_state(d) is not None,
    ),
    NocturneSensorDescription(
        key="cgm_status",
        translation_key="cgm_status",
        icon="mdi:access-point-check",
        coordinator_key="device",
        device=CGM,
        value_fn=_cgm_status,
        available_fn=lambda d: _cgm_status(d) is not None,
    ),
]

ALL_SENSORS = GLUCOSE_SENSORS + DEVICE_SENSORS


class NocturneSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Nocturne sensor."""

    entity_description: NocturneSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: NocturneSensorDescription,
        entry_id: str,
        device_data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        if description.enum_options is not None:
            self._attr_options = description.enum_options
        self._attr_device_info = device_info(entry_id, description.device, device_data)

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        if not super().available or self.coordinator.data is None:
            return False
        return self.entity_description.available_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None or self.coordinator.data is None:
            return None
        return attributes_fn(self.coordinator.data)


class NocturneRecentGlucoseSensor(CoordinatorEntity[GlucoseCoordinator], SensorEntity):
    """The readings of the last few hours, oldest first, for displays that draw a history strip.

    The state is how many readings there are; the readings are an attribute kept out of the
    recorder, since they repeat on every new reading.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "recent_glucose"
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"readings"})

    def __init__(self, coordinator: GlucoseCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_recent_glucose"
        self._attr_device_info = device_info(entry_id, HUB)

    @property
    def _recent(self) -> list:
        return (self.coordinator.data or {}).get("recent_glucose") or []

    @property
    def native_value(self) -> int:
        return len(self._recent)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"readings": [[reading_epoch(r), round(r.mgdl)] for r in self._recent]}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NocturneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up every Nocturne sensor; each stays unavailable until its data arrives."""
    data = entry.runtime_data
    async_add_entities([NocturneRecentGlucoseSensor(data.glucose_coordinator, entry.entry_id)])
    async_add_entities(
        NocturneSensor(
            data.glucose_coordinator
            if desc.coordinator_key == "glucose"
            else data.device_coordinator,
            desc,
            entry.entry_id,
            data.device_coordinator.data,
        )
        for desc in ALL_SENSORS
    )
