import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.setup import async_setup_component
from pyimatrix import IMatrixAuthError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.imatrix.const import (
    DOMAIN,
    OAUTH2_AUTHORIZE_URL,
    OAUTH2_REVOKE_URL,
    OAUTH2_TOKEN_URL,
)
from custom_components.imatrix.helpers import create_token_client

from .conftest import make_account

REDIRECT = "https://example.com/auth/external/callback"
TOKEN = {
    "access_token": "imxat_fake",
    "token_type": "Bearer",
    "expires_in": 900,
    "refresh_token": "imxrt_fake",
    "scope": "imatrix.read",
    "org_id": 4242,
    "org_name": "Example Cold Chain",
}


@pytest.fixture
def mock_token_account() -> MagicMock:
    """Patch the client the flow builds to validate a fresh token."""
    with patch("custom_components.imatrix.config_flow.IMatrixClient") as cls:
        cls.return_value.get_account = AsyncMock(return_value=make_account())
        yield cls.return_value


async def _oauth_flow(
    hass: HomeAssistant,
    client_factory: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    flow_id: str,
    url: str,
) -> dict:
    state = config_entry_oauth2_flow._encode_jwt(
        hass, {"flow_id": flow_id, "redirect_uri": REDIRECT}
    )
    assert url.startswith(f"{OAUTH2_AUTHORIZE_URL}?response_type=code")
    assert "client_id=home-assistant" in url
    assert "code_challenge_method=S256" in url
    assert "scope=imatrix.read" in url
    client = await client_factory()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200
    aioclient_mock.post(OAUTH2_TOKEN_URL, json=TOKEN)
    return await hass.config_entries.flow.async_configure(flow_id)


@pytest.mark.usefixtures("current_request_with_host")
async def test_oauth_full_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_token_account: MagicMock,
) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "oauth"}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    with patch("custom_components.imatrix.async_setup_entry", return_value=True):
        result = await _oauth_flow(
            hass, hass_client_no_auth, aioclient_mock, result["flow_id"], result["url"]
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Example Cold Chain"
    assert result["result"].unique_id == "4242"
    data = result["data"]
    assert data["auth_type"] == "oauth2"
    assert data["token"]["refresh_token"] == "imxrt_fake"
    assert data["base_url"] == "https://api.imatrixsys.com"
    # The token exchange sent the PKCE verifier.
    body = aioclient_mock.mock_calls[-1][2]
    assert body["grant_type"] == "authorization_code"
    assert "code_verifier" in body


@pytest.mark.usefixtures("current_request_with_host")
async def test_oauth_rejected_token(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_token_account: MagicMock,
) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    mock_token_account.get_account.side_effect = IMatrixAuthError("401")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "oauth"}
    )
    result = await _oauth_flow(
        hass, hass_client_no_auth, aioclient_mock, result["flow_id"], result["url"]
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"


def _oauth_entry(expires_at: float | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Example Cold Chain",
        unique_id="4242",
        version=1,
        minor_version=2,
        data={
            "auth_type": "oauth2",
            "auth_implementation": DOMAIN,
            "base_url": "https://api.imatrixsys.com",
            "token": {
                **TOKEN,
                "expires_at": expires_at or time.time() + 900,
            },
        },
    )


@pytest.mark.usefixtures("current_request_with_host")
@pytest.mark.parametrize(
    ("org_id", "reason"), [(4242, "reauth_successful"), (9, "wrong_account")]
)
async def test_oauth_reauth(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_token_account: MagicMock,
    org_id: int,
    reason: str,
) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    entry = _oauth_entry()
    entry.add_to_hass(hass)
    mock_token_account.get_account.return_value = make_account(org_id=org_id)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_oauth"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.imatrix.async_setup_entry", return_value=True):
        result = await _oauth_flow(
            hass, hass_client_no_auth, aioclient_mock, result["flow_id"], result["url"]
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


@pytest.mark.usefixtures("current_request_with_host")
async def test_oauth_reconfigure(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    mock_token_account: MagicMock,
) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    entry = _oauth_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    with patch("custom_components.imatrix.async_setup_entry", return_value=True):
        result = await _oauth_flow(
            hass, hass_client_no_auth, aioclient_mock, result["flow_id"], result["url"]
        )
    assert result["reason"] == "reconfigure_successful"


async def test_oauth_entry_setup_refresh_and_remove(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_client: MagicMock,
) -> None:
    """An OAuth entry refreshes its token on the first request and revokes on delete."""
    assert await async_setup_component(hass, "application_credentials", {})
    entry = _oauth_entry(expires_at=time.time() - 10)
    entry.add_to_hass(hass)
    aioclient_mock.post(
        OAUTH2_TOKEN_URL, json={**TOKEN, "refresh_token": "imxrt_rotated"}
    )
    aioclient_mock.post(OAUTH2_REVOKE_URL, status=200)
    built: list = []

    def build(hass_: HomeAssistant, session: object) -> MagicMock:
        built.append(create_token_client(hass_, session))  # type: ignore[arg-type]
        return mock_client

    with patch("custom_components.imatrix.create_token_client", side_effect=build):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    token = await built[0].auth.async_get_access_token()
    assert token == "imxat_fake"
    assert entry.data["token"]["refresh_token"] == "imxrt_rotated"
    assert entry.runtime_data.account.org_id == 4242

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    revoke = [c for c in aioclient_mock.mock_calls if str(c[1]) == OAUTH2_REVOKE_URL]
    assert revoke and revoke[0][2]["token"] == "imxrt_rotated"


async def test_oauth_refresh_rejected_starts_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    entry = _oauth_entry(expires_at=time.time() - 10)
    entry.add_to_hass(hass)
    aioclient_mock.post(OAUTH2_TOKEN_URL, status=400, json={"error": "invalid_grant"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_migrate_1_1_to_1_2(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="4242",
        version=1,
        minor_version=1,
        data={"api_key": "imx_x_y", "base_url": "https://api.imatrixsys.com"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert entry.data["auth_type"] == "api_key"
