"""Samsung AC Advanced number entities."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ECO_HYSTERESIS_MAX,
    ECO_HYSTERESIS_MIN,
    ECO_HYSTERESIS_STEP,
    ECO_TARGET_TEMPERATURE_STEP,
    FROST_PHYSICAL_TARGET_TEMPERATURE,
    FROST_TARGET_TEMPERATURE_MAX,
    FROST_TARGET_TEMPERATURE_MIN,
    FROST_TARGET_TEMPERATURE_STEP,
    OPTIMISTIC_UI_CONFIRMATION_TIMEOUT,
)
from .coordinator import SamsungACAdvancedCoordinator
from .setpoint import (
    active_temperature_mode,
    resolve_physical_target_temperature,
    resolve_target_temperature,
)


def _attribute_value(
    data: dict[str, Any] | None,
    capability: str,
    attribute: str,
) -> Any:
    """Return a SmartThings capability attribute value."""

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


def _remove_legacy_eco_number_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove obsolete Eco temperature-control entity-registry records.

    Earlier pre-release Min/Max/temperature-margin entities were replaced by
    target + restart margin. One pre-release schema briefly exposed that restart
    margin as ``Eco hysteresis``;
    remove that registry entry too so the clearer user-facing ``Eco margin`` name
    receives a clean entity ID. Stored controller values remain untouched.
    """

    registry = er.async_get(hass)
    device_id = entry.data["device_id"]
    legacy_unique_ids = {
        f"{device_id}_eco_min_temperature",
        f"{device_id}_eco_max_temperature",
        f"{device_id}_eco_temperature_offset",
        f"{device_id}_eco_hysteresis",
    }
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registry_entry.entity_id.startswith("number.")
            and registry_entry.unique_id in legacy_unique_ids
        ):
            registry.async_remove(registry_entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung AC temperature number entities."""

    coordinator: SamsungACAdvancedCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    device_name = entry.data.get("device_name", entry.title)
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.data["device_id"])},
        name=device_name,
        manufacturer="Samsung",
        model="Air Conditioner",
    )
    _remove_legacy_eco_number_entities(hass, entry)
    async_add_entities(
        [
            SamsungACTargetTemperatureNumber(
                coordinator,
                entry,
                device_info,
            ),
            SamsungACEcoTargetTemperatureNumber(
                coordinator,
                entry,
                device_info,
            ),
            SamsungACEcoMarginNumber(
                coordinator,
                entry,
                device_info,
            ),
            SamsungACFrostTargetTemperatureNumber(
                coordinator,
                entry,
                device_info,
            ),
        ]
    )


class SamsungACTargetTemperatureNumber(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    NumberEntity,
):
    """Control the air conditioner's active heating or cooling setpoint."""

    _attr_has_entity_name = True
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER
    _attr_force_update = True

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the target-temperature entity."""

        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_target_temperature"
        self._attr_translation_key = "target_temperature"
        self._optimistic_value: float | None = None
        self._optimistic_deadline = 0.0
        self._optimistic_seen_mismatch = False
        self._ignore_updates_until = 0.0
        self._eco_override_pending = False

    @property
    def native_min_value(self) -> float:
        """Return the air conditioner's minimum setpoint."""

        return self.coordinator.eco.device_limits(self.coordinator.data)[0]

    @property
    def native_max_value(self) -> float:
        """Return the air conditioner's maximum setpoint."""

        return self.coordinator.eco.device_limits(self.coordinator.data)[1]

    @property
    def available(self) -> bool:
        """Return whether a current or deferred Eco restore setpoint is available."""

        if self.coordinator.local_values_active:
            return super().available
        if self.coordinator.frost.enabled:
            return super().available
        return (
            super().available
            and resolve_physical_target_temperature(self.coordinator.data) is not None
        )

    @property
    def native_value(self) -> float | None:
        """Return the current heating or cooling setpoint."""

        # Explicit user intent always wins presentation while confirmation is
        # pending, including the short Eco-exit sequence started by moving this
        # slider.  Eco's logical target remains the fallback when there is no
        # pending user override.
        if self._optimistic_value is not None:
            return self._optimistic_value

        if self.coordinator.frost.enabled:
            return FROST_PHYSICAL_TARGET_TEMPERATURE

        if (
            self.coordinator.eco.enabled
            and self.coordinator.eco.target_temperature is not None
        ):
            return float(self.coordinator.eco.target_temperature)

        # The sole powered-off owner is Eco USER_OFF/Pending Restore. Keep its
        # retained pre-Eco value visible while the AC is Off and while the one
        # bounded Power-On restore is awaiting confirmation.
        if (
            self.coordinator.local_values_active
            and self.coordinator.local_desired is not None
        ):
            return self.coordinator.local_desired.target_temperature

        mode = active_temperature_mode(self.coordinator.data)
        return resolve_physical_target_temperature(self.coordinator.data, mode)

    def _handle_coordinator_update(self) -> None:
        """Keep normal user intent visible until Samsung confirms or times out."""

        if self.coordinator.frost.enabled:
            self._optimistic_value = None
            self._optimistic_deadline = 0.0
            self._optimistic_seen_mismatch = False
            self._eco_override_pending = False
        elif self.coordinator.eco.enabled and not self._eco_override_pending:
            self._optimistic_value = None
            self._optimistic_deadline = 0.0
            self._optimistic_seen_mismatch = False
        elif (
            self._optimistic_value is not None
            and not self.coordinator.local_values_active
        ):
            mode = active_temperature_mode(self.coordinator.data)
            actual = resolve_physical_target_temperature(
                self.coordinator.data, mode
            )
            if (
                actual is not None
                and abs(actual - self._optimistic_value) > 0.26
            ):
                self._optimistic_seen_mismatch = True

            if (
                actual is not None
                and abs(actual - self._optimistic_value) <= 0.26
                and self._optimistic_seen_mismatch
            ):
                self._optimistic_value = None
                self._optimistic_deadline = 0.0
                self._optimistic_seen_mismatch = False
                self._eco_override_pending = False
            elif time.monotonic() >= self._optimistic_deadline:
                # No confirmation arrived inside the same passive window used
                # by the other manual controls. Fall back to physical truth;
                # no polling, retry or additional SmartThings request is added.
                self._optimistic_value = None
                self._optimistic_deadline = 0.0
                self._optimistic_seen_mismatch = False
                self._eco_override_pending = False

        super()._handle_coordinator_update()

    async def async_set_native_value(self, value: float) -> None:
        """Set the active heating or cooling setpoint."""

        if self.coordinator.frost.enabled:
            await self.coordinator.async_disable_frost(
                reason="target temperature changed by user"
            )

        # During a real active Eco cycle, moving the AC target is an explicit
        # USER_OVERRIDE: the user's temperature becomes the overlay on the
        # immutable Original Snapshot and Eco ends.  Keep the selected value
        # visible immediately while the normal coordinator confirmation catches
        # up. ARMED Eco has no Original Snapshot yet and therefore falls through
        # to normal direct device handling below.
        if self.coordinator.eco.enabled and self.coordinator.eco.snapshot is not None:
            requested = float(value)
            self._ignore_updates_until = 0.0
            self._optimistic_value = requested
            self._optimistic_deadline = (
                time.monotonic() + OPTIMISTIC_UI_CONFIRMATION_TIMEOUT
            )
            actual = resolve_physical_target_temperature(
                self.coordinator.data, active_temperature_mode(self.coordinator.data)
            )
            self._optimistic_seen_mismatch = (
                actual is not None and abs(actual - requested) > 0.26
            )
            self._eco_override_pending = True
            self.async_write_ha_state()

            try:
                if self.coordinator.eco.accepts_sleep_user_override:
                    handled = await self.coordinator.async_finish_sleep_with_override(
                        {"target_temperature": requested}
                    )
                else:
                    handled = await self.coordinator.async_finish_eco_with_target_override(
                        requested
                    )
                if handled:
                    return
            except Exception:
                self._optimistic_value = None
                self._optimistic_deadline = 0.0
                self._optimistic_seen_mismatch = False
                self._eco_override_pending = False
                self.async_write_ha_state()
                raise

            # Defensive fall-through for an Eco state that changed while the
            # service call was being handled.  Do not leave stale optimistic
            # presentation behind.
            self._optimistic_value = None
            self._optimistic_deadline = 0.0
            self._optimistic_seen_mismatch = False
            self._eco_override_pending = False
            self.async_write_ha_state()

        if self.coordinator.local_values_active:
            await self.coordinator.async_set_local_target_temperature(float(value))
            # The local desired owner has already accepted the user value. Force
            # an immediate entity-state write instead of waiting for the next
            # coordinator refresh to make the slider catch up visually.
            self.async_write_ha_state()
            return

        mode = active_temperature_mode(self.coordinator.data)
        if mode is None:
            raise HomeAssistantError(
                "Target temperature can only be changed in cooling, heating or automatic mode"
            )

        self._ignore_updates_until = 0.0
        self._optimistic_value = float(value)
        self._optimistic_deadline = (
            time.monotonic() + OPTIMISTIC_UI_CONFIRMATION_TIMEOUT
        )
        actual = resolve_physical_target_temperature(
            self.coordinator.data, mode
        )
        self._optimistic_seen_mismatch = (
            actual is not None and abs(actual - float(value)) > 0.26
        )
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_target_temperature(mode, float(value))
            # Do not clear the user-visible value merely because the POST
            # returned. Samsung confirmation comes only from coordinator truth.
            await self.coordinator.async_request_refresh()
        except Exception:
            self._ignore_updates_until = 0.0
            self._optimistic_value = None
            self._optimistic_deadline = 0.0
            self._optimistic_seen_mismatch = False
            self.async_write_ha_state()
            raise


class SamsungACEcoTargetTemperatureNumber(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    NumberEntity,
):
    """Configure the fixed Eco room target temperature."""

    _attr_has_entity_name = True
    _attr_native_step = ECO_TARGET_TEMPERATURE_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_eco_target_temperature_setting"
        self._attr_translation_key = "eco_target_temperature"

    @property
    def native_min_value(self) -> float:
        return self.coordinator.eco.device_limits(self.coordinator.data)[0]

    @property
    def native_max_value(self) -> float:
        return self.coordinator.eco.device_limits(self.coordinator.data)[1]

    @property
    def native_value(self) -> float:
        return self.coordinator.eco.eco_target_temperature

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.eco.async_set_eco_target_temperature(
            float(value), status=self.coordinator.data
        )
        self.coordinator.async_update_listeners()


class SamsungACEcoMarginNumber(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    NumberEntity,
):
    """Configure the independent Eco restart margin."""

    _attr_has_entity_name = True
    _attr_native_min_value = ECO_HYSTERESIS_MIN
    _attr_native_max_value = ECO_HYSTERESIS_MAX
    _attr_native_step = ECO_HYSTERESIS_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_eco_margin"
        self._attr_translation_key = "eco_margin"

    @property
    def native_value(self) -> float:
        return self.coordinator.eco.eco_hysteresis

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.eco.async_set_eco_hysteresis(float(value))
        self.coordinator.async_update_listeners()

class SamsungACFrostTargetTemperatureNumber(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    NumberEntity,
):
    """Configure the independent Frost Protection threshold."""

    _attr_has_entity_name = True
    _attr_native_min_value = FROST_TARGET_TEMPERATURE_MIN
    _attr_native_max_value = FROST_TARGET_TEMPERATURE_MAX
    _attr_native_step = FROST_TARGET_TEMPERATURE_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_frost_target_temperature_setting"
        )
        self._attr_translation_key = "frost_target_temperature"

    @property
    def available(self) -> bool:
        pending_mode = self.coordinator.pending_user_control_value("hvac_mode")
        return (
            super().available
            and (pending_mode is None or pending_mode == "heat")
            and self.coordinator.frost.available_for_context(
                self.coordinator.data,
                eco_enabled=self.coordinator.eco.enabled,
                local_restore_active=self.coordinator.local_values_active,
            )
        )

    @property
    def native_value(self) -> float:
        return self.coordinator.frost.target_temperature

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.frost.async_set_target_temperature(float(value))
        self.coordinator.async_update_listeners()

