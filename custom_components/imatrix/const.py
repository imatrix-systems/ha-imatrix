"""Constants for the iMatrix integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "imatrix"
MANUFACTURER: Final = "iMatrix Systems"

CONF_BASE_URL: Final = "base_url"
CONF_ADVANCED: Final = "advanced"
CONF_GROUP_IDS: Final = "group_ids"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_INCLUDE_UNMAPPED: Final = "include_unmapped"

DEFAULT_BASE_URL: Final = "https://api.imatrixsys.com"
WEB_APP_URL: Final = "https://cloud.imatrixsys.com"

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 1800
MAX_BACKOFF: Final = timedelta(minutes=30)
# Re-read the device list every N polls; new devices appear within N x interval.
DEVICE_REFRESH_EVERY: Final = 10

# Event feed: pages read per poll before yielding to the next poll.
MAX_EVENT_PAGES: Final = 5
# Vehicles whose fault list is fetched per poll until every vehicle has one.
FAULT_FETCHES_PER_POLL: Final = 10
# Raise a repair issue when rate limited this long, or this close to key expiry.
RATE_LIMIT_ISSUE_AFTER: Final = timedelta(minutes=30)
KEY_EXPIRY_WARNING: Final = timedelta(days=14)
# The active-faults sensor lists at most this many codes as an attribute.
MAX_FAULT_CODES: Final = 20

# Speed above which a vehicle counts as moving.
MOVING_SPEED_KMH: Final = 3.0
# HA wants accuracy in meters. HDOP x this factor is the usual rough estimate.
HDOP_TO_METERS: Final = 5.0

# The read-only preset grants these. The integration cannot work without them.
REQUIRED_PERMISSIONS: Final = frozenset({"device:read", "sensor_data:read"})
