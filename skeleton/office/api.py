"""FastAPI app for the office: REST endpoints, the SSE stream and the static front end.

``create_app(ctx)`` builds the app from an ``OfficeContext``. Endpoints:

* ``GET  /api/office``            business, agents, policy, spend today, demo flag
* ``GET  /api/agents/{id}``       panel data for one agent (Req 2.9)
* ``PATCH /api/agents/{id}``      rename an agent or change its role (Req 1.4)
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

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from skeleton.log.activity import daily_summary, install_append_only_triggers, recent
from skeleton.office.context import OfficeContext
from skeleton.office.sse import sse_response
from skeleton.policy.engine import ALLOWED_KINDS, set_policy, spent_today
from skeleton.runtime.controls import set_agent_paused, set_business_paused

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SQLITE_MAX_INT = 2**63 - 1
MAX_DAILY_LIMIT_IDR = 10**15  # far above any real limit, far below SQLite's int range
MAX_NAME_CHARS = 80
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
def create_app(ctx: OfficeContext) -> FastAPI:
    """Build the office app around ``ctx``. Nothing runs until the app starts."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Idempotent, and cheap insurance that the activity log stays append-only.
        install_append_only_triggers(ctx.conn)
        if ctx.autostart:
            ctx.tracker.start()
            if ctx.heartbeat is not None:
                ctx.heartbeat.start()
        try:
            yield
        finally:
            if ctx.autostart:
                if ctx.heartbeat is not None:
                    await ctx.heartbeat.stop()
                await ctx.tracker.stop()

    app = FastAPI(title="Kantor Agen", lifespan=lifespan)
    app.state.office = ctx
    conn, business_id = ctx.conn, ctx.business_id

    # -------------------------------------------------------------- #
    # Errors: fixed messages, never the rejected input
    # -------------------------------------------------------------- #
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
    def load_business() -> sqlite3.Row:
        row = conn.execute(
            "SELECT id, name, type, paused FROM business WHERE id = ?", (business_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="business not found")
        return row

    def load_agent(agent_id: int) -> sqlite3.Row:
        row = None
        if 0 <= agent_id <= SQLITE_MAX_INT:  # a larger number cannot exist, and would overflow SQLite
            row = conn.execute(
                "SELECT id, name, role, desk_index, paused FROM agent WHERE id = ? AND business_id = ?",
                (agent_id, business_id),
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

    def policy_view() -> dict[str, Any]:
        row = conn.execute(
            "SELECT daily_limit_idr, blocked_actions FROM policy WHERE business_id = ?",
            (business_id,),
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

    def pending_outbox_count() -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE business_id = ? AND status = 'pending'",
            (business_id,),
        ).fetchone()
        return int(row[0])

    def agent_spent_today(agent_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger "
            "WHERE business_id = ? AND agent_id = ? AND day = ?",
            (business_id, agent_id, ctx.clock.today()),
        ).fetchone()
        return int(row[0])

    def refresh_tracker() -> None:
        try:
            ctx.tracker.refresh()
        except Exception:
            logger.exception("visual tracker refresh failed")

    # -------------------------------------------------------------- #
    # Read endpoints
    # -------------------------------------------------------------- #
    @app.get("/api/office")
    async def get_office() -> dict[str, Any]:
        business = load_business()
        agents = conn.execute(
            "SELECT id, name, role, desk_index, paused FROM agent WHERE business_id = ? ORDER BY id",
            (business_id,),
        ).fetchall()
        return {
            "business": {
                "id": int(business["id"]),
                "name": business["name"],
                "type": business["type"],
                "paused": bool(business["paused"]),
            },
            "agents": [agent_summary(a) for a in agents],
            "policy": policy_view(),
            "spent_today_idr": spent_today(conn, business_id, ctx.clock),
            "demo_mode": bool(ctx.settings.public_dict()["demo_mode"]),
        }

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: int) -> dict[str, Any]:
        row = load_agent(agent_id)
        live = next((e for e in ctx.tracker.current() if e["agent_id"] == agent_id), None)
        actions = recent(conn, business_id, limit=RECENT_ACTION_COUNT, agent_id=agent_id)
        return {
            **agent_summary(row),
            "state": live["state"] if live else "idle",
            "bubble_text": live["bubble_text"] if live else "",
            "spent_today_idr": agent_spent_today(agent_id),
            "recent_actions": [{k: a[k] for k in _AGENT_PUBLIC_FIELDS} for a in actions],
        }

    @app.get("/api/activity")
    async def get_activity(
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_ACTIVITY_LIMIT,
    ) -> list[dict[str, Any]]:
        load_business()
        return recent(conn, business_id, limit=limit)

    @app.get("/api/summary")
    async def get_summary() -> dict[str, Any]:
        load_business()
        summary = daily_summary(conn, business_id, ctx.clock.today())
        return {**summary, "pending_outbox": pending_outbox_count()}

    @app.get("/api/outbox")
    async def get_outbox(
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_OUTBOX_LIMIT,
    ) -> list[dict[str, Any]]:
        load_business()
        rows = conn.execute(
            "SELECT id, agent_id, channel, recipient, body, status FROM outbox "
            "WHERE business_id = ? AND status = 'pending' ORDER BY id DESC LIMIT ?",
            (business_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/events")
    async def get_events(request: Request):
        load_business()
        return sse_response(ctx.bus, ctx.tracker, request)

    # -------------------------------------------------------------- #
    # Owner controls
    # -------------------------------------------------------------- #
    @app.post("/api/office/pause")
    async def pause_office(body: PauseBody) -> dict[str, Any]:
        load_business()
        # controls publishes {"type": "pause_changed"} on the bus after the write.
        if not set_business_paused(conn, business_id, body.paused, bus=ctx.bus):
            raise HTTPException(status_code=404, detail="business not found")
        refresh_tracker()
        return {"paused": load_business()["paused"] == 1}

    @app.post("/api/agents/{agent_id}/pause")
    async def pause_agent(agent_id: int, body: PauseBody) -> dict[str, Any]:
        load_agent(agent_id)  # 404 for an unknown agent or one of another business
        if not set_agent_paused(conn, agent_id, body.paused, bus=ctx.bus):
            raise HTTPException(status_code=404, detail="agent not found")
        refresh_tracker()
        return {
            "id": agent_id,
            "paused": bool(load_agent(agent_id)["paused"]),
            "business_paused": bool(load_business()["paused"]),
        }

    @app.patch("/api/agents/{agent_id}")
    async def patch_agent(agent_id: int, body: AgentPatchBody) -> dict[str, Any]:
        load_agent(agent_id)
        if body.name is None and body.role is None:
            raise HTTPException(status_code=422, detail="give a name, a role, or both")
        conn.execute(
            "UPDATE agent SET name = COALESCE(?, name), role = COALESCE(?, role) "
            "WHERE id = ? AND business_id = ?",
            (body.name, body.role, agent_id, business_id),
        )
        return agent_summary(load_agent(agent_id))

    @app.put("/api/policy")
    async def put_policy(body: PolicyBody) -> dict[str, Any]:
        load_business()
        try:
            set_policy(conn, business_id, body.daily_limit_idr, body.blocked_actions)
        except ValueError as exc:  # already checked above; the engine is the last line
            raise HTTPException(status_code=422, detail=str(exc)) from None
        # The engine reads the policy row on every authorize call, so this
        # applies from the next action (Req 8.2).
        return policy_view()

    # -------------------------------------------------------------- #
    # Front end: last, so /api/* wins. html=True serves index.html at "/".
    # -------------------------------------------------------------- #
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="front-end")
    return app
