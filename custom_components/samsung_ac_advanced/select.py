"""Samsung AC Advanced select entities."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    AC_MODE_MAP,
    DOMAIN,
    ECO_SUPPORTED_HVAC_MODES,
    FAN_SPEED_MAP,
    FILTER_ALARM_THRESHOLD_MAP,
    OPTION_MAP,
    OSCILLATION_MAP,
    OPTIMISTIC_UI_CONFIRMATION_TIMEOUT,
    REVERSE_AC_MODE_MAP,
    REVERSE_FAN_SPEED_MAP,
    REVERSE_FILTER_ALARM_THRESHOLD_MAP,
    REVERSE_OPTION_MAP,
    REVERSE_OSCILLATION_MAP,
)
from .coordinator import SamsungACAdvancedCoordinator



def _main_component(
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the main SmartThings component."""

    if not isinstance(data, dict):
        return {}

    components = data.get("components", {})

    if not isinstance(components, dict):
        return {}

    main = components.get("main", {})

    if not isinstance(main, dict):
        return {}

    return main


def _attribute_value(
    data: dict[str, Any] | None,
    capability: str,
    attribute: str,
) -> Any:
    """Return a SmartThings capability attribute value."""

    capability_data = _main_component(data).get(
        capability,
        {},
    )

    if not isinstance(capability_data, dict):
        return None

    attribute_data = capability_data.get(
        attribute,
        {},
    )

    if not isinstance(attribute_data, dict):
        return None

    return attribute_data.get("value")


@dataclass(slots=True)
class _PendingSelection:
    """One user-selected value awaiting normal coordinator confirmation."""

    option: str
    samsung_value: str
    deadline: float


class _OptimisticSelectionMixin:
    """Keep the user's selection visible until coordinator confirmation."""

    _pending_selection: _PendingSelection | None
    _selected_option: str

    def _init_optimistic_selection(self) -> None:
        self._pending_selection = None

    def _begin_optimistic_selection(self, option: str, samsung_value: str) -> None:
        self._pending_selection = _PendingSelection(
            option=option,
            samsung_value=samsung_value,
            deadline=time.monotonic() + OPTIMISTIC_UI_CONFIRMATION_TIMEOUT,
        )
        self._selected_option = option

    def _handle_optimistic_coordinator_value(
        self, *, actual_samsung_value: str | None, fallback_option: str
    ) -> None:
        pending = self._pending_selection
        if pending is None:
            self._selected_option = fallback_option
            return

        if actual_samsung_value == pending.samsung_value:
            self._selected_option = pending.option
            self._pending_selection = None
            return

        if time.monotonic() < pending.deadline:
            # An older physical state may arrive while Samsung is applying the
            # command. Keep the user's explicit choice visible.
            return

        # Confirmation did not arrive within the passive coordinator window.
        # Fall back to the latest physical Samsung state; no polling is added.
        self._pending_selection = None
        self._selected_option = fallback_option

    def _fail_optimistic_selection(self, fallback_option: str) -> None:
        self._pending_selection = None
        self._selected_option = fallback_option


def _power_state(
    data: dict[str, Any] | None,
) -> str | None:
    """Return the Samsung AC power state."""

    value = _attribute_value(
        data,
        "switch",
        "switch",
    )

    return value if isinstance(value, str) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung AC Advanced select entities."""

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SamsungACAdvancedCoordinator = data[
        "coordinator"
    ]

    device_name = entry.data.get(
        "device_name",
        entry.title,
    )

    device_info = DeviceInfo(
        identifiers={
            (
                DOMAIN,
                entry.data["device_id"],
            )
        },
        name=device_name,
        manufacturer="Samsung",
        model="Air Conditioner",
    )

    async_add_entities(
        [
            SamsungACModeSelect(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
            SamsungACOptionalModeSelect(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
            SamsungACFanSpeedSelect(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
            SamsungACOscillationSelect(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
            SamsungACFilterAlarmThresholdSelect(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
        ]
    )


class SamsungACModeSelect(
    _OptimisticSelectionMixin,
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SelectEntity,
):
    """Samsung AC operating mode selector."""

    _attr_has_entity_name = True
    _attr_options = list(AC_MODE_MAP.keys())

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the operating mode selector."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_mode"
        )
        self._attr_translation_key = "mode"

        self._selected_option = self._option_from_data(
            coordinator.data
        )
        self._init_optimistic_selection()

    @property
    def current_option(self) -> str:
        """Return the current operating mode."""

        return self._selected_option

    def _option_from_data(
        self,
        data: dict[str, Any] | None,
    ) -> str:
        """Translate coordinator data to a Home Assistant option."""

        eco = self.coordinator.eco
        transition = eco.transitions.diagnostics()
        if transition is not None and transition.get("name") == "restore":
            expected = transition.get("expected", {})
            expected_mode = expected.get("hvac_mode") if isinstance(expected, dict) else None
            if isinstance(expected_mode, str):
                # Eco has already committed to the restore target. Samsung may
                # still report the physical Stand-by Wind mode or Sleep Off for
                # a few seconds. Keep the intended thermal mode visible until
                # the existing restore transition confirms or times out.
                return REVERSE_AC_MODE_MAP.get(
                    expected_mode,
                    self._selected_option if hasattr(self, "_selected_option") else "auto",
                )

        if _power_state(data) == "off":
            # Sleep is an Eco-owned physical Off state. Keep the stored thermal
            # direction visible so the user sees Cool/Heat rather than Off.
            if eco.state.value == "satisfied_off" and eco.snapshot is not None:
                return REVERSE_AC_MODE_MAP.get(eco.snapshot.hvac_mode, "off")
            return "off"

        if eco.state.value == "satisfied_fan_quiet" and eco.snapshot is not None:
            # Internal Eco Stand-by is physically Samsung Wind mode, but Eco still
            # owns the thermal session. Present the logical Cool/Heat direction
            # consistently with Sleep while leaving coordinator truth untouched.
            return REVERSE_AC_MODE_MAP.get(eco.snapshot.hvac_mode, "fan_only")

        samsung_mode = _attribute_value(
            data,
            "airConditionerMode",
            "airConditionerMode",
        )

        if not isinstance(samsung_mode, str):
            return self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "auto"

        return REVERSE_AC_MODE_MAP.get(
            samsung_mode,
            self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "auto",
        )

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data without visually undoing a user choice."""

        data = self.coordinator.data
        power = _power_state(data)
        actual_mode = (
            "off"
            if power == "off"
            else _attribute_value(data, "airConditionerMode", "airConditionerMode")
        )
        self._handle_optimistic_coordinator_value(
            actual_samsung_value=actual_mode if isinstance(actual_mode, str) else None,
            fallback_option=self._option_from_data(data),
        )
        super()._handle_coordinator_update()

    async def async_select_option(
        self,
        option: str,
    ) -> None:
        """Set the Samsung AC operating mode."""

        if option not in AC_MODE_MAP:
            return

        if self.coordinator.frost.enabled:
            if option in {"heat", "off"}:
                samsung_command = AC_MODE_MAP[option]
                self._begin_optimistic_selection(option, samsung_command)
                self.async_write_ha_state()
                if option == "heat":
                    await self.coordinator.frost.async_request_heat(
                        self.coordinator.data or {}
                    )
                else:
                    await self.coordinator.frost.async_request_off(
                        self.coordinator.data or {}
                    )
                self.coordinator.async_update_listeners()
                return

            await self.coordinator.async_disable_frost(
                reason=f"incompatible HVAC mode selected by user: {option}"
            )

        samsung_command = AC_MODE_MAP[option]
        self._begin_optimistic_selection(option, samsung_command)
        if option != "off":
            self.coordinator.begin_pending_user_control("hvac_mode", samsung_command)
            # Pending mode intent also controls whether Frost Protection is a
            # valid preset. Update sibling entities immediately instead of
            # waiting for the next Samsung status response.
            self.coordinator.async_update_listeners()
        self.async_write_ha_state()

        try:
            if option == "off":
                if await self.coordinator.async_finish_sleep_off():
                    return
                # Outside Eco, Mode Off is a normal device command only.
                # No snapshot or later restore ownership is created.
                await self.coordinator.async_turn_off()
            else:
                await self.coordinator.async_set_hvac_mode(samsung_command)
                await self.coordinator.async_apply_local_desired_settings(
                    target_mode=samsung_command
                )
        except Exception:
            if option != "off":
                self.coordinator.clear_pending_user_control("hvac_mode")
            self._fail_optimistic_selection(
                self._option_from_data(self.coordinator.data)
            )
            self.async_write_ha_state()
            raise

        await self._refresh_after_command()

    async def _refresh_after_command(self) -> None:
        """Refresh the shared coordinator after a command."""

        await self.coordinator.async_request_refresh()


class SamsungACOptionalModeSelect(
    _OptimisticSelectionMixin,
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SelectEntity,
):
    """Samsung AC optional mode selector."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the optional mode selector."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_optional_mode"
        )
        self._attr_translation_key = "optional_mode"

        self._selected_option = self._option_from_data(
            coordinator.data
        )
        self._init_optimistic_selection()
        self._optional_selection_generation = 0
        self._pending_seen_mismatch = False

    def _begin_optional_optimistic_selection(
        self, option: str, samsung_value: str
    ) -> int:
        """Start one latest-wins Optional Mode presentation intent."""

        self._optional_selection_generation += 1
        generation = self._optional_selection_generation
        self._begin_optimistic_selection(option, samsung_value)
        actual = _attribute_value(
            self.coordinator.data,
            "custom.airConditionerOptionalMode",
            "acOptionalMode",
        )
        # If the coordinator already shows the requested value, do not accept
        # that pre-command state as confirmation. This matters for rapid A-B-A
        # sequences such as Quiet -> None -> Quiet: delayed feedback from the
        # first A must not confirm the final A before the intermediate B has
        # had a chance to surface.
        self._pending_seen_mismatch = (
            isinstance(actual, str) and actual != samsung_value
        )
        return generation

    @property
    def options(self) -> list[str]:
        """Return all presets, including the simulated Eco preset.

        Eco remains visible even when the selected temperature source is
        temporarily unavailable. Selecting it then moves the Eco controller
        to SUSPENDED, making the reason visible through the Eco status sensor
        instead of silently hiding the preset from the user interface.
        """

        power = _power_state(self.coordinator.data)
        mode = _attribute_value(
            self.coordinator.data,
            "airConditionerMode",
            "airConditionerMode",
        )
        options = [*OPTION_MAP.keys()]

        if self.coordinator.frost.enabled:
            options.append("frost")
            return options

        if self.coordinator.eco.enabled:
            options.append("eco")
            return options

        eco_selectable = power == "off" or mode in ECO_SUPPORTED_HVAC_MODES
        if eco_selectable:
            options.append("eco")

        pending_mode = self.coordinator.pending_user_control_value("hvac_mode")
        frost_context_available = self.coordinator.frost.available_for_context(
            self.coordinator.data,
            eco_enabled=self.coordinator.eco.enabled,
            local_restore_active=self.coordinator.local_values_active,
        )
        if (
            frost_context_available
            and (pending_mode is None or pending_mode == "heat")
        ):
            options.append("frost")
        return options

    @property
    def current_option(self) -> str:
        """Return the current optional mode without stale-state flip-back."""

        # A direct user choice has presentation priority while Samsung catches
        # up. This is the same passive confirmation contract already used for
        # Mode/Fan/Oscillation: no polling and no retry are introduced.
        if self._pending_selection is not None:
            return self._pending_selection.option
        if self.coordinator.frost.enabled:
            return "frost"
        if self.coordinator.eco.enabled:
            return "eco"
        return self._selected_option

    def _option_from_data(
        self,
        data: dict[str, Any] | None,
    ) -> str:
        """Translate coordinator data to a Home Assistant option."""

        samsung_mode = _attribute_value(
            data,
            "custom.airConditionerOptionalMode",
            "acOptionalMode",
        )

        if not isinstance(samsung_mode, str):
            return self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "none"

        return REVERSE_OPTION_MAP.get(
            samsung_mode,
            self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "none",
        )

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data without visually undoing a user preset."""

        # During an Eco -> Samsung-preset change, keep the explicit user
        # selection visible until Eco has actually relinquished ownership. A
        # physical Optional Mode value observed while Eco is still enabled may
        # merely be Eco's own value and must not confirm/clear that selection.
        if self._pending_selection is not None and not self.coordinator.eco.enabled:
            actual = _attribute_value(
                self.coordinator.data,
                "custom.airConditionerOptionalMode",
                "acOptionalMode",
            )
            pending = self._pending_selection
            if isinstance(actual, str) and actual != pending.samsung_value:
                self._pending_seen_mismatch = True

            # A matching value is accepted as confirmation once this latest
            # intent has observed a different physical value. If the physical
            # value was already identical when the user clicked, keep the
            # latest choice pending instead of mistaking stale A feedback for
            # confirmation in an A-B-A sequence. The existing timeout remains
            # the passive fallback boundary.
            if (
                actual != pending.samsung_value
                or self._pending_seen_mismatch
                or time.monotonic() >= pending.deadline
            ):
                self._handle_optimistic_coordinator_value(
                    actual_samsung_value=actual if isinstance(actual, str) else None,
                    fallback_option=self._option_from_data(self.coordinator.data),
                )
                if self._pending_selection is None:
                    self._pending_seen_mismatch = False
        elif (
            self._pending_selection is None
            and not self.coordinator.eco.enabled
            and not self.coordinator.frost.enabled
        ):
            self._selected_option = self._option_from_data(
                self.coordinator.data
            )

        super()._handle_coordinator_update()

    async def async_select_option(
        self,
        option: str,
    ) -> None:
        """Set the Samsung AC optional mode or simulated Eco preset."""

        if option == "frost":
            if option not in self.options:
                return
            self._optional_selection_generation += 1
            self._pending_selection = None
            self._pending_seen_mismatch = False
            accepted = await self.coordinator.async_enable_frost(
                self.coordinator.data or {}
            )
            self._selected_option = (
                "frost" if accepted else self._option_from_data(self.coordinator.data)
            )
            self.async_write_ha_state()
            self.coordinator.async_set_updated_data(
                self.coordinator.data or {}
            )
            return

        if option == "eco":
            if option not in self.options:
                return
            # Eco is a simulated integration preset rather than a Samsung
            # Optional Mode command. It owns its own presentation immediately.
            self._optional_selection_generation += 1
            self._pending_selection = None
            self._pending_seen_mismatch = False
            accepted = await self.coordinator.async_enable_eco(
                self.coordinator.data or {}
            )
            self._selected_option = "eco" if accepted else "none"
            self.async_write_ha_state()
            self.coordinator.async_set_updated_data(
                self.coordinator.data or {}
            )
            return

        if option not in OPTION_MAP:
            return

        if self.coordinator.frost.enabled:
            await self.coordinator.async_disable_frost(
                reason=f"preset changed by user: {option}"
            )

        samsung_command = OPTION_MAP[option]
        generation = self._begin_optional_optimistic_selection(
            option, samsung_command
        )
        self.async_write_ha_state()

        eco_was_enabled = self.coordinator.eco.enabled
        try:
            if eco_was_enabled:
                self.coordinator.device_logger.info(
                    "Optional preset selection received while Eco is active: %s",
                    option,
                )
                self.coordinator.cancel_queued_eco_activation()
                await self.coordinator.eco.async_disable(
                    restore=option == "none"
                )
                self.coordinator.async_set_updated_data(
                    self.coordinator.data or {}
                )

                if option == "none":
                    # The Eco restore already owns Optional Mode and restores
                    # the Original Snapshot. Do not send a second Off command.
                    # The optimistic selection remains until coordinator status
                    # confirms Off or the normal confirmation window expires.
                    await self._refresh_after_command()
                    return

            await self.coordinator.api.set_optional_mode(
                samsung_command
            )
        except Exception:
            # Only undo this command's presentation if it is still the newest
            # user intent. Rapid preset changes may already have replaced it.
            if generation == self._optional_selection_generation:
                self._fail_optimistic_selection(
                    self._option_from_data(self.coordinator.data)
                )
                self._pending_seen_mismatch = False
                self.async_write_ha_state()
            raise

        await self._refresh_after_command()

    async def _refresh_after_command(self) -> None:
        """Refresh the shared coordinator after a command."""

        await self.coordinator.async_request_refresh()


class SamsungACFanSpeedSelect(
    _OptimisticSelectionMixin,
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SelectEntity,
):
    """Samsung AC fan speed selector."""

    _attr_has_entity_name = True
    _attr_options = list(FAN_SPEED_MAP.keys())

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the fan speed selector."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_fan_speed"
        )
        self._attr_translation_key = "fan_speed"

        self._selected_option = self._option_from_data(
            coordinator.data
        )
        self._init_optimistic_selection()

    @property
    def current_option(self) -> str:
        """Return the current or logically owned fan speed."""

        if self._pending_selection is not None:
            return self._pending_selection.option
        if self.coordinator.frost.enabled:
            return "auto"

        pending = self.coordinator.eco.pending_desired_value("fan_mode")
        if isinstance(pending, str):
            return REVERSE_FAN_SPEED_MAP.get(pending, self._selected_option)
        if self._eco_owns_logical_fan_presentation:
            return (
                "low"
                if self.coordinator.eco.state.value == "satisfied_fan_quiet"
                else "auto"
            )

        if (
            self.coordinator.local_values_active
            and self.coordinator.local_desired is not None
            and self.coordinator.local_desired.fan_mode is not None
        ):
            return REVERSE_FAN_SPEED_MAP.get(
                self.coordinator.local_desired.fan_mode, self._selected_option
            )
        return self._selected_option

    @property
    def _eco_owns_logical_fan_presentation(self) -> bool:
        """Return whether Eco owns the visible live fan value."""

        eco = self.coordinator.eco
        return eco.state.value in {
            "running",
            "satisfied_off",
            "satisfied_fan_quiet",
        } or eco.sleep_entry_pending

    def _option_from_data(
        self,
        data: dict[str, Any] | None,
    ) -> str:
        """Translate coordinator data to a Home Assistant option."""

        samsung_mode = _attribute_value(
            data,
            "airConditionerFanMode",
            "fanMode",
        )

        if not isinstance(samsung_mode, str):
            return self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "auto"

        return REVERSE_FAN_SPEED_MAP.get(
            samsung_mode,
            self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "auto",
        )

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data without visually undoing a user choice."""

        if self.coordinator.frost.enabled:
            self._pending_selection = None
            self._selected_option = "auto"
        elif (
            self.coordinator.eco.enabled
            and not self.coordinator.eco.owns_powered_off_state
            and not self.coordinator.eco.sleep_entry_pending
            and self._pending_selection is not None
            and self.coordinator.pending_user_control_value("fan_mode") is None
        ):
            # A pre-Eco user intent has just been captured in the immutable
            # Original Snapshot. Eco now owns the live fan presentation.
            self._pending_selection = None
            self._selected_option = (
                "low"
                if self.coordinator.eco.state.value == "satisfied_fan_quiet"
                else "auto"
            )
        elif not self.coordinator.local_values_active:
            data = self.coordinator.data
            actual = _attribute_value(data, "airConditionerFanMode", "fanMode")
            self._handle_optimistic_coordinator_value(
                actual_samsung_value=actual if isinstance(actual, str) else None,
                fallback_option=self._option_from_data(data),
            )
        super()._handle_coordinator_update()

    async def async_select_option(
        self,
        option: str,
    ) -> None:
        """Set or locally preselect the Samsung AC fan speed."""

        if option not in FAN_SPEED_MAP:
            return

        if self.coordinator.frost.enabled:
            await self.coordinator.async_disable_frost(
                reason=f"fan speed changed by user: {option}"
            )

        samsung_command = FAN_SPEED_MAP[option]
        if self.coordinator.eco.accepts_sleep_user_override:
            self.coordinator.device_logger.info(
                "Eco Sleep fan user selection received: option=%s mapped=%s phase=%s",
                option,
                samsung_command,
                "entry_pending" if self.coordinator.eco.sleep_entry_pending else "confirmed",
            )

            # The explicit user choice owns the UI immediately. During the
            # short Sleep-entry window Eco queues the override until physical
            # Off is confirmed; during confirmed Sleep the existing staged
            # restore starts immediately. In both cases the UI must not flash
            # back to the logical Sleep value in between.
            self._begin_optimistic_selection(option, samsung_command)
            self.async_write_ha_state()
            try:
                if await self.coordinator.async_finish_sleep_with_override(
                    {"fan_mode": samsung_command}
                ):
                    return
            except Exception:
                self._fail_optimistic_selection(
                    self._option_from_data(self.coordinator.data)
                )
                self.async_write_ha_state()
                raise
            self._pending_selection = None

        if self.coordinator.local_values_active:
            # During the explicit Eco USER_OFF/Pending Restore exception, keep
            # the latest user choice as an overlay. If the AC is already On and
            # confirmation is pending, forward only this changed field.
            self._selected_option = option
            await self.coordinator.async_set_local_fan_mode(samsung_command)
            self.async_write_ha_state()
            return

        self._begin_optimistic_selection(option, samsung_command)
        self.coordinator.begin_pending_user_control("fan_mode", samsung_command)
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_fan_speed(samsung_command)
        except Exception:
            self.coordinator.clear_pending_user_control("fan_mode")
            self._fail_optimistic_selection(
                self._option_from_data(self.coordinator.data)
            )
            self.async_write_ha_state()
            raise

        await self._refresh_after_command()

    async def _refresh_after_command(self) -> None:
        """Refresh the shared coordinator after a command."""

        await self.coordinator.async_request_refresh()


class SamsungACOscillationSelect(
    _OptimisticSelectionMixin,
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SelectEntity,
):
    """Samsung AC oscillation selector."""

    _attr_has_entity_name = True
    _attr_options = list(OSCILLATION_MAP.keys())

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the oscillation selector."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_oscillation"
        )
        self._attr_translation_key = "oscillation"

        self._selected_option = self._option_from_data(
            coordinator.data
        )
        self._init_optimistic_selection()

    @property
    def current_option(self) -> str:
        """Return the current or logically owned oscillation mode."""

        if self._pending_selection is not None:
            return self._pending_selection.option
        if self.coordinator.frost.enabled:
            return "none"

        pending = self.coordinator.eco.pending_desired_value("oscillation_mode")
        if isinstance(pending, str):
            return REVERSE_OSCILLATION_MAP.get(pending, self._selected_option)
        if self._eco_owns_logical_oscillation_presentation:
            return "none"

        if (
            self.coordinator.local_values_active
            and self.coordinator.local_desired is not None
            and self.coordinator.local_desired.oscillation_mode is not None
        ):
            return REVERSE_OSCILLATION_MAP.get(
                self.coordinator.local_desired.oscillation_mode,
                self._selected_option,
            )
        return self._selected_option

    @property
    def _eco_owns_logical_oscillation_presentation(self) -> bool:
        """Return whether Eco owns the visible live oscillation value."""

        eco = self.coordinator.eco
        return eco.state.value in {
            "running",
            "satisfied_off",
            "satisfied_fan_quiet",
        } or eco.sleep_entry_pending

    def _option_from_data(
        self,
        data: dict[str, Any] | None,
    ) -> str:
        """Translate coordinator data to a Home Assistant option."""

        samsung_mode = _attribute_value(
            data,
            "fanOscillationMode",
            "fanOscillationMode",
        )

        if not isinstance(samsung_mode, str):
            return self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "none"

        return REVERSE_OSCILLATION_MAP.get(
            samsung_mode,
            self._selected_option if hasattr(
                self,
                "_selected_option",
            ) else "none",
        )

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data without visually undoing a user choice."""

        if self.coordinator.frost.enabled:
            self._pending_selection = None
            self._selected_option = "none"
        elif (
            self.coordinator.eco.enabled
            and not self.coordinator.eco.owns_powered_off_state
            and not self.coordinator.eco.sleep_entry_pending
            and self._pending_selection is not None
            and self.coordinator.pending_user_control_value("oscillation_mode")
            is None
        ):
            # A pre-Eco user intent has just been captured in the immutable
            # Original Snapshot. Eco now owns the live oscillation presentation.
            self._pending_selection = None
            self._selected_option = "none"
        elif not self.coordinator.local_values_active:
            data = self.coordinator.data
            actual = _attribute_value(
                data, "fanOscillationMode", "fanOscillationMode"
            )
            self._handle_optimistic_coordinator_value(
                actual_samsung_value=actual if isinstance(actual, str) else None,
                fallback_option=self._option_from_data(data),
            )
        super()._handle_coordinator_update()

    async def async_select_option(
        self,
        option: str,
    ) -> None:
        """Set or locally preselect the Samsung AC oscillation mode."""

        if option not in OSCILLATION_MAP:
            return

        if self.coordinator.frost.enabled:
            await self.coordinator.async_disable_frost(
                reason=f"oscillation changed by user: {option}"
            )

        samsung_command = OSCILLATION_MAP[option]
        if self.coordinator.eco.accepts_sleep_user_override:
            self.coordinator.device_logger.info(
                "Eco Sleep oscillation user selection received: option=%s mapped=%s phase=%s",
                option,
                samsung_command,
                "entry_pending" if self.coordinator.eco.sleep_entry_pending else "confirmed",
            )

            # Eco Sleep/Sleep-entry already owns oscillation="fixed". Selecting
            # that same logical value is therefore not an override and must
            # never terminate Eco or rewrite the desired exit state.  This is
            # also a guard against an unexpected stale/no-op select request.
            if samsung_command == "fixed":
                self._pending_selection = None
                self._selected_option = option
                self.async_write_ha_state()
                self.coordinator.device_logger.info(
                    "Eco Sleep oscillation selection ignored as no-op: %s",
                    samsung_command,
                )
                return

            # The explicit user choice owns the UI immediately. During the
            # short Sleep-entry window Eco queues the override until physical
            # Off is confirmed; during confirmed Sleep the existing staged
            # restore starts immediately. In both cases the UI must not flash
            # back to the logical Sleep value in between.
            self._begin_optimistic_selection(option, samsung_command)
            self.async_write_ha_state()
            try:
                if await self.coordinator.async_finish_sleep_with_override(
                    {"oscillation_mode": samsung_command}
                ):
                    return
            except Exception:
                self._fail_optimistic_selection(
                    self._option_from_data(self.coordinator.data)
                )
                self.async_write_ha_state()
                raise
            self._pending_selection = None

        if self.coordinator.local_values_active:
            # Same bounded Eco Pending Restore overlay as Fan: store the explicit
            # user choice and, during live confirmation, send only this field.
            self._selected_option = option
            await self.coordinator.async_set_local_oscillation_mode(samsung_command)
            self.async_write_ha_state()
            return

        self._begin_optimistic_selection(option, samsung_command)
        self.coordinator.begin_pending_user_control(
            "oscillation_mode", samsung_command
        )
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_oscillation_mode(samsung_command)
        except Exception:
            self.coordinator.clear_pending_user_control("oscillation_mode")
            self._fail_optimistic_selection(
                self._option_from_data(self.coordinator.data)
            )
            self.async_write_ha_state()
            raise

        await self._refresh_after_command()

    async def _refresh_after_command(self) -> None:
        """Refresh the shared coordinator after a command."""

        await self.coordinator.async_request_refresh()


class SamsungACFilterAlarmThresholdSelect(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SelectEntity,
):
    """Samsung AC dust-filter alarm threshold selector."""

    _attr_has_entity_name = True
    _attr_translation_key = "filter_alarm_threshold"

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the filter alarm threshold selector."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_filter_alarm_threshold"
        )

        self._selected_option = self._option_from_data(
            coordinator.data
        )
        self._ignore_updates_until = 0.0

    @property
    def options(self) -> list[str]:
        """Return supported filter alarm thresholds."""

        supported = _attribute_value(
            self.coordinator.data,
            "samsungce.dustFilterAlarm",
            "supportedAlarmThresholds",
        )

        if isinstance(supported, list):
            options = [
                REVERSE_FILTER_ALARM_THRESHOLD_MAP[value]
                for value in supported
                if value in REVERSE_FILTER_ALARM_THRESHOLD_MAP
            ]

            if options:
                return options

        return list(FILTER_ALARM_THRESHOLD_MAP.keys())

    @property
    def current_option(self) -> str | None:
        """Return the current filter alarm threshold."""

        return self._selected_option

    def _option_from_data(
        self,
        data: dict[str, Any] | None,
    ) -> str | None:
        """Translate coordinator data to a threshold option."""

        threshold = _attribute_value(
            data,
            "samsungce.dustFilterAlarm",
            "alarmThreshold",
        )

        if isinstance(threshold, int):
            return REVERSE_FILTER_ALARM_THRESHOLD_MAP.get(
                threshold
            )

        return self._selected_option if hasattr(
            self,
            "_selected_option",
        ) else None

    def _handle_coordinator_update(self) -> None:
        """Handle shared coordinator data updates."""

        if True:
            self._selected_option = self._option_from_data(
                self.coordinator.data
            )

        super()._handle_coordinator_update()

    async def async_select_option(
        self,
        option: str,
    ) -> None:
        """Set the filter alarm threshold."""

        if option not in self.options:
            return

        threshold = FILTER_ALARM_THRESHOLD_MAP[option]

        self._ignore_updates_until = (
            0.0
        )
        self._selected_option = option
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_filter_alarm_threshold(
                threshold
            )

            self._ignore_updates_until = 0.0
            await self.coordinator.async_request_refresh()

        except Exception:
            self._ignore_updates_until = 0.0
            await self.coordinator.async_request_refresh()
            raise

