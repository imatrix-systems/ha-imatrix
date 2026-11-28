"""Spec 856: iMatrix sensor visibility, order, virtual sensors, the dashboard."""

import json
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyimatrix import Device, Event, EventFrame, EventPage, EventType, Latest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import DOMAIN
from custom_components.imatrix.dashboard import device_layout

from .conftest import FIXTURES, load_latest

FREEZER = 1000000002
T1, T2, BATT, HUM = 250001, 250002, 250003, 250004
VIRTUAL = 5001


def devices(
    version: str = "v1",
    order: list[tuple[int, bool]] | None = None,
    virtual: bool = False,
    drop: set[int] = frozenset(),
    rename: dict[int, str] | None = None,
) -> list[Device]:
    """The fixture devices; the freezer carries spec 856 fields in the given order."""
    raw = json.loads((FIXTURES / "devices.json").read_text())
    for d in raw:
        if d["sn"] != FREEZER:
            continue
        by_id = {c["sensorId"]: c for c in d["channels"]}
        if virtual:
            by_id[VIRTUAL] = {
                "sensorId": VIRTUAL,
                "key": f"s{VIRTUAL}",
                "name": "Dew point",
                "quantityKind": "temperature",
                "unit": "°C",
                "dataType": "float",
                "mode": "scalar",
                "virtual": True,
                "decimals": 2,
            }
        wanted = order or [(T1, True), (T2, True), (BATT, True), (HUM, True)]
        if virtual and all(sid != VIRTUAL for sid, _ in wanted):
            wanted = [*wanted, (VIRTUAL, True)]
        channels = []
        for pos, (sid, visible) in enumerate(w for w in wanted if w[0] not in drop):
            ch = dict(by_id[sid], visible=visible, position=pos)
            if rename and sid in rename:
                ch["name"] = rename[sid]
            channels.append(ch)
        d["channels"] = channels
        d["configVersion"] = version
    return [Device.from_dict(d) for d in raw]


def entity(hass: HomeAssistant, sensor_id: int) -> er.RegistryEntry | None:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{FREEZER}_{sensor_id}")
    return registry.async_get(entity_id) if entity_id else None


async def setup(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, **kw: Any
) -> None:
    client.get_devices.side_effect = lambda **_: devices(**kw)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def change(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, **kw: Any
) -> None:
    """iMatrix changes the freezer; the next full device read sees it."""
    client.get_devices.side_effect = lambda **_: devices(**kw)
    coordinator = entry.runtime_data.coordinator
    coordinator._force_refresh = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_hidden_sensor_starts_disabled(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(
        hass,
        mock_config_entry,
        mock_client,
        order=[(HUM, True), (T1, False), (T2, True)],
    )
    assert entity(hass, T1).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entity(hass, HUM).disabled_by is None
    # Out of the iMatrix order (ui:false): no entity.
    assert entity(hass, BATT) is None
    assert (
        mock_config_entry.runtime_data.coordinator.visibility.get(f"{FREEZER}_{T1}")
        is False
    )


async def test_hide_then_show_follows_imatrix(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(hass, mock_config_entry, mock_client)
    assert entity(hass, T1).disabled_by is None
    await change(
        hass,
        mock_config_entry,
        mock_client,
        version="v2",
        order=[(T1, False), (T2, True), (BATT, True), (HUM, True)],
    )
    assert entity(hass, T1).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await change(hass, mock_config_entry, mock_client, version="v3")
    assert entity(hass, T1).disabled_by is None


async def test_user_choice_wins(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    order = [(T1, False), (T2, True), (BATT, True), (HUM, True)]
    await setup(hass, mock_config_entry, mock_client, order=order)
    registry = er.async_get(hass)
    # The user enables the hidden sensor, and disables a visible one.
    registry.async_update_entity(entity(hass, T1).entity_id, disabled_by=None)
    registry.async_update_entity(
        entity(hass, T2).entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    # iMatrix changes something else, then shows T2.
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await change(
            hass,
            mock_config_entry,
            mock_client,
            version="v2",
            order=[(HUM, True), (T1, False), (T2, True), (BATT, True)],
        )
    assert entity(hass, T1).disabled_by is None
    assert entity(hass, T2).disabled_by is er.RegistryEntryDisabler.USER


async def test_removed_channel_removes_entity_and_new_channel_reloads(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(hass, mock_config_entry, mock_client)
    assert entity(hass, T2) is not None
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await change(hass, mock_config_entry, mock_client, version="v2", drop={T2})
        assert entity(hass, T2) is None
        reload.assert_not_called()
        await change(
            hass, mock_config_entry, mock_client, version="v3", drop={T2}, virtual=True
        )
        reload.assert_called_once_with(mock_config_entry.entry_id)
        reload.reset_mock()
        await change(
            hass,
            mock_config_entry,
            mock_client,
            version="v4",
            drop={T2},
            virtual=True,
            rename={T1: "Top"},
        )
        reload.assert_called_once_with(mock_config_entry.entry_id)


async def test_same_version_changes_nothing(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(hass, mock_config_entry, mock_client)
    # A changed order under the same version is not looked at.
    await change(
        hass,
        mock_config_entry,
        mock_client,
        version="v1",
        order=[(T1, False), (T2, True), (BATT, True), (HUM, True)],
    )
    assert entity(hass, T1).disabled_by is None


async def test_latest_version_triggers_a_reread(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(hass, mock_config_entry, mock_client)
    base = load_latest()
    state = base.devices[FREEZER]
    mock_client.get_latest.side_effect = lambda *_, **__: Latest(
        server_time=base.server_time,
        devices={**base.devices, FREEZER: replace(state, config_version="v2")},
    )
    mock_client.get_devices.reset_mock()
    mock_client.get_devices.side_effect = lambda **_: devices(
        version="v2", order=[(T1, False), (T2, True), (BATT, True), (HUM, True)]
    )
    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_devices.call_args.kwargs == {"sns": [FREEZER]}
    assert entity(hass, T1).disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_config_event_from_stream_and_feed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(hass, mock_config_entry, mock_client)
    coordinator = mock_config_entry.runtime_data.coordinator
    event = Event(
        id=101,
        type=EventType.DEVICE_CONFIG_CHANGED,
        raw_type="device_config_changed",
        sn=FREEZER,
        time=None,
        data={"configVersion": "v2"},
    )
    mock_client.get_devices.reset_mock()
    mock_client.get_devices.side_effect = lambda **_: devices(
        version="v2", order=[(T1, False), (T2, True), (BATT, True), (HUM, True)]
    )
    with patch.object(coordinator, "async_request_refresh") as refresh:
        coordinator.async_apply_frame(EventFrame(event=event))
        await hass.async_block_till_done()
    refresh.assert_called_once()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_devices.call_args.kwargs == {"sns": [FREEZER]}
    assert entity(hass, T1).disabled_by is er.RegistryEntryDisabler.INTEGRATION

    # The same change arriving in the polled feed.
    mock_client.get_devices.reset_mock()
    mock_client.get_devices.side_effect = lambda **_: devices(version="v3")
    mock_client.get_events.return_value = EventPage(
        next_after=200,
        has_more=False,
        events=(
            Event(
                id=150,
                type=EventType.DEVICE_CONFIG_CHANGED,
                raw_type="device_config_changed",
                sn=FREEZER,
                time=None,
                data={"configVersion": "v3"},
            ),
        ),
    )
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert mock_client.get_devices.call_args.kwargs == {"sns": [FREEZER]}
    assert entity(hass, T1).disabled_by is None


async def test_option_off_keeps_every_channel(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"follow_imatrix_config": False}
    )
    mock_client.get_devices.side_effect = lambda **_: devices(
        order=[(T1, False), (T2, True), (BATT, True), (HUM, True)]
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert entity(hass, T1).disabled_by is None
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.async_apply_frame(
        EventFrame(
            event=Event(
                id=101,
                type=EventType.DEVICE_CONFIG_CHANGED,
                raw_type="device_config_changed",
                sn=FREEZER,
                time=None,
                data={},
            )
        )
    )
    assert coordinator._config_pending == set()


async def test_old_server_is_left_alone(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """FR-19: no configVersion, so a missing channel is not a removal."""
    assert entity(hass, T2) is not None
    raw = json.loads((FIXTURES / "devices.json").read_text())
    for d in raw:
        if d["sn"] == FREEZER:
            d["channels"] = [c for c in d["channels"] if c["sensorId"] != T2]
    mock_client.get_devices.side_effect = lambda **_: [Device.from_dict(d) for d in raw]
    coordinator = init_integration.runtime_data.coordinator
    coordinator._force_refresh = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert entity(hass, T2) is not None


async def test_virtual_sensor_entity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    base = load_latest()
    raw = json.loads((FIXTURES / "latest.json").read_text())
    raw["devices"][str(FREEZER)]["values"][str(VIRTUAL)] = {
        "v": None,
        "t": "2026-11-23T14:59:00Z",
        "alarm": "none",
        "alarmSide": None,
        "error": {"code": "missing_input", "message": "A has no value"},
    }
    mock_client.get_latest.side_effect = lambda *_, **__: Latest.from_dict(raw)
    await setup(hass, mock_config_entry, mock_client, virtual=True)
    reg = entity(hass, VIRTUAL)
    assert reg is not None and reg.disabled_by is None
    state = hass.states.get(reg.entity_id)
    assert state.state == "unknown"
    assert state.attributes["virtual_error"] == "missing_input"
    assert reg.options["sensor"]["suggested_display_precision"] == 2
    # A real channel carries no virtual attribute.
    assert "virtual_error" not in hass.states.get(entity(hass, T1).entity_id).attributes
    assert base.devices  # fixture unchanged


async def test_device_layout_follows_imatrix_order(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(
        hass,
        mock_config_entry,
        mock_client,
        order=[(HUM, True), (T2, True), (T1, False), (BATT, True)],
    )
    layout = {d["sn"]: d for d in device_layout(hass)}
    freezer = layout[FREEZER]
    assert freezer["entities"] == [
        entity(hass, HUM).entity_id,
        entity(hass, T2).entity_id,
        entity(hass, BATT).entity_id,
    ]
    assert freezer["name"] == "Freezer 3"
    assert device_layout(hass, "no-such-entry") == []


async def test_ws_device_layout_and_strategy_script(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: Any,
    hass_client: Any,
) -> None:
    await setup(hass, mock_config_entry, mock_client)
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {
            "id": 1,
            "type": "imatrix/device_layout",
            "config_entry_id": mock_config_entry.entry_id,
        }
    )
    msg = await ws.receive_json()
    assert msg["success"]
    assert any(d["sn"] == FREEZER for d in msg["result"]["devices"])
    client = await hass_client()
    resp = await client.get("/imatrix_static/imatrix-dashboard-strategy.js")
    assert resp.status == 200
    assert "ll-strategy-dashboard-imatrix" in await resp.text()
