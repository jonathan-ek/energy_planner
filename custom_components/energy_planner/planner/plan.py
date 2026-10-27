"""The battery plan as one object: what the plan says per quarter, and now.

Pure, no Home Assistant imports. battery_plan.py builds a `Plan` from the
optimizer's result and stores it in `hass.data[DOMAIN]["plan"]`; the resolvers
(battery_action.py, ev.py, heat_pump.py) ask it what to do at a moment, and
`to_attributes` gives the plan sensor its attributes.

Per-quarter series are tuples parallel to the plan's `timeline` (timeline.py, the
quarters as UTC instants). A run is the stretch of equal values in a series from now on:
the battery's mode, the heat pump on/off, the car charging or not. A plan is stale once
it became available (`finished`) more than `STALE_AFTER` ago; the devices then fall
back.

Moments are looked up in the timeline, so the hour repeated when daylight saving time
ends works; `Run.until` is local time, for display.
"""

from collections.abc import Callable, Sequence
import dataclasses
import datetime as dt
import functools
from typing import Any

from .timeline import Timeline

# Older plans are not followed
STALE_AFTER = dt.timedelta(minutes=30)
VAT = 1.25


@dataclasses.dataclass(frozen=True)
class Run:
    """Equal values in a series from the quarter now (`index`) to `last`."""

    value: Any
    index: int
    last: int
    until: dt.datetime  # the end of the last quarter, local time


@dataclasses.dataclass(frozen=True)
class EvPlan:
    """The car while it is plugged in."""

    need_kwh: float
    need_source: str  # soc, entered, history or trip
    ready: dt.datetime  # the deadline, also past the horizon
    power_kw: tuple[float, ...]  # per quarter
    planned_kwh: float
    missing_kwh: float  # what the plan can not deliver by the deadline


@dataclasses.dataclass(frozen=True)
class Plan:
    """The battery plan from the current quarter on."""

    updated: dt.datetime  # the moment it was planned for
    finished: dt.datetime  # when it became available
    timeline: Timeline
    modes: tuple[str, ...]
    soc_start: float  # % when it was planned (`updated`)
    soc: tuple[float, ...]  # % at the end of each quarter
    grid_import_kw: tuple[float, ...]
    grid_export_kw: tuple[float, ...]
    targets: tuple[float | None, ...]  # import target, kW
    # The power charge level (kW), the import target below the reserve SOC
    peak_levels: tuple[float | None, ...]
    spot: tuple[float, ...]  # SEK/MWh excl. VAT
    estimated: tuple[bool, ...]  # the spot price is an estimate
    heat_pump: tuple[bool, ...]  # on
    marginal: tuple[float, ...]  # öre/kWh more load costs, empty without heat pump
    temperature: tuple[float | None, ...]  # outdoor, °C
    heat_pump_cop: tuple[float | None, ...]
    heat_pump_kw: tuple[float, ...]  # its draw while heating
    ev: EvPlan | None
    energy_cost: float  # SEK
    wear_cost: float
    power_cost: float
    end_value: float
    limit_excess_kwh: float
    fuse_excess_kwh: float
    peaks: tuple[dict[str, Any], ...]
    # The manual slots forced into the plan: the mode and its target SOC % per
    # quarter, None where none covers it. Separate from `modes` and `soc`, which are
    # what the plan does and expects
    manual: tuple[str | None, ...] = ()
    manual_soc: tuple[float | None, ...] = ()

    def index_at(self, moment: dt.datetime) -> int | None:
        """Return the quarter containing `moment`, None outside the plan."""
        return self.timeline.index_at(moment)

    def value_at(self, moment: dt.datetime, values: Sequence[Any]) -> Any:
        """Return the value of a series of this plan at `moment`, None outside it."""
        index = self.index_at(moment)
        return None if index is None or index >= len(values) else values[index]

    def run_at(self, moment: dt.datetime, values: Sequence[Any]) -> Run | None:
        """Return the run of equal `values` from the quarter of `moment` on.

        `values` is a series of this plan (or one derived from it); None outside it.
        """
        index = self.index_at(moment)
        if index is None or index >= len(values):
            return None
        last = index
        while last + 1 < len(values) and values[last + 1] == values[index]:
            last += 1
        return Run(values[index], index, last, self.timeline.local_end(last))

    def is_stale(self, now: dt.datetime) -> bool:
        """Return whether the plan is too old to follow."""
        return now - self.finished > STALE_AFTER

    def soc_at(self, moment: dt.datetime) -> float | None:
        """Return the SOC % the plan expects at `moment`, None outside the plan.

        Between the previous quarter's end SOC and this one's; in the first quarter
        from the SOC when it was planned, which is usually partway through it.
        """
        index = self.index_at(moment)
        if index is None:
            return None
        time = moment.timestamp()
        end = self.timeline.quarter_end(index).timestamp()
        if index == 0:
            begin, before = self.updated.timestamp(), self.soc_start
        else:
            begin, before = self.timeline.starts[index].timestamp(), self.soc[index - 1]
        if time <= begin or end <= begin:
            return before
        return before + (self.soc[index] - before) * (time - begin) / (end - begin)

    def to_attributes(self) -> dict[str, Any]:
        """Return the plan as the plan sensor publishes it (built once per plan)."""
        return self._attributes

    @functools.cached_property
    def _attributes(self) -> dict[str, Any]:
        ev = self.ev
        return {
            "updated": self.updated.isoformat(),
            "finished": self.finished.isoformat(),
            "starts": self.timeline.isoformat(),
            "modes": list(self.modes),
            "soc_start": self.soc_start,
            "soc": list(self.soc),
            "grid_import": list(self.grid_import_kw),
            "grid_export": list(self.grid_export_kw),
            "targets": list(self.targets),
            "peak_levels": list(self.peak_levels),
            # Spot price incl. VAT (öre/kWh), and whether it is an estimate
            "prices": [round(p * VAT / 10, 2) for p in self.spot],
            "estimated": list(self.estimated),
            # Heat pump: on per quarter, what more load costs (öre/kWh), its COP and
            # draw (kW while heating)
            "heat_pump": list(self.heat_pump),
            "marginal": list(self.marginal),
            "temperature": list(self.temperature),
            "heat_pump_cop": list(self.heat_pump_cop),
            "heat_pump_power": list(self.heat_pump_kw),
            # Forced by a manual slot: the mode and target SOC (None: free)
            "manual": list(self.manual),
            "manual_soc": list(self.manual_soc),
            # The car while plugged in: charging power per quarter (kW), the need and
            # what the plan can not deliver by the ready time (None: not planned)
            "ev": None
            if ev is None
            else {
                "need_kwh": ev.need_kwh,
                "need_source": ev.need_source,
                "ready": ev.ready.isoformat(),
                "power": list(ev.power_kw),
                "planned_kwh": ev.planned_kwh,
                "missing_kwh": ev.missing_kwh,
            },
            "target_kw": self.targets[0],
            "energy_cost": self.energy_cost,
            "wear_cost": self.wear_cost,
            "power_cost": self.power_cost,
            "end_value": self.end_value,
            "limit_excess_kwh": self.limit_excess_kwh,
            "fuse_excess_kwh": self.fuse_excess_kwh,
            "peaks": list(self.peaks),
        }


def follow(
    plan: Plan | None,
    now: dt.datetime,
    series: Callable[[Plan], Sequence[Any] | None],
) -> tuple[Run, None] | tuple[None, str]:
    """Return the plan's run now in `series(plan)`, or why it can not be followed."""
    if plan is None:
        return None, "no plan"
    if plan.is_stale(now):
        return None, "plan is old"
    values = series(plan)
    run = plan.run_at(now, values) if values else None
    if run is None:
        return None, "plan does not cover now"
    return run, None


def action(
    state: str | None,
    source: str,
    *,
    plan: Plan | None = None,
    until: dt.datetime | None = None,
    reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Return an action: the keys every device's action has, and its own."""
    return {
        "state": state,
        "source": source,
        "until": until.isoformat() if until else None,
        "reason": reason,
        "plan_updated": plan.updated.isoformat() if plan else None,
        **extra,
    }
