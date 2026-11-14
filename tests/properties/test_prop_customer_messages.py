"""Property test: customer messages are queued in the outbox and never sent.

Self-contained on purpose: no shared conftest, own fixtures and helpers.
"""
import http.client
import smtplib
import socket
import sqlite3
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.policy.engine import (
    DEFAULT_CHANNEL,
    DEFAULT_RECIPIENT,
    OUTCOME_QUEUED,
    authorize,
    set_policy,
    spent_today,
)

BUSINESS_ID = 1
AGENT_ID = 1


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@dataclass
class FakeTask:
    kind: str = "customer_message"
    cost_idr: int = 0
    agent_id: int = AGENT_ID
    payload: object = field(default_factory=dict)


class SendAttempted(AssertionError):
    """Raised by a guard when code touches a network or send path."""


def _make_db() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (?, 'Warung', 'warung')", (BUSINESS_ID,))
    conn.execute(
        "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, ?, 'cs', 'Dewi', 0)",
        (AGENT_ID, BUSINESS_ID),
    )
    return conn


def _make_clock() -> FakeClock:
    # 2025-01-01 03:00 UTC = 10:00 in Jakarta
    return FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))


def _guard(name: str):
    def _raise(*_args, **_kwargs):
        raise SendAttempted(f"send path touched: {name}")

    return _raise


@contextmanager
def no_send_paths():
    """Make every common send path raise if it is called."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(smtplib, "SMTP", _guard("smtplib.SMTP"))
        mp.setattr(smtplib, "SMTP_SSL", _guard("smtplib.SMTP_SSL"))
        mp.setattr(socket.socket, "connect", _guard("socket.socket.connect"))
        mp.setattr(socket.socket, "connect_ex", _guard("socket.socket.connect_ex"))
        mp.setattr(httpx.Client, "send", _guard("httpx.Client.send"))
        mp.setattr(httpx.AsyncClient, "send", _guard("httpx.AsyncClient.send"))
        mp.setattr(urllib.request, "urlopen", _guard("urllib.request.urlopen"))
        mp.setattr(http.client.HTTPConnection, "request", _guard("http.client.HTTPConnection.request"))
        yield


def _outbox_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()


def _ledger_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0]


def _expected_text(payload: object, key: str, default: str) -> str:
    """Independent oracle: str() of the value, default when missing, None or empty."""
    if not isinstance(payload, dict):
        return default
    value = payload.get(key)
    if value is None:
        return default
    text = str(value)
    return text if text != "" else default


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
# Arbitrary text including unicode, empty strings and whitespace.
_text = st.one_of(st.just(""), st.text(max_size=60))

# Non-string values a sloppy model could put in a payload field.
_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=20),
)
_non_string = st.one_of(
    _scalars,
    st.lists(_scalars | _text, max_size=4),
    st.dictionaries(_text, _scalars | _text, max_size=3),
)
_field_value = st.one_of(_text, _text, _non_string)


@st.composite
def payloads(draw):
    """Dict payloads with some keys missing, plus rare non-dict payloads."""
    if draw(st.integers(min_value=0, max_value=9)) == 0:
        return draw(st.one_of(st.none(), st.lists(_text, max_size=3), _text, st.integers()))
    result = {}
    for key in ("channel", "recipient", "body"):
        if draw(st.booleans()) or draw(st.booleans()):  # present about 75% of the time
            result[key] = draw(_field_value)
    if draw(st.booleans()):
        result["extra"] = draw(_field_value)
    return result


@st.composite
def scenarios(draw):
    limit = draw(st.integers(min_value=0, max_value=10**12))
    blocked = draw(st.sets(st.sampled_from(["read", "draft", "spend"])))  # customer_message NOT blocked
    prior_spend = draw(st.lists(st.integers(min_value=0, max_value=10**9), max_size=3))
    tasks = draw(st.lists(payloads(), min_size=1, max_size=5))
    return limit, sorted(blocked), prior_spend, tasks


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
# Feature: kantor-agen, Property 5: Customer messages are never sent
# For any task of kind `customer_message`, the outcome is an outbox row with
# status `pending` and no call to a send function.
# Validates: Requirements 5.4 (and 11.1: the owner sees what was queued).
@settings(max_examples=200, deadline=None)
@given(scenarios())
def test_customer_messages_are_queued_never_sent(scenario):
    limit, blocked, prior_spend, payload_list = scenario
    conn = _make_db()
    clock = _make_clock()
    try:
        set_policy(conn, BUSINESS_ID, limit, blocked)
        for amount in prior_spend:
            conn.execute(
                "INSERT INTO spend_ledger (business_id, agent_id, amount_idr, day, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (BUSINESS_ID, AGENT_ID, amount, clock.today(), clock.now().isoformat()),
            )
        ledger_before = _ledger_count(conn)
        spent_before = spent_today(conn, BUSINESS_ID, clock)

        for index, payload in enumerate(payload_list, start=1):
            rows_before = len(_outbox_rows(conn))
            with no_send_paths():  # any send attempt raises SendAttempted and fails the test
                decision = authorize(conn, BUSINESS_ID, FakeTask(payload=payload), clock)

            # (a) allowed, queued in the outbox
            assert decision.allowed is True
            assert decision.outcome == "queued_in_outbox" == OUTCOME_QUEUED

            # (b) exactly one new pending row, content from the payload or safe defaults
            rows = _outbox_rows(conn)
            assert len(rows) == rows_before + 1 == index
            row = rows[-1]
            assert row["status"] == "pending"
            assert row["business_id"] == BUSINESS_ID
            assert row["agent_id"] == AGENT_ID
            assert row["body"] == _expected_text(payload, "body", "")
            assert row["channel"] == _expected_text(payload, "channel", DEFAULT_CHANNEL)
            assert row["recipient"] == _expected_text(payload, "recipient", DEFAULT_RECIPIENT)

            # (c) no money moved
            assert _ledger_count(conn) == ledger_before
            assert spent_today(conn, BUSINESS_ID, clock) == spent_before

        # (d) nothing in the outbox is anything but pending
        statuses = {r[0] for r in conn.execute("SELECT DISTINCT status FROM outbox")}
        assert statuses == {"pending"}
    finally:
        conn.close()


# Feature: kantor-agen, Property 5: Customer messages are never sent
# The outbox schema itself cannot hold a message with a status other than
# `pending`, so even a buggy caller cannot mark a message as sent.
# Validates: Requirements 5.4.
@settings(max_examples=200, deadline=None)
@given(
    status=st.one_of(st.sampled_from(["sent", "SENT", "delivered", "Pending", "pending ", ""]), _text).filter(
        lambda s: s != "pending"
    ),
    body=_text,
)
def test_outbox_check_constraint_rejects_non_pending_status(status, body):
    conn = _make_db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO outbox (business_id, agent_id, channel, recipient, body, status) "
                "VALUES (?, ?, 'whatsapp', '+62812', ?, ?)",
                (BUSINESS_ID, AGENT_ID, body, status),
            )
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        # The spec example, stated literally.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO outbox (business_id, agent_id, channel, recipient, body, status) "
                "VALUES (?, ?, 'whatsapp', '+62812', 'hi', 'sent')",
                (BUSINESS_ID, AGENT_ID),
            )
    finally:
        conn.close()


# Sanity check for the spies, so the main property cannot pass vacuously.
# Validates: Requirements 5.4.
@pytest.mark.parametrize(
    "call",
    [
        lambda: smtplib.SMTP("localhost"),
        lambda: socket.socket().connect(("127.0.0.1", 9)),
        lambda: httpx.Client().send(httpx.Request("GET", "http://localhost/")),
        lambda: httpx.AsyncClient().send(httpx.Request("GET", "http://localhost/")),
        lambda: urllib.request.urlopen("http://localhost/"),
    ],
    ids=["smtplib.SMTP", "socket.connect", "httpx.Client.send", "httpx.AsyncClient.send", "urlopen"],
)
def test_send_path_guards_actually_trip(call):
    with no_send_paths():
        with pytest.raises(SendAttempted):
            call()
