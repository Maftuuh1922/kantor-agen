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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from skeleton.core.clock import Clock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus

from skeleton.office.onboarding import TemplateRegistry

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


BootstrapCallback = Callable[[str, str], OfficeContext]


@dataclass
class OfficeRuntime:
    """An office that may not exist yet (first run) and the means to create it.

    ``ctx`` is ``None`` while the database has no business. ``create_app`` then
    offers ``templates`` to the owner and, on ``POST /api/onboarding``, calls
    ``bootstrap_callback(name, type)``, which creates the business and returns the
    ready ``OfficeContext``. ``create_app`` stores that context in ``ctx``.
    A runtime whose ``ctx`` is already set (a restart on an existing database)
    starts straight into the office.

    The heartbeat of a runtime waits for the owner's policy (Req 1.3): it starts
    when the policy is saved, or at once when the policy already exists.
    """

    templates: TemplateRegistry
    bootstrap_callback: BootstrapCallback | None = None
    ctx: OfficeContext | None = None
