"""Build battery plans for tests: the series a test sets, neutral values elsewhere."""

from collections.abc import Sequence
import datetime as dt

from custom_components.energy_planner.planner.plan import QUARTER, EvPlan, Plan


def make_plan(
    start: dt.datetime,
    modes: Sequence[str] | None = None,
    *,
    size: int | None = None,
    soc: Sequence[float] | None = None,
    targets: Sequence[float | None] | None = None,
    peak_levels: Sequence[float | None] | None = None,
    spot: Sequence[float] | None = None,
    grid_export_kw: Sequence[float] | None = None,
    heat_pump: Sequence[bool] | None = None,
    marginal: Sequence[float] | None = None,
    heat_pump_cop: Sequence[float | None] | None = None,
    ev_power_kw: Sequence[float] | None = None,
    soc_start: float = 50.0,
    updated: dt.datetime | None = None,
    finished: dt.datetime | None = None,
) -> Plan:
    """Return a plan of quarters from `start`, as many as the longest series given.

    Unset series are self-use, 50 % SOC (also at the start), no import target,
    100 SEK/MWh, no export, the heat pump off at COP 3; `ev_power_kw` plans a car.
    `updated` defaults to `start` and `finished` to `updated`.
    """
    given = [
        s
        for s in (modes, soc, targets, spot, grid_export_kw, heat_pump, ev_power_kw)
        if s is not None
    ]
    n = size if size is not None else max((len(s) for s in given), default=1)
    updated = updated or start
    return Plan(
        updated=updated,
        finished=finished or updated,
        starts=tuple(
            (start.astimezone(dt.UTC) + QUARTER * i).astimezone(start.tzinfo)
            for i in range(n)
        ),
        modes=tuple(modes or ["self_use"] * n),
        soc_start=soc_start,
        soc=tuple(soc or [50.0] * n),
        grid_import_kw=(0.0,) * n,
        grid_export_kw=tuple(grid_export_kw or [0.0] * n),
        targets=tuple(targets or [None] * n),
        peak_levels=tuple(peak_levels or [None] * n),
        spot=tuple(spot or [100.0] * n),
        estimated=(False,) * n,
        heat_pump=tuple(heat_pump or [False] * n),
        marginal=tuple([100.0] * n if marginal is None else marginal),
        temperature=(None,) * n,
        heat_pump_cop=tuple(heat_pump_cop or [3.0] * n),
        heat_pump_kw=(0.0,) * n,
        ev=None
        if ev_power_kw is None
        else EvPlan(
            need_kwh=20.0,
            need_source="history",
            ready=start + dt.timedelta(hours=8),
            power_kw=tuple(ev_power_kw),
            planned_kwh=sum(ev_power_kw) / 4,
            missing_kwh=0.0,
        ),
        energy_cost=0.0,
        wear_cost=0.0,
        power_cost=0.0,
        end_value=0.0,
        limit_excess_kwh=0.0,
        fuse_excess_kwh=0.0,
        peaks=(),
    )
