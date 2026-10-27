"""What the battery should do now: a manual slot, or the battery plan.

Pure functions, no Home Assistant imports. `resolve` combines the manual slot
covering now (manual.py) and the battery plan into one action for the inverter
script (`sensor.energy_planner_battery_action`):

- A manual slot wins: source `manual`, also without a plan.
- With the planner on, the battery follows the plan: source `plan`. If the plan is
  missing, stale (plan.py) or does not cover now, the battery falls back to
  self-use: source `fallback`.
- With the planner off, self-use: source `none`.

The plan keeps the battery above the reserve SOC. When the battery is at or below it
anyway (more load than forecast), the plan's `self_use`, `sell_excess` and `hold` turn
into `hold` with the power charge level as import target: the battery then only
shaves peaks that would raise the power charge, down to the shutdown SOC.

The state is always an action state the inverter scripts know (`charge`,
`discharge`, `sell`, `sell-excess`, `pause`, `discard-excess`).
"""

import dataclasses
import datetime as dt
import math
from typing import Any

from .manual import STATE_MODES, ManualSlot, SocLimits, target
from .plan import Plan, action, follow

# Battery plan mode -> action state
ACTION_STATES = {
    "self_use": "discharge",
    "hold": "pause",
    "sell_excess": "sell-excess",
    "discard_excess": "discard-excess",
    "charge": "charge",
    "sell": "sell",
}


@dataclasses.dataclass(frozen=True)
class BatteryState:
    """The live battery state and the settings the action needs."""

    soc: float | None  # %, None: no reading (the plan's expected SOC is used)
    voltage: float  # V
    capacity_kwh: float
    min_soc: float  # %
    max_soc: float  # %
    max_charge_a: float
    max_discharge_a: float
    reserve_soc: float = 0.0  # %, the plan keeps it, peak shaving may use it


def _action(
    state: str,
    source: str,
    *,
    mode: str | None = None,
    soc: float | None = None,
    current_a: int | None = None,
    import_target_kw: float | None = None,
    **common: Any,
) -> dict[str, Any]:
    """Return a battery action: the common keys (plan.action) and the battery's."""
    return action(
        state,
        source,
        mode=mode,
        soc=soc,
        current_a=current_a,
        import_target_kw=import_target_kw,
        **common,
    )


def _current(kwh: float, until: dt.datetime, now: dt.datetime, battery, limit):
    """Return the battery current in A that moves `kwh` by `until`, at most `limit`."""
    # Timestamps: local times in the hour repeated when DST ends subtract by wall clock
    hours = max((until.timestamp() - now.timestamp()) / 3600, 1 / 60)
    if kwh <= 0 or battery.voltage <= 0:
        return 0
    return min(math.ceil(kwh / hours * 1000 / battery.voltage), int(limit))


def _modes_and_manual(plan: Plan) -> list[tuple[str, str | None]]:
    """Return the plan's mode and forced manual mode per quarter."""
    manual = plan.manual or (None,) * len(plan.modes)
    return list(zip(plan.modes, manual, strict=True))


def plan_action(
    plan: Plan | None, now: dt.datetime, battery: BatteryState
) -> dict[str, Any]:
    """Return the battery plan's action now, or a self-use fallback.

    The run ends where a manual slot's quarters begin or end, even with the same
    mode: its target and current are the plan's own up to there.
    """
    run, reason = follow(plan, now, _modes_and_manual)
    if run is not None and run.value[1] is not None:
        # Planned for a manual slot that has been removed since; not to follow
        run, reason = None, "manual slot removed"
    if run is None or plan is None:
        return _action("discharge", "fallback", plan=plan, reason=reason)
    index = run.index
    mode = run.value[0]
    # Without a reading, the SOC the plan expects: 0 would look like an empty battery
    soc = battery.soc if battery.soc is not None else plan.soc_at(now)
    if soc is None:
        soc = plan.soc[index]
    until = run.until
    target_soc = plan.soc[run.last]
    capacity = battery.capacity_kwh / 100  # kWh per %
    extra: dict[str, Any] = {"soc": None, "current_a": 0}
    target_kw = plan.targets[index]
    reserve = max(battery.min_soc, battery.reserve_soc)
    if (
        mode in ("self_use", "sell_excess", "hold")
        and battery.reserve_soc > battery.min_soc
        and soc <= reserve
    ):
        # Keep the reserve for peaks; no power charge: just keep it
        mode = "hold"
        target_soc = reserve
        target_kw = plan.peak_levels[index]
        extra["reserve"] = True
    if mode == "charge":
        extra["soc"] = round(target_soc)
        extra["current_a"] = _current(
            (target_soc - soc) * capacity,
            until,
            now,
            battery,
            battery.max_charge_a,
        )
    elif mode == "sell":
        extra["soc"] = round(target_soc)
        extra["current_a"] = _current(
            (soc - target_soc) * capacity,
            until,
            now,
            battery,
            battery.max_discharge_a,
        )
    elif mode == "sell_excess":
        extra["soc"] = round(reserve)
    elif mode == "hold":
        # What the plan expects to keep (less if it shaves peaks)
        extra["soc"] = round(target_soc)
    return _action(
        ACTION_STATES[mode],
        "plan",
        mode=mode,
        import_target_kw=target_kw,
        until=until,
        plan=plan,
        **extra,
    )


def resolve(
    manual: ManualSlot | None,
    plan: Plan | None,
    now: dt.datetime,
    battery: BatteryState,
    enabled: bool = True,
) -> dict[str, Any]:
    """Return the action now from the manual slot covering now and the plan.

    A manual slot is followed as it is, so it runs without a plan too: `charge`
    and `sell` at the highest current to their target SOC (the inverter scripts
    stop there), `pause` keeps up to the max SOC, `sell-excess` exports down to the
    reserve.
    """
    if manual is not None:
        limits = SocLimits(battery.min_soc, battery.max_soc, battery.reserve_soc)
        soc = {
            "pause": battery.max_soc,
            "sell-excess": max(battery.min_soc, battery.reserve_soc),
        }.get(manual.state, target(manual, limits))
        current = {"charge": battery.max_charge_a, "sell": battery.max_discharge_a}
        return _action(
            manual.state,
            "manual",
            mode=STATE_MODES[manual.state],
            soc=None if soc is None else round(soc),
            current_a=int(current.get(manual.state, 0)),
            # Kept in UTC; published in local time like the plan's times
            until=manual.end.astimezone(now.tzinfo),
        )
    if not enabled:
        return _action("discharge", "none", reason="planner off")
    return plan_action(plan, now, battery)
