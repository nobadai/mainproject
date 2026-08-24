"""재고·물류 Agent A/B 요청, Snapshot 및 응답 계약."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
LogisticsCycle = Literal["PROCUREMENT", "SALES"]
ConstraintCode = Literal[
    "LOG-H01",
    "LOG-H02",
    "LOG-H03",
    "LOG-H04",
    "LOG-H05",
    "AS_OF_MISMATCH",
    "REQUIRED_LOGISTICS_SNAPSHOT_MISSING",
]


def _reject_boolean(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid numeric inputs")  # noqa: TRY004
    return value


class PurchaseMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date
    item: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    is_refeed: bool
    feedback_attempt: int = Field(ge=0)

    @field_validator("feedback_attempt", mode="before")
    @classmethod
    def reject_boolean_feedback_attempt(cls, value: object) -> object:
        return _reject_boolean(value)


class SplitPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    date: date
    quantity_ton: Decimal = Field(gt=0)

    @field_validator("seq", "quantity_ton", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class PurchaseSourcingPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str = Field(min_length=1)
    grade: str = Field(min_length=1)
    quantity_ton: Decimal = Field(gt=0)
    grade_unit_price: int = Field(gt=0)

    @field_validator("quantity_ton", "grade_unit_price", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class PurchaseAgentScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    strategy_type: str = Field(min_length=1)
    coverage_days: int = Field(gt=0)
    total_quantity_ton: Decimal = Field(gt=0)
    total_amount_krw: Decimal = Field(ge=0)
    split_plan: list[SplitPlanItem] = Field(min_length=1)
    sourcing_plan: list[PurchaseSourcingPlanItem] = Field(min_length=1)

    @field_validator(
        "coverage_days",
        "total_quantity_ton",
        "total_amount_krw",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)

    @model_validator(mode="after")
    def validate_quantity_totals(self) -> "PurchaseAgentScenario":
        split_quantity = sum((item.quantity_ton for item in self.split_plan), start=Decimal(0))
        sourcing_quantity = sum(
            (item.quantity_ton for item in self.sourcing_plan), start=Decimal(0)
        )
        if self.total_quantity_ton != split_quantity:
            raise ValueError("total_quantity_ton must equal split_plan quantity total")
        if self.total_quantity_ton != sourcing_quantity:
            raise ValueError("total_quantity_ton must equal sourcing_plan quantity total")
        return self


class PurchaseAgentOutput(BaseModel):
    """Logistics A가 받는 Purchase Agent v0.4 전체 출력."""

    model_config = ConfigDict(extra="forbid")

    meta: PurchaseMeta
    scenarios: list[PurchaseAgentScenario] = Field(min_length=1)


class ScheduledQuantity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    quantity_kg: Decimal = Field(ge=0)
    item: str | None = None

    @field_validator("quantity_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class InventoryLotSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str = Field(min_length=1)
    item: str = Field(min_length=1)
    available_qty_kg: Decimal = Field(ge=0)
    remaining_freshness_days: int | None = Field(default=None, ge=0)
    status: str = Field(min_length=1)
    storage_zone: str | None = None

    @field_validator("available_qty_kg", "remaining_freshness_days", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class InTransitItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class ConstraintResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ConstraintCode
    passed: bool | None
    skip_reason: str | None = None


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


class LogisticsProcurementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["inventory_logistics"] = "inventory_logistics"
    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    snapshot_id: str | None
    policy_version: Literal["v1.3-PROVISIONAL"] = "v1.3-PROVISIONAL"
    runtime_status: RuntimeStatus
    band: LogisticsBand
    inbound_constraints: InboundConstraints
    hard_constraints: list[ConstraintResult]
    soft_warnings: list[str]
    evidences: list[LogisticsEvidence]


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


class LotConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str
    item: str
    available_qty_kg: Decimal = Field(ge=0)
    remaining_freshness_days: int | None = Field(default=None, ge=0)
    status: str


class LogisticsSalesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["inventory_logistics"] = "inventory_logistics"
    cycle: Literal["SALES"] = "SALES"
    snapshot_id: str | None
    approval_id: str
    runtime_status: RuntimeStatus
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
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime
