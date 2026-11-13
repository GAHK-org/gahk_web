"""Time helpers: "today" is always the Copenhagen calendar day.

The site's day decides which events are upcoming and which visit-count row a view lands in. Python's
zoneinfo needs the system tz database; Debian ships it, but if it is ever missing we fall back to
the EU rule (CET, CEST from the last Sunday of March 01:00 UTC to the last Sunday of October 01:00
UTC) instead of crashing every request.
"""

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo

    COPENHAGEN: tzinfo = ZoneInfo("Europe/Copenhagen")
except Exception:  # no tz database on the host  # pragma: no cover
    COPENHAGEN = timezone(timedelta(hours=1), "CET")
    _HAS_ZONEINFO = False
else:
    _HAS_ZONEINFO = True


def _last_sunday(year: int, month: int) -> date:
    day = date(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _eu_offset(moment: datetime) -> timedelta:
    start = datetime.combine(_last_sunday(moment.year, 3), datetime.min.time(), UTC) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(moment.year, 10), datetime.min.time(), UTC) + timedelta(hours=1)
    return timedelta(hours=2) if start <= moment < end else timedelta(hours=1)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_copenhagen(moment: datetime) -> datetime:
    """Copenhagen wall-clock time for an aware datetime."""
    if _HAS_ZONEINFO:
        return moment.astimezone(COPENHAGEN)
    utc = moment.astimezone(UTC)  # pragma: no cover
    offset = _eu_offset(utc)  # pragma: no cover
    return utc.astimezone(timezone(offset))  # pragma: no cover


def copenhagen_today(moment: datetime | None = None) -> date:
    return to_copenhagen(moment or now_utc()).date()
