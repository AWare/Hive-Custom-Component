"""Support for the Hive devices and services."""
from functools import wraps
import logging
import asyncio
import voluptuous as vol
from aiohttp.web_exceptions import HTTPException
from homeassistant import config_entries
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_USERNAME,
    CONF_PASSWORD
)

from .const import DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_registry import async_entries_for_device
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity import Entity


_LOGGER = logging.getLogger(__name__)
SERVICES = ["heating", "hotwater", "trvcontrol"]
SERVICE_BOOST_HOT_WATER = "boost_hot_water"
SERVICE_BOOST_HEATING = "boost_heating"
ATTR_TIME_PERIOD = "time_period"
ATTR_MODE = "on_off"
PLATFORMS = ["binary_sensor", "climate",
             "light", "sensor", "switch", "water_heater"]
ENTITY_LOOKUP = {}
BOOST_HEATING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_TIME_PERIOD): vol.All(
            cv.time_period, cv.positive_timedelta, lambda td: td.total_seconds() // 60
        ),
        vol.Optional(ATTR_TEMPERATURE, default="25.0"): vol.Coerce(float),
    }
)

BOOST_HOT_WATER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_TIME_PERIOD, default="00:30:00"): vol.All(
            cv.time_period, cv.positive_timedelta, lambda td: td.total_seconds() // 60
        ),
        vol.Required(ATTR_MODE): cv.string,
    }
)


async def async_setup(hass, config):
    """Set up the Hive Component."""
    hass.data[DOMAIN] = {}
    if DOMAIN not in config:
        return True
    else:
        _LOGGER.error(
            "YAML configuration is no longer supported - please remove configuration and setup via the UI.")
        return False


async def async_setup_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry):
    """Set up Hive from a config entry."""
    # Store an API object for your platforms to access
    # hass.data[DOMAIN][entry.entry_id] = MyApi(...)
    from pyhiveapi import Hive

    websession = aiohttp_client.async_get_clientsession(hass)
    hive = Hive(websession)
    hive_config = dict(entry.data)
    hive_options = dict(entry.options)

    async def heating_boost(service_call):
        """Handle the service call."""
        node_id = ENTITY_LOOKUP.get(service_call.data[ATTR_ENTITY_ID])
        if not node_id:
            # log or raise error
            _LOGGER.error("Cannot boost entity id entered")
            return

        device = hive.session.helper.get_device_from_id(node_id)
        minutes = service_call.data[ATTR_TIME_PERIOD]
        temperature = service_call.data[ATTR_TEMPERATURE]

        await hive.heating.turn_boost_on(device, minutes, temperature)

    async def hot_water_boost(service_call):
        """Handle the service call."""
        node_id = ENTITY_LOOKUP.get(service_call.data[ATTR_ENTITY_ID])
        if not node_id:
            # log or raise error
            _LOGGER.error("Cannot boost entity id entered")
            return

        device = hive.session.helper.get_device_from_id(node_id)
        minutes = service_call.data[ATTR_TIME_PERIOD]
        mode = service_call.data[ATTR_MODE]

        if mode == "on":
            await hive.hotwater.turn_boost_on(device, minutes)
        elif mode == "off":
            await hive.hotwater.turn_boost_off(device)

    Username = hive_config["options"].get(CONF_USERNAME)
    Password = hive_config.get(CONF_PASSWORD)

    # Update config entry options
    hive_options = hive_options if len(
        hive_options) > 0 else hive_config["options"]
    hive_config["options"].update(hive_options)
    hass.config_entries.async_update_entry(entry, options=hive_options)
    hass.data[DOMAIN][entry.entry_id] = hive

    try:
        devices = await hive.session.startSession(hive_config)
    except HTTPException as error:
        _LOGGER.error("Could not connect to the internet: %s", error)
        raise ConfigEntryNotReady() from error

    if devices == "INVALID_REAUTH":
        return hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_REAUTH},
                data={"username": Username, "password": Password}
            )
        )

    hive.devices = devices
    for component in PLATFORMS:
        devicelist = devices.get(component)
        if devicelist:
            hass.async_create_task(
                hass.config_entries.async_forward_entry_setup(
                    entry, component)
            )
            if component == "climate":
                hass.services.async_register(
                    DOMAIN,
                    SERVICE_BOOST_HEATING,
                    heating_boost,
                    schema=BOOST_HEATING_SCHEMA,
                )
            if component == "water_heater":
                hass.services.async_register(
                    DOMAIN,
                    SERVICE_BOOST_HOT_WATER,
                    hot_water_boost,
                    schema=BOOST_HOT_WATER_SCHEMA,
                )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry):
    """Unload a config entry."""

    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(
                    entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def refresh_system(func):
    """Force update all entities after state change."""

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        await func(self, *args, **kwargs)
        async_dispatcher_send(self.hass, DOMAIN)

    return wrapper


class HiveEntity(Entity):
    """Initiate Hive Base Class."""

    def __init__(self, hive, hive_device):
        """Initialize the instance."""
        self.hive = hive
        self.device = hive_device
        self.attributes = {}
        self._unique_id = f'{self.device["hiveID"]}-{self.device["hiveType"]}'

    async def async_added_to_hass(self):
        """When entity is added to Home Assistant."""
        async_dispatcher_connect(self.hass, DOMAIN, self._update_callback)
        if self.device["hiveType"] in SERVICES:
            ENTITY_LOOKUP[self.entity_id] = self.device["hiveID"]
    
    async def remove_item(self) -> None:
        """Remove entity if key is part of set.

        Remove entity if no entry in entity registry exist.
        Remove entity registry entry if no entry in device registry exist.
        Remove device registry entry if there is only one linked entity (this entity).
        Remove entity registry entry if there are more than one entity linked to the device registry entry.
        """

        entity_registry = await self.hass.helpers.entity_registry.async_get_registry()
        entity_entry = entity_registry.async_get(self.entity_id)
        if not entity_entry:
            await self.async_remove()
            return

        device_registry = await self.hass.helpers.device_registry.async_get_registry()
        device_entry = device_registry.async_get(entity_entry.device_id)
        if not device_entry:
            entity_registry.async_remove(self.entity_id)
            return

        if (
            len(
                async_entries_for_device(
                    entity_registry,
                    entity_entry.device_id,
                )
            )
            == 1
        ):
            device_registry.async_remove_device(device_entry.id)
            return

        entity_registry.async_remove(self.entity_id)

    @callback
    def _update_callback(self):
        """Call update method."""
        self.async_schedule_update_ha_state()
