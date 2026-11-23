# Design decisions

Newest last. Each entry: date, decision, reason.

## 2026-11-23: minimum Home Assistant version 2026.9.0

Home Assistant replaced voluptuous with probatio in 2026.9.0, and every core config flow in 2026.11
imports `probatio as vol`. The integration does the same so it can move to core without a rewrite.
Spec 817 NFR-7 said 2026.3.0 (for local `brand/` images); 2026.9.0 includes that.

## 2026-11-23: Python 3.14.2 for development and CI

Home Assistant 2026.11.2 requires Python 3.14.2 or later (PyPI metadata).
`pytest-homeassistant-custom-component` 0.13.375 tracks that release.
