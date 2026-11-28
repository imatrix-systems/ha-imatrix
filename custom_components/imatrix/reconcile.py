"""Follow iMatrix sensor visibility and order changes (spec 856 FR-14 to FR-17).

When a device's configVersion changes, its channel list is read again and its
sensor entities are brought in line:

- a channel hidden in iMatrix disables its entity, and showing it again enables it;
- a channel iMatrix no longer serves removes its entity;
- a new channel, or a channel whose name, unit or states changed, reloads the
  entry so the platforms build the entities again.

The user wins (D2): the integration changes an entity's enabled state only while
that state is still the one the integration last set. The last state set is kept
per unique id in a Store, so a user who enables a hidden sensor keeps it enabled.

A reorder changes no entity. Home Assistant sorts a device's entities by name
(spike S1). The order reaches dashboards through the custom:imatrix strategy.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from pyimatrix import Channel, Device

    from . import IMatrixConfigEntry

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY = 10


class VisibilityStore:
    """The enabled state the integration last set, per entity unique id."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create the store of one config entry."""
        self._store: Store[dict[str, bool]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.visibility"
        )
        self._data: dict[str, bool] = {}

    async def async_load(self) -> None:
        """Read the store from disk."""
        self._data = dict(await self._store.async_load() or {})

    def get(self, unique_id: str) -> bool | None:
        """Return the enabled state last set, or None when none was recorded."""
        return self._data.get(unique_id)

    @callback
    def set(self, unique_id: str, enabled: bool) -> None:
        """Record the enabled state the integration set."""
        if self._data.get(unique_id) is enabled:
            return
        self._data[unique_id] = enabled
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)

    @callback
    def forget(self, unique_id: str) -> None:
        """Drop the record of a removed entity."""
        if self._data.pop(unique_id, None) is not None:
            self._store.async_delay_save(lambda: self._data, SAVE_DELAY)

    async def async_remove(self) -> None:
        """Delete the store file (entry removed)."""
        await self._store.async_remove()


@dataclass
class ReconcileResult:
    """What one reconcile changed."""

    enabled: int = 0
    disabled: int = 0
    removed: int = 0
    kept_user_choice: int = 0
    reload: bool = False


def _definition(channel: Channel) -> tuple[object, ...]:
    """Return the parts of a channel an entity reads once, at creation."""
    return (
        channel.key,
        channel.name,
        channel.quantity_kind,
        channel.unit,
        channel.mode,
        channel.states,
        channel.state_count,
        channel.decimals,
    )


class Reconciler:
    """Bring devices' sensor entities in line with their iMatrix channel lists."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IMatrixConfigEntry,
        visibility: VisibilityStore,
        should_create: Callable[[Device, Channel], bool],
        base_enabled: Callable[[Device, Channel], bool],
    ) -> None:
        """Bind to one config entry and the sensor platform's rules."""
        self._hass = hass
        self._entry = entry
        self._visibility = visibility
        self._should_create = should_create
        self._base_enabled = base_enabled

    @callback
    def async_device(
        self, old: Device | None, new: Device, *, detect_added: bool = True
    ) -> ReconcileResult:
        """Reconcile one device. detect_added asks for a reload on new channels."""
        result = ReconcileResult()
        if new.config_version is None:
            return result  # FR-19: a server without spec 856
        registry = er.async_get(self._hass)
        channels = {c.sensor_id: c for c in new.channels}
        pattern = re.compile(rf"{new.sn}_(\d+)")
        present: set[int] = set()
        for reg in er.async_entries_for_config_entry(registry, self._entry.entry_id):
            if reg.domain != "sensor" or reg.platform != DOMAIN:
                continue
            match = pattern.fullmatch(reg.unique_id)
            if match is None:
                continue
            channel = channels.get(int(match.group(1)))
            if channel is None:
                # FR-14: iMatrix no longer serves this sensor (ui:false, or out of
                # the order).
                registry.async_remove(reg.entity_id)
                self._visibility.forget(reg.unique_id)
                result.removed += 1
                continue
            present.add(channel.sensor_id)
            if self._base_enabled(new, channel):
                self._apply_visibility(registry, reg, channel, result)
        if detect_added:
            result.reload = self._needs_reload(old, new, present)
        _LOGGER.debug(
            "Reconciled %s: %d enabled, %d disabled, %d removed, %d kept by user%s",
            new.sn,
            result.enabled,
            result.disabled,
            result.removed,
            result.kept_user_choice,
            ", reload" if result.reload else "",
        )
        return result

    @callback
    def _apply_visibility(
        self,
        registry: er.EntityRegistry,
        reg: er.RegistryEntry,
        channel: Channel,
        result: ReconcileResult,
    ) -> None:
        """Enable or disable one entity, unless the user changed it (D2)."""
        current = reg.disabled_by is None
        last = self._visibility.get(reg.unique_id)
        if last is None:
            last = current  # an entity from before 0.2.0: its state is the default
        if current != last:
            result.kept_user_choice += 1
            return
        if channel.visible == current:
            self._visibility.set(reg.unique_id, current)
            return
        if channel.visible:
            if reg.disabled_by is not er.RegistryEntryDisabler.INTEGRATION:
                result.kept_user_choice += 1
                return
            registry.async_update_entity(reg.entity_id, disabled_by=None)
            result.enabled += 1
        else:
            registry.async_update_entity(
                reg.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
            result.disabled += 1
        self._visibility.set(reg.unique_id, channel.visible)

    def _needs_reload(self, old: Device | None, new: Device, present: set[int]) -> bool:
        """Return True when a channel is new or its definition changed."""
        if any(
            c.sensor_id not in present and self._should_create(new, c)
            for c in new.channels
        ):
            return True
        old_defs = {c.sensor_id: _definition(c) for c in old.channels} if old else {}
        return any(
            c.sensor_id in old_defs and old_defs[c.sensor_id] != _definition(c)
            for c in new.channels
        )
