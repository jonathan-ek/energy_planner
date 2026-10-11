"""Main fuse guard: keep the busiest phase below the main fuse, from live readings.

Pure functions, no Home Assistant imports. The battery plan keeps each quarter's
mean import below the fuse (`battery_optimizer.Limit.fuse_kw`), but as if the load
were spread evenly over the phases, and the house's load is not: at the owner's
parents (Oct 2026, 35 A) the battery charged from the grid at 20 kW with the phases
at 25, 27 and 35 A. Only two loads can be controlled, the car charger (its current
per phase) and the battery's grid charging (the inverter's peak shaving max usable
grid power, a limit on the total import); both are three-phase and balanced. So,
from the live import per phase (`limits`):

1. The house's own load per phase is the phase's import minus the inverter's draw
   (spread evenly) and minus the car's current.
2. The room per phase for the controllable loads is (fuse - margin) x voltage minus
   the house's load; the busiest phase sets it (`room_a`).
3. The car comes first while it should charge (`ev_wanted`): its current
   (`ev_current_a`) is raised to the room, up to the charger's highest, and lowered
   only when the battery can not take enough off (see `limits`); below `EV_MIN_A`
   it can not charge, 0.
4. The battery gets the rest: the inverter's grid limit (`grid_limit_kw`, total
   import) is the house's and the car's load plus the room left per phase (after
   the car's setting, or its draw if higher) times the phases. Above it the inverter's
   peak shaving covers the load from the battery, so the limit also shaves the
   house's peaks while the battery charges or holds.

`smooth` steadies the values the devices get: a lower value is used at once (with
a margin, so the next small rise does not write again), a higher one only once it
is a step higher and the last change is `interval` old. The charger's current is
a cloud setting, rate limited (`EV_RAISE_INTERVAL`).
"""

import dataclasses
import datetime as dt
import math

VOLTAGE = 230.0
# The lowest current a charger charges at (IEC 61851)
EV_MIN_A = 6
GRID_STEP_KW = 0.5
GRID_MARGIN_KW = 0.2
GRID_RAISE_INTERVAL = dt.timedelta(minutes=1)
EV_STEP_A = 1.0
EV_RAISE_INTERVAL = dt.timedelta(minutes=15)


@dataclasses.dataclass(frozen=True)
class Reading:
    """Live readings: import per phase (kW, export negative), the controllable loads.

    `inverter_kw` is what the inverter draws from the house's wiring (charging the
    battery from the grid; negative while it delivers power), `ev_a` the car's
    current now (A per phase, the highest phase) and `ev_setting_a` the charger's
    current setting.
    """

    phase_kw: tuple[float, ...]
    inverter_kw: float = 0.0
    ev_a: float = 0.0
    ev_setting_a: float | None = None


def limits(
    reading: Reading,
    fuse_a: float,
    margin_a: float,
    ev_max_a: float,
    *,
    ev_wanted: bool,
) -> dict:
    """Return the room on the busiest phase and the limits for the car and battery.

    See the module docstring. `ev_current_a` is the charger's current to set (whole
    A, 0: it can not charge), `grid_limit_kw` the inverter's max usable grid power;
    `phase_a` and `house_a` are the currents per phase now, all and without the
    controllable loads, `over_a` how far the busiest phase is above the fuse minus
    the margin.

    The car's current is reported about once a minute and the meter more often, so
    just after the car starts its draw looks like the house's. Its setting is
    therefore only lowered while a phase is over and the battery can not take
    enough off (by the excess), and raised to its share of the room; while it
    should not charge, it is left alone.
    """
    phases = len(reading.phase_kw)
    allowed_w = (fuse_a - margin_a) * VOLTAGE
    inverter_w = reading.inverter_kw * 1000 / phases
    car_w = reading.ev_a * VOLTAGE
    house_w = [kw * 1000 - inverter_w - car_w for kw in reading.phase_kw]
    room_w = min(allowed_w - w for w in house_w)
    over_w = max(kw * 1000 for kw in reading.phase_kw) - allowed_w
    setting = min(
        reading.ev_setting_a if reading.ev_setting_a is not None else ev_max_a,
        ev_max_a,
    )
    ev_a = setting
    if ev_wanted:
        excess_w = over_w - max(inverter_w, 0.0)
        if excess_w > 0:
            ev_a = min(setting, reading.ev_a) - excess_w / VOLTAGE
        else:
            ev_a = max(setting, min(room_w / VOLTAGE, ev_max_a))
    ev_a = math.floor(ev_a + 1e-9)
    if ev_a < EV_MIN_A:
        ev_a = 0
    # The car may draw up to its setting while it should charge
    reserved_w = max(ev_a * VOLTAGE if ev_wanted else 0.0, car_w)
    grid_w = sum(house_w) + phases * (car_w + room_w - reserved_w)
    return {
        "phase_a": [round(kw * 1000 / VOLTAGE, 1) for kw in reading.phase_kw],
        "house_a": [round(w / VOLTAGE, 1) for w in house_w],
        "room_a": round(room_w / VOLTAGE, 1),
        "over_a": round(over_w / VOLTAGE, 1),
        "ev_current_a": ev_a,
        "grid_limit_kw": round(max(grid_w, 0.0) / 1000, 2),
    }


def smooth(
    previous: tuple[float, dt.datetime] | None,
    value: float,
    now: dt.datetime,
    step: float,
    interval: dt.timedelta,
    margin: float = 0.0,
) -> tuple[float, dt.datetime]:
    """Return the (value, changed at) to use, given the last one and a new value.

    A lower value is used at once, `margin` lower so the next small rise does not
    change it again; a higher one once it is at least a step higher and the last
    change is `interval` old.
    """
    if previous is None:
        return value, now
    last, changed = previous
    if value < last:
        return max(value - margin, 0.0), now
    if value >= last + step and now - changed >= interval:
        return value, now
    return previous
