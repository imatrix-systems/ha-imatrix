"""Base entity for the iMatrix integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyimatrix import Connectivity, Device, DeviceState

from .const import DOMAIN
from .coordinator import IMatrixCoordinator


class IMatrixEntity(CoordinatorEntity[IMatrixCoordinator]):
    """An entity of one iMatrix device. The device itself is registered by setup."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IMatrixCoordinator, sn: int, suffix: str) -> None:
        """Bind to a device. unique_id is "<sn>_<suffix>" and never changes."""
        super().__init__(coordinator)
        self.sn = sn
        self._attr_unique_id = f"{sn}_{suffix}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, str(sn))})

    @property
    def device(self) -> Device | None:
        """The device as last listed by the cloud."""
        return self.coordinator.data.devices.get(self.sn)

    @property
    def state_data(self) -> DeviceState | None:
        """The device's latest readings and position."""
        return self.coordinator.data.states.get(self.sn)

    @property
    def connectivity(self) -> Connectivity | None:
        """Latest connectivity, preferring the fresher /latest copy."""
        if (state := self.state_data) is not None:
            return state.connectivity
        return self.device.connectivity if self.device else None

    @property
    def available(self) -> bool:
        """Available while the cloud still expects the device (online, asleep, late)."""
        connectivity = self.connectivity
        return (
            super().available and connectivity is not None and connectivity.is_reachable
        )
