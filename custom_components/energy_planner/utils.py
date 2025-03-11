import datetime as dt
from zoneinfo import ZoneInfo

def tz_diff(tz1, tz2):
    """
    Returns the difference in hours between timezone1 and timezone2
    for a given date.
    """
    date = dt.datetime.now()
    utc_offset_1 = date.astimezone(ZoneInfo(tz1)).utcoffset()
    utc_offset_2 = date.astimezone(ZoneInfo(tz2)).utcoffset()
    return (utc_offset_2 - utc_offset_1).total_seconds() / 3600
