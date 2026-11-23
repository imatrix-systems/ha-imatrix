"""Config flow for the iMatrix integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from pyimatrix import (
    Account,
    IMatrixAuthError,
    IMatrixConnectionError,
    IMatrixPermissionError,
    IMatrixUnsupportedSchemaError,
)

from .const import (
    CONF_ADVANCED,
    CONF_BASE_URL,
    CONF_GROUP_IDS,
    CONF_INCLUDE_UNMAPPED,
    CONF_SCAN_INTERVAL,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    REQUIRED_PERMISSIONS,
)
from .helpers import create_api_key_client

_LOGGER = logging.getLogger(__name__)

API_KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def _schema(base_url: str, *, collapsed: bool) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY): API_KEY_SELECTOR,
            vol.Required(CONF_ADVANCED): section(
                vol.Schema(
                    {vol.Required(CONF_BASE_URL, default=base_url): URL_SELECTOR}
                ),
                {"collapsed": collapsed},
            ),
        }
    )


# Exact exception type to form error key.
VALIDATION_ERRORS: dict[type[Exception], str] = {
    IMatrixAuthError: "invalid_auth",
    IMatrixPermissionError: "insufficient_permissions",
    IMatrixUnsupportedSchemaError: "unsupported_api",
    IMatrixConnectionError: "cannot_connect",
}

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): API_KEY_SELECTOR})


class IMatrixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iMatrix."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IMatrixOptionsFlow:
        """Return the options flow."""
        return IMatrixOptionsFlow()

    async def _async_validate(
        self, base_url: str, api_key: str
    ) -> tuple[Account | None, dict[str, str]]:
        """Return the account, or form errors."""
        client = create_api_key_client(self.hass, base_url, api_key)
        try:
            account = await client.get_account()
        except tuple(VALIDATION_ERRORS) as err:
            return None, {"base": VALIDATION_ERRORS[type(err)]}
        except Exception:
            _LOGGER.exception("Unexpected error validating iMatrix credentials")
            return None, {"base": "unknown"}
        if not account.permissions >= REQUIRED_PERMISSIONS:
            return None, {"base": "insufficient_permissions"}
        return account, {}

    @staticmethod
    def _title(account: Account) -> str:
        return account.org_name or f"iMatrix org {account.org_id}"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for an API key."""
        errors: dict[str, str] = {}
        base_url = DEFAULT_BASE_URL
        if user_input is not None:
            base_url = user_input[CONF_ADVANCED][CONF_BASE_URL].rstrip("/")
            api_key = user_input[CONF_API_KEY].strip()
            account, errors = await self._async_validate(base_url, api_key)
            if account is not None:
                await self.async_set_unique_id(str(account.org_id))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._title(account),
                    data={CONF_API_KEY: api_key, CONF_BASE_URL: base_url},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(base_url, collapsed=base_url == DEFAULT_BASE_URL),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the key was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key for the same org."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            account, errors = await self._async_validate(
                entry.data[CONF_BASE_URL], api_key
            )
            if account is not None:
                await self.async_set_unique_id(str(account.org_id))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"org": entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the API key or base URL of an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        base_url = entry.data[CONF_BASE_URL]
        if user_input is not None:
            base_url = user_input[CONF_ADVANCED][CONF_BASE_URL].rstrip("/")
            api_key = user_input[CONF_API_KEY].strip()
            account, errors = await self._async_validate(base_url, api_key)
            if account is not None:
                await self.async_set_unique_id(str(account.org_id))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    title=self._title(account),
                    data_updates={CONF_API_KEY: api_key, CONF_BASE_URL: base_url},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(base_url, collapsed=base_url == DEFAULT_BASE_URL),
            errors=errors,
        )


class IMatrixOptionsFlow(OptionsFlowWithReload):
    """Choose groups, polling interval and unmapped channels. Saving reloads."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_GROUP_IDS: [int(g) for g in user_input[CONF_GROUP_IDS]],
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_INCLUDE_UNMAPPED: user_input[CONF_INCLUDE_UNMAPPED],
                }
            )
        options = self.config_entry.options
        paths = self.config_entry.runtime_data.coordinator.data.group_paths
        selected = [str(g) for g in options.get(CONF_GROUP_IDS, [])]
        # Keep selected groups visible even when no device is in them right now.
        choices = {str(g): p for g, p in paths.items()} | {
            g: f"Group {g}" for g in selected if int(g) not in paths
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_GROUP_IDS, default=selected): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=g, label=label)
                            for g, label in sorted(choices.items(), key=lambda i: i[1])
                        ],
                        multiple=True,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_INCLUDE_UNMAPPED,
                    default=options.get(CONF_INCLUDE_UNMAPPED, False),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
