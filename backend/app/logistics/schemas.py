"""재고·물류 Agent A/B 요청, Snapshot 및 응답 계약."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from app.logistics.llm.schemas import LLMResponseFields
from app.purchase_agent.schemas import PurchaseProposal

RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
FinalVerdict = Literal["PASS", "REVIEW_REQUIRED", "FAIL"]
RuleStatus = Literal["PASS", "UNRESOLVED", "FAIL"]
LogisticsCycle = Literal["PROCUREMENT", "SALES"]
RuntimeSourceStatus = Literal["CONFIRMED", "CONFIRMED_ZERO", "UNRESOLVED"]
ConstraintCode = Literal[
    "LOG-H01",
    "LOG-H02",
    "LOG-H03",
    "LOG-H04",
    "LOG-H05",
    "N17",
    "N17-LOT",
    "IN_TRANSIT_SCHEDULE_UNRESOLVED",
    "CONFIRMED_OUTBOUND_ITEM_UNRESOLVED",
    "AS_OF_MISMATCH",
    "REQUIRED_LOGISTICS_SNAPSHOT_MISSING",
]
ScenarioVerdict = Literal["ok", "conditional", "reject", "skipped"]
LogisticsReasonCode = Literal[
    "CAPACITY_EXCEEDED",
    "NO_FEASIBLE_ARRIVAL_DATE",
    "FRESHNESS_EXPIRED",
    "FRESHNESS_WARNING",
]
AdjustmentAxis = Literal["quantity", "timing"]


def _reject_boolean(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid numeric inputs")  # noqa: TRY004
    return value


PurchaseAgentOutput = PurchaseProposal


class ScheduledQuantity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    quantity_kg: Decimal = Field(ge=0)
    item: str | None = None
    #: B-1 입고 건 식별자. outbound 등 다른 Schedule에서도 이 모델을 재사용하므로
    #: 전역 필수값이 아니다 — in_transit 정합성 검증에서만 존재 여부를 판단한다.
    inbound_id: str | None = None

    @field_validator("quantity_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class InventoryLotSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str = Field(min_length=1)
    item: str = Field(min_length=1)
    #: Purchase용 정규화 등급(특/상/중/하). 정규화 근거가 없으면 None —
    #: raw `상품`을 근거 없이 `상`으로 바꾸지 않는다.
    grade: str | None = None
    available_qty_kg: Decimal = Field(ge=0)
    remaining_freshness_days: int | None = None
    status: str = Field(min_length=1)
    storage_zone: str | None = None

    @field_validator("available_qty_kg", "remaining_freshness_days", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class InTransitItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: B-1: 행이 존재하면 confirmed_inbound_schedule과 같은 건인지 이 ID로 대조한다.
    inbound_id: str | None = None
    item: str = Field(min_length=1)
    quantity_kg: Decimal = Field(gt=0)
    expected_arrival_date: date | None

    @field_validator("quantity_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class InventoryLogisticsSnapshot(BaseModel):
    """Repository가 조회한 T0 Inventory/Logistics 사실과 미결 정책값."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str | None
    as_of: date
    on_hand_by_lot: list[InventoryLotSnapshot]
    in_transit: list[InTransitItem] | None
    confirmed_inbound_schedule: list[ScheduledQuantity] | None
    confirmed_outbound_schedule: list[ScheduledQuantity] | None
    used_capacity_kg: Decimal = Field(ge=0)
    guaranteed_capacity_kg: Decimal | None = Field(default=None, gt=0)
    burst_capacity_kg: Decimal | None = Field(default=None, gt=0)
    guaranteed_capacity_by_zone_kg: dict[str, Decimal] | None
    inbound_lead_days: int | None = Field(default=None, ge=0)
    daily_inbound_capacity_kg: Decimal | None = Field(default=None, gt=0)
    inbound_transport_capacity_kg: Decimal | None = Field(default=None, gt=0)
    shared_daily_outbound_capacity_kg: Decimal | None = Field(default=None, gt=0)
    policy_version: Literal["v1.3-PROVISIONAL"] = "v1.3-PROVISIONAL"
    evidence_refs: list[str]

    @field_validator(
        "used_capacity_kg",
        "guaranteed_capacity_kg",
        "burst_capacity_kg",
        "inbound_lead_days",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsPolicy(BaseModel):
    """Logistics MVP 실행에 사용하는 운영 제약 및 정책."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guaranteed_capacity_kg: Decimal = Field(gt=0)
    burst_capacity_kg: Decimal = Field(gt=0)
    inbound_lead_days: int = Field(ge=0)
    daily_inbound_capacity_kg: Decimal = Field(gt=0)
    inbound_transport_capacity_kg: Decimal = Field(gt=0)
    shared_daily_outbound_capacity_kg: Decimal = Field(gt=0)
    cap_by_date_policy: Literal["CONFIRMED_ONLY"]
    policy_version: Literal["v1.3-PROVISIONAL"]
    usage_scope: Literal["AGENT_MVP_DEMO"]
    source_refs: dict[str, str]

    @field_validator(
        "guaranteed_capacity_kg",
        "burst_capacity_kg",
        "inbound_lead_days",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
        mode="before",
    )
    @classmethod
    def reject_boolean_policy_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsRuntimeFixture(BaseModel):
    """AGENT_MVP_DEMO 전용 Logistics schedule completeness fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(min_length=1)
    sim_run_id: str = Field(min_length=1)
    as_of: date
    in_transit_status: RuntimeSourceStatus
    in_transit: list[InTransitItem] | None
    confirmed_inbound_status: RuntimeSourceStatus
    confirmed_inbound_schedule: list[ScheduledQuantity] | None
    confirmed_outbound_status: RuntimeSourceStatus
    confirmed_outbound_schedule: list[ScheduledQuantity] | None
    usage_scope: Literal["AGENT_MVP_DEMO"]
    evidence_grade: Literal["SIM_FIXED"]
    source_ref: str = Field(min_length=1)
    approved_by: Literal["HUMAN"]

    @model_validator(mode="after")
    def validate_schedule_statuses(self) -> "LogisticsRuntimeFixture":
        sources = (
            ("in_transit", self.in_transit_status, self.in_transit),
            (
                "confirmed_inbound",
                self.confirmed_inbound_status,
                self.confirmed_inbound_schedule,
            ),
            (
                "confirmed_outbound",
                self.confirmed_outbound_status,
                self.confirmed_outbound_schedule,
            ),
        )
        for name, status, schedule in sources:
            if status == "UNRESOLVED" and schedule is not None:
                raise ValueError(f"{name} UNRESOLVED must preserve None")
            if status == "CONFIRMED_ZERO" and schedule != []:
                raise ValueError(f"{name} CONFIRMED_ZERO must have an empty list")
            if status == "CONFIRMED" and not schedule:
                raise ValueError(f"{name} CONFIRMED must have confirmed rows")
        if (
            self.in_transit_status == "CONFIRMED"
            and self.in_transit is not None
            and any(item.expected_arrival_date is None for item in self.in_transit)
        ):
            raise ValueError("confirmed in_transit rows require expected_arrival_date")
        return self


class ConstraintResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ConstraintCode
    status: RuleStatus
    skip_reason: str | None = None


class InventoryByItem(BaseModel):
    """가용재고 정의를 적용한 품목별 자유재고 합계. 등급 축으로 나누지 않는다."""

    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1)
    available_qty_kg: Decimal = Field(ge=0)

    @field_validator("available_qty_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class ScenarioAdjustment(BaseModel):
    """물류 허용 조정 축은 quantity/timing뿐이다. amount/channel_mix는 반환하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    axis: AdjustmentAxis
    #: 조정 대상 분할 회차의 매입 실행일 — 어느 split에 대한 제안인지 식별용.
    split_date: date
    suggested_qty_kg: Decimal | None = None
    #: 매입 실행일 역산은 Purchase 책임이라 도착일 기준으로만 제안한다.
    suggested_arrival_date: date | None = None


class ScenarioValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    verdict: ScenarioVerdict
    reason_codes: list[LogisticsReasonCode]
    adjustments: list[ScenarioAdjustment]


class LogisticsBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cap_by_date: dict[date, Decimal]
    unit: Literal["kg"] = "kg"


class InboundConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inbound_lead_days: int | None
    daily_inbound_capacity_kg: Decimal | None
    inbound_transport_capacity_kg: Decimal | None


class LogisticsEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)


class LogisticsProcurementResponse(LLMResponseFields):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["inventory_logistics"] = "inventory_logistics"
    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    snapshot_id: str | None
    policy_version: Literal["v1.3-PROVISIONAL"] = "v1.3-PROVISIONAL"
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    band: LogisticsBand
    #: 물류가 직접 집계한 품목별 가용재고. confirmed_outbound.item 누락 등으로
    #: 정확히 계산할 수 없으면 None이며, 직렬화 시 키 자체를 뺀다 — `[]`(0건 확인)와
    #: 구분되어야 하기 때문이다. M-1 missing_data 번역은 Master Adapter 책임.
    inventory_by_item: list[InventoryByItem] | None = None
    scenario_results: list[ScenarioValidationResult] | None = None
    inbound_constraints: InboundConstraints
    hard_constraints: list[ConstraintResult]
    soft_warnings: list[str]
    evidences: list[LogisticsEvidence]

    @model_serializer(mode="wrap")
    def drop_uncomputable_inventory_by_item(self, handler: Any) -> dict:
        data = handler(self)
        if data.get("inventory_by_item") is None:
            data.pop("inventory_by_item", None)
        return data


class ArrivalScheduleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    quantity_kg: Decimal = Field(gt=0)

    @field_validator("quantity_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class LogisticsApprovedPurchaseCommitment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1)
    total_qty_kg: Decimal = Field(gt=0)
    expected_arrival_date: date
    arrival_schedule: list[ArrivalScheduleItem] = Field(min_length=1)

    @field_validator("total_qty_kg", mode="before")
    @classmethod
    def reject_boolean_total(cls, value: object) -> object:
        return _reject_boolean(value)

    @model_validator(mode="after")
    def validate_arrival_total(self) -> "LogisticsApprovedPurchaseCommitment":
        scheduled_total = sum(
            (item.quantity_kg for item in self.arrival_schedule), start=Decimal(0)
        )
        if self.total_qty_kg != scheduled_total:
            raise ValueError("total_qty_kg must equal arrival_schedule quantity total")
        if self.expected_arrival_date != min(item.date for item in self.arrival_schedule):
            raise ValueError("expected_arrival_date must equal the first arrival schedule date")
        return self


class LogisticsSalesRequest(BaseModel):
    """Logistics B가 받는 H1 승인 매입 Delta."""

    model_config = ConfigDict(extra="forbid")

    cycle: Literal["SALES"]
    as_of: date
    approved_purchase: LogisticsApprovedPurchaseCommitment

    @model_validator(mode="after")
    def validate_arrival_dates(self) -> "LogisticsSalesRequest":
        if any(item.date < self.as_of for item in self.approved_purchase.arrival_schedule):
            raise ValueError("arrival_schedule dates must be on or after as_of")
        return self


class LotConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str
    item: str
    available_qty_kg: Decimal = Field(ge=0)
    remaining_freshness_days: int | None = None
    status: str


class LogisticsSalesResponse(LLMResponseFields):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["inventory_logistics"] = "inventory_logistics"
    cycle: Literal["SALES"] = "SALES"
    snapshot_id: str | None
    approval_id: str
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    daily_outbound_capacity_kg: Decimal | None
    lot_constraints: list[LotConstraint]
    hard_constraints: list[ConstraintResult]
    soft_warnings: list[str]


class LogisticsAgentRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    cycle: LogisticsCycle
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime
