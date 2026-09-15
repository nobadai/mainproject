"""최신 공용 계약(timing/split_date) 아래에서도 재무가 자기 축을 잃지 않는가.

★ 이 파일이 지키는 것.
    · 재무의 조정축은 `amount` 하나뿐이다
    · `amount` 에는 회차 개념이 없으므로 `split_date=None` 이 정상이다
    · timing 계약에 맞추려고 회차·도착일을 지어내지 않는다
    · MVP 정책 경계값이 그대로 산다

🔴 공용 계약이 `axis == "timing"` 에 `split_date` 를 필수로 요구하기 시작했다.
   그 요구를 피하려고 재무가 timing 을 흉내내거나, 반대로 amount 에 억지로 날짜를
   붙이면 두 축의 뜻이 섞인다. 재무는 계속 금액만 소유한다.
"""

from datetime import date
from decimal import Decimal
from typing import get_args

import pytest

from app.contracts.core import AdjustAxis, SuggestedAdjustment
from app.finance.execution import _adjustment_from_dict
from app.finance.rules import (
    evaluate_collection_risk_rule,
    evaluate_receivable_capacity_rule,
    evaluate_sales_margin_rule,
    evaluate_sales_payment_term_rule,
)
from app.finance.sales_policy import load_finance_sales_mvp_policy

POLICY = load_finance_sales_mvp_policy()


def _adjustment(**over):
    payload = {
        "target_value": 800.0,
        "unit": "krw",
        "reason": "Verified Finance amount alternative.",
        "ref_ids": ["FIN-AGENT:req-1:1:S2:validate_amount_adjustment"],
    }
    payload.update(over)
    return _adjustment_from_dict(payload)


# ---------------------------------------------------------------------------
# 재무의 축은 amount 하나다
# ---------------------------------------------------------------------------


def test_finance_emits_only_the_amount_axis():
    assert _adjustment().axis == "amount"
    assert _adjustment().dept == "finance"


def test_finance_never_emits_a_timing_adjustment():
    """timing 을 내면 회차를 밝혀야 하는데, 재무에는 회차 개념이 없다."""
    adjustment = _adjustment()

    for foreign in ("timing", "quantity", "channel_mix"):
        assert adjustment.axis != foreign


def test_amount_adjustment_has_no_split_date():
    """회차 개념이 없는 축의 `split_date=None` 은 정상이다."""
    assert _adjustment().split_date is None


def test_amount_adjustment_is_accepted_by_the_common_contract_without_split_date():
    """공용 계약이 amount 에는 회차를 요구하지 않는다 — 지어낼 이유가 없다."""
    adjustment = SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=800.0,
        unit="krw",
        reason="Verified Finance amount alternative.",
        ref_ids=("FIN-REF",),
    )

    assert adjustment.split_date is None


def test_common_contract_still_demands_split_date_for_timing():
    """계약 자체는 살아 있다 — 재무가 그 축을 안 쓸 뿐이다."""
    from app.contracts.core import ContractViolation

    with pytest.raises(ContractViolation):
        SuggestedAdjustment(
            dept="inventory",
            axis="timing",
            target_value=1.0,
            unit="d",
            reason="x",
            ref_ids=("REF",),
        )


def test_finance_is_not_allowed_the_timing_axis_at_all():
    from app.contracts.core import ContractViolation

    with pytest.raises(ContractViolation):
        SuggestedAdjustment(
            dept="finance",
            axis="timing",
            target_value=1.0,
            unit="d",
            reason="x",
            ref_ids=("REF",),
            split_date=date(2026, 1, 5),
        )


def test_finance_does_not_invent_an_arrival_date_in_place_of_split_date():
    """🔴 split_date 는 '어느 회차' 이지 '목표 도착일' 이 아니다."""
    adjustment = _adjustment(split_date=None)

    assert adjustment.split_date is None
    assert not hasattr(adjustment, "suggested_arrival_date")


def test_upstream_split_date_is_carried_when_it_genuinely_exists():
    """완전성을 위해 옮기기만 한다 — 재무가 날짜를 만들지는 않는다."""
    assert _adjustment(split_date=date(2026, 1, 5)).split_date == date(2026, 1, 5)


def test_the_common_axis_vocabulary_is_unchanged():
    assert set(get_args(AdjustAxis)) == {"quantity", "timing", "channel_mix", "amount"}


# ---------------------------------------------------------------------------
# MVP 정책 경계 — 최신 dev 에서도 그대로 산다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "verdict"),
    [
        ("0.2641", "FAIL"),
        ("0.2642", "REVIEW_REQUIRED"),
        ("0.2999", "REVIEW_REQUIRED"),
        ("0.30", "PASS"),
    ],
)
def test_margin_boundaries_survive(rate, verdict):
    result = evaluate_sales_margin_rule(
        contribution_margin_rate=Decimal(rate),
        finance_minimum_margin_rate=POLICY.finance_minimum_margin_rate,
        finance_warning_margin_rate=POLICY.finance_warning_margin_rate,
    )

    assert result["verdict"] == verdict


@pytest.mark.parametrize(("days", "verdict"), [(0, "PASS"), (30, "PASS"), (31, "FAIL")])
def test_payment_boundaries_survive(days, verdict):
    result = evaluate_sales_payment_term_rule(
        payment_terms_type="SINGLE",
        payment_days=days,
        max_finance_allowed_payment_terms_days=(
            POLICY.max_finance_allowed_payment_terms_days
        ),
    )

    assert result["verdict"] == verdict


def test_installment_stays_unjudged():
    result = evaluate_sales_payment_term_rule(
        payment_terms_type="INSTALLMENT",
        payment_days=10,
        max_finance_allowed_payment_terms_days=(
            POLICY.max_finance_allowed_payment_terms_days
        ),
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None


@pytest.mark.parametrize(("overdue", "verdict"), [("0", "PASS"), ("1", "REVIEW_REQUIRED")])
def test_collection_boundaries_survive(overdue, verdict):
    result = evaluate_collection_risk_rule(
        overdue_ar_krw=Decimal(overdue),
        collection_risk_mode=POLICY.collection_risk_mode,
    )

    assert result["verdict"] == verdict


def test_absent_credit_limit_is_still_not_invented():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(1_000_000), credit_limit_krw=None
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert "partner_credit_limit_krw" in result["missing_policy"]


def test_policy_numbers_are_unchanged_by_the_latest_dev():
    assert POLICY.finance_minimum_margin_rate == Decimal("0.2642")
    assert POLICY.finance_warning_margin_rate == Decimal("0.30")
    assert POLICY.max_finance_allowed_payment_terms_days == 30
    assert POLICY.supported_payment_terms_type == "SINGLE"
    assert POLICY.collection_risk_mode == "ANY_OVERDUE_REVIEW"
