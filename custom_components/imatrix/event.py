"""Event platform: fault codes, geofence crossings and iMatrix alerts."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from pyimatrix import DeviceKind, Event, EventType

from . import IMatrixConfigEntry
from .coordinator import IMatrixCoordinator
from .entity import IMatrixEntity

PARALLEL_UPDATES = 0


class IMatrixEventEntity(IMatrixEntity, EventEntity):
    """Fires once per feed event of its types. Never fires on startup or reload."""

    event_map: ClassVar[dict[EventType, str]]

    def __init__(self, coordinator: IMatrixCoordinator, sn: int, suffix: str) -> None:
        """Bind to a device. Events before setup are never replayed."""
        super().__init__(coordinator, sn, suffix)
        self._attr_event_types = list(dict.fromkeys(self.event_map.values()))
        self._last_id = max((e.id for e in coordinator.data.events), default=0)

    @property
    def available(self) -> bool:
        """Event entities stay available while the device is listed."""
        return self.coordinator.last_update_success and self.device is not None

    def _attributes(self, event: Event) -> dict[str, Any]:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        fired = False
        for event in self.coordinator.data.events:
            if event.sn != self.sn or event.id <= self._last_id:
                continue
            self._last_id = event.id
            if (event_type := self.event_map.get(event.type)) is not None:
                self._trigger_event(event_type, self._attributes(event))
                fired = True
        if fired:
            self.async_write_ha_state()
        else:
            super()._handle_coordinator_update()


class IMatrixFaultEvent(IMatrixEventEntity):
    """A fault code was set or cleared."""

    _attr_translation_key = "faults"
    event_map: ClassVar[dict[EventType, str]] = {
        EventType.DTC_SET: "set",
        EventType.DTC_CLEARED: "cleared",
    }

    def __init__(self, coordinator: IMatrixCoordinator, sn: int) -> None:
        """Create the fault event entity of a vehicle."""
        super().__init__(coordinator, sn, "fault_events")

    def _attributes(self, event: Event) -> dict[str, Any]:
        data = event.data
        return {
            "code": data.get("displayCode"),
            "name": data.get("name"),
            "protocol": data.get("protocol"),
            "severity": data.get("severity"),
            "ecu": data.get("ecu"),
            "spn": data.get("spn"),
            "fmi": data.get("fmi"),
            "set_time": data.get("setTs"),
            "clear_time": data.get("clearTs"),
            "inferred": data.get("inferred"),
            "manual": data.get("manual"),
        }


class IMatrixGeofenceEvent(IMatrixEventEntity):
    """The device entered, left or dwelt in an iMatrix geofence."""

    _attr_translation_key = "geofence"
    event_map: ClassVar[dict[EventType, str]] = {
        EventType.GEOFENCE_ENTER: "enter",
        EventType.GEOFENCE_EXIT: "exit",
        EventType.GEOFENCE_DWELL: "dwell",
    }

    def __init__(self, coordinator: IMatrixCoordinator, sn: int) -> None:
        """Create the geofence event entity of a device."""
        super().__init__(coordinator, sn, "geofence_events")

    def _attributes(self, event: Event) -> dict[str, Any]:
        return {
            "geofence_id": event.data.get("geofenceId"),
            "geofence": event.data.get("geofenceName"),
        }


class IMatrixAlertEvent(IMatrixEventEntity):
    """An iMatrix alert was raised or resolved."""

    _attr_translation_key = "alert"
    event_map: ClassVar[dict[EventType, str]] = {
        EventType.ALERT_RAISED: "raised",
        EventType.ALERT_RESOLVED: "resolved",
    }

    def __init__(self, coordinator: IMatrixCoordinator, sn: int) -> None:
        """Create the alert event entity of a device."""
        super().__init__(coordinator, sn, "alert_events")

    def _attributes(self, event: Event) -> dict[str, Any]:
        data = event.data
        return {
            "alert_id": data.get("alertId"),
            "title": data.get("title"),
            "severity": data.get("severity"),
            "source": data.get("source"),
            "transition": data.get("transition"),
            "root_cause": data.get("rootCause"),
        }


def build_events(coordinator: IMatrixCoordinator, sns: set[int]) -> list[EventEntity]:
    """Create event entities for the given devices."""
    entities: list[EventEntity] = []
    for sn in sorted(sns):
        device = coordinator.data.devices[sn]
        if device.kind is DeviceKind.VEHICLE:
            entities.append(IMatrixFaultEvent(coordinator, sn))
            entities.append(IMatrixGeofenceEvent(coordinator, sn))
        entities.append(IMatrixAlertEvent(coordinator, sn))
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IMatrixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up event entities, and add more when devices appear."""
    coordinator = entry.runtime_data.coordinator

    @callback
    def add(sns: set[int]) -> None:
        async_add_entities(build_events(coordinator, sns))

    add(set(coordinator.data.devices))
    entry.async_on_unload(coordinator.async_add_new_device_listener(add))
