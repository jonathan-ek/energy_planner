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
- `discard_excess`: like `self_use`, but PV surplus the battery cannot take is
  curtailed instead of exported (the slot state `discard-excess`, the inverter's
  feed-in limit). Pays when the export price (spot plus the network compensation)
  is negative.
- `sell`: the battery discharges at full power, the surplus is exported.

Dynamic programming over the state of charge finds the cheapest modes for given
import targets. Power charges are not additive (they are set by the month's highest
hours), so the targets are searched in an outer loop: each power charge gets a
target, starting at the peak already reached this month (import up to it is free)
and raised in steps while the total cost, power charges included, goes down. Raising
it lets the battery charge faster in the cheapest hours, at the price of a higher peak.

The main fuse (`Limit.fuse_kw`) caps all grid import, the car and the heat pump
included, at `LIMIT_PENALTY` per kWh above it, and the import targets are capped at
it: the inverter's peak shaving then slows the battery's charging while the car
charges, instead of tripping the fuse.

The plan does not discharge below `Battery.reserve_kwh` (a soft minimum above the
shutdown SOC `min_kwh`): the inverter may use that energy to shave unexpected
peaks. Below the reserve the SOC can be (after such a peak), and the energy missing
at the end is charged at the end price, so the plan refills it.

Battery wear (`Battery.wear_cost`, SEK per kWh taken out of the battery) is added to
every discharge, so the battery is only cycled when the price difference pays for it.

`Plan.marginal` is what `probe_kwh` more load costs per kWh in each quarter, given
the plan: the buy price when importing, the export price lost when exporting, or the
value of the battery energy it uses (from the dynamic program), plus power charges.
The heat pump is planned on it (heat_pump.py); its load can be `exempt` from the
household's import limit, so only the power charges limit it.

An electric car (`Vehicle`) can be planned together with the battery: the dynamic
program gets a second state, the energy delivered to the car, and per quarter the
car charges at 0 or one of the charger's powers while it is plugged in, so that its
need is met by its deadline (see `_optimize`). Planning both at once beats charging
the car first in its cheapest quarters and then planning the battery around it:
they share the cheap hours, the solar surplus and the power peaks. Backtest on 24
random days with the owner's battery and tariff (Oct 2026, synthetic prices and
loads): 5.6 kr/day less than that sequential plan (never worse), 16.5 kr/day less
than charging at full power on arrival. A run takes about 8 s on a desktop and
30-100 s on the HA host, 3-4 times longer than without a car.

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
MODES = ("self_use", "hold", "sell_excess", "discard_excess", "charge", "sell")
SOC_STEPS = 101
TARGET_STEP_KW = 0.5
# The target search first tries levels this far apart, then TARGET_STEP_KW around
# the best (see `_candidate_levels`)
COARSE_STEP_KW = 2.0
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
    reserve_kwh: float = 0.0  # the plan keeps this, the inverter may shave peaks

    @property
    def floor_kwh(self) -> float:
        """The lowest SOC the plan discharges to: the reserve or the shutdown SOC."""
        return max(self.min_kwh, self.reserve_kwh)


@dataclasses.dataclass(frozen=True)
class Limit:
    """The household's grid import limit (see tariff.import_limit) and main fuse.

    `fuse_kw` (0: none) is the most the grid connection can take, all the time and
    for every load: the quarter's mean import, as for the other limits.
    """

    kw: float = 0.0
    start_hour: int = 6
    end_hour: int = 23
    fuse_kw: float = 0.0


@dataclasses.dataclass(frozen=True)
class Vehicle:
    """An electric car to charge, as the plan sees it (energies at the plug).

    The car needs `need_kwh` more by the start of quarter `deadline` (an index into
    the plan's quarters; at or past the end means after the horizon). It charges only
    in quarters where `plugged` is true, at 0 or one of `powers_kw` (the charger's
    lowest to highest power; below its lowest current it can not charge at all).

    Energy still missing at a deadline inside the horizon costs `shortfall_price`
    SEK/kWh: a soft constraint, so a need that can not be met still gives a plan,
    and a high price, so it is met whenever it can be. Missing at a deadline after
    the horizon it costs the cheapest buy price of the horizon's last day (it can
    be charged then). Charging past the need is worth nothing; quarters after the
    deadline do not charge (the next need is planned when it is known).

    `step_kwh` is the resolution of the energy delivered so far, the dynamic
    program's second state (see `_optimize`).
    """

    powers_kw: tuple[float, ...]
    need_kwh: float
    plugged: Sequence[bool]
    deadline: int
    shortfall_price: float = 10.0
    step_kwh: float = 0.5


def charger_powers(min_kw: float, max_kw: float, count: int = 3) -> tuple[float, ...]:
    """Return `count` charging powers from the charger's lowest to its highest."""
    if count <= 1 or max_kw <= min_kw:
        return (max_kw,)
    return tuple(float(kw) for kw in np.linspace(min_kw, max_kw, count))


@dataclasses.dataclass
class Plan:
    """The optimized schedule, one entry per quarter."""

    starts: list[dt.datetime]
    modes: list[str]
    soc: list[float]  # kWh at the end of each quarter
    grid_import: list[float]
    grid_export: list[float]
    targets: list[float | None]
    # The quarter's lowest power charge level (kW, None: no power charge), the import
    # to shave down to with the reserve
    peak_levels: list[float | None]
    energy_cost: float  # SEK for bought minus sold energy
    wear_cost: float  # SEK of battery wear
    power_cost: float  # SEK the power charges increase this month
    end_value: float  # SEK the energy left in the battery is worth
    limit_excess_kwh: float  # import above the household's limit
    # Charged kW (before the plan, with it) per power charge and month, keyed by
    # "<index in tariff.power_charges>:<YYYY-MM>"
    peaks: dict[str, tuple[float, float]]
    objective: float  # what the search minimizes, see the module docstring
    marginal: list[float]  # SEK per kWh of `probe_kwh` more load in each quarter
    # The car (Vehicle): kWh charged per quarter, and what is still missing at the
    # deadline and what that costs (the shortfall price or charging after the horizon)
    ev_kwh: list[float] = dataclasses.field(default_factory=list)
    ev_missing_kwh: float = 0.0
    ev_missing_cost: float = 0.0
    fuse_excess_kwh: float = 0.0  # import above the main fuse


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
    probe_kwh: float  # extra load per quarter that Plan.marginal prices, 0: none
    vehicle: Vehicle | None = None  # a car to charge, None: no car
    ev_end_price: float = 0.0  # SEK per kWh the car still needs after the horizon
    fuse_kwh: float = math.inf  # the main fuse, kWh per quarter, inf if none


def _step(inputs: _Inputs, index: int, mode: str, soc, target_kwh, extra=0.0):
    """Return (next soc, grid import, grid export) for a mode, vectorized over soc.

    `extra` is load added to the quarter's (for Plan.marginal).
    """
    battery = inputs.battery
    eta = math.sqrt(battery.efficiency)
    net = inputs.net[index] + extra
    power = battery.max_power_kw * QUARTER_HOURS
    available = np.maximum(soc - battery.floor_kwh, 0.0) * eta  # deliverable kWh
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
    # self_use, hold and discard_excess store the PV surplus, sell_excess exports
    # it; hold only covers the load above the target
    stored = np.minimum(
        np.minimum(room, power), 0.0 if mode == "sell_excess" else surplus
    )
    covered = max(demand - target_kwh, 0.0) if mode == "hold" else demand
    out = np.minimum(available, min(covered, power))
    grid = demand - out - (surplus - stored)
    next_soc = soc + stored * eta - out / eta
    # discard_excess curtails the surplus the battery cannot take
    exported = np.maximum(-grid, 0.0) * (mode != "discard_excess")
    return next_soc, np.maximum(grid, 0.0), exported


def _quarter_cost(
    inputs: _Inputs, index: int, grid_import, grid_export, caps, exempt=0.0
):
    """Energy cost plus penalties for import above the limit, the fuse and targets.

    `exempt` is extra load that the limit does not apply to (for Plan.marginal); the
    fuse applies to all of it.
    """
    cost = grid_import * inputs.buy[index] - grid_export * inputs.sell[index]
    cost = (
        cost
        + (
            np.maximum(grid_import - inputs.limit[index] - exempt, 0.0)
            + np.maximum(grid_import - inputs.fuse_kwh, 0.0)
        )
        * LIMIT_PENALTY
    )
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


def _ev_levels(inputs: _Inputs) -> np.ndarray:
    """Return the car's state grid: energy delivered so far, 0 to the need (kWh).

    Evenly spaced at about `Vehicle.step_kwh`; one level (no car state) without a
    car or a need.
    """
    vehicle = inputs.vehicle
    if vehicle is None or vehicle.need_kwh <= 0 or vehicle.deadline <= 0:
        return np.zeros(1)
    count = max(math.ceil(vehicle.need_kwh / vehicle.step_kwh), 1) + 1
    return np.linspace(0.0, vehicle.need_kwh, count)


def _ev_actions(inputs: _Inputs, index: int, levels: np.ndarray) -> list[float]:
    """Return the kWh the car can charge in a quarter: 0, or one per charger power.

    Only while it is plugged in, before its deadline, and while it has a need.
    """
    vehicle = inputs.vehicle
    if (
        vehicle is None
        or len(levels) == 1
        or index >= vehicle.deadline
        or not vehicle.plugged[index]
    ):
        return [0.0]
    return [0.0, *(kw * QUARTER_HOURS for kw in vehicle.powers_kw)]


def _ev_missing(inputs: _Inputs, levels: np.ndarray, at_end: bool) -> np.ndarray:
    """Return the cost (SEK) of what each car level still misses of the need.

    At the deadline (`at_end` False) all of it costs the shortfall price. At the end
    of the horizon (`at_end` True, the deadline is after it) the part the charger
    can still deliver before the deadline (the quarters left times its highest
    power, assuming it stays plugged in) costs `_Inputs.ev_end_price`, the rest the
    shortfall price. Without that limit the plan would always leave a later need
    to after the horizon, where the end price can not be beaten, even when there is
    not time enough left to charge it.
    """
    vehicle = inputs.vehicle
    if vehicle is None:
        return np.zeros(len(levels))
    missing = np.maximum(vehicle.need_kwh - levels, 0.0)
    if not at_end:
        return missing * vehicle.shortfall_price
    left = (
        max(vehicle.deadline - len(inputs.starts), 0)
        * max(vehicle.powers_kw)
        * QUARTER_HOURS
    )
    later = np.minimum(missing, left)
    return later * inputs.ev_end_price + (missing - later) * vehicle.shortfall_price


def _at_soc(values: np.ndarray, grid: np.ndarray, soc: np.ndarray) -> np.ndarray:
    """Interpolate `values[soc level, car level]` at each soc, linear in soc.

    `grid` is evenly spaced; a soc outside it is clamped to its ends (as np.interp).
    Returns one row of car levels per soc.
    """
    position = (np.clip(soc, grid[0], grid[-1]) - grid[0]) / (grid[1] - grid[0])
    low = np.minimum(np.floor(position).astype(int), len(grid) - 2)
    weight = (position - low)[:, None]
    return values[low] * (1.0 - weight) + values[low + 1] * weight


def _shifted(values: np.ndarray, kwh: float, levels: np.ndarray) -> np.ndarray:
    """Return `values[..., car level]` at each car level plus `kwh`, linear.

    The car levels are evenly spaced, so the shift is the same fraction of a step
    for every level. Levels past the need are capped at it: energy charged past
    the need is worth nothing.
    """
    count = len(levels)
    if count == 1 or kwh == 0:
        return values
    position = np.arange(count) + kwh / (levels[1] - levels[0])
    low = np.minimum(np.floor(position).astype(int), count - 1)
    high = np.minimum(low + 1, count - 1)
    weight = np.where(low >= count - 1, 0.0, position - np.floor(position))
    return values[..., low] * (1.0 - weight) + values[..., high] * weight


def _optimize(inputs: _Inputs, targets, caps: list[list[tuple]]):
    """Run the dynamic program for fixed import targets; return per quarter results.

    `targets` is (power charge level, other limit) in kWh per quarter: the import
    target is the lower of the two, and the car's charging is added to the other
    limit (it is exempt from the household's import limit, like the heat pump, and
    the charger's power comes on top of the battery's), but not to the power charge
    level, which caps the whole import.

    State, at the start of each quarter: the battery's SOC (`SOC_STEPS` levels from
    the shutdown SOC to the max), the previous quarter's mode (changing it costs
    `SWITCH_COST`; the last row: none, for the first quarter) and, with a car, the
    energy delivered to it so far (`_ev_levels`, 0 to the need). Without a car the
    car axis has one level and only "not charging", so the program is the plain
    battery one.

    Backward pass, from the end: the value (the cost from then on) at the end is
    minus the battery energy above its floor at `end_price`, plus the car's missing
    energy at `ev_end_price` if its deadline is after the horizon. Then for each
    quarter, for each car action (0 or a charger power, `_ev_actions`) and each
    allowed mode: `_step` gives the next SOC, import and export with the car's
    energy added to the load; the quarter's cost (energy, limit penalty, power
    charge caps, wear) plus the value at (next SOC, delivered + charged), linear
    in both (`_at_soc`, `_shifted`). The best action per mode, then the best mode
    per previous mode, is the quarter's value. At the start of the deadline quarter
    the car axis begins: the value after it does not depend on the car, plus what
    each level still misses at `shortfall_price`. Before the deadline the car axis
    is kept even where it can not charge (not plugged in yet), so the program sees
    the need coming.

    Forward pass, from the actual SOC, the mode running now and nothing delivered:
    each quarter takes the (mode, car action) with the lowest cost plus value of
    the next state (ties: the simplest mode, then the lowest power), the car's
    delivered energy is tracked exactly and capped at the need. `Plan.marginal`
    (with `probe_kwh`) is computed here, the extra load added to the car's.

    Returns (modes, socs, imports, exports, switches, marginal, ev kWh per quarter,
    car energy missing at its deadline).
    """
    battery = inputs.battery
    grid = np.linspace(battery.min_kwh, battery.max_kwh, SOC_STEPS)
    count = len(inputs.starts)
    rows = len(MODES) + 1
    switch = np.full((rows, len(MODES)), SWITCH_COST)
    for mode in range(len(MODES)):
        switch[mode, mode] = 0.0
    switch[-1, :] = 0.0
    levels = _ev_levels(inputs)
    vehicle = inputs.vehicle if len(levels) > 1 else None
    deadline = vehicle.deadline if vehicle else count
    level_caps, other_caps = targets

    def target(index: int, ev_kwh: float) -> float:
        return min(level_caps[index], other_caps[index] + ev_kwh, inputs.fuse_kwh)

    # values[t][previous mode, soc level, car level]: the cost from the start of
    # quarter t on. The car axis has one level after the deadline
    battery_end = -(grid - battery.floor_kwh) * inputs.end_price
    end = np.tile(battery_end[:, None], (rows, 1, 1))
    if vehicle and deadline >= count:
        end = end + _ev_missing(inputs, levels, at_end=True)[None, None, :]
    values = [end]
    for index in range(count - 1, -1, -1):
        following = values[-1]
        if vehicle and index + 1 == deadline < count:
            # The deadline: what each car level still misses
            following = following + _ev_missing(inputs, levels, at_end=False)
            values[-1] = following
        car = following.shape[2]
        best = np.full((len(MODES), SOC_STEPS, car), np.inf)
        for ev_kwh in _ev_actions(inputs, index, levels):
            for mode in _allowed(inputs, index):
                soc, imp, exp = _step(
                    inputs, index, MODES[mode], grid, target(index, ev_kwh), ev_kwh
                )
                cost = _quarter_cost(
                    inputs, index, imp, exp, caps[index], ev_kwh
                ) + _wear(battery, grid, soc)
                if car > 1:
                    future = _shifted(
                        _at_soc(following[mode], grid, soc), ev_kwh, levels
                    )
                else:
                    future = np.interp(soc, grid, following[mode][:, 0])[:, None]
                best[mode] = np.minimum(best[mode], cost[:, None] + future)
        # Best over the modes for each previous mode
        values.append(np.min(best[None] + switch[:, :, None, None], axis=1))
    values.reverse()

    # Forward pass from the actual state of charge and the mode running now
    soc = np.array([min(max(battery.soc_kwh, battery.min_kwh), battery.max_kwh)])
    delivered = 0.0
    previous = MODES.index(inputs.current_mode) if inputs.current_mode else rows - 1
    modes, socs, imports, exports, marginal, charged = [], [], [], [], [], []
    switches = 0

    def total(index, mode, ev_kwh, soc, extra=0.0):
        load = ev_kwh + extra
        next_soc, imp, exp = _step(
            inputs, index, MODES[mode], soc, target(index, ev_kwh), load
        )
        following = values[index + 1][mode]
        if following.shape[1] == 1:
            future = float(np.interp(next_soc[0], grid, following[:, 0]))
        else:
            row = _at_soc(following, grid, next_soc)[0]
            future = float(np.interp(min(delivered + ev_kwh, levels[-1]), levels, row))
        cost = float(
            _quarter_cost(inputs, index, imp, exp, caps[index], load)[0]
            + _wear(battery, soc, next_soc)[0]
            + future
            + switch[previous, mode]
        )
        return cost, next_soc, imp, exp

    for index in range(count):
        candidates = []
        actions = _ev_actions(inputs, index, levels)
        for mode in _allowed(inputs, index):
            for ev_kwh in actions:
                cost, next_soc, imp, exp = total(index, mode, ev_kwh, soc)
                candidates.append((cost, mode, ev_kwh, next_soc, imp, exp))
        if inputs.probe_kwh > 0:
            probed = min(
                total(index, mode, ev_kwh, soc, inputs.probe_kwh)[0]
                for mode in _allowed(inputs, index)
                for ev_kwh in actions
            )
            lowest = min(c[0] for c in candidates)
            marginal.append((probed - lowest) / inputs.probe_kwh)
        # Small tie margin: the first (simplest mode, lowest power) wins when equal
        lowest = min(c[0] for c in candidates)
        _, mode, ev_kwh, soc, imp, exp = next(
            c for c in candidates if c[0] <= lowest + 1e-9
        )
        # Modes that do the same this quarter (e.g. `sell` with an empty battery and
        # `sell_excess`) are named after the simplest one, so the inverter does not
        # run a mode the plan does not need
        mode = next(
            c[1]
            for c in candidates
            if c[2] == ev_kwh
            and all(
                abs(float(x[0]) - float(y[0])) < 1e-9
                for x, y in ((c[3], soc), (c[4], imp), (c[5], exp))
            )
        )
        switches += switch[previous, mode] > 0
        previous = mode
        delivered = min(delivered + ev_kwh, levels[-1]) if vehicle else 0.0
        modes.append(MODES[mode])
        socs.append(float(soc[0]))
        imports.append(float(imp[0]))
        exports.append(float(exp[0]))
        charged.append(ev_kwh)
    missing = max(vehicle.need_kwh - delivered, 0.0) if vehicle else 0.0
    return modes, socs, imports, exports, switches, marginal, charged, missing


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


def _candidate_levels(
    start: float, best: float, top: float, *, fine: bool
) -> list[float]:
    """Return the import target levels (kW) to try for one power charge.

    Coarse pass (`fine` False): from `start` (the level before this search round)
    up to `top` in `COARSE_STEP_KW` steps. Fine pass: `TARGET_STEP_KW` steps within
    a coarse step on both sides of the best level found, above `start`, skipping
    the levels the coarse pass tried. With the coarse step equal to the fine step
    this is the plain scan of every level. Tried in increasing order.
    """
    if not fine:
        return [
            float(level)
            for level in np.arange(
                start + COARSE_STEP_KW, top + TARGET_STEP_KW, COARSE_STEP_KW
            )
        ]
    if COARSE_STEP_KW <= TARGET_STEP_KW:
        return []
    tried = {round(start + COARSE_STEP_KW * k, 6) for k in range(1, 1000)}
    low = max(start, best - COARSE_STEP_KW) + TARGET_STEP_KW
    return [
        float(level)
        for level in np.arange(
            low, min(best + COARSE_STEP_KW, top + TARGET_STEP_KW), TARGET_STEP_KW
        )
        if round(float(level), 6) not in tried and abs(level - best) > 1e-9
    ]


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
    exempt: Sequence[float] | None = None,
    probe_kwh: float = 0.0,
    vehicle: Vehicle | None = None,
) -> Plan:
    """Return the cheapest plan.

    `starts` are local quarter starts; `prices` spot SEK/MWh excl. VAT; `load` and
    `pv` kWh per quarter; `history` the hourly mean grid import (kW, keyed by local
    hour start) so far this month, which sets the peaks already paid for; `forced`
    a mode per quarter that must be used (None: free); `current_mode` the mode the
    battery runs now, so a recalculated plan only changes it when it pays; `exempt`
    kWh per quarter of the load that the import limit does not apply to (the heat
    pump); `probe_kwh` the extra load per quarter that `Plan.marginal` prices;
    `vehicle` a car to charge in the same plan (see `Vehicle` and `_optimize`).
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
    ) + np.array(exempt if exempt is not None else [0.0] * len(starts), dtype=float)
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
        0.0,
        vehicle,
        # The car's missing energy after the horizon: bought at the cheapest price of
        # the horizon's last day
        float(np.min(buy[-END_WINDOW:])) if len(buy) else 0.0,
        limit.fuse_kw * QUARTER_HOURS if limit.fuse_kw > 0 else math.inf,
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
        """Plan for fixed import target levels."""
        caps, peak_levels = [], []
        level_caps, other_caps = [], []
        fuse = [limit.fuse_kw] if limit.fuse_kw > 0 else []
        for index, keys in enumerate(quarter_keys):
            peak_levels.append(min([*(levels[k] for k in keys), *fuse], default=None))
            level_caps.append(
                min((levels[k] * QUARTER_HOURS for k in keys), default=math.inf)
            )
            other_caps.append(
                min(
                    float(inputs.limit[index]),
                    battery.max_grid_charge_kw * QUARTER_HOURS
                    + max(inputs.net[index], 0.0),
                )
            )
            caps.append(
                [
                    (
                        levels[k] * QUARTER_HOURS,
                        charges[k][1].kr_per_kw * shares[k] / charges[k][1].peaks,
                    )
                    for k in keys
                ]
            )
        modes, socs, imports, exports, switches, marginal, ev_kwh, missing = _optimize(
            inputs, (level_caps, other_caps), caps
        )
        # As in _optimize: the car's charging comes on top of the battery's
        targets = [
            min(level, other + car_kwh, inputs.fuse_kwh)
            for level, other, car_kwh in zip(
                level_caps, other_caps, ev_kwh, strict=True
            )
        ]
        ev_missing_cost = 0.0
        car = inputs.vehicle
        if car is not None and car.need_kwh > 0:
            ev_missing_cost = float(
                _ev_missing(
                    inputs,
                    np.array([car.need_kwh - missing]),
                    at_end=car.deadline >= len(starts),
                )[0]
            )
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
        excess = float(
            np.sum(np.maximum(np.array(imports) - inputs.limit - np.array(ev_kwh), 0.0))
        )
        fuse_excess = float(
            np.sum(np.maximum(np.array(imports) - inputs.fuse_kwh, 0.0))
        )
        end_value = (socs[-1] - battery.floor_kwh) * end_price if socs else 0.0
        return Plan(
            list(starts),
            modes,
            socs,
            imports,
            exports,
            [None if math.isinf(t) else t / QUARTER_HOURS for t in targets],
            peak_levels,
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
            + (excess + fuse_excess) * LIMIT_PENALTY
            + switches * SWITCH_COST
            + ev_missing_cost,
            marginal,
            ev_kwh,
            missing,
            ev_missing_cost,
            fuse_excess,
        )

    # Coordinate search over the import targets, starting at the peaks already paid.
    # With a car every run is the joint program: searching with the car as a fixed
    # load (in its cheapest quarters at full power) was 2-3 times faster but missed
    # the low levels where the car charges slowly, losing up to 3 kr a day
    levels = {key: before for key, (_, _, before) in charges.items()}
    best = run(levels)
    top = (
        battery.max_grid_charge_kw
        + max(float(np.max(net, initial=0.0)) / QUARTER_HOURS, 0.0)
        + (max(vehicle.powers_kw) if vehicle else 0.0)
    )
    for _ in range(2):
        improved = False
        for key in charges:
            start = levels[key]
            for fine in (False, True):
                for level in _candidate_levels(start, levels[key], top, fine=fine):
                    plan = run({**levels, key: level})
                    if plan.objective < best.objective - 0.01:
                        best, levels, improved = plan, {**levels, key: level}, True
        if not improved:
            break
    if probe_kwh > 0:
        # Only for the chosen targets: the probe slows each run down
        inputs = dataclasses.replace(inputs, probe_kwh=probe_kwh)
        best = run(levels)
    return best
