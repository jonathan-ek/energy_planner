"""Battery schedule optimizer: cheapest mode per quarter under a grid tariff.

Pure functions, no Home Assistant imports. Given forecast load and PV, spot prices,
the tariff and the battery, choose per quarter one of the modes the inverter can run:

- `self_use`: the battery covers the load and stores PV surplus.
- `hold`: the battery keeps its energy and only shaves the grid import above the
  quarter's import target (`targets`: the lowest of the household's import limit and
  the power charge levels), like the inverter's peak shaving. PV surplus is stored.
- `charge`: the battery charges from PV and the grid, keeping the grid import at or
  below the import target.
- `sell_excess`: the PV surplus is exported instead of stored; the battery only
  covers the load when PV does not (the slot state `sell-excess`). Exporting surplus
  now and storing later surplus avoids the battery losses of `sell`. With a full
  battery `self_use` exports the surplus too, and wins the tie.
- `sell`: the battery discharges at full power, the surplus is exported.

Dynamic programming over the state of charge finds the cheapest modes for given
import targets. Power charges are not additive (they are set by the month's highest
hours), so the targets are searched in an outer loop: each power charge gets a
target, starting at the peak already reached this month (import up to it is free)
and raised in steps while the total cost, power charges included, goes down. Raising
it lets the battery charge faster in the cheapest hours, at the price of a higher peak.

Battery wear (`Battery.wear_cost`, SEK per kWh taken out of the battery) is added to
every discharge, so the battery is only cycled when the price difference pays for it.

A peak is paid once per month but then usable for the rest of it, while the plan only
covers a day or two. So when comparing targets, raising a peak costs the share of the
monthly charge that the plan covers of the month's remaining hours (assuming the
remaining days are alike); `Plan.power_cost` is the real increase.
"""

from collections.abc import Mapping, Sequence
import dataclasses
import datetime as dt
from itertools import pairwise
import math

import numpy as np

from .tariff import PowerCharge, Tariff, charged_peak, import_limit

QUARTER_HOURS = 0.25
MODES = ("self_use", "hold", "sell_excess", "charge", "sell")
SOC_STEPS = 101
TARGET_STEP_KW = 0.5
# SEK per kWh above the household's import limit: higher than any price, so it is
# only broken when the battery cannot cover the load
LIMIT_PENALTY = 20.0
# SEK per mode change: a plan recalculated every quarter would otherwise flip
# between modes that differ by a few öre, and each change is written to the inverter
SWITCH_COST = 0.05
# Quarters at the end of the horizon whose cheapest price values the energy left
END_WINDOW = 96


@dataclasses.dataclass(frozen=True)
class Battery:
    """Battery and inverter limits, energies in kWh and powers in kW."""

    min_kwh: float
    max_kwh: float
    soc_kwh: float
    max_grid_charge_kw: float
    max_power_kw: float
    efficiency: float = 0.9
    wear_cost: float = 0.0  # SEK per kWh taken out of the battery


@dataclasses.dataclass(frozen=True)
class Limit:
    """The household's grid import limit (see tariff.import_limit)."""

    kw: float = 0.0
    start_hour: int = 6
    end_hour: int = 23


@dataclasses.dataclass
class Plan:
    """The optimized schedule, one entry per quarter."""

    starts: list[dt.datetime]
    modes: list[str]
    soc: list[float]  # kWh at the end of each quarter
    grid_import: list[float]
    grid_export: list[float]
    targets: list[float | None]
    energy_cost: float  # SEK for bought minus sold energy
    wear_cost: float  # SEK of battery wear
    power_cost: float  # SEK the power charges increase this month
    end_value: float  # SEK the energy left in the battery is worth
    limit_excess_kwh: float  # import above the household's limit
    # Charged kW (before the plan, with it) per power charge and month, keyed by
    # "<index in tariff.power_charges>:<YYYY-MM>"
    peaks: dict[str, tuple[float, float]]
    objective: float  # what the search minimizes, see the module docstring


@dataclasses.dataclass(frozen=True)
class _Inputs:
    """Per quarter arrays and settings shared by all optimizer runs."""

    starts: Sequence[dt.datetime]
    net: np.ndarray  # load - pv, kWh
    buy: np.ndarray  # SEK/kWh incl. VAT and fees
    sell: np.ndarray  # SEK/kWh
    limit: np.ndarray  # kWh per quarter, inf if no limit
    battery: Battery
    end_price: float  # SEK per kWh left in the battery at the end, wear deducted
    forced: Sequence[str | None]  # a mode the quarter must use, e.g. a manual slot
    current_mode: str | None  # the mode running now, changing it costs SWITCH_COST


def _step(inputs: _Inputs, index: int, mode: str, soc, target_kwh):
    """Return (next soc, grid import, grid export) for a mode, vectorized over soc."""
    battery = inputs.battery
    eta = math.sqrt(battery.efficiency)
    net = inputs.net[index]
    power = battery.max_power_kw * QUARTER_HOURS
    available = np.maximum(soc - battery.min_kwh, 0.0) * eta  # deliverable kWh
    room = np.maximum(battery.max_kwh - soc, 0.0) / eta  # kWh it can take in
    surplus = max(-net, 0.0)
    demand = max(net, 0.0)

    if mode == "sell":
        out = np.minimum(available, power)
        grid = net - out
        return soc - out / eta, np.maximum(grid, 0.0), np.maximum(-grid, 0.0)
    if mode == "charge":
        grid_room = min(battery.max_grid_charge_kw * QUARTER_HOURS, target_kwh - demand)
        stored = np.minimum(np.minimum(room, power), surplus + max(grid_room, 0.0))
        grid = demand - surplus + stored
        return soc + stored * eta, np.maximum(grid, 0.0), np.maximum(-grid, 0.0)
    # self_use and hold store the PV surplus, sell_excess exports it; hold only
    # covers the load above the target
    stored = np.minimum(
        np.minimum(room, power), 0.0 if mode == "sell_excess" else surplus
    )
    covered = max(demand - target_kwh, 0.0) if mode == "hold" else demand
    out = np.minimum(available, min(covered, power))
    grid = demand - out - (surplus - stored)
    next_soc = soc + stored * eta - out / eta
    return next_soc, np.maximum(grid, 0.0), np.maximum(-grid, 0.0)


def _quarter_cost(inputs: _Inputs, index: int, grid_import, grid_export, caps):
    """Energy cost plus penalties for import above the limit and the targets."""
    cost = grid_import * inputs.buy[index] - grid_export * inputs.sell[index]
    cost = cost + np.maximum(grid_import - inputs.limit[index], 0.0) * LIMIT_PENALTY
    # A kWh above a target in one quarter raises that hour's mean by 1 kW
    for cap_kwh, kr_per_kw in caps:
        cost = cost + np.maximum(grid_import - cap_kwh, 0.0) * kr_per_kw
    return cost


def _wear(battery: Battery, soc, next_soc):
    """Return the wear cost of going from soc to next_soc, vectorized."""
    return np.maximum(soc - next_soc, 0.0) * battery.wear_cost


def _allowed(inputs: _Inputs, index: int) -> list[int]:
    """Return the indexes in MODES the quarter may use (one if it is forced)."""
    forced = inputs.forced[index]
    return [MODES.index(forced)] if forced else list(range(len(MODES)))


def _optimize(inputs: _Inputs, targets: list[float], caps: list[list[tuple]]):
    """Run the dynamic program for fixed import targets; return per quarter results.

    The state is the state of charge and the previous quarter's mode (the last row:
    none), so that changing mode can cost `SWITCH_COST`.
    """
    battery = inputs.battery
    grid = np.linspace(battery.min_kwh, battery.max_kwh, SOC_STEPS)
    count = len(inputs.starts)
    rows = len(MODES) + 1
    switch = np.full((rows, len(MODES)), SWITCH_COST)
    for mode in range(len(MODES)):
        switch[mode, mode] = 0.0
    switch[-1, :] = 0.0
    value = np.tile(-(grid - battery.min_kwh) * inputs.end_price, (rows, 1))
    values = [value]
    for index in range(count - 1, -1, -1):
        totals = np.full((len(MODES), SOC_STEPS), np.inf)
        for mode in _allowed(inputs, index):
            soc, imp, exp = _step(inputs, index, MODES[mode], grid, targets[index])
            totals[mode] = (
                _quarter_cost(inputs, index, imp, exp, caps[index])
                + _wear(battery, grid, soc)
                + np.interp(soc, grid, values[-1][mode])
            )
        # Best over the modes for each previous mode
        values.append(np.min(totals[None, :, :] + switch[:, :, None], axis=1))
    values.reverse()

    # Forward pass from the actual state of charge and the mode running now
    soc = np.array([min(max(battery.soc_kwh, battery.min_kwh), battery.max_kwh)])
    previous = MODES.index(inputs.current_mode) if inputs.current_mode else rows - 1
    modes, socs, imports, exports = [], [], [], []
    switches = 0
    for index in range(count):
        candidates = []
        for mode in _allowed(inputs, index):
            next_soc, imp, exp = _step(inputs, index, MODES[mode], soc, targets[index])
            total = float(
                _quarter_cost(inputs, index, imp, exp, caps[index])[0]
                + _wear(battery, soc, next_soc)[0]
                + np.interp(next_soc, grid, values[index + 1][mode])[0]
                + switch[previous, mode]
            )
            candidates.append((total, mode, next_soc, imp, exp))
        # Small tie margin: the first (simplest) mode wins when costs are equal
        lowest = min(c[0] for c in candidates)
        _, mode, soc, imp, exp = next(c for c in candidates if c[0] <= lowest + 1e-9)
        # Modes that do the same this quarter (e.g. `sell` with an empty battery and
        # `sell_excess`) are named after the simplest one, so the inverter does not
        # run a mode the plan does not need
        mode = next(
            c[1]
            for c in candidates
            if all(
                abs(float(x[0]) - float(y[0])) < 1e-9
                for x, y in ((c[2], soc), (c[3], imp), (c[4], exp))
            )
        )
        switches += switch[previous, mode] > 0
        previous = mode
        modes.append(MODES[mode])
        socs.append(float(soc[0]))
        imports.append(float(imp[0]))
        exports.append(float(exp[0]))
    return modes, socs, imports, exports, switches


def _hourly(starts: Sequence[dt.datetime], kwh: Sequence[float]):
    """Sum quarter energies per local hour start: the hourly mean power in kW."""
    hourly: dict[dt.datetime, float] = {}
    for start, energy in zip(starts, kwh, strict=True):
        hour = start.replace(minute=0, second=0, microsecond=0)
        hourly[hour] = hourly.get(hour, 0.0) + energy
    return hourly


def _month_share(starts: Sequence[dt.datetime], first: dt.datetime) -> float:
    """Return the share of the month's remaining hours that the plan covers."""
    if first.month == 12:
        month_end = first.replace(year=first.year + 1, month=1, day=1, hour=0, minute=0)
    else:
        month_end = first.replace(month=first.month + 1, day=1, hour=0, minute=0)
    covered = sum(
        QUARTER_HOURS for s in starts if (s.year, s.month) == (first.year, first.month)
    )
    remaining = (month_end - first).total_seconds() / 3600
    return min(1.0, covered / remaining) if remaining > 0 else 1.0


def _charge_key(charge_index: int, moment: dt.datetime) -> str:
    return f"{charge_index}:{moment.year}-{moment.month:02d}"


def optimize(
    starts: Sequence[dt.datetime],
    prices: Sequence[float],
    load: Sequence[float],
    pv: Sequence[float],
    battery: Battery,
    tariff: Tariff,
    limit: Limit | None = None,
    history: Mapping[dt.datetime, float] | None = None,
    forced: Sequence[str | None] | None = None,
    current_mode: str | None = None,
) -> Plan:
    """Return the cheapest plan.

    `starts` are local quarter starts; `prices` spot SEK/MWh excl. VAT; `load` and
    `pv` kWh per quarter; `history` the hourly mean grid import (kW, keyed by local
    hour start) so far this month, which sets the peaks already paid for; `forced`
    a mode per quarter that must be used (None: free); `current_mode` the mode the
    battery runs now, so a recalculated plan only changes it when it pays.
    """
    history = dict(history or {})
    limit = limit or Limit()
    net = np.array(load, dtype=float) - np.array(pv, dtype=float)
    buy = np.array(
        [
            p * 1.25 / 1000 + tariff.buy_fee(s) / 100
            for s, p in zip(starts, prices, strict=True)
        ]
    )
    sell = np.array(
        [
            p / 1000 + tariff.sell_fee(s) / 100
            for s, p in zip(starts, prices, strict=True)
        ]
    )
    limits = [
        import_limit(s, limit.kw, limit.start_hour, limit.end_hour) for s in starts
    ]
    limit_kwh = np.array(
        [math.inf if kw is None else kw * QUARTER_HOURS for kw in limits]
    )
    # Energy left at the end is worth no more than a purchase at the cheapest price
    # of the horizon's last day (minus the wear of taking it out): the battery can be
    # refilled then. A typical price instead makes the plan hoard energy it could
    # use, and charge it, only to have it at the end
    end_price = (
        max(
            float(np.min(buy[-END_WINDOW:])) * math.sqrt(battery.efficiency)
            - battery.wear_cost,
            0.0,
        )
        if len(buy)
        else 0.0
    )
    inputs = _Inputs(
        starts,
        net,
        buy,
        sell,
        limit_kwh,
        battery,
        end_price,
        tuple(forced) if forced else (None,) * len(starts),
        current_mode,
    )

    # The power charges in the horizon, per month, and the peak already reached
    charges: dict[str, tuple[int, PowerCharge, float]] = {}
    shares: dict[str, float] = {}
    quarter_keys: list[list[str]] = []
    for start in starts:
        keys = []
        for index, charge in enumerate(tariff.power_charges):
            if charge.period.contains(start):
                key = _charge_key(index, start)
                if key not in charges:
                    month = {
                        h: kw
                        for h, kw in history.items()
                        if (h.year, h.month) == (start.year, start.month)
                    }
                    charges[key] = (index, charge, charged_peak(charge, month))
                    shares[key] = _month_share(starts, start)
                keys.append(key)
        quarter_keys.append(keys)

    def run(levels: dict[str, float]) -> Plan:
        targets, caps = [], []
        for index, keys in enumerate(quarter_keys):
            target = min(
                [levels[k] * QUARTER_HOURS for k in keys]
                + [
                    float(limit_kwh[index]),
                    battery.max_grid_charge_kw * QUARTER_HOURS + max(net[index], 0.0),
                ]
            )
            targets.append(target)
            caps.append(
                [
                    (
                        levels[k] * QUARTER_HOURS,
                        charges[k][1].kr_per_kw * shares[k] / charges[k][1].peaks,
                    )
                    for k in keys
                ]
            )
        modes, socs, imports, exports, switches = _optimize(inputs, targets, caps)
        energy = float(np.dot(imports, buy) - np.dot(exports, sell))
        first = min(max(battery.soc_kwh, battery.min_kwh), battery.max_kwh)
        wear = sum(
            max(a - b, 0.0) * battery.wear_cost for a, b in pairwise([first, *socs])
        )
        planned = _hourly(starts, imports)
        combined = {**history, **planned}
        power = amortized = 0.0
        peaks = {}
        for key, (index, charge, before) in charges.items():
            month = {
                h: kw for h, kw in combined.items() if _charge_key(index, h) == key
            }
            after = charged_peak(charge, month)
            peaks[key] = (before, after)
            power += charge.kr_per_kw * (after - before)
            amortized += charge.kr_per_kw * (after - before) * shares[key]
        excess = float(np.sum(np.maximum(np.array(imports) - limit_kwh, 0.0)))
        end_value = (socs[-1] - battery.min_kwh) * end_price if socs else 0.0
        return Plan(
            list(starts),
            modes,
            socs,
            imports,
            exports,
            [None if math.isinf(t) else t / QUARTER_HOURS for t in targets],
            energy,
            wear,
            power,
            end_value,
            excess,
            peaks,
            energy
            + wear
            + amortized
            - end_value
            + excess * LIMIT_PENALTY
            + switches * SWITCH_COST,
        )

    # Coordinate search over the import targets, starting at the peaks already paid
    levels = {key: before for key, (_, _, before) in charges.items()}
    best = run(levels)
    top = battery.max_grid_charge_kw + max(
        float(np.max(net, initial=0.0)) / QUARTER_HOURS, 0.0
    )
    for _ in range(2):
        improved = False
        for key in charges:
            for level in np.arange(
                levels[key] + TARGET_STEP_KW, top + TARGET_STEP_KW, TARGET_STEP_KW
            ):
                plan = run({**levels, key: float(level)})
                if plan.objective < best.objective - 0.01:
                    best, levels, improved = plan, {**levels, key: float(level)}, True
        if not improved:
            break
    return best
