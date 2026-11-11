"""Simulated Eco preset controller for Samsung AC Advanced."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import SamsungACAPI
from .const import (
    CONF_ECO_TEMPERATURE_SENSOR,
    ECO_HYSTERESIS_DEFAULT,
    ECO_HYSTERESIS_MAX,
    ECO_HYSTERESIS_MIN,
    ECO_HYSTERESIS_STEP,
    ECO_SUPPORTED_HVAC_MODES,
    ECO_TARGET_TEMPERATURE_DEFAULT,
    SETPOINT_HVAC_MODES,
)
from .eco_override import EcoChangeOwner, EcoOverrideManager
from .eco_snapshot import EcoPreSleepSnapshot, EcoRestoreSnapshot, EcoSnapshotManager
from .eco_strategy import EcoExternalStrategy, EcoInternalStrategy, EcoStrategy
from .eco_transition import EcoTransitionManager
from .device_logger import SamsungACDeviceLoggerAdapter, SamsungACRuntimeLogState
from .setpoint import (
    normalize_half_degree_setpoint,
    normalize_physical_setpoint,
    resolve_target_temperature,
)

_LOGGER = logging.getLogger(__name__)
_STORAGE_VERSION = 1


class EcoState(StrEnum):
    """Internal Eco controller states."""

    DISABLED = "disabled"
    ARMED = "armed"
    RUNNING = "running"
    SATISFIED_OFF = "satisfied_off"
    SATISFIED_FAN_QUIET = "satisfied_fan_quiet"
    SUSPENDED = "suspended"
    TEMPERATURE_STALE = "temperature_stale"
    TEMPERATURE_UNAVAILABLE = "temperature_unavailable"
    INVALID_CONTEXT = "invalid_context"
    PENDING_RESTORE = "pending_restore"


class EcoFinishReason(StrEnum):
    """Canonical reasons for leaving an Eco session."""

    USER_OVERRIDE = "USER_OVERRIDE"
    USER_OFF = "USER_OFF"
    PRESET_OFF = "PRESET_OFF"
    MODE_CHANGED = "MODE_CHANGED"
    INTERNAL_FINISH = "INTERNAL_FINISH"
    ERROR = "ERROR"


_ACTIVE_STATES = {
    EcoState.ARMED,
    EcoState.RUNNING,
    EcoState.SATISFIED_OFF,
    EcoState.SATISFIED_FAN_QUIET,
    EcoState.SUSPENDED,
}


@dataclass(slots=True)
class EcoTemperatureReading:
    """Validated Eco temperature reading."""

    value: float | None
    updated_at: datetime | None
    source_entity_id: str | None
    valid: bool
    reason: str | None = None


@dataclass(slots=True)
class EcoPendingReconcile:
    """One staged desired-state operation waiting on coordinator status."""

    purpose: str
    desired: dict[str, Any]
    detail: str
    finish_eco: bool
    finish_reason: EcoFinishReason | None = None
    preserve_fields: tuple[str, ...] = ()
    in_flight_field: str | None = None


class SamsungACEcoController:
    """Own the state and lifecycle of the simulated Eco preset."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: SamsungACAPI,
        runtime_log_state: SamsungACRuntimeLogState | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        device_name = entry.data.get("device_name", entry.title)
        self.logger = SamsungACDeviceLoggerAdapter(
            _LOGGER, device_name, runtime_log_state
        )
        self.state = EcoState.DISABLED
        self.snapshots = EcoSnapshotManager()
        self.transitions = EcoTransitionManager(self.logger)
        self.overrides = EcoOverrideManager()
        self.target_temperature: float | None = None
        self._thermal_direction: str | None = None
        self.eco_target_temperature = ECO_TARGET_TEMPERATURE_DEFAULT
        self.eco_hysteresis = ECO_HYSTERESIS_DEFAULT
        self._eco_configuration_reconcile_pending = False
        self.temperature = EcoTemperatureReading(None, None, None, False, "not_evaluated")
        self._command_lock = asyncio.Lock()
        self._satisfied_at: datetime | None = None
        self._strategy: EcoStrategy = self._create_strategy()
        self._stored_context: dict[str, Any] | None = None
        self._restore_attempted = False
        self._last_observed: dict[str, Any] | None = None
        self._configuration_resume_in_progress = False
        self._target_reached_route: str | None = None
        self._pending_reconcile: EcoPendingReconcile | None = None
        # A fresh Eco selection may arrive while an Eco-exit restore still owns
        # one confirmed transport step.  Keep the new intent queued and hand
        # ownership over only after that in-flight step is coordinator-confirmed.
        self._queued_reenable_after_finish = False
        self._queued_reenable_snapshot: EcoRestoreSnapshot | None = None
        self._queued_reenable_overrides: dict[str, Any] = {}
        self._suspended_desired: dict[str, Any] | None = None
        self._suspended_baseline: dict[str, Any] | None = None
        self._pending_sleep_override: dict[str, Any] | None = None
        self._pending_sleep_user_off = False
        self._pending_restore_immediate_off_safe = False
        self._latest_status_for_temperature: dict[str, Any] = {}
        # The coordinator may register one lightweight callback that schedules a
        # fresh shared status read after an Eco-exit restore command.  The Eco
        # controller never reads SmartThings directly: coordinator.data remains
        # the single status truth and normal 30 s polling remains the fallback.
        self._confirmation_refresh_callback: Callable[[], None] | None = None
        # Set synchronously by a newer Eco -> None user intent so a target-reached
        # route that already owns the command lock cannot send a not-yet-sent
        # Stand-by/Sleep command after the user has asked to leave Eco.
        self._preset_off_requested = False
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORAGE_VERSION, f"{entry.domain}.{entry.entry_id}.eco"
        )

    def set_confirmation_refresh_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        """Register the coordinator-owned fast confirmation refresh scheduler."""

        self._confirmation_refresh_callback = callback

    def _request_confirmation_refresh(self) -> None:
        """Ask for one fresh coordinator read without waiting or polling here."""

        if self._confirmation_refresh_callback is not None:
            self._confirmation_refresh_callback()

    async def async_initialize(self, status: dict[str, Any] | None = None) -> None:
        """Load the fixed Eco target/hysteresis and migrate legacy temperature data.

        Earlier pre-release versions stored Eco Min/Max plus one offset.  There is no unique mapping
        from an arbitrary Min/Max corridor to one fixed target.  Migration therefore
        only reuses a value that has an unambiguous old meaning: an already-active
        stored target or equal Min/Max.  An unequal corridor deliberately falls back
        to the new 24 °C default instead of inventing a midpoint.  The old offset
        maps directly to the new independent hysteresis.
        """

        stored = await self._store.async_load() or {}
        device_min, device_max = self.device_limits(status)

        configured_target = _float_or_none(stored.get("target_temperature"))
        migration_source = "stored fixed target"
        if configured_target is None:
            context = stored.get("context")
            context_target = (
                _float_or_none(context.get("target_temperature"))
                if isinstance(context, dict)
                else None
            )
            old_min = _float_or_none(stored.get("min_temperature"))
            old_max = _float_or_none(stored.get("max_temperature"))
            if context_target is not None:
                configured_target = context_target
                migration_source = "active legacy Eco target"
            elif old_min is not None and old_max is not None:
                if abs(old_min - old_max) <= 0.01:
                    configured_target = old_min
                    migration_source = "equal legacy Min/Max"
                else:
                    configured_target = ECO_TARGET_TEMPERATURE_DEFAULT
                    migration_source = "default (legacy Min/Max corridor was not uniquely mappable)"
            elif old_min is not None:
                configured_target = old_min
                migration_source = "legacy Min"
            elif old_max is not None:
                configured_target = old_max
                migration_source = "legacy Max"
            else:
                configured_target = ECO_TARGET_TEMPERATURE_DEFAULT
                migration_source = "default"

        self.eco_target_temperature = _clamp(
            normalize_half_degree_setpoint(configured_target),
            device_min,
            device_max,
        )

        hysteresis = _float_or_none(stored.get("hysteresis"))
        if hysteresis is None:
            hysteresis = _float_or_none(stored.get("temperature_offset"))
        if hysteresis is None:
            hysteresis = ECO_HYSTERESIS_DEFAULT
        self.eco_hysteresis = _clamp(
            round(float(hysteresis) / ECO_HYSTERESIS_STEP) * ECO_HYSTERESIS_STEP,
            ECO_HYSTERESIS_MIN,
            ECO_HYSTERESIS_MAX,
        )

        if "target_temperature" not in stored or "hysteresis" not in stored:
            self.logger.info(
                "Eco temperature policy migrated: target=%.1f °C (%s), hysteresis=%.1f °C",
                self.eco_target_temperature,
                migration_source,
                self.eco_hysteresis,
            )

        pending = stored.get("pending_restore")
        self.snapshots.pending_restore = (
            self._snapshot_for_strategy(EcoRestoreSnapshot(**pending))
            if isinstance(pending, dict)
            else None
        )
        self._pending_restore_immediate_off_safe = bool(
            self.snapshots.pending_restore is not None
            and stored.get("pending_restore_immediate_off_safe", False)
        )

        # Do not resurrect an active legacy Min/Max/offset cycle into the new control
        # model.  A context written by this build is explicitly tagged and may be
        # restored normally after later HA restarts.
        context = stored.get("context")
        self._stored_context = (
            context
            if isinstance(context, dict)
            and stored.get("control_model") == "target_hysteresis_v1"
            else None
        )
        if isinstance(context, dict) and context.get("active") is True and self._stored_context is None:
            self.logger.info(
                "Old Eco active context not restored across temperature-policy migration; "
                "physical Samsung state remains authoritative until Eco is selected again"
            )
        self._restore_attempted = self._stored_context is None
        await self._async_save()

    @property
    def enabled(self) -> bool:
        return self.state in _ACTIVE_STATES

    @property
    def owns_powered_off_state(self) -> bool:
        """Return whether an Eco Sleep cycle owns the physical Off state.

        During external Sleep the AC is intentionally powered off while the
        Eco session remains active. The deferred USER_OFF/Pending Restore owner
        may therefore be created only after Eco is explicitly finished.
        """

        return self.state == EcoState.SATISFIED_OFF

    @property
    def sleep_entry_pending(self) -> bool:
        """Return whether Eco is waiting for physical Off before Sleep."""

        return self.transitions.name == "target_reached_external"

    @property
    def accepts_sleep_user_override(self) -> bool:
        """Return whether a Fan/Oscillation Sleep override can be owned by Eco."""

        return self.owns_powered_off_state or self.sleep_entry_pending

    @property
    def snapshot(self) -> EcoRestoreSnapshot | None:
        """Compatibility alias for the immutable Original Snapshot."""

        return self.snapshots.original

    @snapshot.setter
    def snapshot(self, value: EcoRestoreSnapshot | None) -> None:
        self.snapshots.original = value

    @property
    def pending_restore(self) -> EcoRestoreSnapshot | None:
        return self.snapshots.pending_restore

    def pending_desired_value(self, field: str) -> Any:
        """Return a desired field while a staged Sleep operation is active."""

        if self._pending_sleep_override is not None and field in self._pending_sleep_override:
            return self._pending_sleep_override.get(field)
        if self._pending_reconcile is None:
            return None
        return self._pending_reconcile.desired.get(field)

    @property
    def available(self) -> bool:
        return self.temperature.valid

    @property
    def external_temperature_sensor(self) -> str | None:
        value = self.entry.options.get(CONF_ECO_TEMPERATURE_SENSOR)
        return value if isinstance(value, str) and value else None

    @property
    def temperature_source_type(self) -> str:
        return "external" if self.external_temperature_sensor else "internal"

    def _create_strategy(self) -> EcoStrategy:
        if self.external_temperature_sensor:
            return EcoExternalStrategy(self)
        return EcoInternalStrategy(self)

    def _refresh_strategy(self) -> None:
        # A target-reached route is immutable for the current rest cycle.
        if self.state in {EcoState.SATISFIED_OFF, EcoState.SATISFIED_FAN_QUIET}:
            return
        expected = EcoExternalStrategy if self.external_temperature_sensor else EcoInternalStrategy
        if not isinstance(self._strategy, expected):
            self._strategy = expected(self)

    def refresh_external_temperature(self) -> None:
        """Refresh the configured external HA sensor without reading SmartThings."""

        if self.external_temperature_sensor is None:
            return
        self.temperature = self._temperature_reading(self._latest_status_for_temperature)

    async def async_evaluate_external_temperature_change(self) -> None:
        """React to a local HA temperature event without SmartThings polling."""

        if not self.enabled or self.pending_restore is not None:
            return
        if not self.temperature.valid or self.temperature.value is None:
            return
        if self._command_lock.locked() or self._pending_reconcile is not None:
            return

        # Never start the next Eco phase from a local temperature event while
        # Samsung is still confirming the current controller-owned transition.
        # The next shared coordinator status will confirm the transition and
        # immediately re-evaluate the temperature using the same straight path.
        if self.transitions.active:
            return

        if self.state == EcoState.SATISFIED_OFF:
            mode = self.snapshot.hvac_mode if self.snapshot else None
            if mode in ECO_SUPPORTED_HVAC_MODES and self._restart_is_required(mode):
                self.logger.info(
                    "Eco Sleep hysteresis reached after external temperature update: "
                    "room=%.1f °C target=%.1f °C",
                    self.temperature.value,
                    self.target_temperature,
                )
                await self._async_restart_after_temperature_drift(mode)
            return

        if self.state == EcoState.RUNNING:
            mode = self.snapshot.hvac_mode if self.snapshot else None
            if mode in ECO_SUPPORTED_HVAC_MODES and self._target_is_reached(mode):
                self.logger.info(
                    "Eco target reached after external temperature update: "
                    "room=%.1f °C target=%.1f °C",
                    self.temperature.value,
                    self.target_temperature,
                )
                await self._async_target_reached(mode)

    async def async_process_status(self, status: dict[str, Any]) -> None:
        """Process one Samsung status update from the shared coordinator."""

        self._refresh_strategy()
        self._latest_status_for_temperature = status
        self.temperature = self._temperature_reading(status)
        observed = self._controlled_status(status)

        if not self._restore_attempted:
            await self._async_restore_context(status)

        if self.pending_restore is not None:
            # Pending Restore is a deferred ownership token only. The shared
            # coordinator transfers it to the dedicated one-shot restore owner;
            # the Eco controller itself never sends a later restore sequence.
            return

        previous_observed = self._last_observed
        actual_changed_fields = {
            field
            for field in self.overrides.CONTROLLED_FIELDS
            if previous_observed is not None
            and previous_observed.get(field) != observed.get(field)
            and observed.get(field) is not None
        }
        transition_owned_change = self.transitions.owns(actual_changed_fields)
        transition_name = self.transitions.name
        transition_result = self.transitions.observe(observed)
        self._last_observed = dict(observed)

        # External Sleep becomes a real Eco state only after the shared
        # coordinator has observed physical power=off. Until then RUNNING is
        # retained and the target_reached_external transition owns the short
        # in-flight phase. This also keeps delayed activation feedback from
        # being mistaken for a user override.
        if transition_name == "target_reached_external":
            if transition_result == "confirmed":
                self._set_external_satisfied_state()
                self.logger.info(
                    "Eco Sleep confirmed: physical AC is off; Eco-owned values remain visible"
                )
                queued_user_off = self._pending_sleep_user_off
                queued_override = self._pending_sleep_override
                self._pending_sleep_user_off = False
                self._pending_sleep_override = None
                if queued_user_off:
                    self.logger.info(
                        "Eco Sleep-entry queued user Off released after power-off confirmation"
                    )
                    await self.async_finish_sleep_off()
                elif queued_override:
                    self.logger.info(
                        "Eco Sleep-entry queued user override released after power-off confirmation: %s",
                        queued_override,
                    )
                    await self.async_finish_sleep_with_override(queued_override)
                elif self._eco_configuration_reconcile_pending:
                    await self._async_apply_pending_eco_configuration_change(status)
                return
            if transition_result is None:
                return
            if transition_result == "timeout":
                if self._pending_sleep_user_off:
                    self.logger.warning(
                        "Eco Sleep-entry timed out; discarding queued user Off"
                    )
                    self._pending_sleep_user_off = False
                if self._pending_sleep_override:
                    self.logger.warning(
                        "Eco Sleep-entry timed out; discarding queued user override: %s",
                        self._pending_sleep_override,
                    )
                    self._pending_sleep_override = None
                await self._async_restore_original_after_controller_error(
                    purpose="target_reached_external",
                    detail="Sleep power-off confirmation timeout",
                    observed=observed,
                )
                return

        # Controller-owned phase boundaries are strict.  No later Eco phase may
        # overtake an incomplete activation, target update, Stand-by entry or
        # Stand-by resume.  A terminal timeout releases the AC without retry.
        if transition_name in {
            "target_update",
        }:
            if transition_result is None:
                return
            if transition_result == "timeout":
                desired = self._expected_eco_status()
                mismatches = self._state_mismatches(desired, observed)
                self.logger.warning(
                    "Eco controller transition timed out: %s mismatches=%s",
                    transition_name,
                    mismatches,
                )
                await self._async_restore_original_after_controller_error(
                    purpose=transition_name,
                    detail=f"confirmation timeout: mismatches={mismatches}",
                    observed=observed,
                )
                return

        if transition_name == "quiet_repair" and transition_result == "timeout":
            mismatches = self._state_mismatches(
                {"optional_mode": "quiet"}, observed
            )
            self.logger.warning(
                "Eco Quiet confirmation timed out: mismatches=%s", mismatches
            )
            await self._async_restore_original_after_controller_error(
                purpose="quiet_repair",
                detail=f"confirmation timeout: mismatches={mismatches}",
                observed=observed,
            )
            return

        if self._pending_reconcile is not None:
            await self._async_handle_pending_reconcile_status(
                status, observed, transition_name, transition_result
            )
            return

        if self.state == EcoState.SUSPENDED and self._suspended_desired is not None:
            await self._async_handle_suspended_status(
                observed=observed,
                actual_changed_fields=actual_changed_fields,
            )
            return

        if transition_result == "timeout":
            self._schedule_save()

        if (
            self._eco_configuration_reconcile_pending
            and not self.transitions.active
            and self._pending_reconcile is None
            and not self._command_lock.locked()
        ):
            if await self._async_apply_pending_eco_configuration_change(status):
                return

        if not self.enabled or self._command_lock.locked():
            return

        if (
            observed["power"] == "off"
            and self.snapshot is not None
            and self.state != EcoState.SATISFIED_OFF
        ):
            await self.finish_eco(
                EcoFinishReason.USER_OFF,
                detail="air conditioner switched off during Eco",
                observed=observed,
            )
            return

        expected = self._expected_eco_status()
        samsung_feedback_fields: set[str] = set()
        if self.state == EcoState.SATISFIED_FAN_QUIET:
            samsung_feedback_fields.add("optional_mode")

        changed = self.overrides.changed_fields(observed, expected)
        if transition_result != "confirmed":
            changed.update(
                field
                for field in actual_changed_fields
                if field in expected and observed.get(field) == expected.get(field)
            )
        change = self.overrides.classify(
            observed=observed,
            expected=expected,
            transition_owns=(
                transition_owned_change and transition_result != "timeout"
            ),
            samsung_feedback_fields=samsung_feedback_fields,
            changed_fields=changed,
        )

        if change.owner == EcoChangeOwner.USER:
            reason = (
                EcoFinishReason.MODE_CHANGED
                if "hvac_mode" in change.fields
                else EcoFinishReason.USER_OVERRIDE
            )
            if "hvac_mode" in change.fields:
                preserve = {"hvac_mode"}
            elif "target_temperature" in change.fields:
                preserve = {"target_temperature"}
            else:
                preserve = change.fields & {"fan_mode", "oscillation_mode"}
            await self.finish_eco(
                reason,
                detail=f"manual controlled-setting change: {sorted(change.fields)}",
                preserve_fields=preserve,
                observed=observed,
            )
            return

        if self.state in {EcoState.SATISFIED_OFF, EcoState.SATISFIED_FAN_QUIET}:
            try:
                await self._strategy_for_satisfied_state().async_maintain(status)
            except Exception as err:
                self.logger.exception("Failed to maintain Eco rest-state profile")
                await self._async_restore_original_after_controller_error(
                    purpose="rest_state_maintain",
                    detail=f"command error: {err}",
                    observed=observed,
                )
                return
            if not self.temperature.valid:
                return
            mode = self.snapshot.hvac_mode if self.snapshot else None
            if mode in ECO_SUPPORTED_HVAC_MODES and self._restart_is_required(mode):
                await self._async_restart_after_temperature_drift(mode)
            return

        if not self.temperature.valid:
            self.state = EcoState.SUSPENDED
            self._schedule_save()
            return

        mode = observed["hvac_mode"]
        if observed["power"] == "on" and mode in ECO_SUPPORTED_HVAC_MODES:
            if self.snapshot is None or self.target_temperature is None:
                await self._async_activate(status, mode)
            elif self.state == EcoState.SUSPENDED:
                self.state = EcoState.RUNNING
                self._schedule_save()
            if self.state == EcoState.RUNNING and self._target_is_reached(mode):
                await self._async_target_reached(mode)
            return

        if observed["power"] == "on" and mode not in ECO_SUPPORTED_HVAC_MODES:
            await self.finish_eco(
                EcoFinishReason.MODE_CHANGED,
                detail=f"Eco-incompatible HVAC mode selected: {mode!r}",
                restore=self.snapshot is not None,
                preserve_fields={"hvac_mode"},
                observed=observed,
            )
            return

        self.state = EcoState.ARMED
        self._schedule_save()

    async def async_arm_while_power_off_pending(
        self,
        *,
        snapshot_seed: EcoRestoreSnapshot | None = None,
        snapshot_overrides: dict[str, Any] | None = None,
    ) -> bool:
        """Arm Eco while a normal Mode-Off command awaits physical confirmation.

        Coordinator truth remains authoritative; this method does not fabricate an
        Off status or capture an Original Snapshot.  It only records the user's Eco
        intent so activation can occur later from a fresh confirmed status.
        """

        if not self.enabled:
            self.snapshot = None
            self.target_temperature = None
            self._thermal_direction = None
            self._satisfied_at = None
            self._target_reached_route = None
            self._suspended_desired = None
            self._suspended_baseline = None
            self.transitions.cancel(reason="new Eco session")
            self._strategy = self._create_strategy()
        if snapshot_seed is not None:
            self.snapshot = self._overlay_snapshot(snapshot_seed, snapshot_overrides)
        self.state = EcoState.ARMED
        await self._async_save()
        self.logger.info(
            "Eco armed while Power Off confirmation is still pending"
        )
        return True

    async def async_arm_for_serial_handoff(self) -> bool:
        """Expose a new Eco intent while another owner reaches a safe boundary.

        No Samsung command is sent here.  The shared coordinator/controller will
        start the new Eco cycle only after the previous restore owner has stopped.
        """

        if not self.enabled:
            self.snapshot = None
            self.target_temperature = None
            self._thermal_direction = None
            self._satisfied_at = None
            self._target_reached_route = None
            self._suspended_desired = None
            self._suspended_baseline = None
            self.transitions.cancel(reason="Eco serial handoff armed")
            self._strategy = self._create_strategy()
        self.state = EcoState.ARMED
        await self._async_save()
        self.logger.info("Eco armed pending serial ownership handoff")
        return True

    async def async_enable(
        self,
        status: dict[str, Any],
        *,
        snapshot_overrides: dict[str, Any] | None = None,
        snapshot_seed: EcoRestoreSnapshot | None = None,
    ) -> bool:
        """Enable Eco and return whether the preset was accepted."""

        pending_finish = self._pending_reconcile
        if pending_finish is not None and pending_finish.finish_eco:
            # A user may logically re-select Eco while the previous Eco -> None
            # restore is still settling.  Never start a second command owner.
            # Preserve the old restore destination as the next Original Snapshot
            # and wait for any already-sent field to be physically confirmed.
            self._queued_reenable_after_finish = True
            self._queued_reenable_snapshot = self._snapshot_from_desired_state(
                pending_finish.desired
            )
            self._queued_reenable_overrides = dict(snapshot_overrides or {})
            self.logger.info(
                "Eco re-enable queued behind active %s restore; no overlapping commands will be sent",
                pending_finish.purpose,
            )
            return True

        if not self.enabled:
            # Every explicit Eco selection starts a completely fresh session.
            self.snapshot = None
            self.target_temperature = None
            self._thermal_direction = None
            self._satisfied_at = None
            self._target_reached_route = None
            self._suspended_desired = None
            self._suspended_baseline = None
            self.transitions.cancel(reason="new Eco session")
            self._strategy = self._create_strategy()
        else:
            self._refresh_strategy()
        self._latest_status_for_temperature = status
        self.temperature = self._temperature_reading(status)
        if not self.temperature.valid:
            self.snapshot = None
            self.target_temperature = None
            self._thermal_direction = None
            self.state = (
                EcoState.TEMPERATURE_STALE
                if self.temperature.reason == "stale"
                else EcoState.TEMPERATURE_UNAVAILABLE
            )
            await self._async_save()
            self.logger.warning(
                "Eco could not start: temperature source %s (%s)",
                self.temperature_source_type,
                self.temperature.reason or "unknown reason",
            )
            return False

        power = _attribute_value(status, "switch", "switch")
        physical_mode = _attribute_value(
            status, "airConditionerMode", "airConditionerMode"
        )
        pending_mode = (snapshot_overrides or {}).get("hvac_mode")
        mode = pending_mode if isinstance(pending_mode, str) else physical_mode
        if power == "on" and mode in ECO_SUPPORTED_HVAC_MODES:
            return await self._async_activate(
                status,
                mode,
                snapshot_overrides=snapshot_overrides,
                snapshot_seed=snapshot_seed,
            )

        if power == "off":
            self.snapshot = snapshot_seed
            self.target_temperature = None
            self._thermal_direction = None
            self.state = EcoState.ARMED
            await self._async_save()
            self.logger.info(
                "Eco armed using %s temperature sensor", self.temperature_source_type
            )
            return True

        # Auto, Dry and Wind remain available as normal Samsung HVAC modes, but
        # Eco cannot be armed while the AC is already running in one of them.
        self._reset()
        await self._async_save()
        self.logger.info("Eco selection rejected in HVAC mode %r", mode)
        return False

    async def async_disable(self, *, restore: bool) -> None:
        # Publish the newer user intent before waiting for the controller lock.
        # A target-reached handler already inside that lock checks this flag
        # immediately before sending its first route command.
        self._preset_off_requested = True
        try:
            await self.finish_eco(
                EcoFinishReason.PRESET_OFF,
                detail="Eco preset disabled by user",
                restore=restore,
            )
        finally:
            self._preset_off_requested = False

    async def async_prepare_user_off(self) -> bool:
        """Let the newest explicit Mode Off preempt older Eco ownership.

        During an Eco-exit restore the user has already left Eco, so its active
        transport is cancelled and may never turn the AC back on; the restore
        destination itself becomes Pending Restore for a later deliberate Power On.
        During an active Eco transition/reconcile, only that in-flight owner is
        cancelled; the Original Snapshot remains until physical Off is observed,
        after which the existing USER_OFF/Pending Restore route takes over.
        """

        async with self._command_lock:
            pending = self._pending_reconcile
            if pending is not None and pending.finish_eco:
                # The user's newest physical intent is Off, but the restore
                # destination itself is still valuable: fields not yet restored
                # must remain the desired settings for the next Power On.  Turn
                # the complete staged destination into Pending Restore, cancel
                # only its active transport owner, and never send a later Power On.
                restore_destination = self._snapshot_from_desired_state(
                    pending.desired
                )
                self.logger.info(
                    "Explicit Mode Off preempted active Eco exit restore: %s; "
                    "remaining restore destination retained as Pending Restore",
                    pending.purpose,
                )
                self.transitions.cancel(reason="user_off_preempts_finish_restore")
                self._pending_reconcile = None
                self._queued_reenable_after_finish = False
                self._queued_reenable_snapshot = None
                self._queued_reenable_overrides = {}
                self.snapshots.pending_restore = restore_destination
                self._pending_restore_immediate_off_safe = False
                self._reset(keep_pending=True)
                await self._async_save()
                return True

            if not self.enabled:
                return False

            if self.snapshot is None:
                # ARMED Eco while the AC is already Off has no pre-Eco active
                # snapshot to preserve.  Explicit Off simply cancels Eco.
                self.logger.info("Explicit Mode Off cancelled armed Eco")
                self._reset()
                await self._async_save()
                return True

            if self.state == EcoState.SATISFIED_OFF:
                # The dedicated confirmed-Sleep route is handled before the
                # coordinator calls this method.
                return False

            if self.transitions.active or self._pending_reconcile is not None:
                self.logger.info(
                    "Explicit Mode Off preempted Eco transition/reconcile; "
                    "awaiting physical Off before creating Pending Restore"
                )
            self.transitions.cancel(reason="user_off_preempts_active_eco")
            self._pending_reconcile = None
            self._queued_reenable_after_finish = False
            self._queued_reenable_snapshot = None
            self._queued_reenable_overrides = {}
            self._pending_sleep_override = None
            self._pending_sleep_user_off = False
            self._suspended_desired = None
            self._suspended_baseline = None
            self._eco_configuration_reconcile_pending = False
            await self._async_save()
            return True

    async def finish_eco(
        self,
        reason: EcoFinishReason,
        *,
        detail: str,
        restore: bool = True,
        preserve_fields: set[str] | None = None,
        observed: dict[str, Any] | None = None,
        override_values: dict[str, Any] | None = None,
    ) -> None:
        """Leave Eco through the only supported finish path."""

        preserve_fields = set(preserve_fields or set())
        async with self._command_lock:
            snapshot = self.snapshot
            previous_state = self.state
            self.transitions.cancel(reason=f"finish:{reason.value}")

            # Every normal Eco exit restore uses one staged difference-based
            # owner.  If physical Power is Off it first crosses a confirmed Power
            # On boundary; if Power is already On it starts directly from fresh
            # coordinator truth.  This prevents redundant Mode commands (observed
            # HTTP 422) and removes the old blind multi-command restore path.
            if (
                reason != EcoFinishReason.USER_OFF
                and restore
                and snapshot is not None
            ):
                desired = self._desired_finish_restore_from_snapshot(
                    snapshot,
                    preserve_fields=preserve_fields,
                    observed=observed,
                    override_values=override_values,
                )
                try:
                    await self._async_start_pending_reconcile(
                        purpose="finish_restore",
                        desired=desired,
                        detail=detail,
                        finish_eco=True,
                        finish_reason=reason,
                        preserve_fields=preserve_fields,
                    )
                except Exception:
                    # The staged owner must not survive a failed Power-On send.
                    # Release every transition marker immediately so no stale
                    # watchdog can emit a later phantom restore timeout.
                    self.transitions.cancel(reason="finish_restore:start_error")
                    self.logger.exception(
                        "Eco finish restore failed: reason=%s detail=%s",
                        reason.value,
                        detail,
                    )
                    self.logger.info(
                        "Eco finished: reason=%s detail=%s restore=%s preserve=%s pending_restore=False",
                        EcoFinishReason.ERROR.value,
                        detail,
                        True,
                        sorted(preserve_fields),
                    )
                    raise
                self.logger.info(
                    "Eco exit restore staged: reason=%s state=%s; applying only confirmed differences",
                    reason.value,
                    previous_state.value,
                )
                return

            try:
                if reason == EcoFinishReason.USER_OFF and snapshot is not None:
                    pending = self.snapshots.create_pending_restore()
                    self.state = EcoState.PENDING_RESTORE
                    self.logger.info(
                        "Eco Pending Restore created from Original Snapshot: %s",
                        asdict(pending) if pending else None,
                    )
                elif previous_state == EcoState.SATISFIED_FAN_QUIET:
                    await self._strategy.async_cancel_cleanup()
            except Exception:
                self.transitions.cancel(reason="finish_cleanup:command_error")
                self.logger.exception(
                    "Eco finish cleanup failed: reason=%s detail=%s", reason.value, detail
                )
                reason = EcoFinishReason.ERROR
                raise
            finally:
                pending_active = self.pending_restore is not None
                self.logger.info(
                    "Eco finished: reason=%s detail=%s restore=%s preserve=%s pending_restore=%s",
                    reason.value,
                    detail,
                    restore and snapshot is not None and not pending_active,
                    sorted(preserve_fields),
                    pending_active,
                )
                self._reset(keep_pending=pending_active)


    async def async_finish_with_target_override(self, value: float) -> bool:
        """Finish an active non-Sleep Eco cycle with a target overlay.

        The user's selected temperature is an explicit exit instruction, not
        an Eco-limit change.  Restore the immutable Original Snapshot, keep the
        new target as the final overlay, and make that target part of restore
        confirmation.  Sleep/Sleep-entry use the existing staged power-on
        reconcile path instead.
        """

        if self.snapshot is None or not self.enabled or self.accepts_sleep_user_override:
            return False

        observed = self._controlled_status(self._latest_status_for_temperature)
        await self.finish_eco(
            EcoFinishReason.USER_OVERRIDE,
            detail="manual target-temperature change from Samsung AC Advanced",
            preserve_fields={"target_temperature"},
            observed=observed,
            override_values={"target_temperature": float(value)},
        )
        return True


    async def async_take_pending_restore(
        self,
    ) -> tuple[EcoRestoreSnapshot | None, bool]:
        """Transfer Pending Restore and its narrow Sleep-Off restore intent."""

        snapshot = self.pending_restore
        if snapshot is None:
            return None, False
        immediate_off_safe = self._pending_restore_immediate_off_safe
        self.snapshots.clear_pending()
        self._pending_restore_immediate_off_safe = False
        self.state = EcoState.DISABLED
        await self._async_save()
        self.logger.info("Eco Pending Restore transferred to deferred restore owner")
        return snapshot, immediate_off_safe

    async def async_set_eco_target_temperature(
        self, value: float, *, status: dict[str, Any] | None = None
    ) -> None:
        """Set the fixed logical Eco target in 0.5 °C steps.

        Samsung transport and confirmation use the normalized physical whole-degree
        setpoint, while Eco threshold/hysteresis decisions retain this logical value.
        During an active transition only the configuration intent is updated.
        The already-sent Samsung transport step is allowed to reach one confirmed
        coordinator boundary before the newest target is reconciled.
        """

        device_min, device_max = self.device_limits(status)
        new_target = _clamp(
            normalize_half_degree_setpoint(float(value)), device_min, device_max
        )
        if self._controlled_values_match(
            "target_temperature", self.eco_target_temperature, new_target
        ):
            return

        previous = self.eco_target_temperature
        self.eco_target_temperature = new_target
        self.logger.info(
            "Eco target setting changed: %.1f -> %.1f °C", previous, new_target
        )

        if not self.enabled or self.state == EcoState.ARMED:
            self._eco_configuration_reconcile_pending = False
            await self._async_save()
            return

        pending = self._pending_reconcile
        if pending is not None and pending.finish_eco:
            # Eco is already being left; this setting belongs to the next cycle.
            self._eco_configuration_reconcile_pending = False
            await self._async_save()
            return

        self._eco_configuration_reconcile_pending = True
        await self._async_save()
        if self.transitions.active or pending is not None or self._command_lock.locked():
            self.logger.info(
                "Eco target update queued behind current controller-owned transition"
            )
            return
        await self._async_apply_pending_eco_configuration_change(
            status or self._latest_status_for_temperature
        )

    async def async_set_eco_hysteresis(self, value: float) -> None:
        """Set the independent restart hysteresis, including during Eco."""

        new_hysteresis = _clamp(
            round(float(value) / ECO_HYSTERESIS_STEP) * ECO_HYSTERESIS_STEP,
            ECO_HYSTERESIS_MIN,
            ECO_HYSTERESIS_MAX,
        )
        if abs(new_hysteresis - self.eco_hysteresis) <= 0.01:
            return
        previous = self.eco_hysteresis
        self.eco_hysteresis = new_hysteresis
        self.logger.info(
            "Eco restart margin changed: %.1f -> %.1f °C", previous, new_hysteresis
        )

        if not self.enabled or self.state == EcoState.ARMED:
            await self._async_save()
            return

        pending = self._pending_reconcile
        if self.state == EcoState.RUNNING:
            # Hysteresis does not alter an active Running target.  If Running is
            # only the logical state while a rest-entry transition is still in
            # flight, re-evaluate once that confirmed boundary is reached.
            if self.transitions.active or pending is not None:
                self._eco_configuration_reconcile_pending = True
            await self._async_save()
            return

        if pending is not None and pending.finish_eco:
            await self._async_save()
            return

        self._eco_configuration_reconcile_pending = True
        await self._async_save()
        if self.transitions.active or pending is not None or self._command_lock.locked():
            return
        await self._async_apply_pending_eco_configuration_change(
            self._latest_status_for_temperature
        )

    async def _async_apply_pending_eco_configuration_change(
        self, status: dict[str, Any]
    ) -> bool:
        """Apply the newest target/hysteresis at one safe controller boundary.

        Return True when a Samsung transition/restart was started and the caller
        must stop processing the current (now stale) status.
        """

        if not self._eco_configuration_reconcile_pending:
            return False
        if (
            not self.enabled
            or self.state == EcoState.ARMED
            or self.transitions.active
            or self._pending_reconcile is not None
            or self._command_lock.locked()
        ):
            return False

        mode = self.snapshot.hvac_mode if self.snapshot else None
        if mode not in ECO_SUPPORTED_HVAC_MODES:
            self._eco_configuration_reconcile_pending = False
            await self._async_save()
            return False

        target_changed = (
            self.target_temperature is None
            or not self._controlled_values_match(
                "target_temperature",
                self.target_temperature,
                self.eco_target_temperature,
            )
        )
        if target_changed:
            previous_target = self.target_temperature
            self.target_temperature = self.eco_target_temperature
            self.logger.info(
                "Active Eco target updated at safe boundary: %s -> %.1f °C",
                previous_target,
                self.target_temperature,
            )

        self._eco_configuration_reconcile_pending = False
        await self._async_save()

        if self.state == EcoState.RUNNING:
            if target_changed and self._target_is_reached(mode):
                self.logger.info(
                    "Updated Eco target is already satisfied (room: %.1f °C, target: %.1f °C); "
                    "entering Eco rest state without a redundant setpoint command",
                    self.temperature.value,
                    self.target_temperature,
                )
                await self._async_target_reached(mode)
                return True

            if target_changed:
                observed = self._controlled_status(status)
                physical_target = normalize_physical_setpoint(self.target_temperature)
                if not self._controlled_values_match(
                    "target_temperature",
                    observed.get("target_temperature"),
                    physical_target,
                ):
                    self.transitions.begin(
                        "target_update",
                        {"target_temperature": physical_target},
                        timeout=120.0,
                    )
                    try:
                        await self.api.set_target_temperature(mode, physical_target)
                    except Exception as err:
                        self.transitions.cancel(reason="target_update:command_error")
                        self.logger.exception("Eco target update command failed")
                        await self._async_restore_original_after_controller_error(
                            purpose="target_update",
                            detail=f"command error: {err}",
                            observed=observed,
                        )
                        return True
                    self.logger.info(
                        "Eco target update sent: %.1f °C; awaiting coordinator confirmation",
                        physical_target,
                    )
                    self._request_confirmation_refresh()
                    return True
            return False

        if self.state in {EcoState.SATISFIED_OFF, EcoState.SATISFIED_FAN_QUIET}:
            if not self.temperature.valid or self.temperature.value is None:
                return False
            restart_required = self._restart_is_required(mode)
            self.logger.info(
                "Eco target/hysteresis changed during %s: %s "
                "(room: %.1f °C, target: %.1f °C, hysteresis: %.1f °C)",
                "Sleep" if self.state == EcoState.SATISFIED_OFF else "Standby",
                "resume required" if restart_required else "remain at rest",
                self.temperature.value,
                self.target_temperature,
                self.eco_hysteresis,
            )
            if restart_required and not self._configuration_resume_in_progress:
                self._configuration_resume_in_progress = True
                try:
                    await self._async_restart_after_temperature_drift(mode)
                finally:
                    self._configuration_resume_in_progress = False
                return True
            return False

        if self.state == EcoState.SUSPENDED and self._suspended_desired is not None:
            self._suspended_desired["target_temperature"] = (
                normalize_physical_setpoint(self.eco_target_temperature)
            )
            await self._async_save()
        return False

    def device_limits(self, status: dict[str, Any] | None) -> tuple[float, float]:
        minimum = _float_or_none(
            _attribute_value(status, "custom.thermostatSetpointControl", "minimumSetpoint")
        )
        maximum = _float_or_none(
            _attribute_value(status, "custom.thermostatSetpointControl", "maximumSetpoint")
        )
        return minimum if minimum is not None else 16.0, maximum if maximum is not None else 30.0

    async def _async_enter_external_sleep(self, mode: str) -> None:
        """Enter Sleep while keeping the whole Eco session silent."""

        status = self._latest_status_for_temperature
        volume = _attribute_value(status, "audioVolume", "volume")
        auto_clean = _string_value(
            _attribute_value(status, "custom.autoCleaningMode", "autoCleaningMode")
        )
        pre_sleep = self.snapshots.capture_pre_sleep(
            EcoPreSleepSnapshot(
                auto_clean_enabled=(
                    auto_clean == "on" if auto_clean in {"on", "off"} else None
                ),
            )
        )
        self.logger.info(
            "Eco Pre-Sleep captured: Auto Clean=%s",
            "on" if pre_sleep.auto_clean_enabled else "off" if pre_sleep.auto_clean_enabled is False else "unknown",
        )

        self._pending_sleep_override = None
        self.transitions.begin("target_reached_external", {"power": "off"}, timeout=120.0)
        # Sound normally is already muted from Eco activation.  If Samsung or an
        # external actor changed it, re-establish silence before any Sleep command.
        if isinstance(volume, (int, float)) and volume != 0:
            await self.api.set_volume(0)
        if auto_clean == "on":
            await self.api.set_auto_cleaning_mode("off")
        await self.api.turn_off()
        self.logger.info(
            "Eco target reached in %s mode (room: %.1f °C, target: %.1f °C); "
            "power-off command sent, awaiting coordinator confirmation",
            mode,
            self.temperature.value,
            self.target_temperature,
        )

    async def _async_start_external_resume(self, mode: str) -> None:
        """Start automatic Sleep resume; coordinator status drives stage two."""

        if self._pending_reconcile is not None or self.target_temperature is None:
            return
        pre_sleep = self.snapshots.pre_sleep
        if pre_sleep is None:
            pre_sleep = EcoPreSleepSnapshot(
                auto_clean_enabled=(
                    self.snapshot.auto_clean_enabled if self.snapshot else None
                ),
            )
        desired = self._active_eco_desired(mode)
        desired["auto_clean_mode"] = (
            "on"
            if pre_sleep.auto_clean_enabled is True
            else "off"
            if pre_sleep.auto_clean_enabled is False
            else None
        )
        await self._async_start_pending_reconcile(
            purpose="resume_external",
            desired=desired,
            detail="automatic Eco Sleep hysteresis resume",
            finish_eco=False,
        )

    async def async_finish_sleep_off(self) -> bool:
        """Finish Eco for a deliberate Mode Off during external Sleep.

        During confirmed Sleep the physical AC is already off, so no second
        Samsung power-off command is needed. Eco creates Pending Restore from
        the immutable Original Snapshot and hands it to the dedicated deferred
        Eco restore path. During Sleep-entry the intent is queued until the
        already-sent power-off sequence is coordinator-confirmed.
        """

        if self.snapshot is None:
            return False

        if self.sleep_entry_pending:
            self._pending_sleep_user_off = True
            self._pending_sleep_override = None
            self.logger.info(
                "Eco Sleep-entry user Off queued until power-off confirmation"
            )
            return True

        if self.state != EcoState.SATISFIED_OFF:
            return False

        self.logger.info(
            "Eco Sleep user Off: keeping physical AC off and creating Pending Restore"
        )
        # Only this explicit confirmed-Sleep -> Mode Off route restores the
        # off-safe auxiliary settings immediately. Other USER_OFF origins keep
        # the existing deferred Pending Restore behaviour unchanged.
        self._pending_restore_immediate_off_safe = True
        await self.finish_eco(
            EcoFinishReason.USER_OFF,
            detail="manual Mode Off during Eco Sleep",
            restore=False,
        )
        return True

    async def async_finish_sleep_with_override(
        self, overrides: dict[str, Any]
    ) -> bool:
        """Own a Sleep user override without racing the physical Off transition.

        During confirmed Sleep the existing restore starts immediately. During
        Sleep-entry, while ``target_reached_external`` is still waiting for the
        coordinator to confirm physical power=off, the explicit user intent is
        only queued. The already-sent power-off sequence is allowed to finish;
        confirmation then releases the same Original Snapshot + override path.
        """

        if self.snapshot is None:
            return False

        if self.sleep_entry_pending:
            if self._pending_sleep_override is None:
                self._pending_sleep_override = {}
            self._pending_sleep_override.update(overrides)
            self.logger.info(
                "Eco Sleep-entry user override queued until power-off confirmation: %s",
                self._pending_sleep_override,
            )
            return True

        if self.state != EcoState.SATISFIED_OFF:
            return False

        desired = self._desired_from_original_snapshot(self.snapshot)
        normalized_overrides = dict(overrides)
        target_override = normalized_overrides.get("target_temperature")
        if isinstance(target_override, (int, float)):
            normalized_overrides["target_temperature"] = (
                normalize_physical_setpoint(float(target_override))
            )
        desired.update(normalized_overrides)
        self.logger.info(
            "Eco Sleep user override: desired Original Snapshot + %s",
            normalized_overrides,
        )
        await self._async_start_pending_reconcile(
            purpose="finish_sleep_restore",
            desired=desired,
            detail=f"manual Sleep override: {overrides}",
            finish_eco=True,
        )
        return True

    async def _async_start_pending_reconcile(
        self,
        *,
        purpose: str,
        desired: dict[str, Any],
        detail: str,
        finish_eco: bool,
        finish_reason: EcoFinishReason | None = None,
        preserve_fields: set[str] | None = None,
    ) -> None:
        if self._preset_off_requested and not finish_eco:
            self.logger.info(
                "Eco %s sequence cancelled before command send by newer preset-off intent",
                purpose,
            )
            return

        self.transitions.cancel(reason=f"start:{purpose}")
        self._pending_reconcile = EcoPendingReconcile(
            purpose=purpose,
            desired=desired,
            detail=detail,
            finish_eco=finish_eco,
            finish_reason=finish_reason,
            preserve_fields=tuple(sorted(preserve_fields or set())),
        )

        status = self._latest_status_for_temperature
        if self._controlled_status(status).get("power") == "on":
            # Already physically On: do not assert Power or Mode blindly. Start
            # directly from the fresh coordinator differences.  The continuation
            # owns the one transition begin so diagnostics are not duplicated.
            await self._async_continue_pending_reconcile(status)
            self._schedule_save()
            return

        self.transitions.begin(f"{purpose}_power_on", {"power": "on"}, timeout=120.0)
        try:
            await self.api.turn_on()
        except Exception:
            # Starting stage one failed before coordinator confirmation could
            # ever happen.  Do not leave a live transition/pending owner behind.
            self.transitions.cancel(reason=f"{purpose}:power_on_command_error")
            self._pending_reconcile = None
            if finish_eco:
                self._reset()
            raise
        if finish_eco:
            # Fast path only: schedule exactly one fresh coordinator read after
            # the outbound command.  If Samsung has not published Power On yet,
            # no retry or next command is sent; normal polling/timeout remains
            # the safe fallback.
            self.logger.debug(
                "Eco %s Power On sent; requesting fast coordinator confirmation",
                purpose,
            )
            self._request_confirmation_refresh()
        self._schedule_save()

    async def _async_handle_pending_reconcile_status(
        self,
        status: dict[str, Any],
        observed: dict[str, Any],
        transition_name: str | None,
        transition_result: str | None,
    ) -> None:
        pending = self._pending_reconcile
        if pending is None:
            return

        if transition_name == f"{pending.purpose}_power_on":
            if transition_result == "confirmed":
                self._sync_pending_controller_target(pending)
                async with self._command_lock:
                    await self._async_continue_pending_reconcile(status)
            elif transition_result == "timeout":
                await self._async_fail_pending_reconcile("power-on confirmation timeout")
            return

        expected_step_name = (
            f"{pending.purpose}:{pending.in_flight_field}"
            if pending.in_flight_field is not None
            else None
        )
        if transition_name == expected_step_name:
            if transition_result == "confirmed":
                field = pending.in_flight_field
                if field is not None:
                    self.logger.info(
                        "Eco %s step confirmed: %s", pending.purpose, field
                    )
                pending.in_flight_field = None
                self._sync_pending_controller_target(pending)
                async with self._command_lock:
                    await self._async_continue_pending_reconcile(status)
            elif transition_result == "timeout":
                mismatches = self._state_mismatches(
                    {
                        pending.in_flight_field: pending.desired.get(
                            pending.in_flight_field
                        )
                    }
                    if pending.in_flight_field is not None
                    else pending.desired,
                    observed,
                )
                self.logger.warning(
                    "Eco %s confirmation timed out: mismatches=%s",
                    pending.purpose,
                    mismatches,
                )
                await self._async_fail_pending_reconcile(
                    "serial-step confirmation timeout"
                )
            return

    def _sync_pending_controller_target(self, pending: EcoPendingReconcile) -> bool:
        """Align an in-flight Eco activation/resume with the newest target."""

        if pending.finish_eco or pending.purpose not in {
            "activate",
            "resume_external",
            "resume_internal",
        }:
            return False

        latest_logical_target = self.eco_target_temperature
        latest_physical_target = normalize_physical_setpoint(latest_logical_target)
        self.target_temperature = latest_logical_target
        previous_target = pending.desired.get("target_temperature")
        self._eco_configuration_reconcile_pending = False
        if self._controlled_values_match(
            "target_temperature", previous_target, latest_physical_target
        ):
            return False

        pending.desired["target_temperature"] = latest_physical_target
        self.logger.info(
            "Eco %s target updated while controller sequence is in flight: "
            "%s -> %.1f °C physical for %.1f °C logical",
            pending.purpose,
            previous_target,
            latest_physical_target,
            latest_logical_target,
        )
        self._schedule_save()
        return True

    async def _async_continue_pending_reconcile(self, status: dict[str, Any]) -> None:
        pending = self._pending_reconcile
        if pending is None:
            return

        if self._preset_off_requested and not pending.finish_eco:
            self.logger.info(
                "Eco %s sequence stopped at confirmed boundary by newer preset-off intent",
                pending.purpose,
            )
            self.transitions.cancel(reason="preset_off_preempts_unsent_controller_step")
            self._pending_reconcile = None
            return

        observed = self._controlled_status(status)

        # Every pending controller sequence now follows one transport rule:
        # one still-required Samsung command, coordinator confirmation, then the
        # next difference.  This prevents late pre-Eco/user feedback and Samsung
        # mode side effects from overtaking a controller-owned sequence.
        if pending.in_flight_field is not None:
            return

        if pending.finish_eco and self._queued_reenable_after_finish:
            await self._async_handoff_finish_restore_to_new_eco(status, pending)
            return

        self._sync_pending_controller_target(pending)
        mismatches = self._state_mismatches(pending.desired, observed)
        if not mismatches:
            await self._async_complete_pending_reconcile()
            return

        try:
            field = await self._async_apply_one_desired_difference(
                status,
                pending.desired,
                prefer_sound_first=not pending.finish_eco,
            )
        except Exception:
            self.transitions.cancel(reason=f"{pending.purpose}:command_error")
            self.logger.exception("Eco %s serial step failed", pending.purpose)
            await self._async_fail_pending_reconcile("command error")
            return

        if field is None:
            await self._async_complete_pending_reconcile()
            return

        pending.in_flight_field = field
        self.transitions.begin(
            f"{pending.purpose}:{field}",
            {field: pending.desired.get(field)},
            timeout=120.0,
        )
        self.logger.info(
            "Eco %s step sent: %s", pending.purpose, field
        )
        self._request_confirmation_refresh()

    async def _async_handoff_finish_restore_to_new_eco(
        self,
        status: dict[str, Any],
        pending: EcoPendingReconcile,
        *,
        pending_already_cleared: bool = False,
    ) -> None:
        """Serially hand a settling Eco-exit restore to a fresh Eco request.

        Any already-sent restore field must be confirmed before this method is
        reached.  Remaining old restore differences are deliberately not sent.
        The old restore destination becomes the immutable Original Snapshot for
        the newly requested Eco cycle.
        """

        seed = self._queued_reenable_snapshot or self._snapshot_from_desired_state(
            pending.desired
        )
        overrides = dict(self._queued_reenable_overrides)
        self._queued_reenable_after_finish = False
        self._queued_reenable_snapshot = None
        self._queued_reenable_overrides = {}
        if not pending_already_cleared:
            self._pending_reconcile = None
        self.transitions.cancel(reason="serial Eco re-enable handoff")
        self.logger.info(
            "Eco exit restore handed off to queued Eco re-enable after confirmed transport boundary"
        )
        self._reset()
        # Schedule after the current command/status critical section releases.
        self.hass.async_create_task(
            self.async_enable(
                status,
                snapshot_overrides=overrides,
                snapshot_seed=self._overlay_snapshot(seed, overrides),
            )
        )

    def _snapshot_from_desired_state(
        self, desired: dict[str, Any]
    ) -> EcoRestoreSnapshot:
        """Convert one complete desired state into a future Original Snapshot."""

        sound_volume = desired.get("sound_volume")
        auto_clean_mode = desired.get("auto_clean_mode")
        return EcoRestoreSnapshot(
            hvac_mode=desired.get("hvac_mode"),
            target_temperature=desired.get("target_temperature"),
            fan_mode=desired.get("fan_mode"),
            oscillation_mode=desired.get("oscillation_mode"),
            optional_mode=desired.get("optional_mode"),
            sound_enabled=(
                bool(sound_volume > 0)
                if isinstance(sound_volume, (int, float))
                else None
            ),
            auto_clean_enabled=(
                auto_clean_mode == "on"
                if auto_clean_mode in {"on", "off"}
                else None
            ),
        )

    def _overlay_snapshot(
        self, snapshot: EcoRestoreSnapshot, overrides: dict[str, Any] | None
    ) -> EcoRestoreSnapshot:
        """Overlay explicit recent user choices on a snapshot seed."""

        overrides = overrides or {}
        return EcoRestoreSnapshot(
            hvac_mode=overrides.get("hvac_mode", snapshot.hvac_mode),
            target_temperature=(
                float(overrides["target_temperature"])
                if isinstance(overrides.get("target_temperature"), (int, float))
                else snapshot.target_temperature
            ),
            fan_mode=overrides.get("fan_mode", snapshot.fan_mode),
            oscillation_mode=overrides.get("oscillation_mode", snapshot.oscillation_mode),
            optional_mode=overrides.get("optional_mode", snapshot.optional_mode),
            sound_enabled=(
                bool(overrides["sound_enabled"])
                if isinstance(overrides.get("sound_enabled"), bool)
                else snapshot.sound_enabled
            ),
            auto_clean_enabled=(
                bool(overrides["auto_clean_enabled"])
                if isinstance(overrides.get("auto_clean_enabled"), bool)
                else snapshot.auto_clean_enabled
            ),
        )

    async def _async_complete_pending_reconcile(self) -> None:
        pending = self._pending_reconcile
        if pending is None:
            return
        self._pending_reconcile = None
        if pending.finish_eco and self._queued_reenable_after_finish:
            await self._async_handoff_finish_restore_to_new_eco(
                self._latest_status_for_temperature, pending, pending_already_cleared=True
            )
            return
        if pending.finish_eco:
            if pending.finish_reason is not None:
                self.logger.info(
                    "Eco finished: reason=%s detail=%s restore=True preserve=%s pending_restore=False",
                    pending.finish_reason.value,
                    pending.detail,
                    list(pending.preserve_fields),
                )
            else:
                self.logger.info("Eco finished after Sleep override: %s", pending.detail)
            self._reset()
            return

        if pending.purpose == "activate":
            self._set_running_state()
            self.logger.info(
                "Eco activation confirmed: %s mode, fixed target %.1f °C, "
                "Fan Auto, fixed oscillation and background Quiet",
                self.snapshot.hvac_mode if self.snapshot else self._thermal_direction,
                self.target_temperature,
            )
            return

        if pending.purpose == "target_reached_internal":
            self._set_internal_satisfied_state()
            self.logger.info(
                "Eco Stand-by base confirmed: fan-only, Low and fixed; "
                "checking background Quiet"
            )
            try:
                await self._strategy.async_maintain(self._latest_status_for_temperature)
            except Exception as err:
                self.logger.exception(
                    "Failed to establish Eco background Quiet after Stand-by confirmation"
                )
                await self._async_restore_original_after_controller_error(
                    purpose="quiet_repair",
                    detail=f"command error: {err}",
                    observed=self._controlled_status(
                        self._latest_status_for_temperature
                    ),
                )
            return

        self.snapshots.clear_pre_sleep()
        self._set_running_state()
        if pending.purpose == "resume_internal":
            self.logger.info(
                "Eco resumed %s mode (room: %.1f °C, target: %.1f °C); "
                "Fan Auto, fixed oscillation and background Quiet confirmed",
                self._thermal_direction,
                self.temperature.value,
                self.target_temperature,
            )
        else:
            self.logger.info(
                "Eco resumed %s mode (room: %.1f °C, target: %.1f °C) using external temperature sensor",
                self._thermal_direction,
                self.temperature.value,
                self.target_temperature,
            )

    async def _async_restore_original_after_controller_error(
        self,
        *,
        purpose: str,
        detail: str,
        observed: dict[str, Any] | None = None,
    ) -> None:
        """Restore the immutable Original Snapshot before releasing failed Eco.

        Physical Samsung Quiet is an implementation detail while Eco owns the
        session.  Therefore an internal controller failure must not expose a
        half-applied Eco profile (for example Quiet or Fixed) as the new normal
        Home Assistant state.  If an Original Snapshot exists, keep Eco visibly
        selected and use the existing serial finish-restore owner.  Eco is only
        released after every restorable snapshot field, including Optional Mode,
        has been coordinator-confirmed.  If that restore itself is rejected, the
        normal finish-restore failure path releases ownership and the confirmed
        physical Samsung state becomes authoritative.
        """

        self.transitions.cancel(reason=f"{purpose}:error_restore")
        self._pending_reconcile = None
        self._eco_configuration_reconcile_pending = False

        if self.snapshot is None:
            self.logger.warning(
                "Eco controller failure has no Original Snapshot to restore; "
                "releasing Eco: purpose=%s detail=%s",
                purpose,
                detail,
            )
            self._reset()
            return

        controlled = (
            observed
            if observed is not None
            else self._controlled_status(self._latest_status_for_temperature)
        )
        self.logger.warning(
            "Eco controller failure: purpose=%s detail=%s; restoring Original "
            "Snapshot before releasing Eco",
            purpose,
            detail,
        )
        restore_coro = self.finish_eco(
            EcoFinishReason.ERROR,
            detail=f"{purpose}: {detail}",
            restore=True,
            observed=controlled,
        )
        if self._command_lock.locked():
            # Some serial command failures are detected while the controller lock
            # is still held by the current step.  Schedule the restore so it starts
            # immediately after that critical section releases instead of deadlocking.
            self.hass.async_create_task(restore_coro)
        else:
            await restore_coro

    async def _async_fail_pending_reconcile(self, reason: str) -> None:
        pending = self._pending_reconcile
        if pending is None:
            return
        self.logger.warning("Eco %s failed: %s", pending.purpose, reason)
        self._pending_reconcile = None
        self.transitions.cancel(reason=reason)
        if pending.finish_eco:
            # The user explicitly left Eco. Do not leave the UI stuck in Eco if
            # SmartThings failed to reach every desired field. Outside Eco there
            # is no later powered-off restore owner to arm.
            self.logger.warning(
                "Eco exit restore failed irrecoverably; releasing AC and leaving Eco: "
                "purpose=%s reason=%s mismatches=%s",
                pending.purpose,
                reason,
                self._state_mismatches(
                    pending.desired,
                    self._controlled_status(self._latest_status_for_temperature),
                ),
            )
            self._reset()
            return

        # Any failed automatic Eco sequence is still controller-owned.  It may
        # have already changed Mode, target, Fan, oscillation or physical Quiet.
        # Never expose that partial profile as the new normal state: restore the
        # immutable Original Snapshot first and only then release Eco ownership.
        observed = self._controlled_status(self._latest_status_for_temperature)
        mismatches = self._state_mismatches(pending.desired, observed)
        self.logger.warning(
            "Eco controller sequence failed irrecoverably: purpose=%s mismatches=%s",
            pending.purpose,
            mismatches,
        )
        await self._async_restore_original_after_controller_error(
            purpose=pending.purpose,
            detail=f"{reason}; mismatches={mismatches}",
            observed=observed,
        )

    async def _async_handle_suspended_status(
        self,
        *,
        observed: dict[str, Any],
        actual_changed_fields: set[str],
    ) -> None:
        """Keep failed controller feedback separate from later user intent.

        A failed automatic Eco reconcile leaves the physical AC in a known,
        unresolved controller-owned state.  Merely continuing to report those
        same mismatches must never become USER_OVERRIDE.  Eco returns to
        RUNNING only after the complete desired state is later observed.

        A genuinely new controlled-setting value that appears *after* the
        suspension and is neither the failed baseline nor the controller's
        desired value is treated as a new external/user intervention.  This
        covers commands from another HA climate entity or automation without
        inventing a retry or polling path.
        """

        desired = self._suspended_desired
        if desired is None:
            return
        baseline = self._suspended_baseline or {}

        if not self._state_mismatches(desired, observed):
            self._suspended_desired = None
            self._suspended_baseline = None
            self.snapshots.clear_pre_sleep()
            self._set_running_state()
            self.logger.info(
                "Eco suspended controller state later confirmed; returning to Running"
            )
            return

        candidate_fields = set(actual_changed_fields)
        # After a restart there may be no in-memory previous observation.  A
        # third value relative to both persisted baseline and desired is still
        # evidence of a new external change and may be handled immediately.
        if not candidate_fields:
            candidate_fields = {
                field
                for field in self.overrides.CONTROLLED_FIELDS
                if field in desired
                and desired.get(field) is not None
                and observed.get(field) is not None
                and not self._controlled_values_match(
                    field, observed.get(field), desired.get(field)
                )
                and baseline.get(field) is not None
                and not self._controlled_values_match(
                    field, observed.get(field), baseline.get(field)
                )
            }

        user_fields: set[str] = set()
        for field in candidate_fields:
            if field not in desired or desired.get(field) is None:
                continue
            actual = observed.get(field)
            if actual is None:
                continue
            if self._controlled_values_match(field, actual, desired.get(field)):
                # Late controller confirmation is welcome, never a user change.
                continue
            baseline_value = baseline.get(field)
            if baseline_value is not None and self._controlled_values_match(
                field, actual, baseline_value
            ):
                # The same unresolved value that existed at timeout remains
                # controller-owned, even on a later feedback bounce.
                continue
            user_fields.add(field)

        if not user_fields:
            # Stay genuinely suspended: no restore, no retry and no premature
            # RUNNING state while any controller-owned mismatch remains.
            self._schedule_save()
            return

        self.logger.info(
            "Eco suspended detected new external controlled-setting change: %s",
            sorted(user_fields),
        )
        self._suspended_desired = None
        self._suspended_baseline = None

        if "power" in user_fields and observed.get("power") == "off":
            await self.finish_eco(
                EcoFinishReason.USER_OFF,
                detail="air conditioner switched off while Eco suspended",
                observed=observed,
            )
            return

        reason = (
            EcoFinishReason.MODE_CHANGED
            if "hvac_mode" in user_fields
            else EcoFinishReason.USER_OVERRIDE
        )
        if "hvac_mode" in user_fields:
            preserve = {"hvac_mode"}
        elif "target_temperature" in user_fields:
            preserve = {"target_temperature"}
        else:
            preserve = user_fields & {"fan_mode", "oscillation_mode"}
        await self.finish_eco(
            reason,
            detail=(
                "manual controlled-setting change after suspended Eco: "
                f"{sorted(user_fields)}"
            ),
            preserve_fields=preserve,
            observed=observed,
        )

    @staticmethod
    def _controlled_values_match(field: str, actual: Any, expected: Any) -> bool:
        """Compare one normalized controlled field using Eco confirmation rules."""

        if (
            field == "target_temperature"
            and isinstance(actual, (int, float))
            and isinstance(expected, (int, float))
        ):
            return abs(float(actual) - float(expected)) <= 0.26
        return actual == expected

    def _desired_from_original_snapshot(
        self, snapshot: EcoRestoreSnapshot
    ) -> dict[str, Any]:
        return {
            "power": "on",
            "hvac_mode": snapshot.hvac_mode,
            "target_temperature": snapshot.target_temperature,
            "fan_mode": snapshot.fan_mode,
            "oscillation_mode": snapshot.oscillation_mode,
            "optional_mode": snapshot.optional_mode,
            "sound_volume": (100 if snapshot.sound_enabled is True else 0 if snapshot.sound_enabled is False else None),
            "auto_clean_mode": ("on" if snapshot.auto_clean_enabled is True else "off" if snapshot.auto_clean_enabled is False else None),
        }

    def _desired_finish_restore_from_snapshot(
        self,
        snapshot: EcoRestoreSnapshot,
        *,
        preserve_fields: set[str],
        observed: dict[str, Any] | None,
        override_values: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the final staged Eco-exit state from Original Snapshot.

        This mirrors normal finish_eco preserve semantics, but produces one
        desired-state map for the staged difference-based reconcile engine.
        """

        desired = self._desired_from_original_snapshot(snapshot)
        overrides = dict(override_values or {})
        observed = observed or {}

        if "hvac_mode" in preserve_fields:
            value = observed.get("hvac_mode")
            if value is not None:
                desired["hvac_mode"] = value

        if "target_temperature" in preserve_fields:
            explicit = overrides.get("target_temperature")
            value = explicit if isinstance(explicit, (int, float)) else observed.get(
                "target_temperature"
            )
            if isinstance(value, (int, float)):
                desired["target_temperature"] = normalize_physical_setpoint(float(value))

        for field in ("fan_mode", "oscillation_mode", "optional_mode"):
            if field in preserve_fields and observed.get(field) is not None:
                desired[field] = observed[field]

        return desired

    def _state_mismatches(
        self, desired: dict[str, Any], observed: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        mismatches: dict[str, dict[str, Any]] = {}
        for key, expected in desired.items():
            if expected is None:
                continue
            actual = observed.get(key)
            if key == "target_temperature" and isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                matches = abs(float(expected) - float(actual)) <= 0.26
            else:
                matches = actual == expected
            if not matches:
                mismatches[key] = {"expected": expected, "observed": actual}
        return mismatches

    async def _async_apply_one_desired_difference(
        self,
        status: dict[str, Any],
        desired: dict[str, Any],
        *,
        prefer_sound_first: bool = False,
    ) -> str | None:
        """Send at most one still-required desired-state command."""

        observed = self._controlled_status(status)
        mismatches = self._state_mismatches(desired, observed)
        fields = (
            (
                "sound_volume",
                "power",
                "hvac_mode",
                "target_temperature",
                "fan_mode",
                "oscillation_mode",
                "optional_mode",
                "auto_clean_mode",
            )
            if prefer_sound_first
            else (
                "power",
                "hvac_mode",
                "target_temperature",
                "fan_mode",
                "oscillation_mode",
                "optional_mode",
                "sound_volume",
                "auto_clean_mode",
            )
        )
        for field in fields:
            if field not in mismatches:
                continue
            if field == "power" and desired.get("power") == "on":
                await self.api.turn_on()
            elif field == "hvac_mode" and desired.get("hvac_mode") in SETPOINT_HVAC_MODES | {"dry", "wind"}:
                await self.api.set_ac_mode(desired["hvac_mode"])
            elif field == "target_temperature" and desired.get("target_temperature") is not None:
                target_mode = desired.get("hvac_mode") or observed.get("hvac_mode")
                if target_mode not in SETPOINT_HVAC_MODES:
                    continue
                await self.api.set_target_temperature(
                    target_mode, desired["target_temperature"]
                )
            elif field == "fan_mode" and desired.get("fan_mode") is not None:
                await self.api.set_fan_speed(desired["fan_mode"])
            elif field == "oscillation_mode" and desired.get("oscillation_mode") is not None:
                await self.api.set_oscillation_mode(desired["oscillation_mode"])
            elif field == "optional_mode" and desired.get("optional_mode") is not None:
                await self.api.set_optional_mode(desired["optional_mode"])
            elif field == "sound_volume" and desired.get("sound_volume") is not None:
                await self.api.set_volume(int(desired["sound_volume"]))
            elif field == "auto_clean_mode" and desired.get("auto_clean_mode") is not None:
                await self.api.set_auto_cleaning_mode(desired["auto_clean_mode"])
            else:
                continue
            return field
        return None

    async def _async_apply_desired_differences(
        self, status: dict[str, Any], desired: dict[str, Any]
    ) -> None:
        """Apply one controller-owned desired sequence with Sound muted first."""

        observed = self._controlled_status(status)
        mismatches = self._state_mismatches(desired, observed)
        if "sound_volume" in mismatches and desired.get("sound_volume") is not None:
            await self.api.set_volume(int(desired["sound_volume"]))
        if "power" in mismatches and desired.get("power") == "on":
            await self.api.turn_on()
        if "hvac_mode" in mismatches and desired.get("hvac_mode") in SETPOINT_HVAC_MODES | {"dry", "wind"}:
            await self.api.set_ac_mode(desired["hvac_mode"])
        if "target_temperature" in mismatches and desired.get("target_temperature") is not None:
            target_mode = desired.get("hvac_mode") or observed.get("hvac_mode")
            if target_mode in SETPOINT_HVAC_MODES:
                await self.api.set_target_temperature(target_mode, desired["target_temperature"])
        if "fan_mode" in mismatches and desired.get("fan_mode") is not None:
            await self.api.set_fan_speed(desired["fan_mode"])
        if "oscillation_mode" in mismatches and desired.get("oscillation_mode") is not None:
            await self.api.set_oscillation_mode(desired["oscillation_mode"])
        if "optional_mode" in mismatches and desired.get("optional_mode") is not None:
            await self.api.set_optional_mode(desired["optional_mode"])
        if "auto_clean_mode" in mismatches and desired.get("auto_clean_mode") is not None:
            await self.api.set_auto_cleaning_mode(desired["auto_clean_mode"])

    def _active_eco_desired(self, mode: str) -> dict[str, Any]:
        """Return the single active Eco physical profile.

        Active Eco deliberately uses Samsung Quiet in the background while the
        Home Assistant preset remains Eco: Cool/Heat + Fan Auto + fixed
        oscillation + Sound off.
        """

        return {
            "power": "on",
            "hvac_mode": mode,
            "target_temperature": normalize_physical_setpoint(
                self.target_temperature
                if self.target_temperature is not None
                else self.eco_target_temperature
            ),
            "fan_mode": "auto",
            "oscillation_mode": "fixed",
            "optional_mode": "quiet",
            "sound_volume": 0,
        }

    async def _async_activate(
        self,
        status: dict[str, Any],
        mode: str,
        *,
        snapshot_overrides: dict[str, Any] | None = None,
        snapshot_seed: EcoRestoreSnapshot | None = None,
    ) -> bool:
        if self.temperature.value is None:
            self.state = EcoState.TEMPERATURE_UNAVAILABLE
            await self._async_save()
            return False
        async with self._command_lock:
            if self.snapshot is None:
                self.snapshot = (
                    self._overlay_snapshot(snapshot_seed, snapshot_overrides)
                    if snapshot_seed is not None
                    else self._create_snapshot(
                        status, mode, overrides=snapshot_overrides
                    )
                )
            self._thermal_direction = self._infer_thermal_direction(mode)
            self.target_temperature = self.eco_target_temperature
            self._eco_configuration_reconcile_pending = False
            desired = self._active_eco_desired(mode)
            # Keep Eco visibly selected while the physical profile is established,
            # but serialize every Samsung difference through coordinator truth.
            self._set_running_state()
            await self._async_start_pending_reconcile(
                purpose="activate",
                desired=desired,
                detail="Eco activation",
                finish_eco=False,
            )
            await self._async_save()
            self.logger.info(
                "Eco activation started in %s mode (%s direction) with fixed target %.1f °C; "
                "serial confirmation will establish Fan Auto, fixed oscillation and background Quiet using %s sensor",
                mode,
                self._thermal_direction,
                self.target_temperature,
                self.temperature_source_type,
            )
            return True

    def _target_is_reached(self, mode: str) -> bool:
        return self._temperature_threshold_met(mode, restart=False)

    def _restart_is_required(self, mode: str) -> bool:
        return self._temperature_threshold_met(mode, restart=True)

    def _temperature_threshold_met(self, mode: str, *, restart: bool) -> bool:
        measured = self.temperature.value
        target = self.target_temperature
        direction = self._thermal_direction_for(mode)
        if measured is None or target is None or direction is None:
            return False
        if direction == "cool":
            threshold = target + self.eco_hysteresis if restart else target
            return measured >= threshold if restart else measured <= threshold
        threshold = target - self.eco_hysteresis if restart else target
        return measured <= threshold if restart else measured >= threshold

    def _thermal_direction_for(self, mode: str | None) -> str | None:
        """Return the immutable heating/cooling direction of the Eco session.

        Once Eco has activated, threshold checks, Stand-by and resume always use
        the direction stored in the session context. The current Samsung HVAC
        mode may temporarily be ``wind`` during internal Stand-by and must never
        be used to reinterpret the session direction.
        """

        if self._thermal_direction in ECO_SUPPORTED_HVAC_MODES:
            return self._thermal_direction
        return mode if mode in ECO_SUPPORTED_HVAC_MODES else None

    @staticmethod
    def _infer_thermal_direction(mode: str | None) -> str | None:
        """Return Eco's fixed thermal direction for Cool or Heat."""

        return mode if mode in ECO_SUPPORTED_HVAC_MODES else None

    async def _async_target_reached(self, mode: str) -> None:
        """Choose exactly one target-reached route for this rest cycle."""

        async with self._command_lock:
            if self.state != EcoState.RUNNING:
                return

            external = self._configured_external_temperature_reading()
            if external is not None and external.valid:
                route = "sleep"
                strategy: EcoStrategy = EcoExternalStrategy(self)
                self.temperature = external
                reason = "external sensor available"
            else:
                route = "standby"
                strategy = EcoInternalStrategy(self)
                internal = self._internal_temperature_reading(self._latest_status_for_temperature)
                if internal.valid:
                    self.temperature = internal
                reason = (
                    f"external sensor unavailable ({external.reason})"
                    if external is not None
                    else "no external sensor configured"
                )

            self._target_reached_route = route
            self._strategy = strategy
            self.logger.info(
                "Eco target-reached route selected: %s (%s)",
                route,
                reason,
            )
            if self._preset_off_requested:
                self.logger.info(
                    "Eco target-reached route cancelled before command send by newer preset-off intent"
                )
                return
            try:
                await strategy.async_target_reached(mode)
            except Exception as err:
                self.logger.exception("Failed to apply Eco target-reached strategy")
                await self._async_restore_original_after_controller_error(
                    purpose="target_reached",
                    detail=f"command error: {err}",
                    observed=self._controlled_status(
                        self._latest_status_for_temperature
                    ),
                )
                return

    def _strategy_for_satisfied_state(self) -> EcoStrategy:
        """Return the route strategy selected for the current rest cycle."""

        if self.state == EcoState.SATISFIED_OFF:
            if not isinstance(self._strategy, EcoExternalStrategy):
                self._strategy = EcoExternalStrategy(self)
            return self._strategy
        if self.state == EcoState.SATISFIED_FAN_QUIET:
            if not isinstance(self._strategy, EcoInternalStrategy):
                self._strategy = EcoInternalStrategy(self)
            return self._strategy
        return self._strategy

    async def _async_restart_after_temperature_drift(self, mode: str) -> None:
        if self.target_temperature is None:
            self.logger.warning(
                "Eco restart context is incomplete; restoring pre-Eco state"
            )
            await self._async_restore_original_after_controller_error(
                purpose="restart_context",
                detail="logical Eco target is unavailable",
                observed=self._controlled_status(
                    self._latest_status_for_temperature
                ),
            )
            return
        async with self._command_lock:
            try:
                await self._strategy_for_satisfied_state().async_restart(mode)
            except Exception as err:
                self.logger.exception("Failed to restart Eco after temperature drift")
                await self._async_restore_original_after_controller_error(
                    purpose="restart",
                    detail=f"command error: {err}",
                    observed=self._controlled_status(
                        self._latest_status_for_temperature
                    ),
                )
                return

    def _set_external_satisfied_state(self) -> None:
        self.state = EcoState.SATISFIED_OFF
        self._satisfied_at = dt_util.utcnow()
        self._schedule_save()

    def _set_internal_satisfied_state(self) -> None:
        self.state = EcoState.SATISFIED_FAN_QUIET
        self._satisfied_at = dt_util.utcnow()
        self._schedule_save()

    def _set_running_state(self) -> None:
        self.state = EcoState.RUNNING
        self._suspended_desired = None
        self._suspended_baseline = None
        self._satisfied_at = None
        self._schedule_save()

    def _reset(self, *, keep_pending: bool = False) -> None:
        self.state = (
            EcoState.PENDING_RESTORE
            if keep_pending and self.pending_restore is not None
            else EcoState.DISABLED
        )
        self.snapshots.clear_session()
        if not keep_pending:
            self.snapshots.clear_pending()
            self._pending_restore_immediate_off_safe = False
        self.target_temperature = None
        self._thermal_direction = None
        self._satisfied_at = None
        self._configuration_resume_in_progress = False
        self._target_reached_route = None
        self._pending_reconcile = None
        self._queued_reenable_after_finish = False
        self._queued_reenable_snapshot = None
        self._queued_reenable_overrides = {}
        self._suspended_desired = None
        self._suspended_baseline = None
        self._pending_sleep_override = None
        self._pending_sleep_user_off = False
        self._eco_configuration_reconcile_pending = False
        self._latest_status_for_temperature = {}
        self._strategy = self._create_strategy()
        self._stored_context = None
        self._restore_attempted = True
        self._schedule_save()

    def _temperature_reading(self, status: dict[str, Any]) -> EcoTemperatureReading:
        """Return the best usable temperature for the running Eco engine.

        A configured external sensor remains preferred. If it is temporarily
        unusable, the existing internal-temperature path keeps the Eco cycle
        running so the target-reached switch can choose Stand-by.
        """

        external = self._configured_external_temperature_reading()
        if external is not None and external.valid:
            return external
        return self._internal_temperature_reading(status)

    def _configured_external_temperature_reading(
        self,
    ) -> EcoTemperatureReading | None:
        entity_id = self.external_temperature_sensor
        return self._external_temperature_reading(entity_id) if entity_id else None

    def _internal_temperature_reading(
        self, status: dict[str, Any]
    ) -> EcoTemperatureReading:
        """Validate Samsung's event-driven internal temperature reading.

        Samsung commonly updates the capability timestamp only when the numeric
        temperature changes. Therefore timestamp age is diagnostic information
        for the internal strategy, not a validity criterion.
        """

        value = _attribute_value(status, "temperatureMeasurement", "temperature")
        updated_at = _attribute_timestamp(
            status, "temperatureMeasurement", "temperature"
        )
        try:
            temperature = float(value)
        except (TypeError, ValueError):
            return EcoTemperatureReading(
                None, updated_at, None, False, "non_numeric"
            )
        if updated_at is not None and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=dt_util.UTC)
        return EcoTemperatureReading(temperature, updated_at, None, True)

    def _external_temperature_reading(self, entity_id: str) -> EcoTemperatureReading:
        """Return a configured HA sensor while its current state is usable.

        Home Assistant temperature entities often keep the same numeric state
        for a long time.  Age of the last report/change is therefore diagnostic
        metadata, not evidence that the sensor is unavailable.  Only a missing,
        unknown/unavailable, or non-numeric current state invalidates the
        configured external Eco source.
        """

        state: State | None = self.hass.states.get(entity_id)
        if state is None:
            return EcoTemperatureReading(None, None, entity_id, False, "entity_not_found")
        reported_at = getattr(state, "last_reported", state.last_updated)
        if state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE, ""}:
            return EcoTemperatureReading(None, reported_at, entity_id, False, "state_unavailable")
        try:
            temperature = float(state.state)
        except (TypeError, ValueError):
            return EcoTemperatureReading(None, reported_at, entity_id, False, "non_numeric")
        if reported_at is not None and reported_at.tzinfo is None:
            reported_at = reported_at.replace(tzinfo=dt_util.UTC)
        return EcoTemperatureReading(temperature, reported_at, entity_id, True)

    def _snapshot_for_strategy(
        self, snapshot: EcoRestoreSnapshot
    ) -> EcoRestoreSnapshot:
        """Remove Auto Clean ownership from Internal Strategy snapshots.

        Sound remains part of the Original Snapshot for both strategies because
        Eco owns/mutes Sound for its complete active session and restores the
        user's pre-Eco Sound state on exit.  Auto Clean remains external-Sleep
        specific and is therefore omitted for the Internal/Stand-by strategy.
        """

        if self.external_temperature_sensor:
            return snapshot
        return EcoRestoreSnapshot(
            hvac_mode=snapshot.hvac_mode,
            target_temperature=snapshot.target_temperature,
            fan_mode=snapshot.fan_mode,
            oscillation_mode=snapshot.oscillation_mode,
            optional_mode=snapshot.optional_mode,
            sound_enabled=snapshot.sound_enabled,
            auto_clean_enabled=None,
        )

    def _create_snapshot(
        self,
        status: dict[str, Any],
        mode: str,
        *,
        overrides: dict[str, str] | None = None,
    ) -> EcoRestoreSnapshot:
        """Capture one immutable pre-Eco snapshot with a user-intent overlay."""

        overrides = overrides or {}
        volume = _attribute_value(status, "audioVolume", "volume")
        auto_clean = _string_value(
            _attribute_value(status, "custom.autoCleaningMode", "autoCleaningMode")
        )
        sound_enabled = (
            bool(volume > 0) if isinstance(volume, (int, float)) else None
        )
        auto_clean_enabled = (
            (auto_clean == "on")
            if self.external_temperature_sensor and auto_clean in {"on", "off"}
            else None
        )
        physical_fan = _string_value(
            _attribute_value(status, "airConditionerFanMode", "fanMode")
        )
        physical_oscillation = _string_value(
            _attribute_value(status, "fanOscillationMode", "fanOscillationMode")
        )
        physical_target = resolve_target_temperature(status, mode)
        override_target = overrides.get("target_temperature")
        return EcoRestoreSnapshot(
            hvac_mode=overrides.get("hvac_mode", mode),
            target_temperature=(
                normalize_physical_setpoint(float(override_target))
                if isinstance(override_target, (int, float))
                else physical_target
            ),
            fan_mode=overrides.get("fan_mode", physical_fan),
            oscillation_mode=overrides.get(
                "oscillation_mode", physical_oscillation
            ),
            optional_mode=overrides.get(
                "optional_mode",
                _string_value(_attribute_value(status, "custom.airConditionerOptionalMode", "acOptionalMode")),
            ),
            sound_enabled=(
                bool(overrides["sound_enabled"])
                if isinstance(overrides.get("sound_enabled"), bool)
                else sound_enabled
            ),
            auto_clean_enabled=(
                bool(overrides["auto_clean_enabled"])
                if isinstance(overrides.get("auto_clean_enabled"), bool)
                else auto_clean_enabled
            ),
        )

    def _schedule_save(self) -> None:
        """Persist Eco state without blocking a strategy command sequence."""

        self.hass.async_create_task(self._async_save())

    async def _async_save(self) -> None:
        """Persist limits and the active Eco context."""

        context: dict[str, Any] | None = None
        if self.enabled:
            context = {
                "active": True,
                "state": self.state.value,
                "strategy": self.temperature_source_type,
                "snapshot": asdict(self.snapshot) if self.snapshot else None,
                "pre_sleep": (
                    asdict(self.snapshots.pre_sleep)
                    if self.snapshots.pre_sleep else None
                ),
                "target_temperature": self.target_temperature,
                "thermal_direction": self._thermal_direction,
                "target_reached_route": self._target_reached_route,
                "temperature_source_entity": self.external_temperature_sensor,
                "last_temperature": self.temperature.value,
                "last_temperature_updated_at": (
                    self.temperature.updated_at.isoformat()
                    if self.temperature.updated_at is not None
                    else None
                ),
                "satisfied_at": (
                    self._satisfied_at.isoformat()
                    if self._satisfied_at is not None
                    else None
                ),
                "suspended_desired": (
                    dict(self._suspended_desired)
                    if self._suspended_desired is not None
                    else None
                ),
                "suspended_baseline": (
                    dict(self._suspended_baseline)
                    if self._suspended_baseline is not None
                    else None
                ),
            }
        await self._store.async_save(
            {
                "control_model": "target_hysteresis_v1",
                "target_temperature": self.eco_target_temperature,
                "hysteresis": self.eco_hysteresis,
                "context": context,
                "pending_restore": (
                    asdict(self.pending_restore) if self.pending_restore else None
                ),
                "pending_restore_immediate_off_safe": (
                    self._pending_restore_immediate_off_safe
                    if self.pending_restore is not None
                    else False
                ),
            }
        )

    async def _async_restore_context(self, status: dict[str, Any]) -> None:
        """Safely reconstruct an active Eco cycle after reload or restart."""

        self._restore_attempted = True
        context = self._stored_context
        self._stored_context = None
        if not context or context.get("active") is not True:
            return

        try:
            stored_strategy = str(context.get("strategy"))
            if stored_strategy != self.temperature_source_type:
                raise ValueError("configured temperature strategy changed")

            raw_state = EcoState(str(context.get("state")))
            if raw_state not in _ACTIVE_STATES:
                raise ValueError(f"state {raw_state.value} is not recoverable")

            # Some pre-release versions persisted failed automatic reconciles as
            # SUSPENDED together with controller-owned desired/baseline markers.
            # The later fail-safe policy makes those terminal, so
            # do not resurrect such a failed Eco session after an upgrade/restart.
            if raw_state == EcoState.SUSPENDED and isinstance(
                context.get("suspended_desired"), dict
            ):
                self.logger.warning(
                    "Discarding persisted unrecoverable Eco suspension; releasing AC and leaving Eco"
                )
                self._reset()
                await self._async_save()
                return

            raw_snapshot = context.get("snapshot")
            snapshot = (
                self._snapshot_for_strategy(EcoRestoreSnapshot(**raw_snapshot))
                if isinstance(raw_snapshot, dict)
                else None
            )
            target = _float_or_none(context.get("target_temperature"))
            if raw_state != EcoState.ARMED and (snapshot is None or target is None):
                raise ValueError("snapshot or target temperature missing")

            if not self._context_matches_status(
                status=status,
                state=raw_state,
                snapshot=snapshot,
                target=target,
            ):
                raise ValueError("actual AC state no longer matches stored Eco context")

            self.snapshot = snapshot
            raw_pre_sleep = context.get("pre_sleep")
            self.snapshots.pre_sleep = (
                EcoPreSleepSnapshot(
                    auto_clean_enabled=(
                        raw_pre_sleep.get("auto_clean_enabled")
                        if isinstance(raw_pre_sleep.get("auto_clean_enabled"), bool)
                        else None
                    )
                )
                if isinstance(raw_pre_sleep, dict)
                else None
            )
            self.target_temperature = target
            self._eco_configuration_reconcile_pending = (
                raw_state != EcoState.ARMED
                and not self._controlled_values_match(
                    "target_temperature", target, self.eco_target_temperature
                )
            )
            raw_direction = context.get("thermal_direction")
            if raw_state == EcoState.ARMED:
                self._thermal_direction = None
            elif raw_direction in ECO_SUPPORTED_HVAC_MODES:
                self._thermal_direction = str(raw_direction)
            else:
                raise ValueError("stored Eco thermal direction missing or invalid")
            self.state = raw_state
            raw_suspended_desired = context.get("suspended_desired")
            raw_suspended_baseline = context.get("suspended_baseline")
            if raw_state == EcoState.SUSPENDED:
                self._suspended_desired = (
                    dict(raw_suspended_desired)
                    if isinstance(raw_suspended_desired, dict)
                    else self._expected_eco_status()
                )
                self._suspended_baseline = (
                    dict(raw_suspended_baseline)
                    if isinstance(raw_suspended_baseline, dict)
                    else self._controlled_status(status)
                )
            else:
                self._suspended_desired = None
                self._suspended_baseline = None
            self._target_reached_route = (
                "sleep" if raw_state == EcoState.SATISFIED_OFF
                else "standby" if raw_state == EcoState.SATISFIED_FAN_QUIET
                else None
            )
            raw_satisfied = context.get("satisfied_at")
            self._satisfied_at = (
                dt_util.parse_datetime(raw_satisfied)
                if isinstance(raw_satisfied, str)
                else None
            )
            await self._async_save()
            self.logger.info(
                "Eco context restored: strategy=%s state=%s direction=%s target=%s",
                stored_strategy,
                raw_state.value,
                self._thermal_direction or "not-set",
                f"{target:.1f} °C" if target is not None else "none",
            )
        except (TypeError, ValueError) as err:
            self.snapshot = None
            self.target_temperature = None
            self._thermal_direction = None
            self._satisfied_at = None
            self._suspended_desired = None
            self._suspended_baseline = None
            self.state = EcoState.INVALID_CONTEXT
            await self._async_save()
            self.logger.warning("Eco context was not restored: %s", err)

    def _context_matches_status(
        self,
        *,
        status: dict[str, Any],
        state: EcoState,
        snapshot: EcoRestoreSnapshot | None,
        target: float | None,
    ) -> bool:
        """Return whether current device state safely matches stored context."""

        if state == EcoState.ARMED:
            return True
        if snapshot is None or target is None or snapshot.hvac_mode not in ECO_SUPPORTED_HVAC_MODES:
            return False

        power = _string_value(_attribute_value(status, "switch", "switch"))
        mode = _string_value(
            _attribute_value(status, "airConditionerMode", "airConditionerMode")
        )
        fan = _string_value(
            _attribute_value(status, "airConditionerFanMode", "fanMode")
        )
        oscillation = _string_value(
            _attribute_value(status, "fanOscillationMode", "fanOscillationMode")
        )
        optional = _string_value(
            _attribute_value(
                status,
                "custom.airConditionerOptionalMode",
                "acOptionalMode",
            )
        )

        if state == EcoState.SATISFIED_OFF:
            return power == "off"
        if state == EcoState.SATISFIED_FAN_QUIET:
            volume = _attribute_value(status, "audioVolume", "volume")
            return (
                power == "on"
                and mode == "wind"
                and fan == "low"
                and oscillation == "fixed"
                and optional == "quiet"
                and isinstance(volume, (int, float))
                and int(volume) == 0
            )
        if state == EcoState.SUSPENDED:
            # A suspended cycle intentionally represents a physical mismatch.
            # Restore the controller context and let persisted ownership decide.
            return True
        if state == EcoState.RUNNING:
            observed_target = resolve_target_temperature(
                status, snapshot.hvac_mode
            )
            volume = _attribute_value(status, "audioVolume", "volume")
            return (
                power == "on"
                and mode == snapshot.hvac_mode
                and fan == "auto"
                and oscillation == "fixed"
                and optional == "quiet"
                and isinstance(volume, (int, float))
                and int(volume) == 0
                and observed_target is not None
                and abs(
                    observed_target - normalize_physical_setpoint(target)
                ) <= 0.01
            )
        return False

    def _controlled_status(self, status: dict[str, Any]) -> dict[str, Any]:
        """Normalize the controlled Samsung settings for ownership checks."""

        mode = _string_value(
            _attribute_value(status, "airConditionerMode", "airConditionerMode")
        )
        target = resolve_target_temperature(status, mode)
        volume = _attribute_value(status, "audioVolume", "volume")
        return {
            "power": _string_value(_attribute_value(status, "switch", "switch")),
            "hvac_mode": mode,
            "target_temperature": target,
            "fan_mode": _string_value(
                _attribute_value(status, "airConditionerFanMode", "fanMode")
            ),
            "oscillation_mode": _string_value(
                _attribute_value(status, "fanOscillationMode", "fanOscillationMode")
            ),
            "optional_mode": _string_value(
                _attribute_value(
                    status, "custom.airConditionerOptionalMode", "acOptionalMode"
                )
            ),
            "sound_volume": int(volume) if isinstance(volume, (int, float)) else None,
            "auto_clean_mode": _string_value(
                _attribute_value(status, "custom.autoCleaningMode", "autoCleaningMode")
            ),
        }

    def _expected_eco_status(self) -> dict[str, Any]:
        """Return the settings currently owned by the active Eco session."""

        snapshot = self.snapshot
        if self.state == EcoState.SATISFIED_OFF:
            return {"power": "off"}
        if self.state == EcoState.SATISFIED_FAN_QUIET:
            return {
                "power": "on",
                "hvac_mode": "wind",
                "fan_mode": "low",
                "oscillation_mode": "fixed",
                "optional_mode": "quiet",
                "sound_volume": 0,
            }
        if self.state in {EcoState.RUNNING, EcoState.SUSPENDED}:
            return {
                "power": "on",
                "hvac_mode": snapshot.hvac_mode if snapshot else None,
                "target_temperature": (
                    normalize_physical_setpoint(self.target_temperature)
                    if self.target_temperature is not None
                    else None
                ),
                "fan_mode": "auto",
                "oscillation_mode": "fixed",
                "optional_mode": "quiet",
                "sound_volume": 0,
            }
        return {}

    @staticmethod
    def _status_attribute_value(
        status: dict[str, Any], capability: str, attribute: str
    ) -> Any:
        return _attribute_value(status, capability, attribute)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "strategy": type(self._strategy).__name__,
            "target_temperature": self.target_temperature,
            "thermal_direction": self._thermal_direction,
            "configured_target_temperature": self.eco_target_temperature,
            "hysteresis": self.eco_hysteresis,
            "temperature_valid": self.temperature.valid,
            "temperature_reason": self.temperature.reason,
            "snapshot": asdict(self.snapshot) if self.snapshot else None,
            "pre_sleep": (
                asdict(self.snapshots.pre_sleep) if self.snapshots.pre_sleep else None
            ),
            "pending_reconcile": (
                {
                    "purpose": self._pending_reconcile.purpose,
                    "desired": dict(self._pending_reconcile.desired),
                }
                if self._pending_reconcile else None
            ),
            "suspended_desired": (
                dict(self._suspended_desired)
                if self._suspended_desired is not None
                else None
            ),
            "suspended_baseline": (
                dict(self._suspended_baseline)
                if self._suspended_baseline is not None
                else None
            ),
            "pending_sleep_override": (
                dict(self._pending_sleep_override)
                if self._pending_sleep_override else None
            ),
            "pending_sleep_user_off": self._pending_sleep_user_off,
            "satisfied_at": self._satisfied_at.isoformat() if self._satisfied_at else None,
            "transition": self.transitions.diagnostics(),
            "pending_restore": (
                asdict(self.pending_restore) if self.pending_restore else None
            ),
            "pending_restore_immediate_off_safe": (
                self._pending_restore_immediate_off_safe
            ),
            "restore_attempted": self._restore_attempted,
        }


def _main_component(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    components = data.get("components", {})
    if not isinstance(components, dict):
        return {}
    main = components.get("main", {})
    return main if isinstance(main, dict) else {}


def _attribute_data(data: dict[str, Any] | None, capability: str, attribute: str) -> dict[str, Any]:
    capability_data = _main_component(data).get(capability, {})
    if not isinstance(capability_data, dict):
        return {}
    attribute_data = capability_data.get(attribute, {})
    return attribute_data if isinstance(attribute_data, dict) else {}


def _attribute_value(data: dict[str, Any] | None, capability: str, attribute: str) -> Any:
    return _attribute_data(data, capability, attribute).get("value")


def _attribute_timestamp(data: dict[str, Any] | None, capability: str, attribute: str) -> datetime | None:
    raw = _attribute_data(data, capability, attribute).get("timestamp")
    return dt_util.parse_datetime(raw) if isinstance(raw, str) else None


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))
