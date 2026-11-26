"""Spec 822 FR-23 to FR-29: floor plan sync and opt-in device sharing.

The desired state is the spec 823 reference bundle's golden projection for fixture 02 (a
synthetic two-storey house), as the iMatrix cloud returns it.
"""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from pyimatrix import IMatrixPermissionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.config_flow import IMatrixConfigFlow
from custom_components.imatrix.const import AUTH_TYPE_OAUTH2, CONF_AUTH_TYPE, DOMAIN
from custom_components.imatrix.floor_plan import (
    Action,
    FloorPlanManager,
    FloorPlanStore,
    FloorPlanSync,
    area_key,
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


async def test_disambiguated_area_settles(hass: HomeAssistant) -> None:
    """Review F1: after creating Bathroom (1), the next sync has nothing to do."""
    ar.async_get(hass).async_create("Bathroom")
    store = FloorPlanStore(hass, "e4")
    await async_execute(hass, build_sync(hass, desired(), store), store)
    assert build_sync(hass, desired(), store).actions == []


async def test_relink_after_the_id_map_is_lost(hass: HomeAssistant) -> None:
    """Review F2 to F4: a re-added entry links what exists and creates nothing."""
    ar.async_get(hass).async_create("Bathroom")
    first = FloorPlanStore(hass, "e5")
    await async_execute(hass, build_sync(hass, desired(), first), first)
    areas_before = len(ar.async_get(hass).async_list_areas())
    fresh = FloorPlanStore(hass, "e6")
    sync = build_sync(hass, desired(), fresh)
    assert [a for a in sync.actions if a.kind == "create"] == []
    await async_execute(hass, sync, fresh)
    assert len(ar.async_get(hass).async_list_areas()) == areas_before
    assert fresh.data["areas"] == first.data["areas"]
    assert build_sync(hass, desired(), fresh).actions == []


async def test_user_floor_and_labels_are_not_edited(hass: HomeAssistant) -> None:
    """Review F5: a user's floor is used as it is; labels a user adds are kept."""
    floor = fr.async_get(hass).async_create(
        "Ground Floor", aliases={"my alias"}, level=5
    )
    store = FloorPlanStore(hass, "e7")
    await async_execute(hass, build_sync(hass, desired(), store), store)
    kept = fr.async_get(hass).async_get_floor(floor.floor_id)
    assert kept.aliases == {"my alias"} and kept.level == 5
    stairs = ar.async_get(hass).async_get_area_by_name("Main Stairs")
    assert stairs.floor_id == floor.floor_id
    mine = lr.async_get(hass).async_create("mine")
    ar.async_get(hass).async_update(
        stairs.id,
        labels=set(stairs.labels) | {mine.label_id},
        aliases=set(stairs.aliases) | {"Treppe"},
    )
    assert build_sync(hass, desired(), store).actions == []
    body = desired()
    target = next(a for a in body.areas if a["name"] == "Main Stairs")
    target["aliases"] = [*target.get("aliases", []), "Steps"]
    changed = build_sync(hass, body, store)
    await async_execute(hass, changed, store)
    area = ar.async_get(hass).async_get_area(store.data["areas"][area_key(target)])
    assert mine.label_id in area.labels
    assert {"Treppe", "Steps"} <= set(area.aliases)


async def test_device_without_an_area_is_not_counted(hass: HomeAssistant) -> None:
    """Review F3: an assignment whose area is unknown stops the run."""
    store = FloorPlanStore(hass, "e8")
    action = Action("assign", "device", "dev1", {"area_key": "x", "device_name": "d"})
    with pytest.raises(ValueError, match="no area"):
        await async_execute(hass, FloorPlanSync(1, [action], []), store)
    assert store.data["revision"] is None


def _manager(hass: HomeAssistant, entry: MockConfigEntry, share: bool) -> Any:
    client = MagicMock()
    client.get_floor_plans = AsyncMock(
        return_value=[SimpleNamespace(id="p1", revision=3)]
    )
    client.get_floor_plan_desired = AsyncMock(return_value=desired())
    client.put_ha_devices = AsyncMock(return_value=0)
    client.delete_ha_devices = AsyncMock()
    return FloorPlanManager(hass, entry, client, "p1", share=share)


async def test_repair_runs_only_the_list_it_showed(hass: HomeAssistant) -> None:
    """Review F6: a newer check between show and submit aborts the submit."""
    entry = MockConfigEntry(domain=DOMAIN, title="Cold store")
    entry.add_to_hass(hass)
    manager = _manager(hass, entry, share=False)
    await manager.async_start()
    manager.async_stop()
    entry.runtime_data = SimpleNamespace(floor_plan=manager)
    flow = FloorPlanSyncFlow(entry.entry_id)
    flow.hass = hass
    await flow.async_step_init()
    await manager.async_check()
    result = await flow.async_step_confirm({})
    assert result["type"] == "abort"
    assert result["reason"] == "floor_plan_changed"
    assert list(fr.async_get(hass).async_list_floors()) == []


async def test_failed_apply_rebuilds_the_list(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F7: a failure part way keeps what ran and offers a fresh list."""
    entry = MockConfigEntry(domain=DOMAIN, title="Cold store")
    entry.add_to_hass(hass)
    manager = _manager(hass, entry, share=False)
    await manager.async_start()
    manager.async_stop()
    shown = manager.pending
    monkeypatch.setattr(
        "custom_components.imatrix.floor_plan.async_execute",
        AsyncMock(side_effect=ValueError("name in use")),
    )
    assert await manager.async_apply(shown) is False
    assert manager.pending is not None and manager.pending is not shown


async def test_refused_upload_asks_for_the_right_credential(
    hass: HomeAssistant,
) -> None:
    """Review F8: a sign-in reauths with the sharing scope; a key gets a repair."""
    oauth = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_AUTH_TYPE: AUTH_TYPE_OAUTH2},
        options={"share_devices": True},
    )
    oauth.add_to_hass(hass)
    manager = _manager(hass, oauth, share=True)
    manager.client.put_ha_devices.side_effect = IMatrixPermissionError("403")
    with patch.object(oauth, "async_start_reauth") as reauth:
        await manager.async_start()
    manager.async_stop()
    reauth.assert_called_once()
    flow = IMatrixConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_REAUTH, "entry_id": oauth.entry_id}
    assert flow.extra_authorize_data == {"scope": "imatrix.read imatrix.ha_devices"}

    key = MockConfigEntry(
        domain=DOMAIN, title="Key entry", data={CONF_AUTH_TYPE: "api_key"}
    )
    key.add_to_hass(hass)
    manager = _manager(hass, key, share=True)
    manager.client.put_ha_devices.side_effect = IMatrixPermissionError("403")
    await manager.async_start()
    manager.async_stop()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"share_devices_key_{key.entry_id}"
    )
    assert issue is not None


async def test_stopped_manager_never_uploads(hass: HomeAssistant) -> None:
    """Review F9: a timer that fires after unload does not upload."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    manager = _manager(hass, entry, share=False)
    manager.instance_id = "ha-1"
    manager.async_stop()
    await manager.async_upload_devices()
    manager.client.put_ha_devices.assert_not_awaited()


async def test_long_fields_fit_the_cloud_limits(hass: HomeAssistant) -> None:
    """Review F10: long maker and model are cut; a too-long area id is left out."""
    entry = MockConfigEntry(domain="zha")
    entry.add_to_hass(hass)
    area = ar.async_get(hass).async_create("x" * 300)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("zha", "1")},
        manufacturer="m" * 400,
        model="n" * 400,
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    shared = next(d for d in build_device_list(hass) if d["haDeviceId"] == device.id)
    assert len(shared["manufacturer"]) == 255 and len(shared["model"]) == 255
    assert shared["haAreaId"] is None
