"""The iMatrix integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from pyimatrix import (
    Account,
    IMatrixAuthError,
    IMatrixClient,
    IMatrixError,
    IMatrixPermissionError,
)

from .const import CONF_BASE_URL, DOMAIN
from .helpers import create_api_key_client

PLATFORMS: list[Platform] = []


@dataclass
class IMatrixData:
    """Runtime data stored on the config entry."""

    client: IMatrixClient
    account: Account


type IMatrixConfigEntry = ConfigEntry[IMatrixData]


async def async_setup_entry(hass: HomeAssistant, entry: IMatrixConfigEntry) -> bool:
    """Set up iMatrix from a config entry."""
    client = create_api_key_client(
        hass, entry.data[CONF_BASE_URL], entry.data[CONF_API_KEY]
    )
    try:
        account = await client.get_account()
    except (IMatrixAuthError, IMatrixPermissionError) as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except IMatrixError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err

    entry.runtime_data = IMatrixData(client=client, account=account)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IMatrixConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
