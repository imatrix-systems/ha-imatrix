# Design decisions

Newest last. Each entry: date, decision, reason.

## 2026-11-23: minimum Home Assistant version 2026.9.0

Home Assistant replaced voluptuous with probatio in 2026.9.0, and every core config flow in 2026.11
imports `probatio as vol`. The integration does the same so it can move to core without a rewrite.
Spec 817 NFR-7 said 2026.3.0 (for local `brand/` images); 2026.9.0 includes that.

## 2026-11-23: Python 3.14.2 for development and CI

Home Assistant 2026.11.2 requires Python 3.14.2 or later (PyPI metadata).
`pytest-homeassistant-custom-component` 0.13.375 tracks that release.

## 2026-11-23: brand images from the web app source

Greg asked (Echelon T49) to use the icons already in the iMatrix web site and source. `brand/icon*.png`
are the globe mark from `frontend/src/assets/icons/logos/iMatrix.svg`, trimmed and centred on a
transparent square. `brand/logo*.png` come from `iMatrix_logo_with_name.svg`. `dark_logo*.png` are the
same SVG with the wordmark fill changed from #666 to #e2e2e2 so it reads on dark themes. The globe
reads on dark backgrounds, so there is no `dark_icon.png`.

## 2026-11-23: which channels become entities

Spec 817 FR-H3 says "include unmapped channels" defaults off. Environmental probes arrive unmapped
(`key = s<sensorId>`) with a quantity kind, and hiding them would hide the product. The rule is:

- Mapped keys always become entities. Engine detail keys and telematics event flags start disabled.
- Unmapped scalar channels with a quantity kind become entities. They start enabled on environmental
  and gateway devices, and disabled on vehicles, which carry hundreds of CAN signals.
- Unmapped channels with no quantity kind, and unmapped stateful channels, are created only with the
  option on.
- A percentage channel whose name contains "batt" gets the battery device class. The API has no
  battery key yet; remove this when B1 seeds one.

## 2026-11-23: new devices are added from a coordinator listener

New-device callbacks run as coordinator listeners, so platforms read the snapshot that contains the
new device. Calling them inside `_async_update_data` would run before `coordinator.data` is replaced.

## 2026-11-23: live check on cloud-dev, and what changed because of it

First run of the real integration against api-dev (Greg's test org, read-only key, 695 devices:
195 shipping-label and solar trackers, 300 simulated FC-201 vehicles, 200 simulated NEO-1 probes).
Setup loaded with 115 of 120 requests per minute left after the first poll.

- Alarm binary sensors are now created only for temperature, humidity, pressure and concentration
  channels. Before, the run created 9,879 binary sensors, most of them disabled alarm entities on
  accelerometer and voltage channels.
- A key sees only devices in groups granted to its user, even for an org administrator (backend ACL:
  org-wide visibility needs `org:manage`, which the read-only preset does not grant). The setup text
  and the `no_devices` repair say so.
- `kind` does not gate GPS or ignition entities: a tracker is created for any device with a latitude
  channel and the ignition sensor for any device with an ignition channel. Kind only gates
  vehicle-only entities (moving, faults, lamps).
- `/events` answers 404 until backend B3 deploys; the integration treats that as "no event feed".

## 2026-11-23: statistics backfill fills measurement sensors only

FR-H14 names measurement and total_increasing sensors. Only `measurement` sensors are filled: hourly
mean, min and max from `/history`, converted to the entity's display unit, imported as the entity's own
statistics. A total_increasing statistic carries a running `sum`; importing hours into a gap would
need every later row re-based. HA already attributes the whole increase across a gap to the first
hour after it, so odometer and engine-hours totals stay correct; only their hourly split is lost.
The last good poll is stored per entry, so a gap that spans a restart is found. Five devices are
filled per poll.

## 2026-11-23: unmapped vehicle channels need the option (Echelon Q908)

Greg chose to skip unmapped channels on vehicles unless "Include unmapped channels" is on. With the
option on they are created disabled. Unmapped environmental channels are unchanged.
