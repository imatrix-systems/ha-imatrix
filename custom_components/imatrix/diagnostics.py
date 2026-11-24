"""Diagnostics for the iMatrix integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import IMatrixConfigEntry

TO_REDACT = {
    CONF_API_KEY,
    "key_prefix",
    "org_name",
    "title",
    "unique_id",
    "sn",
    "via_sn",
    "name",
    "group_path",
    "latitude",
    "longitude",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IMatrixConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics. Device sns are replaced by list positions."""
    data = entry.runtime_data
    snapshot = data.coordinator.data
    order = {sn: f"device_{i}" for i, sn in enumerate(sorted(snapshot.devices))}
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "account": async_redact_data(asdict(data.account), TO_REDACT),
        "coordinator": {
            "last_update_success": data.coordinator.last_update_success,
            "update_interval": str(data.coordinator.update_interval),
            "server_time": str(snapshot.server_time),
            "rate_limit_remaining": data.client.auth.rate_limit_remaining,
            "event_cursor": data.coordinator.event_cursor,
        },
        "stream": None
        if data.coordinator.push is None
        else {
            "state": data.coordinator.push.state,
            "reconnects": data.coordinator.push.stream.reconnects,
            "last_event_id": data.coordinator.push.stream.last_event_id,
        },
        "devices": {
            order[sn]: async_redact_data(asdict(device), TO_REDACT)
            for sn, device in snapshot.devices.items()
        },
        "states": {
            order[sn]: async_redact_data(asdict(state), TO_REDACT)
            for sn, state in snapshot.states.items()
            if sn in order
        },
    }
