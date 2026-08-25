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

SalesCycle = Literal["PROCUREMENT", "SALES"]
RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]


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
    inventory: Inventory
    cost_basis: PassThrough | None
    # 확정 주문은 null을 허용하지 않는다. "주문 없음"은 빈 배열로 표현한다.
    # null(정보 미수신)을 허용하면 confirmed_obligation_kg=0(사실)과 구분되지 않는다.
    confirmed_orders: list[ConfirmedOrder]
    sales_opportunities: list[PassThrough] | None
    forecast: PassThrough | None = None
    response_state: list[PassThrough] | None = None
    policy: PassThrough | None = None


class ApprovedPurchase(BaseModel):
    """B 사이클에서 추적용으로만 받는 승인 매입 참조."""

    model_config = ConfigDict(extra="allow")

    approval_id: str | None = None


class SalesAllocationInput(BaseModel):
    """POST /sales/allocation 요청."""

    model_config = ConfigDict(extra="forbid")

    cycle: Literal["SALES"] = "SALES"
    as_of: date
    approved_purchase: ApprovedPurchase | None = None
    sales_snapshot: SalesSnapshotB

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
