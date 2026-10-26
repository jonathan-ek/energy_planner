"""Quarters and moments in time, kept as UTC instants.

Pure, no Home Assistant imports. Aware datetimes in one zone compare and hash by wall
clock and ignore `fold`, so in the hour repeated when daylight saving time ends the
two 02:xx quarters look equal, and adding a timedelta keeps the wall clock. So
moments are compared, sorted, added to and used as keys in UTC; local time is only
for calendar logic (the hour of day, the date) and display.

- `Timeline`: consecutive quarters (UTC inside), with lookup by moment and the local
  hour, date and ISO string of each quarter.
- `quarter_start`: the UTC start of the quarter containing a moment, the key of the
  price dicts.
- `hour_start`: the same for the hour, the key of hourly sums (power charges).
- `at_local`: a local date and wall-clock time as an instant.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Iterable
import dataclasses
import datetime as dt
import functools

QUARTER = dt.timedelta(minutes=15)


def quarter_start(moment: dt.datetime) -> dt.datetime:
    """Return the start of the quarter containing `moment`, in UTC."""
    utc = moment.astimezone(dt.UTC)
    return utc.replace(minute=utc.minute - utc.minute % 15, second=0, microsecond=0)


def hour_start(moment: dt.datetime) -> dt.datetime:
    """Return the start of the hour containing `moment`, in UTC.

    The local hour as well where the offset is whole hours (as in Europe).
    """
    return moment.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)


def at_local(day: dt.date, time: dt.time, tz: dt.tzinfo) -> dt.datetime:
    """Return the instant (UTC) of a wall-clock time on a local date.

    A time in the hour repeated when DST ends is its first occurrence; one in the hour
    skipped when DST starts is taken with the offset before the change, so 02:30 on
    the spring morning is 03:30 summer time.
    """
    naive = time.replace(tzinfo=None, fold=0)
    return dt.datetime.combine(day, naive, tz).astimezone(dt.UTC)


@dataclasses.dataclass(frozen=True)
class Timeline:
    """Consecutive quarters, from `starts` (UTC); local time in `tz`."""

    starts: tuple[dt.datetime, ...]
    tz: dt.tzinfo

    def __post_init__(self) -> None:
        """Check that the starts are UTC quarters, one after another."""
        for start in self.starts:
            if start.utcoffset() != dt.timedelta(0) or start != quarter_start(start):
                raise ValueError(f"Not a UTC quarter start: {start}")
        for before, after in zip(self.starts, self.starts[1:], strict=False):
            if after - before != QUARTER:
                raise ValueError(
                    f"Quarters do not follow each other: {before}, {after}"
                )

    @classmethod
    def of(
        cls, starts: Iterable[dt.datetime], tz: dt.tzinfo | None = None
    ) -> "Timeline":
        """Return the timeline of aware quarter starts in any zone.

        `tz` defaults to the first start's zone (UTC without starts).
        """
        starts = tuple(starts)
        if tz is None:
            tz = starts[0].tzinfo if starts and starts[0].tzinfo else dt.UTC
        return cls(tuple(s.astimezone(dt.UTC) for s in starts), tz)

    @classmethod
    def between(cls, start: dt.datetime, end: dt.datetime, tz: dt.tzinfo) -> "Timeline":
        """Return the quarters from the one containing `start` until `end` (excl.).

        A day has 92 or 100 quarters when DST changes, as it should.
        """
        first, stop = quarter_start(start), end.astimezone(dt.UTC)
        count = max(0, -(-(stop - first) // QUARTER))
        return cls(tuple(first + QUARTER * i for i in range(count)), tz)

    def __len__(self) -> int:
        """Return the number of quarters."""
        return len(self.starts)

    def __getitem__(self, index: slice) -> "Timeline":
        """Return a part of the timeline (only slices: an item is `starts[i]`)."""
        if not isinstance(index, slice) or index.step not in (None, 1):
            raise TypeError("Timeline only takes plain slices")
        return Timeline(self.starts[index], self.tz)

    @property
    def start(self) -> dt.datetime:
        """Return the start of the first quarter (UTC)."""
        return self.starts[0]

    @property
    def end(self) -> dt.datetime:
        """Return the end of the last quarter (UTC)."""
        return self.starts[-1] + QUARTER

    @functools.cached_property
    def _timestamps(self) -> tuple[float, ...]:
        return tuple(s.timestamp() for s in self.starts)

    def index_at(self, moment: dt.datetime) -> int | None:
        """Return the quarter containing `moment`, None outside the timeline."""
        time = moment.timestamp()
        index = bisect_right(self._timestamps, time) - 1
        if index < 0 or time >= self._timestamps[index] + QUARTER.total_seconds():
            return None
        return index

    def quarter_end(self, index: int) -> dt.datetime:
        """Return the end of a quarter (UTC)."""
        return self.starts[index] + QUARTER

    def local(self, index: int) -> dt.datetime:
        """Return a quarter's start in local time (for display and calendar logic)."""
        return self.starts[index].astimezone(self.tz)

    def local_end(self, index: int) -> dt.datetime:
        """Return a quarter's end in local time."""
        return self.quarter_end(index).astimezone(self.tz)

    @functools.cached_property
    def locals(self) -> tuple[dt.datetime, ...]:
        """Return all quarter starts in local time."""
        return tuple(s.astimezone(self.tz) for s in self.starts)

    def local_hour(self, index: int) -> int:
        """Return the local hour of day of a quarter."""
        return self.local(index).hour

    def local_date(self, index: int) -> dt.date:
        """Return the local date of a quarter."""
        return self.local(index).date()

    def isoformat(self) -> list[str]:
        """Return the quarter starts as local ISO strings (with their offsets)."""
        return [s.isoformat() for s in self.locals]

    def first_from(self, moment: dt.datetime) -> int:
        """Return the first quarter starting at or after `moment` (len if none)."""
        return bisect_left(self._timestamps, moment.timestamp())
