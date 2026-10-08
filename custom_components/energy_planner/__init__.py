import logging
import datetime as dt
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    callback,
    Event,
    EventStateChangedData,
)
from homeassistant.const import Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_utils

from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started

from .const import (
    DEFAULT_BATTERY_SOC_SENSOR,
    DOMAIN,
    DATE_TIME_ENTITIES,
    NUMBER_ENTITIES,
    SWITCH_ENTITIES,
    SELECT_ENTITIES,
    TIME_ENTITIES,
    SLOT_COUNT,
)
from .planner import (
    async_request_plan,
    async_update_forecast,
    expected_soc,
    write_plan_sensors,
    basic_planner,
    dynamic_planner,
    cheapest_hours_planner,
    add_manual_slots,
    clear_passed_slots,
    update_entities,
    price_peak_planner,
)
from .store import async_save_to_store, async_load_from_store

_LOGGER = logging.getLogger(__name__)

# Settings that change the battery plan: recalculate it when they change
PLAN_SETTINGS = (
    "battery_capacity",
    "battery_shutdown_soc",
    "battery_max_soc",
    "battery_wear_cost",
    "max_charge_current",
    "max_discharge_current",
    "price_peak_efficiency_factor",
    "network_cost",
    "network_compensation",
    "grid_import_limit",
    "grid_import_limit_start",
    "grid_import_limit_end",
    "heat_pump_power",
    "heat_pump_margin",
    "heat_pump_heating_limit",
)
# Recalculate the plan when the actual SOC is this many % from the plan
SOC_DRIFT = 5.0
# ... but not more often than this
SOC_DRIFT_INTERVAL = dt.timedelta(minutes=5)
PLATFORMS = [
    Platform.BUTTON,
    Platform.DATETIME,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]
# Nord Pool publishes the prices for the next day in Stockholm time
NORDPOOL_TIME_ZONE = ZoneInfo("Europe/Stockholm")
NORDPOOL_UPDATE_HOUR = 15


async def async_setup_data_structure(hass: HomeAssistant):
    """Set up the data structure."""

    async def save():
        for data_store in ["values", "config", "manual_slots"]:
            await async_save_to_store(hass, data_store, hass.data[DOMAIN][data_store])

    hass.data[DOMAIN] = {
        "values": {},
        "config": {},
        "manual_slots": [],
        DATE_TIME_ENTITIES: {},
        TIME_ENTITIES: {},
        NUMBER_ENTITIES: {},
        SWITCH_ENTITIES: {},
        SELECT_ENTITIES: {},
        "save": save,
    }
    hass.data[DOMAIN]["values"] = await async_load_from_store(hass, "values")
    hass.data[DOMAIN]["config"] = await async_load_from_store(hass, "config")
    hass.data[DOMAIN]["manual_slots"] = (
        await async_load_from_store(hass, "manual_slots") or []
    )
    parse_stored_data(hass)


def parse_stored_data(hass: HomeAssistant):
    """Convert dates and times, which are stored as strings, back to objects."""
    values = hass.data[DOMAIN]["values"]
    for i in range(1, SLOT_COUNT + 1):
        key = f"slot_{i}_date_time_start"
        if type(values.get(key)) is str:
            values[key] = dt_utils.parse_datetime(values[key])
    config = hass.data[DOMAIN]["config"]
    for key in ["earliest_charge_time", "earliest_discharge_time"]:
        if type(config.get(key)) is str:
            config[key] = dt.time.fromisoformat(config[key])
    for slot in hass.data[DOMAIN]["manual_slots"]:
        for key in ["start", "end"]:
            if type(slot.get(key)) is str:
                slot[key] = dt_utils.parse_datetime(slot[key])


async def run_planner(hass: HomeAssistant) -> None:
    """Run the selected planner."""
    planner_state = hass.data[DOMAIN]["config"].get("planner_state", "basic")
    if planner_state == "off":
        _LOGGER.info("Planner is off")
        return
    if planner_state == "basic":
        _LOGGER.info("Running basic planner")
        await basic_planner(hass)
        return
    if planner_state == "cheapest hours":
        _LOGGER.info("Running cheapest hours planner")
        await cheapest_hours_planner(hass)
        return
    if planner_state == "price peak":
        _LOGGER.info("Running price peak planner")
        await price_peak_planner(hass)
        return
    if planner_state == "dynamic":
        _LOGGER.info("Running dynamic planner")
        await dynamic_planner(hass)
        return
    raise ValueError("Invalid planner state")


async def async_setup(hass: HomeAssistant, config):
    """Set up the Energy Planner component."""
    if DOMAIN not in hass.data:
        await async_setup_data_structure(hass)

    @callback
    async def add_slot_service(call: ServiceCall) -> None:
        """Service to add a slot."""
        try:
            start_datetime = dt_utils.as_local(
                dt.datetime.fromisoformat(call.data["start"])
            )
            end_datetime = dt_utils.as_local(
                dt.datetime.fromisoformat(call.data["end"])
            )
            state = call.data.get("state")
            soc = call.data.get("soc")
            if start_datetime > end_datetime:
                raise ValueError("Start must be before end")
            if state not in [
                "charge",
                "discharge",
                "sell",
                "sell-excess",
                "discard-excess",
                "pause",
                "off",
            ]:
                raise ValueError("Invalid state")
        except Exception as e:
            _LOGGER.error("Error adding slot: %s", e)
            raise ServiceValidationError("Invalid data") from e
        hass.data[DOMAIN]["manual_slots"].append(
            {"start": start_datetime, "end": end_datetime, "state": state, "soc": soc}
        )
        await add_manual_slots(hass)
        await update_entities(hass)
        await hass.data[DOMAIN]["save"]()
        await async_request_plan(hass, "manual slot added")
        _LOGGER.info("Received data: %s", call.data)

    @callback
    async def run_planner_service(call: ServiceCall) -> None:
        """Service to run the planner."""
        _LOGGER.info("Running planner: %s", config)
        _LOGGER.info("Received planning data: %s", call.data)
        await run_planner(hass)

    @callback
    async def clear_manual_slots_service(call: ServiceCall) -> None:
        """Service to run the planner."""
        _LOGGER.info("Running planner: %s", config)
        _LOGGER.info("Received planning data: %s", call.data)
        hass.data[DOMAIN]["manual_slots"] = []
        await hass.data[DOMAIN]["save"]()
        await async_request_plan(hass, "manual slots cleared")

    async def update_forecast_service(call: ServiceCall) -> None:
        """Service to recalculate the forecast."""
        await async_update_forecast(hass)

    async def update_battery_plan_service(call: ServiceCall) -> None:
        """Service to recalculate the battery plan."""
        await async_request_plan(hass, "service")

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, "add_slot", add_slot_service)
    hass.services.async_register(DOMAIN, "update_forecast", update_forecast_service)
    hass.services.async_register(
        DOMAIN, "update_battery_plan", update_battery_plan_service
    )
    hass.services.async_register(DOMAIN, "run_planner", run_planner_service)
    hass.services.async_register(
        DOMAIN, "clear_manual_slots", clear_manual_slots_service
    )

    # Return boolean to indicate that initialization was successful.
    return True


async def state_automation_listener(event: Event[EventStateChangedData]):
    """Handle state change event."""
    _LOGGER.debug(f"state_automation_listener: {event.data}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Modbus from a config entry."""
    # Set up the platforms associated with this integration
    if DOMAIN not in hass.data:
        await async_setup_data_structure(hass)
    hass.data[DOMAIN]["config"]["entry_id"] = entry.entry_id
    hass.data[DOMAIN]["config"]["nordpool_entity_id"] = entry.data["nordpool_entity_id"]
    hass.data[DOMAIN]["options"] = dict(entry.options)

    async def options_updated(hass: HomeAssistant, entry: ConfigEntry):
        # Read by get_tariff, no reload needed
        hass.data[DOMAIN]["options"] = dict(entry.options)
        await async_request_plan(hass, "tariff changed")

    entry.async_on_unload(entry.add_update_listener(options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def update_schedule(now: dt.datetime):
        # Checked every hour to follow daylight saving changes
        if now.astimezone(NORDPOOL_TIME_ZONE).hour != NORDPOOL_UPDATE_HOUR:
            return
        await run_planner(hass)

    entry.async_on_unload(
        async_track_utc_time_change(hass, update_schedule, minute=31, second=15)
    )

    async def check_schedule(now: dt.datetime):
        await clear_passed_slots(hass)
        # The action follows slot 1 and the plan's current quarter
        write_plan_sensors(hass)

    entry.async_on_unload(
        async_track_time_interval(hass, check_schedule, dt.timedelta(minutes=1))
    )

    async def update_forecast(*_):
        # A failing forecast or plan must not stop the timers
        try:
            await async_update_forecast(hass)
        except Exception:
            _LOGGER.exception("Failed to update the forecast")
            return
        await async_request_plan(hass, "forecast updated")

    # Every hour, after the recorder has compiled the hourly statistics, and once
    # Home Assistant has started so Forecast.Solar is loaded
    entry.async_on_unload(
        async_track_utc_time_change(hass, update_forecast, minute=5, second=0)
    )
    entry.async_on_unload(async_at_started(hass, update_forecast))

    async def update_plan(now: dt.datetime):
        await async_request_plan(hass, "new quarter")

    # Every quarter, just after it starts, with the current SOC
    entry.async_on_unload(
        async_track_utc_time_change(hass, update_plan, minute="/15", second=30)
    )
    _track_plan_triggers(hass, entry)
    return True


def _track_plan_triggers(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recalculate the plan on new prices, settings and SOC drift."""
    config = hass.data[DOMAIN]["config"]

    async def prices_changed(event: Event[EventStateChangedData]):
        old, new = event.data["old_state"], event.data["new_state"]
        if (
            new is not None
            and new.attributes.get("tomorrow_valid")
            and not (old is not None and old.attributes.get("tomorrow_valid"))
        ):
            await async_request_plan(hass, "tomorrow's prices")

    nordpool = config.get("nordpool_entity_id")
    if nordpool:
        entry.async_on_unload(
            async_track_state_change_event(hass, nordpool, prices_changed)
        )

    async def setting_changed(event: Event[EventStateChangedData]):
        old, new = event.data["old_state"], event.data["new_state"]
        if old is not None and new is not None and old.state != new.state:
            await async_request_plan(hass, f"{event.data['entity_id']} changed")

    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            [f"number.{DOMAIN}_{key}" for key in PLAN_SETTINGS],
            setting_changed,
        )
    )

    async def soc_changed(event: Event[EventStateChangedData]):
        new = event.data["new_state"]
        try:
            soc = float(new.state) if new is not None else None
        except ValueError:
            return
        now = dt_utils.now()
        expected = expected_soc(hass, now)
        if soc is None or expected is None or abs(soc - expected) < SOC_DRIFT:
            return
        plan = hass.data[DOMAIN].get("plan") or {}
        updated = plan.get("updated")
        if updated and now - dt.datetime.fromisoformat(updated) < SOC_DRIFT_INTERVAL:
            return
        await async_request_plan(hass, f"SOC {soc} %, planned {expected:.0f} %")

    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            config.get("battery_soc_sensor", DEFAULT_BATTERY_SOC_SENSOR),
            soc_changed,
        )
    )

    async def slot_changed(event: Event[EventStateChangedData]):
        write_plan_sensors(hass)

    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            [f"select.{DOMAIN}_slot_1_state", f"switch.{DOMAIN}_slot_1_active"],
            slot_changed,
        )
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Modbus config entry."""
    _LOGGER.debug("init async_unload_entry")
    # Unload platforms associated with this integration
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
