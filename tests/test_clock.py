from datetime import datetime, timedelta, timezone

import pytest

from skeleton.core.clock import Clock, FakeClock, SystemClock


def test_system_clock_is_aware_utc_and_iso_date():
    clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    assert len(clock.today()) == 10
    datetime.fromisoformat(clock.today())


def test_clocks_satisfy_protocol():
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(), Clock)


def test_fake_clock_set_and_advance():
    clock = FakeClock(datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc))
    clock.advance(90)
    assert clock.now() == datetime(2025, 3, 1, 12, 1, 30, tzinfo=timezone.utc)
    clock.set(datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert clock.now() == datetime(2025, 6, 1, tzinfo=timezone.utc)


def test_fake_clock_rejects_naive_and_negative():
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.set(datetime(2025, 1, 1))
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_today_uses_jakarta_day_rollover():
    # 16:59:59 UTC = 23:59:59 Jakarta (UTC+7): still the same local day.
    clock = FakeClock(datetime(2025, 1, 31, 16, 59, 59, tzinfo=timezone.utc))
    assert clock.today() == "2025-01-31"
    clock.advance(1)  # 17:00:00 UTC = 00:00:00 Jakarta next day
    assert clock.today() == "2025-02-01"
    assert clock.now().date().isoformat() == "2025-01-31"  # UTC date lags


def test_today_converts_non_utc_input():
    # 2025-01-01 00:30 in UTC-5 is 05:30 UTC, 12:30 Jakarta, same date.
    tz = timezone(timedelta(hours=-5))
    clock = FakeClock(datetime(2025, 1, 1, 0, 30, tzinfo=tz))
    assert clock.now().utcoffset() == timedelta(0)
    assert clock.today() == "2025-01-01"
