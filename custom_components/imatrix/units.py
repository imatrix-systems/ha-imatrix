"""Map iMatrix quantity kinds and canonical units onto HA device classes and units."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION as PPM,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
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
from pyimatrix import Channel

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


def channel_class_and_unit(
    channel: Channel,
) -> tuple[SensorDeviceClass | None, str | None]:
    """Return the HA device class and native unit of a scalar channel."""
    device_class, unit = QUANTITY.get(channel.quantity_kind or "", (None, None))
    if channel.quantity_kind == "distance":
        unit = DISTANCE_UNITS.get(channel.unit or "", channel.unit)
    elif unit is None:
        unit = channel.unit
    return device_class, unit
