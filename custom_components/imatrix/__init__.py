"""The iMatrix integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from pyimatrix import (
    Account,
    IMatrixAuthError,
    IMatrixClient,
    IMatrixError,
    IMatrixPermissionError,
)

from .const import CONF_BASE_URL, DOMAIN, KEY_EXPIRY_WARNING
from .coordinator import IMatrixCoordinator
from .helpers import create_api_key_client
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.EVENT,
    Platform.SENSOR,
]


@dataclass
class IMatrixData:
    """Runtime data stored on the config entry."""

    client: IMatrixClient
    account: Account
    coordinator: IMatrixCoordinator


type IMatrixConfigEntry = ConfigEntry[IMatrixData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions. They live for the whole HA run, not per entry."""
    async_setup_services(hass)
    return True


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

    _check_key_expiry(hass, entry, account)
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


def _check_key_expiry(
    hass: HomeAssistant, entry: IMatrixConfigEntry, account: Account
) -> None:
    """Raise a repair issue 14 days before the API key expires."""
    issue_id = f"api_key_expiring_{entry.entry_id}"
    expires = account.expires_at
    if expires is None or expires - dt_util.utcnow() > KEY_EXPIRY_WARNING:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="api_key_expiring",
        translation_placeholders={
            "title": entry.title,
            "date": dt_util.as_local(expires).date().isoformat(),
        },
    )
