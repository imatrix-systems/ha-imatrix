# iMatrix for Home Assistant

Home Assistant integration for iMatrix environmental sensors (temperature, humidity and BLE probes) and
vehicles (QConnect gateways with GPS, OBD-II and J1939), distributed through HACS.

Status: beta (0.1.2). The design is iMatrix spec 817.

## Install

The integration is not in the default list yet. Add it from its GitHub link.

On Home Assistant 2026.11 or later, use the Marketplace:

1. Go to **Settings > Marketplace** and select **Add from link**.
2. Enter `imatrix-systems/ha-imatrix` and select **Add**. The first time, connect a GitHub account
   with the code it shows.
3. Open **iMatrix** in the list and select **Install**.

On earlier versions with HACS:

1. Open **HACS**, then the three-dot menu, then **Custom repositories**.
2. Enter `https://github.com/imatrix-systems/ha-imatrix`, choose the type **Integration**, and click **Add**.
3. Find **iMatrix** in HACS, download it, and restart Home Assistant.

Home Assistant 2026.9.0 or later is required.

## Set up

Go to **Settings > Devices & services > Add integration** and pick **iMatrix**. Then choose one of:

- **Sign in with iMatrix**. Your browser opens the iMatrix Cloud, where you pick one organization and
  allow read-only access. The first time, my.home-assistant.io asks for your Home Assistant URL
  (for example `http://192.168.1.10:8123`) so it can send you back. Home Assistant refreshes the access token on its own. You can remove the
  access later in iMatrix Cloud account settings.
- **API key**. Create a key in iMatrix Cloud and paste it in. Home Assistant warns you 14 days before
  the key expires.

The options let you limit the integration to some groups, change the poll interval, and turn live
updates off.

## What you get

Each iMatrix device becomes a Home Assistant device. Environmental sensors get a sensor per channel
(temperature, humidity and so on), plus connectivity, last seen and optional alarm entities.
Vehicles also get a device tracker, ignition, moving, active faults and the J1939 warning lamps,
with event entities for fault set and cleared, geofence enter and exit, and alerts.

Readings arrive over a live connection to the iMatrix Cloud. If that connection drops, the
integration falls back to polling and raises a repair after 30 minutes. After an outage it fills
the gap in long-term statistics from iMatrix history.

The actions `imatrix.get_faults`, `imatrix.get_trips` and `imatrix.get_history` return data for
scripts and automations. Six automation blueprints are in `blueprints/automation/imatrix`.

## Development

Requires Python 3.14.2 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff check && uv run mypy custom_components/imatrix
```

## License

Apache-2.0
