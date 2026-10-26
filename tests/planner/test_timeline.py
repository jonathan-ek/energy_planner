"""Tests for the quarter timeline: the DST suite for every series kept on it."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.planner import timeline
from custom_components.energy_planner.planner.timeline import Timeline

TZ = ZoneInfo("Europe/Stockholm")
SPRING = dt.date(2026, 3, 29)  # 02:00 CET -> 03:00 CEST
AUTUMN = dt.date(2026, 10, 25)  # 03:00 CEST -> 02:00 CET


def day(date: dt.date) -> Timeline:
    """Return the quarters of a local day."""
    start = timeline.at_local(date, dt.time(0), TZ)
    end = timeline.at_local(date + dt.timedelta(days=1), dt.time(0), TZ)
    return Timeline.between(start, end, TZ)


@pytest.mark.parametrize(
    ("date", "quarters"), [(SPRING, 92), (AUTUMN, 100), (dt.date(2026, 1, 13), 96)]
)
def test_days_have_their_quarters(date, quarters):
    """Test that a day has 92 or 100 quarters when DST changes."""
    quarters_of_day = day(date)
    assert len(quarters_of_day) == quarters
    assert {quarters_of_day.local_date(i) for i in range(quarters)} == {date}


def test_between_starts_with_the_quarter_of_its_start():
    """Test that a start inside a quarter includes that quarter."""
    start = dt.datetime(2026, 10, 4, 10, 7, 30, tzinfo=TZ)
    quarters = Timeline.between(start, start.replace(hour=11, minute=0, second=0), TZ)
    assert [q.strftime("%H:%M") for q in quarters.locals] == [
        "10:00",
        "10:15",
        "10:30",
        "10:45",
    ]


def test_the_repeated_hour_is_two_hours():
    """Test lookup, local hours and display in the hour repeated when DST ends."""
    quarters = day(AUTUMN)
    first = dt.datetime(2026, 10, 25, 2, 20, tzinfo=TZ)
    second = first.replace(fold=1)
    assert (quarters.index_at(first), quarters.index_at(second)) == (9, 13)
    assert quarters.local_hour(9) == quarters.local_hour(13) == 2
    assert quarters.isoformat()[9] == "2026-10-25T02:15:00+02:00"
    assert quarters.isoformat()[13] == "2026-10-25T02:15:00+01:00"
    # The end of the first copy is the second copy's start, 15 minutes later
    assert quarters.local_end(11).isoformat() == "2026-10-25T02:00:00+01:00"
    assert quarters.first_from(second) == 14


def test_the_skipped_hour():
    """Test that 01:45 and 03:00 follow each other when DST starts."""
    quarters = day(SPRING)
    assert quarters.local(7).isoformat() == "2026-03-29T01:45:00+01:00"
    assert quarters.local(8).isoformat() == "2026-03-29T03:00:00+02:00"


def test_at_local_on_the_change_days():
    """Test the repeated hour's first copy and the skipped hour pushed forward."""
    assert timeline.at_local(AUTUMN, dt.time(2, 30), TZ) == dt.datetime(
        2026, 10, 25, 0, 30, tzinfo=dt.UTC
    )
    assert timeline.at_local(AUTUMN, dt.time(2, 30, fold=1), TZ) == dt.datetime(
        2026, 10, 25, 0, 30, tzinfo=dt.UTC
    )
    # 02:30 does not exist on the spring morning: 03:30 summer time
    assert timeline.at_local(SPRING, dt.time(2, 30), TZ) == dt.datetime(
        2026, 3, 29, 1, 30, tzinfo=dt.UTC
    )
    assert timeline.at_local(SPRING, dt.time(6, 30), TZ) == dt.datetime(
        2026, 3, 29, 4, 30, tzinfo=dt.UTC
    )


def test_quarter_and_hour_keys_are_utc():
    """Test that the two 02:xx copies get their own keys."""
    first = dt.datetime(2026, 10, 25, 2, 40, tzinfo=TZ)
    second = first.replace(fold=1)
    assert timeline.quarter_start(first) != timeline.quarter_start(second)
    assert timeline.hour_start(first) == dt.datetime(2026, 10, 25, 0, tzinfo=dt.UTC)
    assert timeline.hour_start(second) == dt.datetime(2026, 10, 25, 1, tzinfo=dt.UTC)


def test_of_local_starts_and_slices():
    """Test a timeline from local starts in another zone, and a part of it."""
    quarters = day(AUTUMN)
    again = Timeline.of(quarters.locals)
    assert again == quarters
    part = quarters[8:14]
    assert part.start == quarters.starts[8]
    assert part.end == quarters.quarter_end(13)


@pytest.mark.parametrize(
    "starts",
    [
        (dt.datetime(2026, 1, 1, 0, 5, tzinfo=dt.UTC),),
        (dt.datetime(2026, 1, 1, 1, tzinfo=TZ),),
        (
            dt.datetime(2026, 1, 1, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 1, 1, 0, 30, tzinfo=dt.UTC),
        ),
    ],
)
def test_only_consecutive_utc_quarters(starts):
    """Test that a timeline refuses starts that are not UTC quarters in a row."""
    with pytest.raises(ValueError):
        Timeline(starts, TZ)
