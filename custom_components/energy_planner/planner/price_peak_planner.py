import logging
import datetime as dt

from collections import defaultdict
from homeassistant.core import HomeAssistant

from .manual_slots import add_manual_slots
from .nordpool_utils import fetch_nordpool_data, tzs
from .utils import (
    update_entities,
    parse_datetime,
    reset,
    store_disable_state,
    restore_disable_state,
)
from ..const import DOMAIN
from homeassistant.util import dt as dt_utils

_LOGGER = logging.getLogger(__name__)

def match_charge_discharge_periods(prices, charge_periods, discharge_periods):
    matched_pairs = []

    for discharge in discharge_periods:
        discharge_start = min(discharge)
        discharge_avg_price = sum(prices[i] for i in discharge) / len(discharge)

        for idx, charge in enumerate(charge_periods):
            charge_end = max(charge)
            charge_avg_price = sum(prices[i] for i in charge) / len(charge)

            if charge_end < discharge_start and charge_avg_price*0.85 < discharge_avg_price:
                matched_pairs.append((charge, discharge))
    matched_pairs = sorted(matched_pairs, key=lambda x: (x[1], -x[0][0]), reverse=True)
    matched_pairs = select_best_pairs(prices, matched_pairs)
    return matched_pairs

def select_best_pairs(prices, all_matches):

    # Step 1: Build maps of discharge -> [charge options]
    discharge_to_charges = defaultdict(list)
    for charge, discharge in all_matches:
        discharge_key = tuple(discharge)
        discharge_to_charges[discharge_key].append(charge)

    # Step 2: Sort discharges by:
    # 1. Fewest matching charges
    # 2. Highest discharge average price
    sorted_discharges = sorted(
        discharge_to_charges.keys(),
        key=lambda d: (len(discharge_to_charges[d]), -sum(prices[i] for i in d) / len(d))
    )

    used_charges = set()
    used_discharges = set()
    selected_pairs = []

    for discharge in sorted_discharges:
        if tuple(discharge) in used_discharges:
            continue

        # Try to find first available charge
        for charge in discharge_to_charges[discharge]:
            charge_key = tuple(charge)
            if charge_key in used_charges:
                continue

            # Found a valid unused pair
            selected_pairs.append((charge, list(discharge)))
            used_charges.add(charge_key)
            used_discharges.add(tuple(discharge))
            break  # move to next discharge

    return selected_pairs

async def plan_day(hass: HomeAssistant, nordpool_values: [dict], config: dict):
    _LOGGER.info("plan_day: %s", nordpool_values)
    charge_hours = float(hass.data[DOMAIN]["config"].get('price_peak_nr_of_charge_hours', 2))
    discharge_hours = float(hass.data[DOMAIN]["config"].get('price_peak_nr_of_discharge_hours', 2))
    prices = [x["value"] for x in nordpool_values]
    charge_window_size = int(charge_hours*4)  # 2 hours * 4 (15 min intervals)
    discharge_window_size = int(discharge_hours*4)
    used_indices = set()
    discharge_period_indexes = []

    # Store all candidate windows with their sum and starting index
    discharge_candidates = []
    for i in range(len(prices) - discharge_window_size + 1):
        window = prices[i:i + discharge_window_size]
        total_price = sum(window)
        discharge_candidates.append((total_price, i))
    discharge_candidates.sort(reverse=True, key=lambda x: x[0])
    for total, start_idx in discharge_candidates:
        # Check for overlap
        window_range = set(range(start_idx, start_idx + discharge_window_size))
        if used_indices.isdisjoint(window_range):
            # Mark this window's indices as used and
            # block discharge hours before and after charge period
            j = 0
            price = prices[start_idx]
            while True:
                j += 1
                if start_idx - j < 0:
                    break
                if prices[start_idx - j] > price:
                    if start_idx - j - 1 < 0:
                        break
                    if prices[start_idx - j - 1] > price:
                        if start_idx - j - 2 < 0:
                            break
                        if prices[start_idx - j - 2] > price:
                            break
                price = prices[start_idx - j]
                used_indices.add(start_idx - j)
            used_indices.update(range(max(0, start_idx-charge_window_size), min(len(prices), start_idx + discharge_window_size + charge_window_size)))
            j = 0
            price = prices[start_idx + discharge_window_size + j]
            while True:
                j += 1
                if start_idx + discharge_window_size + j >= len(prices):
                    break
                if prices[start_idx + discharge_window_size + j] > price:
                    if start_idx + discharge_window_size + j + 1 >= len(prices):
                        break
                    if prices[start_idx + discharge_window_size + j + 1] > price:
                        if start_idx + discharge_window_size + j + 2 >= len(prices):
                            break
                        if prices[start_idx + discharge_window_size + j + 2] > price:
                            break
                price = prices[start_idx + discharge_window_size + j]
                used_indices.add(start_idx + discharge_window_size + j)
            discharge_period_indexes.append(start_idx)
    context = 2  # 1 hour context to find top 8 prices
    discharge_periods = []
    for start_idx in discharge_period_indexes:
        expanded_start = max(0, start_idx - context)
        expanded_end = min(len(prices), start_idx + discharge_window_size + context)
        expanded_window = prices[expanded_start:expanded_end]

        sorted_indices = sorted(range(len(expanded_window)), key=lambda i: -expanded_window[i])
        top_8_global = [expanded_start + i for i in sorted(sorted_indices[:discharge_window_size])]
        discharge_periods.append(top_8_global)
    _LOGGER.info("discharge_periods: %s", discharge_periods)
    charge_periods = []
    used_indices = set()
    charge_period_indexes = []

    # Store all candidate windows with their sum and starting index
    charge_candidates = []
    for i in range(len(prices) - charge_window_size + 1):
        window = prices[i:i + charge_window_size]
        total_price = sum(window)
        charge_candidates.append((total_price, i))
    charge_candidates.sort(reverse=False, key=lambda x: x[0])
    for total, start_idx in charge_candidates:
        # Check for overlap
        window_range = set(range(start_idx, start_idx + charge_window_size))
        if used_indices.isdisjoint(window_range):
            # Mark this window's indices as used and
            # block discharge hours before and after charge period
            j = 0
            price = prices[start_idx]
            while True:
                j += 1
                if start_idx - j < 0:
                    break
                if prices[start_idx - j] < price:
                    if start_idx - j - 1 < 0:
                        break
                    if prices[start_idx - j - 1] < price:
                        if start_idx - j - 2 < 0:
                            break
                        if prices[start_idx - j - 2] < price:
                            break
                price = prices[start_idx - j]
                used_indices.add(start_idx - j)
            used_indices.update(range(max(0, start_idx-discharge_window_size), min(len(prices), start_idx + discharge_window_size + charge_window_size)))
            j = 0
            if start_idx + charge_window_size < len(prices):
                price = prices[start_idx + charge_window_size]
                while True:
                    j += 1
                    if start_idx + charge_window_size + j >= len(prices):
                        break
                    if prices[start_idx + charge_window_size + j] < price:
                        if start_idx + charge_window_size + j + 1 >= len(prices):
                            break
                        if prices[start_idx + charge_window_size + j + 1] < price:
                            if start_idx + charge_window_size + j + 2 >= len(prices):
                                break
                            if prices[start_idx + charge_window_size + j + 2] < price:
                                break
                    price = prices[start_idx + charge_window_size + j]
                    used_indices.add(start_idx + charge_window_size + j)
            charge_period_indexes.append(start_idx)
    context = 2  # 1 hour context to find top 8 prices
    charge_periods = []
    for start_idx in charge_period_indexes:
        expanded_start = max(0, start_idx - context)
        expanded_end = min(len(prices), start_idx + charge_window_size + context)
        expanded_window = prices[expanded_start:expanded_end]

        sorted_indices = sorted(range(len(expanded_window)), key=lambda i: expanded_window[i])
        top_8_global = [expanded_start + i for i in sorted(sorted_indices[:charge_window_size])]
        charge_periods.append(top_8_global)
    _LOGGER.info("charge_periods: %s", charge_periods)
    matched = match_charge_discharge_periods(prices, charge_periods, discharge_periods)
    _LOGGER.info("matched charge/discharge periods: %s", matched)

async def planner(hass: HomeAssistant, *args, **kwargs):
    """Run planner."""
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
    }
    days = [*yesterday, *today]
    if tomorrow is not None:
        days = [*yesterday, *today, *tomorrow]

    data = [
        {
            "start": parse_datetime(x["start"], zone),
            "end": parse_datetime(x["end"], zone),
            "value": x["value"],
        }
        for x in days
        if start_of_day
           <= parse_datetime(x["start"], zone)
    ]

    await plan_day(hass, data, config)

    await add_manual_slots(hass)
    await restore_disable_state(hass)
    await update_entities(hass)
    await hass.data[DOMAIN]["save"]()