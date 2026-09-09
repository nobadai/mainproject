"""영업 Agent API 요청·응답과 실행이력 조회 계약.

입력·출력은 팀 공통 I/O 계약(캐논)의 구조에 맞춘다. 다만 미구현 결과를 정상값처럼 채우지 않는다.

- 실제 산출 값(floor_vector, band, 확정 의무량)은 결정론 계산 결과다.
- 후보 생성·정책값·제약 판정처럼 미구현인 부분은 null·빈 목록·명시적 미구현 상태로 낸다.
- 계산에 실제로 쓰는 입력(confirmed_orders·inventory·inbound_lead_days)만 엄격히 검증한다.
- 캐논이 필수로 두는 입력·추적 키는 값이 null이더라도 키 자체는 요구한다.
- 아직 계산에 쓰지 않는 입력 블록은 관대한 모델로 받아 실행이력 JSONB에 그대로 보존한다.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.ml.schemas import Forecast

SalesCycle = Literal["PROCUREMENT", "SALES"]
RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
SalesBusinessMode = Literal[
    "CONTRACT_FULFILLMENT",
    "CONTRACT_PROPOSAL_NEW",
    "CONTRACT_PROPOSAL_RENEWAL",
    "SPOT_SALES",
]

#: 결제방식. **Sales 가 소비하는 사용자·계약 사실이지 재무가 추론하는 값이 아니다.**
#:
#: ★ Sales-local 어휘로 둔다. 재무 실행계층 타입을 import 하면 두 Agent 가 실행
#:   계층에서 붙는다 — 마스터가 중개할 자리가 사라진다.
#:
#: 🔴 `payment_days` 가 있다는 이유로 `SINGLE` 을 만들지 않는다. 결제일수는 *언제*
#:   받는지이고 결제방식은 *몇 번에 나눠* 받는지다 — 하나에서 다른 하나가 따라오지 않는다.
SalesPaymentTermsType = Literal["SINGLE", "INSTALLMENT"]


def _reject_boolean(value: object) -> object:
    """bool을 숫자 입력으로 위장해 들어오는 것을 막는다."""
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid numeric inputs")  # noqa: TRY004
    return value


# ---------------------------------------------------------------------------
# 통과(pass-through) 입력 블록
# 아직 계산에 쓰지 않지만 캐논 입력에 포함되는 블록. 그대로 받아 JSONB에 보존한다.
# ---------------------------------------------------------------------------


class PassThrough(BaseModel):
    """계산에 쓰지 않고 실행이력에 보존만 하는 입력 블록. 임의 필드를 허용한다."""

    model_config = ConfigDict(extra="allow")


class AllocationLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    qty_kg: Decimal
    unit_price: Decimal | None = None
    lot_ids: list[str] = Field(default_factory=list)


class OutboundByDate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    kg: Decimal


class Rationale(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    claim: str
    ref_id: str | None = None


class SalesCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    allocation: list[AllocationLeg]
    expected_contribution_krw: Decimal | None = None
    outbound_by_date: list[OutboundByDate] = Field(default_factory=list)
    estimation_confidence: str | None = None
    rationale: list[Rationale] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    adjustment_axis: Literal[
        "NONE", "QUANTITY", "PRICE", "DELIVERY", "PAYMENT_TERMS", "CONTRACT_TERM", "MIX"
    ] = "NONE"
    payment_days: int | None = None
    contract_term_days: int | None = None
    conditional: bool = False
    external_validation_refs: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    strategy_label: str | None = None
    base_proposal_id: str | None = None


class SalesRecommendation(BaseModel):
    """숫자 없이 후보 선택과 설명만 담는 해석 결과다."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]
    recommended_candidate_id: str | None = None
    summary: str
    recommendation_reason: str
    risk_explanation: str
    user_message: str = ""
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_attempts: int = Field(default=0, ge=0)
    llm_fallback_used: bool = False


SalesCapability = Literal[
    "SELLABLE_SUPPLY_CONTEXT",
    "DELIVERY_FEASIBILITY_CONTEXT",
    "FINANCIAL_VALIDATION",
    "ADDITIONAL_SUPPLY_CONTEXT",
]
SalesCandidateStatus = Literal[
    "EXECUTABLE", "CONDITIONAL", "REVIEW_REQUIRED", "UNRESOLVED", "INFEASIBLE"
]
ExecutionDependency = Literal[
    "PURCHASE_COMMITMENT_REQUIRED",
    "DELIVERY_REVALIDATION_REQUIRED",
    "USER_DELIVERY_ACCEPTANCE_REQUIRED",
    "FINANCE_REVALIDATION_REQUIRED",
    "USER_PAYMENT_TERM_ACCEPTANCE_REQUIRED",
]
ScenarioType = Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
ScenarioObjective = Literal["RISK_DEFENSE", "BALANCE", "SALES_OPPORTUNITY"]


class SalesUserRequest(BaseModel):
    """Sales가 제안의 기준으로 삼는 사용자 요청이다."""

    model_config = ConfigDict(extra="forbid")
    raw_text: str | None = None
    item: str = Field(min_length=1)
    partner_id: str | None = None
    requested_quantity_kg: Decimal | None = Field(default=None, ge=0)
    preferred_unit_price_krw: Decimal | None = Field(default=None, ge=0)
    preferred_delivery_date: date | None = None
    preferred_payment_days: int | None = Field(default=None, ge=0)
    #: None 은 "사용자가 결제방식을 말하지 않았다" 이지 SINGLE 이 아니다.
    preferred_payment_terms_type: SalesPaymentTermsType | None = None
    preferred_contract_term_days: int | None = Field(default=None, ge=0)
    #: 이 요청 자체의 권위 있는 출처 ref. 마스터가 구조화된 사용자 요청을 넘길 때
    #: 채우는 자리이며, 독립 실행에서는 없다(None).
    source_ref: str | None = None
    # SPOT에서 미제공은 추가 소싱 허용이 아니다.
    allow_additional_sourcing: bool = False

    @field_validator(
        "requested_quantity_kg",
        "preferred_unit_price_krw",
        "preferred_payment_days",
        "preferred_contract_term_days",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesContractContext(BaseModel):
    """계약 사실을 소비만 하는 Sales용 축약 뷰다."""

    model_config = ConfigDict(extra="forbid")
    contract_id: str | None = None
    previous_contract_id: str | None = None
    partner_id: str | None = None
    item: str | None = None
    contract_quantity_kg: Decimal | None = Field(default=None, ge=0)
    contract_unit_price_krw: Decimal | None = Field(default=None, ge=0)
    contract_delivery_date: date | None = None
    contract_payment_days: int | None = Field(default=None, ge=0)
    #: 계약 원문이 정한 결제방식. Context 가 주지 않으면 None 이다.
    contract_payment_terms_type: SalesPaymentTermsType | None = None
    contract_term_days: int | None = Field(default=None, ge=0)
    source_ref: str | None = None

    @field_validator(
        "contract_quantity_kg",
        "contract_unit_price_krw",
        "contract_payment_days",
        "contract_term_days",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsQueryScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str | None = None
    as_of: date | None = None
    delivery_window_start: date | None = None
    delivery_window_end: date | None = None
    max_confirmed_sellable_quantity_kg: Decimal | None = Field(default=None, ge=0)

    @field_validator("max_confirmed_sellable_quantity_kg", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsDeliveryFeasibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["READY", "UNRESOLVED", "FAIL"] = "UNRESOLVED"
    daily_outbound_capacity_kg: Decimal | None = Field(default=None, ge=0)
    reason_codes: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("daily_outbound_capacity_kg", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsInventoryByItem(BaseModel):
    """Logistics가 확정한 현재 판매 가능 수량 뷰다."""

    model_config = ConfigDict(extra="forbid")
    item: str
    available_qty_kg: Decimal | None = Field(default=None, ge=0)

    @field_validator("available_qty_kg", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsLotConstraint(BaseModel):
    """Lot은 근거 컨텍스트이며 Sales가 이를 합산하거나 필터링하지 않는다."""

    model_config = ConfigDict(extra="forbid")
    lot_id: str
    item: str
    available_qty_kg: Decimal | None = Field(default=None, ge=0)
    # 음수는 freshness 기준을 지난 실제 일수다. 0으로 보정하지 않는다.
    remaining_freshness_days: int | None = None
    effective_freshness_limit_days: int | None = Field(default=None, ge=0)
    grade: str | None = None
    status: str | None = None

    @field_validator(
        "available_qty_kg",
        "remaining_freshness_days",
        "effective_freshness_limit_days",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsSupplyByDate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    confirmed_sellable_quantity_kg: Decimal | None = Field(default=None, ge=0)
    freshness_unresolved_inbound_quantity_kg: Decimal | None = Field(default=None, ge=0)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator(
        "confirmed_sellable_quantity_kg", "freshness_unresolved_inbound_quantity_kg", mode="before"
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsSellableSupply(BaseModel):
    """최종 Logistics PRE_SALES의 판매 가능 공급 블록을 그대로 소비한다."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["READY", "UNRESOLVED", "FAIL"]
    inventory_by_item: list[LogisticsInventoryByItem] = Field(default_factory=list)
    lot_constraints: list[LogisticsLotConstraint] = Field(default_factory=list)
    supply_capacity_by_date: list[LogisticsSupplyByDate] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class SalesLogisticsContext(BaseModel):
    """Logistics PRE_SALES 결과를 재계산 없이 보존하는 입력 모델이다."""

    model_config = ConfigDict(extra="forbid")
    query_scope: LogisticsQueryScope | None = None
    sellable_supply: LogisticsSellableSupply | None = None
    delivery_feasibility: LogisticsDeliveryFeasibility | None = None
    hard_constraints: list[PassThrough] = Field(default_factory=list)
    soft_warnings: list[PassThrough] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class SalesDomainReply(BaseModel):
    """Master가 전달한 원본 Domain 회신을 손실 없이 보관한다."""

    model_config = ConfigDict(extra="forbid")
    source_agent: Literal["finance", "logistics", "purchase"]
    capability: SalesCapability
    reply_ref: str
    runtime_status: RuntimeStatus
    business_status: str | None = None
    # scenario_feedback가 최종 분배 키다. 기존 호출 입력 호환을 위해서만 남긴다.
    scenario_id: str | None = Field(default=None, deprecated=True)
    payload: dict[str, object] = Field(default_factory=dict)


class PurchaseAdditionalSupplyResult(BaseModel):
    """Purchase 추가공급 회신에서 Sales 가 **실제로 읽는** 사실.

    ★ Sales 안에 두는 **수신 전용** 모델이다. Purchase 모델을 import 하지 않는다 —
      두 Agent 를 실행 계층에서 붙이면 마스터가 중개할 자리가 사라진다.

    🔴 **칸은 필수, 값은 nullable 이다.** 예전에는 `payload.get(...)` 로 읽어서
       *키가 없는 것*과 *명시적 null* 이 같아졌다. 앞의 것은 "약속한 사실을 안 보냈다"
       이고 뒤의 것은 "모른다고 답했다" 라 대응이 다르다.

           {"procurable_quantity_kg": null, "risks": []}   유효 — 모른다고 답함
           {"risks": []}                                   무효 — 수량 칸이 없음
           {"procurable_quantity_kg": 0}                   무효 — risks 칸이 없음

    ★ `risks: []` 는 정상 사실이다 — "위험 0건 확인". 키가 없을 때 `[]` 로 메우면
      *확인 안 함*이 *위험 없음*이 된다.

    ★ **모르는 칸은 무시한다 (`extra="ignore"`).** 이 모델은 Purchase 가 소유한 전체
      공급가능성 DTO 의 정본이 아니라 Sales 가 쓰는 부분집합 계약이다. 매입이 나중에
      Sales 가 아직 쓰지 않는 칸을 더 실어 보낼 때 그 칸 때문에 회신 전체를 무효로
      만들면 안 된다 — 그건 남의 계약을 Sales 가 소유하는 셈이다.
      원본 payload 는 `SalesDomainReply.payload` 에 그대로 남으므로 잃는 것도 없다.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    #: > 0 확보 가능량 확인 / 0 확보 가능량 0kg 확인 / None 미실행·확인 불가
    procurable_quantity_kg: Decimal | None
    risks: list[str]
    expected_unit_price_krw: Decimal | None = Field(default=None, ge=0)
    available_by: date | None = Field(
        default=None,
        validation_alias=AliasChoices("available_by", "available_date"),
    )
    basis: Literal["warehouse", "finance", "unknown"] | None = None
    supply_context_absent: Literal[
        "NOT_EXECUTION_DAY", "LEDGER_GAP", "NO_PROCUREMENT_RUN"
    ] | None = None
    source_ref: str | None = None

    @field_validator("procurable_quantity_kg", "expected_unit_price_krw", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesScenarioFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    reply_refs: list[str] = Field(default_factory=list)


class SalesFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_run_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    domain_replies: list[SalesDomainReply] = Field(default_factory=list)
    scenario_feedback: list[SalesScenarioFeedback] = Field(default_factory=list)


class SalesExecutionIdentity(BaseModel):
    """Master wiring 전에도 받을 수 있는 Sales-local 실행 식별자."""

    model_config = ConfigDict(extra="forbid")
    request_id: str | None = None
    run_id: str | None = None
    as_of: date | None = None
    policy_version: str | None = None
    feedback_attempt: int | None = Field(default=None, ge=0)


class SalesApprovalLine(BaseModel):
    """판매 승인 결과의 단일 행. Sales 원장 1건에 대응한다."""

    model_config = ConfigDict(extra="forbid")

    item_name: str = Field(min_length=1)
    quantity_kg: Decimal = Field(ge=0)
    unit_price_krw_per_kg: Decimal = Field(ge=0)
    grade: str | None = None
    contribution_profit_krw: Decimal | None = Field(default=None, ge=0)
    contribution_margin_rate: Decimal | None = Field(default=None, ge=0)

    @field_validator(
        "quantity_kg",
        "unit_price_krw_per_kg",
        "contribution_profit_krw",
        "contribution_margin_rate",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesConfirmationInput(BaseModel):
    """Sales 승인 결과를 실제 원장 write로 옮기기 위한 입력 계약."""

    model_config = ConfigDict(extra="forbid")

    execution_identity: SalesExecutionIdentity
    selected_scenario: "SalesScenario"
    selected_scenario_id: str = Field(min_length=1)
    sim_run_id: str = Field(min_length=1)
    sale_date: date
    order_date: date
    source_order_id: str | None = Field(default=None, min_length=1)
    note: str | None = None
    line: SalesApprovalLine


class SalesFinanceSummarySubset(BaseModel):
    """Sales가 실제로 소비하는 Finance 회신의 최소 부분집합."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    contribution_margin_krw: Decimal | None = None
    contribution_margin_rate: Decimal | None = None
    scenario_projected_cash_min: Decimal | None = None
    depends_on_projected_inflow: bool | None = None
    overdue_ar_krw: Decimal | None = None


class SalesFinanceReplySubset(BaseModel):
    """Finance 내부 모델과 결합하지 않는 Sales-local typed receiver."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    finance_verdict: Literal["PASS", "REVIEW_REQUIRED", "FAIL"] | None = None
    financial_summary: SalesFinanceSummarySubset | None = None
    reason_codes: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class SalesProposalInput(BaseModel):
    """Master 연동 전에도 독립 실행 가능한 최종 Sales 제안 입력이다."""

    model_config = ConfigDict(extra="forbid")
    business_mode: SalesBusinessMode
    is_refeed: bool = False
    feedback_attempt: int = Field(default=0, ge=0)
    user_request: SalesUserRequest
    contract_context: SalesContractContext | None = None
    ml_context: Forecast | None = None
    finance_context: PassThrough | None = None
    logistics_context: SalesLogisticsContext | None = None
    feedback: SalesFeedback | None = None
    execution_identity: SalesExecutionIdentity | None = None


class ScenarioSupply(BaseModel):
    """세 수량은 **서로 다른 사실**이다. 섞거나 합산하지 않는다.

        confirmed_quantity_kg          Logistics 가 확정한 판매 가능 수량
        required_additional_quantity_kg Sales 가 계산한 부족량 (필요한 양)
        conditional_quantity_kg        Purchase 가 조건부 확보 가능하다고 확인한 수량

    🔴 '필요한 양' 은 '확보 가능한 양' 이 아니다. 앞의 것을 뒤의 칸에 넣으면 아직
       아무도 확보해 주지 않은 수량이 확보된 것처럼 읽힌다.
    """

    model_config = ConfigDict(extra="forbid")
    confirmed_quantity_kg: Decimal | None = Field(default=None, ge=0)
    required_additional_quantity_kg: Decimal | None = Field(default=None, ge=0)
    additional_supply_required: bool = False
    #: Purchase 가 **실제로 확인해 준** 조건부 확보 가능량.
    #: None = 모름(검증 전·미실행·수량 미제공), 0 = 확보 가능량이 0으로 확인됨.
    conditional_quantity_kg: Decimal | None = Field(default=None, ge=0)
    #: 위 조건부 수량을 만든 원본 Purchase 회신 ref. 수량과 근거가 같이 다닌다.
    dependency_ref: str | None = None
    #: Purchase 공급 판단이 보존한 조달 원가·가용일·제약 근거와 부재 사유.
    expected_unit_price_krw: Decimal | None = Field(default=None, ge=0)
    available_by: date | None = None
    basis: Literal["warehouse", "finance", "unknown"] | None = None
    supply_context_absent: Literal[
        "NOT_EXECUTION_DAY", "LEDGER_GAP", "NO_PROCUREMENT_RUN"
    ] | None = None
    #: Purchase 공급 경계의 source lineage. Sales 상업조건 source_ref와 다르다.
    purchase_source_ref: str | None = None

    @field_validator("conditional_quantity_kg", "expected_unit_price_krw", mode="before")
    @classmethod
    def reject_boolean_conditional(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesScenario(BaseModel):
    """Sales가 소유한 제안과 외부 검증 의존성을 분리한 최종 시나리오다."""

    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    parent_scenario_id: str | None = None
    revision: int = Field(default=0, ge=0)
    scenario_type: ScenarioType
    objective: ScenarioObjective
    business_mode: SalesBusinessMode
    item: str
    partner_id: str | None = None
    quantity_kg: Decimal | None = Field(default=None, ge=0)
    unit_price_krw: Decimal | None = Field(default=None, ge=0)
    sales_amount_krw: Decimal | None = Field(default=None, ge=0)
    delivery_date: date | None = None
    payment_days: int | None = Field(default=None, ge=0)
    #: 결제방식. 사용자/계약이 말해 준 경우에만 값이 있고, 아니면 None 이다.
    payment_terms_type: SalesPaymentTermsType | None = None
    contract_term_days: int | None = Field(default=None, ge=0)
    #: 이 Scenario 의 **상업조건이 출발한 직접 authoritative source** 하나.
    #:
    #: ★ `evidence_refs` 와 역할이 다르다. 저쪽은 Logistics·계약·ML·Domain 회신까지
    #:   포함한 전체 보조 근거 계보이고, 이쪽은 "이 조건을 누가 정했나" 한 곳이다.
    #:   그래서 `evidence_refs[0]` 같은 위치 기반 선택으로 만들지 않는다.
    source_ref: str | None = None
    supply: ScenarioSupply
    sales_decision_axes: list[str] = Field(default_factory=list)
    required_validations: list[SalesCapability] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    conditional_purchase: bool = False
    variant_collapsed: bool = False
    variant_collapsed_reason: str | None = None
    domain_replies: list[SalesDomainReply] = Field(default_factory=list)
    status: SalesCandidateStatus = "UNRESOLVED"
    execution_dependencies: list[ExecutionDependency] = Field(default_factory=list)
    unmet_quantity_kg: Decimal | None = Field(default=None, ge=0)
    finance_verdict: Literal["PASS", "REVIEW_REQUIRED", "FAIL"] | None = None
    contribution_margin_krw: Decimal | None = None
    contribution_margin_rate: Decimal | None = None
    scenario_projected_cash_min: Decimal | None = None
    depends_on_projected_inflow: bool | None = None
    sell_priority: str | None = None
    authoritative_inventory_risk_severity: str | None = None
    remaining_freshness_days: int | None = None
    ml_support_used: bool = False


class SalesDecisionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    status: SalesCandidateStatus
    rank: int | None = Field(default=None, ge=1)
    recommended: bool = False
    finance_verdict: Literal["PASS", "REVIEW_REQUIRED", "FAIL"] | None = None
    profitability_krw: Decimal | None = None
    scenario_projected_cash_min: Decimal | None = None
    depends_on_projected_inflow: bool | None = None
    inventory_risk_severity: str | None = None
    sell_priority: str | None = None
    remaining_freshness_days: int | None = None
    dependencies: list[ExecutionDependency] = Field(default_factory=list)
    ml_support_used: bool = False
    changed_axes: list[str] = Field(default_factory=list)
    exclusion_reasons: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    reply_refs: list[str] = Field(default_factory=list)
    policy_model_refs: list[str] = Field(default_factory=list)


class ProposalSelfCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    issue_codes: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)


class SalesProposalReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Literal["sales"] = "sales"
    status: Literal["SCENARIOS_GENERATED", "INPUT_INCOMPLETE"]
    business_mode: SalesBusinessMode
    is_refeed: bool
    feedback_attempt: int
    variant_collapsed: bool = False
    variant_collapsed_reason: str | None = None
    scenarios: list[SalesScenario] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    missing_capabilities: list[SalesCapability] = Field(default_factory=list)
    recommended_scenario_id: str | None = None
    llm: SalesRecommendation
    # 레거시 호출자가 recommendation을 읽는 동안 하나의 해석 결과를 호환 제공한다.
    recommendation: SalesRecommendation
    self_check: ProposalSelfCheck
    decision_trace: list[SalesDecisionTrace] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 실행이력 조회
# ---------------------------------------------------------------------------


class SalesAgentRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    cycle: SalesCycle
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime


# ---------------------------------------------------------------------------
# Dashboard 조회 응답
# ---------------------------------------------------------------------------

class SalesDashboardMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sim_run_id: str
    as_of: date
    data_type: str | None = None


class SalesDashboardSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sales_count: int
    customer_count: int
    total_sales_quantity_kg: Decimal
    total_sales_amount_krw: Decimal
    contribution_profit_krw: Decimal
    contribution_margin_pct: Decimal
    received_amount_krw: Decimal
    outstanding_receivables_krw: Decimal


class SalesCollectionStatusSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    sales_amount_krw: Decimal


class SalesItemSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    item_name: str
    line_count: int
    total_quantity_kg: Decimal
    sales_amount_krw: Decimal
    contribution_profit_krw: Decimal
    contribution_margin_pct: Decimal
    avg_unit_price_krw_per_kg: Decimal


class SalesHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sale_id: str
    sale_date: date
    customer_partner_id: str
    partner_name: str | None = None
    total_quantity_kg: Decimal
    total_amount_krw: Decimal
    contribution_profit_krw: Decimal
    contribution_margin_pct: Decimal
    collection_due_date: date
    collection_status: str
    collection_status_label: str
    order_status: str


class SalesReceivableItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receivable_id: str
    sale_id: str
    sale_date: date
    customer_partner_id: str
    partner_name: str | None = None
    issued_date: date
    due_date: date
    original_amount_krw: Decimal
    received_amount_krw: Decimal
    outstanding_amount_krw: Decimal
    status: str
    display_status: str
    d_day: int | None


class SalesDashboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: SalesDashboardMeta
    summary: SalesDashboardSummary
    collection_summary: dict[str, SalesCollectionStatusSummary]
    items: list[SalesItemSummary]
    recent_sales: list[SalesHistoryItem]
    receivables: list[SalesReceivableItem]
