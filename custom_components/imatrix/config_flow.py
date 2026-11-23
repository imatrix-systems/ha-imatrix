"""Config flow for the iMatrix integration.

Phase H0 scaffold: collects an API key without validating it. Phase H1 validates the key
against GET /api/v1/integration/account and sets the unique ID to the org ID.
"""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class IMatrixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iMatrix."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            return self.async_create_entry(
                title="iMatrix",
                data={
                    CONF_API_KEY: user_input[CONF_API_KEY],
                    CONF_BASE_URL: DEFAULT_BASE_URL,
                },
            )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)
