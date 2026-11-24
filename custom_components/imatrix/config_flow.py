"""Config flow for the iMatrix integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import config_entry_oauth2_flow
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
    IMatrixClient,
    IMatrixConnectionError,
    IMatrixPermissionError,
    IMatrixUnsupportedSchemaError,
)

from .const import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH2,
    CONF_ADVANCED,
    CONF_AUTH_TYPE,
    CONF_BASE_URL,
    CONF_FLOOR_PLAN_ID,
    CONF_GROUP_IDS,
    CONF_INCLUDE_UNMAPPED,
    CONF_PUSH,
    CONF_SCAN_INTERVAL,
    CONF_SHARE_DEVICES,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    OAUTH2_CLIENT_ID,
    OAUTH2_CLIENT_SECRET,
    OAUTH2_SCOPE,
    OAUTH2_SCOPE_HA_DEVICES,
    REQUIRED_PERMISSIONS,
)
from .helpers import StaticTokenAuth, create_api_key_client

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


class IMatrixConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Sign in with iMatrix (OAuth2, default) or with an API key."""

    DOMAIN = DOMAIN
    VERSION = 1
    MINOR_VERSION = 2

    @property
    def logger(self) -> logging.Logger:
        """Logger for the OAuth2 helper."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Ask for the sharing scope on reauth when sharing is on (spec 822 FR-30)."""
        if self.source == SOURCE_REAUTH and self._get_reauth_entry().options.get(
            CONF_SHARE_DEVICES
        ):
            return {"scope": f"{OAUTH2_SCOPE} {OAUTH2_SCOPE_HA_DEVICES}"}
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IMatrixOptionsFlow:
        """Return the options flow."""
        return IMatrixOptionsFlow()

    async def _async_check(
        self, client: IMatrixClient
    ) -> tuple[Account | None, str | None]:
        """Return the account, or an error key."""
        try:
            account = await client.get_account()
        except tuple(VALIDATION_ERRORS) as err:
            return None, VALIDATION_ERRORS[type(err)]
        except Exception:
            _LOGGER.exception("Unexpected error validating iMatrix credentials")
            return None, "unknown"
        if not account.permissions >= REQUIRED_PERMISSIONS:
            return None, "insufficient_permissions"
        return account, None

    async def _async_validate(
        self, base_url: str, api_key: str
    ) -> tuple[Account | None, dict[str, str]]:
        """Return the account, or form errors."""
        account, error = await self._async_check(
            create_api_key_client(self.hass, base_url, api_key)
        )
        return account, ({"base": error} if error else {})

    @staticmethod
    def _title(account: Account) -> str:
        return account.org_name or f"iMatrix org {account.org_id}"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to sign in."""
        return self.async_show_menu(step_id="user", menu_options=["oauth", "api_key"])

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with iMatrix. The public client credential ships with us."""
        await async_import_client_credential(
            self.hass,
            DOMAIN,
            ClientCredential(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET, name="iMatrix"),
        )
        return await self.async_step_pick_implementation()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Validate the new token, then create or update the entry."""
        account, error = await self._async_check(
            IMatrixClient(StaticTokenAuth(self.hass, data["token"]["access_token"]))
        )
        if account is None:
            return self.async_abort(reason=error or "unknown")
        await self.async_set_unique_id(str(account.org_id))
        entry_data = {
            **data,
            CONF_AUTH_TYPE: AUTH_TYPE_OAUTH2,
            CONF_BASE_URL: DEFAULT_BASE_URL,
        }
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=entry_data
            )
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                title=self._title(account),
                data=entry_data,
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._title(account), data=entry_data)

    async def async_step_api_key(
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
                    data={
                        CONF_AUTH_TYPE: AUTH_TYPE_API_KEY,
                        CONF_API_KEY: api_key,
                        CONF_BASE_URL: base_url,
                    },
                )
        return self.async_show_form(
            step_id="api_key",
            data_schema=_schema(base_url, collapsed=base_url == DEFAULT_BASE_URL),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the credential was rejected."""
        if entry_data.get(CONF_AUTH_TYPE) == AUTH_TYPE_OAUTH2:
            return await self.async_step_reauth_oauth()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tell the user to sign in again, then start the OAuth2 flow."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_oauth",
                description_placeholders={"org": self._get_reauth_entry().title},
            )
        return await self.async_step_oauth()

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
        """Change the credential or base URL of an existing entry."""
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_AUTH_TYPE) == AUTH_TYPE_OAUTH2:
            return await self.async_step_oauth()
        errors: dict[str, str] = {}
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
            plan_id = user_input.get(CONF_FLOOR_PLAN_ID) or ""
            return self.async_create_entry(
                data={
                    CONF_GROUP_IDS: [int(g) for g in user_input[CONF_GROUP_IDS]],
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_INCLUDE_UNMAPPED: user_input[CONF_INCLUDE_UNMAPPED],
                    CONF_PUSH: user_input[CONF_PUSH],
                    # Spec 822 FR-23, FR-28: sharing needs a plan; no plan turns sharing
                    # off.
                    CONF_FLOOR_PLAN_ID: plan_id,
                    CONF_SHARE_DEVICES: bool(plan_id)
                    and bool(user_input.get(CONF_SHARE_DEVICES)),
                }
            )
        options = self.config_entry.options
        # Spec 822 FR-23: the org's floor plans, when the key can read them (404 or 403
        # means none).
        plan_choices = [SelectOptionDict(value="", label="Off")]
        try:
            plans = await self.config_entry.runtime_data.client.get_floor_plans()
            plan_choices += [SelectOptionDict(value=p.id, label=p.name) for p in plans]
        except Exception:
            plans = []
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
                    CONF_PUSH, default=options.get(CONF_PUSH, True)
                ): BooleanSelector(),
                vol.Required(
                    CONF_INCLUDE_UNMAPPED,
                    default=options.get(CONF_INCLUDE_UNMAPPED, False),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_FLOOR_PLAN_ID, default=options.get(CONF_FLOOR_PLAN_ID, "")
                ): SelectSelector(SelectSelectorConfig(options=plan_choices)),
                vol.Required(
                    CONF_SHARE_DEVICES, default=options.get(CONF_SHARE_DEVICES, False)
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
