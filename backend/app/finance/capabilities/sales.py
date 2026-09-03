"""판매 재무 검증 Capability — **결정론적이다. LLM 이 들어오지 않는다.**

이 파일이 소유하는 것
    Sales 회신 payload → Finance 내부 모델 파싱 · 없는 사실 식별 ·
    계산(`tools`)과 판정(`rules`)의 조립 · 자기 완결적 내부 결과

여기 **없는 것**
    산술 공식 · 판정 임계값 · Master AgentReply · Planner/Finalizer 호출
    → `tools` · `rules` · `adapter` 소유다.

★ **없는 것을 채우지 않는다.** 판매 마진 정책 · 최대 결제일수 · 여신한도 ·
  회수위험 임계값은 현재 저장소에 권위 있는 값이 없다. 그때 이 계층이 하는 일은
  성공한 척이 아니라 **무엇이 없어서 못 했는지 이름을 대는 것**이다.

★ 두 가지 "못 했다"를 섞지 않는다.

  ```text
  INPUT_INCOMPLETE    제안에 사실이 빠졌다 (Sales/Master 쪽) — Finance 는 멀쩡하다
  RUNTIME_NOT_READY   Finance 가 가진 정책/데이터가 없다 (Finance 쪽)
  ```

  섞으면 영업이 못 고치는 것을 고치라는 말이 되고, 재무가 고쳐야 할 것이 영업
  탓으로 넘어간다.
"""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.finance.capabilities.procurement import load_context
from app.finance.db import FinanceAsOfDataPort
from app.finance.rules import (
    SalesRuleResult,
    aggregate_sales_finance_rules,
    evaluate_collection_risk_rule,
    evaluate_receivable_capacity_rule,
    evaluate_sales_amount_integrity,
    evaluate_sales_cashflow_rule,
    evaluate_sales_margin_rule,
    evaluate_sales_payment_term_rule,
)
from app.finance.sales_models import (
    InventoryCostBasis,
    PartnerReceivableFacts,
    SalesFinancialSummary,
    SalesScenarioCashflow,
    SalesSupply,
    SalesValidationInput,
    SalesValidationResult,
    VerifiedDirectCost,
)
from app.finance.tools import (
    build_proposed_sales_collection_event,
    calculate_available_credit,
    calculate_collection_date,
    calculate_contribution_margin,
    calculate_contribution_margin_rate,
    calculate_projected_partner_ar,
    calculate_sales_amount,
    compare_reported_sales_amount,
    compose_sales_cost_basis,
    project_sales_scenario_cashflow,
)

#: Finance 가 판매 제안 하나를 판정하려면 반드시 있어야 하는 Sales 유래 사실.
#: 이 중 하나라도 없으면 Finance 고장이 아니라 제안이 미완성이다.
REQUIRED_SALES_INPUT_FIELDS: tuple[str, ...] = (
    "scenario_id",
    "partner_id",
    "item",
    "quantity_kg",
    "unit_price_krw",
    "reported_sales_amount_krw",
    "payment_terms_type",
    "source_ref",
)


# ---------------------------------------------------------------------------
# 입력 파싱 — Sales 회신 payload 를 Finance 사실로 옮긴다
# ---------------------------------------------------------------------------


def parse_sales_validation_input(
    payload: Mapping[str, Any],
) -> tuple[SalesValidationInput | None, tuple[str, ...]]:
    """Sales 회신 payload 에서 Finance 가 쓰는 부분집합만 엄격히 읽는다.

    payload 에 Finance 가 안 쓰는 키가 더 있어도 된다 — Master 는 회신을 통째로
    넘기고, 무엇이 필요한지는 Finance 가 정한다. 반환값의 두 번째 항목은 없는
    필드 이름이며, 비어 있지 않으면 첫 항목은 ``None`` 이다.
    """
    missing = [
        field
        for field in REQUIRED_SALES_INPUT_FIELDS
        if payload.get(field) is None or _is_blank(payload.get(field))
    ]
    if missing:
        return None, tuple(missing)

    try:
        parsed = SalesValidationInput(
            scenario_id=str(payload["scenario_id"]),
            partner_id=str(payload["partner_id"]),
            item=str(payload["item"]),
            quantity_kg=_decimal(payload["quantity_kg"]),
            unit_price_krw=_decimal(payload["unit_price_krw"]),
            reported_sales_amount_krw=_decimal(payload["reported_sales_amount_krw"]),
            payment_terms_type=str(payload["payment_terms_type"]),
            payment_days=_optional_int(payload.get("payment_days")),
            collection_reference_date=_optional_date(payload.get("collection_reference_date")),
            supply=_parse_supply(payload.get("supply")),
            inventory_cost_basis=_parse_inventory_cost_basis(
                payload.get("inventory_cost_basis")
            ),
            direct_costs=_parse_direct_costs(payload.get("direct_costs")),
            source_ref=str(payload["source_ref"]),
        )
    except (ValueError, TypeError, InvalidOperation):
        # 값이 있긴 한데 Finance 가 쓸 수 있는 모양이 아니다 — 지어내지 않는다.
        return None, ("sales_payload_not_parseable",)
    return parsed, ()


def _is_blank(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("boolean values are not valid numeric inputs")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # 업무 숫자를 float 로 받지 않는다 — 이미 정밀도를 잃은 값이다.
        raise TypeError("float is not an accepted business numeric input")
    return Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("boolean values are not valid numeric inputs")
    return int(value)


def _optional_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_supply(value: Any) -> SalesSupply | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("supply must be a mapping")
    raw_conditional = value.get("conditional_quantity_kg")
    return SalesSupply(
        confirmed_quantity_kg=_decimal(value["confirmed_quantity_kg"]),
        # 🔴 없는 칸을 0 으로 읽지 않는다. 보내는 쪽이 확정 물량만 알고 조건부 물량을
        #   모를 수 있는데, 그것을 "조건부 0" 으로 바꾸면 모르는 것이 사실이 된다.
        conditional_quantity_kg=(
            None if raw_conditional is None else _decimal(raw_conditional)
        ),
        dependency_ref=(
            None if value.get("dependency_ref") is None else str(value["dependency_ref"])
        ),
    )


def _parse_inventory_cost_basis(value: Any) -> InventoryCostBasis | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("inventory_cost_basis must be a mapping")
    return InventoryCostBasis(
        amount_krw=_decimal(value["amount_krw"]),
        cost_method=str(value["cost_method"]),
        included_components=tuple(str(item) for item in value.get("included_components", ())),
        source_ref=str(value["source_ref"]),
        evidence_grade=str(value["evidence_grade"]),
    )


def _parse_direct_costs(value: Any) -> tuple[VerifiedDirectCost, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("direct_costs must be a sequence")
    return tuple(
        VerifiedDirectCost(
            component=str(item["component"]),
            amount_krw=_decimal(item["amount_krw"]),
            cost_method=str(item["cost_method"]),
            source_ref=str(item["source_ref"]),
            evidence_grade=str(item["evidence_grade"]),
        )
        for item in value
    )


# ---------------------------------------------------------------------------
# 개별 Capability — 각자 자기 사실만 만든다
# ---------------------------------------------------------------------------


def assess_sales_finance_position(
    sales_input: SalesValidationInput,
) -> dict[str, Any]:
    """매출액을 재계산하고 보고 금액과 대조한다 (Finance 가 숫자를 다시 만든다)."""
    recalculated = calculate_sales_amount(
        quantity_kg=sales_input.quantity_kg, unit_price_krw=sales_input.unit_price_krw
    )
    comparison = compare_reported_sales_amount(
        reported_amount_krw=sales_input.reported_sales_amount_krw,
        recalculated_amount_krw=recalculated,
    )
    return {
        "recalculated_sales_amount_krw": recalculated,
        "comparison": comparison,
        "rule": evaluate_sales_amount_integrity(
            reported_amount_krw=sales_input.reported_sales_amount_krw,
            recalculated_amount_krw=recalculated,
        ),
        "evidence_refs": (sales_input.source_ref,),
    }


def evaluate_sales_margin(
    sales_input: SalesValidationInput,
    *,
    sales_amount_krw: Decimal,
    finance_minimum_margin_rate: Decimal | None,
    finance_warning_margin_rate: Decimal | None,
) -> dict[str, Any]:
    """원가 기준을 합성해 공헌이익과 이익률을 구하고 정책에 견준다.

    ★ 권위 있는 재고원가가 없으면 마진을 만들지 않는다. 조건부 물량이 섞여 있으면
      확정 재고원가를 제안 **전체**의 원가처럼 쓰지 않는다 — 두 경우 모두 0으로
      대체하지 않고 없는 사실의 이름을 남긴다.
    """
    missing_data: list[str] = []
    supply = sales_input.supply

    cost_basis = compose_sales_cost_basis(
        inventory_cost_basis=sales_input.inventory_cost_basis,
        direct_costs=sales_input.direct_costs,
    )
    if cost_basis is None:
        missing_data.append("authoritative_inventory_cost_basis")
    elif supply is None:
        # 🔴 공급 자체를 못 받은 것은 **모름**이지 "조건부 공급 없음" 이 아니다.
        #    예전에는 여기서 조건부 0 으로 읽어, 확정 재고원가가 제안 전체를 덮는 것을
        #    막는 방어가 통째로 풀렸다. 공급을 모르면 그 판단을 할 수 없다 — fail closed.
        missing_data.append("sales_supply_context")
        cost_basis = None
    elif supply.conditional_quantity_kg is None:
        # 조건부 물량을 모르면 확정 재고원가가 제안 전체를 덮는지 알 수 없다.
        # 모르는 채로 덮으면 역마진이 마진처럼 보인다 — fail closed.
        missing_data.append("sales_supply_conditional_quantity")
        cost_basis = None
    elif supply.conditional_quantity_kg > 0:
        # 확정 재고원가는 확정 물량에 대한 사실이다. 조건부 물량까지 덮지 않는다.
        missing_data.append("sales_cost_basis_for_conditional_supply")
        cost_basis = None

    margin: Decimal | None = None
    margin_rate: Decimal | None = None
    if cost_basis is not None:
        margin = calculate_contribution_margin(
            sales_amount_krw=sales_amount_krw,
            sales_cost_basis_krw=cost_basis.amount_krw,
        )
        margin_rate = calculate_contribution_margin_rate(
            sales_amount_krw=sales_amount_krw, contribution_margin_krw=margin
        )

    rule = evaluate_sales_margin_rule(
        contribution_margin_rate=margin_rate,
        finance_minimum_margin_rate=finance_minimum_margin_rate,
        finance_warning_margin_rate=finance_warning_margin_rate,
    )
    if cost_basis is None and rule["runtime_status"] == "READY":
        # 정책은 있는데 원가가 없다 — 판정할 사실이 없다는 것을 분명히 남긴다.
        rule = {**rule, "reason_codes": ("SALES_COST_BASIS_UNAVAILABLE",), "verdict": None}
    return {
        "cost_basis": cost_basis,
        "contribution_margin_krw": margin,
        "contribution_margin_rate": margin_rate,
        "rule": rule,
        "missing_data": tuple(missing_data),
        "evidence_refs": cost_basis.source_refs if cost_basis is not None else (),
    }


def evaluate_receivable_capacity(
    *,
    sales_amount_krw: Decimal,
    receivable_facts: PartnerReceivableFacts | None,
    credit_limit_krw: Decimal | None,
) -> dict[str, Any]:
    """제안 성사 후 거래처 채권을 권위 있는 여신한도에 견준다."""
    missing_data: list[str] = []
    if receivable_facts is None:
        missing_data.append("partner_receivable_facts")
    if credit_limit_krw is None:
        missing_data.append("partner_credit_limit_krw")

    current_ar = receivable_facts.current_ar_krw if receivable_facts is not None else None
    projected_ar = (
        None
        if current_ar is None
        else calculate_projected_partner_ar(
            current_partner_ar_krw=current_ar, proposed_sales_amount_krw=sales_amount_krw
        )
    )
    available = (
        None
        if credit_limit_krw is None or current_ar is None
        else calculate_available_credit(
            credit_limit_krw=credit_limit_krw, current_partner_ar_krw=current_ar
        )
    )
    if projected_ar is None:
        # ★ 채권 사실이 없으면 규칙에 0을 대신 넣지 않는다. 0원 채권은 사실이고,
        #   사실 없음은 사실이 아니다 — 자리를 메우면 둘이 같아진다.
        rule: SalesRuleResult = {
            "rule_id": "FIN-SALES-CREDIT",
            "runtime_status": "RUNTIME_NOT_READY",
            "verdict": None,
            "reason_codes": ("REQUIRED_FINANCE_POLICY_MISSING",),
            "missing_policy": tuple(missing_data),
        }
    else:
        rule = evaluate_receivable_capacity_rule(
            projected_partner_ar_krw=projected_ar,
            credit_limit_krw=credit_limit_krw,
        )
    return {
        "current_partner_ar_krw": current_ar,
        "projected_partner_ar_krw": projected_ar,
        "available_credit_krw": available,
        "rule": rule,
        "missing_data": tuple(missing_data),
        "evidence_refs": receivable_facts.source_refs if receivable_facts is not None else (),
    }


def evaluate_sales_cashflow(
    *,
    scenario_cashflow: SalesScenarioCashflow | None,
    minimum_cash_balance_krw: Decimal | None,
) -> dict[str, Any]:
    """BASE 와 SCENARIO 를 최소 현금 정책에 견준다 (제안 유입은 확정 현금이 아니다)."""
    missing_data: list[str] = []
    if scenario_cashflow is None:
        missing_data.append("sales_scenario_cashflow")
    if minimum_cash_balance_krw is None:
        missing_data.append("minimum_cash_balance_krw")
    if scenario_cashflow is None or minimum_cash_balance_krw is None:
        rule: SalesRuleResult = {
            "rule_id": "FIN-SALES-CASHFLOW",
            "runtime_status": "RUNTIME_NOT_READY",
            "verdict": None,
            "reason_codes": ("REQUIRED_FINANCE_POLICY_MISSING",),
            "missing_policy": tuple(missing_data),
        }
        return {"rule": rule, "missing_data": tuple(missing_data), "evidence_refs": ()}

    rule = evaluate_sales_cashflow_rule(
        base_projected_cash_min=scenario_cashflow.base_projected_cash_min,
        scenario_projected_cash_min=scenario_cashflow.scenario_projected_cash_min,
        minimum_cash_balance_krw=minimum_cash_balance_krw,
        depends_on_projected_inflow=scenario_cashflow.depends_on_projected_inflow,
        collection_within_horizon=scenario_cashflow.collection_within_horizon,
    )
    return {
        "rule": rule,
        "missing_data": (),
        "evidence_refs": (scenario_cashflow.proposed_collection_ref_id,),
    }


def assess_collection_risk(
    *,
    receivable_facts: PartnerReceivableFacts | None,
    collection_risk_policy: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """연체 사실은 나르고, 위험 등급/점수는 정책이 없으면 만들지 않는다."""
    overdue = receivable_facts.overdue_ar_krw if receivable_facts is not None else None
    if overdue is None:
        # ★ 연체액을 모르는 것과 연체가 0원인 것은 다른 사실이다. 0으로 메우지 않는다.
        rule: SalesRuleResult = {
            "rule_id": "FIN-SALES-COLLECTION-RISK",
            "runtime_status": "RUNTIME_NOT_READY",
            "verdict": None,
            "reason_codes": ("REQUIRED_FINANCE_POLICY_MISSING",),
            "missing_policy": ("partner_receivable_facts", "sales_collection_risk_policy"),
        }
    else:
        rule = evaluate_collection_risk_rule(
            overdue_ar_krw=overdue,
            collection_risk_policy=collection_risk_policy,
        )
    missing_data = () if receivable_facts is not None else ("partner_receivable_facts",)
    return {"overdue_ar_krw": overdue, "rule": rule, "missing_data": missing_data}


# ---------------------------------------------------------------------------
# 조립 — 위 결과만으로 종합한다
# ---------------------------------------------------------------------------


def evaluate_sales_scenario(
    payload: Mapping[str, Any],
    *,
    finance_minimum_margin_rate: Decimal | None = None,
    finance_warning_margin_rate: Decimal | None = None,
    max_finance_allowed_payment_terms_days: int | None = None,
    minimum_cash_balance_krw: Decimal | None = None,
    credit_limit_krw: Decimal | None = None,
    receivable_facts: PartnerReceivableFacts | None = None,
    scenario_cashflow: SalesScenarioCashflow | None = None,
    collection_risk_policy: Mapping[str, object] | None = None,
) -> SalesValidationResult:
    """Sales 제안 하나를 Finance 사실과 규칙으로 끝까지 검증한다.

    정책/사실이 없으면 그 이름을 `missing_data` 에, 제안에 빠진 것은
    `missing_fields` 에 남긴다. 둘 다 FAIL 이 아니다.
    """
    sales_input, missing_fields = parse_sales_validation_input(payload)
    if sales_input is None:
        return SalesValidationResult(
            scenario_id=_optional_scenario_id(payload),
            runtime_status="READY",
            status="INPUT_INCOMPLETE",
            finance_verdict=None,
            missing_fields=missing_fields,
            reason_codes=("SALES_INPUT_INCOMPLETE",),
        )

    position = assess_sales_finance_position(sales_input)
    sales_amount: Decimal = position["recalculated_sales_amount_krw"]

    margin = evaluate_sales_margin(
        sales_input,
        sales_amount_krw=sales_amount,
        finance_minimum_margin_rate=finance_minimum_margin_rate,
        finance_warning_margin_rate=finance_warning_margin_rate,
    )
    payment_rule = evaluate_sales_payment_term_rule(
        payment_terms_type=sales_input.payment_terms_type,
        payment_days=sales_input.payment_days,
        max_finance_allowed_payment_terms_days=max_finance_allowed_payment_terms_days,
    )
    credit = evaluate_receivable_capacity(
        sales_amount_krw=sales_amount,
        receivable_facts=receivable_facts,
        credit_limit_krw=credit_limit_krw,
    )
    cashflow = evaluate_sales_cashflow(
        scenario_cashflow=scenario_cashflow,
        minimum_cash_balance_krw=minimum_cash_balance_krw,
    )
    risk = assess_collection_risk(
        receivable_facts=receivable_facts, collection_risk_policy=collection_risk_policy
    )

    rules: list[SalesRuleResult] = [
        position["rule"],
        margin["rule"],
        payment_rule,
        credit["rule"],
        cashflow["rule"],
        risk["rule"],
    ]
    aggregate = aggregate_sales_finance_rules(rules)

    collection_date = None
    if sales_input.collection_reference_date is not None and sales_input.payment_days is not None:
        collection_date = calculate_collection_date(
            reference_date=sales_input.collection_reference_date,
            payment_days=sales_input.payment_days,
        )

    cost_basis = margin["cost_basis"]
    summary = SalesFinancialSummary(
        recalculated_sales_amount_krw=sales_amount,
        reported_sales_amount_krw=sales_input.reported_sales_amount_krw,
        amount_difference_krw=position["comparison"]["difference"],
        amount_match=position["comparison"]["is_match"],
        sales_cost_basis_krw=None if cost_basis is None else cost_basis.amount_krw,
        contribution_margin_krw=margin["contribution_margin_krw"],
        contribution_margin_rate=margin["contribution_margin_rate"],
        collection_date=collection_date,
        current_partner_ar_krw=credit["current_partner_ar_krw"],
        projected_partner_ar_krw=credit["projected_partner_ar_krw"],
        credit_limit_krw=credit_limit_krw,
        available_credit_krw=credit["available_credit_krw"],
        overdue_ar_krw=risk["overdue_ar_krw"],
        base_projected_cash_min=(
            None if scenario_cashflow is None else scenario_cashflow.base_projected_cash_min
        ),
        scenario_projected_cash_min=(
            None if scenario_cashflow is None else scenario_cashflow.scenario_projected_cash_min
        ),
        depends_on_projected_inflow=(
            None if scenario_cashflow is None else scenario_cashflow.depends_on_projected_inflow
        ),
        collection_within_horizon=(
            None if scenario_cashflow is None else scenario_cashflow.collection_within_horizon
        ),
    )

    missing_data = (
        *margin["missing_data"],
        *credit["missing_data"],
        *cashflow["missing_data"],
        *risk["missing_data"],
        *aggregate["missing_policy"],
    )
    return SalesValidationResult(
        scenario_id=sales_input.scenario_id,
        runtime_status=aggregate["runtime_status"],
        status=(
            "EVALUATED" if aggregate["runtime_status"] == "READY" else aggregate["runtime_status"]
        ),
        finance_verdict=aggregate["verdict"],
        financial_summary=summary,
        rule_results=aggregate["rule_results"],
        reason_codes=aggregate["reason_codes"],
        max_finance_allowed_payment_terms_days=max_finance_allowed_payment_terms_days,
        missing_data=_unique_refs(missing_data),
        evidence_refs=_unique_refs(
            (
                *position["evidence_refs"],
                *margin["evidence_refs"],
                *credit["evidence_refs"],
                *cashflow["evidence_refs"],
            )
        ),
    )


def _optional_scenario_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("scenario_id")
    return None if value is None else str(value)


def _unique_refs(values: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)


# ---------------------------------------------------------------------------
# 봉투 매핑 — 도메인 판정을 공통 어휘로 옮긴다
#
# ★ **마스터가 재무 판정을 다시 해석하지 않는다.** 재무가 PASS/REVIEW_REQUIRED/
#   FAIL 로 말한 것을 봉투 어휘로 옮기는 일은 재무 소유다. 마스터가 옮기면 재무
#   판정의 뜻이 마스터 코드에 흩어지고, 그때부터 두 곳을 같이 고쳐야 한다.
#
# ★ 원본을 지우지 않는다. `payload.finance_verdict` 는 봉투 상태와 **함께** 나간다 —
#   `conditional` 만 남으면 "마진 경고"인지 "현금 의존"인지 되돌릴 수 없다.
#
# ★ 여기 사는 이유: Adapter 가 소유하는 계약이지만 Orchestration 도 같은 표를 읽어야
#   한다. Adapter 는 Orchestration 을 import 하므로 반대 방향은 순환이 된다. 그래서
#   **표는 상류(여기)에 한 벌 두고** Adapter 가 그것을 다시 내보낸다.
# ---------------------------------------------------------------------------

#: 재무 도메인 판정 → 공통 봉투 어휘. **이 방향으로만 쓴다.**
SALES_VERDICT_TO_BUSINESS_STATUS: dict[str, str] = {
    "PASS": "ok",
    "REVIEW_REQUIRED": "conditional",
    "FAIL": "reject",
}


def map_sales_finance_verdict(result: SalesValidationResult) -> tuple[str, str]:
    """판매 검증 결과를 ``(runtime_status, business_status)`` 로 옮긴다.

    판정이 없는 세 경우는 전부 `skipped` 다 — 없는 판정을 `reject` 로 바꾸면
    "재무가 거절했다"가 되고, `ok` 로 바꾸면 보지 않은 것을 통과시킨 것이 된다.
    """
    if result.status == "INPUT_INCOMPLETE":
        # Finance 는 멀쩡하다. 제안에 사실이 빠졌을 뿐이다.
        return "READY", "skipped"
    if result.status == "RUNTIME_NOT_READY":
        return "RUNTIME_NOT_READY", "skipped"
    if result.status == "ERROR":
        return "ERROR", "skipped"
    if result.finance_verdict is None:
        return result.runtime_status, "skipped"
    return result.runtime_status, SALES_VERDICT_TO_BUSINESS_STATUS[result.finance_verdict]


def sales_business_status(payload: Mapping[str, Any]) -> str:
    """이미 payload 로 굳은 판매 결과에서 봉투 업무 상태만 읽는다.

    Orchestration 은 Tool 이 낸 payload 만 들고 있다 — 같은 표를 두 벌 만들지 않으려고
    `map_sales_finance_verdict` 와 같은 규칙을 여기서 dict 입력으로 쓴다.
    """
    if payload.get("status") != "EVALUATED":
        return "skipped"
    verdict = payload.get("finance_verdict")
    if verdict is None:
        return "skipped"
    return SALES_VERDICT_TO_BUSINESS_STATUS[str(verdict)]


def build_sales_validation_payload(result: SalesValidationResult) -> dict[str, Any]:
    """Refeed 를 견디는 자기 완결적 Finance payload 를 만든다.

    ★ 마스터는 이것을 **통째로** 나른다. 그래서 판정을 만든 근거가 전부 여기 있어야
      한다 — 하나라도 내부 상태에 남겨두면 회송된 회신에서 그 사실이 사라진다.

    ★ 결제일수 상한은 payload 필드다. 공통 `SuggestedAdjustment` 축이 아니다 —
      재무의 조정 축은 `amount` 하나뿐이고, 상한은 조정이 아니라 경계다.
    """
    summary = result.financial_summary
    return {
        "status": result.status,
        # 봉투 상태와 나란히 원본 판정을 남긴다.
        "finance_verdict": result.finance_verdict,
        "scenario_id": result.scenario_id,
        "financial_summary": (None if summary is None else summary.model_dump()),
        "rule_results": [dict(rule) for rule in result.rule_results],
        "reason_codes": list(result.reason_codes),
        "missing_fields": list(result.missing_fields),
        "missing_data": list(result.missing_data),
        "data_quality": (
            "COMPLETE"
            if not result.missing_fields and not result.missing_data
            else "INCOMPLETE"
        ),
        "max_finance_allowed_amount_krw": result.max_finance_allowed_amount_krw,
        "max_finance_allowed_payment_terms_days": (
            result.max_finance_allowed_payment_terms_days
        ),
        "evidence_refs": list(result.evidence_refs),
    }



# ---------------------------------------------------------------------------
# Harness 진입점 — 인자를 받지 않는다
# ---------------------------------------------------------------------------


def run_sales_validation(
    data_port: FinanceAsOfDataPort, args: dict[str, Any], state: Any
) -> dict[str, Any]:
    """SALES_VALIDATION Tool 본체. **Planner 가 준 값을 쓰지 않는다.**

    ★ `args` 는 비어 있어야 하고 실제로 버린다. 제안 숫자는 request payload 가,
      정책은 Finance Policy 가 소유한다 — 모델이 수량·단가·원가·결제일수·여신을
      만들거나 베껴 넣을 자리를 두지 않는다.

    ★ **있는 것은 쓰고 없는 것은 없는 채로 넘긴다.** 최소 현금과 현금흐름은 권위
      있는 Finance 자료라 실제로 읽는다. 반면 판매 마진 임계값 · 최대 결제일수 ·
      여신한도 · 회수위험 정책은 `FinancePolicy` 의 닫힌 키에도
      `agent_policy_config` 의 finance domain 에도 없다 — 그 자리에는 ``None`` 이
      가고, 결과는 값을 지어내는 대신 RUNTIME_NOT_READY 와 없는 정책 이름을 낸다.
    """
    del args
    payload = state.request.payload
    sales_input, _ = parse_sales_validation_input(payload)

    minimum_cash: Decimal | None = None
    scenario_cashflow: SalesScenarioCashflow | None = None
    if sales_input is not None:
        minimum_cash, scenario_cashflow = _load_sales_cashflow_context(
            data_port, state, sales_input
        )

    return build_sales_validation_payload(
        evaluate_sales_scenario(
            payload,
            # 아래 넷은 저장소에 권위 있는 값이 아직 없다 (팀 결정 대기).
            finance_minimum_margin_rate=None,
            finance_warning_margin_rate=None,
            max_finance_allowed_payment_terms_days=None,
            credit_limit_krw=None,
            # 거래처 채권 조회 계약이 아직 없다 — 0으로 메우지 않는다.
            receivable_facts=None,
            collection_risk_policy=None,
            # 여기 둘은 실재하는 Finance 자료다.
            minimum_cash_balance_krw=minimum_cash,
            scenario_cashflow=scenario_cashflow,
        )
    )


def _load_sales_cashflow_context(
    data_port: FinanceAsOfDataPort,
    state: Any,
    sales_input: SalesValidationInput,
) -> tuple[Decimal | None, SalesScenarioCashflow | None]:
    """확정 현금 Event 위에 제안 회수를 얹은 SCENARIO 투영을 만든다.

    회수일을 못 구하면(기준일이나 결제일수가 없으면) 투영을 만들지 않는다 —
    날짜를 지어내면 그 순간 없는 사실이 현금흐름에 들어간다.
    """
    if sales_input.collection_reference_date is None or sales_input.payment_days is None:
        return None, None

    position, policy, base_events = load_context(data_port, state)
    horizon = state.request.context.as_of + timedelta(days=policy.cashflow_projection_days)
    sales_amount = calculate_sales_amount(
        quantity_kg=sales_input.quantity_kg, unit_price_krw=sales_input.unit_price_krw
    )
    proposed = build_proposed_sales_collection_event(
        proposal_ref=sales_input.scenario_id,
        collection_date=calculate_collection_date(
            reference_date=sales_input.collection_reference_date,
            payment_days=sales_input.payment_days,
        ),
        sales_amount_krw=sales_amount,
        source_ref=sales_input.source_ref,
    )
    scenario_cashflow = project_sales_scenario_cashflow(
        as_of=state.request.context.as_of,
        current_cash_krw=Decimal(position["current_cash_krw"]),
        horizon_end=horizon,
        base_cash_events=base_events,
        proposed_collection=proposed,
    )
    return policy.minimum_cash_balance_krw, scenario_cashflow
