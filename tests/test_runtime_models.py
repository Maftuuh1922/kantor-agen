import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.runtime import (
    ActionResult,
    BaseRole,
    BusinessContext,
    Role,
    RoleRegistry,
    Task,
    TaskKind,
)


def make_task(**overrides):
    fields = dict(agent_id=1, kind="read", payload={}, cost_idr=0, reason="check stock")
    fields.update(overrides)
    return Task(**fields)


@pytest.fixture
def ctx():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    yield BusinessContext(conn=conn, business_id=1, clock=FakeClock())
    conn.close()


# --- Task validation ---------------------------------------------------------

def test_task_kind_values_are_plain_strings():
    assert TaskKind.ALL == {"read", "draft", "spend", "customer_message"}
    assert TaskKind.SPEND == "spend" and type(TaskKind.SPEND) is str


@pytest.mark.parametrize("kind", sorted(TaskKind.ALL - {"spend"}))
def test_valid_non_spend_task(kind):
    task = make_task(kind=kind)
    assert task.cost_idr == 0 and task.rule_only is False


def test_valid_spend_task():
    assert make_task(kind="spend", cost_idr=50_000).cost_idr == 50_000


def test_zero_cost_spend_is_allowed():
    assert make_task(kind="spend", cost_idr=0).cost_idr == 0


def test_rule_only_can_be_set():
    task = make_task()
    task.rule_only = True
    assert task.rule_only is True


def test_bad_kind_rejected():
    with pytest.raises(ValueError, match="kind"):
        make_task(kind="delete_everything")


def test_negative_cost_rejected():
    with pytest.raises(ValueError, match="negative"):
        make_task(kind="spend", cost_idr=-1)


@pytest.mark.parametrize("bad", [True, False, 1.0, 100.5, "100", None])
def test_non_int_cost_rejected(bad):
    with pytest.raises(ValueError, match="int"):
        make_task(kind="spend", cost_idr=bad)


def test_decimal_cost_rejected():
    from decimal import Decimal

    with pytest.raises(ValueError, match="int"):
        make_task(kind="spend", cost_idr=Decimal("100"))


@pytest.mark.parametrize("kind", sorted(TaskKind.ALL - {"spend"}))
def test_non_spend_task_with_cost_rejected(kind):
    with pytest.raises(ValueError, match="spend"):
        make_task(kind=kind, cost_idr=1000)


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_empty_reason_rejected(reason):
    with pytest.raises(ValueError, match="reason"):
        make_task(reason=reason)


# --- ActionResult ------------------------------------------------------------

@pytest.mark.parametrize("status", ["done", "rejected", "pending", "failed", "info"])
def test_action_result_valid_status(status):
    result = ActionResult(status=status, message="ok")
    assert result.cost_idr == 0 and result.data is None


def test_action_result_unknown_status_rejected():
    with pytest.raises(ValueError, match="status"):
        ActionResult(status="maybe", message="?")


def test_action_result_cost_must_be_non_negative_int():
    with pytest.raises(ValueError):
        ActionResult(status="done", message="x", cost_idr=-5)
    with pytest.raises(ValueError):
        ActionResult(status="done", message="x", cost_idr=True)


# --- BaseRole ----------------------------------------------------------------

class IdleRole(BaseRole):
    name = "idle"


def test_base_role_defaults(ctx):
    role = IdleRole()
    assert role.triggers(ctx) == []
    result = role.run(make_task(), ctx)
    assert result.status == "info"
    assert IdleRole.needs_llm_kinds == frozenset()
    assert isinstance(role, Role)


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("triggers must not call the model")


class StockRole(BaseRole):
    """Sample rule role: propose a read task for each product under min_qty."""

    name = "stock"
    needs_llm_kinds = frozenset({"draft"})

    def __init__(self, llm=None):
        self.llm = llm

    def triggers(self, ctx):
        rows = ctx.conn.execute(
            "SELECT id, name FROM product WHERE business_id = ? AND qty < min_qty",
            (ctx.business_id,),
        ).fetchall()
        return [
            Task(
                agent_id=1,
                kind=TaskKind.READ,
                payload={"product_id": r["id"]},
                cost_idr=0,
                reason=f"{r['name']} is below minimum stock",
            )
            for r in rows
        ]


def test_triggers_use_rules_and_make_no_llm_call(ctx):
    ctx.conn.execute(
        "INSERT INTO product (business_id, name, qty, min_qty, unit_price_idr) VALUES (1, 'Teh', 2, 5, 3000)"
    )
    ctx.conn.execute(
        "INSERT INTO product (business_id, name, qty, min_qty, unit_price_idr) VALUES (1, 'Kopi', 9, 5, 4000)"
    )
    llm = FakeLLM()
    role = StockRole(llm=llm)

    tasks = role.triggers(ctx)

    assert llm.calls == 0
    assert [t.reason for t in tasks] == ["Teh is below minimum stock"]
    assert all(t.kind == "read" for t in tasks)
    assert StockRole.needs_llm_kinds == {"draft"}


def test_triggers_empty_when_condition_not_met(ctx):
    assert StockRole(llm=FakeLLM()).triggers(ctx) == []


# --- RoleRegistry ------------------------------------------------------------

def test_registry_register_create_names():
    registry = RoleRegistry()
    registry.register(StockRole)
    registry.register(IdleRole)
    assert registry.names() == ["idle", "stock"]
    assert isinstance(registry.create("stock"), StockRole)
    assert registry.create("stock") is not registry.create("stock")


def test_registry_unknown_name_raises_clear_keyerror():
    registry = RoleRegistry()
    registry.register(IdleRole)
    with pytest.raises(KeyError, match="unknown role 'ghost'.*idle"):
        registry.create("ghost")


def test_registry_rejects_duplicate_and_nameless():
    registry = RoleRegistry()
    registry.register(IdleRole)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(IdleRole)
    with pytest.raises(ValueError, match="name"):
        registry.register(BaseRole)


def test_registry_explicit_name():
    registry = RoleRegistry()
    registry.register(IdleRole, name="alias")
    assert registry.names() == ["alias"]
