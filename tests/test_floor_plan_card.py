"""Spec 822 FR-33 to FR-35: floor plan levels for dashboards."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pyimatrix import FloorPlanLevelCard, IMatrixNotFoundError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import CONF_FLOOR_PLAN_ID, DOMAIN
from custom_components.imatrix.floor_plan import FloorPlanStore
from custom_components.imatrix.floor_plan_card import (
    FloorPlanCards,
    async_dashboard,
    entities_for,
    svg_url,
)

from .test_floor_plan import desired

L0 = "00000000-0000-4000-8000-000000000000"
L1 = "00000000-0000-4000-8000-000000000001"
L9 = "00000000-0000-4000-8000-000000000009"


def level(level_id: str, revision: int = 3, sn: str = "1001") -> FloorPlanLevelCard:
    """Return a level card as the cloud sends it."""
    return FloorPlanLevelCard.from_dict(
        {
            "revision": revision,
            "level": {"id": level_id, "name": f"Level {level_id[-1]}", "ordinal": 0},
            "levels": [
                {"id": L0, "name": "Level 0", "ordinal": 0},
                {"id": L1, "name": "Level 1", "ordinal": 1},
            ],
            "svg": f'<svg><g id="fpm-pl-{level_id[-1]}"/></svg>',
            "markers": [
                {
                    "placementId": f"pl-{level_id[-1]}",
                    "deviceId": "d1",
                    "name": "Freezer probe",
                    "category": "temperature_sensor",
                    "x": 25.5,
                    "y": 40,
                    "identifiers": [{"scheme": "imatrix_sn", "value": sn}],
                    "entityHints": [],
                },
                {
                    "placementId": "pl-nothing",
                    "deviceId": "d2",
                    "name": "Unknown",
                    "category": "other",
                    "x": 1,
                    "y": 1,
                    "identifiers": [{"scheme": "mac", "value": "00:11:22:33:44:55"}],
                    "entityHints": ["sensor.not_here"],
                },
            ],
        }
    )


def cloud() -> MagicMock:
    """Return a client whose plan has levels L0 and L1."""
    client = MagicMock()
    client.get_floor_plan_desired = AsyncMock(return_value=desired())
    client.get_floor_plan_level_card = AsyncMock(
        side_effect=lambda _plan, level_id, **_: level(level_id)
    )
    return client


def placed_device(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Register iMatrix device 1001 with one sensor and one diagnostic entity."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "1001")}
    )
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor", DOMAIN, "1001_temp", device_id=device.id, config_entry=entry
    )
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "1001_rssi",
        device_id=device.id,
        config_entry=entry,
        entity_category=er.EntityCategory.DIAGNOSTIC,
    )
    return ent_reg.async_get_entity_id("sensor", DOMAIN, "1001_temp") or ""


async def test_entities_come_from_the_local_registry(hass: HomeAssistant) -> None:
    """FR-35: the device's entities, diagnostics left out, unknown hints dropped."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    temp = placed_device(hass, entry)
    card = level(L0)
    assert entities_for(hass, card.markers[0]) == [temp]
    assert entities_for(hass, card.markers[1]) == []


async def test_cache_token_and_first_level(hass: HomeAssistant) -> None:
    """FR-34: one fetch per level for 5 minutes; the token is made once and kept."""
    client = cloud()
    store = FloorPlanStore(hass, "e1")
    cards = FloorPlanCards(hass, client, "p1", store, "inst")
    lowest = min(desired().floors, key=lambda f: f["level"])["fpm_id"]
    first = await cards.async_level()
    assert first.level.id == lowest
    await cards.async_level(lowest)
    assert client.get_floor_plan_level_card.await_count == 1
    token = await cards.async_token()
    assert token == await cards.async_token()
    assert cards.token_matches(token) and not cards.token_matches("x" * len(token))
    assert [c.level.id for c in await cards.async_all_levels()] == [L0, L1]


async def test_revision_change_clears_the_cache(hass: HomeAssistant) -> None:
    """FR-34: a newer plan revision drops every cached level."""
    client = cloud()
    cards = FloorPlanCards(hass, client, "p1", FloorPlanStore(hass, "e2"), None)
    await cards.async_level(L0)
    await cards.async_level(L1)
    cards._cache[L0] = (-1e9, cards._cache[L0][1])
    client.get_floor_plan_level_card.side_effect = lambda _p, lid, **_: level(lid, 4)
    assert (await cards.async_level(L0)).revision == 4
    assert L1 not in cards._cache


async def test_plan_without_levels(hass: HomeAssistant) -> None:
    """A plan with no floors has no first level."""
    client = cloud()
    client.get_floor_plan_desired = AsyncMock(return_value=SimpleNamespace(floors=[]))
    cards = FloorPlanCards(hass, client, "p1", FloorPlanStore(hass, "e3"), None)
    with pytest.raises(LookupError):
        await cards.async_level()


async def test_dashboard_views(hass: HomeAssistant) -> None:
    """FR-35: one view per level, markers without entities left out."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    temp = placed_device(hass, entry)
    cards = FloorPlanCards(hass, cloud(), "p1", FloorPlanStore(hass, "e4"), None)
    token = await cards.async_token()
    pe = await async_dashboard(hass, cards, "picture_elements", None)
    assert [v["title"] for v in pe["views"]] == ["Level 0", "Level 1"]
    card = pe["views"][0]["cards"][0]
    assert card["image"] == svg_url(token, L0)
    assert card["elements"] == [
        {
            "type": "state-icon",
            "entity": temp,
            "title": "Freezer probe",
            "style": {"left": "25.5%", "top": "40.0%"},
        }
    ]
    fp = await async_dashboard(hass, cards, "ha_floorplan", L1)
    rules = fp["views"][0]["cards"][0]["config"]["rules"]
    assert len(fp["views"]) == 1
    assert rules == [
        {
            "entity": temp,
            "element": "fpm-pl-1",
            "tap_action": "more-info",
            "hover_action": "hover-info",
        }
    ]


@pytest.fixture
async def plan_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> MockConfigEntry:
    """Set up the integration with floor plan p1 chosen."""
    mock_client.get_floor_plans = AsyncMock(
        return_value=[SimpleNamespace(id="p1", revision=3)]
    )
    mock_client.get_floor_plan_desired = AsyncMock(return_value=desired())
    mock_client.get_floor_plan_level_card = AsyncMock(
        side_effect=lambda _plan, level_id, **_: level(level_id)
    )
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_FLOOR_PLAN_ID: "p1"}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return mock_config_entry


async def test_view_serves_the_svg_by_token(
    hass: HomeAssistant, plan_entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """FR-34: the right token gets the SVG; a wrong token or level gets 404."""
    cards = plan_entry.runtime_data.floor_plan.cards
    token = await cards.async_token()
    web = await hass_client_no_auth()
    resp = await web.get(svg_url(token, L0))
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("image/svg+xml")
    assert "fpm-pl-0" in await resp.text()
    assert (await web.get(svg_url("wrong", L0))).status == 404
    mock = plan_entry.runtime_data.client.get_floor_plan_level_card
    mock.side_effect = IMatrixNotFoundError("404")
    assert (await web.get(svg_url(token, L9))).status == 404


async def test_dashboard_action(
    hass: HomeAssistant, plan_entry: MockConfigEntry
) -> None:
    """FR-35: the action returns the views; an entry without a plan is refused."""
    result = await hass.services.async_call(
        DOMAIN,
        "floor_plan_dashboard",
        {"config_entry_id": plan_entry.entry_id, "card": "picture_elements"},
        blocking=True,
        return_response=True,
    )
    assert result is not None and len(result["views"]) == 2
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "floor_plan_dashboard",
            {"config_entry_id": "nope", "card": "picture_elements"},
            blocking=True,
            return_response=True,
        )


async def test_new_link_and_revision(hass: HomeAssistant) -> None:
    """Review F1, F5: a new link replaces the token; a new revision clears the cache."""
    client = cloud()
    cards = FloorPlanCards(hass, client, "p1", FloorPlanStore(hass, "e5"), None)
    old = await cards.async_token()
    new = await cards.async_token(rotate=True)
    assert old != new and not cards.token_matches(old) and cards.token_matches(new)
    await cards.async_level(L0)
    cards.note_revision(3)
    assert L0 in cards._cache
    cards.note_revision(4)
    assert cards._cache == {}
    with pytest.raises(LookupError):
        await cards.async_level("../../p2/levels/x")


async def test_several_buildings_name_their_views(hass: HomeAssistant) -> None:
    """Review F3, F4: levels of every building, titles carry the building name."""
    two = level(L0)
    refs = [
        {
            "id": L0,
            "name": "Ground",
            "ordinal": 0,
            "buildingId": "b1",
            "buildingName": "House",
        },
        {
            "id": L1,
            "name": "Ground",
            "ordinal": 0,
            "buildingId": "b2",
            "buildingName": "Garage",
        },
    ]

    def card(_plan: str, level_id: str, **_: Any) -> FloorPlanLevelCard:
        ref = next(r for r in refs if r["id"] == level_id)
        return FloorPlanLevelCard.from_dict(
            {"revision": 3, "level": ref, "levels": refs, "svg": two.svg, "markers": []}
        )

    client = cloud()
    client.get_floor_plan_level_card = AsyncMock(side_effect=card)
    cards = FloorPlanCards(hass, client, "p1", FloorPlanStore(hass, "e6"), None)
    cards._first_level = L0
    views = (await async_dashboard(hass, cards, "picture_elements", L0))["views"]
    assert views[0]["title"] == "House Ground"
    every = (await async_dashboard(hass, cards, "picture_elements", None))["views"]
    assert [v["title"] for v in every] == ["House Ground", "Garage Ground"]
    assert len({v["path"] for v in every}) == 2


async def test_view_refuses_a_non_uuid_level(
    hass: HomeAssistant, plan_entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """Review F2: the view only accepts level ids; the SVG carries a strict CSP."""
    token = await plan_entry.runtime_data.floor_plan.cards.async_token()
    web = await hass_client_no_auth()
    assert (await web.get(svg_url(token, "x"))).status == 404
    resp = await web.get(svg_url(token, L0))
    assert resp.headers["Content-Security-Policy"].startswith("default-src 'none'")


async def test_card_websocket_and_script(
    hass: HomeAssistant,
    plan_entry: MockConfigEntry,
    hass_ws_client: Any,
    hass_client: Any,
) -> None:
    """FR-39, FR-40: the card script is served; the command returns a level."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    temp = placed_device(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "imatrix/floor_plan/level", "level_id": L0})
    msg = await ws.receive_json()
    assert msg["success"], msg
    assert msg["result"]["level"]["id"] == L0
    assert msg["result"]["markers"][0]["entities"] == [temp]
    assert msg["result"]["levels"][1]["id"] == L1
    await ws.send_json({"id": 2, "type": "imatrix/floor_plan/level", "level_id": "x"})
    assert (await ws.receive_json())["error"]["code"] == "invalid_format"
    await ws.send_json(
        {"id": 3, "type": "imatrix/floor_plan/level", "config_entry_id": "nope"}
    )
    assert (await ws.receive_json())["error"]["code"] == "not_found"
    plan_entry.runtime_data.client.get_floor_plan_level_card.side_effect = (
        IMatrixNotFoundError("404")
    )
    await ws.send_json({"id": 4, "type": "imatrix/floor_plan/level", "level_id": L9})
    assert (await ws.receive_json())["error"]["code"] == "invalid_format"
    plan_entry.runtime_data.floor_plan.cards._cache.clear()
    await ws.send_json({"id": 5, "type": "imatrix/floor_plan/level", "level_id": L9})
    assert (await ws.receive_json())["error"]["code"] == "unavailable"
    web = await hass_client()
    resp = await web.get("/imatrix_static/imatrix-floor-plan-card.js")
    assert resp.status == 200
    assert "imatrix-floor-plan-card" in await resp.text()
