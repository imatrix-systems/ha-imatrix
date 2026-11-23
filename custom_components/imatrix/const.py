"""Constants for the iMatrix integration."""

from typing import Final

DOMAIN: Final = "imatrix"
MANUFACTURER: Final = "iMatrix Systems"

CONF_BASE_URL: Final = "base_url"
CONF_ADVANCED: Final = "advanced"
DEFAULT_BASE_URL: Final = "https://api.imatrixsys.com"

# The read-only preset grants these; the integration cannot work without them.
REQUIRED_PERMISSIONS: Final = frozenset({"device:read", "sensor_data:read"})
