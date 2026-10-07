"""Heat pump economy: when heating with the heat pump is cheaper than district heating.

Pure functions, no Home Assistant imports. The heat pump (here an air-to-air pump in
a house heated by district heating) only pays when a kWh of heat from it,
`electricity price / COP`, costs less than a kWh of district heating.

- COP: interpolated from a curve per outdoor temperature (`COP_CURVES`), `None` below
  the lowest temperature the heat pump runs at.
- District heating: an energy price per month (`DISTRICT_HEATING_PRESETS`). Fixed fees
  are left out; a fee that grows with the use (e.g. Tekniska verken's 370 kr per MWh
  used in Dec-Feb) only counts if the use is above the fee's minimum.
- Electricity: the marginal price of the electricity the heat pump would use. Bought
  electricity costs spot incl. VAT plus the grid fees; electricity that would
  otherwise be exported costs only what selling it would earn (`export_share`).
  Power charges are not included.
"""

from collections.abc import Sequence
import dataclasses
import datetime as dt
from itertools import pairwise

# (outdoor dry-bulb °C, COP) at the compressor's rated frequency, defrost included.
# Mitsubishi MSZ-AP42VG / MUZ-AP42VG, read from the service manual OBH789 (9-1,
# indoor 20 °C) and the rated point (5.4 kW heat for 1.49 kW at +7 °C, COP 3.62).
# The curves use the outdoor wet-bulb temperature, taken here as 1 °C below the
# dry-bulb. Conservative: at low output (a mild day) the COP is higher (SCOP 4.7).
# The step at about +6 °C is where defrosting stops.
COP_CURVES: dict[str, tuple[tuple[float, float], ...]] = {
    "msz_ap42": (
        (-15.0, 3.0),
        (-9.0, 3.05),
        (-4.0, 3.0),
        (1.0, 3.0),
        (5.5, 2.93),
        (6.0, 3.62),
        (7.0, 3.62),
        (11.0, 3.86),
        (16.0, 4.05),
    ),
}


@dataclasses.dataclass(frozen=True)
class Season:
    """A district heating energy price for some months."""

    months: tuple[int, ...]
    ore_per_kwh: float


# Energy price incl. VAT
DISTRICT_HEATING_PRESETS: dict[str, tuple[Season, ...]] = {
    # Tekniska verken Linköping, private customers, 2026
    # (https://tekniskaverken.se/privat/fjarrvarme/information/priser)
    "tekniska_verken_2026": (
        Season((12, 1, 2), 99.7),
        Season((3, 4, 10, 11), 76.7),
        Season((5, 6, 7, 8, 9), 13.0),
    ),
}


def cop(curve: Sequence[tuple[float, float]], outdoor: float) -> float | None:
    """Return the COP at an outdoor temperature, None below the curve.

    Linear between the points, the last point's COP above the curve.
    """
    if not curve or outdoor < curve[0][0]:
        return None
    for (t0, c0), (t1, c1) in pairwise(curve):
        if outdoor <= t1:
            return c0 + (c1 - c0) * (outdoor - t0) / (t1 - t0)
    return curve[-1][1]


def district_heating_price(
    seasons: Sequence[Season], moment: dt.datetime
) -> float | None:
    """Return the district heating price (öre/kWh) for a moment's month."""
    return next((s.ore_per_kwh for s in seasons if moment.month in s.months), None)


def evaluate(
    spot: float,
    buy_fee: float,
    sell_fee: float,
    outdoor: float,
    curve: Sequence[tuple[float, float]],
    district_price: float,
    export_share: float = 0.0,
) -> dict:
    """Compare a kWh of heat from the heat pump with one from district heating.

    `spot` is SEK/MWh excl. VAT, the fees öre/kWh, `export_share` the part of the
    heat pump's electricity that would otherwise be exported (0-1). Prices in the
    result are öre/kWh; `saving` is per kWh of heat (positive: the heat pump is
    cheaper), `break_even_spot` the spot price (öre/kWh excl. VAT) up to which bought
    electricity pays.
    """
    pump_cop = cop(curve, outdoor)
    share = min(max(export_share, 0.0), 1.0)
    bought = spot / 10 * 1.25 + buy_fee
    exported = spot / 10 + sell_fee
    electricity = share * exported + (1 - share) * bought
    result = {
        "cop": None if pump_cop is None else round(pump_cop, 2),
        "outdoor_temperature": outdoor,
        "district_heating_price": district_price,
        "electricity_price": round(electricity, 1),
        "export_share": round(share, 2),
        "heat_pump_heat_price": None,
        "saving": None,
        "break_even_spot": None,
    }
    if pump_cop is None:
        return result
    heat = electricity / pump_cop
    result["heat_pump_heat_price"] = round(heat, 1)
    result["saving"] = round(district_price - heat, 1)
    result["break_even_spot"] = round((district_price * pump_cop - buy_fee) / 1.25, 1)
    return result
