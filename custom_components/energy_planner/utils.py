import datetime as dt
from zoneinfo import ZoneInfo


def tz_diff(tz1, tz2):
    """Return the difference in hours between tz1 and tz2 today."""
    date = dt.datetime.now()
    utc_offset_1 = date.astimezone(ZoneInfo(tz1)).utcoffset()
    utc_offset_2 = date.astimezone(ZoneInfo(tz2)).utcoffset()
    return (utc_offset_2 - utc_offset_1).total_seconds() / 3600
