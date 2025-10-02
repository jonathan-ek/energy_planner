import logging

import datetime as dt
from homeassistant.core import HomeAssistant

from .utils import (
    reset,
    parse_datetime,
    update_entities,
    store_disable_state,
    restore_disable_state,
)
from .manual_slots import add_manual_slots
from .. import DOMAIN
from .nordpool_utils import fetch_nordpool_data, tzs

from homeassistant.util import dt as dt_utils

_LOGGER = logging.getLogger(__name__)


async def plan_day(hass: HomeAssistant, nordpool_values: [dict], config: dict):
    """Plan day."""
    _LOGGER.info("plan_day: %s", nordpool_values)

    cheapest_hours = sorted(
        sorted(nordpool_values, key=lambda x: x["value"])[
            : int(config["nr_of_charge_hours"] * 4)
        ],
        key=lambda x: x["start"],
    )
    max_soc = hass.data[DOMAIN]["config"].get("battery_max_soc", 100)
    min_soc = hass.data[DOMAIN]["config"].get("battery_shutdown_soc", 20)
    # combine neighbouring hours
    schedule = []
    for hour in cheapest_hours:
        if len(schedule) == 0:
            if hour["start"] != config["start_of_day"]:
                schedule.append(
                    {
                        "start": config["start_of_day"],
                        "end": hour["start"],
                        "state": "discharge",
                        "soc": min_soc,
                    }
                )
            schedule.append({**hour, "state": "charge", "soc": max_soc})
        else:
            if schedule[-1]["end"] == hour["start"]:
                schedule[-1]["end"] = hour["end"]
            else:
                schedule.append(
                    {
                        "start": schedule[-1]["end"],
                        "end": hour["start"],
                        "state": "discharge",
                        "soc": min_soc,
                    }
                )
                schedule.append({**hour, "state": "charge", "soc": max_soc})
    schedule.append(
        {
            "start": schedule[-1]["end"]
            if len(schedule) > 0
            else config["start_of_day"],
            "end": config["start_of_day"] + dt.timedelta(days=1),
            "state": "discharge",
            "soc": min_soc,
        }
    )

    now = dt_utils.now()
    # remove past hours
    schedule = [x for x in schedule if x["end"] > now]
    _LOGGER.info("schedule: %s", schedule)
    index = 1
    while True:
        if hass.data[DOMAIN]["values"][f"slot_{index}_state"] == "off":
            break
        index += 1
    for i, slot in enumerate(schedule):
        hass.data[DOMAIN]["values"][f"slot_{index + i}_date_time_start"] = slot["start"]
        hass.data[DOMAIN]["values"][f"slot_{index + i}_state"] = slot["state"]
        hass.data[DOMAIN]["values"][f"slot_{index + i}_active"] = True
        hass.data[DOMAIN]["values"][f"slot_{index + i}_soc"] = slot["soc"]
    if len(schedule) > 0:
        hass.data[DOMAIN]["values"][f"slot_{index + len(schedule)}_date_time_start"] = (
            schedule[-1]["end"]
        )
        hass.data[DOMAIN]["values"][f"slot_{index + len(schedule)}_state"] = "off"
        hass.data[DOMAIN]["values"][f"slot_{index + len(schedule)}_active"] = False


async def planner(hass: HomeAssistant, *args, **kwargs):
    """Planner."""
    nordpool_entity_id = hass.data[DOMAIN]["config"].get("nordpool_entity_id")
    if nordpool_entity_id is None:
        raise ValueError("Nordpool entity not set")
    nordpool_currency = str(nordpool_entity_id.split("_")[3]).upper()
    nordpool_area = str(nordpool_entity_id.split("_")[2]).upper()

    nordpool_state = hass.states.get(nordpool_entity_id)
    if nordpool_state is None:
        raise ValueError("Nordpool entity not found")
    attributes = nordpool_state.attributes
    _LOGGER.info("Running planner")

    tomorrow_valid = attributes.get("tomorrow_valid")
    yesterday, today, tomorrow = await fetch_nordpool_data(
        hass, nordpool_currency, nordpool_area, tomorrow_valid
    )
    if yesterday is None or today is None:
        raise ValueError("Nordpool data not found")
    nr_of_charge_hours = float(
        hass.data[DOMAIN]["config"].get("cheapest_hours_nr_of_charge_hours")
    )
    await store_disable_state(hass)
    await reset(hass)
    now = dt_utils.now()
    zone = tzs.get(nordpool_area)
    if zone is None:
        _LOGGER.debug("Failed to get timezone for %s", nordpool_area)
        return
    zone = await dt_utils.async_get_time_zone(zone)
    start_of_day = now.astimezone(zone).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    config = {
        "start_of_day": start_of_day,
        "nr_of_charge_hours": nr_of_charge_hours,
    }
    today_data = [
        {
            "start": parse_datetime(x["start"], zone),
            "end": parse_datetime(x["end"], zone),
            "value": x["value"],
        }
        for x in [*yesterday, *today]
        if start_of_day
        <= parse_datetime(x["start"], zone)
        < start_of_day + dt.timedelta(days=1)
    ]

    await plan_day(hass, today_data, config)

    if tomorrow is not None:
        start_of_day = (
            (now + dt.timedelta(days=1))
            .astimezone(zone)
            .replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        )

        config = {
            "start_of_day": start_of_day,
            "nr_of_charge_hours": nr_of_charge_hours,
        }
        tomorrow_data = [
            {
                "start": parse_datetime(x["start"], zone),
                "end": parse_datetime(x["end"], zone),
                "value": x["value"],
            }
            for x in [*today, *tomorrow]
            if start_of_day
            <= parse_datetime(x["start"], zone)
            < start_of_day + dt.timedelta(days=1)
        ]
        await plan_day(hass, tomorrow_data, config)
    await add_manual_slots(hass)
    await restore_disable_state(hass)
    await update_entities(hass)
    await hass.data[DOMAIN]["save"]()
