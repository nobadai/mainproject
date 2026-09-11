"""판매 흐름 조회 — 저장된 연결키만, 추정 연결은 없다."""

from datetime import UTC, date, datetime
from decimal import Decimal

from app.sales.console_lifecycle import get_console_sale_lifecycle

AS_OF = date(2026, 1, 30)
RUN_A = "SIM-CONSOLE-A"
RUN_B = "SIM-CONSOLE-B"
SALE = "SALE-1"


class _Stub:
    """표 이름으로 갈라 답하고, 무엇을 물었는지 기록한다."""

    def __init__(self, *, run: str = RUN_A, **rows):
        self.run = run
        self.rows = rows
        self.queries: list[tuple[str, list]] = []

    def __call__(self, query, params):
        statement = str(query)
        self.queries.append((statement, list(params)))
        #  🔴 실행 축이 안 맞으면 아무것도 돌려주지 않는다 — 실제 SQL 이 하는 일과 같다.
        if params and params[0] != self.run:
            return []
        for key in ("sale", "reservations", "moves", "receivables", "collections"):
            if key in self.rows and _matches(key, statement):
                return list(self.rows[key])
        return []


def _matches(key: str, statement: str) -> bool:
    return {
        "sale": "collection_due_date" in statement,
        "reservations": "reservation_id" in statement,
        "moves": "inventory_moves" in statement,
        "receivables": "outstanding_amount_krw" in statement and "inventory" not in statement,
        "collections": "master_collection_events" in statement,
    }[key]


def _sale_row() -> dict:
    return {
        "sale_id": SALE,
        "sim_run_id": RUN_A,
        "customer_partner_id": "KIMCHI_FACTORY_001",
        "order_date": date(2026, 1, 1),
        "sale_date": date(2026, 1, 2),
        "collection_due_date": date(2026, 2, 1),
        "total_quantity_kg": Decimal(100),
        "total_amount_krw": Decimal(1000),
        "contribution_profit_krw": Decimal(200),
        "collection_status": "OPEN",
        "order_status": "DELIVERED",
        "source_order_id": "ORD-1",
    }


def _receivable_row(*, outstanding: str = "1000", due: date = date(2026, 2, 1)) -> dict:
    return {
        "receivable_id": "AR-1",
        "issued_date": date(2026, 1, 2),
        "due_date": due,
        "original_amount_krw": Decimal(1000),
        "received_amount_krw": Decimal(0),
        "outstanding_amount_krw": Decimal(outstanding),
        "status": "OPEN",
    }


def _move_row() -> dict:
    return {
        "move_id": "MOVE-1",
        "moved_at": datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
        "quantity_kg": Decimal(100),
        "lot_id": "LOT-1",
        "sale_item_id": "SI-1",
    }


def _patch(monkeypatch, stub) -> None:
    monkeypatch.setattr("app.sales.console_lifecycle.fetch_all", stub)
    monkeypatch.setattr("app.sales.console_lifecycle.get_db_schema", lambda: "haetdeul")


def _stage(lifecycle, name):
    return next(stage for stage in lifecycle.stages if stage.stage == name)


def test_the_confirmed_chain_is_built_from_stored_keys(monkeypatch):
    stub = _Stub(
        sale=[_sale_row()],
        moves=[_move_row()],
        receivables=[_receivable_row()],
        collections=[{"receivable_id": "AR-1", "collection_date": date(2026, 2, 1),
                      "target_received_total_krw": Decimal(1000)}],
    )
    _patch(monkeypatch, stub)

    lifecycle = get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF)

    assert lifecycle is not None
    assert _stage(lifecycle, "sale").status == "DONE"
    assert _stage(lifecycle, "outbound").status == "DONE"
    assert _stage(lifecycle, "receivable").status == "OPEN"
    assert _stage(lifecycle, "collection").status == "DONE"
    # 🔴 출고는 `sale_item_id` 를 거쳐 붙는다 — 날짜나 품목으로 잇지 않는다.
    outbound_sql = next(sql for sql, _ in stub.queries if "inventory_moves" in sql)
    assert "i.sale_item_id = m.sale_item_id" in outbound_sql
    #  날짜·품목으로 잇지 않는다: 조인 조건에 그 축이 없다.
    assert "sale_date" not in outbound_sql
    assert "i.item_id" not in outbound_sql


def test_the_agent_stages_are_blocked_not_guessed(monkeypatch):
    """🔴 후보 → 판매 연결키가 저장돼 있지 않다. 추정으로 잇지 않고 막혔다고 말한다."""
    _patch(monkeypatch, _Stub(sale=[_sale_row()]))

    lifecycle = get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF)

    assert lifecycle is not None
    assert lifecycle.agent_lineage == "BLOCKED"
    for name in ("candidate", "finance_validation", "logistics_validation", "master_decision"):
        stage = _stage(lifecycle, name)
        assert stage.status == "BLOCKED"
        assert stage.reference is None
        assert "추정" in stage.detail


def test_a_missing_stage_is_not_silently_done(monkeypatch):
    _patch(monkeypatch, _Stub(sale=[_sale_row()]))

    lifecycle = get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF)

    assert lifecycle is not None
    assert _stage(lifecycle, "outbound").status == "MISSING"
    assert _stage(lifecycle, "receivable").status == "MISSING"
    assert lifecycle.confirmed_lineage == "PARTIAL"


def test_not_due_and_missing_are_different_facts(monkeypatch):
    """만기 전 «아직 아니다» 와 만기 후 «없다» 를 한 칸에 뭉치지 않는다."""
    early = _Stub(sale=[_sale_row()], receivables=[_receivable_row(due=date(2026, 3, 1))])
    _patch(monkeypatch, early)
    before = get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF)

    late = _Stub(sale=[_sale_row()], receivables=[_receivable_row(due=date(2026, 1, 1))])
    _patch(monkeypatch, late)
    after = get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF)

    assert before is not None and after is not None
    assert _stage(before, "collection").status == "NOT_DUE"
    assert _stage(after, "collection").status == "MISSING"


def test_a_settled_receivable_reads_done(monkeypatch):
    _patch(monkeypatch, _Stub(sale=[_sale_row()], receivables=[_receivable_row(outstanding="0")]))

    lifecycle = get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF)

    assert lifecycle is not None
    assert _stage(lifecycle, "receivable").status == "DONE"


def test_lifecycle_never_crosses_runs(monkeypatch):
    """다른 실행의 같은 판매 번호를 물으면 **없다**고 답한다."""
    _patch(monkeypatch, _Stub(run=RUN_A, sale=[_sale_row()]))

    assert get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF) is not None
    assert get_console_sale_lifecycle(sim_run_id=RUN_B, sale_id=SALE, as_of=AS_OF) is None


def test_every_query_carries_the_run(monkeypatch):
    stub = _Stub(sale=[_sale_row()], receivables=[_receivable_row()])
    _patch(monkeypatch, stub)

    get_console_sale_lifecycle(sim_run_id=RUN_A, sale_id=SALE, as_of=AS_OF)

    for statement, params in stub.queries:
        assert "sim_run_id = %s" in statement
        assert params[0] == RUN_A
