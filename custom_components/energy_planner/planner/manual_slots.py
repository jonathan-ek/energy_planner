import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from .utils import parse_datetime
from ..const import DOMAIN, SLOT_COUNT

_LOGGER = logging.getLogger(__name__)


async def shift_slots_forward(hass: HomeAssistant, start_index: int, steps: int = 1):
    """Shift slots forward."""
    _LOGGER.info("Shifting slots")
    for i in range(SLOT_COUNT - steps, start_index - 1, -1):
        # shift all slots one step forward
        hass.data[DOMAIN]["values"][f"slot_{i + steps}_date_time_start"] = (
            parse_datetime(hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"])
        )
        hass.data[DOMAIN]["values"][f"slot_{i + steps}_state"] = hass.data[DOMAIN][
            "values"
        ][f"slot_{i}_state"]
        hass.data[DOMAIN]["values"][f"slot_{i + steps}_active"] = hass.data[DOMAIN][
            "values"
        ][f"slot_{i}_active"]
        hass.data[DOMAIN]["values"][f"slot_{i + steps}_soc"] = hass.data[DOMAIN][
            "values"
        ][f"slot_{i}_soc"]


async def shift_slots_back(hass: HomeAssistant, start_index: int, steps: int = 1):
    """Shift slots back."""
    _LOGGER.info("Shifting slots")
    for i in range(start_index, SLOT_COUNT + 1 - steps, 1):
        hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"] = parse_datetime(
            hass.data[DOMAIN]["values"][f"slot_{i + steps}_date_time_start"]
        )
        hass.data[DOMAIN]["values"][f"slot_{i}_state"] = hass.data[DOMAIN]["values"][
            f"slot_{i + steps}_state"
        ]
        hass.data[DOMAIN]["values"][f"slot_{i}_active"] = hass.data[DOMAIN]["values"][
            f"slot_{i + steps}_active"
        ]
        hass.data[DOMAIN]["values"][f"slot_{i}_soc"] = hass.data[DOMAIN]["values"][
            f"slot_{i + steps}_soc"
        ]


def slot_start(hass: HomeAssistant, index: int):
    """Return the start of a slot in local time, None if unused or out of range."""
    return parse_datetime(
        hass.data[DOMAIN]["values"].get(f"slot_{index}_date_time_start"),
        dt_utils.DEFAULT_TIME_ZONE,
    )


async def add_manual_slots(hass: HomeAssistant):
    """Add manual slots."""
    _LOGGER.info("Adding slot")
    for s in hass.data[DOMAIN]["manual_slots"]:
        start = parse_datetime(s["start"], dt_utils.DEFAULT_TIME_ZONE)
        end = parse_datetime(s["end"], dt_utils.DEFAULT_TIME_ZONE)
        if start >= end:
            continue
        if end <= dt_utils.now():
            continue
        state = s["state"]

        max_soc = hass.data[DOMAIN]["config"].get("battery_max_soc", 100)
        min_soc = hass.data[DOMAIN]["config"].get("battery_shutdown_soc", 20)
        if state in ["charge", "sell"]:
            raw_soc = s.get("soc")
            if raw_soc is None:
                if state in "charge":
                    soc = max_soc
                else:  # sell
                    # if no soc is given for sell, skip it
                    continue
            else:
                soc = int(raw_soc)
        elif state == "pause":
            soc = max_soc
        elif state == "sell-excess":
            soc = min_soc
        elif s.get("soc") is not None:
            soc = int(s.get("soc"))
        else:
            # discard-excess or discharge
            # Do not set soc, it will be set to 50% by default
            soc = 50
        soc = max(min_soc, min(max_soc, soc))

        used_slots = 0
        while slot_start(hass, used_slots + 1) is not None:
            used_slots += 1
        # Adding a slot can take up to two new slots
        if used_slots + 2 > SLOT_COUNT:
            _LOGGER.warning("No free slots left, skipping manual slot %s", s)
            continue

        start_index = 1
        while (
            slot_start(hass, start_index) is not None
            and slot_start(hass, start_index) < start
        ):
            start_index += 1
        end_index = start_index
        while (
            slot_start(hass, end_index) is not None
            and slot_start(hass, end_index) < end
        ):
            end_index += 1
        end_is_end = slot_start(hass, end_index) == end
        if start_index == end_index and not end_is_end:
            await shift_slots_forward(hass, start_index, 2)
            hass.data[DOMAIN]["values"][f"slot_{start_index}_date_time_start"] = start
            hass.data[DOMAIN]["values"][f"slot_{start_index}_state"] = state
            hass.data[DOMAIN]["values"][f"slot_{start_index}_active"] = True
            hass.data[DOMAIN]["values"][f"slot_{start_index}_soc"] = soc

            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_date_time_start"] = end
            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_state"] = hass.data[
                DOMAIN
            ]["values"][f"slot_{start_index - 1}_state"]
            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_active"] = True
            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_soc"] = hass.data[
                DOMAIN
            ]["values"][f"slot_{start_index - 1}_soc"]
        else:
            moves = -2 + (end_index - start_index) + (1 if end_is_end else 0)
            if moves < 0:
                await shift_slots_forward(hass, start_index, -moves)
            elif moves > 0:
                await shift_slots_back(hass, start_index, moves)
            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_date_time_start"] = end
            hass.data[DOMAIN]["values"][f"slot_{start_index}_date_time_start"] = start
            hass.data[DOMAIN]["values"][f"slot_{start_index}_state"] = state
            hass.data[DOMAIN]["values"][f"slot_{start_index}_active"] = True
            hass.data[DOMAIN]["values"][f"slot_{start_index}_soc"] = soc
