import logging


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


def match_charge_discharge_periods(
    prices, charge_periods, discharge_periods, price_peak_efficiency_factor
):
    """Match charge and discharge periods."""
    matched_pairs = []
    slots = [0] * len(prices)

    # remove overlapping periods
    for i in range(max(len(charge_periods), len(discharge_periods))):
        if i < len(charge_periods):
            charge_period = charge_periods[i]
            for j in charge_period:
                if slots[j] != 0:
                    charge_periods[i] = []
                    break
                slots[j] += 1
        if i < len(discharge_periods):
            discharge_period = discharge_periods[i]
            for j in discharge_period:
                if slots[j] != 0:
                    discharge_periods[i] = []
                    break
                slots[j] -= 1
    charge_periods = [x for x in charge_periods if x]
    discharge_periods = [x for x in discharge_periods if x]
    slots = [0] * len(prices)
    for cp, c in enumerate(charge_periods):
        for i in c:
            slots[i] += cp + 1
    for dp, d in enumerate(discharge_periods):
        for i in d:
            slots[i] -= dp + 1
    to_remove = []
    last_period = None
    for i in range(len(slots)):
        if i == 0:
            continue
        if slots[i] > 0 and slots[i] != last_period:
            if last_period is None:
                last_period = slots[i]
                continue
            if last_period < 0:
                last_period = slots[i]
                continue
            if last_period > slots[i]:
                to_remove.append(last_period)
                last_period = slots[i]
            elif last_period < slots[i]:
                to_remove.append(slots[i])
                last_period = slots[i]
            else:
                last_period = slots[i]
                continue
        elif slots[i] < 0 and slots[i] != last_period:
            if last_period is None:
                last_period = slots[i]
                continue
            if last_period > 0:
                last_period = slots[i]
                continue
            if last_period < slots[i]:
                to_remove.append(last_period)
                last_period = slots[i]
            elif last_period > slots[i]:
                to_remove.append(slots[i])
                last_period = slots[i]
            else:
                last_period = slots[i]
                continue
    for r in to_remove:
        if r < 0:
            discharge_periods[abs(r) - 1] = []
        elif r > 0:
            charge_periods[r - 1] = []
    charge_periods = [x for x in charge_periods if x]
    discharge_periods = [x for x in discharge_periods if x]
    slots = [0] * len(prices)
    for j, c in enumerate(charge_periods):
        slots[min(c)] = (sum(prices[i] for i in c) / len(c), "c", j)
    for j, d in enumerate(discharge_periods):
        slots[min(d)] = (sum(prices[i] for i in d) / len(d), "d", j)
    slots = [x for x in slots if x != 0]
    prev_price = None
    prev_index = None
    to_remove = []
    for p, t, i in slots:
        if prev_price is None:
            prev_price = p
            prev_index = i
            continue
        if t == "d":
            if prev_price * price_peak_efficiency_factor < p:
                matched_pairs.append((charge_periods[prev_index], discharge_periods[i]))
                prev_price = None
                prev_index = None
            else:
                if i > prev_index:
                    to_remove.append((i, "d"))
                else:
                    to_remove.append((prev_index, "c"))
                break
        elif t == "c":
            prev_price = p
            prev_index = i
    if len(to_remove) > 0:
        for r, t in to_remove:
            if t == "d":
                discharge_periods[r] = []
            elif t == "c":
                charge_periods[r] = []
        charge_periods = [x for x in charge_periods if x]
        discharge_periods = [x for x in discharge_periods if x]
        matched_pairs = match_charge_discharge_periods(
            prices, charge_periods, discharge_periods, price_peak_efficiency_factor
        )

    return matched_pairs


async def plan_day(hass: HomeAssistant, nordpool_values: [dict], config: dict):
    """Plan a day based on nordpool values."""
    _LOGGER.info("plan_day: %s", nordpool_values)
    charge_hours = float(
        hass.data[DOMAIN]["config"].get("price_peak_nr_of_charge_hours", 2)
    )
    discharge_hours = float(
        hass.data[DOMAIN]["config"].get("price_peak_nr_of_discharge_hours", 2)
    )
    price_peak_efficiency_factor = (
        float(hass.data[DOMAIN]["config"].get("price_peak_efficiency_factor", 85)) / 100
    )
    price_peak_planner_cheap_state = hass.data[DOMAIN]["config"].get(
        "price_peak_planner_cheap_state", "charge"
    )
    price_peak_planner_expensive_state = hass.data[DOMAIN]["config"].get(
        "price_peak_planner_expensive_state", "discharge"
    )
    price_peak_planner_inbetween_state = hass.data[DOMAIN]["config"].get(
        "price_peak_planner_inbetween_state", "pause"
    )

    prices = [x["value"] for x in nordpool_values]
    charge_window_size = int(charge_hours * 4)  # 2 hours * 4 (15 min intervals)
    discharge_window_size = int(discharge_hours * 4)
    used_indices = set()
    discharge_period_indexes = []

    # Store all candidate windows with their sum and starting index
    discharge_candidates = []
    for i in range(len(prices) - discharge_window_size + 1):
        window = prices[i : i + discharge_window_size]
        total_price = sum(window)
        discharge_candidates.append((total_price, i))
    discharge_candidates.sort(reverse=True, key=lambda x: x[0])
    for _, start_idx in discharge_candidates:
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
            used_indices.update(
                range(
                    max(0, start_idx - charge_window_size),
                    min(
                        len(prices),
                        start_idx + discharge_window_size + charge_window_size,
                    ),
                )
            )
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

        sorted_indices = sorted(
            range(len(expanded_window)), key=lambda i: -expanded_window[i]
        )
        top_8_global = [
            expanded_start + i for i in sorted(sorted_indices[:discharge_window_size])
        ]
        discharge_periods.append(top_8_global)
    _LOGGER.info("discharge_periods: %s", discharge_periods)
    charge_periods = []
    used_indices = set()
    charge_period_indexes = []

    # Store all candidate windows with their sum and starting index
    charge_candidates = []
    for i in range(len(prices) - charge_window_size + 1):
        window = prices[i : i + charge_window_size]
        total_price = sum(window)
        charge_candidates.append((total_price, i))
    charge_candidates.sort(reverse=False, key=lambda x: x[0])
    for _, start_idx in charge_candidates:
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
            used_indices.update(
                range(
                    max(0, start_idx - discharge_window_size),
                    min(
                        len(prices),
                        start_idx + discharge_window_size + charge_window_size,
                    ),
                )
            )
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

        sorted_indices = sorted(
            range(len(expanded_window)), key=lambda i: expanded_window[i]
        )
        top_8_global = [
            expanded_start + i for i in sorted(sorted_indices[:charge_window_size])
        ]
        charge_periods.append(top_8_global)
    _LOGGER.info("charge_periods: %s", charge_periods)
    matched = match_charge_discharge_periods(
        prices, charge_periods, discharge_periods, price_peak_efficiency_factor
    )
    slots = ["p" for _ in range(len(prices))]
    for c, d in matched:
        for i in c:
            slots[i] = "c"
        for i in d:
            slots[i] = "d"
    _LOGGER.info("slots: %s", slots)
    schedule = [{}]
    prev = None
    for i, slot in enumerate(slots):
        if slot == prev:
            continue
        prev = slot
        if slot == "c":
            schedule[-1]["end"] = nordpool_values[i]["start"]
            schedule.append(
                {
                    "start": nordpool_values[i]["start"],
                    "state": price_peak_planner_cheap_state,
                    "soc": 100,
                }
            )
        elif slot == "d":
            schedule[-1]["end"] = nordpool_values[i]["start"]
            schedule.append(
                {
                    "start": nordpool_values[i]["start"],
                    "state": price_peak_planner_expensive_state,
                    "soc": 0,
                }
            )
        else:
            schedule[-1]["end"] = nordpool_values[i]["start"]
            schedule.append(
                {
                    "start": nordpool_values[i]["start"],
                    "state": price_peak_planner_inbetween_state,
                    "soc": 100,
                }
            )
    schedule[-1]["end"] = nordpool_values[-1]["end"]
    schedule.pop(0)
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

    _LOGGER.info("matched charge/discharge periods: %s", schedule)


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
        if start_of_day <= parse_datetime(x["start"], zone)
    ]

    await plan_day(hass, data, config)

    await add_manual_slots(hass)
    await restore_disable_state(hass)
    await update_entities(hass)
    await hass.data[DOMAIN]["save"]()
