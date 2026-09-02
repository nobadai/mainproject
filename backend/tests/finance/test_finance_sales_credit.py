"""Finance Sales Core Phase 5 — 매출채권 · 여신 · 회수 위험 경계.

★ 이 파일이 지키는 것은 **없는 여신정책이 FAIL 이 되지 않는다**는 것이다.
    · 저장소에 credit_limit 컬럼이 없다 → 여신 판정은 RUNTIME_NOT_READY
    · 거래이력 없는 신규 거래처가 그 이유만으로 FAIL 이 되지 않는다
    · 연체 금액 같은 사실은 계산하되 위험 점수는 만들지 않는다
    · 채권 0원(사실)과 자료 없음은 다르다
"""

from datetime import date
from decimal import Decimal

import pytest

from app.finance.rules import (
    evaluate_collection_risk_rule,
    evaluate_receivable_capacity_rule,
)
from app.finance.sales_models import PartnerReceivable
from app.finance.tools import (
    calculate_available_credit,
    calculate_projected_partner_ar,
    summarize_partner_receivables,
)

AS_OF = date(2026, 3, 1)


def _receivable(
    ref: str,
    amount: str,
    *,
    due: date = date(2026, 4, 1),
    status: str = "OPEN",
) -> PartnerReceivable:
    return PartnerReceivable(
        receivable_id=ref,
        due_date=due,
        outstanding_amount_krw=Decimal(amount),
        status=status,
        source_ref=f"RECEIVABLE:{ref}",
    )


def _facts(receivables, partner_id: str = "P-100"):
    return summarize_partner_receivables(
        partner_id=partner_id, as_of=AS_OF, receivables=receivables
    )


# ---------------------------------------------------------------------------
# 채권 사실 집계
# ---------------------------------------------------------------------------


def test_no_receivables_is_zero_ar_as_a_fact():
    facts = _facts([])

    assert facts.current_ar_krw == Decimal(0)
    assert facts.overdue_ar_krw == Decimal(0)
    assert facts.open_receivable_count == 0
    assert facts.source_refs == ()


def test_open_and_partial_receivables_both_count_towards_ar():
    facts = _facts(
        [
            _receivable("R-1", "1000000"),
            _receivable("R-2", "500000", status="PARTIAL"),
        ]
    )

    assert facts.current_ar_krw == Decimal(1_500_000)
    assert facts.open_receivable_count == 2


def test_collected_and_writeoff_receivables_are_excluded():
    facts = _facts(
        [
            _receivable("R-1", "1000000"),
            _receivable("R-2", "900000", status="COLLECTED"),
            _receivable("R-3", "800000", status="WRITEOFF"),
        ]
    )

    assert facts.current_ar_krw == Decimal(1_000_000)
    assert facts.open_receivable_count == 1


def test_overdue_is_defined_by_due_date_before_as_of():
    facts = _facts(
        [
            _receivable("R-1", "1000000", due=date(2026, 2, 1)),
            _receivable("R-2", "500000", due=date(2026, 4, 1)),
        ]
    )

    assert facts.overdue_ar_krw == Decimal(1_000_000)
    assert facts.overdue_receivable_count == 1


def test_due_exactly_on_as_of_is_not_yet_overdue():
    facts = _facts([_receivable("R-1", "1000000", due=AS_OF)])

    assert facts.overdue_ar_krw == Decimal(0)


def test_receivable_source_refs_are_preserved():
    facts = _facts([_receivable("R-1", "1000000"), _receivable("R-2", "5")])

    assert facts.source_refs == ("RECEIVABLE:R-1", "RECEIVABLE:R-2")


def test_decimal_precision_survives_aggregation():
    facts = _facts([_receivable("R-1", "0.000001"), _receivable("R-2", "0.000002")])

    assert facts.current_ar_krw == Decimal("0.000003")


def test_blank_partner_id_is_rejected():
    with pytest.raises(ValueError):
        _facts([], partner_id="  ")


# ---------------------------------------------------------------------------
# 여신 산술
# ---------------------------------------------------------------------------


def test_available_credit_is_limit_minus_current_ar():
    assert calculate_available_credit(
        credit_limit_krw=Decimal(10_000_000), current_partner_ar_krw=Decimal(4_000_000)
    ) == Decimal(6_000_000)


def test_exactly_filled_limit_leaves_zero_available_credit():
    assert calculate_available_credit(
        credit_limit_krw=Decimal(10_000_000), current_partner_ar_krw=Decimal(10_000_000)
    ) == Decimal(0)


def test_over_limit_available_credit_stays_negative_and_is_not_clamped():
    # 0으로 깎으면 "딱 맞게 찼다"와 "이미 넘겼다"가 구분되지 않는다.
    assert calculate_available_credit(
        credit_limit_krw=Decimal(10_000_000), current_partner_ar_krw=Decimal(12_000_000)
    ) == Decimal(-2_000_000)


def test_negative_credit_limit_is_rejected():
    with pytest.raises(ValueError):
        calculate_available_credit(
            credit_limit_krw=Decimal(-1), current_partner_ar_krw=Decimal(0)
        )


def test_negative_current_ar_is_rejected():
    with pytest.raises(ValueError):
        calculate_available_credit(
            credit_limit_krw=Decimal(1), current_partner_ar_krw=Decimal(-1)
        )


def test_projected_partner_ar_adds_the_proposal():
    assert calculate_projected_partner_ar(
        current_partner_ar_krw=Decimal(4_000_000),
        proposed_sales_amount_krw=Decimal(1_000_000),
    ) == Decimal(5_000_000)


def test_projected_ar_for_a_new_partner_is_just_the_proposal():
    assert calculate_projected_partner_ar(
        current_partner_ar_krw=Decimal(0), proposed_sales_amount_krw=Decimal(1_000_000)
    ) == Decimal(1_000_000)


def test_negative_proposed_amount_is_rejected():
    with pytest.raises(ValueError):
        calculate_projected_partner_ar(
            current_partner_ar_krw=Decimal(0), proposed_sales_amount_krw=Decimal(-1)
        )


# ---------------------------------------------------------------------------
# 여신 판정 — 오늘은 항상 닫힌다
# ---------------------------------------------------------------------------


def test_projected_ar_within_limit_passes():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(5_000_000),
        credit_limit_krw=Decimal(10_000_000),
    )

    assert result["verdict"] == "PASS"
    assert result["reason_codes"] == ("SALES_CREDIT_WITHIN_LIMIT",)


def test_projected_ar_exactly_at_limit_passes():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(10_000_000),
        credit_limit_krw=Decimal(10_000_000),
    )

    assert result["verdict"] == "PASS"


def test_projected_ar_one_won_over_limit_fails():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(10_000_001),
        credit_limit_krw=Decimal(10_000_000),
    )

    assert result["verdict"] == "FAIL"
    assert result["reason_codes"] == ("SALES_CREDIT_LIMIT_EXCEEDED",)


def test_missing_credit_limit_is_runtime_not_ready_not_fail():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(999_000_000), credit_limit_krw=None
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert result["missing_policy"] == ("partner_credit_limit_krw",)


def test_a_new_partner_with_no_history_does_not_automatically_fail():
    facts = _facts([], partner_id="P-NEW")
    projected = calculate_projected_partner_ar(
        current_partner_ar_krw=facts.current_ar_krw,
        proposed_sales_amount_krw=Decimal(1_000_000),
    )

    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=projected, credit_limit_krw=None
    )

    # 이력이 없다는 것은 거절 사유가 아니다 — 막는 것은 없는 정책이다.
    assert result["verdict"] is None
    assert result["runtime_status"] == "RUNTIME_NOT_READY"


def test_a_new_partner_passes_when_an_authoritative_limit_exists():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(1_000_000),
        credit_limit_krw=Decimal(10_000_000),
    )

    assert result["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# 회수 위험 — 점수를 만들지 않는다
# ---------------------------------------------------------------------------


def test_collection_risk_without_policy_is_runtime_not_ready():
    result = evaluate_collection_risk_rule(overdue_ar_krw=Decimal(3_000_000))

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert result["missing_policy"] == ("sales_collection_risk_policy",)


def test_collection_risk_result_carries_no_score_field():
    result = evaluate_collection_risk_rule(overdue_ar_krw=Decimal(3_000_000))

    # 91/100 같은 숫자를 지어내지 않는다.
    assert set(result) == {
        "rule_id",
        "runtime_status",
        "verdict",
        "reason_codes",
        "missing_policy",
    }


def test_overdue_facts_survive_even_though_risk_cannot_be_judged():
    facts = _facts([_receivable("R-1", "3000000", due=date(2026, 1, 1))])
    result = evaluate_collection_risk_rule(overdue_ar_krw=facts.overdue_ar_krw)

    assert facts.overdue_ar_krw == Decimal(3_000_000)
    assert result["verdict"] is None


def test_negative_overdue_amount_is_rejected():
    with pytest.raises(ValueError):
        evaluate_collection_risk_rule(overdue_ar_krw=Decimal(-1))
