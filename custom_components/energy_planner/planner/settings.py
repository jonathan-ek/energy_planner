"""Settings: every key in the config store with its one default, and live readings.

The entity platforms take their defaults from `SETTINGS` (the entity of a setting
has the setting's key as its id), so a setting's default is the same whether the
entity has loaded or not. Keys without a platform are set in the config store only
(the sensors the planner reads, the heat pump model, ...).

Values are read with `get` / `number`, or as typed groups in domain units
(`battery`, `car`, `heat_pump`, `fuse`). A setting with `replans` makes the battery
plan be recalculated when its entity changes (`replan_entity_ids`). A setting the
plan reads must have it, which tests/planner/test_settings.py checks.

`reading` gives the live value of a sensor a setting points to.
"""

from collections.abc import Mapping
import dataclasses
import datetime as dt
from typing import TYPE_CHECKING, Any

from ..const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclasses.dataclass(frozen=True)
class Setting:
    """A config store key with its default.

    `platform`: the entity platform showing it (None: config store only).
    `replans`: a change of its entity recalculates the battery plan.
    """

    key: str
    default: Any
    platform: str | None = None
    replans: bool = False


def _number(key: str, default: float, *, replans: bool = False) -> Setting:
    return Setting(key, default, "number", replans)


SETTINGS: dict[str, Setting] = {
    setting.key: setting
    for setting in (
        # Off: the plan is still made and shown, but the battery does not follow it
        Setting("enabled", True, "switch"),
        # Set up from the config entry
        Setting("nordpool_entity_id", None),
        # The battery
        _number("battery_capacity", 25600, replans=True),  # Wh
        _number("battery_shutdown_soc", 20, replans=True),
        # The battery plan does not discharge below it; the inverter may, down to
        # the shutdown SOC, to shave peaks from unexpected load
        _number("battery_reserve_soc", 20, replans=True),
        _number("battery_max_soc", 90, replans=True),
        _number("max_charge_current", 16, replans=True),
        _number("max_discharge_current", 16, replans=True),
        # Round trip, %
        _number("battery_efficiency", 85, replans=True),
        # öre per kWh taken out
        _number("battery_wear_cost", 20, replans=True),
        Setting("battery_soc_sensor", "sensor.solis_s6_solis_battery_soc"),
        Setting("battery_voltage_sensor", "sensor.solis_s6_solis_battery_voltage"),
        # The grid
        _number("network_cost", 0, replans=True),  # öre/kWh
        _number("network_compensation", 0, replans=True),  # öre/kWh
        _number("grid_import_limit", 0, replans=True),  # kW, 0: no limit
        _number("grid_import_limit_start", 6, replans=True),
        _number("grid_import_limit_end", 23, replans=True),
        # Energy counter of the grid import (the HAN port); its hourly changes this
        # month give the power peaks already paid for
        Setting("grid_import_sensor", "sensor.matarstallning_aktiv_energi_uttag"),
        # A per phase, 0: not limited
        _number("main_fuse", 0, replans=True),
        # A per phase kept free by the fuse guard
        _number("fuse_margin", 2),
        # The fuse guard's inputs (planner/fuse_guard.py), no defaults
        Setting("grid_phase_import_sensors", None),
        Setting("grid_phase_export_sensors", None),
        Setting("inverter_ac_power_sensor", None),
        Setting("inverter_grid_power_sensor", None),
        Setting("ev_phase_current_sensors", None),
        Setting("ev_charger_current_number", None),
        # The forecast
        Setting("forecast_load_model", "profile", "select"),
        Setting("forecast_pv_calibration", "daily", "select"),
        _number("forecast_weekend_reserve", 4),  # kWh
        _number("forecast_reserve_start", 18),
        _number("forecast_reserve_end", 22),
        Setting("forecast_load_sensor", "sensor.solis_s6_solis_household_load_power"),
        Setting("forecast_pv_sensor", "sensor.solis_s6_solis_total_pv_power"),
        Setting("forecast_ev_sensor", "sensor.ehwuhqtp_effekt"),
        # Local calendar with planned loads (sauna, laundry, EV charging, ...)
        Setting("forecast_calendar", "calendar.energiplan"),
        # The heat pump (planner/heat_pump.py)
        # W while heating until the draw is measured, 0: not planned
        _number("heat_pump_power", 300, replans=True),
        # öre per kWh of heat it must save against district heating
        _number("heat_pump_margin", 5, replans=True),
        # °C outdoor: heat is only needed below it
        _number("heat_pump_heating_limit", 15, replans=True),
        Setting("heat_pump_model", "msz_ap42"),
        Setting("district_heating", "tekniska_verken_2026"),
        # Hours in `heat` mode measure its draw and take it out of the load history
        Setting("heat_pump_climate", "climate.hallen"),
        Setting("outdoor_temperature_sensor", "sensor.gw1100a_outdoor_temperature"),
        # In the heat pump's room; with the outdoor temperature it gives the draw
        Setting("indoor_temperature_sensor", "sensor.gw1100a_indoor_temperature"),
        # The car (planner/ev.py). It is planned only while ev_charger_status_sensor
        # says it is plugged in; the sensors have no defaults
        _number("ev_charger_max_power", 11000, replans=True),  # W
        # W, 0: only on/off at the highest
        _number("ev_charger_min_power", 0, replans=True),
        _number("ev_battery_capacity", 75, replans=True),  # kWh usable
        Setting("ev_ready_time", dt.time(6, 30), "time", replans=True),
        # A trip: the need by then if that is before the next working morning
        Setting("ev_departure", None, "datetime", replans=True),
        Setting("ev_charger_status_sensor", None),
        # kWh delivered since it was plugged in
        Setting("ev_session_energy_sensor", None),
        # The car's SOC and charge limit (%), for the need when both are known
        Setting("ev_soc_sensor", None),
        Setting("ev_charge_limit_sensor", None),
    )
}


def default(key: str) -> Any:
    """Return a setting's default."""
    return SETTINGS[key].default


def get(config: Mapping[str, Any], key: str) -> Any:
    """Return a setting, its default when it is not set."""
    value = config.get(key)
    return default(key) if value is None else value


def number(config: Mapping[str, Any], key: str) -> float:
    """Return a numeric setting as a float."""
    return float(get(config, key))


def replan_entity_ids() -> list[str]:
    """Return the entities whose changes recalculate the battery plan."""
    return [
        f"{setting.platform}.{DOMAIN}_{setting.key}"
        for setting in SETTINGS.values()
        if setting.replans and setting.platform is not None
    ]


@dataclasses.dataclass(frozen=True)
class BatterySettings:
    """The battery's settings."""

    capacity_kwh: float
    shutdown_soc: float  # %
    reserve_soc: float  # %
    max_soc: float  # %
    max_charge_a: float
    max_discharge_a: float
    efficiency: float  # round trip, 0-1
    wear_sek_per_kwh: float  # per kWh taken out
    soc_sensor: str
    voltage_sensor: str


def battery(config: Mapping[str, Any]) -> BatterySettings:
    """Return the battery's settings."""
    return BatterySettings(
        capacity_kwh=number(config, "battery_capacity") / 1000,
        shutdown_soc=number(config, "battery_shutdown_soc"),
        reserve_soc=number(config, "battery_reserve_soc"),
        max_soc=number(config, "battery_max_soc"),
        max_charge_a=number(config, "max_charge_current"),
        max_discharge_a=number(config, "max_discharge_current"),
        efficiency=number(config, "battery_efficiency") / 100,
        wear_sek_per_kwh=number(config, "battery_wear_cost") / 100,
        soc_sensor=get(config, "battery_soc_sensor"),
        voltage_sensor=get(config, "battery_voltage_sensor"),
    )


@dataclasses.dataclass(frozen=True)
class CarSettings:
    """The car's and its charger's settings."""

    status_sensor: str
    session_energy_sensor: str | None
    soc_sensor: str | None
    charge_limit_sensor: str | None
    battery_kwh: float
    charger_min_kw: float  # 0: only on/off at the highest
    charger_max_kw: float
    ready_time: dt.time
    departure: dt.datetime | None


def car(config: Mapping[str, Any]) -> CarSettings | None:
    """Return the car's settings, None without a charger status sensor."""
    status = get(config, "ev_charger_status_sensor")
    if not status:
        return None
    return CarSettings(
        status_sensor=status,
        session_energy_sensor=get(config, "ev_session_energy_sensor") or None,
        soc_sensor=get(config, "ev_soc_sensor") or None,
        charge_limit_sensor=get(config, "ev_charge_limit_sensor") or None,
        battery_kwh=number(config, "ev_battery_capacity"),
        charger_min_kw=number(config, "ev_charger_min_power") / 1000,
        charger_max_kw=number(config, "ev_charger_max_power") / 1000,
        ready_time=get(config, "ev_ready_time"),
        departure=get(config, "ev_departure"),
    )


@dataclasses.dataclass(frozen=True)
class HeatPumpSettings:
    """The heat pump's settings."""

    power_kw: float  # while heating until the draw is measured, 0: not planned
    margin_ore_per_kwh: float  # per kWh of heat
    heating_limit_c: float  # outdoor
    model: str
    district_heating: str
    climate: str
    outdoor_sensor: str
    indoor_sensor: str


def heat_pump(config: Mapping[str, Any]) -> HeatPumpSettings:
    """Return the heat pump's settings."""
    return HeatPumpSettings(
        power_kw=number(config, "heat_pump_power") / 1000,
        margin_ore_per_kwh=number(config, "heat_pump_margin"),
        heating_limit_c=number(config, "heat_pump_heating_limit"),
        model=get(config, "heat_pump_model"),
        district_heating=get(config, "district_heating"),
        climate=get(config, "heat_pump_climate"),
        outdoor_sensor=get(config, "outdoor_temperature_sensor"),
        indoor_sensor=get(config, "indoor_temperature_sensor"),
    )


@dataclasses.dataclass(frozen=True)
class FuseSettings:
    """The main fuse guard's settings."""

    main_fuse_a: float  # per phase
    margin_a: float
    charger_max_kw: float
    phase_import_sensors: tuple[str, ...]
    phase_export_sensors: tuple[str, ...]
    inverter_ac_power_sensor: str | None
    inverter_grid_power_sensor: str | None
    ev_phase_current_sensors: tuple[str, ...]
    ev_charger_current_number: str | None
    ev_charger_status_sensor: str | None


def fuse(config: Mapping[str, Any]) -> FuseSettings | None:
    """Return the fuse guard's settings, None without a main fuse or phase sensors."""
    main_fuse = number(config, "main_fuse")
    imports = tuple(get(config, "grid_phase_import_sensors") or ())
    if main_fuse <= 0 or not imports:
        return None
    return FuseSettings(
        main_fuse_a=main_fuse,
        margin_a=number(config, "fuse_margin"),
        charger_max_kw=number(config, "ev_charger_max_power") / 1000,
        phase_import_sensors=imports,
        phase_export_sensors=tuple(get(config, "grid_phase_export_sensors") or ()),
        inverter_ac_power_sensor=get(config, "inverter_ac_power_sensor") or None,
        inverter_grid_power_sensor=get(config, "inverter_grid_power_sensor") or None,
        ev_phase_current_sensors=tuple(get(config, "ev_phase_current_sensors") or ()),
        ev_charger_current_number=get(config, "ev_charger_current_number") or None,
        ev_charger_status_sensor=get(config, "ev_charger_status_sensor") or None,
    )


# Power units `reading` converts between
_WATTS = {"W": 1.0, "kW": 1000.0}


def reading(
    hass: "HomeAssistant",
    entity_id: str | None,
    *,
    unit: str | None = None,
    max_age: dt.timedelta | None = None,
    now: dt.datetime | None = None,
) -> float | None:
    """Return a sensor's value, None if not configured, missing, old or not a number.

    `unit` ("W" or "kW") converts from the sensor's own power unit. With `max_age` a
    value last reported longer ago than that before `now` is old.
    """
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    if max_age is not None and now is not None and now - state.last_reported > max_age:
        return None
    if unit in _WATTS:
        own = state.attributes.get("unit_of_measurement")
        if own in _WATTS:
            value *= _WATTS[own] / _WATTS[unit]
    return value
