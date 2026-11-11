"""Model-aware Samsung thermostat setpoint helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .const import SETPOINT_HVAC_MODES


@dataclass(frozen=True, slots=True)
class SamsungACSetpointEndpoint:
    """SmartThings capability endpoint used for a thermostat setpoint."""

    capability: str
    attribute: str
    command: str


_COOLING_ENDPOINT = SamsungACSetpointEndpoint(
    capability="thermostatCoolingSetpoint",
    attribute="coolingSetpoint",
    command="setCoolingSetpoint",
)
_HEATING_ENDPOINT = SamsungACSetpointEndpoint(
    capability="thermostatHeatingSetpoint",
    attribute="heatingSetpoint",
    command="setHeatingSetpoint",
)


def attribute_value(
    status: dict[str, Any] | None,
    capability: str,
    attribute: str,
) -> Any:
    """Return one SmartThings capability attribute value."""

    attribute_data = _attribute_data(status, capability, attribute)
    return attribute_data.get("value")


def active_temperature_mode(status: dict[str, Any] | None) -> str | None:
    """Return the active mode when it supports a thermostat setpoint."""

    mode = attribute_value(status, "airConditionerMode", "airConditionerMode")
    return mode if mode in SETPOINT_HVAC_MODES else None


def resolve_setpoint_endpoint(
    status: dict[str, Any] | None,
    mode: str,
) -> SamsungACSetpointEndpoint:
    """Resolve the model-specific SmartThings endpoint for a setpoint.

    Some Samsung RAC models expose only ``thermostatCoolingSetpoint`` and use
    that capability as a shared target temperature in both cooling and heating
    mode. Models that expose a real heating setpoint keep using the dedicated
    heating capability.
    """

    if mode in {"cool", "auto"}:
        # Samsung RAC devices expose Auto through the same writable target
        # setpoint path used for cooling/shared setpoints.
        return _COOLING_ENDPOINT
    if mode != "heat":
        raise ValueError("Target temperature requires cool, heat or auto mode")

    if _attribute_exists(status, _HEATING_ENDPOINT.capability, _HEATING_ENDPOINT.attribute):
        return _HEATING_ENDPOINT
    return _COOLING_ENDPOINT


def resolve_target_temperature(
    status: dict[str, Any] | None,
    mode: str | None,
) -> float | None:
    """Return the active target temperature with Samsung model fallback."""

    if mode not in SETPOINT_HVAC_MODES:
        return None

    endpoint = resolve_setpoint_endpoint(status, mode)
    value = _float_or_none(attribute_value(status, endpoint.capability, endpoint.attribute))
    if value is not None:
        return value

    # A capability may exist but temporarily report no numeric value while the
    # device changes mode. For heating, fall back to Samsung's shared cooling
    # setpoint before reporting the target as unavailable.
    if mode == "heat" and endpoint != _COOLING_ENDPOINT:
        return _float_or_none(
            attribute_value(
                status,
                _COOLING_ENDPOINT.capability,
                _COOLING_ENDPOINT.attribute,
            )
        )
    return None



def resolve_physical_target_temperature(
    status: dict[str, Any] | None,
    preferred_mode: str | None = None,
) -> float | None:
    """Return the physical setpoint reported by the AC, independent of HVAC mode.

    Samsung RAC models may keep their shared cooling-setpoint capability populated
    while the device is in Wind/Stand-by or Off. Models with a dedicated heating
    setpoint remain supported when heating is the preferred mode.
    """

    if preferred_mode == "heat":
        heating = _float_or_none(
            attribute_value(
                status,
                _HEATING_ENDPOINT.capability,
                _HEATING_ENDPOINT.attribute,
            )
        )
        if heating is not None:
            return heating

    cooling = _float_or_none(
        attribute_value(
            status,
            _COOLING_ENDPOINT.capability,
            _COOLING_ENDPOINT.attribute,
        )
    )
    if cooling is not None:
        return cooling

    return _float_or_none(
        attribute_value(
            status,
            _HEATING_ENDPOINT.capability,
            _HEATING_ENDPOINT.attribute,
        )
    )


def normalize_half_degree_setpoint(temperature: float) -> float:
    """Normalize a logical target to 0.5 °C using conventional half-up rounding."""

    doubled = Decimal(str(temperature)) * Decimal("2")
    rounded = doubled.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(rounded / Decimal("2"))


def normalize_physical_setpoint(temperature: float) -> float:
    """Normalize a logical target to Samsung's whole-degree physical setpoint.

    Eco may keep a 0.5 °C logical target for threshold/hysteresis decisions, while
    SmartThings confirmation must use the physical whole-degree value Samsung can
    actually report. Conventional half-up rounding keeps 24.5 -> 25.0 deterministic.
    """

    return float(Decimal(str(temperature)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def _main_component(status: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {}
    components = status.get("components", {})
    if not isinstance(components, dict):
        return {}
    main = components.get("main", {})
    return main if isinstance(main, dict) else {}


def _attribute_data(
    status: dict[str, Any] | None,
    capability: str,
    attribute: str,
) -> dict[str, Any]:
    capability_data = _main_component(status).get(capability, {})
    if not isinstance(capability_data, dict):
        return {}
    attribute_data = capability_data.get(attribute, {})
    return attribute_data if isinstance(attribute_data, dict) else {}


def _attribute_exists(
    status: dict[str, Any] | None,
    capability: str,
    attribute: str,
) -> bool:
    return bool(_attribute_data(status, capability, attribute))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
