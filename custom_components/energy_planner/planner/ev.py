"""Electric car charging: what the car needs, and what the charger should do now.

Pure functions, no Home Assistant imports. While the car is plugged in, the battery
plan (battery_plan.py) plans it together with the battery
(`battery_optimizer.Vehicle`): the cheapest quarters until the ready time.
`sensor.energy_planner_ev_action` says whether to charge now, and an automation
makes the charger follow it (examples/follow_ev_action_automation.yml).

The need is (charge limit - SOC) x battery capacity when the car reports both (e.g.
ESPHome Tesla BLE). Otherwise it is the largest recent charging night from the
charger's power history, minus what this session has delivered: overestimating is
cheap, as the car stops by itself when it is full and the rest of the planned
quarters pass without charging. Underestimating is not (the plan would pause a car
that is not full), so once a session has taken more than that the car is not
planned and charges as without the planner.
"""

import datetime as dt
from collections.abc import Mapping, Sequence

# Charger states that mean no car (others, e.g. Zaptec's connected_requesting,
# connected_charging, connected_finished, mean plugged in)
UNPLUGGED_STATES = ("disconnected", "unavailable", "unknown", "off", "none", "")
# A charging night runs from noon to noon, so evening and early morning charging
# for the same morning count together
NIGHT_START_HOUR = 12
NIGHT_MIN_KWH = 1.0  # less is not a charging night
# Of the charging nights; 1.0 = the largest. The 90th percentile (19 kWh at the
# parents' in Oct 2026, nights up to 33) would pause cars that are not full
NEED_PERCENTILE = 1.0
NEED_MIN_NIGHTS = 5
# Energy at the plug per kWh into the car's battery
CHARGE_EFFICIENCY = 0.9
# Without a fresh plan the car charges as it would without the planner
STALE_AFTER = dt.timedelta(minutes=30)
QUARTER = dt.timedelta(minutes=15)


def is_plugged(state: str | None) -> bool:
    """Return whether the charger state means a car is plugged in."""
    return state is not None and state.lower() not in UNPLUGGED_STATES


def charging_nights(
    hourly: Mapping[tuple[dt.date, int], float],
) -> dict[dt.date, float]:
    """Return the energy (kWh) charged per night, keyed by the night's first date.

    hourly maps (local date, hour) to kWh. Nights with less than NIGHT_MIN_KWH are
    left out.
    """
    nights: dict[dt.date, float] = {}
    for (day, hour), kwh in hourly.items():
        night = day if hour >= NIGHT_START_HOUR else day - dt.timedelta(days=1)
        nights[night] = nights.get(night, 0.0) + max(kwh, 0.0)
    return {night: kwh for night, kwh in nights.items() if kwh >= NIGHT_MIN_KWH}


def need_from_history(
    hourly: Mapping[tuple[dt.date, int], float], today: dt.date
) -> float | None:
    """Return the NEED_PERCENTILE of the ended charging nights (kWh).

    Last night (from yesterday's noon) may still run until noon today, so only
    nights before it count. None with fewer than NEED_MIN_NIGHTS nights.
    """
    last_ended = today - dt.timedelta(days=2)
    nights = sorted(
        kwh for night, kwh in charging_nights(hourly).items() if night <= last_ended
    )
    if len(nights) < NEED_MIN_NIGHTS:
        return None
    return nights[min(int(NEED_PERCENTILE * len(nights)), len(nights) - 1)]


def need_from_soc(soc: float, limit: float, capacity_kwh: float) -> float:
    """Return the energy at the plug (kWh) to charge from soc to limit (%)."""
    return max(limit - soc, 0.0) / 100 * capacity_kwh / CHARGE_EFFICIENCY


def deadline_index(starts: Sequence[dt.datetime], ready: dt.time) -> int:
    """Return the index of the first quarter at or after the next ready time.

    The next ready time is the first one after the first quarter's start, so the
    plan always aims at the coming morning. len(starts) when it is past the end.
    """
    if not starts:
        return 0
    first = starts[0]
    target = dt.datetime.combine(first.date(), ready, first.tzinfo)
    if target <= first:
        target = dt.datetime.combine(
            first.date() + dt.timedelta(days=1), ready, first.tzinfo
        )
    return next((i for i, start in enumerate(starts) if start >= target), len(starts))


def ev_action(plan: Mapping | None, now: dt.datetime, plugged: bool) -> dict:
    """Return whether the car should charge now.

    `charge` or `wait` from the plan's car power (source `plan`) while the plan is
    fresh and covers now; `charge` without one (source `fallback`, the car charges as
    without the planner); no state while unplugged (source `unplugged`).
    """
    if not plugged:
        return {"state": None, "source": "unplugged", "until": None, "power_kw": None}
    plan = plan or {}
    car = plan.get("ev")
    if car and now - dt.datetime.fromisoformat(plan["updated"]) <= STALE_AFTER:
        starts = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
        power = car["power"]
        index = next((i for i, s in enumerate(starts) if s <= now < s + QUARTER), None)
        if index is not None and index < len(power):
            charging = power[index] > 0
            last = index
            while last + 1 < len(power) and (power[last + 1] > 0) == charging:
                last += 1
            return {
                "state": "charge" if charging else "wait",
                "source": "plan",
                "until": (starts[last] + QUARTER).isoformat(),
                "power_kw": power[index],
            }
    return {"state": "charge", "source": "fallback", "until": None, "power_kw": None}
