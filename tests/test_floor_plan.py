"""Spec 822 FR-23 to FR-29: floor plan sync and opt-in device sharing.

The desired state is the spec 823 reference bundle's golden projection for fixture 02 (a
synthetic two-storey house), as the iMatrix cloud returns it.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import DOMAIN
from custom_components.imatrix.floor_plan import (
    FloorPlanManager,
    FloorPlanStore,
    async_execute,
    build_sync,
)
from custom_components.imatrix.floor_plan_share import ALLOWED_FIELDS, build_device_list
from custom_components.imatrix.repairs import FloorPlanSyncFlow

from .conftest import FIXTURES


def desired(revision: int = 3, assignments: list | None = None) -> SimpleNamespace:
    """Return the golden desired state, optionally with other device assignments."""
    body = json.loads((FIXTURES / "floor_plan_desired.json").read_text())
    return SimpleNamespace(
        revision=revision,
        floors=body["floors"],
        areas=body["areas"],
        labels=body["labels"],
        device_assignments=body["device_assignments"]
        if assignments is None
        else assignments,
        loss_report=body["loss_report"],
    )


async def test_first_sync_creates_everything_and_second_is_empty(
    hass: HomeAssistant,
) -> None:
    """Source section 12.4: a second plan after applying the first has no actions."""
    store = FloorPlanStore(hass, "e1")
    sync = build_sync(hass, desired(), store)
    kinds = {(a.kind, a.target) for a in sync.actions}
    assert {("create", "label"), ("create", "floor"), ("create", "area")} <= kinds
    assert len(sync.unmatched_devices) == len(desired().device_assignments)
    await async_execute(hass, sync, store)
    floors = {f.name: f for f in fr.async_get(hass).async_list_floors()}
    assert set(floors) == {"Ground Floor", "Upstairs"}
    assert floors["Ground Floor"].level == 0
    areas = {a.name: a for a in ar.async_get(hass).async_list_areas()}
    assert areas["Main Stairs"].floor_id == floors["Ground Floor"].floor_id
    assert areas["Bedroom"].floor_id == floors["Upstairs"].floor_id
    assert store.data["revision"] == 3
    again = build_sync(hass, desired(), store)
    assert again.actions == []


async def test_unmanaged_area_is_never_renamed(hass: HomeAssistant) -> None:
    """FR-25: an unmanaged Bathroom keeps its name; ours becomes Bathroom (1)."""
    mine = ar.async_get(hass).async_create("Bathroom")
    store = FloorPlanStore(hass, "e2")
    await async_execute(hass, build_sync(hass, desired(), store), store)
    names = {a.name for a in ar.async_get(hass).async_list_areas()}
    assert "Bathroom (1)" in names
    assert ar.async_get(hass).async_get_area(mine.id).name == "Bathroom"
    created = ar.async_get(hass).async_get_area_by_name("Bathroom (1)")
    assert "Bathroom" in created.aliases


async def test_imatrix_device_is_put_in_its_area(hass: HomeAssistant) -> None:
    """FR-26: an iMatrix device is matched by its serial and assigned to the area."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "123")},
        name="Freezer probe",
    )
    assignment = {
        "device": "Freezer probe",
        "match_by": ["imatrix_sn=0000000123"],
        "area": "Kitchen",
    }
    store = FloorPlanStore(hass, "e3")
    sync = build_sync(hass, desired(assignments=[assignment]), store)
    assert any(a.kind == "assign" for a in sync.actions)
    await async_execute(hass, sync, store)
    kitchen = ar.async_get(hass).async_get_area_by_name("Kitchen")
    assert dr.async_get(hass).async_get(device.id).area_id == kitchen.id


async def test_shared_list_holds_only_the_allowed_fields(hass: HomeAssistant) -> None:
    """FR-28: no entity ids, states, addresses or user names leave Home Assistant."""
    entry = MockConfigEntry(domain="hue")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("hue", "lamp-1")},
        connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")},
        name="Hall lamp",
        manufacturer="Signify",
        model="LCT015",
    )
    er.async_get(hass).async_get_or_create(
        "light", "hue", "lamp-1-light", device_id=device.id, config_entry=entry
    )
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "hue",
        "lamp-1-motion",
        device_id=device.id,
        config_entry=entry,
        original_device_class="motion",
    )
    devices = build_device_list(hass)
    lamp = next(d for d in devices if d["haDeviceId"] == device.id)
    assert set(lamp) == ALLOWED_FIELDS
    assert lamp["integrationDomain"] == "hue"
    assert lamp["categories"] == ["binary_sensor/motion", "light"]
    assert "aa:bb:cc:dd:ee:ff" not in json.dumps(devices)


async def test_manager_uploads_when_sharing_and_withdraws_when_off(
    hass: HomeAssistant,
) -> None:
    """FR-29: sharing uploads the list; turning it off deletes it from iMatrix."""
    entry = MockConfigEntry(domain=DOMAIN, title="Cold store")
    entry.add_to_hass(hass)
    client = MagicMock()
    client.put_ha_devices = AsyncMock(return_value=0)
    client.delete_ha_devices = AsyncMock()
    client.get_floor_plans = AsyncMock(
        return_value=[SimpleNamespace(id="p1", revision=3)]
    )
    client.get_floor_plan_desired = AsyncMock(return_value=desired())
    sharing = FloorPlanManager(hass, entry, client, "p1", share=True)
    await sharing.async_start()
    sharing.async_stop()
    client.put_ha_devices.assert_awaited_once()
    assert sharing.pending is not None and sharing.pending.actions
    off = FloorPlanManager(hass, entry, client, "p1", share=False)
    await off.async_start()
    off.async_stop()
    client.delete_ha_devices.assert_awaited_once()


async def test_repair_flow_applies_the_pending_changes(hass: HomeAssistant) -> None:
    """FR-24: nothing changes until the user submits the repair."""
    entry = MockConfigEntry(domain=DOMAIN, title="Cold store")
    entry.add_to_hass(hass)
    client = MagicMock()
    client.get_floor_plans = AsyncMock(
        return_value=[SimpleNamespace(id="p1", revision=3)]
    )
    client.get_floor_plan_desired = AsyncMock(return_value=desired())
    manager = FloorPlanManager(hass, entry, client, "p1", share=False)
    await manager.async_start()
    manager.async_stop()
    entry.runtime_data = SimpleNamespace(floor_plan=manager)
    assert list(fr.async_get(hass).async_list_floors()) == []
    flow = FloorPlanSyncFlow(entry.entry_id)
    flow.hass = hass
    shown = await flow.async_step_confirm()
    assert shown["type"] == "form"
    done = await flow.async_step_confirm({})
    assert done["type"] == "create_entry"
    assert {f.name for f in fr.async_get(hass).async_list_floors()} == {
        "Ground Floor",
        "Upstairs",
    }
