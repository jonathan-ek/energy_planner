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
    write_schedule,
)
from ..const import DOMAIN
from homeassistant.util import dt as dt_utils

_LOGGER = logging.getLogger(__name__)

# Nord Pool prices are SEK/MWh excluding VAT, the network settings are öre/kWh
# including VAT (1 öre/kWh = 10 SEK/MWh)
VAT = 1.25
ORE_PER_KWH_IN_SEK_PER_MWH = 10
# Expensive-hour states where the discharged energy is sold instead of used at home
SELLING_STATES = ("sell", "sell-excess")


def is_profitable(
    charge_price,
    discharge_price,
    efficiency,
    network_cost,
    network_compensation,
    sells,
):
    """Return whether storing energy from charge_price for discharge_price pays off.

    Buying costs the spot price with VAT plus the network cost (transfer fee and
    energy tax), and 1 / efficiency kWh has to be bought per kWh delivered. A
    discharged kWh is worth the purchase it replaces when it is used at home, or the
    spot price plus the network compensation when it is sold (private sellers get no
    VAT). For use at home the network cost is paid either way, so only the part lost
    in the battery counts.
    """
    fees = network_cost * ORE_PER_KWH_IN_SEK_PER_MWH
    cost = (charge_price * VAT + fees) / efficiency
    if sells:
        value = discharge_price + network_compensation * ORE_PER_KWH_IN_SEK_PER_MWH
    else:
        value = discharge_price * VAT + fees
    return value > cost


def remove_overlaps(size, charge_periods, discharge_periods):
    """Drop periods that overlap a better ranked one; returns the remaining lists.

    Charge and discharge periods are taken in turn by rank. A dropped period leaves
    no marks behind.
    """
    used = [False] * size
    charge_periods = list(charge_periods)
    discharge_periods = list(discharge_periods)
    for i in range(max(len(charge_periods), len(discharge_periods))):
        for periods in (charge_periods, discharge_periods):
            if i >= len(periods):
                continue
            if any(used[j] for j in periods[i]):
                periods[i] = []
            else:
                for j in periods[i]:
                    used[j] = True
    return [x for x in charge_periods if x], [x for x in discharge_periods if x]


def match_charge_discharge_periods(
    prices, charge_periods, discharge_periods, profitable
):
    """Pair each discharge period with an earlier charge period where it pays off.

    The periods are ranked best first. profitable(charge_price, discharge_price) tells
    whether a pair pays off. Returns a list of (charge indexes, discharge indexes).
    """
    matched_pairs = []
    charge_periods, discharge_periods = remove_overlaps(
        len(prices), charge_periods, discharge_periods
    )
    slots = [0] * len(prices)
    for cp, c in enumerate(charge_periods):
        for i in c:
            slots[i] += cp + 1
    for dp, d in enumerate(discharge_periods):
        for i in d:
            slots[i] -= dp + 1
    to_remove = []
    last_period = None
    # Of two charge (or discharge) periods in a row, keep the better ranked one
    for i in range(len(slots)):
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
    # (first quarter, average price, kind, rank) in time order
    periods = [
        (min(c), sum(prices[i] for i in c) / len(c), "c", j)
        for j, c in enumerate(charge_periods)
    ] + [
        (min(d), sum(prices[i] for i in d) / len(d), "d", j)
        for j, d in enumerate(discharge_periods)
    ]
    periods.sort()
    prev_price = None
    prev_index = None
    to_remove = []
    for _, p, t, i in periods:
        if t == "c":
            prev_price = p
            prev_index = i
            continue
        if prev_index is None:
            # Nothing charged before this discharge period
            continue
        # Only the quarters that pay off on their own
        new_dp = [x for x in discharge_periods[i] if profitable(prev_price, prices[x])]
        if profitable(prev_price, p) and new_dp:
            matched_pairs.append((charge_periods[prev_index], new_dp))
            prev_price = None
            prev_index = None
            continue
        # Drop the worse ranked of the two (higher rank number) and try again
        if i > prev_index:
            to_remove.append((i, "d"))
        else:
            to_remove.append((prev_index, "c"))
        break
    if len(to_remove) > 0:
        for r, t in to_remove:
            if t == "d":
                discharge_periods[r] = []
            elif t == "c":
                charge_periods[r] = []
        charge_periods = [x for x in charge_periods if x]
        discharge_periods = [x for x in discharge_periods if x]
        matched_pairs = match_charge_discharge_periods(
            prices, charge_periods, discharge_periods, profitable
        )

    return matched_pairs


# Quarters around a window searched for its best quarters
CONTEXT = 2


def _best_quarters(prices, start_idx, size, highest):
    """Return the size best quarters around a window, in time order."""
    expanded_start = max(0, start_idx - CONTEXT)
    expanded_end = min(len(prices), start_idx + size + CONTEXT)
    window = prices[expanded_start:expanded_end]
    ranked = sorted(
        range(len(window)), key=lambda i: -window[i] if highest else window[i]
    )
    return [expanded_start + i for i in sorted(ranked[:size])]


def find_discharge_periods(prices, charge_window_size, discharge_window_size):
    """Return the most expensive non-overlapping windows, best first."""
    if discharge_window_size <= 0 or len(prices) < discharge_window_size:
        return []
    used_indices = set()
    discharge_period_indexes = []
    candidates = [
        (sum(prices[i : i + discharge_window_size]), i)
        for i in range(len(prices) - discharge_window_size + 1)
    ]
    candidates.sort(reverse=True, key=lambda x: x[0])
    for _, start_idx in candidates:
        window_range = set(range(start_idx, start_idx + discharge_window_size))
        if not used_indices.isdisjoint(window_range):
            continue
        # Block the price slope leading up to the peak
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
        # Block a charge window on each side of the peak
        used_indices.update(
            range(
                max(0, start_idx - charge_window_size),
                min(
                    len(prices),
                    start_idx + discharge_window_size + charge_window_size,
                ),
            )
        )
        # Block the price slope after the peak
        end = start_idx + discharge_window_size
        if end < len(prices):
            j = 0
            price = prices[end]
            while True:
                j += 1
                if end + j >= len(prices):
                    break
                if prices[end + j] > price:
                    if end + j + 1 >= len(prices):
                        break
                    if prices[end + j + 1] > price:
                        if end + j + 2 >= len(prices):
                            break
                        if prices[end + j + 2] > price:
                            break
                price = prices[end + j]
                used_indices.add(end + j)
        discharge_period_indexes.append(start_idx)
    return [
        _best_quarters(prices, start_idx, discharge_window_size, highest=True)
        for start_idx in discharge_period_indexes
    ]


def find_charge_periods(prices, charge_window_size, discharge_window_size):
    """Return the cheapest non-overlapping windows, best first."""
    if charge_window_size <= 0 or len(prices) < charge_window_size:
        return []
    used_indices = set()
    charge_period_indexes = []
    candidates = [
        (sum(prices[i : i + charge_window_size]), i)
        for i in range(len(prices) - charge_window_size + 1)
    ]
    candidates.sort(key=lambda x: x[0])
    for _, start_idx in candidates:
        window_range = set(range(start_idx, start_idx + charge_window_size))
        if not used_indices.isdisjoint(window_range):
            continue
        # Block the price slope leading down to the dip
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
        # Block a discharge window on each side of the dip
        used_indices.update(
            range(
                max(0, start_idx - discharge_window_size),
                min(
                    len(prices),
                    start_idx + discharge_window_size + charge_window_size,
                ),
            )
        )
        # Block the price slope after the dip
        end = start_idx + charge_window_size
        if end < len(prices):
            j = 0
            price = prices[end]
            while True:
                j += 1
                if end + j >= len(prices):
                    break
                if prices[end + j] < price:
                    if end + j + 1 >= len(prices):
                        break
                    if prices[end + j + 1] < price:
                        if end + j + 2 >= len(prices):
                            break
                        if prices[end + j + 2] < price:
                            break
                price = prices[end + j]
                used_indices.add(end + j)
        charge_period_indexes.append(start_idx)
    return [
        _best_quarters(prices, start_idx, charge_window_size, highest=False)
        for start_idx in charge_period_indexes
    ]


def build_schedule(nordpool_values, matched, states, max_soc, min_soc):
    """Turn matched periods into schedule slots, merging equal neighbours.

    states is (cheap, expensive, inbetween) state.
    """
    if not nordpool_values:
        return []
    cheap_state, expensive_state, inbetween_state = states
    slots = ["p" for _ in range(len(nordpool_values))]
    for c, d in matched:
        for i in c:
            slots[i] = "c"
        for i in d:
            slots[i] = "d"
    kinds = {
        "c": (cheap_state, max_soc),
        "d": (expensive_state, min_soc),
        "p": (inbetween_state, max_soc),
    }
    schedule = []
    prev = None
    for i, slot in enumerate(slots):
        if slot == prev:
            continue
        prev = slot
        if schedule:
            schedule[-1]["end"] = nordpool_values[i]["start"]
        state, soc = kinds[slot]
        schedule.append(
            {"start": nordpool_values[i]["start"], "state": state, "soc": soc}
        )
    schedule[-1]["end"] = nordpool_values[-1]["end"]
    return schedule


async def plan_day(hass: HomeAssistant, nordpool_values: list[dict], config: dict):
    """Plan a day based on nordpool values."""
    _LOGGER.info("plan_day: %s", nordpool_values)
    settings = hass.data[DOMAIN]["config"]
    charge_hours = float(settings.get("price_peak_nr_of_charge_hours", 2))
    discharge_hours = float(settings.get("price_peak_nr_of_discharge_hours", 2))
    efficiency = float(settings.get("price_peak_efficiency_factor", 85)) / 100
    network_cost = float(settings.get("network_cost", 0.0))
    network_compensation = float(settings.get("network_compensation", 0.0))
    states = (
        settings.get("price_peak_planner_cheap_state", "charge"),
        settings.get("price_peak_planner_expensive_state", "discharge"),
        settings.get("price_peak_planner_inbetween_state", "pause"),
    )
    max_soc = settings.get("battery_max_soc", 100)
    min_soc = settings.get("battery_shutdown_soc", 20)

    def profitable(charge_price, discharge_price):
        return is_profitable(
            charge_price,
            discharge_price,
            efficiency,
            network_cost,
            network_compensation,
            sells=states[1] in SELLING_STATES,
        )

    prices = [x["value"] for x in nordpool_values]
    charge_window_size = int(charge_hours * 4)  # quarters
    discharge_window_size = int(discharge_hours * 4)
    discharge_periods = find_discharge_periods(
        prices, charge_window_size, discharge_window_size
    )
    _LOGGER.info("discharge_periods: %s", discharge_periods)
    charge_periods = find_charge_periods(
        prices, charge_window_size, discharge_window_size
    )
    _LOGGER.info("charge_periods: %s", charge_periods)
    matched = match_charge_discharge_periods(
        prices, charge_periods, discharge_periods, profitable
    )
    schedule = build_schedule(nordpool_values, matched, states, max_soc, min_soc)
    now = dt_utils.now()
    # remove past hours
    schedule = [x for x in schedule if x["end"] > now]
    _LOGGER.info("schedule: %s", schedule)
    write_schedule(hass, schedule)


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
        hass, nordpool_currency, nordpool_area, bool(tomorrow_valid)
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
