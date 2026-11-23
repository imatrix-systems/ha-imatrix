# iMatrix for Home Assistant

Home Assistant integration for iMatrix environmental sensors (temperature, humidity and BLE probes) and
vehicles (QConnect gateways with GPS, OBD-II and J1939), distributed through HACS.

Status: pre-release scaffold (phase H0). It installs and loads but creates no entities yet. The
design is iMatrix spec 817.

## Development

Requires Python 3.14.2 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff check && uv run mypy custom_components/imatrix
```

## License

Apache-2.0
