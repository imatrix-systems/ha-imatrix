"""The iMatrix integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from pyimatrix import (
    Account,
    IMatrixAuthError,
    IMatrixClient,
    IMatrixError,
    IMatrixPermissionError,
)

from .const import CONF_BASE_URL, DOMAIN
from .coordinator import IMatrixCoordinator
from .helpers import create_api_key_client

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]


@dataclass
class IMatrixData:
    """Runtime data stored on the config entry."""

    client: IMatrixClient
    account: Account
    coordinator: IMatrixCoordinator


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

    coordinator = IMatrixCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = IMatrixData(
        client=client, account=account, coordinator=coordinator
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IMatrixConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: IMatrixConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a device only when iMatrix no longer lists it."""
    return not any(
        (DOMAIN, str(sn)) in device_entry.identifiers
        for sn in entry.runtime_data.coordinator.data.devices
    )
