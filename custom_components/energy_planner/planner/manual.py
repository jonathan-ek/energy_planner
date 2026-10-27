"""Manual slots: the user's overrides of the battery plan, in whole quarters.

Pure, no Home Assistant imports. A manual slot is an action state (`charge`,
`discharge`, `sell`, `sell-excess`, `discard-excess`, `pause`) from `start` to `end`,
UTC instants on quarter boundaries (`make` rounds them outwards). `charge` and `sell`
have a target SOC: charge up to it and then hold, sell down to it and then self-use.

They are forced into the plan (`forced`: the mode and target per quarter), and the
battery action follows the one covering now even without a plan
(battery_action.resolve). Where slots overlap, the one added last wins. Ended slots
are dropped (`current`).
"""

from collections.abc import Iterable, Mapping, Sequence
import dataclasses
import datetime as dt
from typing import Any

from .timeline import QUARTER, quarter_start

# Action state -> battery plan mode
STATE_MODES = {
    "charge": "charge",
    "discharge": "self_use",
    "sell": "sell",
    "sell-excess": "sell_excess",
    "pause": "hold",
    "discard-excess": "discard_excess",
}
STATES = tuple(STATE_MODES)
# The states with a target SOC
TARGET_STATES = ("charge", "sell")


@dataclasses.dataclass(frozen=True)
class ManualSlot:
    """An action state from `start` to `end` (UTC, whole quarters)."""

    start: dt.datetime
    end: dt.datetime
    state: str
    soc: float | None = None  # target SOC % for `charge` and `sell`, None: default

    def covers(self, moment: dt.datetime) -> bool:
        """Return whether the slot runs at `moment`."""
        return self.start <= moment.astimezone(dt.UTC) < self.end

    def to_stored(self) -> dict[str, Any]:
        """Return the slot as the store keeps it."""
        return {
            "start": self.start,
            "end": self.end,
            "state": self.state,
            "soc": self.soc,
        }


@dataclasses.dataclass(frozen=True)
class SocLimits:
    """The battery's SOC settings (%) that targets are defaulted and limited to."""

    min_soc: float  # shutdown
    max_soc: float
    reserve_soc: float


def make(
    start: dt.datetime, end: dt.datetime, state: str, soc: float | None = None
) -> ManualSlot:
    """Return a manual slot over the whole quarters from `start` to `end`.

    The start is rounded down and the end up. Raises ValueError for an unknown state
    or an end not after the start.
    """
    if state not in STATE_MODES:
        raise ValueError(f"Unknown state {state!r}, expected one of {STATES}")
    first = quarter_start(start)
    last = quarter_start(end)
    if last < end.astimezone(dt.UTC):
        last += QUARTER
    if last <= first:
        raise ValueError("The end must be after the start")
    return ManualSlot(first, last, state, None if soc is None else float(soc))


def from_stored(stored: Mapping[str, Any]) -> ManualSlot:
    """Return a stored slot (times parsed to aware datetimes), in whole quarters."""
    return make(stored["start"], stored["end"], stored["state"], stored.get("soc"))


def current(slots: Iterable[ManualSlot], now: dt.datetime) -> list[ManualSlot]:
    """Return the slots that have not ended, in the order they were added."""
    return [slot for slot in slots if slot.end > now.astimezone(dt.UTC)]


def _covering_index(slots: Sequence[ManualSlot], moment: dt.datetime) -> int | None:
    """Return the index of the slot that runs at `moment` (the last added)."""
    return next(
        (i for i in range(len(slots) - 1, -1, -1) if slots[i].covers(moment)), None
    )


def covering(slots: Sequence[ManualSlot], moment: dt.datetime) -> ManualSlot | None:
    """Return the slot that runs at `moment` (the last added), None if none does."""
    index = _covering_index(slots, moment)
    return None if index is None else slots[index]


def running(slots: Sequence[ManualSlot], moment: dt.datetime) -> ManualSlot | None:
    """Return the slot that runs at `moment`, ending where it stops running.

    That is its end, or earlier where a slot added after it takes over.
    """
    # By position: a slot added again equals the earlier copy
    index = _covering_index(slots, moment)
    if index is None:
        return None
    slot = slots[index]
    now = moment.astimezone(dt.UTC)
    end = slot.end
    for later in slots[index + 1 :]:
        if now < later.start < end:
            end = later.start
    return dataclasses.replace(slot, end=end)


def target(slot: ManualSlot, limits: SocLimits) -> float | None:
    """Return the slot's target SOC %, None for a state without one.

    `charge` defaults to the max SOC and `sell` to the reserve; both are kept between
    the shutdown and the max SOC (a sell may go below the reserve).
    """
    if slot.state not in TARGET_STATES:
        return None
    default = limits.max_soc if slot.state == "charge" else limits.reserve_soc
    soc = default if slot.soc is None else slot.soc
    return min(max(soc, limits.min_soc), limits.max_soc)


def forced(
    slots: Sequence[ManualSlot], starts: Sequence[dt.datetime], limits: SocLimits
) -> tuple[list[str | None], list[float | None]]:
    """Return the mode and target SOC % each quarter must use (None: free)."""
    modes: list[str | None] = []
    targets: list[float | None] = []
    for start in starts:
        slot = covering(slots, start)
        modes.append(STATE_MODES[slot.state] if slot else None)
        targets.append(target(slot, limits) if slot else None)
    return modes, targets
