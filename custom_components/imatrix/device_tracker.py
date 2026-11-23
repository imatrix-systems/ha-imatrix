"""Device tracker platform for the iMatrix integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker.const import SourceType
from homeassistant.components.device_tracker.entity import TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from pyimatrix import Position

from . import IMatrixConfigEntry
from .const import HDOP_TO_METERS
from .coordinator import IMatrixCoordinator
from .entity import IMatrixEntity

PARALLEL_UPDATES = 0


def _has_gps(coordinator: IMatrixCoordinator, sn: int) -> bool:
    device = coordinator.data.devices[sn]
    state = coordinator.data.states.get(sn)
    return device.channel("latitude") is not None or (
        state is not None and state.position is not None
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IMatrixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a tracker for every device with GPS."""
    coordinator = entry.runtime_data.coordinator

    @callback
    def add(sns: set[int]) -> None:
        async_add_entities(
            IMatrixTracker(coordinator, sn)
            for sn in sorted(sns)
            if _has_gps(coordinator, sn)
        )

    add(set(coordinator.data.devices))
    entry.async_on_unload(coordinator.async_add_new_device_listener(add))


class IMatrixTracker(IMatrixEntity, TrackerEntity):
    """GPS position of a device. HA derives the zone state from it."""

    _attr_name = None
    _attr_translation_key = "location"
    _attr_source_type = SourceType.GPS
    _attr_entity_category = None  # type: ignore[assignment]

    def __init__(self, coordinator: IMatrixCoordinator, sn: int) -> None:
        """Create the tracker of a device."""
        super().__init__(coordinator, sn, "location")

    @property
    def _position(self) -> Position | None:
        state = self.state_data
        return state.position if state else None

    @property
    def latitude(self) -> float | None:
        """Return the latitude of the last fix."""
        return self._position.latitude if self._position else None

    @property
    def longitude(self) -> float | None:
        """Return the longitude of the last fix."""
        return self._position.longitude if self._position else None

    @property
    def location_accuracy(self) -> float:
        """Return an accuracy estimate in meters (HDOP x 5), 0 when unknown."""
        position = self._position
        if position is None or position.hdop is None:
            return 0
        return round(position.hdop * HDOP_TO_METERS)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return heading, speed, altitude, satellites and fix time."""
        position = self._position
        if position is None:
            return None
        return {
            "heading": position.heading_deg,
            "speed": position.speed_kmh,
            "altitude": position.altitude_m,
            "satellites": position.satellites,
            "fix_time": position.fix_time.isoformat() if position.fix_time else None,
        }
