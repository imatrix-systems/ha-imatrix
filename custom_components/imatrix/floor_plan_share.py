"""Opt-in sharing of the device list for floor plans (spec 822 FR-28 to FR-30).

Only these fields leave Home Assistant: device id, name, manufacturer, model, area id,
the integration that made the device, and the domains and device classes of its
entities. No entity ids, states, network or radio addresses, user names or config
entry data.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

MAX_DEVICES = 5000
ALLOWED_FIELDS = frozenset(
    {
        "haDeviceId",
        "name",
        "manufacturer",
        "model",
        "haAreaId",
        "integrationDomain",
        "categories",
    }
)


def build_device_list(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the device list the cloud accepts (FR-28), at most 5000 devices."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    out: list[dict[str, Any]] = []
    for device in list(dev_reg.devices.values()):  # type: ignore[attr-defined]
        if (
            device.disabled_by is not None
            or device.entry_type is dr.DeviceEntryType.SERVICE
        ):
            continue
        entries = er.async_entries_for_device(ent_reg, device.id)
        categories = sorted(
            {
                f"{e.domain}/{dc}"
                if (dc := e.device_class or e.original_device_class)
                else e.domain
                for e in entries
            }
        )
        # HA 2027 gives a device one config entry; older releases list several.
        entry_id = getattr(device, "config_entry_id", None) or next(
            iter(device.config_entries), None
        )
        config_entry = (
            hass.config_entries.async_get_entry(entry_id) if entry_id else None
        )
        out.append(
            {
                "haDeviceId": device.id,
                "name": (
                    device.name_by_user or device.name or device.model or device.id
                )[:255],
                "manufacturer": device.manufacturer,
                "model": device.model,
                "haAreaId": device.area_id,
                "integrationDomain": config_entry.domain if config_entry else "unknown",
                "categories": categories[:50],
            }
        )
        if len(out) >= MAX_DEVICES:
            break
    return out
