import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

import datetime as dt

from ..const import (
    DOMAIN,
    DATE_TIME_ENTITIES,
    TIME_ENTITIES,
    SELECT_ENTITIES,
    SWITCH_ENTITIES,
    NUMBER_ENTITIES,
    SLOT_COUNT,
    TARIFF_CUSTOM,
)
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
        float(config.get("network_cost") or 0.0),
        float(config.get("network_compensation") or 0.0),
    )


async def store_disable_state(hass: HomeAssistant):
    """Store disable state."""
    _LOGGER.info("Resetting planner")
    if "tmp" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["tmp"] = {}
    hass.data[DOMAIN]["tmp"]["disable_state"] = []
    for i in range(1, SLOT_COUNT + 1):
        if (
            hass.data[DOMAIN]["values"][f"slot_{i}_state"] != "off"
            and not hass.data[DOMAIN]["values"][f"slot_{i}_active"]
        ):
            hass.data[DOMAIN]["tmp"]["disable_state"].append(
                {
                    "start": str(
                        hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"]
                    ),
                    "end": str(
                        hass.data[DOMAIN]["values"].get(f"slot_{i + 1}_date_time_start")
                    ),
                    "state": hass.data[DOMAIN]["values"][f"slot_{i}_state"],
                    "active": hass.data[DOMAIN]["values"][f"slot_{i}_active"],
                    "soc": hass.data[DOMAIN]["values"][f"slot_{i}_soc"],
                }
            )


async def restore_disable_state(hass: HomeAssistant):
    """Restore disable state."""
    _LOGGER.info("Resetting planner")
    if "tmp" not in hass.data[DOMAIN]:
        return
    if "disable_state" not in hass.data[DOMAIN]["tmp"]:
        return
    for i in range(1, SLOT_COUNT):
        for s in hass.data[DOMAIN]["tmp"]["disable_state"]:
            if (
                str(hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"])
                == s["start"]
                and str(hass.data[DOMAIN]["values"][f"slot_{i + 1}_date_time_start"])
                == s["end"]
            ):
                hass.data[DOMAIN]["values"][f"slot_{i}_active"] = False
    del hass.data[DOMAIN]["tmp"]["disable_state"]


def clear_slot(hass: HomeAssistant, index: int):
    """Set a slot to its unused state."""
    hass.data[DOMAIN]["values"][f"slot_{index}_date_time_start"] = None
    hass.data[DOMAIN]["values"][f"slot_{index}_state"] = "off"
    hass.data[DOMAIN]["values"][f"slot_{index}_active"] = False
    hass.data[DOMAIN]["values"][f"slot_{index}_soc"] = 50


async def reset(hass: HomeAssistant):
    """Reset planner."""
    _LOGGER.info("Resetting planner")
    for i in range(1, SLOT_COUNT + 1):
        clear_slot(hass, i)


def write_schedule(hass: HomeAssistant, schedule: list[dict]):
    """Write schedule to the first free slots, followed by a terminating off slot."""
    values = hass.data[DOMAIN]["values"]
    index = 1
    while index <= SLOT_COUNT and values[f"slot_{index}_state"] != "off":
        index += 1
    # The last slot is reserved for the terminating off slot
    free_slots = max(SLOT_COUNT - index, 0)
    if len(schedule) > free_slots:
        _LOGGER.warning(
            "Schedule has %s slots but only %s are free, dropping the last ones",
            len(schedule),
            free_slots,
        )
        schedule = schedule[:free_slots]
    for i, slot in enumerate(schedule):
        values[f"slot_{index + i}_date_time_start"] = slot["start"]
        values[f"slot_{index + i}_state"] = slot["state"]
        values[f"slot_{index + i}_soc"] = slot["soc"]
        values[f"slot_{index + i}_active"] = True
    if len(schedule) > 0:
        values[f"slot_{index + len(schedule)}_date_time_start"] = schedule[-1]["end"]
        values[f"slot_{index + len(schedule)}_state"] = "off"
        values[f"slot_{index + len(schedule)}_active"] = False


def parse_datetime(val, zone=None):
    """Parse datetime."""
    if zone is None:
        return dt_utils.parse_datetime(val) if type(val) is str else val
    tmp = dt.datetime.fromisoformat(val) if type(val) is str else val
    if tmp is None:
        return None
    return tmp.astimezone(zone)


async def update_entities(hass: HomeAssistant, values=True, config=False):
    """Update entities."""
    for platform in [
        DATE_TIME_ENTITIES,
        TIME_ENTITIES,
        SELECT_ENTITIES,
        SWITCH_ENTITIES,
        NUMBER_ENTITIES,
    ]:
        for entity in hass.data[DOMAIN][platform]:
            if (values and entity.data_store == "values") or (
                config and entity.data_store == "config"
            ):
                entity.update()


async def clear_passed_slots(hass: HomeAssistant):
    """Clear passed slots."""
    now = dt_utils.now()
    next_slot_start = hass.data[DOMAIN]["values"].get("slot_2_date_time_start")
    if next_slot_start is None:
        return
    if now > next_slot_start:
        # shift all slots one step back
        for i in range(2, SLOT_COUNT + 1):
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_date_time_start"] = hass.data[
                DOMAIN
            ]["values"].get(f"slot_{i}_date_time_start")
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_active"] = hass.data[DOMAIN][
                "values"
            ].get(f"slot_{i}_active")
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_state"] = hass.data[DOMAIN][
                "values"
            ].get(f"slot_{i}_state")
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_soc"] = hass.data[DOMAIN][
                "values"
            ].get(f"slot_{i}_soc")
        clear_slot(hass, SLOT_COUNT)
        hass.data[DOMAIN]["manual_slots"][:] = [
            s for s in hass.data[DOMAIN]["manual_slots"] if s["end"] >= now
        ]

        await update_entities(hass)
        await hass.data[DOMAIN]["save"]()


def get_nordpool_price_per_kwh_in_cent(raw_price, tax=0.25):
    """Convert raw nordpool price to price per kWh in cents."""
    return raw_price * 0.1 * (1 + tax)


def price_under_discard_point(hass: HomeAssistant, price):
    """Check if price is under discard point."""
    return price < -1 * int(
        hass.data[DOMAIN]["config"].get("network_compensation", 1000)
    )
