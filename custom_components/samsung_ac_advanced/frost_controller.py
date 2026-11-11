"""Independent Frost Protection controller for Samsung AC Advanced."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import SamsungACAPI
from .const import (
    CONF_ECO_TEMPERATURE_SENSOR,
    FROST_HYSTERESIS,
    FROST_PHYSICAL_TARGET_TEMPERATURE,
    FROST_TARGET_TEMPERATURE_DEFAULT,
    FROST_TARGET_TEMPERATURE_MAX,
    FROST_TARGET_TEMPERATURE_MIN,
    FROST_TARGET_TEMPERATURE_STEP,
)
from .device_logger import SamsungACDeviceLoggerAdapter, SamsungACRuntimeLogState
from .setpoint import resolve_target_temperature

_LOGGER = logging.getLogger(__name__)
_STORAGE_VERSION = 1
_SEQUENCE_TIMEOUT = 120.0


class FrostState(StrEnum):
    """Logical Frost Protection states."""

    DISABLED = "disabled"
    ARMED_OFF = "armed_off"
    HEATING = "heating"
    SUSPENDED = "suspended"


@dataclass(slots=True)
class FrostTemperatureReading:
    """Validated Frost Protection room-temperature reading."""

    value: float | None
    updated_at: datetime | None
    source_entity_id: str | None
    valid: bool
    reason: str | None = None


@dataclass(slots=True)
class FrostSequence:
    """One controller-owned desired-state sequence."""

    purpose: str
    desired: dict[str, Any]
    in_flight_field: str | None = None
    in_flight_expected: Any = None
    sent_at: float | None = None


def _attribute_value(
    data: dict[str, Any] | None,
    capability: str,
    attribute: str,
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


def _attribute_timestamp(
    data: dict[str, Any] | None,
    capability: str,
    attribute: str,
) -> datetime | None:
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
    raw = attribute_data.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return dt_util.parse_datetime(raw)
    except (TypeError, ValueError):
        return None


def _power(data: dict[str, Any] | None) -> str | None:
    value = _attribute_value(data, "switch", "switch")
    return value if isinstance(value, str) else None


def _mode(data: dict[str, Any] | None) -> str | None:
    value = _attribute_value(data, "airConditionerMode", "airConditionerMode")
    return value if isinstance(value, str) else None


def _normalize_frost_target(value: float) -> float:
    stepped = round(float(value) / FROST_TARGET_TEMPERATURE_STEP) * FROST_TARGET_TEMPERATURE_STEP
    return max(FROST_TARGET_TEMPERATURE_MIN, min(FROST_TARGET_TEMPERATURE_MAX, stepped))


class SamsungACFrostController:
    """Small independent controller for Frost Protection.

    Frost Protection deliberately does not share Eco snapshots, restore state or
    profiles. It owns only the fields required for its fixed heating profile:
    Heat + 16 °C + Fan Auto + Fixed oscillation + Optional Mode Off. Sound,
    display lighting and Auto Clean are never changed by this controller.
    """

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
        self.state = FrostState.DISABLED
        self.enabled = False
        self.target_temperature = FROST_TARGET_TEMPERATURE_DEFAULT
        self.temperature = FrostTemperatureReading(
            None, None, None, False, "not_evaluated"
        )
        self._sequence: FrostSequence | None = None
        self._latest_status: dict[str, Any] = {}
        self._command_lock = asyncio.Lock()
        self._confirmation_refresh_callback: Callable[[], None] | None = None
        self._disable_requested_reason: str | None = None
        self._restored_session_pending = False
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORAGE_VERSION, f"{entry.domain}.{entry.entry_id}.frost"
        )

    @property
    def external_temperature_sensor(self) -> str | None:
        """Use the integration's configured alternative room sensor."""

        value = self.entry.options.get(CONF_ECO_TEMPERATURE_SENSOR)
        return value if isinstance(value, str) and value else None

    @property
    def temperature_source_type(self) -> str:
        return "external" if self.temperature.source_entity_id else "internal"

    @property
    def sequence_active(self) -> bool:
        return self._sequence is not None

    def set_confirmation_refresh_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        self._confirmation_refresh_callback = callback

    def _request_confirmation_refresh(self) -> None:
        if self._confirmation_refresh_callback is not None:
            self._confirmation_refresh_callback()

    async def async_initialize(self) -> None:
        """Load the Frost target and persisted active state."""

        stored = await self._store.async_load() or {}
        raw_target = stored.get("target_temperature", FROST_TARGET_TEMPERATURE_DEFAULT)
        try:
            target = float(raw_target)
        except (TypeError, ValueError):
            target = FROST_TARGET_TEMPERATURE_DEFAULT
        self.target_temperature = _normalize_frost_target(target)
        self.enabled = bool(stored.get("enabled", False))
        self._restored_session_pending = self.enabled
        self.state = FrostState.ARMED_OFF if self.enabled else FrostState.DISABLED

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "target_temperature": self.target_temperature,
                "enabled": self.enabled,
            }
        )

    def available_for_context(
        self,
        status: dict[str, Any] | None,
        *,
        eco_enabled: bool = False,
        local_restore_active: bool = False,
    ) -> bool:
        """Return whether Frost Protection may be selected in this context."""

        if self.enabled:
            return True
        if eco_enabled or local_restore_active:
            return False
        power = _power(status)
        return power == "off" or (power == "on" and _mode(status) == "heat")

    async def async_enable(self, status: dict[str, Any]) -> bool:
        """Enable Frost Protection only from physical Off or Heat."""

        async with self._command_lock:
            power = _power(status)
            mode = _mode(status)
            if power != "off" and not (power == "on" and mode == "heat"):
                self.logger.info(
                    "Frost Protection selection rejected in HVAC mode %r", mode
                )
                return False

            self._disable_requested_reason = None
            self._restored_session_pending = False
            self.enabled = True
            self._latest_status = status
            self.temperature = self._temperature_reading(status)
            self._sequence = None
            self.state = FrostState.HEATING if power == "on" else FrostState.ARMED_OFF
            await self._async_save()
            self.logger.info(
                "Frost Protection enabled: target %.1f °C, fixed hysteresis %.1f °C, "
                "profile Heat/16 °C/Fan Auto/Fixed/Optional None; temperature source=%s",
                self.target_temperature,
                FROST_HYSTERESIS,
                self.temperature_source_type,
            )
            await self._async_evaluate(status, activation=True)
            return True

    async def async_disable(self, *, reason: str) -> None:
        """Release Frost ownership; newest user intent preempts unsent Frost work."""

        self._disable_requested_reason = reason
        async with self._command_lock:
            await self._async_disable_unlocked(reason=reason)

    async def _async_disable_unlocked(self, *, reason: str) -> None:
        """Release Frost ownership while the controller lock is already owned."""

        if not self.enabled:
            self._disable_requested_reason = None
            return
        self.enabled = False
        self.state = FrostState.DISABLED
        self._sequence = None
        self._disable_requested_reason = None
        self._restored_session_pending = False
        await self._async_save()
        self.logger.info("Frost Protection disabled: %s", reason)

    async def async_set_target_temperature(self, value: float) -> None:
        """Set the user Frost threshold and re-evaluate at a safe boundary."""

        new_value = _normalize_frost_target(value)
        old_value = self.target_temperature
        self.target_temperature = new_value
        await self._async_save()
        self.logger.info(
            "Frost Protection target changed: %.1f -> %.1f °C",
            old_value,
            new_value,
        )
        if not self.enabled:
            return
        async with self._command_lock:
            self.refresh_temperature()
            if self._sequence is not None and self._sequence.in_flight_field is not None:
                self.logger.info(
                    "Frost Protection target update queued behind current controller-owned step"
                )
                return
            await self._async_evaluate(self._latest_status)

    def refresh_temperature(self) -> None:
        """Refresh the preferred external sensor with internal fallback."""

        self.temperature = self._temperature_reading(self._latest_status)

    async def async_evaluate_external_temperature_change(self) -> None:
        """React to a local external-sensor change without SmartThings polling."""

        if not self.enabled:
            return
        async with self._command_lock:
            self.refresh_temperature()
            if self._sequence is not None:
                return
            await self._async_evaluate(self._latest_status)

    async def async_request_heat(self, status: dict[str, Any]) -> None:
        """Keep Frost enabled while an explicit compatible Heat selection is made."""

        if not self.enabled:
            return
        async with self._command_lock:
            self._latest_status = status
            await self._start_sequence("manual_heat", self._heating_desired(), status)

    async def async_request_off(self, status: dict[str, Any]) -> None:
        """Keep Frost enabled while an explicit compatible Off selection is made."""

        if not self.enabled:
            return
        async with self._command_lock:
            self._latest_status = status
            await self._start_sequence("manual_off", {"power": "off"}, status)

    async def async_process_status(self, status: dict[str, Any]) -> None:
        """Process one confirmed Samsung status from the shared coordinator."""

        self._latest_status = status
        self.temperature = self._temperature_reading(status)
        if not self.enabled:
            return

        async with self._command_lock:
            if self._sequence is not None:
                completed = await self._advance_sequence(status)
                if not completed or not self.enabled:
                    return

            if self._restored_session_pending:
                self._restored_session_pending = False
                power = _power(status)
                mode = _mode(status)
                if power == "on" and mode != "heat":
                    await self._async_disable_unlocked(
                        reason=f"persisted Frost session found incompatible HVAC mode {mode!r}"
                    )
                    return
                self.logger.info(
                    "Frost Protection restored after reload; validating current Heat/Off state"
                )
                if power == "on":
                    await self._start_sequence(
                        "restore", self._heating_desired(), status
                    )
                    if self._sequence is not None:
                        return
                await self._async_evaluate(status)
                return

            # With no Frost command in flight, changes to fields owned by the
            # fixed Frost profile are considered external/user overrides rather
            # than something the controller should fight.
            if _power(status) == "on":
                mismatches = self._heating_profile_mismatches(status)
                if mismatches:
                    await self._async_disable_unlocked(
                        reason=f"managed setting changed outside Frost Protection: {mismatches}"
                    )
                    return
                self.state = FrostState.HEATING
            elif _power(status) == "off":
                self.state = FrostState.ARMED_OFF
            else:
                self.state = FrostState.SUSPENDED
                return

            await self._async_evaluate(status)

    async def _async_evaluate(
        self,
        status: dict[str, Any],
        *,
        activation: bool = False,
    ) -> None:
        """Apply the two-threshold Frost hysteresis policy."""

        if not self.enabled or self._sequence is not None:
            return
        reading = self.temperature
        if not reading.valid or reading.value is None:
            self.state = FrostState.SUSPENDED
            if activation:
                self.logger.warning(
                    "Frost Protection armed but no usable room temperature is available"
                )
            return

        room = reading.value
        power = _power(status)
        if power == "off":
            self.state = FrostState.ARMED_OFF
            if room <= self.target_temperature:
                self.logger.info(
                    "Frost Protection lower threshold reached: room=%.1f °C target=%.1f °C; heating required",
                    room,
                    self.target_temperature,
                )
                await self._start_sequence("heat", self._heating_desired(), status)
            return

        if power == "on" and _mode(status) == "heat":
            self.state = FrostState.HEATING
            upper = self.target_temperature + FROST_HYSTERESIS
            if room >= upper:
                self.logger.info(
                    "Frost Protection upper threshold reached: room=%.1f °C threshold=%.1f °C; physical Off required",
                    room,
                    upper,
                )
                await self._start_sequence("rest", {"power": "off"}, status)
            elif activation:
                # Selecting Frost while already heating also establishes its fixed
                # profile, even when the room is inside the hysteresis band.
                await self._start_sequence("heat", self._heating_desired(), status)
            return

        # Active power in a non-Heat mode is incompatible. During activation the
        # selection was already rejected; during runtime this represents a newer
        # external/user mode choice and Frost must stand down.
        if power == "on":
            await self._async_disable_unlocked(
                reason=f"incompatible HVAC mode selected: {_mode(status)!r}"
            )

    def _heating_desired(self) -> dict[str, Any]:
        return {
            "power": "on",
            "hvac_mode": "heat",
            "target_temperature": FROST_PHYSICAL_TARGET_TEMPERATURE,
            "fan_mode": "auto",
            "oscillation_mode": "fixed",
            "optional_mode": "off",
        }

    def _observed(self, status: dict[str, Any]) -> dict[str, Any]:
        return {
            "power": _power(status),
            "hvac_mode": _mode(status),
            "target_temperature": resolve_target_temperature(status, "heat"),
            "fan_mode": _attribute_value(status, "airConditionerFanMode", "fanMode"),
            "oscillation_mode": _attribute_value(
                status, "fanOscillationMode", "fanOscillationMode"
            ),
            "optional_mode": _attribute_value(
                status, "custom.airConditionerOptionalMode", "acOptionalMode"
            ),
        }

    @staticmethod
    def _matches(field: str, expected: Any, observed: Any) -> bool:
        if field == "target_temperature":
            if not isinstance(expected, (int, float)) or not isinstance(
                observed, (int, float)
            ):
                return False
            return abs(float(expected) - float(observed)) <= 0.26
        return observed == expected

    def _sequence_mismatches(
        self, status: dict[str, Any], desired: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        observed = self._observed(status)
        return {
            field: {"expected": expected, "observed": observed.get(field)}
            for field, expected in desired.items()
            if not self._matches(field, expected, observed.get(field))
        }

    def _heating_profile_mismatches(
        self, status: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        return self._sequence_mismatches(status, self._heating_desired())

    async def _start_sequence(
        self,
        purpose: str,
        desired: dict[str, Any],
        status: dict[str, Any],
    ) -> None:
        if not self.enabled:
            return
        self._sequence = FrostSequence(purpose=purpose, desired=dict(desired))
        await self._advance_sequence(status)

    def _thermal_destination(self, status: dict[str, Any]) -> str | None:
        """Return a definite newest hysteresis destination, if one exists."""

        if not self.temperature.valid or self.temperature.value is None:
            return None
        room = self.temperature.value
        if room <= self.target_temperature:
            return "heat"
        if room >= self.target_temperature + FROST_HYSTERESIS:
            return "rest"
        return None

    async def _advance_sequence(self, status: dict[str, Any]) -> bool:
        """Confirm one Frost step and send at most one next command.

        Return True when the sequence reached a confirmed end boundary.
        """

        sequence = self._sequence
        if sequence is None:
            return True

        if sequence.in_flight_field is not None:
            observed = self._observed(status).get(sequence.in_flight_field)
            if self._matches(
                sequence.in_flight_field,
                sequence.in_flight_expected,
                observed,
            ):
                self.logger.info(
                    "Frost Protection %s step confirmed: %s",
                    sequence.purpose,
                    sequence.in_flight_field,
                )
                sequence.in_flight_field = None
                sequence.in_flight_expected = None
                sequence.sent_at = None
            elif (
                sequence.sent_at is not None
                and time.monotonic() - sequence.sent_at >= _SEQUENCE_TIMEOUT
            ):
                self.logger.warning(
                    "Frost Protection %s step timed out: field=%s expected=%r observed=%r; releasing Frost ownership",
                    sequence.purpose,
                    sequence.in_flight_field,
                    sequence.in_flight_expected,
                    observed,
                )
                self._sequence = None
                self.enabled = False
                self.state = FrostState.DISABLED
                await self._async_save()
                return False
            else:
                return False

        # A target change while a command was in flight may reverse the thermal
        # destination. The already-sent command reached its confirmation boundary;
        # cancel only the unsent remainder and follow the newest threshold intent.
        destination = self._thermal_destination(status)
        if sequence.purpose in {"heat", "rest"} and destination is not None:
            if destination != sequence.purpose:
                self.logger.info(
                    "Frost Protection destination changed at confirmed boundary: %s -> %s",
                    sequence.purpose,
                    destination,
                )
                sequence.purpose = destination
                sequence.desired = (
                    self._heating_desired() if destination == "heat" else {"power": "off"}
                )

        mismatches = self._sequence_mismatches(status, sequence.desired)
        if not mismatches:
            purpose = sequence.purpose
            self._sequence = None
            self.state = (
                FrostState.ARMED_OFF
                if _power(status) == "off"
                else FrostState.HEATING
            )
            self.logger.info(
                "Frost Protection %s confirmed: room=%s °C target=%.1f °C",
                purpose,
                f"{self.temperature.value:.1f}" if self.temperature.value is not None else "unknown",
                self.target_temperature,
            )
            await self._async_save()
            return True

        field = next(
            (
                candidate
                for candidate in (
                    "power",
                    "hvac_mode",
                    "target_temperature",
                    "fan_mode",
                    "oscillation_mode",
                    "optional_mode",
                )
                if candidate in mismatches
            ),
            None,
        )
        if field is None:
            return False

        if self._disable_requested_reason is not None:
            await self._async_disable_unlocked(reason=self._disable_requested_reason)
            return False

        expected = sequence.desired[field]
        if field == "power":
            if expected == "on":
                await self.api.turn_on()
            else:
                await self.api.turn_off()
        elif field == "hvac_mode":
            await self.api.set_ac_mode(str(expected))
        elif field == "target_temperature":
            await self.api.set_target_temperature("heat", float(expected))
        elif field == "fan_mode":
            await self.api.set_fan_speed(str(expected))
        elif field == "oscillation_mode":
            await self.api.set_oscillation_mode(str(expected))
        elif field == "optional_mode":
            await self.api.set_optional_mode(str(expected))
        else:
            return False

        sequence.in_flight_field = field
        sequence.in_flight_expected = expected
        sequence.sent_at = time.monotonic()
        self.logger.info(
            "Frost Protection %s step sent: %s", sequence.purpose, field
        )
        self._request_confirmation_refresh()
        return False

    def _temperature_reading(
        self, status: dict[str, Any]
    ) -> FrostTemperatureReading:
        """Prefer the configured external sensor, falling back to Samsung."""

        external = self._configured_external_temperature_reading()
        if external is not None and external.valid:
            return external
        return self._internal_temperature_reading(status)

    def _configured_external_temperature_reading(
        self,
    ) -> FrostTemperatureReading | None:
        entity_id = self.external_temperature_sensor
        return self._external_temperature_reading(entity_id) if entity_id else None

    def _internal_temperature_reading(
        self, status: dict[str, Any]
    ) -> FrostTemperatureReading:
        value = _attribute_value(status, "temperatureMeasurement", "temperature")
        updated_at = _attribute_timestamp(
            status, "temperatureMeasurement", "temperature"
        )
        try:
            temperature = float(value)
        except (TypeError, ValueError):
            return FrostTemperatureReading(
                None, updated_at, None, False, "internal_non_numeric"
            )
        if updated_at is not None and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=dt_util.UTC)
        return FrostTemperatureReading(temperature, updated_at, None, True)

    def _external_temperature_reading(
        self, entity_id: str
    ) -> FrostTemperatureReading:
        state: State | None = self.hass.states.get(entity_id)
        if state is None:
            return FrostTemperatureReading(
                None, None, entity_id, False, "external_entity_not_found"
            )
        reported_at = getattr(state, "last_reported", state.last_updated)
        if state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE, ""}:
            return FrostTemperatureReading(
                None, reported_at, entity_id, False, "external_unavailable"
            )
        try:
            temperature = float(state.state)
        except (TypeError, ValueError):
            return FrostTemperatureReading(
                None, reported_at, entity_id, False, "external_non_numeric"
            )
        if reported_at is not None and reported_at.tzinfo is None:
            reported_at = reported_at.replace(tzinfo=dt_util.UTC)
        return FrostTemperatureReading(temperature, reported_at, entity_id, True)
