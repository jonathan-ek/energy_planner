# Heavily based on https://github.com/custom-components/nordpool/blob/master/custom_components/nordpool/aio_price.py
import logging
from collections import defaultdict

from datetime import datetime
from datetime import timezone as ts
from dateutil.parser import parse as parse_dt
from pytz import timezone, utc
from homeassistant.util import dt as dt_utils

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
    """
    Parse json response from fetcher.
    Returns dictionary with
        - start time
        - end time
        - update time
        - currency
        - dictionary of areas, based on selection
            - list of values (dictionary with start and endtime and value)
            - possible other values, such as min, max, average for hourly
    """

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
        for area_key in r[data_source[1]].keys():
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

async def join_result_for_correct_time(results, dt):
    """Parse a list of responses from the api
    to extract the correct hours in their timezone.
    """
    # utc = datetime.utcnow()
    fin = defaultdict(dict)
    _LOGGER.debug("join_result_for_correct_time %s", dt)
    if dt is None:
        utc = datetime.now(ts.utc)
    else:
        utc = dt

    for day_ in results:
        for key, value in day_.get("areas", {}).items():
            zone = tzs.get(key)
            if zone is None:
                _LOGGER.debug("Skipping %s", key)
                continue
            else:
                zone = await dt_utils.async_get_time_zone(zone)

            values = day_["areas"][key].pop("values")

            if key not in fin["areas"]:
                fin["areas"][key] = {}
            fin["areas"][key].update(value)
            if "values" not in fin["areas"][key]:
                fin["areas"][key]["values"] = []

            start_of_day = utc.astimezone(zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end_of_day = utc.astimezone(zone).replace(
                hour=23, minute=59, second=59, microsecond=999999
            )

            for val in values:
                local = val["start"].astimezone(zone)
                local_end = val["end"].astimezone(zone)
                if start_of_day <= local <= end_of_day:
                    if local == local_end:
                        _LOGGER.info(
                            "Hour has the same start and end, most likely due to dst change %s excluded this hour",
                            val,
                        )
                    else:
                        fin["areas"][key]["values"].append(val)

    return fin