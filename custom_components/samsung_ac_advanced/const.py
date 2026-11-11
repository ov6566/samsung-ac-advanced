"""Constants for Samsung AC Advanced."""

DOMAIN = "samsung_ac_advanced"

CONF_DEVICE_ID = "device_id"
CONF_TOKEN = "token"
CONF_DEVICE_NAME = "device_name"
CONF_ECO_TEMPERATURE_SENSOR = "eco_temperature_sensor"

DEFAULT_NAME = "Samsung AC Advanced"

COORDINATOR_UPDATE_INTERVAL = 30
OUTBOUND_COMMAND_SPACING = 1.0
OPTIMISTIC_UI_CONFIRMATION_TIMEOUT = 90.0
ECO_TEMPERATURE_MAX_AGE = 15 * 60

# Eco v1 temperature policy: one fixed logical target in 0.5 °C steps and one
# independent restart margin. Samsung still receives a normalized whole-degree
# physical setpoint; the logical target remains the thermal control threshold.
ECO_TARGET_TEMPERATURE_DEFAULT = 24.0
ECO_TARGET_TEMPERATURE_STEP = 0.5
ECO_HYSTERESIS_DEFAULT = 3.0
ECO_HYSTERESIS_MIN = 0.5
ECO_HYSTERESIS_MAX = 5.0
ECO_HYSTERESIS_STEP = 0.5


# Frost Protection is independent from Eco. It uses one user threshold plus a
# fixed 1.0 °C hysteresis and a fixed physical Samsung heating profile.
FROST_TARGET_TEMPERATURE_DEFAULT = 8.0
FROST_TARGET_TEMPERATURE_MIN = 7.0
FROST_TARGET_TEMPERATURE_MAX = 10.0
FROST_TARGET_TEMPERATURE_STEP = 0.5
FROST_HYSTERESIS = 1.0
FROST_PHYSICAL_TARGET_TEMPERATURE = 16.0

# HVAC modes that expose a writable target temperature. Samsung Auto keeps
# ownership of fan and operating behaviour, so it supports target-temperature
# control but must not run the simulated Eco controller.
SETPOINT_HVAC_MODES = frozenset({"cool", "heat", "auto"})
ECO_SUPPORTED_HVAC_MODES = frozenset({"cool", "heat"})
ECO_INCOMPATIBLE_HVAC_MODES = frozenset({"auto", "dry", "wind"})


OPTION_MAP = {
    "none": "off",
    "sleep": "sleep",
    "boost": "speed",
    "quiet": "quiet",
}


OSCILLATION_MAP = {
    "none": "fixed",
    "vertical": "vertical",
}


FAN_SPEED_MAP = {
    "auto": "auto",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "turbo": "turbo",
}


AC_MODE_MAP = {
    "auto": "auto",
    "cool": "cool",
    "heat": "heat",
    "dry": "dry",
    "fan_only": "wind",
    "off": "off",
}


FILTER_ALARM_THRESHOLD_MAP = {
    "hours_180": 180,
    "hours_300": 300,
    "hours_500": 500,
    "hours_700": 700,
}


REVERSE_FILTER_ALARM_THRESHOLD_MAP = {
    value: key
    for key, value in FILTER_ALARM_THRESHOLD_MAP.items()
}


FILTER_STATUS_MAP = {
    "normal": "normal",
    "needsCleaning": "needs_cleaning",
    "needs_cleaning": "needs_cleaning",
    "wash": "wash",
    "replace": "replace",
}


REVERSE_OPTION_MAP = {
    value: key
    for key, value in OPTION_MAP.items()
}


REVERSE_OSCILLATION_MAP = {
    value: key
    for key, value in OSCILLATION_MAP.items()
}


REVERSE_FAN_SPEED_MAP = {
    value: key
    for key, value in FAN_SPEED_MAP.items()
}


REVERSE_AC_MODE_MAP = {
    value: key
    for key, value in AC_MODE_MAP.items()
}
