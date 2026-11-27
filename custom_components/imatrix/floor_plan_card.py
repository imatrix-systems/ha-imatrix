"""Floor plan levels for Home Assistant dashboards (spec 822 FR-33 to FR-35).

The cloud draws each level as SVG with one anchor per placed device and gives the
device positions as percentages of the drawing. This module caches those levels,
serves the SVG to dashboards and builds dashboard view configs the user pastes in.
Entities come from the local registries: nothing about HA entities goes to the cloud.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

import probatio as vol
from aiohttp import web
from homeassistant.components import frontend, websocket_api
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    websocket_command,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .floor_plan import _device_for

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pyimatrix import FloorPlanLevelCard, FloorPlanMarker

    from .floor_plan import FloorPlanStore

CARD_TTL_SECONDS = 300
SVG_URL = "/api/imatrix/floor_plan/{token}/{level_id}.svg"
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'"
CARD_PICTURE_ELEMENTS = "picture_elements"
CARD_HA_FLOORPLAN = "ha_floorplan"


def svg_url(token: str, level_id: str) -> str:
    """Return the dashboard image URL of one level."""
    return SVG_URL.format(token=token, level_id=level_id)


def entities_for(hass: HomeAssistant, marker: FloorPlanMarker) -> list[str]:
    """Return the HA entities of a placed device: hints first, then the device's own.

    The HA device is found the way the floor plan sync finds it (FR-26).
    """
    ent_reg = er.async_get(hass)
    out = [
        hint
        for hint in marker.entity_hints
        if ent_reg.async_get(hint) or hass.states.get(hint)
    ]
    match_by = [f"{i.get('scheme')}={i.get('value')}" for i in marker.identifiers]
    device = _device_for(hass, match_by) if match_by else None
    if device is not None:
        for entry in er.async_entries_for_device(ent_reg, device.id):
            if entry.disabled_by or entry.entity_category is not None:
                continue
            if entry.entity_id not in out:
                out.append(entry.entity_id)
    return out


class FloorPlanCards:
    """Fetch and cache the levels of one entry's plan (FR-34).

    A level is kept 5 minutes; a newer plan revision drops them all.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: Any,
        plan_id: str,
        store: FloorPlanStore,
        instance_id: str | None,
    ) -> None:
        """Bind the entry's client, plan and id map store."""
        self.hass = hass
        self.client = client
        self.plan_id = plan_id
        self.store = store
        self.instance_id = instance_id
        self._cache: dict[str, tuple[float, FloorPlanLevelCard]] = {}
        self._first_level: str | None = None
        self._revision: int | None = None
        self._token_lock = asyncio.Lock()

    def note_revision(self, revision: int) -> None:
        """Drop every cached level when the plan revision changes (FR-34)."""
        if self._revision is not None and revision != self._revision:
            self._cache.clear()
            self._first_level = None
        self._revision = revision

    async def async_token(self, *, rotate: bool = False) -> str:
        """Return the secret path part of the SVG URLs.

        It is made once per entry. ``rotate`` replaces it, which breaks every image
        URL handed out before (review F1).
        """
        async with self._token_lock:
            token = self.store.data.get("card_token")
            if not token or rotate:
                token = secrets.token_urlsafe(24)
                self.store.data["card_token"] = token
                await self.store.async_save()
            return str(token)

    def token_matches(self, token: str) -> bool:
        """Compare a URL token with this entry's in constant time."""
        mine = self.store.data.get("card_token")
        return bool(mine) and hmac.compare_digest(str(mine), token)

    async def _async_first_level(self) -> str:
        if self._first_level is None:
            desired = await self.client.get_floor_plan_desired(
                self.plan_id, instance_id=self.instance_id
            )
            floors = sorted(desired.floors, key=lambda f: f.get("level") or 0)
            if not floors:
                raise LookupError("the plan has no levels")
            self._first_level = str(floors[0]["fpm_id"])
        return self._first_level

    async def async_level(self, level_id: str | None = None) -> FloorPlanLevelCard:
        """Return one level, from the cache when it is under 5 minutes old."""
        level_id = level_id or await self._async_first_level()
        if not is_uuid(level_id):
            raise LookupError("not a level id")
        cached = self._cache.get(level_id)
        if cached and time.monotonic() - cached[0] < CARD_TTL_SECONDS:
            return cached[1]
        card: FloorPlanLevelCard = await self.client.get_floor_plan_level_card(
            self.plan_id, level_id, instance_id=self.instance_id
        )
        self.note_revision(card.revision)
        self._cache[level_id] = (time.monotonic(), card)
        return card

    def knows(self, level_id: str) -> bool:
        """Return whether a cached level lists this level id (or nothing is cached)."""
        listed = {lv.id for _t, card in self._cache.values() for lv in card.levels}
        return not listed or level_id in listed

    async def async_all_levels(self) -> list[FloorPlanLevelCard]:
        """Return every level of the plan, by building and ordinal."""
        first = await self.async_level()
        out = []
        for ref in first.levels:
            out.append(
                first if ref.id == first.level.id else await self.async_level(ref.id)
            )
        return out


def is_uuid(value: str) -> bool:
    """Return whether a value is a UUID, the only form FPM level ids take."""
    try:
        uuid.UUID(value)
    except ValueError, TypeError, AttributeError:
        return False
    return len(value) == 36


async def async_dashboard(
    hass: HomeAssistant,
    cards: FloorPlanCards,
    card_type: str,
    level_id: str | None,
    *,
    new_link: bool = False,
) -> dict[str, Any]:
    """Build the FR-35 dashboard views, one per level."""
    token = await cards.async_token(rotate=new_link)
    several_buildings = (
        len({lv.building_id for lv in (await cards.async_level()).levels}) > 1
    )
    levels = (
        [await cards.async_level(level_id)]
        if level_id
        else await cards.async_all_levels()
    )
    views = []
    for level in levels:
        image = svg_url(token, level.level.id)
        placed = [(m, entities_for(hass, m)) for m in level.markers]
        placed = [(m, ents) for m, ents in placed if ents]
        if card_type == CARD_PICTURE_ELEMENTS:
            card: dict[str, Any] = {
                "type": "picture-elements",
                "image": image,
                "elements": [
                    {
                        "type": "state-icon",
                        "entity": ents[0],
                        "title": m.name,
                        "style": {"left": f"{m.x}%", "top": f"{m.y}%"},
                    }
                    for m, ents in placed
                ],
            }
        else:
            card = {
                "type": "custom:floorplan-card",
                "full_height": True,
                "config": {
                    "image": image,
                    "rules": [
                        {
                            "entity": ents[0],
                            "element": f"fpm-{m.placement_id}",
                            "tap_action": "more-info",
                            "hover_action": "hover-info",
                        }
                        for m, ents in placed
                    ],
                },
            }
        title = level.level.name
        if several_buildings and level.level.building_name:
            title = f"{level.level.building_name} {title}"
        views.append(
            {
                "title": title,
                # Level ids are unique; names repeat across buildings (review F4).
                "path": f"imatrix-{level.level.id}",
                "type": "panel",
                "cards": [card],
            }
        )
    return {"views": views}


def _entries_with_cards(hass: HomeAssistant) -> list[tuple[Any, FloorPlanCards]]:
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        manager = getattr(entry.runtime_data, "floor_plan", None)
        if manager is not None and manager.cards is not None:
            out.append((entry, manager.cards))
    return out


def _cards_of(hass: HomeAssistant) -> list[FloorPlanCards]:
    return [cards for _entry, cards in _entries_with_cards(hass)]


class FloorPlanSvgView(HomeAssistantView):
    """Serve level SVGs to dashboards (FR-34).

    Picture elements and ha-floorplan load the image without the user's token, so
    the URL carries a per-entry secret instead, as HA's camera proxy does.
    """

    url = "/api/imatrix/floor_plan/{token}/{level_id}.svg"
    name = "api:imatrix:floor_plan_svg"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind hass."""
        self.hass = hass

    async def get(
        self, _request: web.Request, token: str, level_id: str
    ) -> web.Response:
        """Return the SVG of one level, or 404."""
        if not is_uuid(level_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        for cards in _cards_of(self.hass):
            if not cards.token_matches(token):
                continue
            try:
                card = await cards.async_level(level_id)
            except Exception:
                break
            return web.Response(
                text=card.svg,
                content_type="image/svg+xml",
                headers={
                    "Cache-Control": f"private, max-age={CARD_TTL_SECONDS}",
                    "Content-Security-Policy": SVG_CSP,
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return web.Response(status=HTTPStatus.NOT_FOUND)


CARD_JS_URL = "/imatrix_static/imatrix-floor-plan-card.js"


def card_payload(hass: HomeAssistant, card: FloorPlanLevelCard) -> dict[str, Any]:
    """Return a level for the card (FR-40): FR-33's fields plus local entities."""

    def ref(lv: Any) -> dict[str, Any]:
        return {
            "id": lv.id,
            "name": lv.name,
            "ordinal": lv.ordinal,
            "buildingId": lv.building_id,
            "buildingName": lv.building_name,
        }

    return {
        "revision": card.revision,
        "level": ref(card.level),
        "levels": [ref(lv) for lv in card.levels],
        "svg": card.svg,
        "markers": [
            {
                "placementId": m.placement_id,
                "name": m.name,
                "category": m.category,
                "x": m.x,
                "y": m.y,
                "entities": entities_for(hass, m),
            }
            for m in card.markers
        ],
    }


@websocket_command(
    {
        vol.Required("type"): "imatrix/floor_plan/level",
        vol.Optional("config_entry_id"): str,
        vol.Optional("level_id"): str,
    }
)
@async_response
async def ws_floor_plan_level(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer the card with one level of the entry's plan (FR-40)."""
    entry_id = msg.get("config_entry_id")
    found = [
        (entry, cards)
        for entry, cards in _entries_with_cards(hass)
        if entry_id is None or entry.entry_id == entry_id
    ]
    if not found:
        connection.send_error(
            msg["id"], "not_found", "No iMatrix entry with a floor plan was found."
        )
        return
    level_id = msg.get("level_id")
    cards = found[0][1]
    # Any signed-in HA user may read the plan: they already see every state. Only
    # level ids the plan lists reach the cloud, so the command cannot be used to
    # send unbounded cloud requests (review F5).
    if level_id is not None and (not is_uuid(level_id) or not cards.knows(level_id)):
        connection.send_error(
            msg["id"], "invalid_format", "level_id is not a level id."
        )
        return
    try:
        card = await cards.async_level(level_id)
    except Exception:
        connection.send_error(
            msg["id"], "unavailable", "The floor plan could not be loaded."
        )
        return
    connection.send_result(msg["id"], card_payload(hass, card))


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card script and load it on every dashboard (FR-39)."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                CARD_JS_URL,
                str(Path(__file__).parent / "www" / "imatrix-floor-plan-card.js"),
                True,
            )
        ]
    )
    websocket_api.async_register_command(hass, ws_floor_plan_level)
    # The frontend loads before us when it is present (after_dependencies).
    if frontend.DATA_EXTRA_MODULE_URL in hass.data:
        # Versioned URL so browsers drop a cached card after an upgrade (review F8).
        version = (await async_get_integration(hass, DOMAIN)).version
        frontend.add_extra_js_url(hass, f"{CARD_JS_URL}?v={version}")
