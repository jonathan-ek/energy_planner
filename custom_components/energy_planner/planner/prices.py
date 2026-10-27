"""Spot prices: the published ones, an estimate of the next day, their history.

`async_update_prices` (called by every plan run) fetches the published prices
through the Nord Pool integration, keeps the plan's own copy of each day's hourly
prices and estimates the day after the published ones (price_estimate.py). The
result is a `SpotPrices` in `hass.data[DOMAIN]["prices"]`, read by the battery plan
and, through `spot_at`, by the heat pump sensors, also when no plan can be made.

Prices are SEK/MWh excl. VAT per quarter start in UTC, so the hour repeated when DST
ends has its own.
"""

from collections.abc import Mapping
import dataclasses
import datetime as dt
import functools
import logging
from statistics import mean

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN
from ..store import async_load_from_store, async_save_to_store
from .nordpool_utils import fetch_nordpool_data
from .price_estimate import (
    MIN_HOURS,
    WIND_FIT_DAYS,
    WIND_SITES,
    Prices,
    Wind,
    estimate,
    merge,
    scale_to,
)
from .timeline import Timeline, quarter_start

_LOGGER = logging.getLogger(__name__)


# Price history for the estimate of the first day without published prices
PRICE_HISTORY_DAYS = 400
MIN_SCALE_HOURS = 4
# The estimate covers the day after the published prices, at most the day after
# tomorrow (lead 2). One more day ahead saved 12-40 kr/year (backtest Oct 2026)
ESTIMATE_MAX_LEAD = 2
# Day-before wind forecasts for the estimate's wind correction (price_estimate.py)
WIND_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
WIND_TIMEOUT = aiohttp.ClientTimeout(total=60)
# The plan's own copy of each day's prices (`_record_prices`): the recorder's history
# of the Nord Pool sensor loses the afternoon when its attributes pass 16 KB (from
# about 13:00, when tomorrow's prices arrive; SEK/kWh sensors, Oct 2026)
PRICE_STORE = "price_history"
# Complete day-ahead prices per bidding zone (EUR/MWh) to fill the gaps
ENERGY_CHARTS_URL = "https://api.energy-charts.info/price"


@dataclasses.dataclass(frozen=True)
class SpotPrices:
    """Spot prices per quarter start (UTC), SEK/MWh excl. VAT."""

    published: Mapping[dt.datetime, float]
    # The day after the published prices, at most the day after tomorrow
    estimated: Mapping[dt.datetime, float]


def nordpool_zone(entity_id: str) -> tuple[str, str]:
    """Return the area and currency of a Nord Pool sensor, by position in its id.

    E.g. `sensor.nordpool_kwh_se3_sek_3_10_025` is ("SE3", "SEK").
    """
    parts = entity_id.split("_")
    return parts[2].upper(), parts[3].upper()


async def async_update_prices(
    hass: HomeAssistant, now: dt.datetime, *, estimate: bool = True
) -> SpotPrices:
    """Fetch the published prices, record them, estimate the next day and store all.

    The published prices are stored before the estimate is made, so the heat pump
    sensors have the price now even if the estimate fails. Without `estimate` (no
    plan can be made) the recorder history and the network are left alone.
    """
    published = await _published(hass)
    await _record_prices(hass, published, now)
    hass.data[DOMAIN]["prices"] = SpotPrices(published, {})
    if not estimate:
        return hass.data[DOMAIN]["prices"]
    prices = SpotPrices(published, await _estimated_prices(hass, published, now))
    hass.data[DOMAIN]["prices"] = prices
    return prices


def spot_at(hass: HomeAssistant, moment: dt.datetime) -> float | None:
    """Return the published spot price at a moment, None before the first fetch."""
    prices: SpotPrices | None = hass.data[DOMAIN].get("prices")
    return prices.published.get(quarter_start(moment)) if prices else None


async def _published(hass: HomeAssistant) -> dict[dt.datetime, float]:
    """Spot prices (SEK/MWh excl. VAT) per quarter start in UTC, yesterday to tomorrow.

    UTC keys: local times in the hour repeated when DST ends are equal (and hash
    alike) in one zone, so the two 02:xx hours would share their prices.
    """
    entity_id = hass.data[DOMAIN]["config"].get("nordpool_entity_id")
    state = hass.states.get(entity_id) if entity_id else None
    if entity_id is None or state is None:
        return {}
    area, currency = nordpool_zone(entity_id)
    tomorrow_valid = state.attributes.get("tomorrow_valid")
    yesterday, today, tomorrow = await fetch_nordpool_data(
        hass, currency, area, bool(tomorrow_valid)
    )
    prices = {}
    for value in [*(yesterday or []), *(today or []), *(tomorrow or [])]:
        start = value["start"]
        if isinstance(start, str):
            start = dt.datetime.fromisoformat(start)
        # A time without an offset is in HA's time zone (as_utc), as before
        prices[quarter_start(dt_utils.as_utc(start))] = float(value["value"])
    return prices


async def _price_history(
    hass: HomeAssistant, statistic_id: str, start: dt.datetime, end: dt.datetime
) -> dict[dt.date, dict[int, float]]:
    """Return the Nord Pool sensor's price per local date and hour, in its unit.

    The recorder keeps the sensor's last state in each hour (the hour's last quarter
    since quarter prices), which is what the estimate was backtested on.
    """
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"state"},
    )
    result: dict[dt.date, dict[int, float]] = {}
    for row in stats.get(statistic_id, []):
        state = row.get("state")
        if state is not None:
            local = dt_utils.as_local(dt.datetime.fromtimestamp(row["start"], dt.UTC))
            result.setdefault(local.date(), {})[local.hour] = state
    return result


async def _wind_forecast(
    hass: HomeAssistant, day: dt.date, lead: int = 1
) -> Wind | None:
    """Return Open-Meteo's wind forecast made `lead` days before, None on failure.

    Wind speed at 100 m (km/h) per site in WIND_SITES, local date and hour, from
    WIND_FIT_DAYS + 1 days before `day` to `day`; one request for all sites. The
    estimate's wind correction is fitted on the past days of the same lead, so the
    day after tomorrow (lead 2) is corrected as a two days old forecast predicts.
    """
    sites = list(WIND_SITES)
    variable = f"wind_speed_100m_previous_day{lead}"
    params = {
        "latitude": ",".join(str(WIND_SITES[site][0]) for site in sites),
        "longitude": ",".join(str(WIND_SITES[site][1]) for site in sites),
        "hourly": variable,
        "start_date": (day - dt.timedelta(days=WIND_FIT_DAYS + 1)).isoformat(),
        "end_date": day.isoformat(),
        "timezone": str(dt_utils.get_default_time_zone()),
    }
    try:
        async with async_get_clientsession(hass).get(
            WIND_URL, params=params, timeout=WIND_TIMEOUT
        ) as response:
            response.raise_for_status()
            data = await response.json()
        wind: dict[str, dict[dt.date, dict[int, float]]] = {}
        for site, location in zip(sites, data, strict=True):
            hourly = location["hourly"]
            for time, speed in zip(hourly["time"], hourly[variable], strict=True):
                if speed is not None:
                    local = dt.datetime.fromisoformat(time)
                    wind.setdefault(site, {}).setdefault(local.date(), {})[
                        local.hour
                    ] = float(speed)
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as err:
        _LOGGER.warning("No wind forecast for the price estimate: %s", err)
        return None
    return wind


async def _stored_prices(hass: HomeAssistant) -> dict[dt.date, dict[int, float]]:
    """Return the recorded hourly prices (SEK/MWh excl. VAT), loaded once."""
    stored = hass.data[DOMAIN].get(PRICE_STORE)
    if stored is None:
        raw = await async_load_from_store(hass, PRICE_STORE)
        stored = {
            dt.date.fromisoformat(day): {int(h): float(p) for h, p in hours.items()}
            for day, hours in raw.items()
        }
        hass.data[DOMAIN][PRICE_STORE] = stored
    return stored


async def _record_prices(
    hass: HomeAssistant, prices: dict[dt.datetime, float], now: dt.datetime
):
    """Store each complete day's hourly mean prices, for PRICE_HISTORY_DAYS."""
    stored = await _stored_prices(hass)
    changed = False
    for day, hours in _hourly(prices).items():
        if len(hours) >= MIN_HOURS and stored.get(day) != hours:
            stored[day] = hours
            changed = True
    oldest = now.date() - dt.timedelta(days=PRICE_HISTORY_DAYS)
    for day in [day for day in stored if day < oldest]:
        del stored[day]
        changed = True
    if changed:
        await async_save_to_store(
            hass,
            PRICE_STORE,
            {
                day.isoformat(): {str(hour): price for hour, price in hours.items()}
                for day, hours in sorted(stored.items())
            },
        )


async def _energy_charts_history(
    hass: HomeAssistant, area: str, start: dt.date, end: dt.date
) -> Prices | None:
    """Return Energy-Charts' day-ahead prices (EUR/MWh) per local date and hour.

    The mean of the hour's quarters; None on failure (e.g. an unknown zone).
    """
    params = {"bzn": area, "start": start.isoformat(), "end": end.isoformat()}
    try:
        async with async_get_clientsession(hass).get(
            ENERGY_CHARTS_URL, params=params, timeout=WIND_TIMEOUT
        ) as response:
            response.raise_for_status()
            data = await response.json()
        values: dict[dt.date, dict[int, list[float]]] = {}
        for seconds, price in zip(data["unix_seconds"], data["price"], strict=True):
            if price is not None:
                local = dt_utils.as_local(dt.datetime.fromtimestamp(seconds, dt.UTC))
                values.setdefault(local.date(), {}).setdefault(local.hour, []).append(
                    float(price)
                )
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as err:
        _LOGGER.warning("No Energy-Charts prices for the price estimate: %s", err)
        return None
    return {
        day: {hour: mean(v) for hour, v in hours.items()}
        for day, hours in values.items()
    }


def _hourly(prices: dict[dt.datetime, float]) -> dict[dt.date, dict[int, float]]:
    """Return the mean price per local date and hour."""
    values: dict[dt.date, dict[int, list[float]]] = {}
    for start, price in prices.items():
        local = dt_utils.as_local(start)
        values.setdefault(local.date(), {}).setdefault(local.hour, []).append(price)
    return {
        day: {hour: mean(v) for hour, v in hours.items()}
        for day, hours in values.items()
    }


async def _estimated_prices(
    hass: HomeAssistant, prices: dict[dt.datetime, float], now: dt.datetime
) -> dict[dt.datetime, float]:
    """Return estimated spot prices per quarter (UTC) after the published ones.

    That is tomorrow until its prices are published (about 13:00), then the day after
    tomorrow, so the plan always sees a day past the night ahead. From the recorded
    prices (`_record_prices`), the Nord Pool sensor's history and Energy-Charts
    (price_estimate.py), the last two scaled to SEK/MWh excl. VAT by the known prices
    of the last two days, with the wind forecast made as many days before
    (`_wind_forecast`) if Open-Meteo answers. Calculated once per day and lead, so
    the evening's estimate of the day after tomorrow is made again at midnight with
    a fresher wind forecast.
    """
    if not prices:
        return {}
    day = max(dt_utils.as_local(max(prices)).date(), now.date()) + dt.timedelta(days=1)
    lead = (day - now.date()).days
    if lead > ESTIMATE_MAX_LEAD:
        return {}
    cached = hass.data[DOMAIN].get("price_estimate")
    if cached and cached["day"] == day and cached.get("lead") == lead:
        return cached["prices"]
    entity_id = hass.data[DOMAIN]["config"].get("nordpool_entity_id")
    if entity_id is None:
        return {}
    start = dt_utils.start_of_local_day() - dt.timedelta(days=PRICE_HISTORY_DAYS)
    known = _hourly(prices)
    # Per day the plan's own copy, else the sensor's history, else Energy-Charts;
    # the sensor's unit, VAT and rounding and Energy-Charts' EUR are scaled to the
    # spot prices by the days both have
    sources = {"stored": await _stored_prices(hass)}
    others = {
        "recorder": await _price_history(hass, entity_id, start, now),
        "energy_charts": await _energy_charts_history(
            hass, nordpool_zone(entity_id)[0], start.date(), now.date()
        ),
    }
    for name, history in others.items():
        scale = scale_to(history, known, MIN_SCALE_HOURS) if history else None
        if history and scale is not None:
            sources[name] = {
                day: {hour: price * scale for hour, price in hours.items()}
                for day, hours in history.items()
            }
    if not any(set(history) - set(known) for history in sources.values()):
        return {}
    wind = await _wind_forecast(hass, day, lead)
    hourly = await hass.async_add_executor_job(
        functools.partial(
            estimate,
            merge(known, *sources.values()),
            day,
            # The prices after midnight start near tonight's
            prices[max(prices)],
            wind,
        )
    )
    if hourly is None:
        return {}
    fallback = mean(hourly.values())
    quarters = Timeline.between(
        dt_utils.start_of_local_day(day),
        dt_utils.start_of_local_day(day + dt.timedelta(days=1)),
        dt_utils.get_default_time_zone(),
    )
    estimated = {
        start: hourly.get(quarters.local_hour(index), fallback)
        for index, start in enumerate(quarters.starts)
    }
    hass.data[DOMAIN]["price_estimate"] = {
        "day": day,
        "lead": lead,
        "prices": estimated,
        "wind": wind is not None,
        # Days each source has (the merge takes them in this order)
        "sources": {name: len(history) for name, history in sources.items()},
    }
    _LOGGER.info(
        "Price estimate for %s: days per source %s, wind %s",
        day,
        hass.data[DOMAIN]["price_estimate"]["sources"],
        wind is not None,
    )
    return estimated
