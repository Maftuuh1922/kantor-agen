"""Everything the office API needs, bundled in one object.

An application builds an ``OfficeContext`` and passes it to
``skeleton.office.api.create_app``. The skeleton never imports an application.

Thread note: the API runs every handler on the event loop thread, so the one
``conn`` is never used by two threads at once. Open it with
``db.connect(path, check_same_thread=False)`` because it is created on a
different thread than the one that serves requests.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from skeleton.core.clock import Clock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus

if TYPE_CHECKING:  # avoid importing the runtime and tracker just for types
    from skeleton.office.visual import VisualTracker
    from skeleton.runtime.heartbeat import Heartbeat
    from skeleton.runtime.manager import Manager


@dataclass
class OfficeContext:
    conn: sqlite3.Connection
    business_id: int
    clock: Clock
    bus: EventBus
    settings: Settings = field(repr=False)  # holds the API key: keep it out of repr
    heartbeat: Heartbeat | None
    tracker: VisualTracker
    manager: Manager | None = None
    # True: the app lifespan starts and stops the heartbeat and the tracker.
    # Tests set this to False and drive them by hand.
    autostart: bool = True
