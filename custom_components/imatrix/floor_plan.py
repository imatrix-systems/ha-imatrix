"""Organise Home Assistant from an iMatrix floor plan (spec 822 FR-23 to FR-27).

The iMatrix cloud computes the desired HA state for a plan (floors, areas, labels,
device
assignments). This module links each desired object to what already exists in this Home
Assistant, builds a list of actions, and runs them only after the user confirms them in
a
repair. The id map lives in this Home Assistant's storage and is never sent to the
cloud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
ISSUE_PREFIX = "floor_plan_sync"
MANAGED_LABEL = "fpm:managed"


def _norm(name: str) -> str:
    return " ".join(name.casefold().split())


def area_key(area: dict[str, Any]) -> str:
    """Return the stable key of a desired area: its FPM object ids."""
    return ",".join(sorted(area.get("fpm_ids") or ()))


@dataclass
class Action:
    """One registry change: create or update a floor, area or label, or assign."""

    kind: str
    target: str
    key: str
    data: dict[str, Any] = field(default_factory=dict)
    existing_id: str | None = None

    def describe(self) -> str:
        """One line for the confirmation list."""
        name = self.data.get("name") or self.data.get("area_name") or self.key
        if self.kind == "assign":
            device = self.data.get("device_name")
            return f"Put device {device} in area {self.data.get('area_name')}"
        verb = "Create" if self.kind == "create" else "Update"
        return f"{verb} {self.target} {name}"


@dataclass
class FloorPlanSync:
    """Links and actions for one plan revision."""

    revision: int
    actions: list[Action]
    unmatched_devices: list[str]


class FloorPlanStore:
    """Map FPM ids to HA ids (FR-27): floors, areas and labels."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Bind the storage file of one config entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.floor_plan.{entry_id}"
        )
        self.data: dict[str, Any] = {
            "floors": {},
            "areas": {},
            "labels": {},
            "revision": None,
        }

    async def async_load(self) -> None:
        """Read the map from disk."""
        if stored := await self._store.async_load():
            self.data = {**self.data, **stored}

    async def async_save(self) -> None:
        """Write the map to disk."""
        await self._store.async_save(self.data)

    async def async_remove(self) -> None:
        """Delete the map (entry removed)."""
        await self._store.async_remove()


def _unique_name(desired: str, floor_short: str | None, taken: set[str]) -> str:
    """Return a name no unmanaged area uses (FR-25): add the floor, then a number."""
    if _norm(desired) not in taken:
        return desired
    base = f"{desired} ({floor_short})" if floor_short else desired
    name, n = base, 2
    while _norm(name) in taken:
        name = f"{base} {n}"
        n += 1
    return name


def _lookup(
    dev_reg: dr.DeviceRegistry,
    identifiers: set[tuple[str, str]] | None = None,
    connections: set[tuple[str, str]] | None = None,
) -> dr.DeviceEntry | None:
    """Find a device by identifier or connection on old and new registry APIs."""
    if hasattr(dev_reg, "async_get_devices"):
        found = dev_reg.async_get_devices(
            identifiers=identifiers, connections=connections
        )
        return found[0] if found else None
    return dev_reg.async_get_device(identifiers=identifiers, connections=connections)


def _device_for(hass: HomeAssistant, match_by: list[str]) -> Any:
    """Find the HA device an FPM device stands for (FR-26, source section 12.3.8)."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    for item in match_by:
        scheme, _, value = item.partition("=")
        device: Any = None
        if scheme == "imatrix_sn":
            sn = str(int(value)) if value.isdigit() else value
            device = _lookup(dev_reg, identifiers={(DOMAIN, sn)})
        elif scheme == "ha_device_id":
            device = dev_reg.async_get(value)
        elif scheme == "mac":
            mac = (dr.CONNECTION_NETWORK_MAC, dr.format_mac(value))
            device = _lookup(dev_reg, connections={mac})
        elif scheme == "zigbee_ieee":
            device = _lookup(dev_reg, connections={(dr.CONNECTION_ZIGBEE, value)})
        elif scheme == "ha_entity_id":
            entity = ent_reg.async_get(value)
            if entity and entity.device_id:
                device = dev_reg.async_get(entity.device_id)
        if device is not None:
            return device
    return None


def _link_labels(
    hass: HomeAssistant, desired: Any, store: FloorPlanStore, actions: list[Action]
) -> dict[str, str]:
    """Link desired labels by stored id, else by name; queue creates for the rest."""
    label_reg = lr.async_get(hass)
    ids: dict[str, str] = {}
    for label in desired.labels:
        key = label["label_id"]
        lid = store.data["labels"].get(key)
        existing = (lid and label_reg.async_get_label(lid)) or (
            label_reg.async_get_label_by_name(label["name"])
        )
        if existing:
            ids[key] = existing.label_id
            continue
        data = {
            "name": label["name"],
            "color": label.get("color"),
            "description": label.get("description"),
        }
        actions.append(Action("create", "label", key, data))
    return ids


def _link_floors(
    hass: HomeAssistant, desired: Any, store: FloorPlanStore, actions: list[Action]
) -> dict[str, str]:
    """Link desired floors by stored id, else by name; queue creates and updates."""
    floor_reg = fr.async_get(hass)
    ids: dict[str, str] = {}
    for floor in desired.floors:
        key = floor["fpm_id"]
        fid = store.data["floors"].get(key)
        existing = (fid and floor_reg.async_get_floor(fid)) or (
            floor_reg.async_get_floor_by_name(floor["name"])
        )
        data: dict[str, Any] = {
            "name": floor["name"],
            "level": floor.get("level"),
            "icon": floor.get("icon"),
            "aliases": set(floor.get("aliases") or ()),
        }
        if not existing:
            actions.append(Action("create", "floor", key, data))
            continue
        ids[key] = existing.floor_id
        changed = (
            existing.name != data["name"]
            or existing.level != data["level"]
            or set(existing.aliases) != data["aliases"]
        )
        if changed:
            actions.append(Action("update", "floor", key, data, existing.floor_id))
    return ids


def _unmanaged_names(hass: HomeAssistant, store: FloorPlanStore) -> set[str]:
    """Return the names of areas this plan does not manage (FR-25)."""
    area_reg = ar.async_get(hass)
    managed = lr.async_get(hass).async_get_label_by_name("Managed by FPM")
    linked = {a for a in store.data["areas"].values() if area_reg.async_get_area(a)}
    return {
        _norm(a.name)
        for a in area_reg.async_list_areas()
        if a.id not in linked and not (managed and managed.label_id in a.labels)
    }


def _find_area(hass: HomeAssistant, area: dict[str, Any], store: FloorPlanStore) -> Any:
    """Return the HA area for a desired area: stored id, else a managed area by name."""
    area_reg = ar.async_get(hass)
    aid = store.data["areas"].get(area_key(area))
    if aid and (existing := area_reg.async_get_area(aid)):
        return existing
    managed = lr.async_get(hass).async_get_label_by_name("Managed by FPM")
    found = area_reg.async_get_area_by_name(area["name"])
    return found if found and managed and managed.label_id in found.labels else None


def _link_areas(
    hass: HomeAssistant,
    desired: Any,
    store: FloorPlanStore,
    actions: list[Action],
    ids: tuple[dict[str, str], dict[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Link desired areas; queue creates (with free names) and updates."""
    label_ids, floor_ids = ids
    short = {
        f["floor_id_predicted"]: (f.get("aliases") or [None])[-1]
        for f in desired.floors
    }
    fpm_of_floor = {f["floor_id_predicted"]: f["fpm_id"] for f in desired.floors}
    taken = _unmanaged_names(hass, store)
    area_ids: dict[str, str] = {}
    names: dict[str, str] = {}
    for area in desired.areas:
        key = area_key(area)
        existing = _find_area(hass, area, store)
        name = (
            area["name"]
            if existing
            else _unique_name(area["name"], short.get(area.get("floor") or ""), taken)
        )
        renamed = {area["name"]} if name != area["name"] else set()
        data: dict[str, Any] = {
            "name": name,
            "floor_fpm": fpm_of_floor.get(area.get("floor") or ""),
            "aliases": set(area.get("aliases") or ()) | renamed,
            "labels": list(area.get("labels") or ()),
        }
        names[key] = name
        if not existing:
            taken.add(_norm(name))
            actions.append(Action("create", "area", key, data))
            continue
        area_ids[key] = existing.id
        want_floor = floor_ids.get(data["floor_fpm"] or "")
        want_labels = {label_ids.get(lab) for lab in data["labels"]}
        changed = (
            existing.name != name
            or set(existing.aliases) != data["aliases"]
            or (want_floor is not None and existing.floor_id != want_floor)
            or (None not in want_labels and set(existing.labels) != want_labels)
        )
        if changed:
            actions.append(Action("update", "area", key, data, existing.id))
    return area_ids, names


def build_sync(
    hass: HomeAssistant, desired: Any, store: FloorPlanStore
) -> FloorPlanSync:
    """Link desired objects to this HA and list the actions (source section 12.4).

    Order: labels, floors, areas, device assignments. Nothing is deleted: objects that
    leave the plan stay in Home Assistant for the user to remove.
    """
    actions: list[Action] = []
    label_ids = _link_labels(hass, desired, store, actions)
    floor_ids = _link_floors(hass, desired, store, actions)
    area_ids, names = _link_areas(hass, desired, store, actions, (label_ids, floor_ids))
    unmatched: list[str] = []
    area_by_name = {a["name"]: area_key(a) for a in desired.areas}
    for assignment in desired.device_assignments:
        key = area_by_name.get(assignment.get("area") or "")
        if key is None:
            continue
        device = _device_for(hass, list(assignment.get("match_by") or ()))
        if device is None:
            unmatched.append(assignment.get("device") or "")
            continue
        want = area_ids.get(key)
        if want is None or device.area_id != want:
            data = {
                "device_name": device.name_by_user or device.name,
                "area_key": key,
                "area_name": names.get(key),
            }
            actions.append(Action("assign", "device", device.id, data))
    return FloorPlanSync(desired.revision, actions, unmatched)


async def async_execute(
    hass: HomeAssistant, sync: FloorPlanSync, store: FloorPlanStore
) -> int:
    """Run the actions in order and record every created id (FR-27).

    A failure stops the run; what already ran is saved, so the next sync resumes from
    there.
    """
    floor_reg = fr.async_get(hass)
    area_reg = ar.async_get(hass)
    label_reg = lr.async_get(hass)
    dev_reg = dr.async_get(hass)
    done = 0
    try:
        for action in sync.actions:
            data = action.data
            if action.target == "label":
                entry = label_reg.async_create(
                    data["name"],
                    color=data.get("color"),
                    description=data.get("description"),
                )
                store.data["labels"][action.key] = entry.label_id
            elif action.target == "floor":
                kwargs = {
                    "aliases": data["aliases"],
                    "icon": data.get("icon"),
                    "level": data.get("level"),
                }
                if action.kind == "create":
                    floor = floor_reg.async_create(data["name"], **kwargs)
                else:
                    floor = floor_reg.async_update(
                        str(action.existing_id), name=data["name"], **kwargs
                    )
                store.data["floors"][action.key] = floor.floor_id
            elif action.target == "area":
                floor_id = store.data["floors"].get(data.get("floor_fpm") or "")
                labels = {
                    store.data["labels"][lab]
                    for lab in data["labels"]
                    if lab in store.data["labels"]
                }
                if action.kind == "create":
                    area = area_reg.async_create(
                        data["name"],
                        aliases=data["aliases"],
                        floor_id=floor_id,
                        labels=labels,
                    )
                else:
                    area = area_reg.async_update(
                        str(action.existing_id),
                        name=data["name"],
                        aliases=data["aliases"],
                        floor_id=floor_id,
                        labels=labels,
                    )
                store.data["areas"][action.key] = area.id
            elif action.target == "device":
                area_id = store.data["areas"].get(data["area_key"])
                if area_id:
                    dev_reg.async_update_device(action.key, area_id=area_id)
            done += 1
    finally:
        if done == len(sync.actions):
            store.data["revision"] = sync.revision
        await store.async_save()
    return done


def issue_id(entry_id: str) -> str:
    """Return the repair issue id of one config entry."""
    return f"{ISSUE_PREFIX}_{entry_id}"


def async_publish_issue(
    hass: HomeAssistant, entry_id: str, title: str, sync: FloorPlanSync
) -> None:
    """Offer the actions as a fixable repair, or clear it when nothing is left."""
    if not sync.actions:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))
        return
    lines = [a.describe() for a in sync.actions[:30]]
    if len(sync.actions) > 30:
        lines.append(f"and {len(sync.actions) - 30} more")
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry_id),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="floor_plan_sync",
        translation_placeholders={
            "title": title,
            "count": str(len(sync.actions)),
            "actions": "\n".join(f"- {line}" for line in lines),
        },
        data={"entry_id": entry_id},
    )


SYNC_INTERVAL_MINUTES = 5
SHARE_DEBOUNCE_SECONDS = 60


class FloorPlanManager:
    """Run the floor plan sync for one config entry.

    Checks the plan, offers the repair and applies it. Also uploads the device list
    when sharing is on (FR-29): when sharing starts, 60 seconds after the last device
    registry change, and once a day. Turning sharing off withdraws the list.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        client: Any,
        plan_id: str | None,
        share: bool,
    ) -> None:
        """Bind the entry, its client and the chosen plan."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.plan_id = plan_id
        self.share = share
        self.store = FloorPlanStore(hass, entry.entry_id)
        self.pending: FloorPlanSync | None = None
        self.instance_id: str | None = None
        self._unsubs: list[Any] = []
        self._share_timer: Any = None

    async def async_start(self) -> None:
        """Load the map, run a first check and schedule the next ones."""
        from datetime import timedelta  # noqa: PLC0415

        from homeassistant.helpers import instance_id  # noqa: PLC0415
        from homeassistant.helpers.event import async_track_time_interval  # noqa: PLC0415

        await self.store.async_load()
        self.instance_id = await instance_id.async_get(self.hass)
        if self.plan_id:
            await self.async_check()
            self._unsubs.append(
                async_track_time_interval(
                    self.hass,
                    self._async_tick,
                    timedelta(minutes=SYNC_INTERVAL_MINUTES),
                )
            )
        if self.share:
            await self.async_upload_devices()
            self._unsubs.append(
                self.hass.bus.async_listen(
                    dr.EVENT_DEVICE_REGISTRY_UPDATED, self._async_registry_changed
                )
            )
            self._unsubs.append(
                async_track_time_interval(
                    self.hass, self._async_daily_upload, timedelta(days=1)
                )
            )
        elif self.store.data.get("shared"):
            await self.async_withdraw_devices()

    def async_stop(self) -> None:
        """Cancel listeners and timers."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._share_timer is not None:
            self._share_timer()
            self._share_timer = None

    async def _async_tick(self, _now: Any) -> None:
        await self.async_check()

    async def async_check(self) -> None:
        """Rebuild the actions from the latest desired state and refresh the repair."""
        try:
            plans = {p.id: p for p in await self.client.get_floor_plans()}
        except Exception as err:
            _LOGGER.debug("floor plan list unavailable: %s", err)
            return
        plan = plans.get(self.plan_id or "")
        if plan is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry.entry_id))
            return
        try:
            desired = await self.client.get_floor_plan_desired(
                plan.id, instance_id=self.instance_id
            )
        except Exception as err:
            _LOGGER.debug("floor plan desired state unavailable: %s", err)
            return
        self.pending = build_sync(self.hass, desired, self.store)
        async_publish_issue(
            self.hass, self.entry.entry_id, self.entry.title, self.pending
        )

    async def async_apply(self) -> int:
        """Run the pending actions (called from the repair flow), then check again."""
        if self.pending is None:
            return 0
        done = await async_execute(self.hass, self.pending, self.store)
        self.pending = None
        await self.async_check()
        return done

    async def _async_registry_changed(self, _event: Any) -> None:
        from homeassistant.helpers.event import async_call_later  # noqa: PLC0415

        if self._share_timer is not None:
            self._share_timer()
        self._share_timer = async_call_later(
            self.hass, SHARE_DEBOUNCE_SECONDS, self._async_debounced_upload
        )

    async def _async_debounced_upload(self, _now: Any) -> None:
        self._share_timer = None
        await self.async_upload_devices()

    async def _async_daily_upload(self, _now: Any) -> None:
        await self.async_upload_devices()

    async def async_upload_devices(self) -> None:
        """FR-29: replace the shared list in the cloud."""
        from .floor_plan_share import build_device_list  # noqa: PLC0415

        if not self.instance_id:
            return
        try:
            await self.client.put_ha_devices(
                self.instance_id, build_device_list(self.hass)
            )
        except Exception as err:
            _LOGGER.warning("Could not share the device list with iMatrix: %s", err)
            return
        self.store.data["shared"] = True
        await self.store.async_save()

    async def async_withdraw_devices(self) -> None:
        """Delete the shared list in the cloud (FR-29)."""
        if not self.instance_id:
            from homeassistant.helpers import instance_id  # noqa: PLC0415

            self.instance_id = await instance_id.async_get(self.hass)
        try:
            await self.client.delete_ha_devices(self.instance_id)
        except Exception as err:
            _LOGGER.warning("Could not withdraw the shared device list: %s", err)
            return
        self.store.data["shared"] = False
        await self.store.async_save()
