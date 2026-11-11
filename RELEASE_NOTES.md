# Samsung AC Advanced 1.0.0 — release notes

Samsung AC Advanced 1.0.0 is the first public release of the project.

## What Samsung AC Advanced adds

Samsung AC Advanced controls Samsung air conditioners through the SmartThings REST API and exposes Samsung-specific features that are not all available through the standard Home Assistant SmartThings climate entity.

The release includes normal HVAC controls, Samsung Optional Modes, self-cleaning, sound, display-lighting commands, filter information and two Home Assistant-side thermal controllers: Eco and Frost Protection.

## Highlights

### Eco

Eco is a simulated controller rather than a Samsung preset. It combines:

- a logical Eco target temperature;
- an independent restart margin;
- external temperature-sensor support with Samsung internal-sensor fallback;
- external-sensor Sleep with physical Power Off;
- internal-sensor Stand-by;
- coordinator-confirmed serial transitions;
- Original Snapshot restore;
- deferred Pending Restore after a deliberate Mode Off during Eco Sleep.

The visible preset remains Eco while the controller owns the session, even when Samsung Quiet is used as a background implementation detail.

### Frost Protection

Frost Protection is independent from Eco and is available in Heat or physical Off.

- Adjustable target: 7.0–10.0 °C in 0.5 °C steps.
- Fixed hysteresis: 1.0 °C.
- Fixed heating profile: Heat, 16 °C, Fan Auto, Oscillation Fixed, Samsung Optional Mode None.
- Physical Power Off above the upper threshold while the Frost Protection preset remains selected.
- Sound/Beep, Display and Auto Clean are not modified by the routine.

### Advanced Samsung controls

- None, Sleep, Boost and Quiet optional modes.
- Auto, Low, Medium, High and Turbo fan modes.
- Vertical oscillation control.
- Self-cleaning.
- Sound effect.
- Stateless display-lighting On/Off commands.
- Filter status, usage, capacity, warning threshold and reminder reset.

### Diagnostics

The per-device Debug mode switch enables useful controller, coordinator and capability-change diagnostics without requiring a Samsung AC Advanced logger entry in `configuration.yaml`.

Routine successful fetch timing is filtered to keep logs readable; real coordinator failures and warnings/errors remain visible.

## Compatibility

The release was developed and tested primarily with a Samsung Room A/C reporting SmartThings model identifier `ARTIK051_PRAC_20K`.

Support for other Samsung air conditioners depends on the SmartThings capabilities exposed by each model and firmware version. The developer cannot validate other models directly, so broader compatibility depends on community testing, feedback and contributions.

## Installation

Samsung AC Advanced can be installed from its GitHub repository through the Home Assistant Marketplace as a custom repository, or manually by copying `custom_components/samsung_ac_advanced/` into Home Assistant's `/config/custom_components/` directory.

Catalog inclusion is a separate review process and may take time after the 1.0.0 release.

## SmartThings authentication notice

Version 1.0.0 uses SmartThings Personal Access Token authentication.

Samsung has announced that affected legacy PATs expire on **December 15, 2026**. A future authentication migration or another supported SmartThings API access method will be required for continued operation where those legacy PATs are affected.

## Upgrade notes

Existing compatible pre-release entity registry records and stored Eco state are migrated conservatively. Obsolete legacy entity entries are removed only where the replacement is known and stored controller context is not resurrected when it cannot be mapped safely to the current control model.

Back up the existing custom component before upgrading from a pre-release installation.

## Known limitations

See `KNOWN_ISSUES.md` for the current release-level compatibility notes.

There are no known release-blocking defects in the tested 1.0.0 feature set at the time of publication.

## Community direction

Samsung AC Advanced originated as a private project and is shared publicly so its additional controls and controller architecture may also be useful to others.

Further development is not guaranteed. Additional Samsung model support and feature expansion will depend primarily on community testing and contributions unless they also serve a personal need of the developer.

## Disclaimer

Samsung AC Advanced is unofficial and is not affiliated with or endorsed by Samsung Electronics, SmartThings or the Home Assistant project.

Development has included AI-assisted coding, analysis and documentation together with manual hardware testing and iterative Home Assistant validation.
