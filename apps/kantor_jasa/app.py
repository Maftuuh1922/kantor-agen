"""Kantor Jasa: wires the shared skeleton to the Jasa roles and starts the web app.

    .venv/bin/python apps/kantor-jasa/app.py --port 8001

then open http://127.0.0.1:8001/ . The first run asks for the business name, creates
the three AI staff (booking, billing, follow-up) and the demo data, and then asks for
the spending rules. Every later run on the same database file goes straight into the
office. See ``apps/kantor-jasa/README.md``.

* No API key, no network: the LLM is the offline fake (``build_llm_client`` returns
  ``FakeLLMClient`` in demo mode). A key in ``KANTOR_LLM_API_KEY`` (environment or
  ``.env``) switches on the real client. The key is read from the server environment
  only and never leaves it (Req 10.2).
* Booking requests and payments come from the simulated feeds
  (``SimulatedBookingFeed``, ``SimulatedPaymentFeed``), so the demo has customers.
* The server binds to 127.0.0.1 unless ``--host`` says otherwise.
* The heartbeat interval comes from ``KANTOR_HEARTBEAT_SECONDS`` (default 300), or
  ``--interval``. The heartbeat starts when the owner has saved the spending rules.
* Work pacing ("think time", ``docs/pacing.md``) is ON when you start the app from the command
  line, so the staff are visibly working: each task is held as "running" for a few seconds.
  ``--no-pacing`` (or ``KANTOR_PACING=0``) turns it off; ``--pace-min`` / ``--pace-max`` (or
  ``KANTOR_PACING_MIN`` / ``KANTOR_PACING_MAX``) set the range in seconds. The function
  ``create_jasa_runtime`` itself keeps pacing off unless its ``settings`` turn it on, so tests
  stay instant.

This app offers only the Jasa business type, so the first-run screen has one card.
``create_jasa_runtime`` is the function the tests and ``main`` share. It is the same
shape as ``create_warung_runtime``.

Depends only on ``skeleton.*``; the skeleton never imports this module.
"""
from __future__ import annotations

import argparse
import dataclasses
import sqlite3

from apps.kantor_jasa.schema import init_jasa_schema
from apps.kantor_jasa.template import JASA_KEY, JASA_TEMPLATE
from skeleton.core import db
from skeleton.core.clock import Clock, SystemClock
from skeleton.core.config import Settings, load_settings
from skeleton.core.events import EventBus
from skeleton.log.activity import install_append_only_triggers
from skeleton.office.api import create_app
from skeleton.office.context import OfficeContext, OfficeRuntime
from skeleton.office.onboarding import TemplateRegistry, bootstrap, ensure_label_table
from skeleton.office.visual import VisualTracker
from skeleton.runtime.heartbeat import Heartbeat
from skeleton.runtime.llm import build_llm_client
from skeleton.runtime.manager import Manager
from skeleton.runtime.pacing import settings_with_pacing

DEFAULT_HOST = "127.0.0.1"  # never 0.0.0.0 unless the owner asks for it
DEFAULT_PORT = 8000
DEFAULT_DB_PATH = "kantor-jasa.db"
MIN_INTERVAL_SECONDS = 1


def _existing_business(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT id, type FROM business ORDER BY id LIMIT 1").fetchone()


def create_jasa_runtime(
    db_path: str,
    settings: Settings | None = None,
    clock: Clock | None = None,
    interval: float | None = None,
    autostart: bool = True,
) -> OfficeRuntime:
    """Open (or create) the database and return the office runtime for ``create_app``.

    * ``settings``: default ``load_settings()`` (environment and ``.env``).
    * ``interval``: heartbeat seconds, overrides the settings (at least 1).
    * ``autostart=False``: the app does not start the heartbeat or the tracker; tests
      drive ``runtime.ctx.heartbeat.tick()`` by hand.

    The database has no business yet: ``runtime.ctx`` is ``None`` and the API runs the
    onboarding. The database already has a Jasa office: ``runtime.ctx`` is built at once,
    with the roles rebuilt from the agent rows. A database of another business type raises
    ``ValueError``.
    """
    settings = settings if settings is not None else load_settings()
    settings = dataclasses.replace(settings, db_path=db_path)
    if interval is not None:
        # Settings only checks "at least 1", so a fractional interval works (tests, demos).
        settings = dataclasses.replace(settings, heartbeat_seconds=interval)  # type: ignore[arg-type]
    clock = clock if clock is not None else SystemClock()

    conn = db.connect(db_path, check_same_thread=False)
    try:
        db.init_schema(conn)
        install_append_only_triggers(conn)
        ensure_label_table(conn)
        found = _existing_business(conn)
    except Exception:
        conn.close()
        raise
    if found is not None and found["type"] != JASA_KEY:
        conn.close()
        raise ValueError(
            f"{db_path} belongs to a {found['type']!r} office, not a Kantor Jasa. Use another --db file."
        )
    if found is not None:
        try:
            init_jasa_schema(conn)  # idempotent: also re-installs the invoice triggers
        except Exception:
            conn.close()
            raise

    def build_context(business_id: int) -> OfficeContext:
        bus = EventBus()
        roles = JASA_TEMPLATE.build_roles(conn, business_id, clock)
        manager = Manager(conn, business_id, clock, build_llm_client(settings), settings, roles, bus)
        heartbeat = Heartbeat(conn, business_id, clock, manager, roles, settings, bus)
        tracker = VisualTracker(conn, business_id, bus, clock, heartbeat)
        return OfficeContext(
            conn=conn,
            business_id=business_id,
            clock=clock,
            bus=bus,
            settings=settings,
            heartbeat=heartbeat,
            tracker=tracker,
            manager=manager,
            autostart=autostart,
        )

    def create_business(name: str, business_type: str) -> OfficeContext:
        template = templates.get(business_type)
        return build_context(bootstrap(conn, template, name, clock))

    templates = TemplateRegistry([JASA_TEMPLATE])  # this app offers its own business type only
    runtime = OfficeRuntime(templates=templates, bootstrap_callback=create_business)
    if found is not None:
        runtime.ctx = build_context(int(found["id"]))
    return runtime


# ---------------------------------------------------------------------- #
# Command line
# ---------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apps/kantor-jasa/app.py",
        description="Kantor Jasa: a 3D office of AI staff for a salon, workshop or other small service business.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on (default 8000)")
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH, help="SQLite file, created on first run (default kantor-jasa.db)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between heartbeat ticks, at least 1 (default: KANTOR_HEARTBEAT_SECONDS, else 300)",
    )
    parser.add_argument(
        "--no-pacing",
        action="store_true",
        help="turn off work pacing: tasks finish at once and staff are rarely seen working (see docs/pacing.md)",
    )
    parser.add_argument(
        "--pace-min",
        type=float,
        default=None,
        help="shortest think time in seconds (default: KANTOR_PACING_MIN, else 3)",
    )
    parser.add_argument(
        "--pace-max",
        type=float,
        default=None,
        help="longest think time in seconds, at most 60 (default: KANTOR_PACING_MAX, else 8)",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="address to listen on (default 127.0.0.1: this computer only)",
    )
    return parser.parse_args(argv)


def _pacing_note(settings: Settings) -> str:
    if not settings.pacing_enabled:
        return "Work pacing: off (tasks finish at once)."
    return (
        f"Work pacing: on, {settings.pacing_min_seconds:g} to {settings.pacing_max_seconds:g} s of think time "
        "per task (--no-pacing turns it off; see docs/pacing.md)."
    )


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.interval is not None and args.interval < MIN_INTERVAL_SECONDS:
        raise SystemExit("--interval must be at least 1 second")
    try:
        # Pacing is on for the live app unless KANTOR_PACING or --no-pacing says otherwise.
        settings = load_settings(pacing_default=True)
        if args.no_pacing:
            settings = settings_with_pacing(settings, enabled=False)
        elif args.pace_min is not None or args.pace_max is not None:
            settings = settings_with_pacing(
                settings, enabled=settings.pacing_enabled, min=args.pace_min, max=args.pace_max
            )
        runtime = create_jasa_runtime(args.db, settings=settings, interval=args.interval)
    except (ValueError, sqlite3.Error) as exc:
        raise SystemExit(f"Cannot start Kantor Jasa: {exc}") from None
    import uvicorn  # imported here so tests can import this module without starting a server

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Warning: listening on {args.host}. Anyone who can reach this address can open the office "
            "and change its settings: there is no login.",
            flush=True,
        )
    print(_pacing_note(settings), flush=True)
    print(f"Kantor Jasa: http://{args.host}:{args.port}/  (Ctrl+C to stop)", flush=True)
    uvicorn.run(create_app(runtime), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
