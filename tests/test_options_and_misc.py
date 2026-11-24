from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.imatrix.const import (
    CONF_GROUP_IDS,
    CONF_INCLUDE_UNMAPPED,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    hass.config_entries.async_update_entry(
        init_integration, options={CONF_GROUP_IDS: [77]}
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    labels = {
        o["value"]: o["label"]
        for o in result["data_schema"].schema[CONF_GROUP_IDS].config["options"]
    }
    assert labels == {"10": "Fleet / North", "11": "Depot", "77": "Group 77"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_GROUP_IDS: ["10"],
            CONF_SCAN_INTERVAL: 120,
            CONF_INCLUDE_UNMAPPED: True,
            "push": False,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {
        CONF_GROUP_IDS: [10],
        CONF_SCAN_INTERVAL: 120,
        CONF_INCLUDE_UNMAPPED: True,
        "push": False,
    }
    coordinator = init_integration.runtime_data.coordinator
    assert coordinator.update_interval.total_seconds() == 120
    assert mock_client.get_devices.call_args.kwargs["group_ids"] == [10]
    # Unmapped channels now exist: the CAN gear (scalar) and door state (stateful).
    registry = er.async_get(hass)
    can_odo = registry.async_get_entity_id("sensor", DOMAIN, "1000000001_617")
    assert can_odo is not None
    assert (
        registry.async_get(can_odo).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    )
    assert registry.async_get_entity_id("sensor", DOMAIN, "1000000001_250900")
    door = registry.async_get_entity_id("sensor", DOMAIN, "1000000001_250901")
    assert door is not None
    # A stateful channel sent without its state list is never an entity.
    assert registry.async_get_entity_id("sensor", DOMAIN, "1000000001_534") is None


async def test_stateful_enum_sensor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor", DOMAIN, "1000000001_300", config_entry=mock_config_entry
    ).entity_id
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "no"
    assert state.attributes["options"] == ["no", "yes"]


async def test_remove_device(
    hass: HomeAssistant,
    hass_ws_client: object,
    init_integration: MockConfigEntry,
) -> None:
    assert await async_setup_component(hass, "config", {})
    registry = dr.async_get(hass)
    live = registry.async_get_device_by_identifier(
        (DOMAIN, "1000000001"), init_integration.entry_id
    )
    stale = registry.async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={(DOMAIN, "42")}
    )
    client = await hass_ws_client(hass)  # type: ignore[operator]
    for device, expected in ((live, False), (stale, True)):
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": init_integration.entry_id,
                "device_id": device.id,
            }
        )
        response = await client.receive_json()
        assert response["success"] is expected


async def test_diagnostics_redacted(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    text = str(diag)
    for secret in (
        "imx_00000000",
        "1000000001",
        "Truck 12",
        "39.1",
        "Example Cold Chain",
        "Depot",
    ):
        assert secret not in text
    assert diag["coordinator"]["rate_limit_remaining"] == 118
    assert set(diag["devices"]) == {"device_0", "device_1", "device_2"}
