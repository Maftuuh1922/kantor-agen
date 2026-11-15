"""Work pacing ("think time", task 11.5): config, durations, honesty and pause behavior.

No real waiting: the pacer's sleep is a fake that records the requested durations.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings, load_settings
from skeleton.core.events import EventBus
from skeleton.log.activity import recent
from skeleton.office.visual import snapshot
from skeleton.runtime import (
    ActionResult,
    BaseRole,
    BusinessContext,
    FakeLLMClient,
    Heartbeat,
    Manager,
    Pacer,
    Task,
    TaskKind,
    set_agent_paused,
    set_business_paused,
    settings_with_pacing,
)
from skeleton.runtime.pacing import HANDOFF_WALK_SECONDS


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    def publish(self, event):
        self.events.append(dict(event))
        super().publish(event)

    def states(self, agent_id):
        return [e["state"] for e in self.events if e["type"] == "agent_state" and e["agent_id"] == agent_id]


class FakeSleep:
    """Records each requested duration and does not wait. ``hook`` runs on every call."""

    def __init__(self, hook=None):
        self.calls: list[float] = []
        self.hook = hook

    async def __call__(self, seconds):
        self.calls.append(seconds)
        if self.hook is not None:
            self.hook(len(self.calls), seconds)
        await asyncio.sleep(0)

    @property
    def total(self):
        return sum(self.calls)


class KindRole(BaseRole):
    """Emits one task of a given kind per tick for its agent."""

    def __init__(self, agent_id, kind=TaskKind.READ, handoff_to=None):
        self.agent_id = agent_id
        self.kind = kind
        self.handoff_to = handoff_to
        self.name = f"agent{agent_id}"
        self.runs: list[Task] = []

    def triggers(self, ctx):
        payload = {} if self.handoff_to is None else {"handoff_to": self.handoff_to}
        cost = 1000 if self.kind == TaskKind.SPEND else 0
        return [Task(self.agent_id, self.kind, payload, cost, f"{self.name} {self.kind}")]

    def run(self, task, ctx):
        self.runs.append(task)
        return ActionResult(status="done", message="ok")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    for agent_id in (1, 2, 3):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, 'r', 'A', ?)",
            (agent_id, agent_id),
        )
    yield c
    c.close()


def make_ctx(conn, clock=None):
    return BusinessContext(conn=conn, business_id=1, clock=clock or FakeClock(), settings=Settings())


def build(conn, roles, sleep, min_s=3.0, max_s=8.0, bus=None, llm=None, clock=None, pacer="default"):
    clock = clock or FakeClock()
    settings = Settings(heartbeat_seconds=7)
    if pacer == "default":
        pacer = Pacer(min_s, max_s, sleep=sleep)
    manager = Manager(conn, 1, clock, llm or FakeLLMClient(), settings, roles, bus=bus, pacer=pacer)
    return Heartbeat(conn, 1, clock, manager, roles, settings, bus=bus)


def task_of(kind, agent_id=1, reason="check", **payload):
    cost = 500 if kind == TaskKind.SPEND else 0
    return Task(agent_id, kind, dict(payload), cost, reason)


# ---------------------------------------------------------------------- #
# Config
# ---------------------------------------------------------------------- #
def test_pacing_defaults_are_off_with_3_to_8_seconds():
    s = Settings()
    assert (s.pacing_enabled, s.pacing_min_seconds, s.pacing_max_seconds) == (False, 3.0, 8.0)
    assert load_settings({}, dotenv_path="/nonexistent/.env").pacing_enabled is False
    assert s.public_dict()["pacing_enabled"] is False


def test_pacing_default_argument_applies_only_without_env_var():
    on = load_settings({}, dotenv_path="/nonexistent/.env", pacing_default=True)
    assert on.pacing_enabled is True
    off = load_settings({"KANTOR_PACING": "0"}, dotenv_path="/nonexistent/.env", pacing_default=True)
    assert off.pacing_enabled is False


def test_pacing_env_vars_are_read():
    env = {"KANTOR_PACING": "Yes", "KANTOR_PACING_MIN": "1.5", "KANTOR_PACING_MAX": "4"}
    s = load_settings(env, dotenv_path="/nonexistent/.env")
    assert (s.pacing_enabled, s.pacing_min_seconds, s.pacing_max_seconds) == (True, 1.5, 4.0)


@pytest.mark.parametrize(
    "env",
    [
        {"KANTOR_PACING": "maybe"},
        {"KANTOR_PACING_MIN": "fast"},
        {"KANTOR_PACING_MIN": "9", "KANTOR_PACING_MAX": "8"},
        {"KANTOR_PACING_MIN": "-1"},
        {"KANTOR_PACING_MAX": "61"},
        {"KANTOR_PACING_MAX": "nan"},
    ],
)
def test_invalid_pacing_env_is_rejected(env):
    with pytest.raises(ValueError):
        load_settings(env, dotenv_path="/nonexistent/.env")


@pytest.mark.parametrize("low,high", [(5, 4), (-0.1, 4), (0, 60.5), (True, 4), ("1", 4), (float("nan"), 4)])
def test_invalid_pacing_ranges_are_rejected(low, high):
    with pytest.raises(ValueError):
        Settings(pacing_min_seconds=low, pacing_max_seconds=high)
    with pytest.raises(ValueError):
        Pacer(low, high)


def test_boundary_ranges_are_accepted():
    Settings(pacing_min_seconds=0, pacing_max_seconds=0)
    Settings(pacing_min_seconds=60, pacing_max_seconds=60)
    with pytest.raises(ValueError):
        Settings(pacing_enabled="yes")


def test_settings_with_pacing_helper():
    base = Settings()
    on = settings_with_pacing(base)
    assert on.pacing_enabled and (on.pacing_min_seconds, on.pacing_max_seconds) == (3.0, 8.0)
    ranged = settings_with_pacing(base, enabled=True, min=1, max=2)
    assert (ranged.pacing_min_seconds, ranged.pacing_max_seconds) == (1, 2)
    assert settings_with_pacing(ranged, enabled=False).pacing_enabled is False
    assert base.pacing_enabled is False  # the original is not changed
    with pytest.raises(ValueError):
        settings_with_pacing(base, min=10, max=2)


def test_pacer_from_settings_is_none_when_off():
    assert Pacer.from_settings(Settings()) is None
    pacer = Pacer.from_settings(Settings(pacing_enabled=True, pacing_min_seconds=1, pacing_max_seconds=2))
    assert (pacer.min_seconds, pacer.max_seconds) == (1.0, 2.0)


# ---------------------------------------------------------------------- #
# Duration
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("low,high", [(3.0, 8.0), (0.0, 60.0), (2.0, 4.0), (5.0, 5.0)])
def test_duration_is_deterministic_and_inside_its_band(conn, low, high):
    pacer, ctx = Pacer(low, high), make_ctx(conn)
    span = high - low
    bands = {
        TaskKind.READ: (0.0, 0.6),
        TaskKind.SPEND: (0.6, 0.85),
        TaskKind.DRAFT: (0.6, 0.85),
        TaskKind.CUSTOMER_MESSAGE: (0.85, 1.0),
    }
    for kind, (lo_f, hi_f) in bands.items():
        for agent_id in range(1, 9):
            for n in range(10):
                t = task_of(kind, agent_id, reason=f"reason {n}")
                d = pacer.duration(t, ctx)
                assert d == pacer.duration(t, ctx)  # same inputs, same answer
                assert low + span * lo_f - 1e-9 <= d <= low + span * hi_f + 1e-9
                assert low <= d <= high


def test_duration_ordering_read_then_spend_draft_then_message(conn):
    pacer, ctx = Pacer(3.0, 8.0), make_ctx(conn)
    for n in range(40):
        reason = f"case {n}"
        read = pacer.duration(task_of(TaskKind.READ, 2, reason), ctx)
        spend = pacer.duration(task_of(TaskKind.SPEND, 2, reason), ctx)
        draft = pacer.duration(task_of(TaskKind.DRAFT, 2, reason), ctx)
        message = pacer.duration(task_of(TaskKind.CUSTOMER_MESSAGE, 2, reason), ctx)
        assert read < spend and read < draft
        assert spend < message and draft < message


def test_duration_varies_with_inputs_and_day_but_not_with_time_of_day(conn):
    pacer = Pacer(3.0, 8.0)
    t = task_of(TaskKind.READ, 1, "stock check")
    day1 = make_ctx(conn, FakeClock(datetime(2025, 3, 1, 1, 0, tzinfo=timezone.utc)))
    same_day_later = make_ctx(conn, FakeClock(datetime(2025, 3, 1, 9, 0, tzinfo=timezone.utc)))
    day2 = make_ctx(conn, FakeClock(datetime(2025, 3, 2, 1, 0, tzinfo=timezone.utc)))
    assert pacer.duration(t, day1) == pacer.duration(t, same_day_later)
    values = {pacer.duration(task_of(TaskKind.READ, a, f"r{n}"), day1) for a in (1, 2, 3) for n in range(5)}
    assert len(values) > 5  # not one constant
    assert any(
        pacer.duration(task_of(TaskKind.READ, 1, f"r{n}"), day1)
        != pacer.duration(task_of(TaskKind.READ, 1, f"r{n}"), day2)
        for n in range(10)
    )


def test_handoff_task_adds_the_walk_time(conn):
    pacer, ctx = Pacer(3.0, 8.0), make_ctx(conn)
    plain = pacer.duration(task_of(TaskKind.READ, 1, "x"), ctx)
    walking = pacer.duration(task_of(TaskKind.READ, 1, "x", handoff_to=2), ctx)
    assert walking == pytest.approx(plain + HANDOFF_WALK_SECONDS)
    ignored = pacer.duration(task_of(TaskKind.READ, 1, "x", handoff_to="2"), ctx)  # not a valid target
    assert ignored == plain


def test_zero_range_means_no_wait_even_for_a_handoff(conn):
    pacer, ctx = Pacer(0, 0), make_ctx(conn)
    assert pacer.duration(task_of(TaskKind.CUSTOMER_MESSAGE, 1, "x", handoff_to=2), ctx) == 0


async def test_wait_sleeps_in_poll_steps_that_sum_to_the_duration(conn):
    fake = FakeSleep()
    pacer, ctx = Pacer(3.0, 8.0, sleep=fake), make_ctx(conn)
    t = task_of(TaskKind.SPEND, 1, "pay supplier")
    waited = await pacer.wait(t, None, ctx)
    assert waited == pytest.approx(pacer.duration(t, ctx))
    assert fake.total == pytest.approx(pacer.duration(t, ctx))
    assert all(0 < c <= 0.25 + 1e-9 for c in fake.calls)


# ---------------------------------------------------------------------- #
# Honesty: the task is running for the whole wait (Req 2.8)
# ---------------------------------------------------------------------- #
async def test_task_is_reported_running_for_the_whole_wait_and_not_outside_it(conn):
    bus = RecordingBus()
    observed: list[tuple] = []
    box: dict = {}

    def hook(n, seconds):
        view = {e["agent_id"]: e["state"] for e in snapshot(conn, 1, box["hb"], FakeClock())}
        observed.append((1 in box["hb"].running, bus.states(1)[-1], view[1]))

    fake = FakeSleep(hook)
    role = KindRole(1)
    hb = build(conn, {1: role}, fake, bus=bus)
    box["hb"] = hb

    assert hb.running == {} and bus.states(1) == []  # before
    results = await hb.tick()

    assert [r.status for r in results] == ["done"]
    assert len(fake.calls) > 10  # a real wait happened
    assert set(observed) == {(True, "working", "working")}  # every step of the wait
    assert bus.states(1) == ["working", "idle"]  # working is published once, before the wait
    assert hb.running == {}  # after
    # The role ran after the wait and its row was written after it too.
    assert len(role.runs) == 1
    assert [r["status"] for r in recent(conn, 1)] == ["done"]


async def test_activity_row_and_role_run_come_after_the_wait(conn):
    seen = {}

    def hook(n, seconds):
        seen["rows_during_wait"] = len(recent(conn, 1))
        seen["runs_during_wait"] = len(role.runs)

    role = KindRole(1)
    hb = build(conn, {1: role}, FakeSleep(hook))
    await hb.tick()
    assert seen == {"rows_during_wait": 0, "runs_during_wait": 0}
    assert len(role.runs) == 1 and len(recent(conn, 1)) == 1


async def test_handoff_event_is_published_before_the_walk_wait(conn):
    bus = RecordingBus()
    types_at_first_sleep: list[list[str]] = []

    def hook(n, seconds):
        if n == 1:
            types_at_first_sleep.append([e["type"] for e in bus.events])

    fake = FakeSleep(hook)
    receiver = KindRole(2)
    hb = build(conn, {1: KindRole(1, handoff_to=2), 2: receiver}, fake, bus=bus)
    # Only agent 1 has a trigger in this tick: use a role for agent 2 that makes none.
    receiver.triggers = lambda ctx: []

    await hb.tick()

    (types,) = types_at_first_sleep
    assert types[0] == "agent_state" and "handoff" in types  # working first, handoff before the walk
    assert fake.total >= HANDOFF_WALK_SECONDS + 3.0
    assert len(receiver.runs) == 1 and receiver.runs[0].agent_id == 2
    assert bus.states(1) == ["working", "idle"]


async def test_failures_are_not_hidden_by_pacing(conn):
    class Boom(KindRole):
        def run(self, task, ctx):
            raise RuntimeError("kaboom")

    fake = FakeSleep()
    hb = build(conn, {1: Boom(1)}, fake)
    results = await hb.tick()
    assert results[0].status == "failed" and "kaboom" in results[0].message
    assert [r["status"] for r in recent(conn, 1)] == ["failed"]
    assert fake.total > 0


async def test_a_pacer_that_raises_does_not_fail_the_task(conn):
    class BrokenPacer(Pacer):
        async def wait(self, *args, **kwargs):
            raise RuntimeError("pacer broke")

    role = KindRole(1)
    hb = build(conn, {1: role}, None, pacer=BrokenPacer(1, 2))
    results = await hb.tick()
    assert [r.status for r in results] == ["done"] and len(role.runs) == 1


async def test_no_role_task_is_not_paced(conn):
    fake = FakeSleep()
    manager = Manager(conn, 1, FakeClock(), FakeLLMClient(), Settings(), {}, pacer=Pacer(3, 8, sleep=fake))
    result = await manager.handle(task_of(TaskKind.READ, 1))
    assert result.status == "failed" and fake.calls == []


# ---------------------------------------------------------------------- #
# Off by default
# ---------------------------------------------------------------------- #
async def test_pacing_disabled_makes_no_sleeps(conn, monkeypatch):
    calls = []
    real_sleep = asyncio.sleep

    async def spy(seconds, *a, **k):
        calls.append(seconds)
        await real_sleep(0)

    role = KindRole(1, TaskKind.CUSTOMER_MESSAGE)
    settings = Settings(pacing_enabled=False)
    manager = Manager(conn, 1, FakeClock(), FakeLLMClient(), settings, {1: role})
    hb = Heartbeat(conn, 1, FakeClock(), manager, {1: role}, settings)
    monkeypatch.setattr(asyncio, "sleep", spy)

    await hb.tick()

    assert manager._pacer is None
    assert calls == [] and len(role.runs) == 1


async def test_enabled_settings_make_the_manager_pace_by_itself(conn):
    settings = settings_with_pacing(Settings(), True, min=0.5, max=1.0)
    manager = Manager(conn, 1, FakeClock(), FakeLLMClient(), settings, {1: KindRole(1)})
    assert isinstance(manager._pacer, Pacer)
    assert (manager._pacer.min_seconds, manager._pacer.max_seconds) == (0.5, 1.0)


# ---------------------------------------------------------------------- #
# Pause (Req 8.3)
# ---------------------------------------------------------------------- #
async def test_business_pause_during_wait_ends_it_early_and_starts_no_later_task(conn):
    roles = {i: KindRole(i, TaskKind.CUSTOMER_MESSAGE) for i in (1, 2, 3)}
    full = Pacer(3.0, 8.0).duration(roles[1].triggers(None)[0], make_ctx(conn))

    def hook(n, seconds):
        if n == 4:
            set_business_paused(conn, 1, True)

    fake = FakeSleep(hook)
    hb = build(conn, roles, fake)

    results = await hb.tick()

    assert len(fake.calls) == 4 and fake.total < full  # cut short, within one poll step
    assert len(results) == 1 and results[0].status in ("done", "rejected", "pending", "info")
    assert len(roles[1].runs) + len(recent(conn, 1)) >= 1  # the running task finished its step
    assert all(not roles[i].runs for i in (2, 3))
    assert hb.running == {}


async def test_agent_pause_during_wait_ends_it_early(conn):
    roles = {i: KindRole(i) for i in (1, 2)}

    def hook(n, seconds):
        if n == 2:
            set_agent_paused(conn, 1, True)

    fake = FakeSleep(hook)
    hb = build(conn, roles, fake)
    results = await hb.tick()

    assert len(roles[1].runs) == 1  # finished its step
    assert roles[1].runs and results[0].status == "done"
    assert fake.calls[:2] == [0.25, 0.25]
    # Agent 2 still runs, and gets its full wait.
    assert len(roles[2].runs) == 1
    assert fake.total > 3.0


async def test_pausing_the_agent_that_handed_over_also_ends_the_walk(conn):
    def hook(n, seconds):
        if n == 3:
            set_agent_paused(conn, 1, True)

    fake = FakeSleep(hook)
    receiver = KindRole(2)
    receiver.triggers = lambda ctx: []
    hb = build(conn, {1: KindRole(1, handoff_to=2), 2: receiver}, fake)

    await hb.tick()

    assert len(fake.calls) == 3
    assert len(receiver.runs) == 1  # the task still finished its step


async def test_paused_before_the_wait_means_no_sleep(conn):
    fake = FakeSleep()
    ctx = make_ctx(conn)
    set_business_paused(conn, 1, True)
    waited = await Pacer(3, 8, sleep=fake).wait(task_of(TaskKind.READ), None, ctx)
    assert waited == 0 and fake.calls == []


async def test_stopping_the_heartbeat_during_a_wait_clears_running(conn):
    gate = asyncio.Event()

    async def blocking_sleep(seconds):
        gate.set()
        await asyncio.Event().wait()

    hb = build(conn, {1: KindRole(1)}, blocking_sleep)
    hb.start()
    await asyncio.wait_for(gate.wait(), 5)
    assert 1 in hb.running
    await hb.stop()
    assert hb.running == {}


# ---------------------------------------------------------------------- #
# Ticks never overlap, even when a tick is longer than the interval
# ---------------------------------------------------------------------- #
async def test_run_forever_never_overlaps_ticks_when_a_tick_outlasts_the_interval(conn):
    active = {"now": 0, "max": 0, "done": 0}
    reached = asyncio.Event()
    interval_sleeps: list[float] = []

    async def slow_pacing_sleep(seconds):
        await asyncio.sleep(0.01)  # a tick lasts many such steps: far more than the "interval"

    async def interval_sleep(seconds):
        interval_sleeps.append(seconds)
        await asyncio.sleep(0)  # an interval that is tiny compared to a tick
        if active["done"] >= 3:
            reached.set()
            await asyncio.Event().wait()

    roles = {i: KindRole(i, TaskKind.CUSTOMER_MESSAGE) for i in (1, 2)}
    settings = Settings(heartbeat_seconds=1)
    manager = Manager(conn, 1, FakeClock(), FakeLLMClient(), settings, roles, pacer=Pacer(0.5, 0.5, sleep=slow_pacing_sleep))
    hb = Heartbeat(conn, 1, FakeClock(), manager, roles, settings, sleep=interval_sleep)
    original = hb.tick

    async def counting_tick():
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        try:
            return await original()
        finally:
            active["now"] -= 1
            active["done"] += 1

    hb.tick = counting_tick
    hb.start()
    await asyncio.wait_for(reached.wait(), 10)
    await hb.stop()

    assert active["max"] == 1
    assert active["done"] >= 3
    assert interval_sleeps and set(interval_sleeps) == {1}


async def test_two_ticks_started_together_run_one_after_the_other(conn):
    active = {"now": 0, "max": 0}

    async def sleep(seconds):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0)
        active["now"] -= 1

    roles = {1: KindRole(1)}
    hb = build(conn, roles, sleep, min_s=1, max_s=1)
    first, second = await asyncio.gather(hb.tick(), hb.tick())
    assert len(first) == 1 and len(second) == 1
    assert active["max"] == 1 and len(roles[1].runs) == 2


# ---------------------------------------------------------------------- #
# Cost: no extra model calls
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", [TaskKind.READ, TaskKind.SPEND, TaskKind.DRAFT, TaskKind.CUSTOMER_MESSAGE])
async def test_pacing_makes_no_extra_model_calls(kind):
    async def run(paced):
        c = db.connect(":memory:")
        db.init_schema(c)
        c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
        c.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (1, 1, 'r', 'A', 1)")
        llm = FakeLLMClient()
        pacer = Pacer(3, 8, sleep=FakeSleep()) if paced else None
        hb = build(c, {1: KindRole(1, kind)}, None, llm=llm, pacer=pacer)
        await hb.tick()
        rows = [(r["action"], r["status"]) for r in recent(c, 1)]
        c.close()
        return len(llm.calls), rows

    assert await run(True) == await run(False)


# ---------------------------------------------------------------------- #
# The live Warung app: flags, env, wiring and the API field
# ---------------------------------------------------------------------- #
def _captured_warung_settings(monkeypatch, tmp_path, argv, env=None):
    from apps.kantor_warung import app as warung_app

    for name in ("KANTOR_PACING", "KANTOR_PACING_MIN", "KANTOR_PACING_MAX"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)  # no stray .env
    seen = {}
    real = warung_app.create_warung_runtime

    def spy(db_path, settings=None, **kw):
        seen["settings"] = settings
        return real(db_path, settings=settings, **kw)

    monkeypatch.setattr(warung_app, "create_warung_runtime", spy)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(app=app))
    warung_app.main(argv)
    return seen["settings"]


def test_warung_pacing_is_on_by_default_with_3_to_8(monkeypatch, tmp_path):
    s = _captured_warung_settings(monkeypatch, tmp_path, [])
    assert (s.pacing_enabled, s.pacing_min_seconds, s.pacing_max_seconds) == (True, 3.0, 8.0)


def test_warung_no_pacing_flag_turns_it_off(monkeypatch, tmp_path):
    assert _captured_warung_settings(monkeypatch, tmp_path, ["--no-pacing"]).pacing_enabled is False


def test_warung_env_can_turn_pacing_off_and_pace_flags_set_the_range(monkeypatch, tmp_path):
    assert _captured_warung_settings(monkeypatch, tmp_path, [], {"KANTOR_PACING": "0"}).pacing_enabled is False
    s = _captured_warung_settings(monkeypatch, tmp_path, ["--pace-min", "2", "--pace-max", "4"])
    assert (s.pacing_enabled, s.pacing_min_seconds, s.pacing_max_seconds) == (True, 2.0, 4.0)


@pytest.mark.parametrize("argv", [["--pace-min", "5", "--pace-max", "2"], ["--pace-max", "61"], ["--pace-min", "-1"]])
def test_warung_bad_pace_range_stops_the_start(argv, monkeypatch, tmp_path):
    from apps.kantor_warung import app as warung_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KANTOR_PACING_MIN", raising=False)
    monkeypatch.delenv("KANTOR_PACING_MAX", raising=False)
    with pytest.raises(SystemExit, match="Cannot start Kantor Warung"):
        warung_app.main(argv)


async def test_office_api_reports_pacing_enabled_and_no_secret(tmp_path):
    import httpx

    from apps.kantor_warung.app import create_warung_runtime
    from skeleton.office import create_app

    for enabled in (False, True):
        settings = settings_with_pacing(Settings(llm_api_key="sk-SECRET-pacing"), enabled)
        rt = create_warung_runtime(str(tmp_path / f"w{enabled}.db"), settings=settings, autostart=False)
        app = create_app(rt)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://w") as c:
            assert (await c.post("/api/onboarding", json={"name": "Warung", "type": "warung"})).status_code == 200
            body = (await c.get("/api/office")).json()
        assert body["pacing_enabled"] is enabled
        assert "sk-SECRET-pacing" not in str(body)
        assert isinstance(rt.ctx.manager._pacer, Pacer) is enabled
        rt.ctx.conn.close()
