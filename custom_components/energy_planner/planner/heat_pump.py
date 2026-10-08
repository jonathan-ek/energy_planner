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
- Draw: the heat the heat pump delivers is taken as proportional to the indoor-outdoor
  difference (`heat_loss`, measured from the house load), so it draws
  `loss * (indoor - outdoor) / COP` (`draw`).
"""

from collections.abc import Mapping, Sequence
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


# Heat loss measurement: hours with a smaller indoor-outdoor difference say little
LOSS_MIN_DIFFERENCE = 3.0
LOSS_MIN_HOURS = 6
# Highest draw (kW) the model gives; the MSZ-AP42 draws at most about 1.9 kW
MAX_DRAW_KW = 2.0


def heat_loss[K](
    excess: Mapping[K, float],
    indoor: Mapping[K, float],
    outdoor: Mapping[K, float],
    curve: Sequence[tuple[float, float]],
) -> tuple[float | None, int]:
    """Measure the heat the heat pump delivers per °C indoor-outdoor (kW/K).

    `excess` is its measured draw per hour (kW), `indoor` / `outdoor` the hour's mean
    temperatures. Heat = draw * COP, so loss = draw * COP / (indoor - outdoor).
    Returns the mean and the number of hours, None for the mean with fewer than
    LOSS_MIN_HOURS.
    """
    losses = []
    for key, kw in excess.items():
        inside, outside = indoor.get(key), outdoor.get(key)
        if inside is None or outside is None:
            continue
        pump_cop = cop(curve, outside)
        if pump_cop is None or inside - outside < LOSS_MIN_DIFFERENCE:
            continue
        losses.append(kw * pump_cop / (inside - outside))
    if len(losses) < LOSS_MIN_HOURS:
        return None, len(losses)
    return max(sum(losses) / len(losses), 0.0), len(losses)


def draw(
    loss: float, indoor: float, outdoor: float, curve: Sequence[tuple[float, float]]
) -> float:
    """Return what the heat pump draws (kW) to hold indoor at an outdoor temperature."""
    pump_cop = cop(curve, outdoor)
    if pump_cop is None:
        return 0.0
    return min(max(loss * (indoor - outdoor) / pump_cop, 0.0), MAX_DRAW_KW)


def schedule(
    marginal: Sequence[float],
    cops: Sequence[float | None],
    district: Sequence[float | None],
    heating: Sequence[bool],
    margin: float,
    min_quarters: int = 2,
) -> list[bool]:
    """Return per quarter whether the heat pump should heat.

    `marginal` is what the heat pump's electricity costs in each quarter (öre/kWh, the
    battery plan's Plan.marginal), `district` the district heating price (öre/kWh),
    `heating` whether heat is needed. It runs where its heat is at least `margin`
    öre/kWh cheaper. Gaps and runs shorter than `min_quarters` are removed (gaps
    first), so the compressor does not start and stop every quarter; a run at the
    start or end of the horizon may continue outside it and is kept.
    """
    on = [
        need
        and pump_cop is not None
        and price is not None
        and price - cost / pump_cop >= margin
        for cost, pump_cop, price, need in zip(
            marginal, cops, district, heating, strict=True
        )
    ]

    def runs(value):
        start = None
        for index, state in enumerate([*on, not value]):
            if state == value and start is None:
                start = index
            elif state != value and start is not None:
                yield start, index
                start = None

    for value in (False, True):
        for start, end in list(runs(value)):
            if end - start < min_quarters and start > 0 and end < len(on):
                on[start:end] = [not value] * (end - start)
    return on
