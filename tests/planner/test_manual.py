"""Tests for manual slots, also in the hour repeated when DST ends (2026-10-25)."""

import datetime as dt
import importlib
from zoneinfo import ZoneInfo

import pytest

from .plans import battery_settings

manual = importlib.import_module("custom_components.energy_planner.planner.manual")
battery_action = importlib.import_module(
    "custom_components.energy_planner.planner.battery_action"
)

TZ = ZoneInfo("Europe/Stockholm")
# 02:30 local twice: CEST (00:30 UTC), then CET (01:30 UTC)
FIRST = dt.datetime(2026, 10, 25, 2, 30, tzinfo=TZ)
SECOND = FIRST.replace(fold=1)
LIMITS = manual.SocLimits(min_soc=15.0, max_soc=90.0, reserve_soc=20.0)


def utc(day, hour, minute=0):
    """Return a UTC time in October 2026."""
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=dt.UTC)


def test_rounded_out_to_whole_quarters():
    """Test that the start is rounded down and the end up, kept in UTC."""
    slot = manual.make(
        dt.datetime(2026, 10, 20, 17, 5, tzinfo=TZ),
        dt.datetime(2026, 10, 20, 18, 31, tzinfo=TZ),
        "charge",
        80,
    )
    assert (slot.start, slot.end) == (utc(20, 15), utc(20, 16, 45))
    assert slot.soc == 80.0


@pytest.mark.parametrize(
    ("state", "start", "end"),
    [
        ("auto", utc(20, 15), utc(20, 16)),
        ("off", utc(20, 15), utc(20, 16)),
        ("charge", utc(20, 16), utc(20, 15)),
        ("charge", utc(20, 15), utc(20, 15)),
    ],
)
def test_invalid_slots(state, start, end):
    """Test that unknown states and empty slots are refused."""
    with pytest.raises(ValueError):
        manual.make(start, end, state)


def test_the_two_copies_of_the_repeated_hour_are_apart():
    """Test that a slot in the second 02:xx does not cover the first."""
    slot = manual.make(SECOND, SECOND + dt.timedelta(minutes=30), "sell")
    assert (slot.start, slot.end) == (utc(25, 1, 30), utc(25, 2))
    assert not slot.covers(FIRST)
    assert slot.covers(SECOND)


def test_the_last_added_wins_and_ended_ones_go():
    """Test overlapping slots and dropping ended ones."""
    early = manual.make(utc(20, 15), utc(20, 17), "charge")
    later = manual.make(utc(20, 16), utc(20, 18), "sell")
    slots = [early, later]
    assert manual.covering(slots, utc(20, 15, 30)) == early
    assert manual.covering(slots, utc(20, 16, 30)) == later
    assert manual.covering(slots, utc(20, 18)) is None
    assert manual.current(slots, utc(20, 17)) == [later]


def test_running_ends_where_a_later_slot_takes_over():
    """Test that an earlier slot's run ends where one added after it starts."""
    early = manual.make(utc(20, 12), utc(20, 14), "charge")
    later = manual.make(utc(20, 13), utc(20, 13, 30), "sell")

    def end(slots, moment):
        slot = manual.running(slots, moment)
        return slot.end if slot else None

    assert end([early, later], utc(20, 12, 30)) == utc(20, 13)
    assert manual.running([early, later], utc(20, 13)) == later
    assert end([early, later], utc(20, 13, 45)) == utc(20, 14)
    assert manual.running([later, early], utc(20, 12, 30)) == early
    # The same slot added again runs from its new position, past the other one
    again = manual.make(utc(20, 12), utc(20, 14), "charge")
    assert end([early, later, again], utc(20, 12, 30)) == utc(20, 14)


def test_forced_modes_and_targets():
    """Test the mode and target per quarter, defaults and limits."""
    starts = [utc(20, 15) + dt.timedelta(minutes=15 * i) for i in range(6)]
    slots = [
        manual.make(starts[0], starts[2], "charge"),
        manual.make(starts[2], starts[3], "sell", 5),
        manual.make(starts[3], starts[4], "pause", 50),
    ]
    modes, targets = manual.forced(slots, starts, LIMITS)
    assert modes == ["charge", "charge", "sell", "hold", None, None]
    # Charge to the max SOC by default; sell not below the shutdown SOC; no target
    # for the other states
    assert targets == [90.0, 90.0, 15.0, None, None, None]


def test_stored_round_trip():
    """Test that a stored slot comes back the same."""
    slot = manual.make(utc(20, 15), utc(20, 16), "sell-excess")
    assert manual.from_stored(slot.to_stored()) == slot


def test_manual_until_is_published_in_local_time():
    """Test that a slot's end, kept in UTC, is published like the plan's times."""
    slot = manual.ManualSlot(utc(24, 22), utc(25, 1, 30), "charge", 90)
    battery = battery_settings()
    result = battery_action.resolve(slot, None, FIRST, battery, 50.0, 500.0)
    assert (result["source"], result["until"]) == ("manual", SECOND.isoformat())
