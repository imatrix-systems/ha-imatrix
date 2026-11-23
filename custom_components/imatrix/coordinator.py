"""Data coordinator for the iMatrix integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyimatrix import (
    Device,
    DeviceState,
    IMatrixAuthError,
    IMatrixClient,
    IMatrixError,
    IMatrixPermissionError,
    IMatrixRateLimitError,
)

from .const import (
    CONF_GROUP_IDS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_REFRESH_EVERY,
    DOMAIN,
    MANUFACTURER,
    MAX_BACKOFF,
    WEB_APP_URL,
)

if TYPE_CHECKING:
    from . import IMatrixConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class IMatrixSnapshot:
    """Everything the entities read. Replaced, never mutated, on each update."""

    devices: dict[int, Device]
    states: dict[int, DeviceState]
    server_time: datetime | None = None
    group_paths: dict[int, str] = field(default_factory=dict)


class IMatrixCoordinator(DataUpdateCoordinator[IMatrixSnapshot]):
    """Polls the integration API. Delta polls with `since` between device refreshes."""

    config_entry: IMatrixConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: IMatrixConfigEntry, client: IMatrixClient
    ) -> None:
        """Create the coordinator with the interval from the options."""
        self._interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._interval,
        )
        self.client = client
        self._since_refresh = 0
        self._force_refresh = False
        # sns first seen in the latest update. Empty when nothing is new.
        self.new_sns: set[int] = set()
        self._known: set[int] = set()

    @property
    def _group_ids(self) -> list[int] | None:
        ids = self.config_entry.options.get(CONF_GROUP_IDS) or []
        return [int(i) for i in ids] or None

    @callback
    def async_add_new_device_listener(
        self, listener: Callable[[set[int]], None]
    ) -> CALLBACK_TYPE:
        """Call listener with the sns of devices that appear after setup.

        Runs as a coordinator listener, so the new snapshot is already in self.data.
        """

        @callback
        def on_update() -> None:
            if self.new_sns:
                listener(set(self.new_sns))

        return self.async_add_listener(on_update)

    async def _async_update_data(self) -> IMatrixSnapshot:
        try:
            snapshot = await self._async_fetch()
        except (IMatrixAuthError, IMatrixPermissionError) as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except IMatrixRateLimitError as err:
            self._back_off(err.retry_after)
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="rate_limited"
            ) from err
        except IMatrixError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="update_failed"
            ) from err
        self.update_interval = self._interval
        self._handle_device_changes(snapshot)
        return snapshot

    async def _async_fetch(self) -> IMatrixSnapshot:
        previous = self.data
        if (
            previous is None
            or previous.server_time is None
            or self._force_refresh
            or self._since_refresh >= DEVICE_REFRESH_EVERY
        ):
            listed = await self.client.get_devices(group_ids=self._group_ids)
            devices = {d.sn: d for d in listed}
            latest = await self.client.get_latest(devices)
            states = {sn: s for sn, s in latest.devices.items() if sn in devices}
            self._since_refresh = 0
            self._force_refresh = False
        else:
            self._since_refresh += 1
            devices = previous.devices
            latest = await self.client.get_latest(devices, since=previous.server_time)
            # `since` returns only changed devices, each in full.
            states = {
                **previous.states,
                **{sn: s for sn, s in latest.devices.items() if sn in devices},
            }
            if unknown := set(latest.devices) - set(devices):
                _LOGGER.debug("Latest named %d unknown devices", len(unknown))
                self._force_refresh = True  # re-list devices on the next poll
        return IMatrixSnapshot(
            devices=devices,
            states=states,
            server_time=latest.server_time,
            group_paths={
                d.group_id: d.group_path
                for d in devices.values()
                if d.group_id is not None and d.group_path
            },
        )

    def _back_off(self, retry_after: float | None) -> None:
        current = self.update_interval or self._interval
        wait = max(timedelta(seconds=retry_after or 0), current * 2)
        self.update_interval = min(wait, MAX_BACKOFF)

    @callback
    def _handle_device_changes(self, snapshot: IMatrixSnapshot) -> None:
        current = set(snapshot.devices)
        sync_device_registry(self.hass, self.config_entry, snapshot)
        if not self._known:
            self._known = current
            self.new_sns = set()
            return
        self.new_sns = current - self._known
        if removed := self._known - current:
            registry = dr.async_get(self.hass)
            for sn in removed:
                if device := registry.async_get_device_by_identifier(
                    (DOMAIN, str(sn)), self.config_entry.entry_id
                ):
                    registry.async_update_device(
                        device.id, remove_config_entry_id=self.config_entry.entry_id
                    )
        self._known = current


@callback
def sync_device_registry(
    hass: HomeAssistant, entry: IMatrixConfigEntry, snapshot: IMatrixSnapshot
) -> None:
    """Create or update one registry device per sn. Gateways go first for via_device."""
    registry = dr.async_get(hass)
    ordered = sorted(
        snapshot.devices.values(), key=lambda d: (d.via_sn is not None, d.sn)
    )
    for device in ordered:
        via = (
            registry.async_get_device_by_identifier(
                (DOMAIN, str(device.via_sn)), entry.entry_id
            )
            if device.via_sn in snapshot.devices
            else None
        )
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, str(device.sn))},
            name=device.name or f"iMatrix {device.sn}",
            manufacturer=MANUFACTURER,
            model=device.product_name,
            model_id=None if device.product_id is None else str(device.product_id),
            serial_number=str(device.sn),
            sw_version=device.firmware,
            configuration_url=f"{WEB_APP_URL}/dashboard/devices/{device.sn}",
            via_device_id=via.id if via is not None else None,
        )
