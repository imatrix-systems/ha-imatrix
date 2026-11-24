from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pyimatrix import (
    IMatrixAuthError,
    IMatrixConnectionError,
    IMatrixPermissionError,
    IMatrixUnsupportedSchemaError,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.imatrix.const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

from .conftest import FAKE_KEY, make_account

NEW_KEY = "imx_11111111_newnewnewnewnewnewnewnewnewnewnewne"


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Flows here create or reload entries; setting them up is tested elsewhere."""
    with patch(
        "custom_components.imatrix.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def _start_api_key_flow(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["oauth", "api_key"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "api_key"}
    )


def _input(key: str = FAKE_KEY, base_url: str = DEFAULT_BASE_URL) -> dict:
    return {"api_key": f"  {key} ", "advanced": {CONF_BASE_URL: base_url}}


async def test_user_flow_creates_entry(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    result = await _start_api_key_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "api_key"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], _input())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Cold Chain"
    assert result["data"] == {
        "auth_type": "api_key",
        "api_key": FAKE_KEY,
        CONF_BASE_URL: DEFAULT_BASE_URL,
    }
    assert result["result"].unique_id == "4242"
    mock_client.factory.assert_called_with(hass, DEFAULT_BASE_URL, FAKE_KEY)


async def test_user_flow_custom_url_and_unnamed_org(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    account = make_account()
    mock_client.get_account.return_value = account.__class__(
        **{**{f: getattr(account, f) for f in account.__slots__}, "org_name": None}
    )
    result = await _start_api_key_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _input(base_url="https://api-dev.imatrixsys.com/")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "iMatrix org 4242"
    assert result["data"][CONF_BASE_URL] == "https://api-dev.imatrixsys.com"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (IMatrixAuthError("401"), "invalid_auth"),
        (IMatrixPermissionError("403"), "insufficient_permissions"),
        (IMatrixUnsupportedSchemaError("v2"), "unsupported_api"),
        (IMatrixConnectionError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_recover(
    hass: HomeAssistant, mock_client: MagicMock, error: Exception, key: str
) -> None:
    mock_client.get_account.side_effect = error
    result = await _start_api_key_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _input(base_url="https://api-dev.imatrixsys.com")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    mock_client.get_account.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _input())
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_missing_permissions(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    mock_client.get_account.return_value = make_account(permissions={"device:read"})
    result = await _start_api_key_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _input())
    assert result["errors"] == {"base": "insufficient_permissions"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start_api_key_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _input())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_client.get_account.side_effect = IMatrixAuthError("401")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": NEW_KEY}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.get_account.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": NEW_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data["api_key"] == NEW_KEY


async def test_reauth_wrong_account(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_client.get_account.return_value = make_account(org_id=9999)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": NEW_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert mock_config_entry.data["api_key"] == FAKE_KEY


async def test_reconfigure_success(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    mock_client.get_account.side_effect = IMatrixConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _input(NEW_KEY, "https://api-dev.imatrixsys.com")
    )
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.get_account.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _input(NEW_KEY, "https://api-dev.imatrixsys.com")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {
        "auth_type": "api_key",
        "api_key": NEW_KEY,
        CONF_BASE_URL: "https://api-dev.imatrixsys.com",
    }


async def test_reconfigure_wrong_account(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_client.get_account.return_value = make_account(org_id=9999)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _input(NEW_KEY)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
