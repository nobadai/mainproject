"""MVP v0.1 정책이 실제 판정으로 이어지는가 — 경계값을 Decimal 로 못 박는다.

★ 이 파일이 지키는 것.
    · 마진 0.2642 / 0.30 경계가 정확히 갈린다 (float 로 새면 뒤집힌다)
    · SINGLE 30일 경계, INSTALLMENT 는 판정하지 않음
    · 여신한도는 없으면 판정하지 않음 (0원도 무제한도 아니다)
    · 연체가 있으면 REVIEW_REQUIRED 이고 **FAIL 은 여신 규칙이 소유**한다
    · 제안 유입에 기댄다는 사실만으로 판정을 낮추지 않는다
"""

from decimal import Decimal

import pytest

from app.finance.rules import (
    aggregate_sales_finance_rules,
    evaluate_collection_risk_rule,
    evaluate_receivable_capacity_rule,
    evaluate_sales_cashflow_rule,
    evaluate_sales_margin_rule,
    evaluate_sales_payment_term_rule,
)
from app.finance.sales_policy import load_finance_sales_mvp_policy

POLICY = load_finance_sales_mvp_policy()


def _margin(rate: str):
    return evaluate_sales_margin_rule(
        contribution_margin_rate=Decimal(rate),
        finance_minimum_margin_rate=POLICY.finance_minimum_margin_rate,
        finance_warning_margin_rate=POLICY.finance_warning_margin_rate,
    )


def _payment(days, terms_type="SINGLE"):
    return evaluate_sales_payment_term_rule(
        payment_terms_type=terms_type,
        payment_days=days,
        max_finance_allowed_payment_terms_days=(
            POLICY.max_finance_allowed_payment_terms_days
        ),
    )


def _collection(overdue: str):
    return evaluate_collection_risk_rule(
        overdue_ar_krw=Decimal(overdue),
        collection_risk_mode=POLICY.collection_risk_mode,
    )


# ---------------------------------------------------------------------------
# Margin — 경계값
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
def test_margin_boundaries(rate, verdict):
    assert _margin(rate)["verdict"] == verdict


def test_margin_boundary_is_exact_not_floating():
    """0.2642 는 포함, 그 바로 아래는 제외 — 한 자리 차이로 갈린다."""
    assert _margin("0.26419999")["verdict"] == "FAIL"
    assert _margin("0.2642")["verdict"] == "REVIEW_REQUIRED"


def test_negative_margin_is_a_fail_not_an_error():
    assert _margin("-0.5")["verdict"] == "FAIL"


def test_high_margin_passes():
    assert _margin("0.9")["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# Payment — SINGLE 30일
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("days", "verdict"), [(0, "PASS"), (30, "PASS"), (31, "FAIL")])
def test_single_payment_boundaries(days, verdict):
    assert _payment(days)["verdict"] == verdict


def test_zero_days_is_a_value_not_absence():
    result = _payment(0)

    assert result["verdict"] == "PASS"
    assert result["runtime_status"] == "READY"


def test_installment_is_not_judged_and_not_normalised_to_single():
    result = _payment(10, terms_type="INSTALLMENT")

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert "sales_installment_payment_policy" in result["missing_policy"]


def test_absent_payment_days_is_not_read_as_zero():
    result = _payment(None)

    assert result["verdict"] is None
    assert "SALES_PAYMENT_DAYS_ABSENT" in result["reason_codes"]


# ---------------------------------------------------------------------------
# Credit — 권위 있는 사실이 없으면 판정하지 않는다
# ---------------------------------------------------------------------------


def test_projected_ar_equal_to_limit_passes():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(50_000_000),
        credit_limit_krw=Decimal(50_000_000),
    )

    assert result["verdict"] == "PASS"


def test_projected_ar_above_limit_fails():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(50_000_001),
        credit_limit_krw=Decimal(50_000_000),
    )

    assert result["verdict"] == "FAIL"


def test_absent_credit_limit_is_not_unlimited_and_not_zero():
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(50_000_000), credit_limit_krw=None
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert "partner_credit_limit_krw" in result["missing_policy"]


def test_zero_credit_limit_is_a_real_limit():
    """0원 한도는 '한도 없음'이 아니라 '한도가 0원'이라는 사실이다."""
    result = evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(1), credit_limit_krw=Decimal(0)
    )

    assert result["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# Collection Risk — ANY_OVERDUE_REVIEW
# ---------------------------------------------------------------------------


def test_no_overdue_passes():
    assert _collection("0")["verdict"] == "PASS"


def test_any_overdue_is_review_required_not_fail():
    """🔴 연체가 있다고 거래를 막지 않는다 — 막는 판단은 여신 규칙이 소유한다."""
    result = _collection("1")

    assert result["verdict"] == "REVIEW_REQUIRED"
    assert result["verdict"] != "FAIL"


def test_large_overdue_is_still_only_review_required():
    assert _collection("999999999")["verdict"] == "REVIEW_REQUIRED"


def test_collection_risk_produces_no_score():
    result = _collection("100000")

    # 점수·가중치·등급을 만들지 않는다.
    assert set(result.keys()) >= {"rule_id", "verdict", "reason_codes"}
    assert "score" not in result
    assert "weight" not in result


def test_absent_mode_stays_closed():
    result = evaluate_collection_risk_rule(
        overdue_ar_krw=Decimal(0), collection_risk_mode=None
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None


def test_unknown_mode_is_not_guessed():
    result = evaluate_collection_risk_rule(
        overdue_ar_krw=Decimal(0), collection_risk_mode="WEIGHTED_SCORE"
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None


# ---------------------------------------------------------------------------
# Aggregate — 어느 규칙이 무엇을 소유하는지
# ---------------------------------------------------------------------------


def _credit(projected: str, limit: str):
    return evaluate_receivable_capacity_rule(
        projected_partner_ar_krw=Decimal(projected), credit_limit_krw=Decimal(limit)
    )


def test_credit_pass_with_overdue_review_aggregates_to_review_required():
    aggregate = aggregate_sales_finance_rules(
        [_credit("10", "1000"), _collection("100000")]
    )

    assert aggregate["verdict"] == "REVIEW_REQUIRED"


def test_credit_fail_with_overdue_review_aggregates_to_fail():
    aggregate = aggregate_sales_finance_rules(
        [_credit("2000", "1000"), _collection("100000")]
    )

    assert aggregate["verdict"] == "FAIL"


def test_all_clear_aggregates_to_pass():
    aggregate = aggregate_sales_finance_rules(
        [_credit("10", "1000"), _collection("0"), _margin("0.35"), _payment(30)]
    )

    assert aggregate["verdict"] == "PASS"


def test_one_missing_policy_keeps_the_aggregate_closed():
    aggregate = aggregate_sales_finance_rules(
        [_collection("0"), evaluate_receivable_capacity_rule(
            projected_partner_ar_krw=Decimal(10), credit_limit_krw=None
        )]
    )

    assert aggregate["runtime_status"] == "RUNTIME_NOT_READY"
    assert aggregate["verdict"] is None


def test_aggregate_keeps_every_rule_result():
    rules = [_credit("10", "1000"), _collection("100000"), _margin("0.35")]

    aggregate = aggregate_sales_finance_rules(rules)

    # 어느 규칙 때문인지 감추지 않는다.
    assert len(aggregate["rule_results"]) == 3
    assert "SALES_PARTNER_HAS_OVERDUE_AR" in aggregate["reason_codes"]


# ---------------------------------------------------------------------------
# Cashflow — 제안 유입에 기댄다는 사실만으로 낮추지 않는다
# ---------------------------------------------------------------------------


def test_depending_on_projected_inflow_alone_does_not_lower_the_verdict():
    result = evaluate_sales_cashflow_rule(
        base_projected_cash_min=Decimal(5_000_000),
        scenario_projected_cash_min=Decimal(9_000_000),
        minimum_cash_balance_krw=Decimal(1_000_000),
        depends_on_projected_inflow=True,
        collection_within_horizon=True,
    )

    assert result["verdict"] == "PASS"
    # 사실 자체는 잃지 않는다.
    assert "SALES_CASHFLOW_DEPENDS_ON_PROJECTED_INFLOW" in result["reason_codes"]


def test_base_below_minimum_still_fails_even_with_a_projected_inflow():
    result = evaluate_sales_cashflow_rule(
        base_projected_cash_min=Decimal(0),
        scenario_projected_cash_min=Decimal(9_000_000),
        minimum_cash_balance_krw=Decimal(1_000_000),
        depends_on_projected_inflow=True,
        collection_within_horizon=True,
    )

    # 아직 안 들어온 돈이 이미 난 구멍을 가리지 못한다.
    assert result["verdict"] == "FAIL"
