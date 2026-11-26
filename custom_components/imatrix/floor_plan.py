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

import asyncio
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
from pyimatrix import IMatrixPermissionError

from .const import AUTH_TYPE_OAUTH2, CONF_AUTH_TYPE, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
ISSUE_PREFIX = "floor_plan_sync"
MANAGED_LABEL = "fpm:managed"
MANAGED_LABEL_NAME = "Managed by FPM"


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
    # Existing HA objects linked by name this run (F2): recorded on apply.
    links: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"floors": {}, "areas": {}, "labels": {}}
    )


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
            # Floors the user already had, linked by name: used, never edited.
            "user_floors": [],
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
    hass: HomeAssistant,
    desired: Any,
    store: FloorPlanStore,
    actions: list[Action],
    links: dict[str, str],
) -> dict[str, str]:
    """Link desired labels by stored id, else by name; queue creates for the rest.

    A label found by name is used as it is and never edited.
    """
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
            if lid != existing.label_id:
                links[key] = existing.label_id
            continue
        data = {
            "name": label["name"],
            "color": label.get("color"),
            "description": label.get("description"),
        }
        actions.append(Action("create", "label", key, data))
    return ids


def _link_floors(
    hass: HomeAssistant,
    desired: Any,
    store: FloorPlanStore,
    actions: list[Action],
    links: dict[str, str],
) -> dict[str, str]:
    """Link desired floors by stored id, else by name; queue creates and updates.

    Only a floor this plan created is updated. A floor found by name belongs to the
    user (HA floors have no labels to mark them managed), so it is used, never edited.
    """
    floor_reg = fr.async_get(hass)
    ids: dict[str, str] = {}
    for floor in desired.floors:
        key = floor["fpm_id"]
        fid = store.data["floors"].get(key)
        owned = (
            fid
            and fid not in store.data["user_floors"]
            and floor_reg.async_get_floor(fid)
        )
        existing = owned or floor_reg.async_get_floor_by_name(floor["name"])
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
        if not owned:
            links[key] = existing.floor_id
            continue
        changed = (
            existing.name != data["name"]
            or existing.level != data["level"]
            or set(existing.aliases) != data["aliases"]
        )
        if changed:
            actions.append(Action("update", "floor", key, data, existing.floor_id))
    return ids


def _managed_label_id(hass: HomeAssistant, store: FloorPlanStore) -> str | None:
    """Return the HA id of the plan's managed label, if it exists."""
    label_reg = lr.async_get(hass)
    lid = store.data["labels"].get(MANAGED_LABEL)
    label = (lid and label_reg.async_get_label(lid)) or (
        label_reg.async_get_label_by_name(MANAGED_LABEL_NAME)
    )
    return label.label_id if label else None


def _is_managed(area: Any, managed: str | None, linked: set[str]) -> bool:
    return area.id in linked or bool(managed and managed in area.labels)


def _unmanaged_names(hass: HomeAssistant, store: FloorPlanStore) -> set[str]:
    """Return the names of areas this plan does not manage (FR-25)."""
    area_reg = ar.async_get(hass)
    managed = _managed_label_id(hass, store)
    linked = set(store.data["areas"].values())
    return {
        _norm(a.name)
        for a in area_reg.async_list_areas()
        if not _is_managed(a, managed, linked)
    }


def _find_area(
    hass: HomeAssistant, area: dict[str, Any], store: FloorPlanStore, used: set[str]
) -> Any:
    """Return the HA area for a desired area (FR-23).

    The stored id first, else a managed area with the desired name or, when it was
    disambiguated (FR-25), with the desired name as an alias.
    """
    area_reg = ar.async_get(hass)
    aid = store.data["areas"].get(area_key(area))
    if aid and (existing := area_reg.async_get_area(aid)):
        return existing
    managed = _managed_label_id(hass, store)
    if managed is None:
        return None
    want = _norm(area["name"])
    candidates = [
        a
        for a in area_reg.async_list_areas()
        if managed in a.labels and a.id not in used
    ]
    for a in candidates:
        if _norm(a.name) == want:
            return a
    for a in candidates:
        if want in {_norm(x) for x in a.aliases}:
            return a
    return None


def _link_areas(
    hass: HomeAssistant,
    desired: Any,
    store: FloorPlanStore,
    actions: list[Action],
    ids: tuple[dict[str, str], dict[str, str], dict[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Link desired areas; queue creates (with free names) and updates.

    Updates only add: the plan's aliases and labels are merged into what the area
    already has, and an area keeps its floor when the plan names none.
    """
    label_ids, floor_ids, links = ids
    short = {
        f["floor_id_predicted"]: (f.get("aliases") or [None])[-1]
        for f in desired.floors
    }
    fpm_of_floor = {f["floor_id_predicted"]: f["fpm_id"] for f in desired.floors}
    taken = _unmanaged_names(hass, store)
    used: set[str] = set()
    area_ids: dict[str, str] = {}
    names: dict[str, str] = {}
    for area in desired.areas:
        key = area_key(area)
        existing = _find_area(hass, area, store, used)
        floor_short = short.get(area.get("floor") or "")
        if existing is not None:
            used.add(existing.id)
            # Its own name is never a clash with itself.
            name = _unique_name(
                area["name"], floor_short, taken - {_norm(existing.name)}
            )
        else:
            name = _unique_name(area["name"], floor_short, taken)
        taken.add(_norm(name))
        renamed = {area["name"]} if name != area["name"] else set()
        data: dict[str, Any] = {
            "name": name,
            "floor_fpm": fpm_of_floor.get(area.get("floor") or ""),
            "aliases": set(area.get("aliases") or ()) | renamed,
            "labels": list(area.get("labels") or ()),
        }
        names[key] = name
        if existing is None:
            actions.append(Action("create", "area", key, data))
            continue
        area_ids[key] = existing.id
        if store.data["areas"].get(key) != existing.id:
            links[key] = existing.id
        want_floor = floor_ids.get(data["floor_fpm"] or "")
        want_labels = {label_ids.get(lab) for lab in data["labels"]}
        changed = (
            existing.name != name
            or not data["aliases"] <= set(existing.aliases)
            or (want_floor is not None and existing.floor_id != want_floor)
            or (None not in want_labels and not want_labels <= set(existing.labels))
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
    links: dict[str, dict[str, str]] = {"floors": {}, "areas": {}, "labels": {}}
    label_ids = _link_labels(hass, desired, store, actions, links["labels"])
    floor_ids = _link_floors(hass, desired, store, actions, links["floors"])
    area_ids, names = _link_areas(
        hass, desired, store, actions, (label_ids, floor_ids, links["areas"])
    )
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
    return FloorPlanSync(desired.revision, actions, unmatched, links)


def _run_floor(hass: HomeAssistant, action: Action, store: FloorPlanStore) -> None:
    floor_reg = fr.async_get(hass)
    data = action.data
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


def _run_area(hass: HomeAssistant, action: Action, store: FloorPlanStore) -> None:
    area_reg = ar.async_get(hass)
    data = action.data
    floor_id = store.data["floors"].get(data.get("floor_fpm") or "")
    labels = {
        store.data["labels"][lab]
        for lab in data["labels"]
        if lab in store.data["labels"]
    }
    if action.kind == "create":
        area = area_reg.async_create(
            data["name"], aliases=data["aliases"], floor_id=floor_id, labels=labels
        )
    else:
        current = area_reg.async_get_area(str(action.existing_id))
        if current is None:
            raise ValueError(f"area {data['name']} was removed")
        area = area_reg.async_update(
            current.id,
            name=data["name"],
            aliases=set(current.aliases) | data["aliases"],
            floor_id=floor_id or current.floor_id,
            labels=set(current.labels) | labels,
        )
    store.data["areas"][action.key] = area.id


def _run_action(hass: HomeAssistant, action: Action, store: FloorPlanStore) -> None:
    data = action.data
    if action.target == "label":
        entry = lr.async_get(hass).async_create(
            data["name"], color=data.get("color"), description=data.get("description")
        )
        store.data["labels"][action.key] = entry.label_id
    elif action.target == "floor":
        _run_floor(hass, action, store)
    elif action.target == "area":
        _run_area(hass, action, store)
    elif action.target == "device":
        area_id = store.data["areas"].get(data["area_key"])
        if not area_id:
            raise ValueError(f"no area for {data.get('device_name')}")
        dr.async_get(hass).async_update_device(action.key, area_id=area_id)


async def async_execute(
    hass: HomeAssistant, sync: FloorPlanSync, store: FloorPlanStore
) -> int:
    """Run the actions in order and record every created id (FR-27).

    A failure stops the run; what already ran is saved, so the next sync resumes from
    there.
    """
    for kind, linked in sync.links.items():
        store.data[kind].update(linked)
    user_floors = set(store.data["user_floors"]) | set(sync.links["floors"].values())
    store.data["user_floors"] = sorted(user_floors)
    done = 0
    try:
        for action in sync.actions:
            _run_action(hass, action, store)
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
        self._stopped = False
        # One lock per entry, shared by every manager it ever has, so an upload still
        # in flight finishes before a later withdrawal starts (FR-29).
        locks = hass.data.setdefault(f"{DOMAIN}_share_locks", {})
        self._share_lock: asyncio.Lock = locks.setdefault(
            entry.entry_id, asyncio.Lock()
        )

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
        """Cancel listeners and timers; later uploads from this manager do nothing."""
        self._stopped = True
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

    async def async_apply(self, sync: FloorPlanSync | None = None) -> bool:
        """Run the actions the user confirmed, then check again (FR-24).

        `sync` is the list the repair showed; if a newer check replaced it, nothing
        runs and the repair shows the new list. Returns False when nothing ran or a
        step failed; what ran is kept and the next check starts from there.
        """
        if sync is None or sync is not self.pending:
            return False
        self.pending = None
        try:
            done = await async_execute(self.hass, sync, self.store)
        except Exception:
            _LOGGER.exception("Applying the floor plan stopped part way")
            done = -1
        await self.async_check()
        return done == len(sync.actions)

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
        async with self._share_lock:
            if self._stopped:
                return
            try:
                await self.client.put_ha_devices(
                    self.instance_id, build_device_list(self.hass)
                )
            except IMatrixPermissionError:
                self._async_permission_refused()
                return
            except Exception as err:
                _LOGGER.warning("Could not share the device list with iMatrix: %s", err)
                return
            self.store.data["shared"] = True
            await self.store.async_save()

    def _async_permission_refused(self) -> None:
        """FR-30: the credential lacks device sharing.

        A sign-in is asked to sign in again with the sharing scope. An API key needs
        the sharing preset, which the repair explains.
        """
        if self.entry.data.get(CONF_AUTH_TYPE) == AUTH_TYPE_OAUTH2:
            self.entry.async_start_reauth(self.hass)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"share_devices_key_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="share_devices_key",
            translation_placeholders={"title": self.entry.title},
        )

    async def async_withdraw_devices(self) -> bool:
        """Delete the shared list in the cloud (FR-29)."""
        if not self.instance_id:
            from homeassistant.helpers import instance_id  # noqa: PLC0415

            self.instance_id = await instance_id.async_get(self.hass)
        async with self._share_lock:
            try:
                await self.client.delete_ha_devices(self.instance_id)
            except Exception as err:
                _LOGGER.warning(
                    "Could not withdraw the shared device list; iMatrix deletes it "
                    "after 30 days without an update: %s",
                    err,
                )
                return False
            self.store.data["shared"] = False
            await self.store.async_save()
            return True
