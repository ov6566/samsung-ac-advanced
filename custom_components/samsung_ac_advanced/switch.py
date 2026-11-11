"""Samsung AC Advanced switch entities."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OPTIMISTIC_UI_CONFIRMATION_TIMEOUT
from .coordinator import SamsungACAdvancedCoordinator


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung AC Advanced switch entities."""

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SamsungACAdvancedCoordinator = data["coordinator"]

    device_name = entry.data.get(
        "device_name",
        entry.title,
    )

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.data["device_id"])},
        name=device_name,
        manufacturer="Samsung",
        model="Air Conditioner",
    )

    async_add_entities(
        [
            SamsungACAutoCleaningSwitch(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
            SamsungACSoundEffectSwitch(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
            SamsungACDebugModeSwitch(
                coordinator,
                entry,
                device_info,
                device_name,
            ),
        ]
    )


class SamsungACAutoCleaningSwitch(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SwitchEntity,
):
    """Switch automatic cleaning on or off."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the automatic cleaning switch."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_auto_cleaning"
        )
        self._attr_translation_key = "auto_cleaning"

        self._optimistic_state: bool | None = None
        self._optimistic_deadline = 0.0

    def _state_from_coordinator(self) -> bool | None:
        """Return the confirmed Samsung Auto Clean state."""

        samsung_state = _attribute_value(
            self.coordinator.data,
            "custom.autoCleaningMode",
            "autoCleaningMode",
        )

        if samsung_state == "on":
            return True

        if samsung_state == "off":
            return False

        return None

    @property
    def is_on(self) -> bool | None:
        """Return whether automatic cleaning is enabled."""

        if self._optimistic_state is not None:
            return self._optimistic_state

        return self._state_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Keep a user choice visible until Samsung confirms or times out."""

        pending = self._optimistic_state
        if pending is not None:
            actual = self._state_from_coordinator()

            if actual == pending:
                self._optimistic_state = None
                self._optimistic_deadline = 0.0
            elif time.monotonic() >= self._optimistic_deadline:
                # Fall back to coordinator truth only after the same passive
                # confirmation window used by Sound/Mode/Fan/Oscillation.
                # No polling, delay or retry is added.
                self._optimistic_state = None
                self._optimistic_deadline = 0.0

        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable automatic cleaning."""

        await self._async_set_auto_cleaning_mode("on", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable automatic cleaning."""

        await self._async_set_auto_cleaning_mode("off", False)

    async def _async_set_auto_cleaning_mode(
        self,
        mode: str,
        state: bool,
    ) -> None:
        """Send the automatic cleaning command."""

        self._optimistic_state = state
        self._optimistic_deadline = (
            time.monotonic() + OPTIMISTIC_UI_CONFIRMATION_TIMEOUT
        )
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_auto_cleaning_mode(mode)
        except Exception:
            self._optimistic_state = None
            self._optimistic_deadline = 0.0
            self.async_write_ha_state()
            raise

        # Keep the explicit user choice visible while the existing shared
        # coordinator refresh/normal cadence confirms Samsung's physical state.
        # Older status may arrive first and must not visually undo the choice.
        await self.coordinator.async_request_refresh()


class SamsungACSoundEffectSwitch(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SwitchEntity,
):
    """Switch the Samsung sound effect on or off."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the sound-effect switch."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_sound_effect"
        )
        self._attr_translation_key = "sound_effect"

        self._optimistic_state: bool | None = None
        self._optimistic_deadline = 0.0

    def _state_from_coordinator(self) -> bool | None:
        """Return the confirmed Samsung sound state."""

        volume = _attribute_value(
            self.coordinator.data,
            "audioVolume",
            "volume",
        )

        if isinstance(volume, (int, float)):
            return volume > 0

        return None

    @property
    def available(self) -> bool:
        """Sound is owned by Eco for the complete Eco session."""

        return super().available and (
            not self.coordinator.eco.enabled
            or self.coordinator.eco.state.value == "armed"
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the Samsung sound effect is enabled."""

        if self._optimistic_state is not None:
            return self._optimistic_state

        return self._state_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Keep a user choice visible until Samsung confirms or times out."""

        pending = self._optimistic_state
        if pending is not None:
            actual = self._state_from_coordinator()

            if actual == pending:
                self._optimistic_state = None
                self._optimistic_deadline = 0.0
            elif time.monotonic() >= self._optimistic_deadline:
                # Confirmation did not arrive within the same passive window
                # used by Mode/Fan/Oscillation. Fall back to coordinator truth;
                # no polling, delay or retry is added.
                self._optimistic_state = None
                self._optimistic_deadline = 0.0

        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the Samsung sound effect."""

        await self._async_set_volume(100, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the Samsung sound effect."""

        await self._async_set_volume(0, False)

    async def _async_set_volume(
        self,
        volume: int,
        state: bool,
    ) -> None:
        """Send the sound-effect volume command."""

        self._optimistic_state = state
        self._optimistic_deadline = (
            time.monotonic() + OPTIMISTIC_UI_CONFIRMATION_TIMEOUT
        )
        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_volume(volume)
        except Exception:
            self._optimistic_state = None
            self._optimistic_deadline = 0.0
            self.async_write_ha_state()
            raise

        # Keep the explicit user choice visible while the existing shared
        # coordinator refresh/normal cadence confirms Samsung's physical state.
        # Older status may arrive first and must not visually undo the choice.
        await self.coordinator.async_request_refresh()


class SamsungACDebugModeSwitch(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SwitchEntity,
):
    """Enable or disable non-sensitive runtime diagnostics."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize debug mode."""

        super().__init__(coordinator)

        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_developer_mode"  # Legacy ID retained
        )
        self._attr_translation_key = "debug_mode"

    @property
    def is_on(self) -> bool:
        """Return whether debug mode is enabled."""

        return self.coordinator.debug_tracker.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable debug mode."""

        self.coordinator.set_debug_mode(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable debug mode."""

        self.coordinator.set_debug_mode(False)
        self.async_write_ha_state()
