from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import DOMAIN

TRUCK = "1000000001"
FREEZER = "1000000002"
ROOM = "1000000003"


async def test_device_registry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = dr.async_get(hass)
    entry_id = init_integration.entry_id
    truck = registry.async_get_device_by_identifier((DOMAIN, TRUCK), entry_id)
    freezer = registry.async_get_device_by_identifier((DOMAIN, FREEZER), entry_id)
    room = registry.async_get_device_by_identifier((DOMAIN, ROOM), entry_id)
    assert truck is not None and freezer is not None and room is not None
    assert truck.name == "Truck 12"
    assert truck.manufacturer == "iMatrix Systems"
    assert truck.model == "QConnect FC-202"
    assert truck.model_id == "900001"
    assert truck.serial_number == TRUCK
    assert truck.sw_version == "2.0.40"
    assert truck.configuration_url == (
        "https://cloud.imatrixsys.com/dashboard/devices/1000000001"
    )
    assert freezer.via_device_id == truck.id
    assert room.name == "iMatrix 1000000003"
    assert room.via_device_id is None  # its gateway is not visible to this key


async def test_vehicle_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    odometer = hass.states.get("sensor.truck_12_odometer")
    assert odometer is not None
    assert odometer.state == "120455.2"
    assert odometer.attributes["unit_of_measurement"] == "km"
    assert odometer.attributes["state_class"] == "total_increasing"
    assert hass.states.get("sensor.truck_12_speed").state == "63.0"
    assert hass.states.get("binary_sensor.truck_12_ignition").state == "on"
    assert hass.states.get("binary_sensor.truck_12_moving").state == "on"
    assert hass.states.get("sensor.truck_12_connectivity").state == "online"

    tracker = hass.states.get("device_tracker.truck_12")
    assert tracker is not None
    assert tracker.attributes["latitude"] == 39.1
    assert tracker.attributes["longitude"] == -120.0
    assert tracker.attributes["gps_accuracy"] == 5
    assert tracker.attributes["heading"] == 270
    assert tracker.attributes["satellites"] == 11

    registry = er.async_get(hass)
    unique_ids = {
        e.unique_id: e
        for e in er.async_entries_for_config_entry(registry, init_integration.entry_id)
    }
    # Latitude and ignition are owned by other platforms, not sensors.
    assert f"{TRUCK}_2" not in unique_ids
    assert unique_ids[f"{TRUCK}_ignition"].domain == "binary_sensor"
    assert f"{TRUCK}_93" not in unique_ids
    # Unmapped vehicle channels are not created without the option (Q908).
    assert f"{TRUCK}_617" not in unique_ids
    # Engine details and event flags start disabled.
    for sensor_id in (150, 300):
        entry = unique_ids[f"{TRUCK}_{sensor_id}"]
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # Unmapped channels with no unit meaning are skipped without the option.
    assert f"{TRUCK}_250900" not in unique_ids
    assert f"{TRUCK}_250901" not in unique_ids


async def test_environmental_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    probe = hass.states.get("sensor.freezer_3_probe_1")
    assert probe is not None
    assert probe.state == "-14.2"  # asleep keeps the last value
    assert probe.attributes["device_class"] == "temperature"
    assert probe.attributes["unit_of_measurement"] == "°C"
    battery = hass.states.get("sensor.freezer_3_battery")
    assert battery.attributes["device_class"] == "battery"
    assert hass.states.get("sensor.freezer_3_sensor_250004").state == STATE_UNKNOWN
    assert hass.states.get("sensor.freezer_3_connectivity").state == "asleep"
    assert hass.states.get("device_tracker.freezer_3") is None

    registry = er.async_get(hass)
    alarm = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{FREEZER}_250001_alarm"
    )
    assert alarm is not None
    assert registry.async_get(alarm).disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_offline_device_unavailable(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert hass.states.get("sensor.imatrix_1000000003_room").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.imatrix_1000000003_connectivity").state == "offline"
    assert hass.states.get("sensor.imatrix_1000000003_last_seen").state == (
        "2026-11-01T00:00:00+00:00"
    )


async def test_us_customary_units(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: object
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    probe = hass.states.get("sensor.freezer_3_probe_1")
    assert probe.attributes["unit_of_measurement"] == "°F"
    assert float(probe.state) == 6.44  # -14.2 °C
    speed = hass.states.get("sensor.truck_12_speed")
    assert speed.attributes["unit_of_measurement"] == "mph"
    assert (
        hass.states.get("sensor.truck_12_odometer").attributes["unit_of_measurement"]
        == "mi"
    )
