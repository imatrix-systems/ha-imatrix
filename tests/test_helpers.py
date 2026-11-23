from homeassistant.core import HomeAssistant
from pyimatrix import ApiKeyAuth

from custom_components.imatrix.helpers import create_api_key_client


async def test_create_api_key_client(hass: HomeAssistant) -> None:
    client = create_api_key_client(hass, "https://api.test.invalid", "imx_x_y")
    assert isinstance(client.auth, ApiKeyAuth)
