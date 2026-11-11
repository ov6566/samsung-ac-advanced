"""Samsung AC Advanced."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .api import SamsungACAPI
from .const import CONF_DEVICE_ID, CONF_TOKEN, DOMAIN
from .coordinator import SamsungACAdvancedCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = ["button", "number", "select", "sensor", "switch"]

type SamsungConfigEntry = ConfigEntry


async def async_setup(
    hass: HomeAssistant,
    config: dict,
) -> bool:
    """Set up Samsung AC Advanced."""

    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SamsungConfigEntry,
) -> bool:
    """Set up Samsung AC Advanced from a config entry."""

    api = SamsungACAPI(
        device_id=entry.data[CONF_DEVICE_ID],
        token=entry.data[CONF_TOKEN],
    )
    coordinator = SamsungACAdvancedCoordinator(hass, api, entry)
    await coordinator.async_initialize()
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "name": entry.title,
    }

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant,
    entry: SamsungConfigEntry,
) -> None:
    """Reload when integration options change."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: SamsungConfigEntry,
) -> bool:
    """Unload Samsung AC Advanced."""

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
