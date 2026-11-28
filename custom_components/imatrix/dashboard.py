"""The iMatrix dashboard: sensors in the iMatrix order (spec 856 FR-25, D4).

Home Assistant sorts a device's entities by name on its own pages. This module
serves a dashboard strategy, `strategy: {type: custom:imatrix}`, that asks the
integration for each device's visible sensors in the iMatrix display order every
time the dashboard loads, so a change made in iMatrix shows on the next load.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import probatio as vol
from homeassistant.components import frontend, websocket_api
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.components.websocket_api.decorators import websocket_command
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .const import CONF_FOLLOW_IMATRIX, DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.websocket_api.connection import ActiveConnection
    from homeassistant.core import HomeAssistant

STRATEGY_JS_URL = "/imatrix_static/imatrix-dashboard-strategy.js"


@callback
def device_layout(
    hass: HomeAssistant, entry_id: str | None = None
) -> list[dict[str, Any]]:
    """Each loaded device with its enabled, visible sensor entities in iMatrix order.

    Devices are sorted by area, then name. A device with nothing to show is left out.
    """
    from .sensor import channel_unique_id, display_order  # noqa: PLC0415

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    out: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry_id is not None and entry.entry_id != entry_id:
            continue
        snapshot = entry.runtime_data.coordinator.data
        follow = entry.options.get(CONF_FOLLOW_IMATRIX, True)
        for device in snapshot.devices.values():
            entities: list[str] = []
            for channel in display_order(device):
                if follow and not channel.visible:
                    continue
                entity_id = ent_reg.async_get_entity_id(
                    "sensor", DOMAIN, channel_unique_id(device.sn, channel)
                )
                reg = ent_reg.async_get(entity_id) if entity_id else None
                if (
                    reg is None
                    or reg.disabled_by is not None
                    or reg.hidden_by is not None
                ):
                    continue
                entities.append(reg.entity_id)
            if not entities:
                continue
            ha_device = dev_reg.async_get_device(identifiers={(DOMAIN, str(device.sn))})
            area = (
                area_reg.async_get_area(ha_device.area_id)
                if ha_device is not None and ha_device.area_id
                else None
            )
            name = (
                ((ha_device.name_by_user or ha_device.name) if ha_device else None)
                or device.name
                or f"iMatrix {device.sn}"
            )
            out.append(
                {
                    "sn": device.sn,
                    "name": name,
                    "area": area.name if area else None,
                    "entities": entities,
                }
            )
    out.sort(
        key=lambda d: (d["area"] is None, (d["area"] or "").lower(), d["name"].lower())
    )
    return out


@websocket_command(
    {
        vol.Required("type"): "imatrix/device_layout",
        vol.Optional("config_entry_id"): str,
    }
)
@callback
def ws_device_layout(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer the dashboard strategy. Any signed-in user already sees these states."""
    connection.send_result(
        msg["id"], {"devices": device_layout(hass, msg.get("config_entry_id"))}
    )


async def async_register_dashboard(hass: HomeAssistant) -> None:
    """Serve the strategy script and its websocket command."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                STRATEGY_JS_URL,
                str(Path(__file__).parent / "www" / "imatrix-dashboard-strategy.js"),
                True,
            )
        ]
    )
    websocket_api.async_register_command(hass, ws_device_layout)
    if frontend.DATA_EXTRA_MODULE_URL in hass.data:
        version = (await async_get_integration(hass, DOMAIN)).version
        frontend.add_extra_js_url(hass, f"{STRATEGY_JS_URL}?v={version}")
