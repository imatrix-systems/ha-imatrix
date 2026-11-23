"""Fixtures for iMatrix tests."""

from collections.abc import Generator
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

FAKE_KEY = "imx_00000000_fakefakefakefakefakefakefakefakefake"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> Generator[None]:
    """Enable loading custom_components in every test."""
    yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry with fake credentials."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="iMatrix",
        data={"api_key": FAKE_KEY, CONF_BASE_URL: DEFAULT_BASE_URL},
        version=1,
        minor_version=1,
    )
