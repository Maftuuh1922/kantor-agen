"""FastAPI app for the office: REST endpoints, the SSE stream and the static front end.

``create_app(source)`` builds the app from an ``OfficeContext`` (the office exists)
or from an ``OfficeRuntime`` (the office may not exist yet: first run). Endpoints:

* ``GET  /api/office``            business, agents, policy, spend today, demo flag.
                                  Before onboarding: ``{"onboarding_required": true,
                                  "business_types": [...]}``
* ``POST /api/onboarding``        first run: create the business (Req 1.1, 1.2)
* ``GET  /api/agents/{id}``       panel data for one agent (Req 2.9)
* ``PATCH /api/agents/{id}``      rename an agent or change its role label (Req 1.4)
* ``GET  /api/activity``          newest activity first
* ``GET  /api/summary``           today's summary plus the pending outbox count
* ``GET  /api/outbox``            pending customer messages (read only)
* ``POST /api/office/pause``      pause or resume the whole office (Req 8.1)
* ``POST /api/agents/{id}/pause`` pause or resume one agent (Req 8.1)
* ``PUT  /api/policy``            set the daily limit and blocked actions (Req 1.3, 8.2)
* ``GET  /api/events``            SSE stream (snapshot, visual_state, activity)
* ``/``                           the static front end (``static/index.html``)

Rules:

* Input is validated with strict Pydantic models before anything is written.
  A rejected request writes nothing.
* Every handler is ``async`` and so runs on the event loop thread, the same
  thread as the heartbeat and the tracker. The single SQLite connection is
  therefore never used by two threads at once.
* No response contains the API key. The office endpoint uses
  ``Settings.public_dict()`` and error responses carry fixed messages, not the
  rejected input (Req 10.2).
* The static mount is added last, so ``/api/*`` routes win over it.
* While there is no business (``runtime.ctx is None``) every ``/api/*`` route except
  ``GET /api/office`` and ``POST /api/onboarding`` answers 409
  ``{"error": "onboarding_required"}``, before the body is even validated.
* The heartbeat of an ``OfficeRuntime`` starts only when the policy is set (Req 1.3).
  An ``OfficeContext`` passed directly keeps the old behavior: it starts with the app.
* ``agent.role`` is a stable key (roles find each other by it). The role the owner
  sees and edits is the label in ``agent_label`` (see ``onboarding.py``).

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from skeleton.core.db import transaction
from skeleton.log.activity import daily_summary, install_append_only_triggers, recent
from skeleton.office.context import OfficeContext, OfficeRuntime
from skeleton.office.onboarding import (
    AlreadyOnboarded,
    InvalidBusinessName,
    TemplateRegistry,
    UnknownBusinessType,
    ensure_label_table,
    normalize_business_name,
    set_role_label,
)
from skeleton.office.sse import sse_response
from skeleton.policy.engine import ALLOWED_KINDS, set_policy, spent_today
from skeleton.runtime.controls import set_agent_paused, set_business_paused

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SQLITE_MAX_INT = 2**63 - 1
MAX_DAILY_LIMIT_IDR = 10**15  # far above any real limit, far below SQLite's int range
MAX_NAME_CHARS = 80
MAX_ONBOARDING_FIELD_CHARS = 1000  # a coarse cap before the real 60 character check
ERROR_ONBOARDING_REQUIRED = "onboarding_required"
ERROR_ALREADY_ONBOARDED = "already_onboarded"
# The only /api routes that work before a business exists.
_OPEN_BEFORE_ONBOARDING = frozenset({"/api/office", "/api/onboarding"})
MAX_LIST_LIMIT = 500
RECENT_ACTION_COUNT = 10
DEFAULT_ACTIVITY_LIMIT = 50
DEFAULT_OUTBOX_LIMIT = 100

_AGENT_PUBLIC_FIELDS = ("id", "action", "reason", "cost_idr", "status", "created_at")


# ---------------------------------------------------------------------- #
# Request bodies (strict: no bool-as-int, no "true" strings, no extra keys)
# ---------------------------------------------------------------------- #
class PauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: StrictBool


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_limit_idr: Annotated[StrictInt, Field(ge=0, le=MAX_DAILY_LIMIT_IDR)]
    blocked_actions: list[StrictStr]

    @field_validator("blocked_actions")
    @classmethod
    def _known_actions(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALLOWED_KINDS)
        if unknown:
            raise ValueError(f"unknown action type; allowed: {sorted(ALLOWED_KINDS)}")
        return value


class OnboardingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[StrictStr, Field(max_length=MAX_ONBOARDING_FIELD_CHARS)]
    type: Annotated[StrictStr, Field(max_length=MAX_ONBOARDING_FIELD_CHARS)]

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return normalize_business_name(value)  # trimmed, 1 to 60 characters


class AgentPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[StrictStr, Field(min_length=1, max_length=MAX_NAME_CHARS)] | None = None
    role: Annotated[StrictStr, Field(min_length=1, max_length=MAX_NAME_CHARS)] | None = None

    @field_validator("name", "role")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("must not be blank")
        return value


# ---------------------------------------------------------------------- #
# App factory
# ---------------------------------------------------------------------- #
class _OnboardingRequired(Exception):
    """Raised by the gate: there is no business yet."""


def create_app(source: OfficeContext | OfficeRuntime) -> FastAPI:
    """Build the office app around an office or around an office that is yet to be created.

    Nothing runs until the app starts.
    """
    if isinstance(source, OfficeContext):
        runtime = OfficeRuntime(templates=TemplateRegistry(), ctx=source)
        gate_heartbeat = False  # an existing office keeps its old behavior: start with the app
    else:
        runtime = source
        gate_heartbeat = True  # a runtime waits for the policy (Req 1.3)
    heartbeat_waiting = False  # True: autostart is on but the heartbeat waits for the policy

    def policy_is_set(ctx: OfficeContext) -> bool:
        return ctx.conn.execute(
            "SELECT 1 FROM policy WHERE business_id = ?", (ctx.business_id,)
        ).fetchone() is not None

    def activate(ctx: OfficeContext) -> None:
        """Start what a running office needs. Idempotent; the append-only triggers are cheap insurance."""
        nonlocal heartbeat_waiting
        install_append_only_triggers(ctx.conn)
        ensure_label_table(ctx.conn)
        if not ctx.autostart:
            return
        ctx.tracker.start()
        if ctx.heartbeat is None:
            return
        if gate_heartbeat and not policy_is_set(ctx):
            heartbeat_waiting = True
        else:
            ctx.heartbeat.start()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if runtime.ctx is not None:
            activate(runtime.ctx)
        try:
            yield
        finally:
            ctx = runtime.ctx
            if ctx is not None and ctx.autostart:
                if ctx.heartbeat is not None:
                    await ctx.heartbeat.stop()
                await ctx.tracker.stop()

    async def gate(request: Request) -> None:
        # Runs before the body is validated, so a bad body still gets the 409.
        if runtime.ctx is None and request.url.path not in _OPEN_BEFORE_ONBOARDING:
            raise _OnboardingRequired()

    app = FastAPI(title="Kantor Agen", lifespan=lifespan, dependencies=[Depends(gate)])
    app.state.office = runtime.ctx
    app.state.runtime = runtime
    if runtime.ctx is not None:
        ensure_label_table(runtime.ctx.conn)  # the lifespan may not run (plain TestClient)

    def current() -> OfficeContext:
        if runtime.ctx is None:
            raise _OnboardingRequired()
        return runtime.ctx

    # -------------------------------------------------------------- #
    # Errors: fixed messages, never the rejected input
    # -------------------------------------------------------------- #
    @app.exception_handler(_OnboardingRequired)
    async def _needs_onboarding(_request: Request, _exc: _OnboardingRequired) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": ERROR_ONBOARDING_REQUIRED})

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"loc": list(err.get("loc", ())), "msg": str(err.get("msg", "")), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": problems})

    @app.exception_handler(sqlite3.Error)
    async def _database_error(_request: Request, exc: sqlite3.Error) -> JSONResponse:
        logger.error("database error: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "database unavailable, try again"})

    # -------------------------------------------------------------- #
    # Helpers (all run on the event loop thread)
    # -------------------------------------------------------------- #
    def load_business(ctx: OfficeContext) -> sqlite3.Row:
        row = ctx.conn.execute(
            "SELECT id, name, type, paused FROM business WHERE id = ?", (ctx.business_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="business not found")
        return row

    # role = the owner's label when there is one, else the stable role key
    agent_columns = (
        "SELECT a.id, a.name, COALESCE(l.label, a.role) AS role, a.desk_index, a.paused "
        "FROM agent a LEFT JOIN agent_label l ON l.agent_id = a.id "
    )

    def load_agent(ctx: OfficeContext, agent_id: int) -> sqlite3.Row:
        row = None
        if 0 <= agent_id <= SQLITE_MAX_INT:  # a larger number cannot exist, and would overflow SQLite
            row = ctx.conn.execute(
                agent_columns + "WHERE a.id = ? AND a.business_id = ?", (agent_id, ctx.business_id)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="agent not found")
        return row

    def agent_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "role": row["role"],
            "desk_index": int(row["desk_index"]),
            "paused": bool(row["paused"]),
        }

    def policy_view(ctx: OfficeContext) -> dict[str, Any]:
        row = ctx.conn.execute(
            "SELECT daily_limit_idr, blocked_actions FROM policy WHERE business_id = ?",
            (ctx.business_id,),
        ).fetchone()
        if row is None:
            # Not set yet: agents cannot spend until the owner sets it (Req 1.3).
            return {"daily_limit_idr": None, "blocked_actions": [], "is_set": False}
        try:
            blocked = json.loads(row["blocked_actions"])
            if not isinstance(blocked, list) or not all(isinstance(b, str) for b in blocked):
                raise ValueError("malformed")
        except ValueError:
            # The engine treats a corrupt list as "everything blocked"; show the same.
            blocked = sorted(ALLOWED_KINDS)
        return {
            "daily_limit_idr": int(row["daily_limit_idr"]),
            "blocked_actions": blocked,
            "is_set": True,
        }

    def pending_outbox_count(ctx: OfficeContext) -> int:
        row = ctx.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE business_id = ? AND status = 'pending'",
            (ctx.business_id,),
        ).fetchone()
        return int(row[0])

    def agent_spent_today(ctx: OfficeContext, agent_id: int) -> int:
        row = ctx.conn.execute(
            "SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger "
            "WHERE business_id = ? AND agent_id = ? AND day = ?",
            (ctx.business_id, agent_id, ctx.clock.today()),
        ).fetchone()
        return int(row[0])

    def refresh_tracker(ctx: OfficeContext) -> None:
        try:
            ctx.tracker.refresh()
        except Exception:
            logger.exception("visual tracker refresh failed")

    def office_view(ctx: OfficeContext) -> dict[str, Any]:
        business = load_business(ctx)
        agents = ctx.conn.execute(
            agent_columns + "WHERE a.business_id = ? ORDER BY a.id", (ctx.business_id,)
        ).fetchall()
        return {
            "business": {
                "id": int(business["id"]),
                "name": business["name"],
                "type": business["type"],
                "paused": bool(business["paused"]),
            },
            "agents": [agent_summary(a) for a in agents],
            "policy": policy_view(ctx),
            "spent_today_idr": spent_today(ctx.conn, ctx.business_id, ctx.clock),
            "demo_mode": bool(ctx.settings.public_dict()["demo_mode"]),
        }

    # -------------------------------------------------------------- #
    # Read endpoints
    # -------------------------------------------------------------- #
    @app.get("/api/office")
    async def get_office() -> dict[str, Any]:
        if runtime.ctx is None:
            return {"onboarding_required": True, "business_types": runtime.templates.public_list()}
        return office_view(runtime.ctx)

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: int) -> dict[str, Any]:
        ctx = current()
        row = load_agent(ctx, agent_id)
        live = next((e for e in ctx.tracker.current() if e["agent_id"] == agent_id), None)
        actions = recent(ctx.conn, ctx.business_id, limit=RECENT_ACTION_COUNT, agent_id=agent_id)
        return {
            **agent_summary(row),
            "state": live["state"] if live else "idle",
            "bubble_text": live["bubble_text"] if live else "",
            "spent_today_idr": agent_spent_today(ctx, agent_id),
            "recent_actions": [{k: a[k] for k in _AGENT_PUBLIC_FIELDS} for a in actions],
        }

    @app.get("/api/activity")
    async def get_activity(
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_ACTIVITY_LIMIT,
    ) -> list[dict[str, Any]]:
        ctx = current()
        load_business(ctx)
        return recent(ctx.conn, ctx.business_id, limit=limit)

    @app.get("/api/summary")
    async def get_summary() -> dict[str, Any]:
        ctx = current()
        load_business(ctx)
        summary = daily_summary(ctx.conn, ctx.business_id, ctx.clock.today())
        return {**summary, "pending_outbox": pending_outbox_count(ctx)}

    @app.get("/api/outbox")
    async def get_outbox(
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_OUTBOX_LIMIT,
    ) -> list[dict[str, Any]]:
        ctx = current()
        load_business(ctx)
        rows = ctx.conn.execute(
            "SELECT id, agent_id, channel, recipient, body, status FROM outbox "
            "WHERE business_id = ? AND status = 'pending' ORDER BY id DESC LIMIT ?",
            (ctx.business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/events")
    async def get_events(request: Request):
        ctx = current()
        load_business(ctx)
        return sse_response(ctx.bus, ctx.tracker, request)

    # -------------------------------------------------------------- #
    # First run (Req 1.1, 1.2)
    # -------------------------------------------------------------- #
    @app.post("/api/onboarding")
    async def post_onboarding(body: OnboardingBody) -> dict[str, Any]:
        nonlocal heartbeat_waiting
        if runtime.ctx is not None:
            return JSONResponse(status_code=409, content={"error": ERROR_ALREADY_ONBOARDED})
        if runtime.bootstrap_callback is None:
            raise HTTPException(status_code=409, detail="this app cannot create a business")
        try:
            runtime.templates.get(body.type)
        except UnknownBusinessType:
            # Same 422 shape as every other validation error, and the input is not echoed.
            raise RequestValidationError(
                [{"type": "value_error", "loc": ("body", "type"), "msg": "unknown business type"}]
            ) from None
        try:
            new_ctx = runtime.bootstrap_callback(body.name, body.type)
        except InvalidBusinessName:
            raise RequestValidationError(
                [{"type": "value_error", "loc": ("body", "name"), "msg": "invalid business name"}]
            ) from None
        except AlreadyOnboarded:
            return JSONResponse(status_code=409, content={"error": ERROR_ALREADY_ONBOARDED})
        except Exception:
            logger.exception("onboarding failed")
            return JSONResponse(status_code=500, content={"detail": "could not create the business"})
        runtime.ctx = new_ctx
        app.state.office = new_ctx
        heartbeat_waiting = False
        activate(new_ctx)
        return office_view(new_ctx)

    # -------------------------------------------------------------- #
    # Owner controls
    # -------------------------------------------------------------- #
    @app.post("/api/office/pause")
    async def pause_office(body: PauseBody) -> dict[str, Any]:
        ctx = current()
        load_business(ctx)
        # controls publishes {"type": "pause_changed"} on the bus after the write.
        if not set_business_paused(ctx.conn, ctx.business_id, body.paused, bus=ctx.bus):
            raise HTTPException(status_code=404, detail="business not found")
        refresh_tracker(ctx)
        return {"paused": load_business(ctx)["paused"] == 1}

    @app.post("/api/agents/{agent_id}/pause")
    async def pause_agent(agent_id: int, body: PauseBody) -> dict[str, Any]:
        ctx = current()
        load_agent(ctx, agent_id)  # 404 for an unknown agent or one of another business
        if not set_agent_paused(ctx.conn, agent_id, body.paused, bus=ctx.bus):
            raise HTTPException(status_code=404, detail="agent not found")
        refresh_tracker(ctx)
        return {
            "id": agent_id,
            "paused": bool(load_agent(ctx, agent_id)["paused"]),
            "business_paused": bool(load_business(ctx)["paused"]),
        }

    @app.patch("/api/agents/{agent_id}")
    async def patch_agent(agent_id: int, body: AgentPatchBody) -> dict[str, Any]:
        ctx = current()
        load_agent(ctx, agent_id)
        if body.name is None and body.role is None:
            raise HTTPException(status_code=422, detail="give a name, a role, or both")
        with transaction(ctx.conn):
            if body.name is not None:
                ctx.conn.execute(
                    "UPDATE agent SET name = ? WHERE id = ? AND business_id = ?",
                    (body.name, agent_id, ctx.business_id),
                )
            if body.role is not None:
                # The label only. agent.role stays the key the roles use to find each other.
                set_role_label(ctx.conn, agent_id, body.role)
        return agent_summary(load_agent(ctx, agent_id))

    @app.put("/api/policy")
    async def put_policy(body: PolicyBody) -> dict[str, Any]:
        nonlocal heartbeat_waiting
        ctx = current()
        load_business(ctx)
        try:
            set_policy(ctx.conn, ctx.business_id, body.daily_limit_idr, body.blocked_actions)
        except ValueError as exc:  # already checked above; the engine is the last line
            raise HTTPException(status_code=422, detail=str(exc)) from None
        # The engine reads the policy row on every authorize call, so this
        # applies from the next action (Req 8.2).
        if heartbeat_waiting and ctx.heartbeat is not None:
            heartbeat_waiting = False
            ctx.heartbeat.start()  # the owner has set the policy: the agents may start (Req 1.3)
        return policy_view(ctx)

    # -------------------------------------------------------------- #
    # Front end: last, so /api/* wins. html=True serves index.html at "/".
    # -------------------------------------------------------------- #
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="front-end")
    return app
