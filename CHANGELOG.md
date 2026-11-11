# Changelog

All notable public changes to Samsung AC Advanced will be documented in this file.

## 1.0.0 — Unreleased

Initial public release.

### Added

- SmartThings REST-based Samsung air-conditioner control for Home Assistant.
- HVAC mode, target temperature, fan speed and oscillation entities.
- Samsung Optional Mode support for None, Sleep, Boost and Quiet.
- Simulated Eco controller with configurable target and margin, external/internal temperature-source support, Sleep/Stand-by routing and exact restore semantics.
- Simulated Frost Protection controller with a 7.0–10.0 °C user target and fixed 1.0 °C hysteresis.
- Self-cleaning and sound-effect switches.
- Stateless display-lighting On/Off buttons.
- Filter status, filter usage, filter capacity, configurable filter-warning threshold and filter reminder reset.
- Per-device Debug mode with readable controller/capability diagnostics and no required `configuration.yaml` logger override.
- English and Dutch translations.
- Conservative migration of supported pre-release entity and controller state.

### Architecture

- One shared `DataUpdateCoordinator` per configured air conditioner.
- Serialized controller transitions: one command, coordinator confirmation, then the next command.
- Explicit command ownership so newer user intent can supersede obsolete unsent controller work.
- No generic retry/backoff loop or artificial state-confirmation delay.
