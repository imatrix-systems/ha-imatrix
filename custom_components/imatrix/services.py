"""Actions that return iMatrix data (trips, faults, history, floor plan dashboards)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import probatio as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from pyimatrix import Faults, History, IMatrixError, LatLng, Trip

from .const import DOMAIN
from .floor_plan_card import (
    CARD_HA_FLOORPLAN,
    CARD_PICTURE_ELEMENTS,
    async_dashboard,
)

if TYPE_CHECKING:
    from . import IMatrixConfigEntry

ATTR_START = "start"
ATTR_END = "end"

DEVICE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
TRIPS_SCHEMA = DEVICE_SCHEMA.extend(
    {vol.Optional(ATTR_START): cv.datetime, vol.Optional(ATTR_END): cv.datetime}
)
UUID_RE = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_CARD = "card"
ATTR_LEVEL_ID = "level_id"
ATTR_NEW_LINK = "new_link"
DASHBOARD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_CARD): vol.In([CARD_PICTURE_ELEMENTS, CARD_HA_FLOORPLAN]),
        vol.Optional(ATTR_LEVEL_ID): vol.All(cv.string, vol.Match(UUID_RE)),
        vol.Optional(ATTR_NEW_LINK, default=False): cv.boolean,
    }
)
HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
    }
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _latlng(value: LatLng | None) -> dict[str, float] | None:
    return None if value is None else {"lat": value.latitude, "lng": value.longitude}


def _loaded_entry(hass: HomeAssistant, entry_ids: set[str]) -> IMatrixConfigEntry:
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="entry_not_loaded"
                )
            return entry
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="device_not_found"
    )


def _resolve_device(
    hass: HomeAssistant, device_id: str
) -> tuple[IMatrixConfigEntry, int]:
    device = dr.async_get(hass).async_get(device_id)
    if device is not None:
        for domain, value in device.identifiers:
            if domain == DOMAIN and value.isdigit():
                return _loaded_entry(hass, device.config_entries), int(value)
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="device_not_found"
    )


def _resolve_channel(
    hass: HomeAssistant, entity_id: str
) -> tuple[IMatrixConfigEntry, int, int]:
    entity = er.async_get(hass).async_get(entity_id)
    if entity is not None and entity.platform == DOMAIN and entity.config_entry_id:
        sn, _, sensor_id = entity.unique_id.partition("_")
        if sn.isdigit() and sensor_id.isdigit():
            entry = _loaded_entry(hass, {entity.config_entry_id})
            return entry, int(sn), int(sensor_id)
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="entity_not_channel",
        translation_placeholders={"entity_id": entity_id},
    )


def _faults_response(faults: Faults) -> dict[str, Any]:
    def lamps(value: Any) -> dict[str, bool | None] | None:
        if value is None:
            return None
        return {
            "mil": value.mil,
            "red_stop": value.red_stop,
            "amber": value.amber,
            "protect": value.protect,
        }

    return {
        "active_count": faults.active_count,
        "lamps": lamps(faults.lamps),
        "lamps_time": _iso(faults.lamps_time),
        "truncated": faults.truncated,
        "faults": [
            {
                "code": f.display_code,
                "name": f.name,
                "protocol": f.protocol.value,
                "status": f.status.value,
                "severity": f.severity.value,
                "ecu": f.ecu,
                "spn": f.spn,
                "fmi": f.fmi,
                "source_address": f.source_address,
                "occurrence": f.occurrence,
                "set_time": _iso(f.set_time),
                "last_seen": _iso(f.last_seen),
                "lamps": lamps(f.lamps),
            }
            for f in faults.faults
        ],
    }


def _trip_response(trip: Trip) -> dict[str, Any]:
    return {
        "status": trip.status.value,
        "start": _iso(trip.start),
        "end": _iso(trip.end),
        "distance_km": trip.distance_km,
        "duration_s": trip.duration_s,
        "start_position": _latlng(trip.start_position),
        "end_position": _latlng(trip.end_position),
    }


def _history_response(history: History, sensor_id: int) -> dict[str, Any]:
    series = history.series.get(sensor_id)
    return {
        "unit": series.unit if series else None,
        "truncated": series.truncated if series else False,
        "points": [
            {
                "start": p.start.isoformat(),
                "mean": p.mean,
                "min": p.min,
                "max": p.max,
                "last": p.last,
            }
            for p in (series.points if series else ())
        ],
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the response-only actions once, for all entries."""

    async def get_faults(call: ServiceCall) -> ServiceResponse:
        entry, sn = _resolve_device(hass, call.data[ATTR_DEVICE_ID])
        try:
            faults = await entry.runtime_data.client.get_faults(sn)
        except IMatrixError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="api_error"
            ) from err
        return _faults_response(faults)

    async def get_trips(call: ServiceCall) -> ServiceResponse:
        entry, sn = _resolve_device(hass, call.data[ATTR_DEVICE_ID])
        try:
            trips = await entry.runtime_data.client.get_trips(
                sn, call.data.get(ATTR_START), call.data.get(ATTR_END)
            )
        except IMatrixError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="api_error"
            ) from err
        return {"trips": [_trip_response(t) for t in trips]}

    async def get_history(call: ServiceCall) -> ServiceResponse:
        entry, sn, sensor_id = _resolve_channel(hass, call.data[ATTR_ENTITY_ID])
        try:
            history = await entry.runtime_data.client.get_history(
                sn, [sensor_id], call.data.get(ATTR_START), call.data.get(ATTR_END)
            )
        except IMatrixError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="api_error"
            ) from err
        return _history_response(history, sensor_id)

    async def floor_plan_dashboard(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="floor_plan_off"
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        manager = entry.runtime_data.floor_plan
        if manager is None or manager.cards is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="floor_plan_off"
            )
        try:
            return await async_dashboard(
                hass,
                manager.cards,
                call.data[ATTR_CARD],
                call.data.get(ATTR_LEVEL_ID),
                new_link=call.data[ATTR_NEW_LINK],
            )
        except (IMatrixError, LookupError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="api_error"
            ) from err

    # Admin only: the answer holds the secret image link (FR-34).
    async_register_admin_service(
        hass,
        DOMAIN,
        "floor_plan_dashboard",
        floor_plan_dashboard,
        DASHBOARD_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    for name, handler, schema in (
        ("get_faults", get_faults, DEVICE_SCHEMA),
        ("get_trips", get_trips, TRIPS_SCHEMA),
        ("get_history", get_history, HISTORY_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema, supports_response=SupportsResponse.ONLY
        )
