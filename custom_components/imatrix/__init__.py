"""The iMatrix integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ImplementationUnavailableError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from pyimatrix import (
    Account,
    IMatrixAuthError,
    IMatrixClient,
    IMatrixError,
    IMatrixPermissionError,
)

from .backfill import StatisticsBackfill
from .const import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH2,
    CONF_AUTH_TYPE,
    CONF_BASE_URL,
    CONF_FLOOR_PLAN_ID,
    CONF_PUSH,
    CONF_SHARE_DEVICES,
    DOMAIN,
    KEY_EXPIRY_WARNING,
    OAUTH2_CLIENT_ID,
    OAUTH2_CLIENT_SECRET,
    OAUTH2_REVOKE_URL,
)
from .coordinator import IMatrixCoordinator
from .floor_plan import FloorPlanManager, FloorPlanStore
from .helpers import create_api_key_client, create_token_client
from .push import IMatrixPush
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

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
    floor_plan: FloorPlanManager | None = None


type IMatrixConfigEntry = ConfigEntry[IMatrixData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions. They live for the whole HA run, not per entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IMatrixConfigEntry) -> bool:
    """Set up iMatrix from a config entry."""
    client = await _async_create_client(hass, entry)
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

    if entry.data.get(CONF_AUTH_TYPE) == AUTH_TYPE_API_KEY:
        _check_key_expiry(hass, entry, account)
    coordinator = IMatrixCoordinator(hass, entry, client)
    coordinator.backfill = StatisticsBackfill(hass, entry, coordinator)
    await coordinator.backfill.async_load()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = IMatrixData(
        client=client, account=account, coordinator=coordinator
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    plan_id = entry.options.get(CONF_FLOOR_PLAN_ID) or None
    share = bool(entry.options.get(CONF_SHARE_DEVICES, False)) and plan_id is not None
    if plan_id or CONF_SHARE_DEVICES in entry.options:
        manager = FloorPlanManager(hass, entry, client, plan_id, share)
        entry.runtime_data.floor_plan = manager
        entry.async_create_background_task(
            hass, manager.async_start(), f"{DOMAIN}_floor_plan_{entry.entry_id}"
        )
        entry.async_on_unload(manager.async_stop)

    if entry.options.get(CONF_PUSH, True):
        push = IMatrixPush(hass, entry, coordinator, client)
        coordinator.push = push
        entry.async_create_background_task(
            hass, push.async_run(), f"{DOMAIN}_stream_{entry.entry_id}"
        )
        entry.async_on_unload(push.async_stop)
    return True


async def _async_create_client(
    hass: HomeAssistant, entry: IMatrixConfigEntry
) -> IMatrixClient:
    if entry.data.get(CONF_AUTH_TYPE) == AUTH_TYPE_OAUTH2:
        # The public client credential ships with the integration; make sure it
        # is registered even if application_credentials storage was reset.
        await async_import_client_credential(
            hass,
            DOMAIN,
            ClientCredential(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET, name="iMatrix"),
        )
        try:
            implementation = await async_get_config_entry_implementation(hass, entry)
        except ImplementationUnavailableError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            ) from err
        return create_token_client(hass, OAuth2Session(hass, entry, implementation))
    return create_api_key_client(
        hass, entry.data[CONF_BASE_URL], entry.data[CONF_API_KEY]
    )


async def async_migrate_entry(hass: HomeAssistant, entry: IMatrixConfigEntry) -> bool:
    """Migrate entries. 1.1 had API keys only; 1.2 records the auth type."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_AUTH_TYPE: AUTH_TYPE_API_KEY},
            minor_version=2,
        )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: IMatrixConfigEntry) -> None:
    """Withdraw a shared device list, then revoke the OAuth2 grant. Never raises."""
    await _async_withdraw_shared_devices(hass, entry)
    if entry.data.get(CONF_AUTH_TYPE) != AUTH_TYPE_OAUTH2:
        return
    token = entry.data.get("token", {}).get("refresh_token")
    if not token:
        return
    try:
        await async_get_clientsession(hass).post(
            OAUTH2_REVOKE_URL,
            data={"token": token, "client_id": OAUTH2_CLIENT_ID},
            timeout=aiohttp.ClientTimeout(total=10),
        )
    except aiohttp.ClientError, TimeoutError:
        _LOGGER.debug("Could not revoke the iMatrix authorization")


async def _async_withdraw_shared_devices(
    hass: HomeAssistant, entry: IMatrixConfigEntry
) -> None:
    """Delete the shared device list and the id map (spec 822 FR-29)."""
    store = FloorPlanStore(hass, entry.entry_id)
    await store.async_load()
    try:
        if store.data.get("shared"):
            client = await _async_create_client(hass, entry)
            manager = FloorPlanManager(hass, entry, client, None, False)
            await manager.async_withdraw_devices()
    except Exception as err:
        _LOGGER.debug("Could not withdraw the shared device list: %s", err)
    await store.async_remove()


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
