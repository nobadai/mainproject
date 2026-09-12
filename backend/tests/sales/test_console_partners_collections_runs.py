"""Sales console read models: run isolation, shared aging, and no invented credit."""

from datetime import UTC, date, datetime
from decimal import Decimal

from app.sales.console_collections import get_console_collections
from app.sales.console_partners import get_console_partner_detail, get_console_partners
from app.sales.console_runs import get_console_sales_runs

AS_OF = date(2026, 1, 9)
RUN_A = "SIM-CONSOLE-A"
RUN_B = "SIM-CONSOLE-B"
PARTNER = "KIMCHI_FACTORY_001"


class _Capture:
    """Answers per run and records what was actually asked of the database."""

    def __init__(self, rows_by_run: dict[str, list[dict]]):
        self.rows_by_run = rows_by_run
        self.queries: list[tuple[str, list]] = []

    def __call__(self, query, params):
        self.queries.append((str(query), list(params)))
        run = next((value for value in params if value in self.rows_by_run), None)
        return list(self.rows_by_run.get(run, []))


def _patch(monkeypatch, module: str, capture) -> None:
    monkeypatch.setattr(f"app.sales.{module}.fetch_all", capture)
    monkeypatch.setattr(f"app.sales.{module}.get_db_schema", lambda: "haetdeul")


# ---------------------------------------------------------------------------
# Partner list
# ---------------------------------------------------------------------------


def _partner_row(suffix: str, *, sales: str, overdue: str) -> dict:
    return {
        "partner_id": PARTNER,
        "partner_name": f"김치공장{suffix}",
        "partner_type": "CUSTOMER",
        "active": True,
        "total_sales_krw": Decimal(sales),
        "total_sales_count": 2,
        "latest_sale_date": AS_OF,
        "receivable_balance_krw": Decimal(500),
        "overdue_balance_krw": Decimal(overdue),
    }


def test_partner_aggregates_never_mix_runs(monkeypatch):
    capture = _Capture(
        {
            RUN_A: [_partner_row("A", sales="1000", overdue="0")],
            RUN_B: [_partner_row("B", sales="2000", overdue="300")],
        }
    )
    _patch(monkeypatch, "console_partners", capture)

    a = get_console_partners(sim_run_id=RUN_A, as_of=AS_OF)
    b = get_console_partners(sim_run_id=RUN_B, as_of=AS_OF)

    assert a.rows[0].total_sales_krw == Decimal(1000)
    assert a.rows[0].overdue_balance_krw == Decimal(0)
    assert b.rows[0].total_sales_krw == Decimal(2000)
    assert b.rows[0].overdue_balance_krw == Decimal(300)
    # 🔴 Both aggregate sub-selects must carry the run, not just one of them.
    statement, params = capture.queries[0]
    assert statement.count("sim_run_id = %s") == 2
    assert params[0] == RUN_A and params[3] == RUN_A


def test_a_partner_with_no_sales_in_this_run_reports_zero_and_no_date(monkeypatch):
    """0 원 매출과 «판매 없음» 은 다른 사실이다 — 금액은 0, 날짜는 null 이다."""
    capture = _Capture(
        {
            RUN_A: [
                {
                    "partner_id": PARTNER,
                    "partner_name": "김치공장",
                    "partner_type": "CUSTOMER",
                    "active": False,
                    "total_sales_krw": Decimal(0),
                    "total_sales_count": 0,
                    "latest_sale_date": None,
                    "receivable_balance_krw": Decimal(0),
                    "overdue_balance_krw": Decimal(0),
                }
            ]
        }
    )
    _patch(monkeypatch, "console_partners", capture)

    row = get_console_partners(sim_run_id=RUN_A, as_of=AS_OF).rows[0]

    assert row.total_sales_krw == Decimal(0)
    assert row.latest_sale_date is None
    assert row.status == "INACTIVE"


def test_partner_filters_reach_sql(monkeypatch):
    capture = _Capture({RUN_A: []})
    _patch(monkeypatch, "console_partners", capture)

    get_console_partners(
        sim_run_id=RUN_A, as_of=AS_OF, query="김치", status="ACTIVE", partner_type="CUSTOMER"
    )
    statement, params = capture.queries[0]

    assert "ILIKE %s" in statement
    assert "p.partner_type = %s" in statement
    assert params[-3:] == ["%김치%", "ACTIVE", "CUSTOMER"]


# ---------------------------------------------------------------------------
# Partner detail
# ---------------------------------------------------------------------------


class _DetailStub:
    """Routes the four detail queries by the table each one names."""

    def __init__(self, *, receivable_outstanding: str = "500"):
        self.receivable_outstanding = receivable_outstanding
        self.queries: list[tuple[str, list]] = []

    def __call__(self, query, params):
        statement = str(query)
        self.queries.append((statement, list(params)))
        if "pricing_contract_type" in statement:
            return [
                {
                    "partner_id": PARTNER,
                    "partner_name": "김치공장",
                    "partner_type": "CUSTOMER",
                    "client_type": "FACTORY",
                    "factory_region": "전남",
                    "sales_collection_days": 30,
                    "pricing_contract_type": "SPOT",
                    "active": True,
                }
            ]
        if "sales_count" in statement:
            return [
                {
                    "total_sales_krw": Decimal(1000),
                    "sales_count": 2,
                    "contribution_profit_krw": Decimal(250),
                    "latest_sale_date": AS_OF,
                }
            ]
        if "GROUP BY i.item_id" in statement:
            return [
                {
                    "item_id": "배추",
                    "quantity_kg": Decimal(100),
                    "sales_amount_krw": Decimal(1000),
                    "contribution_profit_krw": Decimal(250),
                }
            ]
        if "r.receivable_id" in statement:
            return [
                {
                    "receivable_id": "AR-1",
                    "sale_id": "S-1",
                    "due_date": date(2026, 1, 1),
                    "original_amount_krw": Decimal(500),
                    "received_amount_krw": Decimal(0),
                    "outstanding_amount_krw": Decimal(self.receivable_outstanding),
                    "status": "OPEN",
                }
            ]
        return [
            {
                "sale_id": "S-1",
                "sale_date": AS_OF,
                "total_quantity_kg": Decimal(100),
                "total_amount_krw": Decimal(1000),
                "contribution_profit_krw": Decimal(250),
                "item_id": "배추",
                "unit_price_krw_per_kg": Decimal(10),
            }
        ]


def test_partner_detail_summarises_the_stored_rows(monkeypatch):
    stub = _DetailStub()
    _patch(monkeypatch, "console_partners", stub)

    detail = get_console_partner_detail(sim_run_id=RUN_A, as_of=AS_OF, partner_id=PARTNER)

    assert detail is not None
    assert detail.summary.total_sales_krw == Decimal(1000)
    assert detail.summary.sales_count == 2
    assert detail.summary.contribution_margin_rate == Decimal("0.25")
    assert detail.recent_sales[0].sale_id == "S-1"
    assert detail.item_summary[0].item == "배추"
    # 🔴 Aging is Finance's rule; 8 days late lands in the shared 8_30 bucket.
    assert detail.receivables[0].aging_bucket == "8_30"
    assert detail.receivables[0].days_overdue == 8
    assert detail.summary.overdue_balance_krw == Decimal(500)


def test_partner_detail_never_computes_credit(monkeypatch):
    """🔴 `credit_limit - receivables` 를 판매가 세우지 않는다.

    Finance 정본을 안전하게 못 읽으면 UNSUPPORTED 로 둔다.
    """
    _patch(monkeypatch, "console_partners", _DetailStub())

    detail = get_console_partner_detail(sim_run_id=RUN_A, as_of=AS_OF, partner_id=PARTNER)

    assert detail is not None
    assert detail.credit is None
    assert detail.credit_status == "UNSUPPORTED"


def test_a_settled_receivable_does_not_become_an_overdue_balance(monkeypatch):
    _patch(monkeypatch, "console_partners", _DetailStub(receivable_outstanding="0"))

    detail = get_console_partner_detail(sim_run_id=RUN_A, as_of=AS_OF, partner_id=PARTNER)

    assert detail is not None
    assert detail.receivables[0].aging_bucket == "PAID"
    assert detail.receivables[0].days_overdue is None
    assert detail.summary.overdue_balance_krw == Decimal(0)
    assert detail.summary.receivable_balance_krw == Decimal(0)


def test_an_unknown_partner_is_not_invented(monkeypatch):
    _patch(monkeypatch, "console_partners", lambda *_args, **_kwargs: [])

    assert get_console_partner_detail(sim_run_id=RUN_A, as_of=AS_OF, partner_id="NOPE") is None


def test_partner_detail_carries_the_run_into_every_aggregate(monkeypatch):
    stub = _DetailStub()
    _patch(monkeypatch, "console_partners", stub)

    get_console_partner_detail(sim_run_id=RUN_A, as_of=AS_OF, partner_id=PARTNER)

    # The partner master is run-independent; every other query is run-scoped.
    run_scoped = [params for statement, params in stub.queries if "sim_run_id" in statement]
    assert len(run_scoped) == 4
    assert all(params[0] == RUN_A for params in run_scoped)


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


def _collection_row(
    receivable_id: str, *, due: date, outstanding: str, received: str = "0"
) -> dict:
    return {
        "receivable_id": receivable_id,
        "sale_id": "S-1",
        "due_date": due,
        "original_amount_krw": Decimal(1000),
        "received_amount_krw": Decimal(received),
        "outstanding_amount_krw": Decimal(outstanding),
        "status": "OPEN",
        "partner_id": PARTNER,
        "partner_name": "김치공장",
    }


def test_collections_never_mix_runs(monkeypatch):
    capture = _Capture(
        {
            RUN_A: [_collection_row("AR-A", due=AS_OF, outstanding="100")],
            RUN_B: [_collection_row("AR-B", due=AS_OF, outstanding="200")],
        }
    )
    _patch(monkeypatch, "console_collections", capture)

    a = get_console_collections(sim_run_id=RUN_A, as_of=AS_OF)
    b = get_console_collections(sim_run_id=RUN_B, as_of=AS_OF)

    assert a.rows[0].receivable_id == "AR-A"
    assert a.summary.total_outstanding_krw == Decimal(100)
    assert b.rows[0].receivable_id == "AR-B"
    assert b.summary.total_outstanding_krw == Decimal(200)
    assert capture.queries[0][1] == [RUN_A, AS_OF]


def test_collections_use_the_finance_aging_buckets(monkeypatch):
    """🔴 판매 전용 Aging 을 만들지 않는다 — 경계값이 Finance 규칙과 같아야 한다."""
    capture = _Capture(
        {
            RUN_A: [
                _collection_row("CURRENT", due=AS_OF, outstanding="10"),
                _collection_row("LATE-7", due=date(2026, 1, 2), outstanding="20"),
                _collection_row("LATE-30", due=date(2025, 12, 25), outstanding="30"),
                _collection_row("LATE-31", due=date(2025, 11, 1), outstanding="40"),
                _collection_row("DONE", due=AS_OF, outstanding="0", received="1000"),
            ]
        }
    )
    _patch(monkeypatch, "console_collections", capture)

    response = get_console_collections(sim_run_id=RUN_A, as_of=AS_OF)

    assert [row.aging_bucket for row in response.rows] == [
        "CURRENT",
        "1_7",
        "8_30",
        "30_PLUS",
        "PAID",
    ]
    assert response.summary.total_outstanding_krw == Decimal(100)
    assert response.summary.overdue_krw == Decimal(90)
    assert response.summary.collected_krw == Decimal(1000)


def test_a_collection_filter_narrows_rows_without_moving_the_summary(monkeypatch):
    capture = _Capture(
        {
            RUN_A: [
                _collection_row("CURRENT", due=AS_OF, outstanding="10"),
                _collection_row("LATE", due=date(2025, 11, 1), outstanding="40"),
            ]
        }
    )
    _patch(monkeypatch, "console_collections", capture)

    response = get_console_collections(sim_run_id=RUN_A, as_of=AS_OF, aging_bucket="30_PLUS")

    assert [row.receivable_id for row in response.rows] == ["LATE"]
    assert response.summary.total_outstanding_krw == Decimal(50)


# ---------------------------------------------------------------------------
# Sales runs
# ---------------------------------------------------------------------------


def _sales_run_row(run_id: str, *, run: str) -> dict:
    return {
        "run_id": run_id,
        "as_of": AS_OF,
        "runtime_status": "READY",
        "request_payload": {
            "context": {"request_id": f"REQ-{run_id}", "sim_run_id": run},
            "payload": {"user_request": {"partner_id": PARTNER, "item": "배추"}},
        },
        "response_payload": {"payload": {"llm": {"llm_status": "SKIPPED_TEMPLATE"}}},
        "created_at": datetime(2026, 1, 9, 10, 0, tzinfo=UTC),
        "partner_name": "김치공장",
        "master_end_code": "SL1_PRESENTED",
    }


def test_sales_runs_are_scoped_to_the_requested_run(monkeypatch):
    capture = _Capture(
        {RUN_A: [_sales_run_row("R-A", run=RUN_A)], RUN_B: [_sales_run_row("R-B", run=RUN_B)]}
    )
    _patch(monkeypatch, "console_runs", capture)

    a = get_console_sales_runs(sim_run_id=RUN_A)
    b = get_console_sales_runs(sim_run_id=RUN_B)

    assert [row.run_id for row in a.rows] == ["R-A"]
    assert [row.run_id for row in b.rows] == ["R-B"]
    statement, params = capture.queries[0]
    # 🔴 The axis is the one Sales stored in its own context, bound as a parameter.
    assert "request_payload->'context'->>'sim_run_id'" in statement
    assert params[0] == RUN_A


def test_sales_runs_read_only_what_was_stored(monkeypatch):
    capture = _Capture({RUN_A: [_sales_run_row("R-A", run=RUN_A)]})
    _patch(monkeypatch, "console_runs", capture)

    row = get_console_sales_runs(sim_run_id=RUN_A).rows[0]

    assert row.partner_id == PARTNER
    assert row.item == "배추"
    assert row.llm_status == "SKIPPED_TEMPLATE"
    assert row.master_end_code == "SL1_PRESENTED"
    # 🔴 Sales stores no verdict of its own; the column stays null.
    assert row.verdict is None


def test_sales_run_filters_and_order_reach_sql(monkeypatch):
    capture = _Capture({RUN_A: []})
    _patch(monkeypatch, "console_runs", capture)

    get_console_sales_runs(
        sim_run_id=RUN_A, as_of=AS_OF, partner_id=PARTNER, item="배추", runtime_status="READY"
    )
    statement, params = capture.queries[0]

    assert "ORDER BY s.created_at DESC, s.run_id DESC" in statement
    assert params[:5] == [RUN_A, AS_OF, PARTNER, "배추", "READY"]
