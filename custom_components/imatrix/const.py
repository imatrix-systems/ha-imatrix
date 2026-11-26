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
CONF_PUSH: Final = "push"
# Spec 822 FR-23, FR-28: floor plan sync (plan id, empty for off) and opt-in device list
# sharing.
CONF_FLOOR_PLAN_ID: Final = "floor_plan_id"
CONF_SHARE_DEVICES: Final = "share_devices"

DEFAULT_BASE_URL: Final = "https://api.imatrixsys.com"

# OAuth2 (backend B5, spec 817 FR-C22). Home Assistant is a public client: PKCE,
# fixed client id, and a placeholder secret the server ignores.
CONF_AUTH_TYPE: Final = "auth_type"
AUTH_TYPE_API_KEY: Final = "api_key"
AUTH_TYPE_OAUTH2: Final = "oauth2"
OAUTH2_CLIENT_ID: Final = "home-assistant"
# Not a secret: the server ignores it for this public client.
OAUTH2_CLIENT_SECRET: Final = "public-client"  # noqa: S105
OAUTH2_AUTHORIZE_URL: Final = f"{DEFAULT_BASE_URL}/api/v1/oauth/authorize"
OAUTH2_TOKEN_URL: Final = f"{DEFAULT_BASE_URL}/api/v1/oauth/token"
OAUTH2_REVOKE_URL: Final = f"{DEFAULT_BASE_URL}/api/v1/oauth/revoke"
OAUTH2_SCOPE: Final = "imatrix.read"
# Spec 822 FR-30: asked for only when the user turns device sharing on.
OAUTH2_SCOPE_HA_DEVICES: Final = "imatrix.ha_devices"
WEB_APP_URL: Final = "https://cloud.imatrixsys.com"

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 1800
MAX_BACKOFF: Final = timedelta(minutes=30)
# While the live stream is up, poll only as a safety net.
PUSH_POLL_INTERVAL: Final = timedelta(minutes=5)
STREAM_ISSUE_AFTER: Final = timedelta(minutes=30)
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

# Statistics backfill after an outage (FR-H14).
BACKFILL_AFTER: Final = timedelta(hours=1)
BACKFILL_MAX: Final = timedelta(days=7)
BACKFILL_DEVICES_PER_POLL: Final = 5

# Speed above which a vehicle counts as moving.
MOVING_SPEED_KMH: Final = 3.0
# HA wants accuracy in meters. HDOP x this factor is the usual rough estimate.
HDOP_TO_METERS: Final = 5.0

# The read-only preset grants these. The integration cannot work without them.
REQUIRED_PERMISSIONS: Final = frozenset({"device:read", "sensor_data:read"})
