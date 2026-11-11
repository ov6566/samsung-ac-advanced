"""Samsung AC Advanced coordinator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SamsungACAPI
from .const import (
    COORDINATOR_UPDATE_INTERVAL,
    OPTIMISTIC_UI_CONFIRMATION_TIMEOUT,
    SETPOINT_HVAC_MODES,
)
from .debug import SamsungACDebugTracker
from .device_logger import SamsungACDeviceLoggerAdapter, SamsungACRuntimeLogState
from .eco_controller import EcoFinishReason, SamsungACEcoController
from .eco_snapshot import EcoRestoreSnapshot
from .frost_controller import SamsungACFrostController
from .local_settings import LocalDesiredSettings
from .setpoint import resolve_target_temperature

_LOGGER = logging.getLogger(__name__)
_LOCAL_STORE_VERSION = 1
_LOCAL_APPLY_TIMEOUT = 120.0


def _attribute_value(
    data: dict[str, Any] | None, capability: str, attribute: str
) -> Any:
    if not isinstance(data, dict):
        return None
    components = data.get("components", {})
    if not isinstance(components, dict):
        return None
    main = components.get("main", {})
    if not isinstance(main, dict):
        return None
    capability_data = main.get(capability, {})
    if not isinstance(capability_data, dict):
        return None
    attribute_data = capability_data.get(attribute, {})
    if not isinstance(attribute_data, dict):
        return None
    return attribute_data.get("value")


def _power(data: dict[str, Any] | None) -> str | None:
    value = _attribute_value(data, "switch", "switch")
    return value if isinstance(value, str) else None


def _mode(data: dict[str, Any] | None) -> str | None:
    value = _attribute_value(data, "airConditionerMode", "airConditionerMode")
    return value if isinstance(value, str) else None


@dataclass(slots=True)
class _PendingUserControl:
    """Explicit normal user intent awaiting coordinator confirmation."""

    value: str
    deadline: float


class SamsungACAdvancedCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate shared Samsung AC status updates."""

    def __init__(
        self, hass: HomeAssistant, api: SamsungACAPI, entry: ConfigEntry
    ) -> None:
        self.api = api
        self.entry = entry
        device_name = entry.data.get("device_name", entry.title)
        self.runtime_log_state = SamsungACRuntimeLogState()
        self.device_logger = SamsungACDeviceLoggerAdapter(
            _LOGGER, device_name, self.runtime_log_state
        )
        self.debug_tracker = SamsungACDebugTracker(
            device_name, self.runtime_log_state
        )
        self.eco = SamsungACEcoController(
            hass, entry, api, self.runtime_log_state
        )
        self.frost = SamsungACFrostController(
            hass, entry, api, self.runtime_log_state
        )
        self.local_desired: LocalDesiredSettings | None = None
        self._local_apply_lock = asyncio.Lock()
        self._local_apply_sent_at: float | None = None
        self._local_in_flight_field: str | None = None
        self._local_in_flight_expected: Any = None
        self._queued_eco_after_local_restore = False
        self._queued_eco_snapshot_seed: EcoRestoreSnapshot | None = None
        self._queued_eco_snapshot_overrides: dict[str, Any] = {}
        # A fresh Eco selection may arrive while a normal user command (for
        # example Oscillation Vertical) is still awaiting Samsung feedback.
        # Keep Eco visually armed, but do not let its controller commands race
        # that older user transport.  The stored overrides become the immutable
        # pre-Eco snapshot once the older boundary is confirmed/expired.
        self._queued_eco_after_pending_user_controls = False
        self._queued_eco_pending_user_overrides: dict[str, Any] = {}
        self._queued_eco_pending_user_snapshot_seed: EcoRestoreSnapshot | None = None
        self._pending_local_user_overrides: set[str] = set()
        self._pending_user_controls: dict[str, _PendingUserControl] = {}
        self._pending_power_on_hvac_mode: str | None = None
        self._pending_power_on_hvac_mode_sent = False
        self._pending_power_on_requested = False
        self._pending_power_off = False
        self._local_store: Store[dict[str, Any]] = Store(
            hass, _LOCAL_STORE_VERSION, f"{entry.domain}.{entry.entry_id}.local_settings"
        )

        super().__init__(
            hass,
            self.device_logger,
            name="Samsung AC Advanced",
            update_interval=timedelta(seconds=COORDINATOR_UPDATE_INTERVAL),
        )
        # INFO diagnostics are opened by SamsungACDeviceLoggerAdapter and gated
        # per device by the Debug-mode runtime state; no YAML logger override is
        # required.
        self.eco.set_confirmation_refresh_callback(
            self._schedule_controller_confirmation_refresh
        )
        self.frost.set_confirmation_refresh_callback(
            self._schedule_controller_confirmation_refresh
        )

    @callback
    def _schedule_controller_confirmation_refresh(self) -> None:
        """Schedule one non-blocking fresh read for a controller-owned step.

        Eco/Frost controller commands can be issued while processing a coordinator
        update. Awaiting another refresh from inside that update would recurse
        into the coordinator lock. Scheduling it lets the current update finish
        first; the fresh read then runs immediately afterwards. No polling loop
        is created and no next controller command is sent until status confirms
        the previous one.
        """

        self.hass.async_create_task(self.async_refresh())

    async def async_initialize(self) -> None:
        """Initialize persistent deferred Eco Pending Restore settings."""

        await self.eco.async_initialize()
        await self.frost.async_initialize()

        external_sensor = (
            self.eco.external_temperature_sensor
            or self.frost.external_temperature_sensor
        )
        if external_sensor is not None:
            self.entry.async_on_unload(
                async_track_state_change_event(
                    self.hass,
                    [external_sensor],
                    self._async_external_temperature_changed,
                )
            )

        stored = await self._local_store.async_load() or {}
        values = stored.get("local_desired")
        discarded_legacy_owner = False
        if isinstance(values, dict):
            try:
                candidate = LocalDesiredSettings(**values)
            except TypeError:
                candidate = None
            if candidate is not None and candidate.source == "eco_snapshot":
                self.local_desired = candidate
            else:
                # Only the explicit Eco USER_OFF/Pending Restore exception may
                # survive a restart and restore pre-Eco settings later. Older
                # normal powered-off owners are deliberately discarded.
                self.local_desired = None
                discarded_legacy_owner = True

        legacy_waiting_flag = bool(
            stored.get("local_waiting_for_off_confirmation", False)
        )
        if discarded_legacy_owner or legacy_waiting_flag:
            await self._async_save_local()
            if discarded_legacy_owner:
                self.device_logger.info(
                    "Discarded legacy non-Eco powered-off restore state"
                )

    @callback
    def _async_external_temperature_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Process a local external temperature event without SmartThings reads."""

        if event.data.get("new_state") is None:
            return
        self.eco.refresh_external_temperature()
        self.frost.refresh_temperature()
        self.async_update_listeners()
        self.hass.async_create_task(self._async_evaluate_external_temperature_change())

    async def _async_evaluate_external_temperature_change(self) -> None:
        """Evaluate a local room-temperature event for the active controller."""

        if self.eco.enabled:
            before_state = self.eco.state
            before_transition = self.eco.transitions.name
            await self.eco.async_evaluate_external_temperature_change()
            if (
                self.eco.state != before_state
                or self.eco.transitions.name != before_transition
            ):
                await self.async_request_refresh()
            return

        if self.frost.enabled:
            # Frost schedules one fast confirmation refresh itself only when it
            # actually sends a Samsung command. A temperature change that does
            # not cross a threshold causes no extra SmartThings read.
            await self.frost.async_evaluate_external_temperature_change()
            self.async_update_listeners()

    def set_debug_mode(self, enabled: bool) -> None:
        """Enable/disable all per-device integration diagnostics at runtime."""

        if enabled:
            self.runtime_log_state.enabled = True
            self.debug_tracker.enable(self.data)
            self.eco.transitions.set_debug_enabled(True)
            return

        # Keep the gate open long enough to record the final disable line.
        self.debug_tracker.disable()
        self.eco.transitions.set_debug_enabled(False)
        self.runtime_log_state.enabled = False

    async def async_enable_frost(self, status: dict[str, Any]) -> bool:
        """Enable Frost only at a clean transport/ownership boundary."""

        self._expire_pending_user_controls()
        if (
            self.eco.enabled
            or self.local_values_active
            or bool(self._pending_user_controls)
            or self._pending_power_off
            or self._pending_power_on_hvac_mode is not None
        ):
            self.device_logger.info(
                "Frost Protection selection deferred/rejected because another command owner is still active"
            )
            return False
        return await self.frost.async_enable(status)

    async def async_disable_frost(self, *, reason: str) -> None:
        """Release Frost before applying a newer normal user setting."""

        await self.frost.async_disable(reason=reason)
        self.async_update_listeners()

    async def async_turn_off(self) -> None:
        """Send one normal Power Off and block opposite commands until confirmed.

        The latest explicit user Off always wins over an older Eco transition or
        Eco-exit restore.  Eco first relinquishes or cancels that older owner; only
        then is the one physical Power Off command sent.
        """

        self.cancel_queued_eco_activation()
        if await self.eco.async_prepare_user_off():
            self.async_update_listeners()

        # A deferred Eco Pending Restore is intentionally retained while the AC
        # simply waits Off for a future deliberate Power On. Once that restore
        # has actually started, however, a newer explicit Mode Off is an abort:
        # do not keep the old pre-Eco destination alive for a later Power On.
        active_pending_restore = (
            self.local_desired is not None
            and self.local_desired.source == "eco_snapshot"
            and (
                self._local_apply_sent_at is not None
                or self._local_in_flight_field is not None
                or self._pending_power_on_hvac_mode is not None
                or self._pending_power_on_requested
                or _power(self.data) == "on"
            )
        )
        if active_pending_restore:
            self.device_logger.info(
                "Explicit Mode Off cancelled active Eco Pending Restore; "
                "remaining old restore destination discarded"
            )
            self.local_desired = None
            self._local_apply_sent_at = None
            self._local_in_flight_field = None
            self._local_in_flight_expected = None
            self._pending_local_user_overrides.clear()
            await self._async_save_local()

        self._pending_power_off = True
        self._pending_power_on_requested = False
        try:
            await self.api.turn_off()
        except Exception:
            self._pending_power_off = False
            raise
        self.device_logger.info(
            "Power Off staged until coordinator confirms physical Off"
        )

    async def async_set_hvac_mode(self, mode: str) -> None:
        """Set HVAC mode with confirmed power sequencing when needed.

        Samsung may reject setAirConditionerMode with HTTP 422 while the device
        is still changing power state.  If Power Off is still in flight, keep the
        new mode only as user intent.  After physical Off is confirmed, send one
        Power On and wait for its confirmation before sending Mode at most once.
        """

        if self._pending_power_off:
            self._pending_power_on_hvac_mode = mode
            self._pending_power_on_hvac_mode_sent = False
            self._pending_power_on_requested = False
            self.device_logger.info(
                "HVAC mode %s staged behind pending Power Off confirmation", mode
            )
            return

        if _power(self.data) == "on":
            self._pending_power_on_hvac_mode = None
            self._pending_power_on_hvac_mode_sent = False
            self._pending_power_on_requested = False
            await self.api.set_ac_mode(mode)
            return

        already_waiting_for_power = self._pending_power_on_hvac_mode is not None
        self._pending_power_on_hvac_mode = mode
        self._pending_power_on_hvac_mode_sent = False
        if already_waiting_for_power and self._pending_power_on_requested:
            self.device_logger.info(
                "Updated staged HVAC mode while awaiting Power On confirmation: %s",
                mode,
            )
            return

        try:
            await self.api.turn_on()
        except Exception:
            self._pending_power_on_hvac_mode = None
            self._pending_power_on_hvac_mode_sent = False
            self._pending_power_on_requested = False
            raise
        self._pending_power_on_requested = True
        self.device_logger.info(
            "HVAC mode %s staged until coordinator confirms Power On", mode
        )

    async def _async_process_pending_power_on_hvac_mode(
        self, status: dict[str, Any]
    ) -> bool:
        """Advance one serialized Off -> On -> Mode user sequence.

        Return True while a physical power boundary or the requested mode is still
        awaiting coordinator confirmation.  Eco must not consume stale status in
        that window.
        """

        power = _power(status)

        if self._pending_power_off:
            if power != "off":
                return True
            self._pending_power_off = False
            self.device_logger.info(
                "Pending Power Off confirmed by coordinator"
            )
            if self._pending_power_on_hvac_mode is None:
                return False
            try:
                await self.api.turn_on()
            except Exception as err:
                desired_mode = self._pending_power_on_hvac_mode
                self._pending_power_on_hvac_mode = None
                self._pending_power_on_hvac_mode_sent = False
                self._pending_power_on_requested = False
                self.clear_pending_user_control("hvac_mode")
                self.device_logger.warning(
                    "Power On failed after confirmed Power Off; releasing staged "
                    "HVAC intent: mode=%s error=%s",
                    desired_mode,
                    err,
                )
                if self.eco.enabled:
                    await self.eco.finish_eco(
                        EcoFinishReason.ERROR,
                        detail="Power On failed after confirmed Power Off",
                        restore=False,
                    )
                return True
            self._pending_power_on_requested = True
            self.device_logger.info(
                "Confirmed Power Off; Power On sent for staged HVAC mode %s",
                self._pending_power_on_hvac_mode,
            )
            return True

        desired_mode = self._pending_power_on_hvac_mode
        if desired_mode is None:
            return False

        if power != "on":
            # One Power On has already been sent.  Do not queue another command
            # while Samsung is still crossing the physical power boundary.
            if self._pending_power_on_requested:
                return True
            try:
                await self.api.turn_on()
            except Exception:
                self._pending_power_on_hvac_mode = None
                self._pending_power_on_hvac_mode_sent = False
                self._pending_power_on_requested = False
                raise
            self._pending_power_on_requested = True
            return True

        observed_mode = _mode(status)
        if observed_mode == desired_mode:
            self.device_logger.info(
                "Staged HVAC mode confirmed after Power On: %s", desired_mode
            )
            self._pending_power_on_hvac_mode = None
            self._pending_power_on_hvac_mode_sent = False
            self._pending_power_on_requested = False
            return False

        if not self._pending_power_on_hvac_mode_sent:
            try:
                await self.api.set_ac_mode(desired_mode)
            except Exception as err:
                self._pending_power_on_hvac_mode = None
                self._pending_power_on_hvac_mode_sent = False
                self._pending_power_on_requested = False
                self.clear_pending_user_control("hvac_mode")
                self.device_logger.warning(
                    "Staged HVAC mode command failed after confirmed Power On; "
                    "releasing user intent: mode=%s error=%s",
                    desired_mode,
                    err,
                )
                if self.eco.enabled:
                    await self.eco.finish_eco(
                        EcoFinishReason.ERROR,
                        detail=(
                            "staged HVAC mode command failed after confirmed Power On"
                        ),
                        restore=False,
                    )
                return True

            self._pending_power_on_hvac_mode_sent = True
            self.device_logger.info(
                "Staged HVAC mode sent after confirmed Power On: %s",
                desired_mode,
            )
            return True

        # The one allowed mode command has already been sent.  Keep all Eco and
        # deferred-restore controller actions paused until Samsung confirms it.
        if self.pending_user_control_value("hvac_mode") is not None:
            return True

        self.device_logger.warning(
            "Staged HVAC mode was not confirmed within the user-control window; "
            "releasing intent: expected=%s observed=%s",
            desired_mode,
            observed_mode,
        )
        self._pending_power_on_hvac_mode = None
        self._pending_power_on_hvac_mode_sent = False
        self._pending_power_on_requested = False
        if self.eco.enabled:
            await self.eco.finish_eco(
                EcoFinishReason.ERROR,
                detail="staged HVAC mode was not confirmed",
                restore=False,
            )
        return True

    def begin_pending_user_control(self, field: str, value: str) -> None:
        """Remember an explicit normal user choice until Samsung confirms it.

        This is intent metadata, not coordinator status. It exists so a new Eco
        Original Snapshot can respect a user command that SmartThings has accepted
        but has not yet reflected in the shared status response.
        """

        self._pending_user_controls[field] = _PendingUserControl(
            value=value,
            deadline=time.monotonic() + OPTIMISTIC_UI_CONFIRMATION_TIMEOUT,
        )

    def clear_pending_user_control(self, field: str) -> None:
        """Drop one pending user intent after command failure or consumption."""

        self._pending_user_controls.pop(field, None)

    def pending_user_controls_for_snapshot(self) -> dict[str, str]:
        """Return non-expired user intent that may overlay a new Original Snapshot."""

        self._expire_pending_user_controls()
        return {
            field: pending.value
            for field, pending in self._pending_user_controls.items()
        }

    def consume_pending_user_controls(self, fields: set[str]) -> None:
        """Consume intent once it has been captured by a new Original Snapshot."""

        for field in fields:
            self._pending_user_controls.pop(field, None)

    def pending_user_control_value(self, field: str) -> str | None:
        """Return one non-expired explicit user intent without changing status."""

        self._expire_pending_user_controls()
        pending = self._pending_user_controls.get(field)
        return pending.value if pending is not None else None

    def cancel_queued_eco_activation(self) -> None:
        """Cancel any not-yet-started Eco handoff behind newer user intent."""

        self._queued_eco_after_pending_user_controls = False
        self._queued_eco_pending_user_overrides = {}
        self._queued_eco_pending_user_snapshot_seed = None
        self._queued_eco_after_local_restore = False
        self._queued_eco_snapshot_seed = None
        self._queued_eco_snapshot_overrides = {}

    async def async_enable_eco(self, status: dict[str, Any]) -> bool:
        """Enable Eco without overlapping an older restore owner.

        Recent explicit user choices still overlay the new Original Snapshot.
        If a deferred Eco Pending Restore exists, its desired pre-Eco state is
        transferred into the new cycle instead of first being pushed to Samsung.
        When one Pending-Restore field is already in flight, the new Eco intent
        is armed and waits only for that field's coordinator confirmation.
        """

        if self.frost.enabled:
            self.device_logger.info(
                "Eco selection rejected while Frost Protection is active"
            )
            return False

        snapshot_overrides = self.pending_user_controls_for_snapshot()
        snapshot_seed: EcoRestoreSnapshot | None = None

        if (
            self.local_desired is not None
            and self.local_desired.source == "eco_snapshot"
        ):
            async with self._local_apply_lock:
                if (
                    self.local_desired is not None
                    and self.local_desired.source == "eco_snapshot"
                ):
                    snapshot_seed = self._snapshot_seed_from_local_desired(status)
                    if self._local_in_flight_field is not None:
                        self._queued_eco_after_local_restore = True
                        self._queued_eco_snapshot_seed = snapshot_seed
                        self._queued_eco_snapshot_overrides = dict(snapshot_overrides)
                        accepted = await self.eco.async_arm_for_serial_handoff()
                        self.device_logger.info(
                            "Eco re-enable queued behind Eco Pending Restore field %s",
                            self._local_in_flight_field,
                        )
                        return accepted

                    # No Samsung command is in flight: atomically transfer this
                    # deferred owner into the next Original Snapshot.  Do not
                    # physically restore values only to overwrite them with Eco.
                    self.local_desired = None
                    self._local_apply_sent_at = None
                    self._local_in_flight_field = None
                    self._local_in_flight_expected = None
                    self._pending_local_user_overrides.clear()
                    await self._async_save_local()
                    self.device_logger.info(
                        "Eco Pending Restore transferred into new Eco Original Snapshot"
                    )

        # A normal user command that predates the Eco click owns transport until
        # coordinator feedback has reached that boundary.  Starting Eco now can
        # otherwise produce an A/B race (for example old Vertical arriving after
        # Eco Fixed).  Arm Eco immediately for presentation, but postpone all
        # Samsung Eco commands until the older user intent is confirmed/expired.
        if snapshot_overrides:
            self._queued_eco_after_pending_user_controls = True
            self._queued_eco_pending_user_overrides = dict(snapshot_overrides)
            self._queued_eco_pending_user_snapshot_seed = snapshot_seed
            accepted = await self.eco.async_arm_for_serial_handoff()
            self.device_logger.info(
                "Eco activation queued behind pending user-control confirmation: %s",
                snapshot_overrides,
            )
            return accepted

        if self._pending_power_off:
            accepted = await self.eco.async_arm_while_power_off_pending(
                snapshot_seed=snapshot_seed,
                snapshot_overrides=snapshot_overrides,
            )
        else:
            accepted = await self.eco.async_enable(
                status,
                snapshot_overrides=snapshot_overrides,
                snapshot_seed=snapshot_seed,
            )
        if accepted and self.eco.snapshot is not None and snapshot_overrides:
            self.consume_pending_user_controls(set(snapshot_overrides))
            self.device_logger.info(
                "Eco Original Snapshot consumed pending user intent: %s",
                snapshot_overrides,
            )
        return accepted

    async def _async_process_queued_eco_after_pending_user_controls(
        self, status: dict[str, Any]
    ) -> bool:
        """Start Eco only after older normal user transport has settled."""

        if not self._queued_eco_after_pending_user_controls:
            return False

        self._expire_pending_user_controls()
        if self._pending_user_controls:
            # The pre-Eco user command is still the newest transport owner.
            # Do not let ARMED Eco inspect/act on this intermediate status.
            return True

        overrides = dict(self._queued_eco_pending_user_overrides)
        snapshot_seed = self._queued_eco_pending_user_snapshot_seed
        self._queued_eco_after_pending_user_controls = False
        self._queued_eco_pending_user_overrides = {}
        self._queued_eco_pending_user_snapshot_seed = None
        accepted = await self.eco.async_enable(
            status,
            snapshot_overrides=overrides,
            snapshot_seed=snapshot_seed,
        )
        if accepted:
            self.device_logger.info(
                "Eco activation started after pending user-control boundary; "
                "Original Snapshot preserves: %s",
                overrides,
            )
        return True

    def _snapshot_seed_from_local_desired(
        self, status: dict[str, Any]
    ) -> EcoRestoreSnapshot:
        """Convert deferred Pending Restore ownership into an Eco snapshot."""

        settings = self.local_desired
        assert settings is not None
        mode = settings.target_mode or _mode(status)
        target = settings.target_temperature
        if target is None:
            target = resolve_target_temperature(status, mode)
        fan = settings.fan_mode
        if fan is None:
            value = _attribute_value(status, "airConditionerFanMode", "fanMode")
            fan = value if isinstance(value, str) else None
        oscillation = settings.oscillation_mode
        if oscillation is None:
            value = _attribute_value(status, "fanOscillationMode", "fanOscillationMode")
            oscillation = value if isinstance(value, str) else None
        optional = settings.optional_mode
        if optional is None:
            value = _attribute_value(
                status, "custom.airConditionerOptionalMode", "acOptionalMode"
            )
            optional = value if isinstance(value, str) else None
        return EcoRestoreSnapshot(
            hvac_mode=mode,
            target_temperature=target,
            fan_mode=fan,
            oscillation_mode=oscillation,
            optional_mode=optional,
            sound_enabled=settings.sound_enabled,
            auto_clean_enabled=settings.auto_clean_enabled,
        )

    def _expire_pending_user_controls(self) -> None:
        now = time.monotonic()
        expired = [
            field
            for field, pending in self._pending_user_controls.items()
            if now >= pending.deadline
        ]
        for field in expired:
            self._pending_user_controls.pop(field, None)

    def _confirm_pending_user_controls(self, status: dict[str, Any]) -> None:
        """Clear intent only when normal coordinator status confirms the value."""

        self._expire_pending_user_controls()
        observed = {
            "hvac_mode": _mode(status),
            "fan_mode": _attribute_value(status, "airConditionerFanMode", "fanMode"),
            "oscillation_mode": _attribute_value(
                status, "fanOscillationMode", "fanOscillationMode"
            ),
        }
        confirmed = [
            field
            for field, pending in self._pending_user_controls.items()
            if observed.get(field) == pending.value
        ]
        for field in confirmed:
            self._pending_user_controls.pop(field, None)

    async def async_finish_sleep_off(self) -> bool:
        """Route deliberate Mode Off during Sleep directly to the Eco owner."""

        handled = await self.eco.async_finish_sleep_off()
        if handled:
            self.async_update_listeners()
            # One coordinator refresh is allowed; it remains the single status
            # source. During confirmed Sleep this also transfers Pending Restore
            # to its dedicated deferred Eco restore owner without a second Off.
            await self.async_request_refresh()
        return handled

    async def async_finish_sleep_with_override(
        self, overrides: dict[str, Any]
    ) -> bool:
        """Route a deliberate Sleep user action directly to the Eco owner."""

        handled = await self.eco.async_finish_sleep_with_override(overrides)
        if handled:
            self.async_update_listeners()
            # One coordinator refresh is allowed; it remains the single status
            # source and is not delayed by the outbound command-spacing rule.
            await self.async_request_refresh()
        return handled

    async def async_finish_eco_with_target_override(self, value: float) -> bool:
        """Finish an active non-Sleep Eco cycle with a target overlay."""

        handled = await self.eco.async_finish_with_target_override(float(value))
        if handled:
            self.async_update_listeners()
            await self.async_request_refresh()
        return handled

    @property
    def local_values_active(self) -> bool:
        """Return whether Eco Pending Restore currently owns powered-off values."""

        return (
            self.local_desired is not None
            and self.local_desired.source == "eco_snapshot"
            and (_power(self.data) == "off" or self._local_apply_sent_at is not None)
        )

    async def async_set_local_target_temperature(self, value: float) -> None:
        """Store a local target and forward a live user override when applicable."""

        if self.local_desired is None or self.local_desired.source != "eco_snapshot":
            return
        self.local_desired.update(target_temperature=value)
        await self._async_save_local()
        self.async_update_listeners()
        self.device_logger.info("Eco Pending Restore target temperature set to %.1f °C", value)
        await self._async_forward_local_user_override("target_temperature")

    async def async_set_local_fan_mode(self, value: str) -> None:
        """Store a local fan mode and forward a live user override when applicable."""

        if self.local_desired is None or self.local_desired.source != "eco_snapshot":
            return
        self.local_desired.update(fan_mode=value)
        await self._async_save_local()
        self.async_update_listeners()
        self.device_logger.info("Eco Pending Restore fan mode set to %s", value)
        await self._async_forward_local_user_override("fan_mode")

    async def async_set_local_oscillation_mode(self, value: str) -> None:
        """Store a local oscillation mode and forward a live override when applicable."""

        if self.local_desired is None or self.local_desired.source != "eco_snapshot":
            return
        self.local_desired.update(oscillation_mode=value)
        await self._async_save_local()
        self.async_update_listeners()
        self.device_logger.info("Eco Pending Restore oscillation mode set to %s", value)
        await self._async_forward_local_user_override("oscillation_mode")

    async def _async_forward_local_user_override(self, field: str) -> None:
        """Send one explicit user override without restarting Eco Pending Restore.

        If Samsung is temporarily still reported Off, remember only the field
        name. The next fresh coordinator status that confirms Power On sends the
        latest stored value exactly once. This is delayed first delivery of the
        user's command, not a retry or a full restore.
        """

        if (
            self.local_desired is None
            or self.local_desired.source != "eco_snapshot"
            or self._local_apply_sent_at is None
        ):
            return

        if _power(self.data) != "on" or self._local_in_flight_field is not None:
            # One transport step already owns the device, or Power is not yet
            # confirmed On. Keep only the latest user value in local_desired and
            # deliver this field after the current confirmed boundary.
            self._pending_local_user_overrides.add(field)
            return

        self._pending_local_user_overrides.discard(field)
        await self._async_send_local_user_override(field, status=self.data)
        self._local_in_flight_field = field
        self._local_in_flight_expected = self._local_expected_value(field)

    async def _async_send_local_user_override(
        self, field: str, *, status: dict[str, Any] | None = None
    ) -> None:
        """Send one changed local field to Samsung exactly once."""

        settings = self.local_desired
        if settings is None:
            return

        if field == "target_temperature":
            value = settings.target_temperature
            if value is None:
                return
            mode = _mode(status if status is not None else self.data)
            if mode not in SETPOINT_HVAC_MODES:
                mode = settings.target_mode
            if mode not in SETPOINT_HVAC_MODES:
                return
            await self.api.set_target_temperature(mode, value)
            self.device_logger.info(
                "Eco Pending Restore user override sent: target_temperature=%.1f",
                value,
            )
            return

        if field == "fan_mode":
            value = settings.fan_mode
            if value is None:
                return
            await self.api.set_fan_speed(value)
            self.device_logger.info("Eco Pending Restore user override sent: fan_mode=%s", value)
            return

        if field == "oscillation_mode":
            value = settings.oscillation_mode
            if value is None:
                return
            await self.api.set_oscillation_mode(value)
            self.device_logger.info(
                "Eco Pending Restore user override sent: oscillation_mode=%s",
                value,
            )

    async def _async_send_pending_local_user_overrides(
        self, status: dict[str, Any]
    ) -> None:
        """Send delayed user overrides once after fresh Power-On confirmation."""

        if (
            not self._pending_local_user_overrides
            or self.local_desired is None
            or self.local_desired.source != "eco_snapshot"
        ):
            return

        field = next(
            (
                candidate
                for candidate in ("target_temperature", "fan_mode", "oscillation_mode")
                if candidate in self._pending_local_user_overrides
            ),
            None,
        )
        if field is None:
            return
        self._pending_local_user_overrides.discard(field)
        try:
            await self._async_send_local_user_override(field, status=status)
            self._local_in_flight_field = field
            self._local_in_flight_expected = self._local_expected_value(field)
        except Exception as err:
            # This was the first delivery attempt of an explicit user command.
            # Never repeat it automatically; later queued fields remain for a
            # later confirmed coordinator cycle rather than forming a burst.
            self.device_logger.warning(
                "Eco Pending Restore user override failed; no automatic retry "
                "will be sent: field=%s error=%s",
                field,
                err,
            )

    def _local_expected_value(self, field: str) -> Any:
        settings = self.local_desired
        if settings is None:
            return None
        if field == "target_temperature":
            return settings.target_temperature
        if field == "fan_mode":
            return settings.fan_mode
        if field == "oscillation_mode":
            return settings.oscillation_mode
        if field == "optional_mode":
            return settings.optional_mode
        if field == "sound_enabled":
            return settings.sound_enabled
        if field == "auto_clean_enabled":
            return settings.auto_clean_enabled
        return None

    def _local_in_flight_confirmed(self, status: dict[str, Any]) -> bool:
        field = self._local_in_flight_field
        expected = self._local_in_flight_expected
        if field is None:
            return True
        if field == "target_temperature":
            actual = resolve_target_temperature(status, _mode(status))
            return (
                isinstance(expected, (int, float))
                and isinstance(actual, (int, float))
                and abs(float(actual) - float(expected)) <= 0.26
            )
        if field == "fan_mode":
            return _attribute_value(status, "airConditionerFanMode", "fanMode") == expected
        if field == "oscillation_mode":
            return _attribute_value(status, "fanOscillationMode", "fanOscillationMode") == expected
        if field == "optional_mode":
            return (
                _attribute_value(
                    status,
                    "custom.airConditionerOptionalMode",
                    "acOptionalMode",
                )
                == expected
            )
        if field == "sound_enabled":
            volume = _attribute_value(status, "audioVolume", "volume")
            actual = bool(volume > 0) if isinstance(volume, (int, float)) else None
            return actual == expected
        if field == "auto_clean_enabled":
            value = _attribute_value(status, "custom.autoCleaningMode", "autoCleaningMode")
            actual = value == "on" if value in {"on", "off"} else None
            return actual == expected
        return False

    async def _async_reconcile_pending_restore_off_safe_settings(
        self, status: dict[str, Any]
    ) -> None:
        """Restore Sound/Auto Clean immediately for Sleep -> Mode Off only.

        These settings are safe and meaningful while the AC remains physically
        Off. They are sent only when the fresh coordinator status differs. A
        failure is not retried here; the desired value remains in Pending
        Restore and may still be reconciled once on the next physical Power On.
        """

        settings = self.local_desired
        if (
            settings is None
            or settings.source != "eco_snapshot"
            or _power(status) != "off"
        ):
            return

        if settings.auto_clean_enabled is not None:
            observed_auto_clean = _attribute_value(
                status, "custom.autoCleaningMode", "autoCleaningMode"
            )
            expected_auto_clean = "on" if settings.auto_clean_enabled else "off"
            if observed_auto_clean != expected_auto_clean:
                try:
                    await self.api.set_auto_cleaning_mode(expected_auto_clean)
                    self.device_logger.info(
                        "Eco Sleep Off immediate restore sent: auto_clean_enabled=%s",
                        settings.auto_clean_enabled,
                    )
                except Exception as err:
                    self.device_logger.warning(
                        "Eco Sleep Off immediate restore failed; keeping value for "
                        "next Power On: field=auto_clean_enabled error=%s",
                        err,
                    )

        if settings.sound_enabled is not None:
            observed_volume = _attribute_value(status, "audioVolume", "volume")
            observed_sound = (
                bool(observed_volume > 0)
                if isinstance(observed_volume, (int, float))
                else None
            )
            if observed_sound != settings.sound_enabled:
                try:
                    await self.api.set_volume(100 if settings.sound_enabled else 0)
                    self.device_logger.info(
                        "Eco Sleep Off immediate restore sent: sound_enabled=%s",
                        settings.sound_enabled,
                    )
                except Exception as err:
                    self.device_logger.warning(
                        "Eco Sleep Off immediate restore failed; keeping value for "
                        "next Power On: field=sound_enabled error=%s",
                        err,
                    )

    async def async_apply_local_desired_settings(
        self,
        *,
        target_mode: str | None = None,
        status: dict[str, Any] | None = None,
    ) -> None:
        """Advance deferred Eco Pending Restore by at most one command.

        The owner is deliberately serial: one necessary field is sent, then no
        next Samsung command is allowed until fresh coordinator status confirms
        that field.  This prevents the former Pending-Restore command burst.
        """

        confirmed_status = status if status is not None else self.data
        if (
            self.local_desired is None
            or self.local_desired.source != "eco_snapshot"
            or _power(confirmed_status) != "on"
            or self._local_apply_lock.locked()
        ):
            return

        async with self._local_apply_lock:
            settings = self.local_desired
            if (
                settings is None
                or settings.source != "eco_snapshot"
                or _power(confirmed_status) != "on"
            ):
                return

            if self._local_apply_sent_at is None:
                self._local_apply_sent_at = time.monotonic()
                self.device_logger.info(
                    "Applying Eco Pending Restore settings serially: %s",
                    settings.as_dict(),
                )

            mismatches = self._local_settings_mismatches(confirmed_status)
            if self._local_in_flight_field is not None:
                if not self._local_in_flight_confirmed(confirmed_status):
                    return
                self.device_logger.info(
                    "Eco Pending Restore step confirmed: %s",
                    self._local_in_flight_field,
                )
                self._local_in_flight_field = None
                self._local_in_flight_expected = None

            # A newly selected Eco cycle may take ownership only at this confirmed
            # boundary; remaining old restore differences are then not sent.
            if self._queued_eco_after_local_restore:
                return

            mismatches = self._local_settings_mismatches(confirmed_status)
            if not mismatches:
                return

            mode = target_mode or _mode(confirmed_status) or settings.target_mode
            field: str | None = None
            if "target_temperature" in mismatches and settings.target_temperature is not None and mode in SETPOINT_HVAC_MODES:
                await self.api.set_target_temperature(mode, settings.target_temperature)
                field = "target_temperature"
            elif "fan_mode" in mismatches and settings.fan_mode is not None:
                await self.api.set_fan_speed(settings.fan_mode)
                field = "fan_mode"
            elif "oscillation_mode" in mismatches and settings.oscillation_mode is not None:
                await self.api.set_oscillation_mode(settings.oscillation_mode)
                field = "oscillation_mode"
            elif "optional_mode" in mismatches and settings.optional_mode is not None:
                await self.api.set_optional_mode(settings.optional_mode)
                field = "optional_mode"
            elif "sound_enabled" in mismatches and settings.sound_enabled is not None:
                await self.api.set_volume(100 if settings.sound_enabled else 0)
                field = "sound_enabled"
            elif "auto_clean_enabled" in mismatches and settings.auto_clean_enabled is not None:
                await self.api.set_auto_cleaning_mode(
                    "on" if settings.auto_clean_enabled else "off"
                )
                field = "auto_clean_enabled"

            if field is not None:
                self._local_in_flight_field = field
                self._local_in_flight_expected = self._local_expected_value(field)
                self.device_logger.info(
                    "Eco Pending Restore step sent: %s", field
                )

    async def _async_process_queued_eco_after_local_restore(
        self, status: dict[str, Any]
    ) -> bool:
        """Hand Pending Restore to a queued new Eco cycle at a safe boundary."""

        if not self._queued_eco_after_local_restore:
            return False

        if self.local_desired is None or self.local_desired.source != "eco_snapshot":
            self._queued_eco_after_local_restore = False
            self._queued_eco_snapshot_seed = None
            self._queued_eco_snapshot_overrides = {}
            return False

        mismatches = self._local_settings_mismatches(status)
        if self._local_in_flight_field is not None:
            if not self._local_in_flight_confirmed(status):
                if (
                    self._local_apply_sent_at is not None
                    and time.monotonic() - self._local_apply_sent_at > _LOCAL_APPLY_TIMEOUT
                ):
                    field = self._local_in_flight_field
                    self.device_logger.warning(
                        "Eco re-enable handoff timed out waiting for Pending Restore field %s; releasing both owners",
                        field,
                    )
                    self.local_desired = None
                    self._local_apply_sent_at = None
                    self._local_in_flight_field = None
                    self._local_in_flight_expected = None
                    self._pending_local_user_overrides.clear()
                    self._queued_eco_after_local_restore = False
                    self._queued_eco_snapshot_seed = None
                    self._queued_eco_snapshot_overrides = {}
                    await self._async_save_local()
                    await self.eco.finish_eco(
                        EcoFinishReason.ERROR,
                        detail="serial Pending Restore to Eco handoff timed out",
                        restore=False,
                    )
                return True
            self.device_logger.info(
                "Eco Pending Restore step confirmed before Eco handoff: %s",
                self._local_in_flight_field,
            )
            self._local_in_flight_field = None
            self._local_in_flight_expected = None

        seed = self._queued_eco_snapshot_seed or self._snapshot_seed_from_local_desired(status)
        overrides = dict(self._queued_eco_snapshot_overrides)
        self.local_desired = None
        self._local_apply_sent_at = None
        self._local_in_flight_field = None
        self._local_in_flight_expected = None
        self._pending_local_user_overrides.clear()
        self._queued_eco_after_local_restore = False
        self._queued_eco_snapshot_seed = None
        self._queued_eco_snapshot_overrides = {}
        await self._async_save_local()
        self.device_logger.info(
            "Eco Pending Restore handed off to queued new Eco cycle; remaining old restore commands cancelled"
        )
        accepted = await self.eco.async_enable(
            status,
            snapshot_overrides=overrides,
            snapshot_seed=seed,
        )
        if accepted and self.eco.snapshot is not None and overrides:
            self.consume_pending_user_controls(set(overrides))
        return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch one shared status response from Samsung."""

        try:
            status = await self.api.get_status()
        except Exception as err:
            raise UpdateFailed(f"Samsung AC status update failed: {err}") from err

        if not isinstance(status, dict):
            raise UpdateFailed("Samsung AC returned an invalid status response")
        error = status.get("error")
        if error is not None:
            code = error.get("code", "unknown")
            message = error.get("message", "Unknown SmartThings error")
            raise UpdateFailed(f"SmartThings status error {code}: {message}")
        if "components" not in status:
            raise UpdateFailed("Samsung AC status response has no components")

        self.debug_tracker.process(status)
        self._confirm_pending_user_controls(status)

        # A normal user Mode selection made while the physical AC was Off is
        # deliberately two-stage.  Do not let Eco or Pending Restore consume a
        # status that confirms only Power On but not yet the requested mode.
        if await self._async_process_pending_power_on_hvac_mode(status):
            return status

        # A new Eco selection waiting behind one already-sent deferred restore
        # field must be resolved before Eco sees this status; otherwise ARMED Eco
        # could activate while the old owner still has transport in flight.
        if await self._async_process_queued_eco_after_local_restore(status):
            return status

        # A newly selected Eco preset must not overtake a still-pending normal
        # user command captured as its future Original Snapshot.
        if await self._async_process_queued_eco_after_pending_user_controls(status):
            return status

        await self.eco.async_process_status(status)

        # Eco and Frost are independent simulated presets and may never own the
        # same physical AC simultaneously. Existing Eco/deferred restore has
        # priority if persisted state from an older/reloaded session conflicts.
        if self.frost.enabled and (self.eco.enabled or self.local_values_active):
            await self.frost.async_disable(
                reason="Eco or Eco Pending Restore owns the air conditioner"
            )
        await self.frost.async_process_status(status)

        current_power = _power(status)

        # The only deferred restore owner is the explicit Eco USER_OFF /
        # Pending Restore exception. Transfer the immutable Original Snapshot
        # as soon as Eco has finished. This also covers a restart where the AC
        # is already On before the first fresh coordinator status arrives.
        if self.eco.pending_restore is not None:
            snapshot, immediate_off_safe = await self.eco.async_take_pending_restore()
            if snapshot is not None:
                self.local_desired = LocalDesiredSettings(
                    target_temperature=snapshot.target_temperature,
                    fan_mode=snapshot.fan_mode,
                    oscillation_mode=snapshot.oscillation_mode,
                    target_mode=snapshot.hvac_mode,
                    optional_mode=snapshot.optional_mode,
                    sound_enabled=snapshot.sound_enabled,
                    auto_clean_enabled=snapshot.auto_clean_enabled,
                    source="eco_snapshot",
                )
                self._local_apply_sent_at = None
                self._local_in_flight_field = None
                self._local_in_flight_expected = None
                self._pending_local_user_overrides.clear()
                await self._async_save_local()
                self.device_logger.info(
                    "Eco Pending Restore settings created from Original Snapshot: %s",
                    self.local_desired.as_dict(),
                )
                if immediate_off_safe and current_power == "off":
                    await self._async_reconcile_pending_restore_off_safe_settings(
                        status
                    )

        if (
            current_power == "on"
            and self.local_desired is not None
            and self.local_desired.source == "eco_snapshot"
        ):
            try:
                await self.async_apply_local_desired_settings(
                    target_mode=_mode(status),
                    status=status,
                )
            except Exception as err:
                # A failed serial restore step is terminal for this deferred
                # owner.  Never retry it automatically or send later fields.
                self.device_logger.warning(
                    "Eco Pending Restore serial step failed; releasing local "
                    "ownership with no automatic retry: %s",
                    err,
                )
                self.local_desired = None
                self._local_apply_sent_at = None
                self._local_in_flight_field = None
                self._local_in_flight_expected = None
                self._pending_local_user_overrides.clear()
                await self._async_save_local()
                return status

            # A user may have changed Target/Fan/Oscillation while Samsung still
            # reported Off during the already-started restore. As soon as this
            # fresh status confirms Power On, deliver those latest explicit
            # choices once before evaluating confirmation.
            if (
                self._local_apply_sent_at is not None
                and self._local_in_flight_field is None
            ):
                await self._async_send_pending_local_user_overrides(status)

            if self._local_settings_match(status):
                self.device_logger.info(
                    "Eco Pending Restore confirmed; SmartThings owns values again"
                )
                self.local_desired = None
                self._local_apply_sent_at = None
                self._local_in_flight_field = None
                self._local_in_flight_expected = None
                self._pending_local_user_overrides.clear()
                await self._async_save_local()
            elif (
                self._local_apply_sent_at is not None
                and time.monotonic() - self._local_apply_sent_at > _LOCAL_APPLY_TIMEOUT
            ):
                mismatches = self._local_settings_mismatches(status)
                self.device_logger.warning(
                    "Eco Pending Restore was not fully confirmed within %.0f "
                    "seconds; releasing local ownership and accepting SmartThings "
                    "state; mismatches=%s",
                    _LOCAL_APPLY_TIMEOUT,
                    mismatches,
                )
                self.local_desired = None
                self._local_apply_sent_at = None
                self._local_in_flight_field = None
                self._local_in_flight_expected = None
                self._pending_local_user_overrides.clear()
                await self._async_save_local()

        return status

    def _local_settings_match(self, status: dict[str, Any]) -> bool:
        return not self._local_settings_mismatches(status)

    def _local_settings_mismatches(
        self, status: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Return remaining Eco Pending Restore differences for diagnostics."""

        settings = self.local_desired
        if settings is None:
            return {}

        mismatches: dict[str, dict[str, Any]] = {}
        mode = _mode(status)
        observed_target = resolve_target_temperature(status, mode)
        if settings.target_temperature is not None:
            if (
                observed_target is None
                or abs(observed_target - settings.target_temperature) > 0.26
            ):
                mismatches["target_temperature"] = {
                    "expected": settings.target_temperature,
                    "observed": observed_target,
                }

        observed_fan = _attribute_value(status, "airConditionerFanMode", "fanMode")
        if settings.fan_mode is not None and observed_fan != settings.fan_mode:
            mismatches["fan_mode"] = {
                "expected": settings.fan_mode,
                "observed": observed_fan,
            }

        observed_osc = _attribute_value(
            status, "fanOscillationMode", "fanOscillationMode"
        )
        if (
            settings.oscillation_mode is not None
            and observed_osc != settings.oscillation_mode
        ):
            mismatches["oscillation_mode"] = {
                "expected": settings.oscillation_mode,
                "observed": observed_osc,
            }

        observed_optional = _attribute_value(
            status,
            "custom.airConditionerOptionalMode",
            "acOptionalMode",
        )
        if (
            settings.optional_mode is not None
            and observed_optional != settings.optional_mode
        ):
            mismatches["optional_mode"] = {
                "expected": settings.optional_mode,
                "observed": observed_optional,
            }

        if settings.sound_enabled is not None:
            observed_volume = _attribute_value(status, "audioVolume", "volume")
            observed_sound = (
                bool(observed_volume > 0)
                if isinstance(observed_volume, (int, float))
                else None
            )
            if observed_sound != settings.sound_enabled:
                mismatches["sound_enabled"] = {
                    "expected": settings.sound_enabled,
                    "observed": observed_sound,
                }

        if settings.auto_clean_enabled is not None:
            observed_auto_clean = _attribute_value(
                status, "custom.autoCleaningMode", "autoCleaningMode"
            )
            expected_auto_clean = "on" if settings.auto_clean_enabled else "off"
            if observed_auto_clean != expected_auto_clean:
                mismatches["auto_clean_enabled"] = {
                    "expected": settings.auto_clean_enabled,
                    "observed": (
                        observed_auto_clean == "on"
                        if observed_auto_clean in {"on", "off"}
                        else None
                    ),
                }

        return mismatches

    async def _async_save_local(self) -> None:
        await self._local_store.async_save(
            {
                "local_desired": (
                    self.local_desired.as_dict()
                    if self.local_desired is not None
                    and self.local_desired.source == "eco_snapshot"
                    else None
                ),
            }
        )
