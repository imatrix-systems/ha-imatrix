"""Spec 822 FR-41 to FR-47: HA changes reported back to the plan."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from pyimatrix import IMatrixPermissionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.config_flow import IMatrixConfigFlow
from custom_components.imatrix.const import (
    AUTH_TYPE_OAUTH2,
    CONF_AUTH_TYPE,
    CONF_OWNERSHIP,
    DOMAIN,
    OAUTH2_SCOPE_FLOOR_PLAN_WRITE,
    OWNERSHIP_HA_NAMES,
    OWNERSHIP_PLAN,
)
from custom_components.imatrix.floor_plan import FloorPlanManager
from custom_components.imatrix.floor_plan_changes import probe_capabilities

from .test_floor_plan import desired


def cloud() -> MagicMock:
    """Return a client with plan p1 at revision 3."""
    client = MagicMock()
    client.get_floor_plans = AsyncMock(
        return_value=[SimpleNamespace(id="p1", revision=3)]
    )
    client.get_floor_plan_desired = AsyncMock(return_value=desired())
    client.post_floor_plan_ha_changes = AsyncMock(
        return_value={"applied": 1, "waiting": 0, "revision": 4}
    )
    return client


async def started(
    hass: HomeAssistant, ownership: str, offer: bool = False
) -> FloorPlanManager:
    """Start a manager, apply the golden plan, and clear what the apply queued."""
    entry = MockConfigEntry(domain=DOMAIN, title="Home")
    entry.add_to_hass(hass)
    manager = FloorPlanManager(
        hass, entry, cloud(), "p1", False, ownership=ownership, offer_new_areas=offer
    )
    await manager.async_start()
    assert await manager.async_apply(manager.pending)
    await hass.async_block_till_done()
    manager.tracker.classify()
    assert manager.tracker.pending == {}
    return manager


def space_of(area_name: str) -> str:
    """Return the single space id behind a golden area."""
    area = next(a for a in desired().areas if a["name"] == area_name)
    return str(area["fpm_ids"][0])


async def test_capabilities_probe() -> None:
    """FR-47: three flags; this HA has floor levels."""
    caps = probe_capabilities()
    assert set(caps) == {
        "supports_nested_areas",
        "supports_area_types",
        "supports_floor_level",
    }
    assert caps["supports_floor_level"] is True


async def test_plan_owns_names_sends_nothing(hass: HomeAssistant) -> None:
    """FR-42: with the plan owning names, renames are not reported."""
    manager = await started(hass, OWNERSHIP_PLAN)
    assert not manager.tracker.active
    area = ar.async_get(hass).async_get_area_by_name("Kitchen")
    ar.async_get(hass).async_update(area.id, name="Cook room")
    await hass.async_block_till_done()
    manager.tracker.classify()
    assert manager.tracker.pending == {}
    manager.async_stop()


async def test_ha_names_reports_each_kind(hass: HomeAssistant) -> None:
    """FR-41, FR-43: rename, floor move, deletion and floor rename are queued."""
    manager = await started(hass, OWNERSHIP_HA_NAMES)
    areas = ar.async_get(hass)
    floors = fr.async_get(hass)
    kitchen = areas.async_get_area_by_name("Kitchen")
    upstairs = next(f for f in floors.async_list_floors() if f.name == "Upstairs")
    areas.async_update(kitchen.id, name="Cook room", floor_id=upstairs.floor_id)
    bedroom = areas.async_get_area_by_name("Bedroom")
    areas.async_delete(bedroom.id)
    floors.async_update(upstairs.floor_id, name="First floor")
    areas.async_create("Shed")  # not offered: the option is off
    await hass.async_block_till_done()
    manager.tracker.classify()
    upstairs_fpm = next(
        f["fpm_id"] for f in desired().floors if f["name"] == "Upstairs"
    )
    kinds = {
        (k, v.get("name") or v.get("levelId"))
        for (k, _), v in manager.tracker.pending.items()
    }
    assert ("rename", "Cook room") in kinds
    assert ("level_move", upstairs_fpm) in kinds
    assert ("rename", "First floor") in kinds
    assert ("area_deleted", None) in kinds
    assert not any(k == "new_area" for k, _ in manager.tracker.pending)
    await manager.tracker._flush()
    client = manager.client
    sent = client.post_floor_plan_ha_changes.await_args.args
    assert sent[0] == "p1" and sent[2] == 3
    assert {c["kind"] for c in sent[3]} == {"rename", "level_move", "area_deleted"}
    assert next(c for c in sent[3] if c.get("name") == "Cook room")[
        "fpmId"
    ] == space_of("Kitchen")
    assert set(sent[4]) == set(probe_capabilities())
    manager.async_stop()


async def test_new_areas_offered_only_when_on(hass: HomeAssistant) -> None:
    """FR-46: with the plan owning names, only new areas go up, and only when asked."""
    manager = await started(hass, OWNERSHIP_PLAN, offer=True)
    ar.async_get(hass).async_create("Shed")
    kitchen = ar.async_get(hass).async_get_area_by_name("Kitchen")
    ar.async_get(hass).async_update(kitchen.id, name="Cook room")
    await hass.async_block_till_done()
    manager.tracker.classify()
    assert [v for v in manager.tracker.pending.values()] == [
        {"kind": "new_area", "haAreaName": "Shed"}
    ]
    manager.async_stop()


async def test_device_moved(hass: HomeAssistant) -> None:
    """FR-45: a placed device moved to another area is reported with both plan ids."""
    manager = await started(hass, OWNERSHIP_HA_NAMES)
    assignment = desired().device_assignments[0]
    scheme, _, value = assignment["match_by"][0].partition("=")
    assert scheme == "zigbee_ieee"
    entry = MockConfigEntry(domain="zha")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, connections={(dr.CONNECTION_ZIGBEE, value)}
    )
    kitchen = ar.async_get(hass).async_get_area_by_name("Kitchen")
    dr.async_get(hass).async_update_device(device.id, area_id=kitchen.id)
    await hass.async_block_till_done()
    manager.tracker.classify()
    change = manager.tracker.pending[("device_moved", assignment["device_fpm_id"])]
    assert change == {
        "kind": "device_moved",
        "fpmId": assignment["device_fpm_id"],
        "toFpmId": space_of("Kitchen"),
    }
    manager.async_stop()


async def test_refused_report_asks_for_the_write_preset(hass: HomeAssistant) -> None:
    """FR-44: an API key without plan updates gets a repair naming the preset."""
    manager = await started(hass, OWNERSHIP_HA_NAMES)
    manager.client.post_floor_plan_ha_changes.side_effect = IMatrixPermissionError(
        "403"
    )
    await manager.async_send_changes([{"kind": "rename", "fpmId": "x", "name": "y"}])
    issue_id = f"floor_plan_write_key_{manager.entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    manager.client.post_floor_plan_ha_changes.side_effect = RuntimeError("down")
    await manager.async_send_changes([{"kind": "rename", "fpmId": "x", "name": "y"}])
    manager.async_stop()
    await manager.async_send_changes([])  # stopped: nothing is sent


async def test_reauth_asks_for_the_write_scope(hass: HomeAssistant) -> None:
    """FR-44: an OAuth reauth with HA names asks for imatrix.floor_plan_write."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_AUTH_TYPE: AUTH_TYPE_OAUTH2},
        options={CONF_OWNERSHIP: OWNERSHIP_HA_NAMES},
    )
    entry.add_to_hass(hass)
    flow = IMatrixConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_REAUTH, "entry_id": entry.entry_id}
    assert OAUTH2_SCOPE_FLOOR_PLAN_WRITE in flow.extra_authorize_data["scope"]
