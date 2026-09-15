"""같은 1,000만원 여신을 **7일 수금으로 더 빨리 돌리는** 계약.

★ 이 파일이 지키는 것:

```text
7일 결제는 재무 허용 최대 결제일(30일) 안이라 결제 규칙을 통과한다
여신 판정 경계는 그대로다 — 한도를 늘리거나 되돌려 주지 않는다
한도가 모자라면 «먼저 받아야 할 미수금» 을 재무가 센다 (수금을 만들지는 않는다)
사용률 · 예상 회복일은 표시용 사실이고 판정에 들어가지 않는다
여신은 **수금이 실제로 기록돼 미수가 줄 때만** 풀린다
```
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.finance import console_credit
from app.finance.capabilities.sales import (
    _summary_payload,
    evaluate_receivable_capacity,
)
from app.finance.collection import build_collection_transition
from app.finance.rules import evaluate_sales_payment_term_rule
from app.finance.sales_policy import load_finance_sales_mvp_policy
from app.finance.sales_validation import (
    OpenReceivableDue,
    PartnerReceivable,
    SalesFinancialSummary,
)
from app.finance.tools import (
    calculate_available_credit,
    calculate_credit_utilization_rate,
    estimate_credit_recovery_date,
    summarize_partner_receivables,
)

AS_OF = date(2026, 1, 8)
LIMIT = Decimal(10_000_000)


def _receivable(ref: str, amount: int, due: date, status: str = "OPEN") -> PartnerReceivable:
    return PartnerReceivable(
        receivable_id=ref,
        due_date=due,
        outstanding_amount_krw=Decimal(amount),
        status=status,
        source_ref=ref,
    )


def _facts(*receivables: PartnerReceivable):
    return summarize_partner_receivables(
        partner_id="KIMCHI_FACTORY_001", as_of=AS_OF, receivables=list(receivables)
    )


def _capacity(current_ar: int, sale: int):
    facts = (
        _facts(_receivable("AR-1", current_ar, AS_OF + timedelta(days=5)))
        if current_ar
        else _facts()
    )
    return evaluate_receivable_capacity(
        sales_amount_krw=Decimal(sale), receivable_facts=facts, credit_limit_krw=LIMIT
    )


# ── 7일 결제 ──────────────────────────────────────────────────────────────


def test_seven_day_payment_passes_the_existing_payment_term_rule():
    """재무 허용 최대치를 7일로 줄이지 않는다. 7일 계약은 30일 상한 안에서 통과한다."""
    policy = load_finance_sales_mvp_policy()
    result = evaluate_sales_payment_term_rule(
        payment_terms_type="SINGLE",
        payment_days=7,
        max_finance_allowed_payment_terms_days=policy.max_finance_allowed_payment_terms_days,
    )

    assert policy.max_finance_allowed_payment_terms_days == 30
    assert result["verdict"] == "PASS"


# ── 여신 경계 (1,000만원 유지) ─────────────────────────────────────────────


def test_empty_ledger_and_a_three_million_sale_passes():
    credit = _capacity(0, 3_000_000)

    assert credit["rule"]["verdict"] == "PASS"
    assert credit["current_partner_ar_krw"] == Decimal(0)
    assert credit["required_collection_before_sale_krw"] == Decimal(0)


def test_eight_million_open_and_a_two_million_sale_is_exactly_the_limit():
    credit = _capacity(8_000_000, 2_000_000)

    assert credit["rule"]["verdict"] == "PASS"
    assert credit["projected_partner_ar_krw"] == LIMIT
    assert credit["required_collection_before_sale_krw"] == Decimal(0)


def test_one_won_over_the_limit_fails_and_names_the_one_won():
    credit = _capacity(8_000_000, 2_000_001)

    assert credit["rule"]["verdict"] == "FAIL"
    assert credit["rule"]["reason_codes"] == ("SALES_CREDIT_LIMIT_EXCEEDED",)
    assert credit["required_collection_before_sale_krw"] == Decimal(1)


def test_required_collection_is_projected_minus_limit():
    credit = _capacity(8_000_000, 5_000_000)

    assert credit["projected_partner_ar_krw"] == Decimal(13_000_000)
    assert credit["available_credit_krw"] == Decimal(2_000_000)
    assert credit["required_collection_before_sale_krw"] == Decimal(3_000_000)
    assert credit["rule"]["verdict"] == "FAIL"


def test_zero_required_collection_is_a_fact_not_missing():
    """🔴 0 은 «더 받을 필요 없음» 이다. 여신 사실이 없을 때만 `None` 이다."""
    assert _capacity(1_000_000, 1_000_000)["required_collection_before_sale_krw"] == Decimal(0)

    unknown = evaluate_receivable_capacity(
        sales_amount_krw=Decimal(1_000_000), receivable_facts=None, credit_limit_krw=LIMIT
    )
    assert unknown["required_collection_before_sale_krw"] is None
    assert unknown["credit_utilization_rate"] is None
    assert unknown["expected_credit_recovery_date"] is None


def test_the_required_collection_does_not_move_the_ledger():
    """🔴 선회수 필요액을 셌다고 미수가 줄지 않는다 — 안내이지 수금이 아니다."""
    facts = _facts(_receivable("AR-1", 8_000_000, AS_OF + timedelta(days=5)))
    evaluate_receivable_capacity(
        sales_amount_krw=Decimal(5_000_000), receivable_facts=facts, credit_limit_krw=LIMIT
    )

    assert facts.current_ar_krw == Decimal(8_000_000)
    assert facts.open_receivable_schedule[0].outstanding_amount_krw == Decimal(8_000_000)


# ── 사용률 · 예상 회복일 (표시용) ─────────────────────────────────────────


def test_utilization_is_current_ar_over_limit():
    assert calculate_credit_utilization_rate(
        current_partner_ar_krw=Decimal(6_500_000), credit_limit_krw=LIMIT
    ) == Decimal("0.65")


def test_utilization_over_a_zero_limit_is_not_invented():
    assert (
        calculate_credit_utilization_rate(
            current_partner_ar_krw=Decimal(0), credit_limit_krw=Decimal(0)
        )
        is None
    )
    with pytest.raises(ValueError):
        calculate_credit_utilization_rate(
            current_partner_ar_krw=Decimal(-1), credit_limit_krw=LIMIT
        )


def test_recovery_date_is_the_first_due_date_that_covers_the_need():
    schedule = [
        OpenReceivableDue(due_date=date(2026, 1, 12), outstanding_amount_krw=Decimal(1_000_000)),
        OpenReceivableDue(due_date=date(2026, 1, 15), outstanding_amount_krw=Decimal(2_500_000)),
        OpenReceivableDue(due_date=date(2026, 1, 20), outstanding_amount_krw=Decimal(4_500_000)),
    ]

    assert estimate_credit_recovery_date(
        as_of=AS_OF, schedule=schedule, required_collection_krw=Decimal(3_000_000)
    ) == date(2026, 1, 15)
    assert (
        estimate_credit_recovery_date(
            as_of=AS_OF, schedule=schedule, required_collection_krw=Decimal(0)
        )
        is None
    )
    assert (
        estimate_credit_recovery_date(
            as_of=AS_OF, schedule=schedule, required_collection_krw=Decimal(9_000_000)
        )
        is None
    )


def test_an_overdue_due_date_is_not_reported_as_a_past_recovery():
    """지난 예정일을 그대로 적으면 «이미 풀렸어야 한다» 로 읽힌다. 기준일로 올린다."""
    schedule = [
        OpenReceivableDue(due_date=date(2026, 1, 2), outstanding_amount_krw=Decimal(5_000_000))
    ]

    assert (
        estimate_credit_recovery_date(
            as_of=AS_OF, schedule=schedule, required_collection_krw=Decimal(1)
        )
        == AS_OF
    )


def test_capacity_carries_utilization_and_recovery_without_touching_the_verdict():
    facts = _facts(
        _receivable("AR-1", 3_000_000, date(2026, 1, 12)),
        _receivable("AR-2", 5_000_000, date(2026, 1, 15)),
    )
    credit = evaluate_receivable_capacity(
        sales_amount_krw=Decimal(5_000_000), receivable_facts=facts, credit_limit_krw=LIMIT
    )

    assert credit["credit_utilization_rate"] == Decimal("0.8")
    assert credit["required_collection_before_sale_krw"] == Decimal(3_000_000)
    assert credit["expected_credit_recovery_date"] == date(2026, 1, 12)
    assert credit["rule"]["verdict"] == "FAIL"


def test_schedule_skips_collected_rows_and_orders_by_due_date():
    facts = _facts(
        _receivable("AR-B", 2_000_000, date(2026, 1, 20)),
        _receivable("AR-A", 1_000_000, date(2026, 1, 10)),
        _receivable("AR-C", 0, date(2026, 1, 5), status="COLLECTED"),
    )

    assert [entry.due_date for entry in facts.open_receivable_schedule] == [
        date(2026, 1, 10),
        date(2026, 1, 20),
    ]


def test_summary_payload_serializes_the_recovery_date():
    summary = SalesFinancialSummary(
        recalculated_sales_amount_krw=Decimal(1),
        reported_sales_amount_krw=Decimal(1),
        amount_difference_krw=Decimal(0),
        amount_match=True,
        credit_utilization_rate=Decimal("0.8"),
        expected_credit_recovery_date=date(2026, 1, 15),
    )

    payload = _summary_payload(summary)

    assert payload["expected_credit_recovery_date"] == "2026-01-15"
    assert payload["credit_utilization_rate"] == Decimal("0.8")


# ── 수금 뒤 여신 복구 ─────────────────────────────────────────────────────


def test_a_recorded_collection_restores_available_credit():
    """AR 8M · 여신 2M → 5M 수금 기록 → AR 3M · 여신 7M · 현금 +5M."""
    receivable = {
        "receivable_id": "AR-1",
        "sim_run_id": "SIM-T",
        "original_amount_krw": Decimal(8_000_000),
        "received_amount_krw": Decimal(0),
        "outstanding_amount_krw": Decimal(8_000_000),
        "status": "OPEN",
    }
    state = {
        "finance_state_id": "FIN-T",
        "sim_run_id": "SIM-T",
        "current_cash_krw": Decimal(1_000_000),
        "receivables_krw": Decimal(8_000_000),
    }
    before = _facts(_receivable("AR-1", 8_000_000, AS_OF))
    assert calculate_available_credit(
        credit_limit_krw=LIMIT, current_partner_ar_krw=before.current_ar_krw
    ) == Decimal(2_000_000)

    plan = build_collection_transition(
        receivable, state, target_received_total_krw=Decimal(5_000_000)
    )
    after = _facts(
        _receivable("AR-1", int(plan.next_outstanding_amount_krw), AS_OF, plan.next_status)
    )

    assert plan.next_receivables_krw == Decimal(3_000_000)
    assert plan.next_current_cash_krw == Decimal(6_000_000)
    assert after.current_ar_krw == Decimal(3_000_000)
    assert calculate_available_credit(
        credit_limit_krw=LIMIT, current_partner_ar_krw=after.current_ar_krw
    ) == Decimal(7_000_000)


# ── 화면용 여신 현황 ──────────────────────────────────────────────────────


def _console(monkeypatch, *, limit, receivables, days=7):
    monkeypatch.setattr(
        console_credit,
        "load_credit_partner_rows",
        lambda **_: [
            {
                "partner_id": "KIMCHI_FACTORY_001",
                "partner_name": "김치제조공장",
                "sales_collection_days": days,
                "credit_limit_evidence_grade": "SIM_FIXED",
            }
        ],
    )
    monkeypatch.setattr(console_credit, "load_partner_receivables_as_of", lambda **_: receivables)
    monkeypatch.setattr(console_credit, "load_partner_credit_limit", lambda **_: limit)
    return console_credit.get_console_credit(sim_run_id="SIM-T", as_of=AS_OF)


def test_console_credit_answers_how_much_more_can_be_sold(monkeypatch):
    response = _console(
        monkeypatch,
        limit=LIMIT,
        receivables=[
            _receivable("AR-1", 2_500_000, date(2026, 1, 12)),
            _receivable("AR-2", 4_000_000, date(2026, 1, 15)),
        ],
    )
    partner = response.partners[0]

    assert partner.payment_days == 7
    assert partner.current_ar_krw == Decimal(6_500_000)
    assert partner.available_credit_krw == Decimal(3_500_000)
    assert partner.credit_utilization_rate == Decimal("0.65")
    assert [item.available_credit_after_krw for item in partner.upcoming_collections] == [
        Decimal(6_000_000),
        LIMIT,
    ]
    assert all(item.overdue is False for item in partner.upcoming_collections)


def test_console_credit_without_a_limit_does_not_invent_one(monkeypatch):
    partner = _console(
        monkeypatch,
        limit=None,
        receivables=[_receivable("AR-1", 1_000_000, date(2026, 1, 12))],
    ).partners[0]

    assert partner.credit_limit_krw is None
    assert partner.available_credit_krw is None
    assert partner.credit_utilization_rate is None
    assert partner.upcoming_collections[0].available_credit_after_krw is None


def test_console_credit_flags_a_past_due_collection(monkeypatch):
    partner = _console(
        monkeypatch,
        limit=LIMIT,
        receivables=[_receivable("AR-1", 1_000_000, date(2026, 1, 2))],
    ).partners[0]

    assert partner.overdue_ar_krw == Decimal(1_000_000)
    assert partner.upcoming_collections[0].overdue is True


def test_console_credit_reads_receivables_as_of_the_day(monkeypatch):
    """🔴 기준일 뒤의 수금이 과거 화면에 섞이지 않는다 — 복원 JOIN 과 날짜 인자를 함께 쓴다."""
    captured: dict[str, object] = {}

    def fake_fetch_all(query, params):
        captured["sql"] = query.as_string(None) if hasattr(query, "as_string") else str(query)
        captured["params"] = params
        return [
            {
                "receivable_id": "AR-1",
                "due_date": date(2026, 1, 12),
                "original_amount_krw": Decimal(3_000_000),
                "received_amount_krw": Decimal(1_000_000),
                "outstanding_amount_krw": Decimal(2_000_000),
            }
        ]

    monkeypatch.setattr(console_credit, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(console_credit, "get_db_schema", lambda: "haetdeul")
    rows = console_credit.load_partner_receivables_as_of(
        sim_run_id="SIM-T", as_of=AS_OF, partner_id="KIMCHI_FACTORY_001"
    )

    assert "master_collection_events" in captured["sql"]
    assert "collection_date <= %s" in captured["sql"]
    assert captured["params"] == [AS_OF, "SIM-T", "KIMCHI_FACTORY_001", AS_OF, AS_OF]
    assert rows[0].status == "PARTIAL"
    assert rows[0].outstanding_amount_krw == Decimal(2_000_000)
