import logging

from homeassistant.core import HomeAssistant

from ..const import DOMAIN, TARIFF_CUSTOM
from . import settings
from .tariff import TARIFF_PRESETS, Tariff, flat_tariff, tariff_from_dict

_LOGGER = logging.getLogger(__name__)


def get_tariff(hass: HomeAssistant) -> Tariff:
    """Return the grid tariff chosen in the integration options.

    Falls back to the flat `network_cost` / `network_compensation` settings when no
    preset is chosen or the stored custom tariff is invalid.
    """
    options = hass.data[DOMAIN].get("options", {})
    preset = options.get("tariff_preset")
    try:
        if preset == TARIFF_CUSTOM:
            return tariff_from_dict(options.get("tariff") or {})
        if preset in TARIFF_PRESETS:
            return tariff_from_dict(TARIFF_PRESETS[preset])
    except ValueError:
        _LOGGER.exception("Invalid tariff, using the flat network settings")
    config = hass.data[DOMAIN]["config"]
    return flat_tariff(
        settings.number(config, "network_cost"),
        settings.number(config, "network_compensation"),
    )
