from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pyimatrix import History, IMatrixConnectionError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.imatrix.const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

from .conftest import FAKE_KEY

ENTRY_ID = "01BACKFILLENTRY000000000000"
IMPORT = "homeassistant.components.recorder.statistics.async_import_statistics"


def _history(sn: int, sensor_ids: list[int], start: Any, end: Any) -> History:
    hour = start.replace(minute=0, second=0, microsecond=0)
    points = [
        {
            "t": (hour + timedelta(hours=h)).isoformat(),
            "mean": -15.0 + h,
            "min": -16.0 + h,
            "max": -14.0 + h,
            "last": -15.0 + h,
            "n": 60,
        }
        for h in range(2)
    ]
    return History.from_dict(
        {
            "schemaVersion": 1,
            "sn": sn,
            "series": {str(i): {"unit": "°C", "points": points} for i in sensor_ids},
        }
    )


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        title="Example Cold Chain",
        unique_id="4242",
        data={
            "auth_type": "api_key",
            "api_key": FAKE_KEY,
            CONF_BASE_URL: DEFAULT_BASE_URL,
        },
        minor_version=2,
    )


def _seed(hass_storage: dict[str, Any], hours_ago: float) -> None:
    key = f"{DOMAIN}.{ENTRY_ID}.last_success"
    when = dt_util.utcnow() - timedelta(hours=hours_ago)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"last_success": when.isoformat()},
    }


async def _setup(hass: HomeAssistant, recorder: bool = True) -> MockConfigEntry:
    if recorder:
        hass.config.components.add("recorder")
    entry = _entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _imports(mock_import: MagicMock) -> dict[str, tuple[dict, list]]:
    return {
        c.args[1]["statistic_id"]: (c.args[1], list(c.args[2]))
        for c in mock_import.call_args_list
    }


async def test_backfill_after_restart(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    _seed(hass_storage, hours_ago=3)
    mock_client.get_history = AsyncMock(side_effect=_history)
    with patch(IMPORT) as mock_import:
        await _setup(hass)
        mock_import.assert_not_called()  # entities do not exist on the first poll
        await _tick(hass, freezer)

    imports = _imports(mock_import)
    meta, stats = imports["sensor.freezer_3_probe_1"]
    assert meta["unit_of_measurement"] == "°C"
    assert meta["source"] == "recorder"
    assert meta["has_sum"] is False
    assert [s["mean"] for s in stats] == [-15.0, -14.0]
    assert "sensor.truck_12_speed" in imports
    # Totals and disabled sensors are not filled.
    assert "sensor.truck_12_odometer" not in imports
    assert "sensor.truck_12_coolant_temperature" not in imports
    _, _, start, end = mock_client.get_history.await_args_list[0].args
    assert end - start <= timedelta(hours=5)

    # The queue is done: later polls do not fetch history again.
    calls = mock_client.get_history.await_count
    await _tick(hass, freezer)
    assert mock_client.get_history.await_count == calls


async def test_backfill_converts_to_display_unit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    _seed(hass_storage, hours_ago=3)
    mock_client.get_history = AsyncMock(side_effect=_history)
    with patch(IMPORT) as mock_import:
        await _setup(hass)
        await _tick(hass, freezer)
    meta, stats = _imports(mock_import)["sensor.freezer_3_probe_1"]
    assert meta["unit_of_measurement"] == "°F"
    assert meta["unit_class"] == "temperature"
    assert stats[0]["mean"] == pytest.approx(5.0)
    # km/h speed sensor shown in mph: converted with the speed converter.
    speed_meta, _ = _imports(mock_import)["sensor.truck_12_speed"]
    assert speed_meta["unit_of_measurement"] == "mph"


@pytest.mark.parametrize(("hours_ago", "recorder"), [(0.5, True), (3, False)])
async def test_no_backfill(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    hours_ago: float,
    recorder: bool,
) -> None:
    _seed(hass_storage, hours_ago=hours_ago)
    mock_client.get_history = AsyncMock(side_effect=_history)
    with patch(IMPORT) as mock_import:
        await _setup(hass, recorder=recorder)
        await _tick(hass, freezer)
    mock_import.assert_not_called()
    mock_client.get_history.assert_not_awaited()


async def test_backfill_after_cloud_outage_and_errors(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.get_history = AsyncMock(side_effect=IMatrixConnectionError("x"))
    with patch(IMPORT) as mock_import:
        await _setup(hass)
        latest = mock_client.get_latest.side_effect
        mock_client.get_latest.side_effect = IMatrixConnectionError("down")
        freezer.tick(timedelta(hours=2))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        mock_client.get_latest.side_effect = latest
        await _tick(hass, freezer)  # recovery plans the window
        await _tick(hass, freezer)  # history fails: device skipped, poll succeeds
    assert mock_client.get_history.await_count >= 1
    mock_import.assert_not_called()
    assert hass.states.get("sensor.truck_12_odometer").state == "120455.2"


async def test_backfill_skips_empty_series(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    def empty_history(sn: int, ids: list[int], start: Any, end: Any) -> History:
        series = {str(i): {"unit": "°C", "points": []} for i in ids}
        return History.from_dict({"schemaVersion": 1, "sn": sn, "series": series})

    _seed(hass_storage, hours_ago=3)
    mock_client.get_history = AsyncMock(side_effect=empty_history)
    with patch(IMPORT) as mock_import:
        await _setup(hass)
        await _tick(hass, freezer)
    assert mock_client.get_history.await_count >= 1
    mock_import.assert_not_called()
