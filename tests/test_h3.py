from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pyimatrix import (
    Event,
    EventPage,
    IMatrixAuthError,
    IMatrixConnectionError,
    IMatrixNotFoundError,
    IMatrixRateLimitError,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.imatrix.const import DOMAIN

from .conftest import load_latest, make_account

TRUCK = 1000000001
FREEZER = 1000000002


def _event(eid: int, etype: str, sn: int = TRUCK, **data: object) -> Event:
    return Event.from_dict(
        {"id": eid, "type": etype, "sn": sn, "ts": "2026-11-23T15:00:00Z", "data": data}
    )


def _page(
    *events: Event, next_after: int | None = None, more: bool = False
) -> EventPage:
    last = events[-1].id if events else 100
    return EventPage(next_after=next_after or last, has_more=more, events=events)


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, s: int = 60
) -> None:
    freezer.tick(timedelta(seconds=s))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


DTC_NEW = {
    "faultId": "obd2:P0128:16@1763910000000",
    "displayCode": "P0128",
    "name": "Coolant Thermostat",
    "protocol": "obd2",
    "severity": "warning",
    "setTs": "2026-11-23T15:00:00Z",
}


async def test_fault_entities_after_setup(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_faults.assert_awaited_once_with(TRUCK)  # vehicles only
    active = hass.states.get("sensor.truck_12_active_fault_codes")
    assert active.state == "3"
    assert active.attributes["codes"][0] == "SPN 110 FMI 0"
    assert hass.states.get("binary_sensor.truck_12_fault_code_present").state == "on"
    assert hass.states.get("binary_sensor.truck_12_malfunction_lamp").state == "on"
    assert hass.states.get("binary_sensor.truck_12_red_stop_lamp").state == "off"
    registry = er.async_get(hass)
    amber = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{TRUCK}_lamp_amber")
    assert registry.async_get(amber).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # No events fire at startup.
    assert hass.states.get("event.truck_12_fault_codes").state == STATE_UNKNOWN
    assert hass.states.get("event.freezer_3_imatrix_alert").state == STATE_UNKNOWN
    assert hass.states.get("event.freezer_3_fault_codes") is None


async def test_events_fire_and_update_faults(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # The first call has no `after`: it only fetches the start cursor.
    assert mock_client.get_events.await_args_list[0].args == ()
    events = (
        _event(101, "dtc_set", **DTC_NEW),
        _event(
            102,
            "dtc_cleared",
            faultId="obd2:P0420:16@reported",
            displayCode="P0420",
            clearTs="2026-11-23T15:00:00Z",
            inferred=True,
            manual=False,
        ),
        _event(103, "lamp_changed", lamps={"mil": False, "redStop": True}),
        _event(104, "geofence_enter", geofenceId=55, geofenceName="Yard"),
        _event(105, "alert_raised", sn=FREEZER, alertId=7, title="Probe 1 high"),
        _event(106, "ignition_on", value=1),
        _event(107, "dtc_set", sn=4040, **DTC_NEW),  # device not imported
    )
    mock_client.get_events.return_value = _page(*events)
    await _tick(hass, freezer)
    assert mock_client.get_events.await_args.args == (100,)

    fault_event = hass.states.get("event.truck_12_fault_codes")
    assert fault_event.attributes["event_type"] == "cleared"
    assert fault_event.attributes["code"] == "P0420"
    assert fault_event.attributes["inferred"] is True
    assert hass.states.get("sensor.truck_12_active_fault_codes").state == "3"
    assert (
        "P0128"
        in hass.states.get("sensor.truck_12_active_fault_codes").attributes["codes"]
    )
    assert hass.states.get("binary_sensor.truck_12_malfunction_lamp").state == "off"
    assert hass.states.get("binary_sensor.truck_12_red_stop_lamp").state == "on"
    fence = hass.states.get("event.truck_12_geofence")
    assert fence.attributes["event_type"] == "enter"
    assert fence.attributes["geofence"] == "Yard"
    alert = hass.states.get("event.freezer_3_imatrix_alert")
    assert alert.attributes["event_type"] == "raised"
    assert alert.attributes["title"] == "Probe 1 high"

    # The same page again (or a failed update) must not fire anything twice.
    fired_at = fault_event.state
    await _tick(hass, freezer)
    assert hass.states.get("event.truck_12_fault_codes").state == fired_at
    mock_client.get_latest.side_effect = IMatrixConnectionError("down")
    await _tick(hass, freezer)
    assert hass.states.get("event.truck_12_fault_codes").state == "unavailable"
    mock_client.get_latest.side_effect = lambda *_, **__: load_latest()
    await _tick(hass, freezer)
    assert hass.states.get("event.truck_12_fault_codes").state == fired_at


async def test_event_paging(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.get_events.side_effect = [
        _page(_event(101, "dtc_set", **DTC_NEW), more=True),
        _page(next_after=250),
    ]
    await _tick(hass, freezer)
    assert [c.args for c in mock_client.get_events.await_args_list[-2:]] == [
        (100,),
        (101,),
    ]
    mock_client.get_events.side_effect = None
    mock_client.get_events.return_value = _page(next_after=250)
    await _tick(hass, freezer)
    assert mock_client.get_events.await_args.args == (250,)


async def test_fault_fetch_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.get_faults.side_effect = IMatrixConnectionError("flaky")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    active = "sensor.truck_12_active_fault_codes"
    assert hass.states.get(active).state == STATE_UNKNOWN
    # A dtc event before the first fetch is ignored; the fetch includes it.
    mock_client.get_events.return_value = _page(_event(101, "dtc_set", **DTC_NEW))
    mock_client.get_faults.side_effect = IMatrixNotFoundError("gone")
    await _tick(hass, freezer)
    assert hass.states.get(active).state == "0"
    assert hass.states.get("binary_sensor.truck_12_malfunction_lamp").state == (
        STATE_UNKNOWN
    )


async def test_fault_fetches_are_spread(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    with patch("custom_components.imatrix.coordinator.FAULT_FETCHES_PER_POLL", 0):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        mock_client.get_faults.assert_not_awaited()
    await _tick(hass, freezer)
    mock_client.get_faults.assert_awaited_once()
    await _tick(hass, freezer)
    mock_client.get_faults.assert_awaited_once()  # fetched once, then events


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (IMatrixAuthError("401"), ConfigEntryState.SETUP_ERROR),
        (IMatrixConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_event_cursor_errors_at_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    mock_client.get_events.side_effect = error
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is state
    if state is ConfigEntryState.SETUP_ERROR:
        flows = hass.config_entries.flow.async_progress()
        assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_key_expiry_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    account = make_account()
    soon = dt_util.utcnow() + timedelta(days=5)
    mock_client.get_account.return_value = type(account)(
        **{**{f: getattr(account, f) for f in account.__slots__}, "expires_at": soon}
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"api_key_expiring_{mock_config_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["date"] == (
        dt_util.as_local(soon).date().isoformat()
    )

    mock_client.get_account.return_value = make_account()
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_no_devices_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.get_devices.side_effect = lambda **_: []
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"no_devices_{mock_config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_rate_limit_issue(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    issue_id = f"rate_limited_{init_integration.entry_id}"
    mock_client.get_latest.side_effect = IMatrixRateLimitError("429", retry_after=600)
    await _tick(hass, freezer)
    for _ in range(4):
        await _tick(hass, freezer, 600)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    mock_client.get_latest.side_effect = lambda *_, **__: load_latest()
    await _tick(hass, freezer, 1800)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_backend_without_event_feed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_client.get_events.side_effect = IMatrixNotFoundError("404")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    await _tick(hass, freezer)
    assert mock_client.get_events.await_count == 1  # never polled without a cursor
    assert hass.states.get("event.truck_12_fault_codes").state == STATE_UNKNOWN
