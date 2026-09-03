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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# ---------------------------------------------------------------------------
# 공통 입력 조각 — 계산에 실제로 쓰는 것만 엄격히 검증
# ---------------------------------------------------------------------------


class ConfirmedOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)
    delivery_date: date
    qty_kg: Decimal = Field(ge=0)
    partner_id: str | None = None
    unit_price: Decimal | None = None
    evidence_grade: str | None = None

    @field_validator("qty_kg", "unit_price", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class OnHandLot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str = Field(min_length=1)
    qty_kg: Decimal = Field(ge=0)
    freshness_days_left: int = Field(ge=0)
    reserved_for_confirmed_kg: Decimal = Field(default=Decimal(0), ge=0)
    grade: str | None = None
    received_at: date | None = None
    purchase_unit_price: Decimal | None = None
    evidence_grade: str | None = None

    @field_validator(
        "qty_kg",
        "freshness_days_left",
        "reserved_for_confirmed_kg",
        "purchase_unit_price",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)

    @model_validator(mode="after")
    def reserved_within_quantity(self) -> "OnHandLot":
        if self.reserved_for_confirmed_kg > self.qty_kg:
            raise ValueError("reserved_for_confirmed_kg must not exceed qty_kg")
        return self


class InTransitLot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qty_kg: Decimal = Field(ge=0)
    expected_arrival_date: date
    lot_id: str | None = None
    item: str | None = None

    @field_validator("qty_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class Inventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_hand: list[OnHandLot]
    in_transit: list[InTransitLot]


# ---------------------------------------------------------------------------
# 사이클 A — 매입 하한
# ---------------------------------------------------------------------------


class SalesSnapshotA(BaseModel):
    """T2 동결 스냅샷. 계산에 쓰는 필드만 엄격 검증, 나머지는 보존용.

    추적 키(snapshot_id·policy_version)는 값이 null일 수 있어도 키 자체는 요구한다.
    '값을 모른다'와 '키를 보내지 않았다'를 구분한다.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str | None
    as_of: date
    item: str = Field(min_length=1)
    confirmed_orders: list[ConfirmedOrder]
    inventory: Inventory
    policy_version: str | None
    inbound_lead_days: int | None = Field(default=None, ge=0)
    contract_terms: list[PassThrough] | None = None
    sales_opportunities: list[PassThrough] | None = None
    forecast: PassThrough | None = None
    policy: PassThrough | None = None

    @field_validator("inbound_lead_days", mode="before")
    @classmethod
    def reject_boolean_lead_days(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesFloorInput(BaseModel):
    """POST /sales/procurement 요청."""

    model_config = ConfigDict(extra="forbid")

    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    sales_snapshot: SalesSnapshotA

    @model_validator(mode="after")
    def as_of_matches_snapshot(self) -> "SalesFloorInput":
        if self.as_of != self.sales_snapshot.as_of:
            raise ValueError("as_of must match sales_snapshot.as_of")
        return self


class FloorVectorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    kg: Decimal


class SalesBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    today_floor_kg: Decimal | None
    binding_delivery_date: date | None


class HardConstraintResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    basis: str = "BASE"
    passed: bool | None
    ref_ids: list[str] = Field(default_factory=list)
    skip_reason: str | None = None


class SoftWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: str
    ref_ids: list[str] = Field(default_factory=list)


class SuggestedAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: str
    action: str
    min_qty_kg: Decimal | None = None


class SalesFloorReply(BaseModel):
    """POST /sales/procurement 응답. 캐논 T2 회신 구조."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["sales"] = "sales"
    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    snapshot_id: str | None
    policy_version: str | None
    item: str
    verdict: str | None
    band: SalesBand
    floor_vector: list[FloorVectorEntry]
    hard_constraints: list[HardConstraintResult]
    soft_warnings: list[SoftWarning]
    suggested_adjustment: SuggestedAdjustment


# ---------------------------------------------------------------------------
# 사이클 B — 판매 배분 제안
# ---------------------------------------------------------------------------


class SalesSnapshotB(BaseModel):
    """S1 동결 스냅샷. 계산에 쓰는 inventory만 엄격 검증, 나머지는 보존용.

    캐논이 필수로 두는 입력 키(cost_basis·confirmed_orders·sales_opportunities)와
    추적 키(snapshot_id·policy_version)는 값이 null·빈 배열이더라도 키 자체는 요구한다.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str | None
    as_of: date
    item: str = Field(min_length=1)
    policy_version: str | None
    business_mode: SalesBusinessMode | None = None
    inventory: Inventory
    cost_basis: PassThrough | None
    # 확정 주문은 null을 허용하지 않는다. "주문 없음"은 빈 배열로 표현한다.
    # null(정보 미수신)을 허용하면 confirmed_obligation_kg=0(사실)과 구분되지 않는다.
    confirmed_orders: list[ConfirmedOrder]
    sales_opportunities: list["SalesOpportunity"] | None
    forecast: PassThrough | None = None
    response_state: list[PassThrough] | None = None
    policy: PassThrough | None = None


class ApprovedPurchase(BaseModel):
    """B 사이클에서 추적용으로만 받는 승인 매입 참조."""

    model_config = ConfigDict(extra="allow")

    approval_id: str | None = None


class SalesOpportunity(BaseModel):
    """Master가 전달하는 근거 보유 계약·제안 사실.

    선택 상업값은 의도적으로 null을 허용한다. 가격이 없는 기회에 Sales가 가격을 채우지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    opportunity_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    qty_kg: Decimal | None = Field(default=None, ge=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    delivery_date: date | None = None
    payment_days: int | None = Field(default=None, ge=0)
    contract_term_days: int | None = Field(default=None, ge=0)
    evidence_ref: str | None = None
    rationale: str | None = None

    @field_validator("qty_kg", "unit_price", "payment_days", "contract_term_days", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class FinanceBaseContext(BaseModel):
    """Master가 제공하는 간결한 Finance 상태 뷰. Sales는 값을 계산하지 않는다."""

    model_config = ConfigDict(extra="forbid")
    reference: str | None = None
    cash_status: str | None = None
    receivable_status: str | None = None
    restrictions: list[str] = Field(default_factory=list)


class LogisticsBaseContext(BaseModel):
    """상황 파악용으로 Master가 제공하는 간결한 Inventory/Logistics 상태 뷰."""

    model_config = ConfigDict(extra="forbid")
    reference: str | None = None
    sellable_status: str | None = None
    outbound_capacity_status: str | None = None
    earliest_delivery_date: date | None = None


class SalesInitialContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finance: FinanceBaseContext | None = None
    logistics: LogisticsBaseContext | None = None


class ExternalValidationResult(BaseModel):
    """기작성 후보에 대한 외부 Agent의 권위 있는 결과.

    수치 한도는 담당 Agent가 반환한 사실이며 Sales가 산출하지 않는다.
    """

    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1)
    source: Literal["FINANCE", "LOGISTICS", "PURCHASE"]
    verdict: Literal["PASS", "FAIL", "UNRESOLVED"]
    ref_id: str | None = None
    max_qty_kg: Decimal | None = Field(default=None, ge=0)
    max_payment_days: int | None = Field(default=None, ge=0)
    earliest_delivery_date: date | None = None
    additional_qty_kg: Decimal | None = Field(default=None, ge=0)
    available_date: date | None = None
    conditional: bool = False
    unresolved_fields: list[str] = Field(default_factory=list)

    @field_validator("max_qty_kg", "additional_qty_kg", "max_payment_days", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesAllocationInput(BaseModel):
    """POST /sales/allocation 요청."""

    model_config = ConfigDict(extra="forbid")

    cycle: Literal["SALES"] = "SALES"
    as_of: date
    approved_purchase: ApprovedPurchase | None = None
    sales_snapshot: SalesSnapshotB
    initial_context: SalesInitialContext | None = None
    refeed_results: list[ExternalValidationResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def as_of_matches_snapshot(self) -> "SalesAllocationInput":
        if self.as_of != self.sales_snapshot.as_of:
            raise ValueError("as_of must match sales_snapshot.as_of")
        return self


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


class MissingCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    capability: Literal[
        "SELLABLE_SUPPLY_CONTEXT",
        "DELIVERY_FEASIBILITY_CONTEXT",
        "FINANCIAL_VALIDATION",
        "ADDITIONAL_SUPPLY_CONTEXT",
    ]
    reason: str


class SalesSelfCheck(BaseModel):
    """반환 직전 Sales 책임 범위만 검사한 결과다."""

    model_config = ConfigDict(extra="forbid")
    passed: bool
    issue_codes: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)


class BaseSalesProposal(BaseModel):
    """외부 제약을 적용하기 전, 근거가 있는 원안의 고정 표현이다."""

    model_config = ConfigDict(extra="forbid")
    proposal_id: str
    source_opportunity_id: str
    allocation: AllocationLeg
    delivery_date: date
    payment_days: int | None = None
    contract_term_days: int | None = None
    evidence_ref: str | None = None


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


class SalesProposalMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date
    item: str
    snapshot_id: str | None
    approval_id: str | None
    policy_version: str | None
    agent_version: str | None = None


class SalesAllocationReply(BaseModel):
    """POST /sales/allocation 응답. 캐논 S1 SalesProposal 구조.

    candidates는 후보 생성·정책값(SAL-08~10) 구현 전까지 플레이스홀더다.
    """

    model_config = ConfigDict(extra="forbid")

    agent: Literal["sales"] = "sales"
    cycle: Literal["SALES"] = "SALES"
    stage: Literal["S1"] = "S1"
    meta: SalesProposalMeta
    candidates: list[SalesCandidate]
    confirmed_obligation_kg: Decimal
    coverable_kg: Decimal | None
    no_feasible_reason: str | None = None
    no_feasible_message: str | None = None
    missing_capabilities: list[MissingCapability] = Field(default_factory=list)
    business_mode: SalesBusinessMode | None = None
    situation: str
    self_check: SalesSelfCheck
    base_proposals: list[BaseSalesProposal] = Field(default_factory=list)
    recommendation: SalesRecommendation


# ---------------------------------------------------------------------------
# 최종 Sales Proposal Core — 레거시 allocation 계약과 병행한다.
# ---------------------------------------------------------------------------

SalesCapability = Literal[
    "SELLABLE_SUPPLY_CONTEXT",
    "DELIVERY_FEASIBILITY_CONTEXT",
    "FINANCIAL_VALIDATION",
    "ADDITIONAL_SUPPLY_CONTEXT",
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
    remaining_freshness_days: int | None = Field(default=None, ge=0)
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

    @field_validator("conditional_quantity_kg", mode="before")
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
