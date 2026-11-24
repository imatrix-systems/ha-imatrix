"""Shared helpers for setup and the config flow."""

from __future__ import annotations

import aiohttp
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session
from pyimatrix import (
    ApiKeyAuth,
    IMatrixAuthError,
    IMatrixClient,
    IMatrixConnectionError,
    TokenAuth,
)

from .const import DEFAULT_BASE_URL

USER_AGENT = f"HomeAssistant/{HA_VERSION}"


def create_api_key_client(
    hass: HomeAssistant, base_url: str, api_key: str
) -> IMatrixClient:
    """Build an API key client on Home Assistant's shared aiohttp session."""
    auth = ApiKeyAuth(
        async_get_clientsession(hass), base_url, api_key, user_agent=USER_AGENT
    )
    return IMatrixClient(auth)


class StaticTokenAuth(TokenAuth):
    """Uses one access token as is. For validating a token in the config flow."""

    def __init__(self, hass: HomeAssistant, access_token: str) -> None:
        """Wrap a fresh access token."""
        super().__init__(
            async_get_clientsession(hass), DEFAULT_BASE_URL, user_agent=USER_AGENT
        )
        self._token = access_token

    async def async_get_access_token(self) -> str:
        """Return the token."""
        return self._token


class SessionTokenAuth(TokenAuth):
    """Refreshes through Home Assistant's OAuth2 session before each request."""

    def __init__(self, hass: HomeAssistant, session: OAuth2Session) -> None:
        """Wrap the config entry's OAuth2 session."""
        super().__init__(
            async_get_clientsession(hass), DEFAULT_BASE_URL, user_agent=USER_AGENT
        )
        self._oauth = session

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing (and rotating) it when due."""
        try:
            await self._oauth.async_ensure_token_valid()
        except OAuth2TokenRequestReauthError as err:
            # A 4xx on refresh: revoked, expired or replayed. HA starts reauth.
            raise IMatrixAuthError("refresh token rejected") from err
        except (OAuth2TokenRequestError, aiohttp.ClientError) as err:
            raise IMatrixConnectionError("token refresh failed") from err
        return str(self._oauth.token["access_token"])


def create_token_client(hass: HomeAssistant, session: OAuth2Session) -> IMatrixClient:
    """Build an OAuth2 client for a config entry."""
    return IMatrixClient(SessionTokenAuth(hass, session))
