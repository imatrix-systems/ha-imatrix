import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pyimatrix import Faults, History, IMatrixConnectionError, Trip
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import DOMAIN

from .conftest import FIXTURES


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _device_id(hass: HomeAssistant, entry: MockConfigEntry, sn: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, sn), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_get_faults(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_faults = AsyncMock(
        return_value=Faults.from_dict(_load("faults.json"))
    )
    response = await hass.services.async_call(
        DOMAIN,
        "get_faults",
        {"device_id": _device_id(hass, init_integration, "1000000001")},
        blocking=True,
        return_response=True,
    )
    mock_client.get_faults.assert_awaited_once_with(1000000001)
    assert response["active_count"] == 2
    assert response["lamps"] == {
        "mil": True,
        "red_stop": False,
        "amber": None,
        "protect": False,
    }
    first = response["faults"][0]
    assert first["code"] == "SPN 110 FMI 0"
    assert first["severity"] == "critical"
    assert first["lamps"]["red_stop"] is True
    assert response["faults"][1]["lamps"] is None


async def test_get_trips(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    trips = [Trip.from_dict(t) for t in _load("trips.json")["trips"]]
    mock_client.get_trips = AsyncMock(return_value=trips)
    response = await hass.services.async_call(
        DOMAIN,
        "get_trips",
        {
            "device_id": _device_id(hass, init_integration, "1000000001"),
            "start": "2026-11-20T00:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    sn, start, end = mock_client.get_trips.await_args.args
    assert sn == 1000000001
    assert start.isoformat() == "2026-11-20T00:00:00+00:00"
    assert end is None
    active, done = response["trips"]
    assert active["status"] == "active"
    assert active["end"] is None
    assert active["start_position"] == {"lat": 39.0, "lng": -120.1}
    assert done["end_position"] == {"lat": 39.2, "lng": -120.3}


async def test_get_history(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_history = AsyncMock(
        return_value=History.from_dict(_load("history.json"))
    )
    response = await hass.services.async_call(
        DOMAIN,
        "get_history",
        {"entity_id": "sensor.truck_12_odometer"},
        blocking=True,
        return_response=True,
    )
    assert mock_client.get_history.await_args.args[:2] == (1000000001, [64])
    assert response["unit"] == "km"
    assert [p["last"] for p in response["points"]] == [101.0, 103.0]


async def test_get_history_without_series(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_history = AsyncMock(
        return_value=History.from_dict({"schemaVersion": 1, "sn": 1000000002})
    )
    response = await hass.services.async_call(
        DOMAIN,
        "get_history",
        {"entity_id": "sensor.freezer_3_probe_1"},
        blocking=True,
        return_response=True,
    )
    assert response == {"unit": None, "truncated": False, "points": []}


async def test_history_rejects_non_channel_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "get_history",
            {"entity_id": "sensor.truck_12_connectivity"},
            blocking=True,
            return_response=True,
        )
    assert info.value.translation_key == "entity_not_channel"


async def test_unknown_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "x")}
    )
    for device_id in ("missing", foreign.id):
        with pytest.raises(ServiceValidationError) as info:
            await hass.services.async_call(
                DOMAIN,
                "get_faults",
                {"device_id": device_id},
                blocking=True,
                return_response=True,
            )
        assert info.value.translation_key == "device_not_found"


async def test_entry_not_loaded(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device_id = _device_id(hass, init_integration, "1000000001")
    await hass.config_entries.async_unload(init_integration.entry_id)
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "get_faults",
            {"device_id": device_id},
            blocking=True,
            return_response=True,
        )
    assert info.value.translation_key == "entry_not_loaded"


@pytest.mark.parametrize(
    ("action", "method", "data"),
    [
        ("get_faults", "get_faults", "device"),
        ("get_trips", "get_trips", "device"),
        ("get_history", "get_history", "entity"),
    ],
)
async def test_api_errors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    action: str,
    method: str,
    data: str,
) -> None:
    setattr(mock_client, method, AsyncMock(side_effect=IMatrixConnectionError("x")))
    payload = (
        {"device_id": _device_id(hass, init_integration, "1000000001")}
        if data == "device"
        else {"entity_id": "sensor.truck_12_odometer"}
    )
    with pytest.raises(HomeAssistantError) as info:
        await hass.services.async_call(
            DOMAIN, action, payload, blocking=True, return_response=True
        )
    assert info.value.translation_key == "api_error"
