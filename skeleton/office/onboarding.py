"""First-run onboarding: business templates and ``bootstrap`` (Req 1.1, 1.2, 1.3, 10.1).

The skeleton does not know any business type. An application describes its type
with a :class:`BusinessTemplate` (key, texts, the AI staff to hire, seed data and
how to build the roles) and registers it in a :class:`TemplateRegistry`. The
office API offers the registered types to the owner on first run and calls
``bootstrap`` for the one that was picked. Kantor Warung and Kantor Jasa both
use this module unchanged.

What ``bootstrap`` does
-----------------------
1. Validates the business name: text, trimmed, 1 to 60 characters, no control
   characters. A bad name raises :class:`InvalidBusinessName` before anything is
   written.
2. In ONE ``BEGIN IMMEDIATE`` transaction: refuses when a business already
   exists (:class:`AlreadyOnboarded`, one business per database file), creates the
   business row, one agent row per :class:`AgentSpec` (``desk_index`` follows
   the order of ``template.agents``, ``agent.role`` is the template's role key
   so ``template.build_roles`` can map the agents) and the agent's display label.
3. Runs ``template.seed`` (demo data, Req 10.1).
4. Does NOT set the policy. The owner must set it before any agent can spend
   (Req 1.3); until then ``authorize`` refuses every spend ("policy not set").

Why seed is not inside the transaction of step 2
------------------------------------------------
An application seeds through its own data layer, and each data-layer function
opens its own ``transaction`` (``BEGIN IMMEDIATE``). SQLite does not nest those,
so ``seed`` cannot run inside the transaction of step 2. If ``seed`` raises, the
business is deleted again (foreign keys cascade to agents, products, sales and
so on), so a failed onboarding leaves no business behind and the owner can try
again. A seed function must therefore not write ``activity`` rows (the activity
log is append-only and a cascade cannot delete them).

Role key and role label
-----------------------
Roles find each other by ``agent.role`` (the stock role hands a purchase to the
agent whose role is ``finance``), so that column is a stable KEY. What the owner
sees and may change (Req 1.4) is a LABEL kept in ``agent_label``. The office API
shows the label when there is one and falls back to ``agent.role``, so contexts
built without onboarding behave as before.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from skeleton.core.clock import Clock
from skeleton.core.db import transaction

if TYPE_CHECKING:
    from skeleton.runtime.models import Role

logger = logging.getLogger(__name__)

MAX_BUSINESS_NAME_CHARS = 60
MAX_LABEL_CHARS = 80
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

LABEL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_label (
    agent_id  INTEGER PRIMARY KEY REFERENCES agent(id) ON DELETE CASCADE,
    label     TEXT    NOT NULL
)
"""


class OnboardingError(ValueError):
    """Base class of every rejection raised by this module."""


class InvalidBusinessName(OnboardingError):
    """The business name is not text, is empty after trimming, is too long, or has control characters."""


class AlreadyOnboarded(OnboardingError):
    """The database already holds a business (one business per database file)."""


class UnknownBusinessType(OnboardingError):
    """No template is registered under that key."""


# ---------------------------------------------------------------------- #
# Templates
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgentSpec:
    """One AI employee a business type hires."""

    role_key: str  # stored in agent.role; the app's role registry maps it to a Role
    default_name: str  # first name shown at the desk; the owner may rename later
    role_label: str  # what the owner reads, for example "Kasir"

    def __post_init__(self) -> None:
        for field_name in ("role_key", "default_name", "role_label"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"AgentSpec.{field_name} must be a non-empty string")
        if len(self.default_name) > MAX_LABEL_CHARS or len(self.role_label) > MAX_LABEL_CHARS:
            raise ValueError(f"AgentSpec texts must be at most {MAX_LABEL_CHARS} characters")


SeedFn = Callable[[sqlite3.Connection, int, Clock], None]
BuildRolesFn = Callable[[sqlite3.Connection, int, Clock], "dict[int, Role]"]


@dataclass(frozen=True)
class BusinessTemplate:
    """Everything the skeleton needs to know about one business type.

    ``default_policy`` is only a suggestion for the policy form, as
    ``(daily_limit_idr, blocked_actions)``. ``bootstrap`` never writes it.
    """

    key: str
    label: str
    description: str
    agents: tuple[AgentSpec, ...]
    seed: SeedFn
    build_roles: BuildRolesFn
    default_policy: tuple[int, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not _KEY_PATTERN.match(self.key):
            raise ValueError("template key must be lowercase letters, digits, '_' or '-' (1 to 32 chars)")
        for field_name in ("label", "description"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"template {field_name} must be a non-empty string")
        agents = tuple(self.agents)
        if not agents or not all(isinstance(a, AgentSpec) for a in agents):
            raise ValueError("a template needs at least one AgentSpec")
        object.__setattr__(self, "agents", agents)
        if not callable(self.seed) or not callable(self.build_roles):
            raise ValueError("seed and build_roles must be callable")

    def public_view(self) -> dict[str, Any]:
        """What the browser may see: texts and the staff preview. Never a function."""
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "agents": [{"name": a.default_name, "role": a.role_label} for a in self.agents],
        }


class TemplateRegistry:
    """Business templates by key, in registration order."""

    def __init__(self, templates: Iterable[BusinessTemplate] = ()) -> None:
        self._templates: dict[str, BusinessTemplate] = {}
        for template in templates:
            self.register(template)

    def register(self, template: BusinessTemplate) -> BusinessTemplate:
        if template.key in self._templates:
            raise ValueError(f"business type already registered: {template.key!r}")
        self._templates[template.key] = template
        return template

    def get(self, key: object) -> BusinessTemplate:
        template = self._templates.get(key) if isinstance(key, str) else None
        if template is None:
            raise UnknownBusinessType("unknown business type")
        return template

    def keys(self) -> list[str]:
        return list(self._templates)

    def public_list(self) -> list[dict[str, Any]]:
        return [t.public_view() for t in self._templates.values()]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._templates

    def __len__(self) -> int:
        return len(self._templates)


# ---------------------------------------------------------------------- #
# Agent labels
# ---------------------------------------------------------------------- #
def ensure_label_table(conn: sqlite3.Connection) -> None:
    """Create ``agent_label`` when missing. One statement, safe to repeat and to call in a transaction."""
    conn.execute(LABEL_TABLE_SQL)


def set_role_label(conn: sqlite3.Connection, agent_id: int, label: str) -> None:
    """Set the label the owner sees for an agent's role. ``agent.role`` (the key) is not touched."""
    conn.execute(
        "INSERT INTO agent_label (agent_id, label) VALUES (?, ?) "
        "ON CONFLICT(agent_id) DO UPDATE SET label = excluded.label",
        (agent_id, label),
    )


# ---------------------------------------------------------------------- #
# Validation and bootstrap
# ---------------------------------------------------------------------- #
def normalize_business_name(name: object) -> str:
    """Trimmed name of 1 to 60 characters, or :class:`InvalidBusinessName`."""
    if not isinstance(name, str):
        raise InvalidBusinessName("business name must be text")
    clean = name.strip()
    if not clean:
        raise InvalidBusinessName("business name must not be empty")
    if len(clean) > MAX_BUSINESS_NAME_CHARS:
        raise InvalidBusinessName(f"business name must be at most {MAX_BUSINESS_NAME_CHARS} characters")
    if any(unicodedata.category(ch).startswith("C") for ch in clean):
        raise InvalidBusinessName("business name must not contain control characters")
    return clean


def business_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM business LIMIT 1").fetchone() is not None


def bootstrap(conn: sqlite3.Connection, template: BusinessTemplate, name: str, clock: Clock) -> int:
    """Create the business, its agents and its seed data. Returns the business id.

    The policy is NOT set (Req 1.3). Raises :class:`InvalidBusinessName` or
    :class:`AlreadyOnboarded` before writing anything. If ``template.seed`` raises,
    the business is removed again and the error is re-raised. See the module
    docstring for why seed runs after the transaction that creates the agents.
    """
    clean_name = normalize_business_name(name)
    with transaction(conn):
        if business_exists(conn):
            raise AlreadyOnboarded("this database already has a business")
        ensure_label_table(conn)
        business_id = int(
            conn.execute(
                "INSERT INTO business (name, type) VALUES (?, ?)", (clean_name, template.key)
            ).lastrowid
        )
        for desk_index, spec in enumerate(template.agents):
            agent_id = int(
                conn.execute(
                    "INSERT INTO agent (business_id, role, name, desk_index) VALUES (?, ?, ?, ?)",
                    (business_id, spec.role_key, spec.default_name, desk_index),
                ).lastrowid
            )
            set_role_label(conn, agent_id, spec.role_label)
    try:
        template.seed(conn, business_id, clock)
    except BaseException:
        _discard_business(conn, business_id)
        raise
    return business_id


def _discard_business(conn: sqlite3.Connection, business_id: int) -> None:
    """Undo a half finished onboarding. Never raises: the caller re-raises the seed error."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        with transaction(conn):
            conn.execute(
                "DELETE FROM purchase_order WHERE product_id IN "
                "(SELECT id FROM product WHERE business_id = ?)",
                (business_id,),
            )
            conn.execute("DELETE FROM business WHERE id = ?", (business_id,))
    except Exception:
        logger.exception("could not remove the half finished business %s", business_id)
