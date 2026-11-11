# Samsung AC Advanced — architecture and design rationale

This document describes the stable architecture behind Samsung AC Advanced 1.0.0. It is intended for maintainers, contributors and advanced users who need to understand why the integration behaves as it does.

## 1. Design goals

Samsung AC Advanced was designed around several constraints observed in real SmartThings-controlled Samsung air conditioners:

- SmartThings is cloud-based and coordinator status may lag physical action.
- Some useful Samsung capabilities are not exposed by the standard Home Assistant SmartThings climate entity.
- A Samsung command can normalize other settings as a side effect.
- Multi-step controller actions must not overlap with newer explicit user intent.
- Repeated blind retries can create API pressure and make state ownership ambiguous.

The resulting architecture prioritizes confirmed state, explicit ownership and simple serial transitions.

## 2. Shared coordinator as the confirmed state source

Each configured air conditioner has one shared `DataUpdateCoordinator`.

The coordinator is the integration's source of confirmed SmartThings state. Entity code may present short-lived optimistic user intent where appropriate, but controller decisions are reconciled against coordinator-confirmed values.

Incoming status processing is never intentionally delayed by outbound command spacing.

## 3. Outbound command discipline

Separate outgoing Samsung commands are spaced by approximately one second at the API transport layer. This is transport discipline only; it is not a status delay or retry strategy.

Controller-owned sequences follow the stronger rule:

**one command → coordinator confirmation → next command**

There is no generic retry/backoff loop and no artificial fixed wait for SmartThings state confirmation. A controller may request one fast coordinator refresh after an actually sent command; normal coordinator cadence remains the fallback.

## 4. Command ownership

At any time, a multi-step controller or restore sequence has a single logical owner.

Important ownership rules:

- newer explicit user intent wins over older not-yet-sent controller intent;
- an already-sent command may still be allowed to confirm, but obsolete unsent remainder must not continue afterward;
- Eco, Frost Protection and deferred restore ownership are mutually coordinated;
- normal confirmed SmartThings state becomes authoritative again when controller ownership ends.

This ownership model prevents overlapping controller sequences and reduces race conditions between Home Assistant UI actions and delayed SmartThings confirmations.

## 5. Normal manual control

Manual Mode, target temperature, Fan, Oscillation and Samsung Optional Mode use the coordinator as confirmed truth.

When Power is Off and a mode requiring Power On is selected, the intended HVAC mode is staged until physical Power On is coordinator-confirmed. Only then is the mode transition considered complete.

Samsung may normalize requested combinations. Outside controller-owned transitions, confirmed Samsung state is accepted rather than corrected indefinitely.

## 6. Eco design rationale

Eco is a simulated Home Assistant-side controller, not a Samsung Optional Mode.

The design deliberately separates:

- logical Eco target temperature;
- Eco restart margin/hysteresis;
- physical Samsung setpoint;
- active heating/cooling profile;
- rest behaviour using Sleep or Stand-by;
- restore ownership.

### 6.1 Temperature policy

Eco uses one logical target in 0.5 °C steps and one independent margin.

For cooling:

- Running → rest when room temperature is at or below the Eco target.
- Rest → resume when room temperature is at or above target + margin.

For heating:

- Running → rest when room temperature is at or above the Eco target.
- Rest → resume when room temperature is at or below target - margin.

Threshold comparisons are inclusive.

Samsung receives the normalized whole-degree physical target required by the hardware. The logical half-degree target remains the controller threshold.

### 6.2 Eco active profile

Eco Running uses:

- Heat or Cool according to the selected direction;
- Fan Auto;
- Oscillation Fixed;
- physical Samsung Quiet in the background.

The visible simulated preset remains Eco. Background physical Quiet must not appear as a user-selected Quiet preset while Eco owns the session.

### 6.3 External-sensor Sleep

When the configured external temperature sensor is usable, Eco may physically power the air conditioner Off after reaching target.

Before Sleep, the controller captures the relevant pre-Sleep auxiliary state. Auto Clean is temporarily disabled when necessary before physical Off and restored on resume or on a deliberate user Off handoff.

### 6.4 Internal-sensor Stand-by

When only the Samsung internal room-temperature sensor is available, Eco uses a powered Stand-by state so temperature measurement remains suitable for control.

The Stand-by profile is:

- Fan/Wind mode;
- Fan Low;
- Oscillation Fixed;
- physical Quiet restored after the base mode transition is confirmed.

Samsung mode transitions can reset Quiet. The controller therefore confirms the base Stand-by or Running state first and repairs Quiet as a separate serial step where required.

## 7. Original Snapshot and restore semantics

Before Eco takes ownership, the integration captures an immutable Original Snapshot of the settings that Eco may need to restore.

The snapshot includes, where applicable:

- HVAC mode;
- target temperature;
- fan mode;
- oscillation mode;
- Samsung Optional Mode;
- Sound;
- Auto Clean.

A normal Eco exit restores only confirmed differences and does so serially. The simulated Eco preset remains visible until the restore has completed.

If the restore itself cannot complete through a valid recoverable path, ownership is released and coordinator-confirmed Samsung state becomes authoritative.

## 8. Pending Restore

A deliberate **Mode Off** during Eco Sleep is treated differently from a normal Eco exit.

Because the user explicitly wants the unit to remain Off, the Original Snapshot is retained as a deferred Pending Restore. Off-safe auxiliary values such as Auto Clean and Sound may be restored immediately while physical Power remains Off. Active settings such as target, Fan and Oscillation remain deferred.

On the next deliberate Power On, Pending Restore is applied serially and confirmed before normal SmartThings ownership is released.

If a deferred restore has already started and the user explicitly selects Mode Off again, that newer user intent cancels the active old restore destination. A dormant Pending Restore that is merely waiting while the unit is already Off is retained.

## 9. Frost Protection design rationale

Frost Protection is intentionally independent from Eco.

Its purpose is narrow and predictable: prevent room temperature from falling below a low user-defined threshold without inheriting Eco's Sleep/Stand-by and snapshot complexity.

Availability:

- available in Heat and physical Off;
- unavailable in Auto, Cool, Dry and Fan/Wind;
- mutually exclusive with Eco.

Temperature policy:

- target range 7.0–10.0 °C;
- step 0.5 °C;
- fixed hysteresis 1.0 °C;
- at or below target → heating required;
- at or above target + 1.0 °C → physical Off.

Heating profile:

- Heat;
- physical target 16 °C;
- Fan Auto;
- Oscillation Fixed;
- Samsung Optional Mode None/Off.

Frost Protection does not own Sound/Beep, Display or Auto Clean.

The configured alternative room-temperature sensor is preferred. If unavailable or invalid, the controller falls back to Samsung's internal room-temperature measurement.

## 10. User overrides during Frost Protection

Heat and Off are compatible with active Frost Protection and do not terminate it.

The following explicit user changes terminate Frost Protection before the newer command proceeds:

- normal target temperature;
- Fan;
- Oscillation;
- another preset;
- an incompatible HVAC mode.

Sound/Beep and Display changes are independent and do not terminate Frost Protection.

## 11. Debug logging

Debug mode is implemented per configured device and does not require a Home Assistant `configuration.yaml` logger override.

When Debug mode is enabled, useful coordinator, Eco, Frost and capability-change diagnostics are emitted. Routine successful fetch-timing messages are filtered because they add substantial log volume without useful state information.

Coordinator failures, exceptions, warnings and errors remain visible.

## 12. Compatibility and migrations

The integration preserves legacy entity IDs and stored controller data where a safe migration is possible. Obsolete pre-release registry entities are removed only where a replacement entity has a clear one-to-one purpose.

Legacy Eco Min/Max/offset data is migrated conservatively. Where an old corridor cannot be mapped unambiguously to the current fixed-target model, the controller uses the defined default rather than inventing a midpoint.

Persisted controller contexts that no longer match the current control model are not resurrected into active ownership.

## 13. Extending support to other Samsung models

Samsung models may expose different SmartThings capabilities and may react differently to identical command combinations.

Contributions for additional models should therefore preserve these principles:

1. coordinator-confirmed state remains authoritative;
2. controller commands stay serialized;
3. user intent must preempt obsolete unsent controller work;
4. model-specific capability differences should be documented rather than hidden behind assumptions;
5. unsupported physical features should not be exposed merely because metadata advertises them.

## 14. Release policy

The 1.0.0 branch is feature-frozen. Before public release, changes should be limited to confirmed bugs, release-blocking compatibility issues, documentation and packaging.

Future feature work should be developed after the 1.0.0 release rather than folded into the release candidate. Any such extensions are expected primarily through community contributions unless they also serve a personal need of the developer.
