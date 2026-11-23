"""Sensor platform for the iMatrix integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONCENTRATION_PARTS_PER_MILLION as PPM
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfMass,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from pyimatrix import Channel, ChannelMode, ConnectivityState, DeviceKind

from . import IMatrixConfigEntry
from .const import CONF_INCLUDE_UNMAPPED
from .coordinator import IMatrixCoordinator
from .entity import IMatrixEntity

PARALLEL_UPDATES = 0

# quantityKind -> (device class, HA unit). None unit means "take it from the channel".
QUANTITY: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    "temperature": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "humidity": (SensorDeviceClass.HUMIDITY, PERCENTAGE),
    "percentage": (None, PERCENTAGE),
    "speed": (SensorDeviceClass.SPEED, UnitOfSpeed.KILOMETERS_PER_HOUR),
    "distance": (SensorDeviceClass.DISTANCE, None),
    "duration": (SensorDeviceClass.DURATION, UnitOfTime.HOURS),
    "voltage": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    "current": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    "pressure": (SensorDeviceClass.PRESSURE, UnitOfPressure.KPA),
    "volume": (SensorDeviceClass.VOLUME_STORAGE, UnitOfVolume.LITERS),
    "mass": (SensorDeviceClass.WEIGHT, UnitOfMass.KILOGRAMS),
    "signal_strength": (
        SensorDeviceClass.SIGNAL_STRENGTH,
        SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    ),
    "concentration": (None, PPM),
    "rotation_speed": (None, REVOLUTIONS_PER_MINUTE),
    "angle": (None, DEGREE),
}
DISTANCE_UNITS = {"km": UnitOfLength.KILOMETERS, "m": UnitOfLength.METERS}


@dataclass(frozen=True, kw_only=True)
class KeyOptions:
    """Per-key settings for channels with a semantic key."""

    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    enabled: bool = True
    category: EntityCategory | None = None
    precision: int | None = None


# Keys the sensor platform never creates: other platforms own them.
SKIP_KEYS = frozenset({"latitude", "longitude", "ignition"})

KEYS: dict[str, KeyOptions] = {
    "odometer": KeyOptions(state_class=SensorStateClass.TOTAL_INCREASING, precision=0),
    "engine_hours": KeyOptions(
        state_class=SensorStateClass.TOTAL_INCREASING, precision=1
    ),
    "idle_time": KeyOptions(
        state_class=SensorStateClass.TOTAL_INCREASING, enabled=False
    ),
    "vehicle_speed": KeyOptions(precision=0),
    "gps_speed": KeyOptions(precision=0, enabled=False),
    "fuel_level": KeyOptions(precision=0),
    "def_level": KeyOptions(precision=0),
    "module_voltage": KeyOptions(precision=2),
    "engine_rpm": KeyOptions(precision=0, enabled=False),
    "coolant_temperature": KeyOptions(precision=0, enabled=False),
    "oil_temperature": KeyOptions(precision=0, enabled=False),
    "intake_air_temperature": KeyOptions(precision=0, enabled=False),
    "engine_load": KeyOptions(precision=0, enabled=False),
    "fuel_rate": KeyOptions(enabled=False),
    "throttle_position": KeyOptions(precision=0, enabled=False),
    "timing_advance": KeyOptions(enabled=False),
    "altitude": KeyOptions(precision=0, enabled=False),
    "heading": KeyOptions(precision=0, enabled=False),
    "hdop": KeyOptions(enabled=False, category=EntityCategory.DIAGNOSTIC),
    "satellites": KeyOptions(
        precision=0, enabled=False, category=EntityCategory.DIAGNOSTIC
    ),
}
# Stateful event flags from the telematics unit. Off by default.
EVENT_KEYS = frozenset(
    {
        "speeding",
        "idle_state",
        "vehicle_stopped",
        "hard_brake",
        "hard_acceleration",
        "hard_turn",
        "hard_bump",
        "hard_pothole",
        "hard_impact",
    }
)
DEFAULT_PRECISION = {SensorDeviceClass.TEMPERATURE: 1, SensorDeviceClass.HUMIDITY: 0}


def _should_create(channel: Channel, include_unmapped: bool) -> bool:
    if channel.key in SKIP_KEYS:
        return False
    # Too many states to list (for example the OBD-II DTC sensor). Faults come
    # from the fault-code endpoint instead.
    if channel.mode is ChannelMode.STATEFUL and not channel.states:
        return False
    if channel.is_mapped:
        return True
    if channel.mode is ChannelMode.STATEFUL:
        return include_unmapped
    return channel.quantity_kind is not None or include_unmapped


def build_sensors(
    coordinator: IMatrixCoordinator, sns: set[int], include_unmapped: bool
) -> list[SensorEntity]:
    """Create sensor entities for the given devices."""
    entities: list[SensorEntity] = []
    for sn in sorted(sns):
        device = coordinator.data.devices[sn]
        entities.append(IMatrixConnectivitySensor(coordinator, sn))
        entities.append(IMatrixLastSeenSensor(coordinator, sn))
        entities.extend(
            IMatrixChannelSensor(coordinator, sn, channel)
            for channel in device.channels
            if _should_create(channel, include_unmapped)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IMatrixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, and add more when devices appear."""
    coordinator = entry.runtime_data.coordinator
    include_unmapped = entry.options.get(CONF_INCLUDE_UNMAPPED, False)

    @callback
    def add(sns: set[int]) -> None:
        async_add_entities(build_sensors(coordinator, sns, include_unmapped))

    add(set(coordinator.data.devices))
    entry.async_on_unload(coordinator.async_add_new_device_listener(add))


class IMatrixChannelSensor(IMatrixEntity, SensorEntity):
    """One channel of a device. unique_id uses sensorId, which iMatrix never reuses."""

    def __init__(
        self, coordinator: IMatrixCoordinator, sn: int, channel: Channel
    ) -> None:
        """Map the channel's key and quantity kind onto HA sensor attributes."""
        super().__init__(coordinator, sn, str(channel.sensor_id))
        self.channel = channel
        device = coordinator.data.devices[sn]
        options = KEYS.get(channel.key, KeyOptions())

        if channel.is_mapped:
            self._attr_translation_key = channel.key
        else:
            self._attr_name = channel.name or f"Sensor {channel.sensor_id}"

        if channel.mode is ChannelMode.STATEFUL:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [self._label(s.value, s.label) for s in channel.states]
            self._attr_entity_registry_enabled_default = channel.key not in EVENT_KEYS
            return

        device_class, unit = QUANTITY.get(channel.quantity_kind or "", (None, None))
        if channel.quantity_kind == "distance":
            unit = DISTANCE_UNITS.get(channel.unit or "", channel.unit)
        elif unit is None:
            unit = channel.unit
        if (
            channel.quantity_kind == "percentage"
            and "batt" in (channel.name or "").lower()
        ):
            device_class = SensorDeviceClass.BATTERY
            options = KeyOptions(category=EntityCategory.DIAGNOSTIC)
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = options.state_class
        self._attr_entity_category = options.category
        self._attr_suggested_display_precision = (
            options.precision
            if options.precision is not None
            else DEFAULT_PRECISION.get(device_class)  # type: ignore[arg-type]
        )
        # Vehicles carry hundreds of unmapped CAN signals; show only the mapped ones.
        self._attr_entity_registry_enabled_default = options.enabled and (
            channel.is_mapped or device.kind is not DeviceKind.VEHICLE
        )

    @staticmethod
    def _label(value: int, label: str | None) -> str:
        return (label or str(value)).strip().lower().replace(" ", "_")

    @property
    def native_value(self) -> StateType:
        """Return the reading in the channel's canonical unit."""
        state = self.state_data
        reading = state.readings.get(self.channel.sensor_id) if state else None
        if reading is None or reading.value is None:
            return None
        if self.channel.mode is ChannelMode.STATEFUL:
            labels = {
                s.value: self._label(s.value, s.label) for s in self.channel.states
            }
            try:
                return labels.get(int(float(reading.value)))
            except ValueError:
                return None
        if isinstance(reading.value, str):
            return None
        return reading.value


class IMatrixConnectivitySensor(IMatrixEntity, SensorEntity):
    """Cloud check-in state. Stays available so it can show offline."""

    _attr_translation_key = "connectivity"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IMatrixCoordinator, sn: int) -> None:
        """Create the connectivity sensor of a device."""
        super().__init__(coordinator, sn, "connectivity")
        self._attr_options = [
            s.value for s in ConnectivityState if s is not ConnectivityState.UNKNOWN
        ]

    @property
    def available(self) -> bool:
        """Available whenever the coordinator is."""
        return self.coordinator.last_update_success and self.device is not None

    @property
    def native_value(self) -> str | None:
        """Return the state, or None when the cloud sent a value HA does not know."""
        connectivity = self.connectivity
        if connectivity is None or connectivity.state is ConnectivityState.UNKNOWN:
            return None
        return connectivity.state.value


class IMatrixLastSeenSensor(IMatrixEntity, SensorEntity):
    """When the device last checked in."""

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IMatrixCoordinator, sn: int) -> None:
        """Create the last-seen sensor of a device."""
        super().__init__(coordinator, sn, "last_seen")

    @property
    def available(self) -> bool:
        """Available whenever the coordinator is."""
        return self.coordinator.last_update_success and self.device is not None

    @property
    def native_value(self) -> datetime | None:
        """Return the last check-in time."""
        connectivity = self.connectivity
        return connectivity.last_seen if connectivity else None
