import dataclasses
from unittest.mock import MagicMock

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyimatrix import AlarmLevel, Connectivity, ConnectivityState, Latest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import DOMAIN

from .conftest import load_latest


def _edited_latest() -> Latest:
    full = load_latest()
    truck = full.devices[1000000001]
    freezer = full.devices[1000000002]
    truck = dataclasses.replace(
        truck,
        position=dataclasses.replace(truck.position, hdop=None),  # type: ignore[arg-type]
        readings={
            **truck.readings,
            93: dataclasses.replace(truck.readings[93], value="on?"),
            300: dataclasses.replace(truck.readings[300], value="x"),
            142: dataclasses.replace(truck.readings[142], value="fast"),
        },
    )
    freezer = dataclasses.replace(
        freezer,
        connectivity=Connectivity(ConnectivityState.UNKNOWN, None, None),
        readings={
            **freezer.readings,
            250002: dataclasses.replace(
                freezer.readings[250002], alarm=AlarmLevel.UNKNOWN
            ),
        },
    )
    return Latest(
        server_time=full.server_time,
        devices={**full.devices, 1000000001: truck, 1000000002: freezer},
    )


async def _setup_with_enabled(
    hass: HomeAssistant, entry: MockConfigEntry, entities: list[tuple[str, str]]
) -> dict[str, str]:
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ids = {
        uid: registry.async_get_or_create(
            domain, DOMAIN, uid, config_entry=entry
        ).entity_id
        for domain, uid in entities
    }
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return ids


async def test_alarm_sensor_states(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    ids = await _setup_with_enabled(
        hass,
        mock_config_entry,
        [
            ("binary_sensor", "1000000002_250001_alarm"),
            ("binary_sensor", "1000000002_250002_alarm"),
            ("binary_sensor", "1000000002_250004_alarm"),
        ],
    )
    high = hass.states.get(ids["1000000002_250001_alarm"])
    assert high.state == "on"
    assert high.attributes["level"] == "alarm"
    assert high.attributes["side"] == "high"
    assert high.attributes["friendly_name"] == "Freezer 3 Probe 1 alarm"
    normal = hass.states.get(ids["1000000002_250002_alarm"])
    assert normal.state == "off"
    assert normal.attributes["side"] is None
    no_reading = hass.states.get(ids["1000000002_250004_alarm"])
    assert no_reading.state == STATE_UNKNOWN
    assert "level" not in no_reading.attributes


async def test_unparseable_values(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_latest.side_effect = lambda *_, **__: _edited_latest()
    ids = await _setup_with_enabled(
        hass,
        mock_config_entry,
        [("sensor", "1000000001_300"), ("binary_sensor", "1000000002_250002_alarm")],
    )
    assert hass.states.get("binary_sensor.truck_12_ignition").state == STATE_UNKNOWN
    assert hass.states.get(ids["1000000001_300"]).state == STATE_UNKNOWN
    assert hass.states.get("sensor.truck_12_speed").state == STATE_UNKNOWN
    assert hass.states.get("device_tracker.truck_12").attributes["gps_accuracy"] == 0
    assert hass.states.get("sensor.freezer_3_connectivity").state == STATE_UNKNOWN
    alarm = hass.states.get(ids["1000000002_250002_alarm"])
    # UNKNOWN connectivity makes the device unavailable.
    assert alarm.state == "unavailable"


async def test_missing_state_data(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    full = load_latest()
    mock_client.get_latest.side_effect = lambda *_, **__: Latest(
        server_time=full.server_time,
        devices={k: v for k, v in full.devices.items() if k != 1000000001},
    )
    ids = await _setup_with_enabled(
        hass, mock_config_entry, [("binary_sensor", "1000000002_250001_alarm")]
    )
    # The truck has no /latest entry: device list connectivity is used.
    assert hass.states.get("sensor.truck_12_connectivity").state == "online"
    assert hass.states.get("binary_sensor.truck_12_ignition").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.truck_12_moving").state == STATE_UNKNOWN
    assert hass.states.get("sensor.truck_12_odometer").state == STATE_UNKNOWN
    assert ids
