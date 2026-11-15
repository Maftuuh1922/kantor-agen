"""Injectable clock so tests control time.

`now()` is timezone-aware UTC. `today()` is the ISO date in Asia/Jakarta,
because the owner thinks in local days (daily spend limits, daily reports).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

JAKARTA = ZoneInfo("Asia/Jakarta")


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...

    def today(self) -> str:
        """Current local date in Asia/Jakarta, formatted as YYYY-MM-DD."""
        ...


def _jakarta_date(moment: datetime) -> str:
    return moment.astimezone(JAKARTA).date().isoformat()


class SystemClock:
    """Clock backed by the real system time."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> str:
        return _jakarta_date(self.now())


class FakeClock:
    """Settable, advanceable clock for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        if start is not None:
            self.set(start)

    def now(self) -> datetime:
        return self._now

    def today(self) -> str:
        return _jakarta_date(self._now)

    def set(self, dt: datetime) -> None:
        """Set the current time. `dt` must be timezone-aware; stored as UTC."""
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("FakeClock.set requires a timezone-aware datetime")
        self._now = dt.astimezone(timezone.utc)

    def advance(self, seconds: float) -> None:
        """Move time forward by `seconds` (must not be negative)."""
        if seconds < 0:
            raise ValueError("advance seconds must not be negative")
        self._now = self._now + timedelta(seconds=seconds)
