"""Fill long-term statistics for hours missed during an outage (spec 817 FR-H14).

When Home Assistant or the iMatrix cloud was unreachable for an hour or more, the
recorder has no samples for those hours. After the next successful poll, hourly
mean, min and max come from GET /devices/:sn/history and are imported as the
entity's own statistics. A few devices are done per poll to stay inside the rate
limit. Only `measurement` sensors are filled: totals such as the odometer already
come out right, because HA counts the increase at the first sample after the gap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor.const import UNIT_CONVERTERS
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pyimatrix import IMatrixError

from .const import (
    BACKFILL_AFTER,
    BACKFILL_DEVICES_PER_POLL,
    BACKFILL_MAX,
    DOMAIN,
)
from .units import channel_class_and_unit

if TYPE_CHECKING:
    from . import IMatrixConfigEntry
    from .coordinator import IMatrixCoordinator

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
MAX_SENSORS_PER_CALL = 20


def _hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


class StatisticsBackfill:
    """Remembers the last good poll and fills the gap after an outage."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IMatrixConfigEntry,
        coordinator: IMatrixCoordinator,
    ) -> None:
        """Create the backfill helper of one config entry."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store: Store[dict[str, str]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.last_success"
        )
        self._last_success: datetime | None = None
        self.window: tuple[datetime, datetime] | None = None
        self.queue: list[int] = []

    async def async_load(self) -> None:
        """Read the last good poll time saved before the last shutdown."""
        data = await self._store.async_load()
        if data and (raw := data.get("last_success")):
            self._last_success = dt_util.parse_datetime(raw)

    @callback
    def async_mark_success(self, sns: list[int]) -> None:
        """Record a good poll and plan a backfill when the last one is old."""
        now = dt_util.utcnow()
        previous = self._last_success
        self._last_success = now
        self._store.async_delay_save(lambda: {"last_success": now.isoformat()}, 60)
        if previous is None or now - previous < BACKFILL_AFTER:
            return
        start = _hour(max(previous, now - BACKFILL_MAX))
        end = _hour(now)
        if end <= start or "recorder" not in self.hass.config.components:
            return
        self.window = (start, end)
        self.queue = sorted(sns)
        _LOGGER.info(
            "Filling statistics for %d devices, %s to %s", len(self.queue), start, end
        )

    async def async_step(self) -> None:
        """Fill the next few devices. Errors skip the device, never fail the poll."""
        if self.window is None:
            return
        batch, self.queue = (
            self.queue[:BACKFILL_DEVICES_PER_POLL],
            self.queue[BACKFILL_DEVICES_PER_POLL:],
        )
        for sn in batch:
            try:
                await self._async_fill_device(sn, *self.window)
            except IMatrixError as err:
                _LOGGER.debug("Statistics backfill skipped one device: %s", err)
        if not self.queue:
            self.window = None

    def _targets(self, sn: int) -> dict[int, tuple[str, Any]]:
        """Return sensorId -> (entity_id, state) for enabled measurement sensors."""
        registry = er.async_get(self.hass)
        device = self.coordinator.data.devices.get(sn)
        if device is None:
            return {}
        channels = {c.sensor_id: c for c in device.channels}
        targets: dict[int, tuple[str, Any]] = {}
        for entry in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            prefix, _, sensor_id = entry.unique_id.partition("_")
            if (
                entry.domain != "sensor"
                or entry.disabled_by is not None
                or prefix != str(sn)
                or not sensor_id.isdigit()
                or int(sensor_id) not in channels
            ):
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is None or (
                state.attributes.get("state_class") != SensorStateClass.MEASUREMENT
            ):
                continue
            targets[int(sensor_id)] = (entry.entity_id, state)
        return targets

    async def _async_fill_device(self, sn: int, start: datetime, end: datetime) -> None:
        # Imported lazily: the recorder is an after-dependency, not a dependency.
        from homeassistant.components.recorder.models import (  # noqa: PLC0415
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
            async_import_statistics,
        )

        targets = self._targets(sn)
        ids = sorted(targets)
        channels = {c.sensor_id: c for c in self.coordinator.data.devices[sn].channels}
        for offset in range(0, len(ids), MAX_SENSORS_PER_CALL):
            chunk = ids[offset : offset + MAX_SENSORS_PER_CALL]
            history = await self.coordinator.client.get_history(
                sn, chunk, start, end + timedelta(hours=1)
            )
            for sensor_id in chunk:
                series = history.series.get(sensor_id)
                if series is None or not series.points:
                    continue
                entity_id, state = targets[sensor_id]
                _, native_unit = channel_class_and_unit(channels[sensor_id])
                state_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
                converter = UNIT_CONVERTERS.get(state.attributes.get(ATTR_DEVICE_CLASS))
                convert: Callable[[float], float]
                if native_unit == state_unit:
                    convert = float
                elif (
                    converter is not None
                    and native_unit in converter.VALID_UNITS
                    and state_unit in converter.VALID_UNITS
                ):
                    convert = converter.converter_factory(native_unit, state_unit)
                else:
                    continue  # unknown conversion: leave the gap
                metadata = StatisticMetaData(
                    mean_type=StatisticMeanType.ARITHMETIC,
                    has_sum=False,
                    name=None,
                    source="recorder",
                    statistic_id=entity_id,
                    unit_class=converter.UNIT_CLASS if converter else None,
                    unit_of_measurement=state_unit,
                )
                stats = [
                    StatisticData(
                        start=p.start,
                        mean=convert(p.mean),
                        min=convert(p.min),
                        max=convert(p.max),
                    )
                    for p in series.points
                    if start <= p.start < end
                ]
                if stats:
                    async_import_statistics(self.hass, metadata, stats)
