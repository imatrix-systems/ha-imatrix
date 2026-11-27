"""Report Home Assistant changes back to the iMatrix plan (spec 822 FR-41 to FR-47).

The integration watches the floors, areas and devices it linked to the plan. A rename,
a move to another floor, a deleted area or a device moved to another area is reported
to the cloud with plan ids only. Which changes are sent depends on who owns names:

- the plan (default): nothing is sent; the next sync proposes the plan's names again.
- Home Assistant names, or both ways: renames are applied in the plan; every other
  change waits there for a person.

New unmanaged areas are offered to the plan only when the user turns that on.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.event import async_call_later

from .const import OWNERSHIP_PLAN
from .floor_plan import _device_for, area_key

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

    from .floor_plan import FloorPlanManager

_LOGGER = logging.getLogger(__name__)
DEBOUNCE = timedelta(seconds=10)
RETRY = timedelta(seconds=60)


def _fields(cls: Any) -> set[str]:
    """Return the field names of a registry entry class (attrs or dataclass)."""
    attrs_fields = getattr(cls, "__attrs_attrs__", None)
    if attrs_fields is not None:
        return {a.name for a in attrs_fields}
    if dataclasses.is_dataclass(cls):
        return {f.name for f in dataclasses.fields(cls)}
    return set()


def probe_capabilities() -> dict[str, bool]:
    """FR-47: what this HA version's registries can hold, for later projections."""
    area = _fields(ar.AreaEntry)
    floor = _fields(fr.FloorEntry)
    return {
        "supports_nested_areas": "parent_id" in area,
        "supports_area_types": "type" in area or "area_type" in area,
        "supports_floor_level": "level" in floor,
    }


class HaChangeTracker:
    """Watch linked registry entries and send what changed (FR-41)."""

    def __init__(
        self, manager: FloorPlanManager, ownership: str, offer_new_areas: bool
    ) -> None:
        """Bind the manager whose plan and id map are watched."""
        self.manager = manager
        self.hass: HomeAssistant = manager.hass
        self.ownership = ownership
        self.offer_new_areas = offer_new_areas
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}
        # Raw events wait for the debounce: the integration's own writes fire events
        # before its id map is updated, so they are classified only at flush time.
        self.raw: list[tuple[str, dict[str, Any]]] = []
        self._timer: Any = None
        self._unsubs: list[Any] = []

    @property
    def active(self) -> bool:
        """Return whether anything would ever be sent."""
        return self.ownership != OWNERSHIP_PLAN or self.offer_new_areas

    def start(self) -> None:
        """Listen to the three registries."""
        if not self.active:
            return
        bus = self.hass.bus
        self._unsubs = [
            bus.async_listen(fr.EVENT_FLOOR_REGISTRY_UPDATED, self._floor_event),
            bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, self._area_event),
            bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_event),
        ]

    def stop(self) -> None:
        """Stop listening and drop unsent changes."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._timer is not None:
            self._timer()
            self._timer = None
        self.pending.clear()
        self.raw.clear()

    # ---------------------------------------------------------------- lookups

    @property
    def _store(self) -> dict[str, Any]:
        return self.manager.store.data

    def _desired_floor(self, fpm_id: str) -> dict[str, Any] | None:
        desired = self.manager.desired
        if desired is None:
            return None
        return next((f for f in desired.floors if f.get("fpm_id") == fpm_id), None)

    def _desired_area(self, key: str) -> dict[str, Any] | None:
        desired = self.manager.desired
        if desired is None:
            return None
        return next((a for a in desired.areas if area_key(a) == key), None)

    def _floor_fpm(self, floor_id: str | None) -> str | None:
        """Return the one level behind an HA floor; none when several share it (F7)."""
        found = [f for f, ha in self._store["floors"].items() if ha == floor_id]
        return str(found[0]) if len(found) == 1 else None

    def _area_key(self, area_id: str | None) -> str | None:
        for key, ha_id in self._store["areas"].items():
            if ha_id == area_id:
                return str(key)
        return None

    def _single_space(self, key: str | None) -> str | None:
        """Return the one space behind an area; merged areas name no single space."""
        area = self._desired_area(key) if key else None
        ids = list(area.get("fpm_ids") or ()) if area else []
        return str(ids[0]) if len(ids) == 1 else None

    def _expected_area_name(self, key: str, desired_name: str) -> str:
        return self.manager.expected_names.get(key, desired_name)

    @staticmethod
    def _plan_name(ha_name: str, expected: str, desired: str) -> str:
        """Drop the disambiguation suffix the integration added (review F2).

        "Bathroom (F1)" renamed to "Bath (F1)" sends "Bath".
        """
        if expected != desired and expected.startswith(desired):
            suffix = expected[len(desired) :]
            if ha_name.endswith(suffix) and len(ha_name) > len(suffix):
                return ha_name[: -len(suffix)].rstrip()
        return ha_name

    # ---------------------------------------------------------------- events

    def _queue(self, kind: str, key: str, change: dict[str, Any]) -> None:
        if kind == "new_area":
            if not self.offer_new_areas:
                return
        elif self.ownership == OWNERSHIP_PLAN:
            return
        self.pending[(kind, key)] = {"kind": kind, **change}

    def _note(self, registry: str, data: dict[str, Any]) -> None:
        self.raw.append((registry, dict(data)))
        if self._timer is not None:
            self._timer()
        self._timer = async_call_later(self.hass, DEBOUNCE, self._flush)

    @callback
    def _floor_event(self, event: Event[Any]) -> None:
        self._note("floor", event.data)

    @callback
    def _area_event(self, event: Event[Any]) -> None:
        self._note("area", event.data)

    @callback
    def _device_event(self, event: Event[Any]) -> None:
        self._note("device", event.data)

    def classify(self) -> None:
        """Turn the noted events into changes, with the id map as it is now."""
        raw, self.raw = self.raw, []
        handlers = {
            "floor": self._floor_change,
            "area": self._area_change,
            "device": self._device_change,
        }
        for registry, data in raw:
            handlers[registry](data)

    def _floor_change(self, data: dict[str, Any]) -> None:
        if data.get("action") != "update":
            return
        fpm_id = self._floor_fpm(data.get("floor_id"))
        if fpm_id is None or fpm_id in self._store.get("user_floors", []):
            return
        floor = fr.async_get(self.hass).async_get_floor(data["floor_id"])
        desired = self._desired_floor(fpm_id)
        if floor is None or desired is None:
            return
        if floor.name != desired["name"]:
            self._queue("rename", fpm_id, {"fpmId": fpm_id, "name": floor.name})

    def _area_change(self, data: dict[str, Any]) -> None:
        action = data.get("action")
        area_id = str(data.get("area_id") or "")
        key = self._area_key(area_id)
        if action == "create" and key is None:
            area = ar.async_get(self.hass).async_get_area(area_id)
            if area is not None:
                self._queue("new_area", area.id, {"haAreaName": area.name[:255]})
            return
        space = self._single_space(key)
        if key is None or space is None:
            return
        if action == "remove":
            self._queue("area_deleted", space, {"fpmId": space})
            return
        if action != "update":
            return
        area = ar.async_get(self.hass).async_get_area(area_id)
        desired = self._desired_area(key)
        if area is None or desired is None:
            return
        expected = self._expected_area_name(key, desired["name"])
        if area.name != expected:
            name = self._plan_name(area.name, expected, desired["name"])
            if name != desired["name"]:
                self._queue("rename", space, {"fpmId": space, "name": name[:255]})
        wanted = self._store["floors"].get(desired.get("floor_fpm") or "")
        moved_to = self._floor_fpm(area.floor_id)
        if area.floor_id and area.floor_id != wanted and moved_to is not None:
            self._queue("level_move", space, {"fpmId": space, "levelId": moved_to})

    def _device_change(self, data: dict[str, Any]) -> None:
        if data.get("action") != "update" or "area_id" not in (
            data.get("changes") or {}
        ):
            return
        device = dr.async_get(self.hass).async_get(data["device_id"])
        desired = self.manager.desired
        if device is None or desired is None:
            return
        # Moved out of a deleted area: not a move of the device (review F1).
        if device.area_id is None:
            return
        area_key_by_name = {a["name"]: area_key(a) for a in desired.areas}
        for assignment in desired.device_assignments:
            found = _device_for(self.hass, list(assignment.get("match_by") or ()))
            if found is None or found.id != device.id:
                continue
            # Our own sync put it in its planned area: not a move either.
            planned = self._store["areas"].get(
                area_key_by_name.get(assignment.get("area") or "") or ""
            )
            if planned == device.area_id:
                return
            fpm_id = str(assignment["device_fpm_id"])
            change: dict[str, Any] = {"fpmId": fpm_id}
            to_space = self._single_space(self._area_key(device.area_id))
            if to_space:
                change["toFpmId"] = to_space
            self._queue("device_moved", fpm_id, change)
            return

    async def _flush(self, _now: Any = None) -> None:
        if self._timer is not None:
            self._timer()  # a direct call cancels the scheduled one
            self._timer = None
        self.classify()
        changes = list(self.pending.values())
        self.pending.clear()
        if changes and not await self.manager.async_send_changes(changes):
            # Not sent: keep them (newer changes for the same object win) and retry.
            for change in changes:
                key = change.get("fpmId") or change.get("haAreaName") or ""
                self.pending.setdefault((change["kind"], key), change)
            self._timer = async_call_later(self.hass, RETRY, self._flush)
