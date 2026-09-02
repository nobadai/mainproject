"""Finance P0의 결정론적 재무 판정 규칙."""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Literal, TypedDict, cast

from app.finance.schemas import FinalVerdict, RuntimeStatus

Verdict = FinalVerdict
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


def classify_base_stress(*, base_safe: bool, stress_safe: bool) -> str:
    """대안 BASE/STRESS Projection을 Finance 업무 계약으로 매핑한다."""
    if not base_safe and stress_safe:
        raise ValueError("BASE unsafe with STRESS safe is an invalid Finance scenario")
    if not base_safe:
        return "reject"
    return "ok" if stress_safe else "conditional"

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


class FinanceRuntimeRuleResult(TypedDict):
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    max_feasible_amount_krw: Decimal | None
    hard_constraints: list[HardConstraint]
    soft_warnings: list[SoftWarning]


class FinanceSalesRuleResult(TypedDict):
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    hard_constraints: list[HardConstraint]
    soft_warnings: list[FinanceRuntimeSoftWarning]


def has_required_finance_state(finance_state: Mapping[str, object] | None) -> bool:
    """Finance Rule 판단과 계산에 필요한 State 필드의 존재를 확인한다."""
    return finance_state is not None and all(
        field in finance_state and finance_state[field] is not None
        for field in _REQUIRED_FINANCE_STATE_FIELDS
    )


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
            "verdict": None,
            "max_feasible_amount_krw": None,
            "hard_constraints": ["REQUIRED_FINANCE_STATE_MISSING"],
            "soft_warnings": soft_warnings,
        }

    if not policy_available:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "verdict": None,
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
            "verdict": None,
            "max_feasible_amount_krw": None,
            "hard_constraints": ["AS_OF_MISMATCH"],
            "soft_warnings": soft_warnings,
        }
    if unresolved_sources:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "verdict": None,
            "max_feasible_amount_krw": None,
            "hard_constraints": ["CASH_EVENT_SOURCE_UNRESOLVED"],
            "soft_warnings": soft_warnings,
        }
    if projected_cash_min is None or minimum_cash_balance is None or max_feasible_amount is None:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "verdict": None,
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
        "verdict": "FAIL" if hard_constraints else "PASS",
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
            "verdict": None,
            "hard_constraints": hard_constraints,
            "soft_warnings": list(base_result["soft_warnings"]),
        }
    if post_h1_projected_cash_min is None or minimum_cash_balance is None:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "verdict": None,
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
        "verdict": "FAIL" if hard_constraints else "PASS",
        "hard_constraints": hard_constraints,
        "soft_warnings": [],
    }


# ---------------------------------------------------------------------------
# Sales Core Phase 4 — 판매 재무 판정 규칙
#
# ★ 계산과 판정을 갈라 둔다. 계산(`tools`)은 사실만 만들고, PASS·REVIEW_REQUIRED·
#   FAIL 은 여기서만 나온다.
#
# ★ **정책 숫자를 여기서 만들지 않는다.** 판매 마진 임계값과 Finance 최대 허용
#   결제일수는 현재 저장소 어디에도 권위 있는 값이 없다 — `FinancePolicy` 의 닫힌
#   키 목록에도, `agent_policy_config` 의 finance domain 에도 없다. 값이 없으면
#   추측하지 않고 RUNTIME_NOT_READY 로 닫는다. **없는 정책은 FAIL 이 아니다.**
#
# ★ Purchase 의 `margin_defense_floor_rate` 를 판매 마진 임계값으로 쓰지 않는다 —
#   매입 방어선과 판매 수익성 기준은 같은 숫자가 아니다.
# ---------------------------------------------------------------------------

SalesRuleId = Literal[
    "FIN-SALES-AMOUNT",
    "FIN-SALES-MARGIN",
    "FIN-SALES-PAYMENT-TERM",
    "FIN-SALES-CASHFLOW",
    "FIN-SALES-CREDIT",
    "FIN-SALES-COLLECTION-RISK",
]


class SalesRuleResult(TypedDict):
    """규칙 1건의 결정론적 결과.

    `verdict is None` 은 **판정할 수 없었다**는 뜻이고, 왜인지는 `runtime_status`
    가 가른다.

        RUNTIME_NOT_READY     Finance 가 가진 정책/데이터가 없다 (Finance 쪽 사정)
        READY + verdict None  사실 자체가 판정을 허락하지 않는다 (예: 매출 0원)
    """

    rule_id: SalesRuleId
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    reason_codes: tuple[str, ...]
    missing_policy: tuple[str, ...]


class SalesAggregateResult(TypedDict):
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    reason_codes: tuple[str, ...]
    missing_policy: tuple[str, ...]
    rule_results: tuple[SalesRuleResult, ...]


def _sales_rule(
    rule_id: SalesRuleId,
    *,
    runtime_status: RuntimeStatus = "READY",
    verdict: FinalVerdict | None,
    reason_codes: tuple[str, ...],
    missing_policy: tuple[str, ...] = (),
) -> SalesRuleResult:
    return {
        "rule_id": rule_id,
        "runtime_status": runtime_status,
        "verdict": verdict,
        "reason_codes": reason_codes,
        "missing_policy": missing_policy,
    }


def evaluate_sales_amount_integrity(
    *,
    reported_amount_krw: Decimal,
    recalculated_amount_krw: Decimal,
) -> SalesRuleResult:
    """보고 매출액과 Finance 재계산액의 정확한 항등을 본다.

    허용오차를 두지 않는다 — 현행 Finance 계약에 판매 금액 허용오차가 없다.
    현재 Sales 회신 계약에는 할인/총액-순액 축이 없으므로 재계산 항등도 하나뿐이다.
    할인 축이 계약에 생기면 그때 항등을 늘린다.
    """
    if reported_amount_krw == recalculated_amount_krw:
        return _sales_rule(
            "FIN-SALES-AMOUNT", verdict="PASS", reason_codes=("SALES_AMOUNT_MATCH",)
        )
    return _sales_rule(
        "FIN-SALES-AMOUNT", verdict="FAIL", reason_codes=("SALES_AMOUNT_MISMATCH",)
    )


def evaluate_sales_margin_rule(
    *,
    contribution_margin_rate: Decimal | None,
    finance_minimum_margin_rate: Decimal | None,
    finance_warning_margin_rate: Decimal | None,
) -> SalesRuleResult:
    """마진율을 권위 있는 판매 마진 정책 두 값에 견준다.

        rate < hard floor              FAIL
        hard floor <= rate < warning   REVIEW_REQUIRED
        rate >= warning                PASS

    두 정책 중 하나라도 없으면 판정하지 않는다. 음수 마진율 자체는 계산 사실이라
    그대로 정책에 견주고, 정책이 없다는 이유로 FAIL 을 만들지 않는다.
    """
    missing = tuple(
        name
        for name, value in (
            ("finance_minimum_margin_rate", finance_minimum_margin_rate),
            ("finance_warning_margin_rate", finance_warning_margin_rate),
        )
        if value is None
    )
    if missing:
        return _sales_rule(
            "FIN-SALES-MARGIN",
            runtime_status="RUNTIME_NOT_READY",
            verdict=None,
            reason_codes=("REQUIRED_FINANCE_POLICY_MISSING",),
            missing_policy=missing,
        )
    if finance_warning_margin_rate < finance_minimum_margin_rate:
        raise ValueError("finance_warning_margin_rate must not be below the minimum rate")

    if contribution_margin_rate is None:
        # 매출액 0 등으로 이익률을 계산할 수 없다 — 지어내지 않고 판정을 미룬다.
        return _sales_rule(
            "FIN-SALES-MARGIN",
            verdict=None,
            reason_codes=("SALES_MARGIN_RATE_UNCOMPUTABLE",),
        )
    if contribution_margin_rate < finance_minimum_margin_rate:
        return _sales_rule(
            "FIN-SALES-MARGIN", verdict="FAIL", reason_codes=("SALES_MARGIN_BELOW_MINIMUM",)
        )
    if contribution_margin_rate < finance_warning_margin_rate:
        return _sales_rule(
            "FIN-SALES-MARGIN",
            verdict="REVIEW_REQUIRED",
            reason_codes=("SALES_MARGIN_BELOW_WARNING",),
        )
    return _sales_rule(
        "FIN-SALES-MARGIN", verdict="PASS", reason_codes=("SALES_MARGIN_MEETS_WARNING",)
    )


def evaluate_sales_payment_term_rule(
    *,
    payment_terms_type: str,
    payment_days: int | None,
    max_finance_allowed_payment_terms_days: int | None,
) -> SalesRuleResult:
    """결제조건이 Finance 최대 허용 결제일수 안인지 본다.

    INSTALLMENT 는 권위 있는 분할결제 정책이 없어 판정하지 않는다 — 조용히 SINGLE
    로 바꾸지 않는다. Finance 권고 결제일수를 지어내지도 않는다.
    """
    if payment_terms_type != "SINGLE":
        return _sales_rule(
            "FIN-SALES-PAYMENT-TERM",
            runtime_status="RUNTIME_NOT_READY",
            verdict=None,
            reason_codes=("SALES_PAYMENT_TERM_TYPE_UNSUPPORTED",),
            missing_policy=("sales_installment_payment_policy",),
        )
    if max_finance_allowed_payment_terms_days is None:
        return _sales_rule(
            "FIN-SALES-PAYMENT-TERM",
            runtime_status="RUNTIME_NOT_READY",
            verdict=None,
            reason_codes=("REQUIRED_FINANCE_POLICY_MISSING",),
            missing_policy=("max_finance_allowed_payment_terms_days",),
        )
    if payment_days is None:
        # null 은 0 도 아니고 "제한 없음"도 아니다 — 판정할 사실이 없다.
        return _sales_rule(
            "FIN-SALES-PAYMENT-TERM",
            verdict=None,
            reason_codes=("SALES_PAYMENT_DAYS_ABSENT",),
        )
    if payment_days < 0:
        raise ValueError("payment_days must not be negative")
    if payment_days <= max_finance_allowed_payment_terms_days:
        return _sales_rule(
            "FIN-SALES-PAYMENT-TERM",
            verdict="PASS",
            reason_codes=("SALES_PAYMENT_TERM_WITHIN_LIMIT",),
        )
    return _sales_rule(
        "FIN-SALES-PAYMENT-TERM",
        verdict="FAIL",
        reason_codes=("SALES_PAYMENT_TERM_EXCEEDS_LIMIT",),
    )


def evaluate_sales_cashflow_rule(
    *,
    base_projected_cash_min: Decimal,
    scenario_projected_cash_min: Decimal,
    minimum_cash_balance_krw: Decimal,
    depends_on_projected_inflow: bool,
    collection_within_horizon: bool,
) -> SalesRuleResult:
    """BASE 와 SCENARIO 를 각각 최소 현금 정책에 견준다.

    ★ 제안 유입은 확정 현금이 아니다. SCENARIO 최저 현금이 그 유입 덕분에만 기준을
      넘는다면(`depends_on_projected_inflow`) 통과가 아니라 REVIEW_REQUIRED 다 —
      아직 들어오지 않은 돈으로 안전하다고 말하지 않는다. 이것은 새 숫자 정책이
      아니라 BASE/SCENARIO 분리에서 곧바로 따라오는 구조 규칙이다.
    """
    reasons: list[str] = []
    if not collection_within_horizon:
        reasons.append("SALES_COLLECTION_OUTSIDE_HORIZON")

    if base_projected_cash_min < minimum_cash_balance_krw:
        return _sales_rule(
            "FIN-SALES-CASHFLOW",
            verdict="FAIL",
            reason_codes=(*reasons, "BASE_MINIMUM_CASH_VIOLATED"),
        )
    if scenario_projected_cash_min < minimum_cash_balance_krw:
        return _sales_rule(
            "FIN-SALES-CASHFLOW",
            verdict="FAIL",
            reason_codes=(*reasons, "SCENARIO_MINIMUM_CASH_VIOLATED"),
        )
    if depends_on_projected_inflow:
        return _sales_rule(
            "FIN-SALES-CASHFLOW",
            verdict="REVIEW_REQUIRED",
            reason_codes=(*reasons, "SALES_CASHFLOW_DEPENDS_ON_PROJECTED_INFLOW"),
        )
    return _sales_rule(
        "FIN-SALES-CASHFLOW",
        verdict="PASS",
        reason_codes=(*reasons, "SALES_CASHFLOW_SAFE"),
    )


def aggregate_sales_finance_rules(
    rule_results: Sequence[SalesRuleResult],
) -> SalesAggregateResult:
    """하위 규칙 결과만으로 종합 판정을 만든다 (LLM 이 종합하지 않는다).

        ERROR 있음                    → ERROR · verdict None
        RUNTIME_NOT_READY 있음        → RUNTIME_NOT_READY · verdict None
        판정 불가(verdict None) 있음  → READY · verdict None
        FAIL 있음                     → FAIL
        REVIEW_REQUIRED 있음          → REVIEW_REQUIRED
        그 외                         → PASS

    어느 규칙 때문인지 감추지 않는다 — 개별 결과와 reason code 를 그대로 나른다.
    """
    if not rule_results:
        raise ValueError("aggregate requires at least one rule result")

    results = tuple(rule_results)
    reason_codes = tuple(code for result in results for code in result["reason_codes"])
    missing_policy = tuple(name for result in results for name in result["missing_policy"])
    statuses = {result["runtime_status"] for result in results}

    for blocking in ("ERROR", "RUNTIME_NOT_READY"):
        if blocking in statuses:
            return {
                "runtime_status": cast(RuntimeStatus, blocking),
                "verdict": None,
                "reason_codes": reason_codes,
                "missing_policy": missing_policy,
                "rule_results": results,
            }

    verdicts = [result["verdict"] for result in results]
    aggregate: FinalVerdict | None
    if any(verdict is None for verdict in verdicts):
        aggregate = None
    elif "FAIL" in verdicts:
        aggregate = "FAIL"
    elif "REVIEW_REQUIRED" in verdicts:
        aggregate = "REVIEW_REQUIRED"
    else:
        aggregate = "PASS"
    return {
        "runtime_status": "READY",
        "verdict": aggregate,
        "reason_codes": reason_codes,
        "missing_policy": missing_policy,
        "rule_results": results,
    }


# ---------------------------------------------------------------------------
# Sales Core Phase 5 — 여신 여력 · 회수 위험
#
# ★ **여신한도는 저장소에 없다.** `partners` 에도 `agent_policy_config` 에도
#   credit_limit 컬럼이 없다. 그래서 아래 두 규칙은 오늘 항상 닫힌다.
#   닫히는 것과 FAIL 은 다르다 — 한도를 모르는 것은 거래처 잘못이 아니다.
#
# ★ 거래이력이 없다는 이유만으로 신규 거래처를 FAIL 로 만들지 않는다. 채권 0원은
#   **사실**이고, 판정을 막는 것은 언제나 없는 정책 쪽이다.
# ---------------------------------------------------------------------------



def evaluate_receivable_capacity_rule(
    *,
    projected_partner_ar_krw: Decimal,
    credit_limit_krw: Decimal | None,
) -> SalesRuleResult:
    """제안 성사 후 거래처 채권이 권위 있는 여신한도 안인지 본다.

        projected AR <= limit   PASS
        projected AR >  limit   FAIL

    한도가 없으면 판정하지 않는다 — 회사 현금·판매이력·마진에서 한도를 역산하지
    않는다.
    """
    if projected_partner_ar_krw < 0:
        raise ValueError("projected_partner_ar_krw must not be negative")
    if credit_limit_krw is None:
        return _sales_rule(
            "FIN-SALES-CREDIT",
            runtime_status="RUNTIME_NOT_READY",
            verdict=None,
            reason_codes=("REQUIRED_FINANCE_POLICY_MISSING",),
            missing_policy=("partner_credit_limit_krw",),
        )
    if credit_limit_krw < 0:
        raise ValueError("credit_limit_krw must not be negative")
    if projected_partner_ar_krw <= credit_limit_krw:
        return _sales_rule(
            "FIN-SALES-CREDIT",
            verdict="PASS",
            reason_codes=("SALES_CREDIT_WITHIN_LIMIT",),
        )
    return _sales_rule(
        "FIN-SALES-CREDIT",
        verdict="FAIL",
        reason_codes=("SALES_CREDIT_LIMIT_EXCEEDED",),
    )


def evaluate_collection_risk_rule(
    *,
    overdue_ar_krw: Decimal,
    collection_risk_policy: Mapping[str, object] | None = None,
) -> SalesRuleResult:
    """회수 위험 판정 — 권위 있는 임계값/가중치가 없으면 점수를 만들지 않는다.

    ★ 연체 금액 같은 **사실**은 이미 `summarize_partner_receivables` 가 계산해
      두었고 그대로 밖으로 나간다. 여기서 막는 것은 그 사실을 등급·점수로 바꾸는
      일이다. 가중치 없는 점수는 숫자처럼 보이는 추측이다.
    """
    if overdue_ar_krw < 0:
        raise ValueError("overdue_ar_krw must not be negative")
    if collection_risk_policy is None:
        return _sales_rule(
            "FIN-SALES-COLLECTION-RISK",
            runtime_status="RUNTIME_NOT_READY",
            verdict=None,
            reason_codes=("REQUIRED_FINANCE_POLICY_MISSING",),
            missing_policy=("sales_collection_risk_policy",),
        )
    raise NotImplementedError(
        "collection risk scoring requires an authoritative threshold/weight contract"
    )
