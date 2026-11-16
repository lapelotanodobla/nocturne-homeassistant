"""The Home Assistant devices one Nocturne entry is split into.

Each physical device gets its own HA device, so a device page shows that device's
battery rather than whichever battery sensor happens to be listed first.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

HUB = "hub"
PUMP = "pump"
CGM = "cgm"
UPLOADER = "uploader"

_NAMES = {PUMP: "Nocturne Pump", CGM: "Nocturne CGM", UPLOADER: "Nocturne Uploader"}


def device_info(entry_id: str, kind: str, device_data: dict[str, Any] | None = None) -> DeviceInfo:
    """Describe the HA device of ``kind``, naming the hardware when Nocturne reports it."""
    if kind == HUB:
        return DeviceInfo(identifiers={(DOMAIN, entry_id)}, name="Nocturne", manufacturer="Nocturne")

    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{kind}")},
        name=_NAMES[kind],
        via_device=(DOMAIN, entry_id),
    )
    data = device_data or {}
    hardware: dict[str, Any] = {}
    if kind == PUMP and data.get("pump") is not None:
        hardware = {"manufacturer": data["pump"].manufacturer, "model": data["pump"].model}
    elif kind == CGM and data.get("cgm") is not None:
        cgm = data["cgm"]["cgm"]
        hardware = {"manufacturer": cgm.get("manufacturer"), "model": cgm.get("model")}
    for key, value in hardware.items():
        if value:
            info[key] = value
    return info
