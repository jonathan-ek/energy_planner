"""Grid tariff model: energy fees, sell compensation and power charges.

Pure functions, no Home Assistant imports. A tariff is configured as a dict (YAML in
the integration options) and parsed by `tariff_from_dict`:

    energy_fees:          # öre/kWh incl. VAT on bought energy, matching rules add up
      - ore_per_kwh: 45   # energy tax, always
      - ore_per_kwh: 18
        hours: [6, 23]    # from 06:00 to 23:00, local time
      - ore_per_kwh: 9
        hours: [23, 6]    # windows may wrap past midnight
    sell_compensation:    # öre/kWh received when selling, matching rules add up
      - ore_per_kwh: 5.1
    power_charges:        # kr/kW per month, each charge is measured on its own
      - kr_per_kw: 45
        months: [11, 12, 1, 2, 3]
        weekdays: [1, 2, 3, 4, 5]   # ISO weekdays, Monday = 1 (optional)
        hours: [6, 23]
        peaks: 1                    # average of the highest N hourly means
        one_per_day: false          # true: at most one of the N peaks per day

Every rule may limit `months`, `weekdays` and `hours`; a missing key means always.
Holidays are not detected.
"""

from collections.abc import Iterable, Mapping
import dataclasses
import datetime as dt
from typing import Any

ALL_MONTHS = frozenset(range(1, 13))
ALL_WEEKDAYS = frozenset(range(1, 8))
WINTER_MONTHS = (11, 12, 1, 2, 3)
SUMMER_MONTHS = (4, 5, 6, 7, 8, 9, 10)


@dataclasses.dataclass(frozen=True)
class Period:
    """When a rule applies, in local time."""

    months: frozenset[int] = ALL_MONTHS
    weekdays: frozenset[int] = ALL_WEEKDAYS
    start_hour: int = 0
    end_hour: int = 24

    def contains(self, moment: dt.datetime) -> bool:
        """Return True if the local time `moment` is in the period."""
        if moment.month not in self.months or moment.isoweekday() not in self.weekdays:
            return False
        return in_hours(moment.hour, self.start_hour, self.end_hour)


@dataclasses.dataclass(frozen=True)
class Rate:
    """A fee in öre/kWh during a period."""

    ore_per_kwh: float
    period: Period = Period()


@dataclasses.dataclass(frozen=True)
class PowerCharge:
    """A monthly charge on the highest hourly mean power during a period."""

    kr_per_kw: float
    period: Period = Period()
    peaks: int = 1
    one_per_day: bool = False


@dataclasses.dataclass(frozen=True)
class Tariff:
    """A household's grid tariff."""

    energy_fees: tuple[Rate, ...] = ()
    sell_compensation: tuple[Rate, ...] = ()
    power_charges: tuple[PowerCharge, ...] = ()

    def buy_fee(self, moment: dt.datetime) -> float:
        """Return the fees in öre/kWh incl. VAT on energy bought at `moment`."""
        return _sum_rates(self.energy_fees, moment)

    def sell_fee(self, moment: dt.datetime) -> float:
        """Return the compensation in öre/kWh for energy sold at `moment`."""
        return _sum_rates(self.sell_compensation, moment)

    def power_cost(self, hourly_kw: Mapping[dt.datetime, float]) -> float:
        """Return the power charges in kr for hourly mean grid import.

        `hourly_kw` maps local hour starts to the mean import in kW. Each month in it
        is charged separately.
        """
        months: dict[tuple[int, int], dict[dt.datetime, float]] = {}
        for hour, kw in hourly_kw.items():
            months.setdefault((hour.year, hour.month), {})[hour] = kw
        return sum(
            charge.kr_per_kw * charged_peak(charge, month)
            for charge in self.power_charges
            for month in months.values()
        )


def in_hours(hour: int, start_hour: int, end_hour: int) -> bool:
    """Return True if `hour` is in [start_hour, end_hour), which may wrap midnight."""
    if start_hour == end_hour or (start_hour == 0 and end_hour == 24):
        return True
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _sum_rates(rates: Iterable[Rate], moment: dt.datetime) -> float:
    return sum(rate.ore_per_kwh for rate in rates if rate.period.contains(moment))


def charged_peak(charge: PowerCharge, hourly_kw: Mapping[dt.datetime, float]) -> float:
    """Return the power in kW that `charge` is billed for, within one month.

    The mean of the `charge.peaks` highest hourly values in the charge's period, or
    of the daily maximums if `one_per_day`. With fewer values than peaks, the missing
    ones count as zero (the month is not over yet).
    """
    values = {
        hour: kw for hour, kw in hourly_kw.items() if charge.period.contains(hour)
    }
    if charge.one_per_day:
        daily: dict[dt.date, float] = {}
        for hour, kw in values.items():
            daily[hour.date()] = max(daily.get(hour.date(), 0.0), kw)
        candidates = list(daily.values())
    else:
        candidates = list(values.values())
    highest = sorted(candidates, reverse=True)[: charge.peaks]
    return sum(highest) / charge.peaks


def _period(rule: Mapping[str, Any]) -> Period:
    months = frozenset(int(m) for m in rule.get("months", ALL_MONTHS))
    weekdays = frozenset(int(d) for d in rule.get("weekdays", ALL_WEEKDAYS))
    start_hour, end_hour = (int(h) for h in rule.get("hours", (0, 24)))
    if not months or not months <= ALL_MONTHS:
        msg = f"months must be 1-12: {sorted(months)}"
        raise ValueError(msg)
    if not weekdays or not weekdays <= ALL_WEEKDAYS:
        msg = f"weekdays must be 1-7 (Monday = 1): {sorted(weekdays)}"
        raise ValueError(msg)
    if not (0 <= start_hour <= 24 and 0 <= end_hour <= 24):
        msg = f"hours must be 0-24: {[start_hour, end_hour]}"
        raise ValueError(msg)
    return Period(months, weekdays, start_hour, end_hour)


def _rates(rules: Iterable[Mapping[str, Any]]) -> tuple[Rate, ...]:
    return tuple(Rate(float(rule["ore_per_kwh"]), _period(rule)) for rule in rules)


def _power_charge(rule: Mapping[str, Any]) -> PowerCharge:
    peaks = int(rule.get("peaks", 1))
    if peaks < 1:
        msg = f"peaks must be at least 1: {peaks}"
        raise ValueError(msg)
    return PowerCharge(
        float(rule["kr_per_kw"]),
        _period(rule),
        peaks,
        bool(rule.get("one_per_day", False)),
    )


def tariff_from_dict(data: Mapping[str, Any]) -> Tariff:
    """Parse a tariff dict (see the module docstring), raising ValueError if invalid."""
    try:
        return Tariff(
            _rates(data.get("energy_fees") or ()),
            _rates(data.get("sell_compensation") or ()),
            tuple(_power_charge(rule) for rule in data.get("power_charges") or ()),
        )
    except (KeyError, TypeError) as err:
        msg = f"invalid tariff: {err!r}"
        raise ValueError(msg) from err


def flat_tariff(network_cost: float, network_compensation: float) -> Tariff:
    """Return a tariff with a constant fee and compensation and no power charge."""
    return Tariff((Rate(network_cost),), (Rate(network_compensation),))


# Tekniska verken Linköping, Konsumtionsabonnemang "Prislista alternativ", 2026, incl.
# VAT (https://www.tekniskaverken.se/privat/elnat/priser-ersattningar)
TEKNISKA_VERKEN_ALTERNATIV: dict[str, Any] = {
    "energy_fees": [
        {"ore_per_kwh": 45.0},
        {"ore_per_kwh": 18.0, "hours": [6, 23]},
        {"ore_per_kwh": 9.0, "hours": [23, 6]},
    ],
    "sell_compensation": [
        {"ore_per_kwh": 5.1},
        {
            "ore_per_kwh": 2.1,
            "months": [1, 2, 3, 11, 12],
            "weekdays": [1, 2, 3, 4, 5],
            "hours": [6, 22],
        },
    ],
    "power_charges": [
        {"kr_per_kw": 45.0, "months": list(WINTER_MONTHS), "hours": [6, 23]},
        {"kr_per_kw": 23.0, "months": list(SUMMER_MONTHS), "hours": [6, 23]},
        {"kr_per_kw": 12.0, "months": list(WINTER_MONTHS), "hours": [23, 6]},
        {"kr_per_kw": 8.0, "months": list(SUMMER_MONTHS), "hours": [23, 6]},
    ],
}

# E.ON Energidistribution, 20 A, 2026, from a customer's price list (Virserum): no
# power charge; selling earns spot + 10 öre. E.ON's prices differ between areas
EON_20A: dict[str, Any] = {
    "energy_fees": [
        {"ore_per_kwh": 45.0},  # energy tax incl. VAT
        {"ore_per_kwh": 32.3},  # transfer fee
    ],
    "sell_compensation": [{"ore_per_kwh": 10.0}],
    "power_charges": [],
}

TARIFF_PRESETS: dict[str, dict[str, Any]] = {
    "tekniska_verken_alternativ": TEKNISKA_VERKEN_ALTERNATIV,
    "eon_20a": EON_20A,
}


def import_limit(
    moment: dt.datetime, limit_kw: float, start_hour: int, end_hour: int
) -> float | None:
    """Return the grid import limit in kW at `moment`, or None if unlimited.

    `limit_kw` <= 0 turns the limit off. The limit is a household choice, not part
    of the tariff: it keeps the hourly mean import below a level, e.g. so the battery
    is not charged from the grid during hours with an expensive power charge.
    """
    if limit_kw <= 0 or not in_hours(moment.hour, start_hour, end_hour):
        return None
    return limit_kw
