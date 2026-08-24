"""Finance P0의 결정론적 재무 계산 도구."""

from decimal import Decimal
from typing import TypedDict

from app.finance.schemas import PurchaseSourcingPlanItem, SourcingPlanItem

KG_PER_TON = Decimal(1000)


class ExpectedCostComparison(TypedDict):
    is_match: bool
    expected_cost: Decimal
    recalculated_cost: Decimal
    difference: Decimal


class ReportedAmountComparison(TypedDict):
    is_match: bool
    reported_amount_krw: Decimal
    recalculated_amount_krw: Decimal
    difference: Decimal


def calculate_proposal_amount(sourcing_plan: list[SourcingPlanItem]) -> Decimal:
    """톤 단위 수량과 kg당 단가로 매입 제안 총액을 재계산한다."""
    return sum(
        (item.quantity_ton * KG_PER_TON * Decimal(item.unit_price) for item in sourcing_plan),
        start=Decimal(0),
    )


def calculate_purchase_scenario_amount(
    sourcing_plan: list[PurchaseSourcingPlanItem],
) -> Decimal:
    """Purchase Agent v0.4 소싱 계획의 총 매입금액을 재계산한다."""
    return sum(
        (item.quantity_ton * KG_PER_TON * Decimal(item.grade_unit_price) for item in sourcing_plan),
        start=Decimal(0),
    )


def compare_expected_cost(
    expected_cost: int | Decimal,
    recalculated_cost: Decimal,
) -> ExpectedCostComparison:
    """매입 Agent의 예상 비용과 Finance 재계산 비용을 비교한다."""
    expected_cost_decimal = Decimal(expected_cost)
    difference = recalculated_cost - expected_cost_decimal
    return {
        "is_match": difference == Decimal(0),
        "expected_cost": expected_cost_decimal,
        "recalculated_cost": recalculated_cost,
        "difference": difference,
    }


def compare_reported_amount(
    reported_amount_krw: Decimal,
    recalculated_amount_krw: Decimal,
) -> ReportedAmountComparison:
    """Purchase Agent v0.4 보고 금액과 Finance 재계산 금액을 비교한다."""
    difference = recalculated_amount_krw - reported_amount_krw
    return {
        "is_match": difference == Decimal(0),
        "reported_amount_krw": reported_amount_krw,
        "recalculated_amount_krw": recalculated_amount_krw,
        "difference": difference,
    }


def calculate_financial_limit(
    current_cash: Decimal,
    minimum_operating_cash: Decimal,
    committed_outflows: Decimal,
    unsettled_purchase_payables: Decimal,
) -> Decimal:
    """필수 현금과 확정 지출을 차감한 재무 한도를 계산한다."""
    return current_cash - minimum_operating_cash - committed_outflows - unsettled_purchase_payables


def calculate_post_purchase_cash(
    current_cash: Decimal,
    proposal_amount: Decimal,
    committed_outflows: Decimal,
    unsettled_purchase_payables: Decimal,
) -> Decimal:
    """제안 매입과 확정 지출 이후의 현금을 계산한다."""
    return current_cash - proposal_amount - committed_outflows - unsettled_purchase_payables
