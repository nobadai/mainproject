"""Finance/Sales MVP Policy v0.1 — 값과 그 값의 성격.

★ 이 파일이 지키는 것.
    · 정책 로더는 언제 불러도 같은 값을 낸다
    · 임계값은 Decimal 이다 (float 로 새면 경계에서 판정이 뒤집힌다)
    · 여신한도는 정책이 아니라 거래처가 소유한 사실이라 여기 없다
    · 매입 마진 정책을 베껴 오지 않았다
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.finance.sales_policy import (
    FINANCE_SALES_MVP_POLICY_REF,
    FinanceSalesMvpPolicy,
    load_finance_sales_mvp_policy,
)


def test_policy_values_are_the_mvp_decision():
    policy = load_finance_sales_mvp_policy()

    assert policy.finance_minimum_margin_rate == Decimal("0.2642")
    assert policy.finance_warning_margin_rate == Decimal("0.30")
    assert policy.max_finance_allowed_payment_terms_days == 30
    assert policy.supported_payment_terms_type == "SINGLE"
    assert policy.collection_risk_mode == "ANY_OVERDUE_REVIEW"


def test_thresholds_are_decimal_not_float():
    """float 로 새면 0.2642 경계에서 판정이 뒤집힌다."""
    policy = load_finance_sales_mvp_policy()

    assert isinstance(policy.finance_minimum_margin_rate, Decimal)
    assert isinstance(policy.finance_warning_margin_rate, Decimal)


def test_loader_is_deterministic():
    """같은 제안이 날마다 다른 판정을 받으면 안 된다."""
    first = load_finance_sales_mvp_policy()
    second = load_finance_sales_mvp_policy()

    assert first == second
    assert first.finance_minimum_margin_rate == second.finance_minimum_margin_rate


def test_policy_metadata_keeps_both_axes():
    """`PROVISIONAL`(수명)과 `SIM_FIXED`(근거 등급)는 다른 축이다."""
    policy = load_finance_sales_mvp_policy()

    assert policy.status == "PROVISIONAL"
    assert policy.evidence_grade == "SIM_FIXED"
    assert policy.usage_scope == "AGENT_MVP_DEMO"
    assert policy.policy_version == "Finance/Sales MVP Policy v0.1"


def test_decision_ref_points_at_this_decision_only():
    policy = load_finance_sales_mvp_policy()

    assert policy.decision_ref == FINANCE_SALES_MVP_POLICY_REF
    assert policy.decision_ref == "FIN-SALES-MVP-POLICY-V0.1"


def test_policy_does_not_carry_a_credit_limit():
    """🔴 여신한도는 재무가 정하는 값이 아니라 거래처가 소유한 사실이다.

    여기 기본값을 두면 없는 한도를 재무가 발명하게 된다.
    """
    policy = load_finance_sales_mvp_policy()

    assert not hasattr(policy, "credit_limit_krw")
    assert not hasattr(policy, "partner_credit_limit_krw")


def test_sales_margin_policy_is_not_the_purchase_floor():
    """매입 `margin_defense_floor_rate`(0.267)를 베껴 오지 않았다."""
    policy = load_finance_sales_mvp_policy()

    assert policy.finance_minimum_margin_rate != Decimal("0.267")


def test_policy_is_frozen():
    policy = load_finance_sales_mvp_policy()

    with pytest.raises(ValidationError):
        policy.finance_minimum_margin_rate = Decimal("0.1")  # type: ignore[misc]


def test_warning_below_minimum_is_rejected():
    """경고선이 하한보다 낮으면 REVIEW 구간이 사라진다 — 만들 수 없게 막는다."""
    with pytest.raises(ValueError):
        FinanceSalesMvpPolicy(
            finance_minimum_margin_rate=Decimal("0.30"),
            finance_warning_margin_rate=Decimal("0.20"),
            max_finance_allowed_payment_terms_days=30,
            supported_payment_terms_type="SINGLE",
            collection_risk_mode="ANY_OVERDUE_REVIEW",
            policy_version="test",
            status="PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            evidence_grade="SIM_FIXED",
            decision_ref="TEST",
        )


def test_unknown_collection_risk_mode_is_rejected():
    """점수·가중치 방식을 어휘에 몰래 들이지 않는다."""
    with pytest.raises(ValueError):
        FinanceSalesMvpPolicy(
            finance_minimum_margin_rate=Decimal("0.2642"),
            finance_warning_margin_rate=Decimal("0.30"),
            max_finance_allowed_payment_terms_days=30,
            supported_payment_terms_type="SINGLE",
            collection_risk_mode="WEIGHTED_SCORE",  # type: ignore[arg-type]
            policy_version="test",
            status="PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            evidence_grade="SIM_FIXED",
            decision_ref="TEST",
        )
