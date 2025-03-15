# Heavily based on https://github.com/custom-components/nordpool/blob/master/custom_components/nordpool/aio_price.py
import logging

from datetime import datetime
from datetime import timedelta
from dateutil.parser import parse as parse_dt
from pytz import timezone, utc
from homeassistant.util import dt as dt_utils
from homeassistant.core import HomeAssistant

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)
tzs = {
    "DK1": "Europe/Copenhagen",
    "DK2": "Europe/Copenhagen",
    "FI": "Europe/Helsinki",
    "EE": "Europe/Tallinn",
    "LT": "Europe/Vilnius",
    "LV": "Europe/Riga",
    "NO1": "Europe/Oslo",
    "NO2": "Europe/Oslo",
    "NO3": "Europe/Oslo",
    "NO4": "Europe/Oslo",
    "NO5": "Europe/Oslo",
    "SE1": "Europe/Stockholm",
    "SE2": "Europe/Stockholm",
    "SE3": "Europe/Stockholm",
    "SE4": "Europe/Stockholm",
    # What zone is this?
    "SYS": "Europe/Stockholm",
    "FR": "Europe/Paris",
    "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels",
    "AT": "Europe/Vienna",
    "GER": "Europe/Berlin",
}


def _parse_dt(time_str):
    """Parse datetimes to UTC from Stockholm time, which Nord Pool uses."""
    time = parse_dt(time_str, tzinfos={"Z": timezone("Europe/Stockholm")})
    if time.tzinfo is None:
        return timezone("Europe/Stockholm").localize(time).astimezone(utc)
    return time.astimezone(utc)


def _conv_to_float(s):
    """Convert numbers to float. Return infinity, if conversion fails."""
    # Skip if already float
    if isinstance(s, float):
        return s
    try:
        return float(s.replace(",", ".").replace(" ", ""))
    except ValueError:
        return float("inf")


def parse_json(data, currency=None, areas=None):
    """Parse json response from fetcher.

    Returns dictionary with
        - start time
        - end time
        - update time
        - currency
        - dictionary of areas, based on selection
            - list of values (dictionary with start and endtime and value)
            - possible other values, such as min, max, average for hourly
    """
    if data is None:
        return None
    if areas is None:
        areas = []

    if not isinstance(areas, list) and areas is not None:
        areas = [i.strip() for i in areas.split(",")]

    data_source = ("multiAreaEntries", "entryPerArea")

    if data.get("status", 200) != 200 and "version" not in data:
        raise Exception(f"Invalid response from Nordpool API: {data}")

    currency = data.get("currency", currency)

    start_time = None
    end_time = None
    # multiAreaDailyAggregates
    if len(data[data_source[0]]) > 0:
        start_time = _parse_dt(data[data_source[0]][0]["deliveryStart"])
        end_time = _parse_dt(data[data_source[0]][-1]["deliveryEnd"])
    updated = _parse_dt(data["updatedAt"])

    area_data = {}

    # Loop through response rows
    for r in data[data_source[0]]:
        row_start_time = _parse_dt(r["deliveryStart"])
        row_end_time = _parse_dt(r["deliveryEnd"])

        # Loop through columns
        for area_key in r[data_source[1]]:
            area_price = r[data_source[1]][area_key]
            # If areas is defined and name isn't in areas, skip column
            if area_key not in areas:
                continue

            # If name isn't in area_data, initialize dictionary
            if area_key not in area_data:
                area_data[area_key] = {
                    "values": [],
                }

            # Append dictionary to value list
            area_data[area_key]["values"].append(
                {
                    "start": row_start_time,
                    "end": row_end_time,
                    "value": _conv_to_float(area_price),
                }
            )

    return {
        "start": start_time,
        "end": end_time,
        "updated": updated,
        "currency": currency,
        "areas": area_data,
    }


async def join_result_for_correct_time(results, dt, nordpool_area):
    """Join raw data to format correctly.

    Parse a list of responses from the api to extract
    the correct hours in their timezone.
    """
    fin = []
    _LOGGER.debug("join_result_for_correct_time %s", dt)
    zone = tzs.get(nordpool_area)
    if zone is None:
        _LOGGER.debug("Failed to get timezone for %s", nordpool_area)
        return []
    zone = await dt_utils.async_get_time_zone(zone)
    start_of_day = dt.astimezone(zone).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_of_day = dt.astimezone(zone).replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    for day_ in results:
        if day_ is None:
            continue
        for val in day_["areas"][nordpool_area].get("values", []):
            start = val["start"]
            end = val["end"]
            if type(start) is str:
                start = datetime.fromisoformat(start)
                end = datetime.fromisoformat(end)
            local = start.astimezone(zone)
            local_end = end.astimezone(zone)
            if start_of_day <= local <= end_of_day:
                if local == local_end:
                    _LOGGER.info(
                        "Hour has the same start and end, "
                        "most likely due to dst change %s excluded this hour",
                        val,
                    )
                else:
                    fin.append(val)
    return fin


async def fetch_single_day(
    hass: HomeAssistant, nordpool_currency: str, nordpool_area: str, date: str
):
    """Fetch nordpool data for a single day."""
    nordpool_values = hass.data[DOMAIN]["values"].get("nordpool_values", {})
    if nordpool_area not in nordpool_values:
        nordpool_values[nordpool_area] = []
    else:
        nordpool_values[nordpool_area] = [*nordpool_values[nordpool_area]]
    for value in nordpool_values[nordpool_area]:
        if value.get("date") == date:
            _LOGGER.info(
                "Using cached nordpool data for %s %s %s",
                nordpool_currency,
                nordpool_area,
                date,
            )
            return value.get("values")
    try:
        values = await hass.services.async_call(
            "nordpool",
            "hourly",
            {"currency": nordpool_currency, "area": nordpool_area, "date": date},
            True,
            return_response=True,
        )
    except Exception:
        _LOGGER.error(
            "Failed to fetch nordpool data for %s %s %s",
            nordpool_currency,
            nordpool_area,
            date,
        )
        values = None
    tmp = parse_json(values, nordpool_currency, areas=[nordpool_area])
    if tmp is not None:
        nordpool_values[nordpool_area].append({"date": date, "values": tmp})
    hass.data[DOMAIN]["values"]["nordpool_values"] = nordpool_values
    return tmp


async def fetch_nordpool_data(
    hass: HomeAssistant,
    nordpool_currency: str,
    nordpool_area: str,
    include_tomorrow: bool = True,
):
    """Fetch nordpool data for today, tomorrow and the day after tomorrow."""
    now = dt_utils.now()
    nordpool_values = hass.data[DOMAIN]["values"].get("nordpool_values", {})
    if nordpool_area not in nordpool_values:
        nordpool_values[nordpool_area] = []
    else:
        nordpool_values[nordpool_area] = [
            value
            for value in nordpool_values[nordpool_area]
            if datetime.strptime(value.get("date"), "%Y-%m-%d")
            >= datetime.strptime(
                (now - timedelta(days=2)).strftime("%Y-%m-%d"), "%Y-%m-%d"
            )
        ]
    hass.data[DOMAIN]["values"]["nordpool_values"] = nordpool_values
    yesterdays_yesterdays_values = await fetch_single_day(
        hass,
        nordpool_currency,
        nordpool_area,
        (now - timedelta(days=2)).strftime("%Y-%m-%d"),
    )
    yesterdays_values = await fetch_single_day(
        hass,
        nordpool_currency,
        nordpool_area,
        (now - timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    todays_values = await fetch_single_day(
        hass, nordpool_currency, nordpool_area, now.strftime("%Y-%m-%d")
    )
    tomorrows_values = await fetch_single_day(
        hass,
        nordpool_currency,
        nordpool_area,
        (now + timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    # _LOGGER.info("Fetching nordpool data for %s %s", nordpool_currency, nordpool_area)
    # _LOGGER.info("Yesterdays yesterday: %s", yesterdays_yesterdays_values)
    # _LOGGER.info("Yesterday: %s", yesterdays_values)
    # _LOGGER.info("Today: %s", todays_values)
    # _LOGGER.info("Tomorrow: %s", tomorrows_values)

    tomorrows_tomorrows_values = None
    if include_tomorrow:
        tomorrows_tomorrows_values = await fetch_single_day(
            hass,
            nordpool_currency,
            nordpool_area,
            (now + timedelta(days=2)).strftime("%Y-%m-%d"),
        )
        # _LOGGER.info("Tomorrow's tomorrow: %s", tomorrows_tomorrows_values)

    yesterday = await join_result_for_correct_time(
        [
            yesterdays_yesterdays_values,
            yesterdays_values,
            todays_values,
        ],
        now - timedelta(days=1),
        nordpool_area,
    )
    today = await join_result_for_correct_time(
        [
            yesterdays_values,
            todays_values,
            tomorrows_values,
        ],
        now,
        nordpool_area,
    )
    tomorrow = None
    if include_tomorrow:
        tomorrow = await join_result_for_correct_time(
            [
                todays_values,
                tomorrows_values,
                tomorrows_tomorrows_values,
            ],
            now + timedelta(days=1),
            nordpool_area,
        )
    return yesterday, today, tomorrow
