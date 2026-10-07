"""What the battery should do now: the current slot, or the battery plan.

Pure functions, no Home Assistant imports. `resolve` combines the current slot (slot
1) and the battery plan into one action for the inverter script
(`sensor.energy_planner_battery_action`):

- An active slot with a real state (a manual slot, or a slot from the basic or price
  peak planner) wins: source `slot`.
- An active `auto` slot follows the battery plan: source `plan`. If the plan is
  missing, older than `STALE_AFTER` or does not cover now, the battery falls back to
  self-use: source `fallback`.
- Otherwise (no slot, `off`, disabled) self-use: source `none`.

The state is always a slot state the inverter scripts know (`charge`, `discharge`,
`sell`, `sell-excess`, `pause`, `discard-excess`).
"""

import dataclasses
import datetime as dt
import math
from typing import Any

# Battery plan mode -> slot state
SLOT_STATES = {
    "self_use": "discharge",
    "hold": "pause",
    "sell_excess": "sell-excess",
    "charge": "charge",
    "sell": "sell",
}
# Slot state -> battery plan mode, for planning around manual slots
SLOT_MODES = {
    "charge": "charge",
    "discharge": "self_use",
    "sell": "sell",
    "sell-excess": "sell_excess",
    "pause": "hold",
    "discard-excess": "self_use",
}
STALE_AFTER = dt.timedelta(minutes=30)
QUARTER = dt.timedelta(minutes=15)


@dataclasses.dataclass(frozen=True)
class Slot:
    """The current slot (slot 1); `end` is the next slot's start."""

    state: str
    active: bool
    start: dt.datetime | None
    end: dt.datetime | None
    soc: float


@dataclasses.dataclass(frozen=True)
class BatteryState:
    """The live battery state and the settings the action needs."""

    soc: float  # %
    voltage: float  # V
    capacity_kwh: float
    min_soc: float  # %
    max_soc: float  # %
    max_charge_a: float
    max_discharge_a: float


def _action(state: str, source: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": state,
        "source": source,
        "mode": None,
        "soc": None,
        "current_a": None,
        "import_target_kw": None,
        "until": None,
        **extra,
    }


def _current(kwh: float, until: dt.datetime, now: dt.datetime, battery, limit):
    """Return the battery current in A that moves `kwh` by `until`, at most `limit`."""
    hours = max((until - now).total_seconds() / 3600, 1 / 60)
    if kwh <= 0 or battery.voltage <= 0:
        return 0
    return min(math.ceil(kwh / hours * 1000 / battery.voltage), int(limit))


def plan_action(
    plan: dict[str, Any] | None, now: dt.datetime, battery: BatteryState
) -> dict[str, Any]:
    """Return the battery plan's action now, or a self-use fallback."""
    if not plan or not plan.get("starts"):
        return _action("discharge", "fallback", reason="no plan")
    updated = dt.datetime.fromisoformat(plan["updated"])
    if now - updated > STALE_AFTER:
        return _action("discharge", "fallback", reason="plan is old")
    starts = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
    index = next(
        (i for i, s in enumerate(starts) if s <= now < s + QUARTER),
        None,
    )
    if index is None:
        return _action("discharge", "fallback", reason="plan does not cover now")
    modes = plan["modes"]
    mode = modes[index]
    last = index
    while last + 1 < len(modes) and modes[last + 1] == mode:
        last += 1
    until = starts[last] + QUARTER
    target_soc = plan["soc"][last]
    capacity = battery.capacity_kwh / 100  # kWh per %
    extra: dict[str, Any] = {"soc": None, "current_a": 0}
    if mode == "charge":
        extra["soc"] = round(target_soc)
        extra["current_a"] = _current(
            (target_soc - battery.soc) * capacity,
            until,
            now,
            battery,
            battery.max_charge_a,
        )
    elif mode == "sell":
        extra["soc"] = round(target_soc)
        extra["current_a"] = _current(
            (battery.soc - target_soc) * capacity,
            until,
            now,
            battery,
            battery.max_discharge_a,
        )
    elif mode == "sell_excess":
        extra["soc"] = round(battery.min_soc)
    elif mode == "hold":
        extra["soc"] = round(battery.max_soc)
    return _action(
        SLOT_STATES[mode],
        "plan",
        mode=mode,
        import_target_kw=plan["targets"][index],
        until=until.isoformat(),
        plan_updated=plan["updated"],
        **extra,
    )


def resolve(
    slot: Slot | None,
    plan: dict[str, Any] | None,
    now: dt.datetime,
    battery: BatteryState,
) -> dict[str, Any]:
    """Return the action now from the current slot and the battery plan."""
    running = (
        slot is not None
        and slot.active
        and slot.start is not None
        and slot.start <= now
        and slot.state != "off"
    )
    if not running or slot is None:
        return _action("discharge", "none")
    if slot.state == "auto":
        return plan_action(plan, now, battery)
    current = {"charge": battery.max_charge_a, "sell": battery.max_discharge_a}
    return _action(
        slot.state,
        "slot",
        mode=SLOT_MODES.get(slot.state),
        soc=round(slot.soc),
        current_a=int(current.get(slot.state, 0)),
        until=slot.end.isoformat() if slot.end else None,
    )
