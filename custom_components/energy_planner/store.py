import logging

from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import Store

from custom_components.energy_planner.const import VERSION_STORAGE
_LOGGER = logging.getLogger(__name__)

def get_store_key(key):
    """Return the key to use with homeassistant.helpers.storage.Storage."""
    return key if "/" in key else f"energy_planner.{key}"


def _get_store_for_key(hass, key, encoder):
    """Create a Store object for the key."""
    return Store(hass, VERSION_STORAGE, get_store_key(key), encoder=encoder, atomic_writes=True)


def get_store_for_key(hass, key):
    """Create a Store object for the key."""
    return _get_store_for_key(hass, key, JSONEncoder)

async def async_load_from_store(hass, key):
    """Load the retained data from store and return de-serialized data."""
    return await get_store_for_key(hass, key).async_load() or {}

async def async_save_to_store(hass, key, data):
    """Generate dynamic data to store and save it to the filesystem.

    The data is only written if the content on the disk has changed
    by reading the existing content and comparing it.

    If the data has changed this will generate two executor jobs

    If the data has not changed this will generate one executor job
    """
    current = await async_load_from_store(hass, key)
    if current is None or current != data:
        await get_store_for_key(hass, key).async_save(data)
        return
    _LOGGER.debug(
        "<HACSStore async_save_to_store> Did not store data for '%s'. Content did not change",
        get_store_key(key),
    )


async def async_remove_store(hass, key):
    """Remove a store element that should no longer be used."""
    if "/" not in key:
        return
    await get_store_for_key(hass, key).async_remove()