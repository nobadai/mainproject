"""Finance Sales Core Phase 4 — 판매 재무 판정 규칙.

★ 이 파일이 지키는 것은 **없는 정책을 FAIL 로 바꾸지 않는다**는 것이다.
    · 판매 마진 임계값과 최대 결제일수는 아직 권위 있는 값이 없다
    · 없으면 RUNTIME_NOT_READY 이고 verdict 는 None 이다
    · 경계는 정확히 등호에서 갈린다
    · 종합 판정은 어느 규칙 때문인지 감추지 않는다
  임계값 숫자는 전부 시험 픽스처다 — 업무 정책이 아니다.
"""

from decimal import Decimal

import pytest

from app.finance.rules import (
    aggregate_sales_finance_rules,
    evaluate_sales_amount_integrity,
    evaluate_sales_cashflow_rule,
    evaluate_sales_margin_rule,
    evaluate_sales_payment_term_rule,
)

# 시험 픽스처 — 저장소의 권위 있는 정책이 아니다.
MIN_RATE = Decimal("0.10")
WARN_RATE = Decimal("0.20")
MAX_DAYS = 30
MIN_CASH = Decimal(5_000_000)


def _margin(rate, *, minimum=MIN_RATE, warning=WARN_RATE):
    return evaluate_sales_margin_rule(
        contribution_margin_rate=rate,
        finance_minimum_margin_rate=minimum,
        finance_warning_margin_rate=warning,
    )


def _payment(days, *, maximum=MAX_DAYS, terms_type="SINGLE"):
    return evaluate_sales_payment_term_rule(
        payment_terms_type=terms_type,
        payment_days=days,
        max_finance_allowed_payment_terms_days=maximum,
    )


def _cash(
    *,
    base=Decimal(9_000_000),
    scenario=Decimal(9_000_000),
    depends=False,
    within_horizon=True,
):
    return evaluate_sales_cashflow_rule(
        base_projected_cash_min=base,
        scenario_projected_cash_min=scenario,
        minimum_cash_balance_krw=MIN_CASH,
        depends_on_projected_inflow=depends,
        collection_within_horizon=within_horizon,
    )


# ---------------------------------------------------------------------------
# 금액 정합
# ---------------------------------------------------------------------------


def test_amount_match_passes():
    result = evaluate_sales_amount_integrity(
        reported_amount_krw=Decimal(1_020_000),
        recalculated_amount_krw=Decimal(1_020_000),
    )

    assert result["verdict"] == "PASS"
    assert result["reason_codes"] == ("SALES_AMOUNT_MATCH",)


def test_amount_mismatch_fails_with_no_tolerance_band():
    result = evaluate_sales_amount_integrity(
        reported_amount_krw=Decimal("1020000.000001"),
        recalculated_amount_krw=Decimal(1_020_000),
    )

    assert result["verdict"] == "FAIL"
    assert result["reason_codes"] == ("SALES_AMOUNT_MISMATCH",)


# ---------------------------------------------------------------------------
# 마진 — 경계는 등호에서 갈린다
# ---------------------------------------------------------------------------


def test_margin_at_warning_threshold_passes():
    assert _margin(WARN_RATE)["verdict"] == "PASS"


def test_margin_above_warning_passes():
    assert _margin(Decimal("0.25"))["verdict"] == "PASS"


def test_margin_exactly_at_hard_floor_is_review_not_fail():
    result = _margin(MIN_RATE)

    assert result["verdict"] == "REVIEW_REQUIRED"
    assert result["reason_codes"] == ("SALES_MARGIN_BELOW_WARNING",)


def test_margin_between_floor_and_warning_is_review():
    assert _margin(Decimal("0.15"))["verdict"] == "REVIEW_REQUIRED"


def test_margin_just_below_hard_floor_fails():
    result = _margin(Decimal("0.09999999"))

    assert result["verdict"] == "FAIL"
    assert result["reason_codes"] == ("SALES_MARGIN_BELOW_MINIMUM",)


def test_negative_margin_rate_is_judged_against_policy_as_a_fact():
    # 역마진은 계산 사실이고, 정책이 있을 때 비로소 FAIL 이 된다.
    assert _margin(Decimal("-0.30"))["verdict"] == "FAIL"


def test_missing_warning_policy_is_runtime_not_ready_not_fail():
    result = _margin(Decimal("-0.30"), warning=None)

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert result["missing_policy"] == ("finance_warning_margin_rate",)


def test_missing_hard_floor_policy_is_runtime_not_ready_not_fail():
    result = _margin(Decimal("0.25"), minimum=None)

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert result["missing_policy"] == ("finance_minimum_margin_rate",)


def test_both_margin_policies_missing_are_both_reported():
    result = _margin(Decimal("0.25"), minimum=None, warning=None)

    assert result["missing_policy"] == (
        "finance_minimum_margin_rate",
        "finance_warning_margin_rate",
    )


def test_uncomputable_margin_rate_does_not_become_a_pass():
    result = _margin(None)

    assert result["runtime_status"] == "READY"
    assert result["verdict"] is None
    assert result["reason_codes"] == ("SALES_MARGIN_RATE_UNCOMPUTABLE",)


def test_inverted_margin_policy_is_rejected_as_invalid():
    with pytest.raises(ValueError):
        _margin(Decimal("0.25"), minimum=Decimal("0.30"), warning=Decimal("0.20"))


# ---------------------------------------------------------------------------
# 결제조건
# ---------------------------------------------------------------------------


def test_payment_days_below_max_passes():
    assert _payment(15)["verdict"] == "PASS"


def test_payment_days_exactly_at_max_passes():
    result = _payment(MAX_DAYS)

    assert result["verdict"] == "PASS"
    assert result["reason_codes"] == ("SALES_PAYMENT_TERM_WITHIN_LIMIT",)


def test_payment_days_one_over_max_fails():
    result = _payment(MAX_DAYS + 1)

    assert result["verdict"] == "FAIL"
    assert result["reason_codes"] == ("SALES_PAYMENT_TERM_EXCEEDS_LIMIT",)


def test_zero_payment_days_is_a_value_and_passes():
    assert _payment(0)["verdict"] == "PASS"


def test_missing_max_policy_is_runtime_not_ready_not_fail():
    result = _payment(999, maximum=None)

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert result["missing_policy"] == ("max_finance_allowed_payment_terms_days",)


def test_null_payment_days_is_not_treated_as_zero_or_unlimited():
    result = _payment(None)

    assert result["runtime_status"] == "READY"
    assert result["verdict"] is None
    assert result["reason_codes"] == ("SALES_PAYMENT_DAYS_ABSENT",)


def test_installment_is_not_silently_converted_to_single():
    result = _payment(15, terms_type="INSTALLMENT")

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert result["missing_policy"] == ("sales_installment_payment_policy",)


def test_negative_payment_days_is_rejected():
    with pytest.raises(ValueError):
        _payment(-1)


# ---------------------------------------------------------------------------
# 현금흐름 — 제안 유입을 확정 현금으로 취급하지 않는다
# ---------------------------------------------------------------------------


def test_scenario_safe_without_relying_on_the_proposal_passes():
    result = _cash()

    assert result["verdict"] == "PASS"
    assert result["reason_codes"] == ("SALES_CASHFLOW_SAFE",)


def test_broken_base_is_not_masked_by_the_proposed_inflow():
    result = _cash(base=Decimal(4_000_000), scenario=Decimal(9_000_000), depends=True)

    # BASE 가 이미 기준 아래다 — 제안 유입으로 가려지지 않는다.
    assert result["verdict"] == "FAIL"
    assert "BASE_MINIMUM_CASH_VIOLATED" in result["reason_codes"]


def test_dependence_on_the_proposed_inflow_is_a_fact_not_a_verdict():
    """🔴 예전에는 이 경우를 REVIEW_REQUIRED 로 낮췄다 — 근거가 없었다.

    저장소의 권위 있는 규칙(설계서 v2.2.2 §9 BASE/STRESS)은 *매입 STRESS 가 기준을
    밑돌 때* conditional 이라는 규칙이지, *유입에 기대는가* 를 다루지 않는다. 근거
    없이 판정을 낮추면 그것이 곧 합의되지 않은 정책이 된다. 사실은 reason code 로
    남기고, 판정은 최소 현금 정책만 움직인다.
    """
    result = _cash(base=Decimal(6_000_000), scenario=Decimal(9_000_000), depends=True)

    assert result["verdict"] == "PASS"
    assert "SALES_CASHFLOW_DEPENDS_ON_PROJECTED_INFLOW" in result["reason_codes"]


def test_the_dependence_fact_is_not_lost_when_the_verdict_passes():
    depends = _cash(base=Decimal(6_000_000), scenario=Decimal(9_000_000), depends=True)
    independent = _cash(base=Decimal(9_000_000), scenario=Decimal(9_000_000), depends=False)

    assert "SALES_CASHFLOW_DEPENDS_ON_PROJECTED_INFLOW" in depends["reason_codes"]
    assert "SALES_CASHFLOW_DEPENDS_ON_PROJECTED_INFLOW" not in independent["reason_codes"]
    assert depends["verdict"] == independent["verdict"] == "PASS"


def test_scenario_below_minimum_fails():
    result = _cash(base=Decimal(9_000_000), scenario=Decimal(4_000_000))

    assert result["verdict"] == "FAIL"
    assert "SCENARIO_MINIMUM_CASH_VIOLATED" in result["reason_codes"]


def test_exactly_at_minimum_cash_is_not_a_violation():
    assert _cash(base=MIN_CASH, scenario=MIN_CASH)["verdict"] == "PASS"


def test_collection_outside_horizon_is_reported_alongside_the_verdict():
    result = _cash(within_horizon=False)

    assert result["verdict"] == "PASS"
    assert result["reason_codes"] == ("SALES_COLLECTION_OUTSIDE_HORIZON", "SALES_CASHFLOW_SAFE")


# ---------------------------------------------------------------------------
# 종합
# ---------------------------------------------------------------------------


def _passing_rules():
    return [
        evaluate_sales_amount_integrity(
            reported_amount_krw=Decimal(1), recalculated_amount_krw=Decimal(1)
        ),
        _margin(Decimal("0.25")),
        _payment(15),
        _cash(),
    ]


def test_all_pass_aggregates_to_pass():
    result = aggregate_sales_finance_rules(_passing_rules())

    assert result["runtime_status"] == "READY"
    assert result["verdict"] == "PASS"
    assert len(result["rule_results"]) == 4


def test_one_review_downgrades_the_aggregate():
    rules = _passing_rules()
    rules[1] = _margin(Decimal("0.15"))

    result = aggregate_sales_finance_rules(rules)

    assert result["verdict"] == "REVIEW_REQUIRED"
    assert "SALES_MARGIN_BELOW_WARNING" in result["reason_codes"]


def test_one_fail_beats_a_review():
    rules = _passing_rules()
    rules[1] = _margin(Decimal("0.15"))
    rules[2] = _payment(MAX_DAYS + 1)

    result = aggregate_sales_finance_rules(rules)

    assert result["verdict"] == "FAIL"


def test_runtime_not_ready_beats_a_fail_and_never_becomes_a_verdict():
    rules = _passing_rules()
    rules[1] = _margin(Decimal("0.15"), warning=None)
    rules[2] = _payment(MAX_DAYS + 1)

    result = aggregate_sales_finance_rules(rules)

    # 정책이 없는데 FAIL 을 내면 "없는 문제"를 만든 것이 된다.
    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["verdict"] is None
    assert result["missing_policy"] == ("finance_warning_margin_rate",)


def test_an_unjudgeable_rule_blocks_a_pass_without_claiming_infrastructure_failure():
    rules = _passing_rules()
    rules[1] = _margin(None)

    result = aggregate_sales_finance_rules(rules)

    assert result["runtime_status"] == "READY"
    assert result["verdict"] is None


def test_aggregate_keeps_every_rule_result_visible():
    rules = _passing_rules()
    rules[2] = _payment(MAX_DAYS + 1)

    result = aggregate_sales_finance_rules(rules)

    failing = [item for item in result["rule_results"] if item["verdict"] == "FAIL"]
    assert [item["rule_id"] for item in failing] == ["FIN-SALES-PAYMENT-TERM"]


def test_empty_rule_set_is_rejected_rather_than_passing_vacuously():
    with pytest.raises(ValueError):
        aggregate_sales_finance_rules([])
