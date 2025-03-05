import logging
from collections import defaultdict

from datetime import datetime, timedelta
from datetime import timezone as ts

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

async def join_result_for_correct_time(results, dt):
    """Parse a list of responses from the api
    to extract the correct hours in there timezone.
    """
    # utc = datetime.utcnow()
    fin = defaultdict(dict)
    # _LOGGER.debug("join_result_for_correct_time %s", dt)
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

            # We add junk here as the peak etc
            # from the api is based on cet, not the
            # hours in the we want so invalidate them
            # its later corrected in the sensor.

            values = day_["areas"][key].pop("values")

            # We need to check this so we dont overwrite stuff.
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
                            "Hour has the same start and end, most likly due to dst change %s exluded this hour",
                            val,
                        )
                    else:
                        fin["areas"][key]["values"].append(val)

    return fin