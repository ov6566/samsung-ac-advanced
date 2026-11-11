"""Samsung AC Advanced sensor entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, FILTER_STATUS_MAP
from .coordinator import SamsungACAdvancedCoordinator
from .setpoint import (
    active_temperature_mode,
    resolve_physical_target_temperature,
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


def _remove_legacy_eco_target_sensor(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove the obsolete calculated Eco target sensor from the registry."""

    registry = er.async_get(hass)
    legacy_unique_id = f"{entry.data['device_id']}_eco_target_temperature"
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registry_entry.entity_id.startswith("sensor.")
            and registry_entry.unique_id == legacy_unique_id
        ):
            registry.async_remove(registry_entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung AC Advanced sensor entities."""

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

    _remove_legacy_eco_target_sensor(hass, entry)
    async_add_entities(
        [
            SamsungACFilterStatusSensor(
                coordinator, entry, device_info, device_name
            ),
            SamsungACFilterUsageSensor(
                coordinator, entry, device_info, device_name
            ),
            SamsungACFilterCapacitySensor(
                coordinator, entry, device_info, device_name
            ),
            SamsungACCurrentTargetTemperatureSensor(
                coordinator, entry, device_info, device_name
            ),
            SamsungACEcoStatusSensor(
                coordinator, entry, device_info, device_name
            ),
            SamsungACEcoAmbientTemperatureSensor(
                coordinator, entry, device_info, device_name
            ),
        ]
    )


class SamsungACFilterSensorBase(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SensorEntity,
):
    """Base class for Samsung AC filter sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
        key: str,
    ) -> None:
        """Initialize a filter sensor."""

        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_{key}"
        self._attr_translation_key = key


class SamsungACFilterStatusSensor(SamsungACFilterSensorBase):
    """Report Samsung AC dust-filter status."""

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the filter status sensor."""

        super().__init__(
            coordinator, entry, device_info, device_name, "filter_status"
        )

    @property
    def native_value(self) -> str | None:
        """Return the current filter status."""

        value = _attribute_value(
            self.coordinator.data,
            "custom.dustFilter",
            "dustFilterStatus",
        )

        if not isinstance(value, str):
            return None

        return FILTER_STATUS_MAP.get(value, value)


class SamsungACFilterUsageSensor(SamsungACFilterSensorBase):
    """Report Samsung AC dust-filter usage."""

    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the filter usage sensor."""

        super().__init__(
            coordinator, entry, device_info, device_name, "filter_usage"
        )

    @property
    def native_value(self) -> int | float | None:
        """Return filter usage as a percentage."""

        value = _attribute_value(
            self.coordinator.data,
            "custom.dustFilter",
            "dustFilterUsage",
        )
        return value if isinstance(value, (int, float)) else None


class SamsungACFilterCapacitySensor(SamsungACFilterSensorBase):
    """Report Samsung AC dust-filter capacity."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the filter capacity sensor."""

        super().__init__(
            coordinator, entry, device_info, device_name, "filter_capacity"
        )

    @property
    def native_value(self) -> int | float | None:
        """Return configured filter capacity in hours."""

        value = _attribute_value(
            self.coordinator.data,
            "custom.dustFilter",
            "dustFilterCapacity",
        )
        return value if isinstance(value, (int, float)) else None


class SamsungACCurrentTargetTemperatureSensor(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SensorEntity,
):
    """Expose the physical target temperature reported by the air conditioner."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_current_ac_target_temperature"
        self._attr_translation_key = "current_ac_target_temperature"

    @property
    def native_value(self) -> float | None:
        """Return the setpoint directly reported by the Samsung air conditioner."""

        mode = active_temperature_mode(self.coordinator.data)
        return resolve_physical_target_temperature(self.coordinator.data, mode)


class SamsungACEcoStatusSensor(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SensorEntity,
):
    """Report the internal Eco controller state for diagnostics."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        """Initialize the Eco status sensor."""

        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_eco_status"
        self._attr_translation_key = "eco_status"

    @property
    def native_value(self) -> str:
        """Return the Eco controller state."""

        eco = self.coordinator.eco
        if eco.sleep_entry_pending:
            # Present the user's logical Eco state immediately while the internal
            # controller still waits for coordinator confirmation of physical Off.
            return "satisfied_off"

        state = eco.state.value
        if state == "pending_restore":
            # Pending Restore is an internal ownership-transfer state, not a
            # user-facing Eco mode. Visually the user has already switched Eco off.
            return "disabled"
        if state == "running":
            return (
                "running_external"
                if eco.external_temperature_sensor
                else "running_internal"
            )
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return non-sensitive Eco diagnostics."""

        reading = self.coordinator.eco.temperature
        return {
            "temperature_source_type": self.coordinator.eco.temperature_source_type,
            "temperature_source": reading.source_entity_id or "internal",
            "temperature": reading.value,
            "temperature_updated_at": (
                reading.updated_at.isoformat()
                if reading.updated_at is not None
                else None
            ),
            "temperature_valid": reading.valid,
            "temperature_reason": reading.reason,
            "snapshot_stored": self.coordinator.eco.snapshot is not None,
        }



class SamsungACEcoAmbientTemperatureSensor(
    CoordinatorEntity[SamsungACAdvancedCoordinator],
    SensorEntity,
):
    """Expose the room temperature actually used by Eco."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: SamsungACAdvancedCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.data['device_id']}_eco_ambient_temperature"
        self._attr_translation_key = (
            "eco_ambient_temperature_external"
            if coordinator.eco.external_temperature_sensor
            else "eco_ambient_temperature_internal"
        )

    @property
    def native_value(self) -> float | None:
        return self.coordinator.eco.temperature.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self.coordinator.eco.temperature
        return {
            "temperature_source_type": self.coordinator.eco.temperature_source_type,
            "source_entity": reading.source_entity_id,
            "updated_at": (
                reading.updated_at.isoformat()
                if reading.updated_at is not None
                else None
            ),
            "valid": reading.valid,
            "reason": reading.reason,
        }
