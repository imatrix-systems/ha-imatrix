"""Floor plan levels for Home Assistant dashboards (spec 822 FR-33 to FR-35).

The cloud draws each level as SVG with one anchor per placed device and gives the
device positions as percentages of the drawing. This module caches those levels,
serves the SVG to dashboards and builds dashboard view configs the user pastes in.
Entities come from the local registries: nothing about HA entities goes to the cloud.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import HomeAssistantView

from .const import DOMAIN
from .floor_plan import _device_for

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pyimatrix import FloorPlanLevelCard, FloorPlanMarker

    from .floor_plan import FloorPlanStore

CARD_TTL_SECONDS = 300
SVG_URL = "/api/imatrix/floor_plan/{token}/{level_id}.svg"
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

    async def async_token(self) -> str:
        """Return the secret path part of the SVG URLs, creating it once per entry."""
        token = self.store.data.get("card_token")
        if not token:
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
        cached = self._cache.get(level_id)
        if cached and time.monotonic() - cached[0] < CARD_TTL_SECONDS:
            return cached[1]
        card: FloorPlanLevelCard = await self.client.get_floor_plan_level_card(
            self.plan_id, level_id, instance_id=self.instance_id
        )
        if cached and cached[1].revision != card.revision:
            self._cache.clear()
        self._cache[level_id] = (time.monotonic(), card)
        return card

    async def async_all_levels(self) -> list[FloorPlanLevelCard]:
        """Return every level of the plan's building, by ordinal."""
        first = await self.async_level()
        out = []
        for ref in first.levels:
            out.append(
                first if ref.id == first.level.id else await self.async_level(ref.id)
            )
        return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "level"


async def async_dashboard(
    hass: HomeAssistant, cards: FloorPlanCards, card_type: str, level_id: str | None
) -> dict[str, Any]:
    """Build the FR-35 dashboard views, one per level."""
    token = await cards.async_token()
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
        views.append(
            {
                "title": level.level.name,
                "path": f"imatrix-{_slug(level.level.name)}",
                "type": "panel",
                "cards": [card],
            }
        )
    return {"views": views}


def _cards_of(hass: HomeAssistant) -> list[FloorPlanCards]:
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        manager = getattr(entry.runtime_data, "floor_plan", None)
        if manager is not None and manager.cards is not None:
            out.append(manager.cards)
    return out


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
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return web.Response(status=HTTPStatus.NOT_FOUND)
