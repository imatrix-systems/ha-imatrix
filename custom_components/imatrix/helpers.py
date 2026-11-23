"""Shared helpers for setup and the config flow."""

from __future__ import annotations

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pyimatrix import ApiKeyAuth, IMatrixClient


def create_api_key_client(
    hass: HomeAssistant, base_url: str, api_key: str
) -> IMatrixClient:
    """Build a client on Home Assistant's shared aiohttp session."""
    auth = ApiKeyAuth(
        async_get_clientsession(hass),
        base_url,
        api_key,
        user_agent=f"HomeAssistant/{HA_VERSION}",
    )
    return IMatrixClient(auth)
