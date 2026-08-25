"""영업 Agent API 요청·응답과 실행이력 조회 계약.

이번 범위는 순수 계산 세 함수와 그 결과를 저장·조회하는 API다.
따라서 실제 API에 필요한 최소 Pydantic 모델만 둔다.
후보·검증·lot_ids·정책값 관련 타입은 이 범위에서 만들지 않는다.
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
# 공통 입력 조각
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


class SalesFloorInput(BaseModel):
    """POST /sales/procurement 요청. 동결 스냅샷 부분집합을 그대로 담는다."""

    model_config = ConfigDict(extra="forbid")

    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    snapshot_id: str | None = None
    policy_version: str | None = None
    item: str = Field(min_length=1)
    confirmed_orders: list[ConfirmedOrder]
    inventory: Inventory
    inbound_lead_days: int | None = Field(default=None, ge=0)

    @field_validator("inbound_lead_days", mode="before")
    @classmethod
    def reject_boolean_lead_days(cls, value: object) -> object:
        return _reject_boolean(value)


class FloorVectorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    kg: Decimal


class SalesFloorReply(BaseModel):
    """POST /sales/procurement 응답."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["sales"] = "sales"
    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    stage: Literal["T2"] = "T2"
    as_of: date
    snapshot_id: str | None
    item: str
    runtime_status: RuntimeStatus
    today_floor_kg: Decimal | None
    binding_delivery_date: date | None
    floor_vector: list[FloorVectorEntry]


# ---------------------------------------------------------------------------
# 사이클 B — 전략 판매 가능 재고
# ---------------------------------------------------------------------------


class SalesAllocationInput(BaseModel):
    """POST /sales/allocation 요청. 동결 스냅샷 부분집합을 그대로 담는다."""

    model_config = ConfigDict(extra="forbid")

    cycle: Literal["SALES"] = "SALES"
    as_of: date
    snapshot_id: str | None = None
    policy_version: str | None = None
    item: str = Field(min_length=1)
    inventory: Inventory


class StrategicInventoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    kg: Decimal


class SalesAllocationReply(BaseModel):
    """POST /sales/allocation 응답.

    이번 범위는 날짜별 전략 판매 가능 재고까지다. 후보 배분은 포함하지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    agent: Literal["sales"] = "sales"
    cycle: Literal["SALES"] = "SALES"
    stage: Literal["S1"] = "S1"
    as_of: date
    snapshot_id: str | None
    item: str
    runtime_status: RuntimeStatus
    strategic_inventory_by_date: list[StrategicInventoryEntry]


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
