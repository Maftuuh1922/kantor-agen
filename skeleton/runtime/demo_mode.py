"""Demo hours: should the simulated feeds keep to business hours? (Req 3.2, 10.1)

The live apps run a simulator (walk-in customers, booking requests). If it only ran in
Jakarta business hours, the office would be idle when a judge or the owner opens the app at
night. So by default the simulators IGNORE business hours ("demo hours"). The owner can ask
for the real hours back with ``--real-hours`` or ``KANTOR_REAL_HOURS=1``.

This module only reads the flag. The feeds themselves are in ``apps/`` (parameter
``demo_hours``). It must not import anything from ``apps/``.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from skeleton.core.config import parse_dotenv

REAL_HOURS_ENV = "KANTOR_REAL_HOURS"
# Heartbeat intervals at or above this are "real time": simulated volume is not scaled down.
REAL_INTERVAL_SECONDS = 300.0

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def real_hours_from_env(env: Mapping[str, str] | None = None, dotenv_path: str | os.PathLike[str] | None = None) -> bool:
    """True when ``KANTOR_REAL_HOURS`` is on, in the environment or in ``.env`` (the environment wins).

    A value that is not 1/0, true/false, yes/no, on/off raises ``ValueError``, like the other settings.
    """
    values: dict[str, str] = {}
    path = Path(dotenv_path) if dotenv_path is not None else Path.cwd() / ".env"
    try:
        values.update(parse_dotenv(path.read_text(encoding="utf-8")))
    except OSError:
        pass
    values.update(os.environ if env is None else env)
    raw = values.get(REAL_HOURS_ENV, "").strip().lower()
    if not raw:
        return False
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    raise ValueError(f"{REAL_HOURS_ENV} must be one of 1/0, true/false, yes/no, on/off, got {raw!r}")


def demo_speed(interval_seconds: float | None, boost: float) -> float:
    """Factor (0 to 1] that scales simulated volume per tick for a short heartbeat interval.

    ``None`` or an interval of 300 s or more gives 1 (real time). A shorter interval gives
    ``boost * interval / 300``, at most 1: the simulated shop then runs ``boost`` times faster
    than real time, whatever the interval, instead of ``300 / interval`` times faster.
    """
    if interval_seconds is None or interval_seconds >= REAL_INTERVAL_SECONDS:
        return 1.0
    if interval_seconds <= 0:
        return 1.0
    return min(1.0, boost * interval_seconds / REAL_INTERVAL_SECONDS)
