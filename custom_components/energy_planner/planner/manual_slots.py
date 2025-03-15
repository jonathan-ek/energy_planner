import logging
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from .utils import parse_datetime
from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def shift_slots_forward(hass: HomeAssistant, start_index: int, steps: int = 1):
    """Shift slots forward."""
    _LOGGER.info("Shifting slots")
    for i in range(49 - steps, start_index - 1, -1):
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


async def shift_slots_back(hass: HomeAssistant, start_index: int, steps: int = 1):
    """Shift slots back."""
    _LOGGER.info("Shifting slots")
    for i in range(start_index, 50 - steps, 1):
        hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"] = parse_datetime(
            hass.data[DOMAIN]["values"][f"slot_{i + steps}_date_time_start"]
        )
        hass.data[DOMAIN]["values"][f"slot_{i}_state"] = hass.data[DOMAIN]["values"][
            f"slot_{i + steps}_state"
        ]
        hass.data[DOMAIN]["values"][f"slot_{i}_active"] = hass.data[DOMAIN]["values"][
            f"slot_{i + steps}_active"
        ]


def localize_datetime(val):
    """Localize datetime."""
    return parse_datetime(val, ZoneInfo("Europe/Stockholm"))


async def add_manual_slots(hass: HomeAssistant):
    """Add manual slots."""
    _LOGGER.info("Adding slot")
    for s in hass.data[DOMAIN]["manual_slots"]:
        start = parse_datetime(s["start"], ZoneInfo("Europe/Stockholm"))
        end = parse_datetime(s["end"], ZoneInfo("Europe/Stockholm"))
        state = s["state"]

        start_index = 1
        while True:
            if hass.data[DOMAIN]["values"][
                f"slot_{start_index}_date_time_start"
            ] is None or (
                parse_datetime(
                    hass.data[DOMAIN]["values"][f"slot_{start_index}_date_time_start"],
                    ZoneInfo("Europe/Stockholm"),
                )
                >= start
            ):
                break
            start_index += 1
        end_index = start_index
        while True:
            if hass.data[DOMAIN]["values"][
                f"slot_{end_index}_date_time_start"
            ] is None or (
                parse_datetime(
                    hass.data[DOMAIN]["values"][f"slot_{end_index}_date_time_start"],
                    ZoneInfo("Europe/Stockholm"),
                )
                >= end
            ):
                break
            end_index += 1
        end_is_end = (
            parse_datetime(
                hass.data[DOMAIN]["values"][f"slot_{end_index}_date_time_start"],
                ZoneInfo("Europe/Stockholm"),
            )
            == end
        )
        if start_index == end_index and not end_is_end:
            await shift_slots_forward(hass, start_index, 2)
            hass.data[DOMAIN]["values"][f"slot_{start_index}_date_time_start"] = start
            hass.data[DOMAIN]["values"][f"slot_{start_index}_state"] = state
            hass.data[DOMAIN]["values"][f"slot_{start_index}_active"] = True

            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_date_time_start"] = end
            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_state"] = hass.data[
                DOMAIN
            ]["values"][f"slot_{start_index - 1}_state"]
            hass.data[DOMAIN]["values"][f"slot_{start_index + 1}_active"] = True
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
