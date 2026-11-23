"""Fixtures for iMatrix tests."""

import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyimatrix import Account, Device, Latest, Limits
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_KEY = "imx_00000000_fakefakefakefakefakefakefakefakefake"
ORG_ID = 4242


def make_account(org_id: int = ORG_ID, permissions: set[str] | None = None) -> Account:
    """Return a synthetic account."""
    return Account(
        org_id=org_id,
        org_name="Example Cold Chain",
        key_prefix="00000000",
        permissions=frozenset(
            permissions
            if permissions is not None
            else {"device:read", "sensor_data:read", "group:read"}
        ),
        expires_at=datetime(2027, 6, 1, tzinfo=UTC),
        limits=Limits(requests_per_minute=120, stream_connections=5),
    )


def load_devices() -> list[Device]:
    """Return the synthetic device list."""
    return [
        Device.from_dict(d) for d in json.loads((FIXTURES / "devices.json").read_text())
    ]


def load_latest() -> Latest:
    """Return the synthetic latest response."""
    return Latest.from_dict(json.loads((FIXTURES / "latest.json").read_text()))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> Generator[None]:
    """Enable loading custom_components in every test."""
    yield


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch client creation in setup and the config flow."""
    client = MagicMock()
    client.get_account = AsyncMock(return_value=make_account())
    client.get_devices = AsyncMock(side_effect=lambda **_: load_devices())
    client.get_latest = AsyncMock(side_effect=lambda *_, **__: load_latest())
    client.auth.rate_limit_remaining = 118
    with (
        patch("custom_components.imatrix.create_api_key_client", return_value=client),
        patch(
            "custom_components.imatrix.config_flow.create_api_key_client",
            return_value=client,
        ) as factory,
    ):
        client.factory = factory
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry with fake credentials."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Example Cold Chain",
        unique_id=str(ORG_ID),
        data={"api_key": FAKE_KEY, CONF_BASE_URL: DEFAULT_BASE_URL},
        version=1,
        minor_version=1,
    )


@pytest.fixture
async def init_integration(
    hass: Any, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> MockConfigEntry:
    """Set up the integration with the synthetic account."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
