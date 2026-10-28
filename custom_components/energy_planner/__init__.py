from collections.abc import Callable
import logging
import datetime as dt

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    Event,
    EventStateChangedData,
)
from homeassistant.const import Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_utils

from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
)
from homeassistant.helpers.start import async_at_started

from .const import (
    DOMAIN,
    DATE_TIME_ENTITIES,
    FUSE_GUARD_SENSOR,
    NUMBER_ENTITIES,
    SWITCH_ENTITIES,
    SELECT_ENTITIES,
    TIME_ENTITIES,
)
from .migrate import migrate_config, migrate_values, remove_entities
from .planner import manual, settings
from .planner.timeline import quarter_start
from .planner.ev import is_plugged as ev_is_plugged
from .planner.ev import is_unplugged as ev_is_unplugged
from .planner.fuse_guard import config_entities, update_guard
from .planner import (
    async_car_unplugged,
    async_request_plan,
    async_set_ev_soc,
    async_update_forecast,
    resolve_actions,
    write_plan_sensors,
)
from .store import async_save_to_store, async_load_from_store

_LOGGER = logging.getLogger(__name__)

# Recalculate the plan when the actual SOC is this many % from the plan
SOC_DRIFT = 5.0
# ... but not more often than this
SOC_DRIFT_INTERVAL = dt.timedelta(minutes=5)
# The fuse guard follows the meter's phase readings, at most this often
FUSE_GUARD_INTERVAL = dt.timedelta(seconds=5)
PLATFORMS = [
    Platform.BUTTON,
    Platform.DATETIME,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


async def async_setup_data_structure(hass: HomeAssistant):
    """Set up the data structure."""

    async def save():
        for data_store in ["values", "config"]:
            await async_save_to_store(hass, data_store, hass.data[DOMAIN][data_store])
        await async_save_to_store(
            hass,
            "manual_slots",
            [slot.to_stored() for slot in hass.data[DOMAIN]["manual_slots"]],
        )

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
    migrated = migrate_config(hass.data[DOMAIN]["config"])
    migrated = migrate_values(hass.data[DOMAIN]["values"]) or migrated
    stored_slots = await async_load_from_store(hass, "manual_slots") or []
    parse_stored_data(hass)
    hass.data[DOMAIN]["manual_slots"] = _load_manual_slots(stored_slots)
    if migrated or len(hass.data[DOMAIN]["manual_slots"]) != len(stored_slots):
        await save()


def _load_manual_slots(stored: list) -> list[manual.ManualSlot]:
    """Return the stored manual slots that have not ended, in whole quarters.

    A time without an offset is in HA's time zone. Invalid ones are dropped.
    """
    slots = []
    for item in stored:
        try:
            slots.append(
                manual.from_stored(
                    {
                        **item,
                        "start": dt_utils.as_utc(_parse(item["start"])),
                        "end": dt_utils.as_utc(_parse(item["end"])),
                    }
                )
            )
        except KeyError, TypeError, ValueError:
            _LOGGER.warning("Dropping an invalid manual slot: %s", item)
    return manual.current(slots, dt_utils.now())


def _parse(value) -> dt.datetime:
    """Return a stored time as a datetime (ISO strings in the store)."""
    parsed = dt_utils.parse_datetime(value) if isinstance(value, str) else value
    if not isinstance(parsed, dt.datetime):
        raise ValueError(f"Not a time: {value!r}")
    return parsed


def parse_stored_data(hass: HomeAssistant):
    """Convert dates and times, which are stored as strings, back to objects."""
    config = hass.data[DOMAIN]["config"]
    if type(config.get("ev_ready_time")) is str:
        config["ev_ready_time"] = dt.time.fromisoformat(config["ev_ready_time"])
    if type(config.get("ev_departure")) is str:
        config["ev_departure"] = dt_utils.parse_datetime(config["ev_departure"])


async def _manual_slots_changed(hass: HomeAssistant, reason: str) -> None:
    """Store the manual slots, show them at once and plan around them."""
    await hass.data[DOMAIN]["save"]()
    write_plan_sensors(hass)
    await async_request_plan(hass, reason)


async def add_manual_slot(hass: HomeAssistant, data: dict) -> manual.ManualSlot:
    """Add a manual slot over whole quarters (the times are rounded outwards).

    A time without an offset is in HA's time zone. Raises ValueError for invalid data.
    """
    start = dt_utils.as_utc(dt.datetime.fromisoformat(str(data["start"])))
    end = dt_utils.as_utc(dt.datetime.fromisoformat(str(data["end"])))
    soc = data.get("soc")
    slot = manual.make(start, end, data.get("state", ""), soc)
    if (slot.start, slot.end) != (start, end):
        _LOGGER.info(
            "Manual slot rounded to whole quarters: %s - %s", slot.start, slot.end
        )
    if slot.end <= dt_utils.now():
        raise ValueError("The slot has already ended")
    hass.data[DOMAIN]["manual_slots"].append(slot)
    await _manual_slots_changed(hass, "manual slot added")
    return slot


async def remove_manual_slot(hass: HomeAssistant, start: str) -> None:
    """Remove the manual slots starting in the quarter of `start`.

    Raises ValueError if none does.
    """
    first = quarter_start(dt_utils.as_utc(dt.datetime.fromisoformat(str(start))))
    slots = hass.data[DOMAIN]["manual_slots"]
    kept = [slot for slot in slots if slot.start != first]
    if len(kept) == len(slots):
        raise ValueError(f"No manual slot starts at {start}")
    hass.data[DOMAIN]["manual_slots"] = kept
    await _manual_slots_changed(hass, "manual slot removed")


async def clear_manual_slots(hass: HomeAssistant) -> None:
    """Drop all manual slots."""
    hass.data[DOMAIN]["manual_slots"] = []
    await _manual_slots_changed(hass, "manual slots cleared")


async def async_setup(hass: HomeAssistant, config):
    """Set up the Energy Planner component."""
    if DOMAIN not in hass.data:
        await async_setup_data_structure(hass)

    async def add_slot_service(call: ServiceCall) -> None:
        """Service to add a manual slot."""
        try:
            await add_manual_slot(hass, dict(call.data))
        except (KeyError, TypeError, ValueError) as e:
            _LOGGER.error("Error adding slot: %s", e)
            raise ServiceValidationError(f"Invalid slot: {e}") from e

    async def remove_manual_slot_service(call: ServiceCall) -> None:
        """Service to remove a manual slot."""
        try:
            await remove_manual_slot(hass, call.data["start"])
        except (KeyError, TypeError, ValueError) as e:
            raise ServiceValidationError(str(e)) from e

    async def clear_manual_slots_service(call: ServiceCall) -> None:
        """Service to clear the manual slots."""
        await clear_manual_slots(hass)

    async def update_forecast_service(call: ServiceCall) -> None:
        """Service to recalculate the forecast."""
        await async_update_forecast(hass)

    async def update_battery_plan_service(call: ServiceCall) -> None:
        """Service to recalculate the battery plan."""
        await async_request_plan(hass, "service")

    async def set_ev_soc_service(call: ServiceCall) -> None:
        """Service to enter the car's SOC for this session."""
        try:
            await async_set_ev_soc(
                hass, float(call.data["soc"]), call.data.get("charge_limit")
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ServiceValidationError(str(e)) from e

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, "add_slot", add_slot_service)
    hass.services.async_register(DOMAIN, "update_forecast", update_forecast_service)
    hass.services.async_register(
        DOMAIN, "update_battery_plan", update_battery_plan_service
    )
    hass.services.async_register(
        DOMAIN, "remove_manual_slot", remove_manual_slot_service
    )
    hass.services.async_register(
        DOMAIN, "clear_manual_slots", clear_manual_slots_service
    )
    hass.services.async_register(DOMAIN, "set_ev_soc", set_ev_soc_service)

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
    remove_entities(hass)

    async def options_updated(hass: HomeAssistant, entry: ConfigEntry):
        # Read by get_tariff, no reload needed
        hass.data[DOMAIN]["options"] = dict(entry.options)
        await async_request_plan(hass, "tariff changed")

    entry.async_on_unload(entry.add_update_listener(options_updated))
    # Resolved before the sensors are added, so they start with them (not with the
    # ones from before a reload, or unavailable)
    hass.data[DOMAIN].pop("actions_failing", None)
    resolve_actions(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    update_fuse_guard = _track_fuse_guard(hass, entry)

    async def every_minute(now: dt.datetime):
        # The actions follow the plan's current quarter and the manual slots; at
        # second 0, so a new quarter's run starts on time. Then the fuse guard, from
        # the car action just published
        write_plan_sensors(hass, now)
        update_fuse_guard()

    entry.async_on_unload(async_track_utc_time_change(hass, every_minute, second=0))

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

    async def forecast_setting_changed(event: Event[EventStateChangedData]):
        old, new = event.data["old_state"], event.data["new_state"]
        if old is not None and new is not None and old.state != new.state:
            await update_forecast()

    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            [
                f"select.{DOMAIN}_forecast_load_model",
                f"select.{DOMAIN}_forecast_pv_calibration",
            ],
            forecast_setting_changed,
        )
    )

    async def update_plan(now: dt.datetime):
        await async_request_plan(hass, "new quarter")

    # Every quarter, just after it starts, with the current SOC
    entry.async_on_unload(
        async_track_utc_time_change(hass, update_plan, minute="/15", second=30)
    )
    _track_plan_triggers(hass, entry)
    return True


def _track_fuse_guard(hass: HomeAssistant, entry: ConfigEntry) -> Callable[[], None]:
    """Update the fuse guard on new meter readings; return the update for the minute.

    Readings update it at most every `FUSE_GUARD_INTERVAL`; the minute's update is
    never held back, so the car's new run is seen when its quarter starts.
    """
    last: list[dt.datetime] = []

    def update() -> None:
        last[:] = [dt_utils.utcnow()]
        try:
            update_guard(hass)
        except Exception:
            _LOGGER.exception("Failed to update the fuse guard")
            return
        sensor = hass.data[DOMAIN].get(FUSE_GUARD_SENSOR)
        if sensor is not None and sensor.hass is not None:
            sensor.async_write_ha_state()

    async def reading_changed(_event: Event[EventStateChangedData]):
        if last and dt_utils.utcnow() - last[0] < FUSE_GUARD_INTERVAL:
            return
        update()

    entities = config_entities(hass.data[DOMAIN]["config"])
    if entities:
        entry.async_on_unload(
            async_track_state_change_event(hass, entities, reading_changed)
        )
    return update


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
            settings.replan_entity_ids(),
            setting_changed,
        )
    )

    async def car_plugged(event: Event[EventStateChangedData]):
        old, new = event.data["old_state"], event.data["new_state"]
        old_state = old.state if old is not None else None
        new_state = new.state if new is not None else None
        if old_state == new_state:
            return
        # The car action follows the charger now, not after the replan
        write_plan_sensors(hass)
        if ev_is_unplugged(new_state):
            await async_car_unplugged(hass)
        was = ev_is_plugged(old_state)
        now = ev_is_plugged(new_state)
        if was != now:
            await async_request_plan(hass, "car plugged in" if now else "car unplugged")

    car = settings.car(config)
    if car is not None:
        entry.async_on_unload(
            async_track_state_change_event(hass, car.status_sensor, car_plugged)
        )

    drift_requested: list[dt.datetime] = []

    async def soc_changed(event: Event[EventStateChangedData]):
        soc = settings.reading(hass, event.data["entity_id"])
        now = dt_utils.now()
        plan = hass.data[DOMAIN].get("plan")
        expected = plan.soc_at(now) if plan is not None else None
        if soc is None or expected is None or abs(soc - expected) < SOC_DRIFT:
            return
        # From the plan or the last drift request: while plan runs fail the plan
        # stays old, and every SOC change would start another run
        last = max([plan.updated, *drift_requested])
        if now - last < SOC_DRIFT_INTERVAL:
            return
        drift_requested[:] = [now]
        await async_request_plan(hass, f"SOC {soc} %, planned {expected:.0f} %")

    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            settings.get(config, "battery_soc_sensor"),
            soc_changed,
        )
    )

    async def enabled_changed(event: Event[EventStateChangedData]):
        write_plan_sensors(hass)

    entry.async_on_unload(
        async_track_state_change_event(
            hass, f"switch.{DOMAIN}_enabled", enabled_changed
        )
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Modbus config entry."""
    _LOGGER.debug("init async_unload_entry")
    # Unload platforms associated with this integration
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
