import asyncio
import dataclasses
from datetime import timedelta
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pyimatrix import (
    Connectivity,
    ConnectivityFrame,
    ConnectivityState,
    Event,
    EventFrame,
    IMatrixAuthError,
    IMatrixNotFoundError,
    IMatrixPermissionError,
    Position,
    PositionFrame,
    Reading,
    ReplayDoneFrame,
    ValuesFrame,
)
from pyimatrix.models import AlarmLevel
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.imatrix.const import DOMAIN

from .conftest import load_latest

TRUCK = 1000000001


class FakeStream:
    """Stands in for IMatrixStream; tests drive frames and connection state."""

    instances: ClassVar[list[FakeStream]] = []

    def __init__(
        self, auth: Any, on_frame: Any, *, after: Any, on_connected: Any
    ) -> None:
        self.on_frame = on_frame
        self.on_connected = on_connected
        self.after = after
        self.reconnects = 0
        self.last_event_id = after
        self.error: Exception | None = None
        self.done = asyncio.Event()
        FakeStream.instances.append(self)

    async def run(self) -> None:
        await self.done.wait()
        if self.error:
            raise self.error

    async def stop(self) -> None:
        self.done.set()


@pytest.fixture
def fake_stream() -> Any:
    FakeStream.instances = []
    with patch("custom_components.imatrix.push.IMatrixStream", FakeStream):
        yield FakeStream


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, s: int) -> None:
    freezer.tick(timedelta(seconds=s))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_push_frames_update_entities(
    hass: HomeAssistant,
    fake_stream: Any,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
) -> None:
    stream = fake_stream.instances[0]
    assert stream.after == 100  # resumes from the coordinator's event cursor
    coordinator = init_integration.runtime_data.coordinator

    await stream.on_connected(True)
    await hass.async_block_till_done()
    assert coordinator.update_interval == timedelta(minutes=5)
    assert mock_client.get_latest.await_count >= 2  # refresh on connect

    stream.on_frame(
        ValuesFrame(
            sn=TRUCK,
            ingest_at=None,
            readings={
                142: Reading(
                    value=88.0,
                    time=None,
                    alarm=AlarmLevel.NONE,
                    alarm_side=None,
                    clock_skew=False,
                )
            },
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.truck_12_speed").state == "88.0"
    # A frame updates only that device's entities, never the whole account.
    with patch.object(coordinator, "async_update_listeners") as update_all:
        stream.on_frame(ValuesFrame(sn=TRUCK, ingest_at=None, readings={}))
        await hass.async_block_till_done()
    update_all.assert_not_called()
    assert hass.states.get("sensor.truck_12_odometer").state == "120455.2"

    position = Position(40.0, -121.0, None, None, 0.0, 2.0, 9, None, False)
    stream.on_frame(PositionFrame(sn=TRUCK, ingest_at=None, position=position))
    await hass.async_block_till_done()
    tracker = hass.states.get("device_tracker.truck_12")
    assert tracker.attributes["latitude"] == 40.0
    assert hass.states.get("binary_sensor.truck_12_moving").state == "off"

    offline = Connectivity(ConnectivityState.OFFLINE, None, None)
    stream.on_frame(ConnectivityFrame(sn=TRUCK, connectivity=offline))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.truck_12_connectivity").state == "offline"
    assert hass.states.get("sensor.truck_12_speed").state == "unavailable"

    event = Event.from_dict(
        {
            "id": 120,
            "type": "geofence_exit",
            "sn": TRUCK,
            "ts": "2026-11-23T15:00:00Z",
            "data": {"geofenceId": 1, "geofenceName": "Yard"},
        }
    )
    stream.on_frame(EventFrame(event=event))
    await hass.async_block_till_done()
    fence = hass.states.get("event.truck_12_geofence")
    assert fence.attributes["event_type"] == "exit"
    fired = fence.state
    stream.on_frame(EventFrame(event=event))  # duplicate from a replay
    await hass.async_block_till_done()
    assert hass.states.get("event.truck_12_geofence").state == fired
    assert coordinator.event_cursor == 120

    # Frames for devices not imported, and frames HA does not use, change nothing.
    stream.on_frame(dataclasses.replace(PositionFrame(9, None, position)))
    stream.on_frame(EventFrame(event=dataclasses.replace(event, id=121, sn=9)))
    stream.on_frame(ReplayDoneFrame(replayed=0, next_after=121, truncated=False))
    stream.on_frame(ValuesFrame(sn=1000000002, ingest_at=None, readings={}))
    await hass.async_block_till_done()

    await stream.on_connected(False)
    assert coordinator.update_interval == timedelta(seconds=60)

    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert stream.done.is_set()


async def test_values_for_device_without_latest(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    fake_stream: Any,
) -> None:
    full = load_latest()
    mock_client.get_latest.side_effect = lambda *_, **__: dataclasses.replace(
        full, devices={k: v for k, v in full.devices.items() if k != TRUCK}
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    stream = fake_stream.instances[0]
    reading = Reading(77.0, None, AlarmLevel.NONE, None, False)
    stream.on_frame(ValuesFrame(sn=TRUCK, ingest_at=None, readings={142: reading}))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.truck_12_speed").state == "77.0"


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (IMatrixNotFoundError("404"), "unsupported"),
        (IMatrixPermissionError("403"), "forbidden"),
        (IMatrixAuthError("4401"), "auth_failed"),
    ],
)
async def test_stream_ends(
    hass: HomeAssistant,
    fake_stream: Any,
    init_integration: MockConfigEntry,
    error: Exception,
    state: str,
) -> None:
    stream = fake_stream.instances[0]
    stream.error = error
    stream.done.set()
    await hass.async_block_till_done()
    push = init_integration.runtime_data.coordinator.push
    assert push.state == state
    if state == "auth_failed":
        flows = hass.config_entries.flow.async_progress()
        assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_stream_down_raises_issue(
    hass: HomeAssistant,
    fake_stream: Any,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    issue_id = f"stream_unavailable_{init_integration.entry_id}"
    for _ in range(31):
        await _tick(hass, freezer, 60)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    await fake_stream.instances[0].on_connected(True)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_push_off_in_options(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={"push": False})
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.coordinator.push is None
    mock_client.auth.ws_connect.assert_not_awaited()
