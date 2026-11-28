"""Data coordinator for the iMatrix integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from pyimatrix import (
    ConnectivityFrame,
    Device,
    DeviceKind,
    DeviceState,
    Event,
    EventFrame,
    EventType,
    Fault,
    IMatrixAuthError,
    IMatrixClient,
    IMatrixConnectionError,
    IMatrixError,
    IMatrixNotFoundError,
    IMatrixPermissionError,
    IMatrixRateLimitError,
    IMatrixResponseError,
    Lamps,
    PositionFrame,
    ValuesFrame,
)
from pyimatrix import Frame as StreamFrame

from .const import (
    CONF_GROUP_IDS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_REFRESH_EVERY,
    DOMAIN,
    FAULT_FETCHES_PER_POLL,
    MANUFACTURER,
    MAX_BACKOFF,
    MAX_EVENT_PAGES,
    PUSH_POLL_INTERVAL,
    RATE_LIMIT_ISSUE_AFTER,
    TRANSIENT_FAILURES_TOLERATED,
    TRANSIENT_RETRY_INTERVAL,
    WEB_APP_URL,
)

if TYPE_CHECKING:
    from . import IMatrixConfigEntry
    from .backfill import StatisticsBackfill
    from .push import IMatrixPush

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FaultState:
    """Active faults and lamps of one vehicle, kept current from the event feed."""

    lamps: Lamps | None
    faults: dict[str, Fault]


@dataclass
class IMatrixSnapshot:
    """Everything the entities read. Replaced, never mutated, on each update."""

    devices: dict[int, Device]
    states: dict[int, DeviceState]
    server_time: datetime | None = None
    group_paths: dict[int, str] = field(default_factory=dict)
    faults: dict[int, FaultState] = field(default_factory=dict)
    # Events read in this update only, oldest first. Entities dedupe by id.
    events: tuple[Event, ...] = ()


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
        self._failures = 0  # transient poll failures in a row
        # None until the first device list. An empty first list is still a first list.
        self._known: set[int] | None = None
        self._event_cursor: int | None = None
        self._rate_limited_since: datetime | None = None
        self.backfill: StatisticsBackfill | None = None
        self.push: IMatrixPush | None = None
        self._push_connected = False
        # sns first seen in the latest update. Empty when nothing is new.
        self.new_sns: set[int] = set()
        # Entities by device, so a stream frame updates one device, not the account.
        self._device_listeners: dict[int, list[CALLBACK_TYPE]] = {}

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

    @property
    def event_cursor(self) -> int | None:
        """Last event id read, from polling or the stream."""
        return self._event_cursor

    @callback
    def async_add_device_listener(
        self, sn: int, listener: CALLBACK_TYPE
    ) -> CALLBACK_TYPE:
        """Call listener when a stream frame changes this device."""
        listeners = self._device_listeners.setdefault(sn, [])
        listeners.append(listener)

        @callback
        def remove() -> None:
            listeners.remove(listener)
            if not listeners:
                self._device_listeners.pop(sn, None)

        return remove

    @callback
    def _async_update_device(self, sn: int) -> None:
        for listener in list(self._device_listeners.get(sn, ())):
            listener()

    @callback
    def async_set_push(self, connected: bool) -> None:
        """Poll every 5 minutes while the stream is up, else at the set interval."""
        self._push_connected = connected
        self.update_interval = PUSH_POLL_INTERVAL if connected else self._interval

    @callback
    def async_apply_frame(self, frame: StreamFrame) -> None:
        """Merge one stream frame into a new snapshot and notify entities."""
        data = self.data
        if data is None:
            return
        if isinstance(frame, EventFrame):
            event = frame.event
            if self._event_cursor is not None and event.id <= self._event_cursor:
                return
            self._event_cursor = event.id
            if event.sn not in data.devices:
                return
            self.data = replace(
                data,
                faults=_apply_events(data.faults, (event,)),
                events=(event,),
            )
            self._async_update_device(event.sn)
            return
        if not isinstance(frame, (ValuesFrame, PositionFrame, ConnectivityFrame)):
            return
        if frame.sn not in data.devices:
            self._force_refresh = True  # a new device: re-list on the next poll
            return
        state = data.states.get(frame.sn)
        if state is None:
            device = data.devices[frame.sn]
            state = DeviceState(
                connectivity=device.connectivity, position=None, readings={}
            )
        if isinstance(frame, ValuesFrame):
            state = replace(state, readings={**state.readings, **frame.readings})
        elif isinstance(frame, PositionFrame):
            state = replace(state, position=frame.position)
        else:
            state = replace(state, connectivity=frame.connectivity)
        self.data = replace(data, states={**data.states, frame.sn: state}, events=())
        self._async_update_device(frame.sn)

    async def _async_setup(self) -> None:
        """Start the event cursor at the newest event, so old events never replay."""
        try:
            self._event_cursor = (await self.client.get_events()).next_after
        except IMatrixNotFoundError:
            # Backend without the event feed: entities from events stay idle.
            _LOGGER.info("The iMatrix server has no event feed; events are off")
            self._event_cursor = None
        except (IMatrixAuthError, IMatrixPermissionError) as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except IMatrixError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

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
        except IMatrixConnectionError as err:
            if self.data is None or self._failures >= TRANSIENT_FAILURES_TOLERATED:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="update_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            self._failures += 1
            _LOGGER.warning(
                "iMatrix poll failed (%s); keeping the last values, retrying in %s s",
                err,
                int(TRANSIENT_RETRY_INTERVAL.total_seconds()),
            )
            self.update_interval = TRANSIENT_RETRY_INTERVAL
            return self.data
        except IMatrixError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._failures = 0
        self.update_interval = (
            PUSH_POLL_INTERVAL if self._push_connected else self._interval
        )
        self._clear_rate_limit()
        if self.push is not None:
            self.push.async_check_health()
        self._handle_device_changes(snapshot)
        if self.backfill is not None:
            # Fill first: entities from the previous snapshot exist by now.
            await self.backfill.async_step()
            self.backfill.async_mark_success(list(snapshot.devices))
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

        events = await self._async_fetch_events(devices)
        faults = {
            sn: f
            for sn, f in (previous.faults if previous else {}).items()
            if sn in devices
        }
        faults = _apply_events(faults, events)
        faults = await self._async_fetch_missing_faults(devices, faults)

        return IMatrixSnapshot(
            devices=devices,
            states=states,
            server_time=latest.server_time,
            group_paths={
                d.group_id: d.group_path
                for d in devices.values()
                if d.group_id is not None and d.group_path
            },
            faults=faults,
            events=events,
        )

    async def _async_fetch_events(
        self, devices: dict[int, Device]
    ) -> tuple[Event, ...]:
        if self._event_cursor is None:
            return ()
        events: list[Event] = []
        for _ in range(MAX_EVENT_PAGES):
            page = await self.client.get_events(self._event_cursor)
            self._event_cursor = page.next_after
            events.extend(e for e in page.events if e.sn in devices)
            if not page.has_more:
                break
        return tuple(events)

    async def _async_fetch_missing_faults(
        self, devices: dict[int, Device], faults: dict[int, FaultState]
    ) -> dict[int, FaultState]:
        """Fetch the fault list of a few vehicles per poll until all have one."""
        missing = sorted(
            sn
            for sn, d in devices.items()
            if d.kind is DeviceKind.VEHICLE and sn not in faults
        )
        result = dict(faults)
        for sn in missing[:FAULT_FETCHES_PER_POLL]:
            try:
                fetched = await self.client.get_faults(sn)
            except IMatrixNotFoundError, IMatrixPermissionError:
                result[sn] = FaultState(lamps=None, faults={})
                continue
            except (IMatrixConnectionError, IMatrixResponseError) as err:
                _LOGGER.debug("Fault list for one vehicle failed, retrying: %s", err)
                continue
            result[sn] = FaultState(
                lamps=fetched.lamps, faults={f.id: f for f in fetched.faults}
            )
        return result

    def _back_off(self, retry_after: float | None) -> None:
        current = self.update_interval or self._interval
        wait = max(timedelta(seconds=retry_after or 0), current * 2)
        self.update_interval = min(wait, MAX_BACKOFF)
        now = dt_util.utcnow()
        if self._rate_limited_since is None:
            self._rate_limited_since = now
        elif now - self._rate_limited_since >= RATE_LIMIT_ISSUE_AFTER:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"rate_limited_{self.config_entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="rate_limited",
                translation_placeholders={"title": self.config_entry.title},
            )

    def _clear_rate_limit(self) -> None:
        if self._rate_limited_since is not None:
            self._rate_limited_since = None
            ir.async_delete_issue(
                self.hass, DOMAIN, f"rate_limited_{self.config_entry.entry_id}"
            )

    @callback
    def _handle_device_changes(self, snapshot: IMatrixSnapshot) -> None:
        current = set(snapshot.devices)
        sync_device_registry(self.hass, self.config_entry, snapshot)
        issue_id = f"no_devices_{self.config_entry.entry_id}"
        if current:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_devices",
                translation_placeholders={"title": self.config_entry.title},
            )
        if self._known is None:
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


def _apply_events(
    faults: dict[int, FaultState], events: tuple[Event, ...]
) -> dict[int, FaultState]:
    """Apply dtc_set, dtc_cleared and lamp_changed to vehicles already fetched."""
    result = dict(faults)
    for event in events:
        state = result.get(event.sn)
        if state is None:
            continue  # the first fetch for this vehicle will include it
        if event.type is EventType.DTC_SET and (fault := event.fault):
            result[event.sn] = FaultState(
                state.lamps, {**state.faults, fault.id: fault}
            )
        elif event.type is EventType.DTC_CLEARED and (fault := event.fault):
            remaining = {k: v for k, v in state.faults.items() if k != fault.id}
            result[event.sn] = FaultState(state.lamps, remaining)
        elif event.type is EventType.LAMP_CHANGED:
            result[event.sn] = FaultState(event.lamps, state.faults)
    return result


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
            sw_version=None if device.firmware is None else str(device.firmware),
            configuration_url=f"{WEB_APP_URL}/dashboard/devices/{device.sn}",
            via_device_id=via.id if via is not None else None,
        )
