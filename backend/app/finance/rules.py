"""Finance P0의 결정론적 재무 판정 규칙."""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Literal, TypedDict, cast

from app.finance.schemas import RuntimeStatus
from app.finance.tools import ExpectedCostComparison

Verdict = Literal["ok", "conditional", "reject"]
HardConstraint = Literal[
    "FINANCIAL_LIMIT_EXCEEDED",
    "NO_FINANCIAL_CAPACITY",
    "REQUIRED_FINANCE_STATE_MISSING",
    "AS_OF_MISMATCH",
    "FIN-H01_MINIMUM_CASH_BALANCE",
    "CASH_EVENT_SOURCE_UNRESOLVED",
    "REQUIRED_FINANCE_POLICY_MISSING",
]
SoftWarning = Literal["COST_MISMATCH"]
FinanceRuntimeSoftWarning = Literal[
    "COST_MISMATCH",
    "CASH_PRIORITY_POLICY_UNRESOLVED",
]

_REQUIRED_FINANCE_STATE_FIELDS = (
    "finance_state_id",
    "sim_run_id",
    "state_date",
    "state_type",
    "financing_mode",
    "current_cash_krw",
    "minimum_operating_cash_krw",
    "committed_outflows_krw",
    "unsettled_purchase_payables_krw",
    "financial_limit_krw",
)


class FinanceRuleResult(TypedDict):
    verdict: Verdict
    max_feasible_amount_krw: Decimal | None
    hard_constraints: list[HardConstraint]
    soft_warnings: list[SoftWarning]


class FinanceRuntimeRuleResult(TypedDict):
    runtime_status: RuntimeStatus
    max_feasible_amount_krw: Decimal | None
    hard_constraints: list[HardConstraint]
    soft_warnings: list[SoftWarning]


class FinanceSalesRuleResult(TypedDict):
    runtime_status: RuntimeStatus
    hard_constraints: list[HardConstraint]
    soft_warnings: list[FinanceRuntimeSoftWarning]


def has_required_finance_state(finance_state: Mapping[str, object] | None) -> bool:
    """Finance Rule 판단과 계산에 필요한 State 필드의 존재를 확인한다."""
    return finance_state is not None and all(
        field in finance_state and finance_state[field] is not None
        for field in _REQUIRED_FINANCE_STATE_FIELDS
    )


def evaluate_finance_rules(
    *,
    purchase_as_of: date,
    proposal_amount: Decimal,
    expected_cost_comparison: ExpectedCostComparison,
    finance_state: Mapping[str, object] | None,
) -> FinanceRuleResult:
    """계산 완료된 매입금액과 Finance State를 우선순위에 따라 판정한다."""
    soft_warnings: list[SoftWarning] = []
    if not expected_cost_comparison["is_match"]:
        soft_warnings.append("COST_MISMATCH")

    if not has_required_finance_state(finance_state):
        return {
            "verdict": "reject",
            "max_feasible_amount_krw": None,
            "hard_constraints": ["REQUIRED_FINANCE_STATE_MISSING"],
            "soft_warnings": soft_warnings,
        }

    finance_state = cast(Mapping[str, object], finance_state)
    state_date = finance_state["state_date"]
    financial_limit = finance_state["financial_limit_krw"]
    if not isinstance(state_date, date):
        raise TypeError("finance_state.state_date must be a date")
    if not isinstance(financial_limit, Decimal):
        raise TypeError("finance_state.financial_limit_krw must be a Decimal")

    if purchase_as_of != state_date:
        return {
            "verdict": "reject",
            "max_feasible_amount_krw": None,
            "hard_constraints": ["AS_OF_MISMATCH"],
            "soft_warnings": soft_warnings,
        }
    if financial_limit <= Decimal(0):
        return {
            "verdict": "reject",
            "max_feasible_amount_krw": Decimal(0),
            "hard_constraints": ["NO_FINANCIAL_CAPACITY"],
            "soft_warnings": soft_warnings,
        }
    if proposal_amount > financial_limit:
        return {
            "verdict": "conditional",
            "max_feasible_amount_krw": financial_limit,
            "hard_constraints": ["FINANCIAL_LIMIT_EXCEEDED"],
            "soft_warnings": soft_warnings,
        }
    return {
        "verdict": "ok",
        "max_feasible_amount_krw": financial_limit,
        "hard_constraints": [],
        "soft_warnings": soft_warnings,
    }


def evaluate_finance_runtime_rules(
    *,
    as_of: date,
    finance_state: Mapping[str, object] | None,
    has_cost_mismatch: bool = False,
    projected_cash_min: Decimal | None = None,
    minimum_cash_balance: Decimal | None = None,
    max_feasible_amount: Decimal | None = None,
    policy_available: bool = True,
    unresolved_sources: tuple[str, ...] = (),
) -> FinanceRuntimeRuleResult:
    """Finance A의 실행 상태와 전사 공통 매입 가능 Band를 결정한다."""
    soft_warnings: list[SoftWarning] = []
    if has_cost_mismatch:
        soft_warnings.append("COST_MISMATCH")

    if not has_required_finance_state(finance_state):
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "max_feasible_amount_krw": None,
            "hard_constraints": ["REQUIRED_FINANCE_STATE_MISSING"],
            "soft_warnings": soft_warnings,
        }

    if not policy_available:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "max_feasible_amount_krw": None,
            "hard_constraints": ["REQUIRED_FINANCE_POLICY_MISSING"],
            "soft_warnings": soft_warnings,
        }

    finance_state = cast(Mapping[str, object], finance_state)
    state_date = finance_state["state_date"]
    financial_limit = finance_state["financial_limit_krw"]
    if not isinstance(state_date, date):
        raise TypeError("finance_state.state_date must be a date")
    if not isinstance(financial_limit, Decimal):
        raise TypeError("finance_state.financial_limit_krw must be a Decimal")

    if as_of != state_date:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "max_feasible_amount_krw": None,
            "hard_constraints": ["AS_OF_MISMATCH"],
            "soft_warnings": soft_warnings,
        }
    if unresolved_sources:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "max_feasible_amount_krw": None,
            "hard_constraints": ["CASH_EVENT_SOURCE_UNRESOLVED"],
            "soft_warnings": soft_warnings,
        }
    if projected_cash_min is None or minimum_cash_balance is None or max_feasible_amount is None:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "max_feasible_amount_krw": None,
            "hard_constraints": ["REQUIRED_FINANCE_POLICY_MISSING"],
            "soft_warnings": soft_warnings,
        }
    hard_constraints: list[HardConstraint] = []
    if projected_cash_min < minimum_cash_balance:
        hard_constraints.append("FIN-H01_MINIMUM_CASH_BALANCE")
    if max_feasible_amount <= Decimal(0):
        hard_constraints.append("NO_FINANCIAL_CAPACITY")
    return {
        "runtime_status": "READY",
        "max_feasible_amount_krw": max_feasible_amount,
        "hard_constraints": hard_constraints,
        "soft_warnings": soft_warnings,
    }


def evaluate_finance_sales_rules(
    *,
    as_of: date,
    finance_state: Mapping[str, object] | None,
    base_projected_cash_min: Decimal | None,
    post_h1_projected_cash_min: Decimal | None,
    minimum_cash_balance: Decimal | None,
    policy_available: bool = True,
    unresolved_sources: tuple[str, ...] = (),
) -> FinanceSalesRuleResult:
    """Finance B 실행 경계와 H1 이후 FIN-H01을 판정한다."""
    base_result = evaluate_finance_runtime_rules(
        as_of=as_of,
        finance_state=finance_state,
        projected_cash_min=base_projected_cash_min,
        minimum_cash_balance=minimum_cash_balance,
        max_feasible_amount=Decimal(0) if base_projected_cash_min is not None else None,
        policy_available=policy_available,
        unresolved_sources=unresolved_sources,
    )
    hard_constraints = [
        item for item in base_result["hard_constraints"] if item != "NO_FINANCIAL_CAPACITY"
    ]
    if base_result["runtime_status"] != "READY":
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "hard_constraints": hard_constraints,
            "soft_warnings": list(base_result["soft_warnings"]),
        }
    if post_h1_projected_cash_min is None or minimum_cash_balance is None:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "hard_constraints": ["REQUIRED_FINANCE_POLICY_MISSING"],
            "soft_warnings": [],
        }
    if (
        post_h1_projected_cash_min < minimum_cash_balance
        and "FIN-H01_MINIMUM_CASH_BALANCE" not in hard_constraints
    ):
        hard_constraints.append("FIN-H01_MINIMUM_CASH_BALANCE")
    return {
        "runtime_status": "READY",
        "hard_constraints": hard_constraints,
        "soft_warnings": [],
    }
