"""Samsung AC Advanced button entities."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SamsungACAdvancedCoordinator


def _has_capability(data: dict | None, capability: str) -> bool:
    """Return whether the latest coordinator status exposes a capability."""

    if not isinstance(data, dict):
        return False
    components = data.get("components", {})
    if not isinstance(components, dict):
        return False
    main = components.get("main", {})
    return isinstance(main, dict) and isinstance(main.get(capability), dict)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung AC Advanced button entities."""

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SamsungACAdvancedCoordinator = data["coordinator"]

    device_name = entry.data.get("device_name", entry.title)
    device_id = entry.data["device_id"]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=device_name,
        manufacturer="Samsung",
        model="Air Conditioner",
    )

    # Remove the obsolete optimistic display-lighting switch from the entity
    # registry when upgrading from an earlier pre-release version.
    entity_registry = er.async_get(hass)
    legacy_entity_id = entity_registry.async_get_entity_id(
        "switch",
        DOMAIN,
        f"{device_id}_display_lighting",
    )
    if legacy_entity_id is not None:
        entity_registry.async_remove(legacy_entity_id)

    async_add_entities(
        [
            SamsungACDisplayLightingButton(
                coordinator,
                entry,
                device_info,
                enabled=True,
            ),
            SamsungACDisplayLightingButton(
                coordinator,
                entry,
                device_info,
                enabled=False,
            ),
            SamsungACFilterReminderResetButton(
                coordinator,
                entry,
                device_info,
            ),
        ]
    )


class SamsungACDisplayLightingButton(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    ButtonEntity,
):
    """Send a stateless display-lighting command."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        *,
        enabled: bool,
    ) -> None:
        """Initialize a display-lighting command button."""

        super().__init__(coordinator)

        command = "on" if enabled else "off"
        self._enabled = enabled
        self._attr_device_info = device_info
        self._attr_unique_id = (
            f"{entry.data['device_id']}_display_lighting_{command}"
        )
        self._attr_translation_key = f"display_lighting_{command}"

    async def async_press(self) -> None:
        """Send the display-lighting command."""

        await self.coordinator.api.set_display_light(self._enabled)


class SamsungACFilterReminderResetButton(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    ButtonEntity,
):
    """Reset Samsung's dust-filter cleaning reminder."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the filter-reminder reset button."""

        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_filter_reminder_reset"
        self._attr_translation_key = "filter_reminder_reset"

    @property
    def available(self) -> bool:
        """Expose the button only while the dust-filter capability is available."""

        return super().available and _has_capability(
            self.coordinator.data, "custom.dustFilter"
        )

    async def async_press(self) -> None:
        """Reset the filter reminder and refresh the shared coordinator once."""

        try:
            await self.coordinator.api.reset_dust_filter()
        except Exception:
            await self.coordinator.async_request_refresh()
            raise
        await self.coordinator.async_request_refresh()
