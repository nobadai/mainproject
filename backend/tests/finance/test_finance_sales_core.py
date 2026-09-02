"""Finance Sales Core Phase 1 — 결정론적 매출 계산 원시함수.

★ 이 파일이 지키는 것은 **계산 사실**이지 판정이 아니다.
    · 수량 × 단가는 반올림 없이 정확히 맞는다
    · 보고 금액 비교는 허용오차 없이 차이를 그대로 남긴다
    · 권위 있는 원가 기준액이 없으면 공헌이익을 만들지 않는다 (0으로 대체 금지)
    · 매출액 0에서는 이익률을 지어내지 않고 계산 불가(None)로 남긴다
  Sales Margin 임계값/PASS·FAIL은 아직 정책이 없으므로 여기서 시험하지 않는다.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.finance.tools import (
    build_sales_calculation_facts,
    calculate_collection_date,
    calculate_contribution_margin,
    calculate_contribution_margin_rate,
    calculate_sales_amount,
    compare_reported_sales_amount,
)

# ---------------------------------------------------------------------------
# 매출액
# ---------------------------------------------------------------------------


def test_sales_amount_multiplies_integer_kg_by_unit_price():
    amount = calculate_sales_amount(quantity_kg=Decimal(120), unit_price_krw=Decimal(8500))

    assert amount == Decimal(1020000)


def test_sales_amount_keeps_decimal_quantity_exact():
    amount = calculate_sales_amount(
        quantity_kg=Decimal("12.345"), unit_price_krw=Decimal("8500.25")
    )

    # float로 새면 어긋나는 자리까지 정확히 남아야 한다.
    assert amount == Decimal("104935.58625")


def test_zero_quantity_is_a_value_not_a_missing_input():
    assert calculate_sales_amount(
        quantity_kg=Decimal(0), unit_price_krw=Decimal(8500)
    ) == Decimal(0)


def test_zero_unit_price_is_a_value_not_a_missing_input():
    assert calculate_sales_amount(
        quantity_kg=Decimal(120), unit_price_krw=Decimal(0)
    ) == Decimal(0)


def test_negative_quantity_is_rejected_instead_of_clamped():
    with pytest.raises(ValueError):
        calculate_sales_amount(quantity_kg=Decimal(-1), unit_price_krw=Decimal(8500))


def test_negative_unit_price_is_rejected_instead_of_clamped():
    with pytest.raises(ValueError):
        calculate_sales_amount(quantity_kg=Decimal(120), unit_price_krw=Decimal(-1))


# ---------------------------------------------------------------------------
# 보고 금액 비교 — 기존 compare_reported_amount 계약 그대로
# ---------------------------------------------------------------------------


def test_reported_amount_matching_recalculation_has_zero_difference():
    comparison = compare_reported_sales_amount(
        reported_amount_krw=Decimal(1020000),
        recalculated_amount_krw=Decimal(1020000),
    )

    assert comparison["is_match"] is True
    assert comparison["difference"] == Decimal(0)


def test_under_reported_amount_keeps_positive_difference_lineage():
    comparison = compare_reported_sales_amount(
        reported_amount_krw=Decimal(1019999),
        recalculated_amount_krw=Decimal(1020000),
    )

    assert comparison["is_match"] is False
    assert comparison["reported_amount_krw"] == Decimal(1019999)
    assert comparison["recalculated_amount_krw"] == Decimal(1020000)
    assert comparison["difference"] == Decimal(1)


def test_over_reported_amount_keeps_negative_difference_lineage():
    comparison = compare_reported_sales_amount(
        reported_amount_krw=Decimal(1020001),
        recalculated_amount_krw=Decimal(1020000),
    )

    assert comparison["is_match"] is False
    assert comparison["difference"] == Decimal(-1)


def test_sub_won_gap_is_a_mismatch_no_tolerance_band():
    comparison = compare_reported_sales_amount(
        reported_amount_krw=Decimal("1020000.000001"),
        recalculated_amount_krw=Decimal(1020000),
    )

    assert comparison["is_match"] is False


# ---------------------------------------------------------------------------
# 공헌이익
# ---------------------------------------------------------------------------


def test_positive_contribution_margin():
    assert calculate_contribution_margin(
        sales_amount_krw=Decimal(1020000), sales_cost_basis_krw=Decimal(700000)
    ) == Decimal(320000)


def test_zero_contribution_margin():
    assert calculate_contribution_margin(
        sales_amount_krw=Decimal(1020000), sales_cost_basis_krw=Decimal(1020000)
    ) == Decimal(0)


def test_negative_contribution_margin_is_a_valid_calculated_fact():
    # 역마진은 계산 사실로 남긴다 — 이 단계에서 FAIL로 바꾸지 않는다.
    assert calculate_contribution_margin(
        sales_amount_krw=Decimal(700000), sales_cost_basis_krw=Decimal(1020000)
    ) == Decimal(-320000)


def test_negative_cost_basis_is_rejected():
    with pytest.raises(ValueError):
        calculate_contribution_margin(
            sales_amount_krw=Decimal(1020000), sales_cost_basis_krw=Decimal(-1)
        )


def test_negative_sales_amount_is_rejected_for_margin():
    with pytest.raises(ValueError):
        calculate_contribution_margin(
            sales_amount_krw=Decimal(-1), sales_cost_basis_krw=Decimal(0)
        )


# ---------------------------------------------------------------------------
# 공헌이익률
# ---------------------------------------------------------------------------


def test_positive_margin_rate():
    assert calculate_contribution_margin_rate(
        sales_amount_krw=Decimal(1000000), contribution_margin_krw=Decimal(250000)
    ) == Decimal("0.25")


def test_zero_margin_rate():
    assert calculate_contribution_margin_rate(
        sales_amount_krw=Decimal(1000000), contribution_margin_krw=Decimal(0)
    ) == Decimal(0)


def test_negative_margin_rate_is_returned_as_calculated():
    assert calculate_contribution_margin_rate(
        sales_amount_krw=Decimal(1000000), contribution_margin_krw=Decimal(-250000)
    ) == Decimal("-0.25")


def test_zero_sales_amount_reports_uncomputable_rate_instead_of_dividing():
    assert (
        calculate_contribution_margin_rate(
            sales_amount_krw=Decimal(0), contribution_margin_krw=Decimal(0)
        )
        is None
    )


def test_negative_sales_amount_is_rejected_for_margin_rate():
    with pytest.raises(ValueError):
        calculate_contribution_margin_rate(
            sales_amount_krw=Decimal(-1), contribution_margin_krw=Decimal(0)
        )


# ---------------------------------------------------------------------------
# 회수일 — 기준일의 의미는 호출자가 가진다
# ---------------------------------------------------------------------------


def test_payment_days_zero_collects_on_the_reference_date():
    assert calculate_collection_date(reference_date=date(2026, 3, 10), payment_days=0) == date(
        2026, 3, 10
    )


def test_normal_d_plus_n():
    assert calculate_collection_date(reference_date=date(2026, 3, 10), payment_days=30) == date(
        2026, 4, 9
    )


def test_month_boundary_is_crossed_by_real_days_not_month_arithmetic():
    assert calculate_collection_date(reference_date=date(2026, 1, 31), payment_days=1) == date(
        2026, 2, 1
    )


def test_year_boundary():
    assert calculate_collection_date(reference_date=date(2026, 12, 20), payment_days=30) == date(
        2027, 1, 19
    )


def test_leap_year_february_is_counted():
    assert calculate_collection_date(reference_date=date(2028, 2, 28), payment_days=1) == date(
        2028, 2, 29
    )


def test_non_leap_year_february_skips_to_march():
    assert calculate_collection_date(reference_date=date(2027, 2, 28), payment_days=1) == date(
        2027, 3, 1
    )


def test_negative_payment_days_is_rejected():
    with pytest.raises(ValueError):
        calculate_collection_date(reference_date=date(2026, 3, 10), payment_days=-1)


# ---------------------------------------------------------------------------
# 내부 사실 묶음 — 없는 입력을 지어내지 않는다
# ---------------------------------------------------------------------------


def test_facts_carry_every_computable_value():
    facts = build_sales_calculation_facts(
        quantity_kg=Decimal("120.5"),
        unit_price_krw=Decimal(8000),
        reported_amount_krw=Decimal(964000),
        sales_cost_basis_krw=Decimal(723000),
        reference_date=date(2026, 3, 10),
        payment_days=45,
    )

    comparison = facts["reported_amount_comparison"]
    assert facts["recalculated_sales_amount_krw"] == Decimal(964000)
    assert comparison is not None
    assert comparison["is_match"] is True
    assert facts["contribution_margin_krw"] == Decimal(241000)
    assert facts["contribution_margin_rate"] == Decimal("0.25")
    assert facts["collection_date"] == date(2026, 4, 24)


def test_missing_cost_basis_produces_no_margin_instead_of_zero():
    facts = build_sales_calculation_facts(
        quantity_kg=Decimal(100), unit_price_krw=Decimal(8000)
    )

    assert facts["recalculated_sales_amount_krw"] == Decimal(800000)
    assert facts["contribution_margin_krw"] is None
    assert facts["contribution_margin_rate"] is None


def test_missing_collection_inputs_produce_no_collection_date():
    facts = build_sales_calculation_facts(
        quantity_kg=Decimal(100),
        unit_price_krw=Decimal(8000),
        reference_date=date(2026, 3, 10),
    )

    assert facts["collection_date"] is None
    assert facts["reported_amount_comparison"] is None


def test_zero_cost_basis_is_honoured_when_it_is_the_authoritative_input():
    # 0원 원가와 원가 없음은 다르다 — 0을 주면 0으로 계산한다.
    facts = build_sales_calculation_facts(
        quantity_kg=Decimal(100),
        unit_price_krw=Decimal(8000),
        sales_cost_basis_krw=Decimal(0),
    )

    assert facts["contribution_margin_krw"] == Decimal(800000)
    assert facts["contribution_margin_rate"] == Decimal(1)
