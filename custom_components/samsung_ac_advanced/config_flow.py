"""Config flow for Samsung AC Advanced."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import SamsungACAPI
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_ECO_TEMPERATURE_SENSOR,
    CONF_TOKEN,
    DEFAULT_NAME,
    DOMAIN,
)


class SamsungACAdvancedConfigFlow(
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Samsung AC Advanced config flow."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the first setup step."""

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
            self._abort_if_unique_id_configured()

            api = SamsungACAPI(
                user_input[CONF_DEVICE_ID],
                user_input[CONF_TOKEN],
            )
            device = await api.get_device()
            device_name = device.get("label", DEFAULT_NAME)
            user_input[CONF_DEVICE_NAME] = device_name

            return self.async_create_entry(
                title=device_name,
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): str,
                    vol.Required(CONF_TOKEN): str,
                }
            ),
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SamsungACAdvancedOptionsFlow:
        """Return the options flow handler."""

        return SamsungACAdvancedOptionsFlow(config_entry)


class SamsungACAdvancedOptionsFlow(config_entries.OptionsFlow):
    """Manage Samsung AC Advanced options."""

    def __init__(
        self,
        config_entry: config_entries.ConfigEntry,
    ) -> None:
        """Initialize the options flow."""

        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Configure shared Eco/Frost temperature-source options."""

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_sensor = self._config_entry.options.get(
            CONF_ECO_TEMPERATURE_SENSOR,
            "",
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ECO_TEMPERATURE_SENSOR,
                        default=current_sensor,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class=SensorDeviceClass.TEMPERATURE,
                        )
                    ),
                }
            ),
        )
