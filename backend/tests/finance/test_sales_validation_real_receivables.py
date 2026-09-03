"""SALES_VALIDATION 이 **실 원장 채권**으로 회수위험을 판정한다.

★ 이 파일이 지키는 것.
    · 연체가 없으면 회수위험 PASS, 1원이라도 있으면 REVIEW_REQUIRED
    · 그 판정이 **실제 채권 행**에서 나온다 (기본값도, 0 대체도 아니다)
    · 채권이 생겨도 **여신은 여전히 못 연다** — 한도는 별개의 사실이다
    · 조회 실패는 0원 채권이 아니라 실행 자체가 서는 일이다
    · 회수위험이 열렸다고 재무 전체 판정이 열리지 않는다

🔴 여기서 가장 위험한 착각은 **여신과 회수위험을 하나로 보는 것**이다. 둘은 다른
   질문이다 — "이 거래처에 더 팔아도 되는 한도가 남았나"(여신)와 "이미 준 것을 제때
   받고 있나"(회수). 채권 잔액이 생겼다고 한도가 생기지는 않는다.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.finance.capabilities.sales import run_sales_validation
from app.finance.db import FinanceDataNotReady
from app.finance.sales_models import PartnerReceivable

AS_OF = date(2025, 12, 31)


def _receivable(receivable_id, *, due, status="OPEN", outstanding="100000"):
    return PartnerReceivable(
        receivable_id=receivable_id,
        due_date=due,
        outstanding_amount_krw=Decimal(outstanding),
        status=status,
        source_ref=receivable_id,
    )


class _LedgerPort:
    """실 조회 자리에 원장 행을 놓는 최소 Port."""

    def __init__(self, *receivables):
        self.receivables = list(receivables)
        self.asked: list[tuple[date, str]] = []

    def load_partner_receivables(self, as_of, partner_id):
        self.asked.append((as_of, partner_id))
        return list(self.receivables)


class _BrokenPort:
    def load_partner_receivables(self, as_of, partner_id):
        del as_of, partner_id
        raise FinanceDataNotReady("partner_receivables")


def _state(**over):
    from types import SimpleNamespace

    payload = {
        "scenario_id": "SC-1",
        "partner_id": "P-100",
        "item": "배추",
        "quantity_kg": "100",
        "unit_price_krw": "10000",
        "reported_sales_amount_krw": "1000000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "source_ref": "SALES-REPLY:R-1",
    }
    payload.update(over)
    return SimpleNamespace(
        request=SimpleNamespace(payload=payload, context=SimpleNamespace(as_of=AS_OF))
    )


def _rule(result, rule_id):
    for rule in result["rule_results"]:
        if rule["rule_id"] == rule_id:
            return rule
    raise AssertionError(f"{rule_id} 규칙 결과가 없다")


_COLLECTION = "FIN-SALES-COLLECTION-RISK"
_CREDIT = "FIN-SALES-CREDIT"


# ---------------------------------------------------------------------------
# Case 1 — 채권 0원 / 연체 0원
# ---------------------------------------------------------------------------


def test_a_clean_partner_passes_collection_risk():
    result = run_sales_validation(_LedgerPort(), {}, _state())

    assert _rule(result, _COLLECTION)["verdict"] == "PASS"
    assert result["financial_summary"]["overdue_ar_krw"] == Decimal(0)
    assert result["financial_summary"]["current_partner_ar_krw"] == Decimal(0)


def test_credit_stays_closed_even_for_a_clean_partner():
    """🔴 깨끗한 거래처라고 없는 한도가 생기지 않는다."""
    result = run_sales_validation(_LedgerPort(), {}, _state())

    credit = _rule(result, _CREDIT)
    assert credit["runtime_status"] == "RUNTIME_NOT_READY"
    assert credit["verdict"] is None
    assert "partner_credit_limit_krw" in result["missing_data"]
    assert result["financial_summary"]["credit_limit_krw"] is None
    assert result["financial_summary"]["available_credit_krw"] is None


# ---------------------------------------------------------------------------
# Case 2 — 채권 있음 / 연체 있음
# ---------------------------------------------------------------------------


def test_an_overdue_partner_needs_review():
    port = _LedgerPort(_receivable("AR-1", due=date(2025, 12, 30)))

    result = run_sales_validation(port, {}, _state())

    assert _rule(result, _COLLECTION)["verdict"] == "REVIEW_REQUIRED"
    assert result["financial_summary"]["overdue_ar_krw"] == Decimal(100000)


def test_credit_stays_closed_for_an_overdue_partner_too():
    """연체가 있어도 막는 판단은 여신 규칙이 소유한다 — 그 규칙은 아직 못 연다."""
    port = _LedgerPort(_receivable("AR-1", due=date(2025, 12, 30)))

    result = run_sales_validation(port, {}, _state())

    credit = _rule(result, _CREDIT)
    assert credit["runtime_status"] == "RUNTIME_NOT_READY"
    assert credit["verdict"] is None


def test_ar_that_is_not_overdue_does_not_trip_collection_risk():
    port = _LedgerPort(_receivable("AR-1", due=date(2026, 1, 5)))

    result = run_sales_validation(port, {}, _state())

    assert _rule(result, _COLLECTION)["verdict"] == "PASS"
    assert result["financial_summary"]["current_partner_ar_krw"] == Decimal(100000)
    assert result["financial_summary"]["overdue_ar_krw"] == Decimal(0)


# ---------------------------------------------------------------------------
# 계산 가능한 사실은 계산한다 — 판정할 수 없는 것만 남긴다
# ---------------------------------------------------------------------------


def test_projected_ar_is_computed_from_the_real_balance():
    """제안이 성사되면 채권이 얼마가 되는지는 한도가 없어도 알 수 있다."""
    port = _LedgerPort(_receivable("AR-1", due=date(2026, 1, 5), outstanding="250000"))

    summary = run_sales_validation(port, {}, _state())["financial_summary"]

    assert summary["current_partner_ar_krw"] == Decimal(250000)
    assert summary["projected_partner_ar_krw"] == Decimal(1250000)


def test_available_credit_is_not_computed_without_a_limit():
    """🔴 여력은 한도에서 빼는 값이다 — 한도가 없으면 만들 수 없다."""
    port = _LedgerPort(_receivable("AR-1", due=date(2026, 1, 5)))

    summary = run_sales_validation(port, {}, _state())["financial_summary"]

    assert summary["available_credit_krw"] is None


def test_the_real_ledger_rows_are_carried_as_evidence():
    port = _LedgerPort(
        _receivable("AR-1", due=date(2026, 1, 5)),
        _receivable("AR-2", due=date(2025, 12, 1)),
    )

    result = run_sales_validation(port, {}, _state())

    for ref in ("AR-1", "AR-2"):
        assert ref in result["evidence_refs"], ref


# ---------------------------------------------------------------------------
# 조회 기준일 — 판정 기준일과 같은 날이어야 한다
# ---------------------------------------------------------------------------


def test_the_ledger_is_asked_for_this_run_partner_and_date():
    port = _LedgerPort()

    run_sales_validation(port, {}, _state(partner_id="P-777"))

    assert port.asked == [(AS_OF, "P-777")]


def test_an_incomplete_proposal_asks_the_ledger_nothing():
    """거래처를 모르면 물어볼 곳도 없다 — 아무 거래처나 대신 읽지 않는다."""
    port = _LedgerPort()

    result = run_sales_validation(port, {}, _state(partner_id=None))

    assert result["status"] == "INPUT_INCOMPLETE"
    assert port.asked == []


# ---------------------------------------------------------------------------
# 못 읽은 것은 0원이 아니다
# ---------------------------------------------------------------------------


def test_a_broken_ledger_stops_the_run_instead_of_reporting_zero_ar():
    """🔴 조회 실패가 "연체 없음" 이 되면 위험한 거래처가 깨끗해 보인다."""
    with pytest.raises(FinanceDataNotReady) as caught:
        run_sales_validation(_BrokenPort(), {}, _state())

    assert caught.value.key == "partner_receivables"


# ---------------------------------------------------------------------------
# 회수위험이 열렸다고 재무 전체가 열리지는 않는다
# ---------------------------------------------------------------------------


def test_a_passing_collection_rule_does_not_make_the_whole_verdict_pass():
    """★ 종합 판정은 기존 aggregate 소유다 — 규칙 하나가 대신 답하지 않는다.

    여신·마진·현금흐름이 아직 닫혀 있으므로 전체는 판정되지 않는다.
    """
    result = run_sales_validation(_LedgerPort(), {}, _state())

    assert _rule(result, _COLLECTION)["verdict"] == "PASS"
    assert result["status"] == "RUNTIME_NOT_READY"
    assert result["finance_verdict"] is None


def test_missing_names_shrink_to_the_facts_finance_still_cannot_get():
    result = run_sales_validation(_LedgerPort(), {}, _state())

    assert "partner_receivable_facts" not in result["missing_data"]
    assert "partner_credit_limit_krw" in result["missing_data"]
