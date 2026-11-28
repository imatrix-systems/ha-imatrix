import dataclasses
from datetime import timedelta
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pyimatrix import (
    IMatrixAuthError,
    IMatrixConnectionError,
    IMatrixRateLimitError,
    Latest,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.imatrix.const import DEVICE_REFRESH_EVERY, DOMAIN

from .conftest import load_devices, load_latest


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 60
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_delta_poll_uses_since_and_merges(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    full = load_latest()
    truck = full.devices[1000000001]
    faster = dataclasses.replace(
        truck,
        readings={
            **truck.readings,
            142: dataclasses.replace(truck.readings[142], value=80.0),
        },
    )
    mock_client.get_latest.side_effect = lambda *_, **__: Latest(
        server_time=full.server_time, devices={1000000001: faster, 5555: truck}
    )
    await _tick(hass, freezer)

    _, kwargs = mock_client.get_latest.call_args
    assert kwargs["since"] == full.server_time
    assert mock_client.get_devices.call_count == 1
    assert hass.states.get("sensor.truck_12_speed").state == "80.0"
    # Devices missing from a delta keep their previous state.
    assert hass.states.get("sensor.freezer_3_probe_1").state == "-14.2"

    # 5555 is unknown, so the next poll refreshes the device list.
    mock_client.get_latest.side_effect = lambda *_, **__: load_latest()
    await _tick(hass, freezer)
    assert mock_client.get_devices.call_count == 2


async def test_device_list_refresh_interval(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    for _ in range(DEVICE_REFRESH_EVERY):
        await _tick(hass, freezer)
    assert mock_client.get_devices.call_count == 1
    await _tick(hass, freezer)
    assert mock_client.get_devices.call_count == 2


async def test_new_and_removed_devices(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    devices = load_devices()
    newcomer = dataclasses.replace(devices[2], sn=1000000004, name="Cooler 4")
    mock_client.get_devices.side_effect = lambda **_: [devices[0], devices[1], newcomer]
    coordinator = init_integration.runtime_data.coordinator
    coordinator.data = dataclasses.replace(coordinator.data, server_time=None)
    await _tick(hass, freezer)

    registry = dr.async_get(hass)
    entry_id = init_integration.entry_id
    assert registry.async_get_device_by_identifier((DOMAIN, "1000000004"), entry_id)
    assert hass.states.get("sensor.cooler_4_connectivity") is not None
    assert (
        registry.async_get_device_by_identifier((DOMAIN, "1000000003"), entry_id)
        is None
    )


async def test_rename_propagates(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    devices = load_devices()
    devices[0] = dataclasses.replace(devices[0], name="Truck 12 (spare)")
    mock_client.get_devices.side_effect = lambda **_: devices
    coordinator = init_integration.runtime_data.coordinator
    coordinator.data = dataclasses.replace(coordinator.data, server_time=None)
    await _tick(hass, freezer)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "1000000001"), init_integration.entry_id
    )
    assert device is not None
    assert device.name == "Truck 12 (spare)"


async def test_update_failure_and_recovery(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = init_integration.runtime_data.coordinator
    mock_client.get_latest.side_effect = IMatrixConnectionError("HTTP 502")
    # A short outage (a backend deploy) keeps the last values and retries in 30 s.
    await _tick(hass, freezer)
    assert hass.states.get("sensor.truck_12_odometer").state == "120455.2"
    assert coordinator.update_interval == timedelta(seconds=30)
    for _ in range(4):
        await _tick(hass, freezer, 30)
    # Still failing after the tolerated retries: now the entities go unavailable.
    assert hass.states.get("sensor.truck_12_odometer").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.truck_12_connectivity").state == STATE_UNAVAILABLE

    mock_client.get_latest.side_effect = lambda *_, **__: load_latest()
    await _tick(hass, freezer)
    assert hass.states.get("sensor.truck_12_odometer").state == "120455.2"


async def test_rate_limit_backoff(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = init_integration.runtime_data.coordinator
    mock_client.get_latest.side_effect = IMatrixRateLimitError("429", retry_after=300)
    await _tick(hass, freezer)
    assert coordinator.update_interval == timedelta(seconds=300)

    mock_client.get_latest.side_effect = IMatrixRateLimitError("429", retry_after=None)
    await _tick(hass, freezer, 300)
    assert coordinator.update_interval == timedelta(seconds=600)

    mock_client.get_latest.side_effect = lambda *_, **__: load_latest()
    await _tick(hass, freezer, 600)
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_auth_failure_during_update_starts_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.get_latest.side_effect = IMatrixAuthError("401")
    await _tick(hass, freezer)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_moving_falls_back_to_vehicle_speed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    full = load_latest()
    truck = dataclasses.replace(full.devices[1000000001], position=None)
    mock_client.get_latest.side_effect = lambda *_, **__: Latest(
        server_time=full.server_time, devices={**full.devices, 1000000001: truck}
    )
    await _tick(hass, freezer)
    assert hass.states.get("binary_sensor.truck_12_moving").state == "on"
    assert hass.states.get("device_tracker.truck_12").attributes.get("latitude") is None
