"""Application credentials for iMatrix OAuth2 sign-in."""

from __future__ import annotations

from typing import Any

from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import OAUTH2_AUTHORIZE_URL, OAUTH2_SCOPE, OAUTH2_TOKEN_URL


class IMatrixOAuth2Implementation(
    config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce
):
    """Authorization code with PKCE (S256) against the iMatrix cloud."""

    @property
    def name(self) -> str:
        """Name shown when more than one implementation exists."""
        return "iMatrix"

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Add the read-only scope to the authorize request."""
        return {"scope": OAUTH2_SCOPE, **super().extra_authorize_data}


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the iMatrix authorization server."""
    return AuthorizationServer(
        authorize_url=OAUTH2_AUTHORIZE_URL, token_url=OAUTH2_TOKEN_URL
    )


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return the PKCE implementation for the stored client credential."""
    return IMatrixOAuth2Implementation(
        hass,
        auth_domain,
        credential.client_id,
        OAUTH2_AUTHORIZE_URL,
        OAUTH2_TOKEN_URL,
        credential.client_secret,
    )
