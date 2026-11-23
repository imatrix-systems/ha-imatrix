"""Binary sensor platform for the iMatrix integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from pyimatrix import AlarmLevel, Channel, ChannelMode, DeviceKind

from . import IMatrixConfigEntry
from .const import MOVING_SPEED_KMH
from .coordinator import IMatrixCoordinator
from .entity import IMatrixEntity

PARALLEL_UPDATES = 0


def build_binary_sensors(
    coordinator: IMatrixCoordinator, sns: set[int]
) -> list[BinarySensorEntity]:
    """Create binary sensors for the given devices."""
    entities: list[BinarySensorEntity] = []
    for sn in sorted(sns):
        device = coordinator.data.devices[sn]
        if ignition := device.channel("ignition"):
            entities.append(IMatrixIgnitionSensor(coordinator, sn, ignition))
        if device.kind is DeviceKind.VEHICLE:
            entities.append(IMatrixMovingSensor(coordinator, sn))
        entities.extend(
            IMatrixAlarmSensor(coordinator, sn, channel)
            for channel in device.channels
            if channel.mode is ChannelMode.SCALAR
            and channel.quantity_kind
            and (channel.is_mapped or device.kind is not DeviceKind.VEHICLE)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IMatrixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors, and add more when devices appear."""
    coordinator = entry.runtime_data.coordinator

    @callback
    def add(sns: set[int]) -> None:
        async_add_entities(build_binary_sensors(coordinator, sns))

    add(set(coordinator.data.devices))
    entry.async_on_unload(coordinator.async_add_new_device_listener(add))


class IMatrixIgnitionSensor(IMatrixEntity, BinarySensorEntity):
    """Engine ignition. On for any non-zero state."""

    _attr_translation_key = "ignition"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self, coordinator: IMatrixCoordinator, sn: int, channel: Channel
    ) -> None:
        """Bind to the device's ignition channel."""
        super().__init__(coordinator, sn, "ignition")
        self._sensor_id = channel.sensor_id

    @property
    def is_on(self) -> bool | None:
        """Return True when ignition is on."""
        state = self.state_data
        reading = state.readings.get(self._sensor_id) if state else None
        if reading is None or reading.value is None:
            return None
        try:
            return float(reading.value) != 0
        except ValueError:
            return None


class IMatrixMovingSensor(IMatrixEntity, BinarySensorEntity):
    """Vehicle moving, from GPS speed, or vehicle speed when there is no fix."""

    _attr_translation_key = "moving"
    _attr_device_class = BinarySensorDeviceClass.MOVING

    def __init__(self, coordinator: IMatrixCoordinator, sn: int) -> None:
        """Create the moving sensor of a vehicle."""
        super().__init__(coordinator, sn, "moving")

    @property
    def is_on(self) -> bool | None:
        """Return True above 3 km/h."""
        state = self.state_data
        if state is None:
            return None
        speed: float | None = state.position.speed_kmh if state.position else None
        if (
            speed is None
            and self.device
            and (ch := self.device.channel("vehicle_speed"))
        ):
            reading = state.readings.get(ch.sensor_id)
            if reading is not None and isinstance(reading.value, float):
                speed = reading.value
        return None if speed is None else speed > MOVING_SPEED_KMH


class IMatrixAlarmSensor(IMatrixEntity, BinarySensorEntity):
    """On while a channel is past one of its iMatrix thresholds."""

    _attr_translation_key = "channel_alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: IMatrixCoordinator, sn: int, channel: Channel
    ) -> None:
        """Bind to one scalar channel."""
        super().__init__(coordinator, sn, f"{channel.sensor_id}_alarm")
        self._sensor_id = channel.sensor_id
        self._attr_translation_placeholders = {
            "channel": channel.name or f"Sensor {channel.sensor_id}"
        }

    @property
    def is_on(self) -> bool | None:
        """Return True at advisory, warning or alarm level."""
        state = self.state_data
        reading = state.readings.get(self._sensor_id) if state else None
        if reading is None or reading.alarm is AlarmLevel.UNKNOWN:
            return None
        return reading.alarm is not AlarmLevel.NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the level and which threshold was crossed."""
        state = self.state_data
        reading = state.readings.get(self._sensor_id) if state else None
        if reading is None:
            return None
        return {
            "level": reading.alarm.value,
            "side": reading.alarm_side.value if reading.alarm_side else None,
        }
