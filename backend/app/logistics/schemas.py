"""재고·물류 Agent A/B 요청, Snapshot 및 응답 계약."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, get_args
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

#: 물류 운영 Policy 의 현재 버전. **문서 세트 버전(v1.4)과 다른 축이다** — 이쪽은
#: `agent_policy_config` 의 행을 고르는 값이라 DB 와 함께 움직인다.
#: 타입(Literal)과 값이 같이 가야 하므로 한 곳에서 만든다 — 종전에는 이 문자열이
#: repository 상수 1곳 + Literal 3곳으로 흩어져 버전을 올릴 때 네 곳을 동시에
#: 고쳐야 했다 (#121 ⑤).
PolicyVersion = Literal["v1.3-PROVISIONAL"]
#: 값은 타입에서 **파생한다** — 문자열이 한 번만 적히게 하려는 것이다. 둘을 나란히
#: 적으면 버전을 올릴 때 여전히 두 줄을 함께 고쳐야 한다 (2026-09-01 교차검증 지적).
POLICY_VERSION: PolicyVersion = get_args(PolicyVersion)[0]

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
    #: remaining_freshness_days 계산에 실제 사용된 유효 보관한계.
    #: `중` 등급은 operational_limit × medium_grade_factor 가 유효 한계이므로,
    #: 신선도 잔여 비율의 분모로 operational_limit 원값을 쓰면 갓 입고된 중 등급이
    #: 즉시 임박 판정된다 — Rule 이 재계산하지 않도록 계산 주체가 여기 실어 준다.
    effective_freshness_limit_days: int | None = None
    status: str = Field(min_length=1)
    storage_zone: str | None = None

    @field_validator(
        "available_qty_kg",
        "remaining_freshness_days",
        "effective_freshness_limit_days",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class InTransitItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: B-1: 행이 존재하면 confirmed_inbound_schedule과 같은 건인지 이 ID로 대조한다.
    inbound_id: str | None = None
    #: 이 운송 건이 **어느 매입에서 왔나**. 도착 시점에 이 참조로 `purchase_items` 를
    #: 읽어 `purchase_item_id` · `item_id` · `grade` · `unit_price_krw_per_kg` 를
    #: **그때의 권위값**으로 가져오려는 자리다.
    #:
    #: 🟡 **받을 자리는 뚫려 있지만 아직 안 켜졌다 (2026-09-05).** `purchase_id` 를
    #:    만드는 곳은 마스터(`app/master/transition.py` 의 `purchase_id_for`)이고,
    #:    그 값이 물류로 넘어오려면 **마스터 전이 규약
    #:    (`LogisticsTransition.build`)이 바뀌어야 한다.** 그것은 마스터 소유
    #:    파일이라 물류가 고칠 자리가 아니다 — **후속 협의 안건**이다.
    #:
    #:    ```text
    #:    지금      transition.build_next_inventory(commitment)                 → None
    #:    협의 뒤    transition.build_next_inventory(…, purchase_ids={seq: id})  → 그 값
    #:    ```
    #:
    #:    물류 쪽 준비는 끝났다 — `purchase_ids` 인자가 기본값 `None` 으로 이미 있고,
    #:    마스터가 넘겨 주는 날 값이 그대로 실린다.
    #:
    #: ★ **물류가 이 ID 를 지어내지 않는다.** 같은 규칙으로 다시 조립하면 같은
    #:   사실의 주인이 둘이 되고, 마스터가 형식을 바꾸는 날 두 곳이 어긋난 채로
    #:   조용히 돈다. 받아서 보관하는 것 외의 경로를 만들지 않는다.
    #:
    #: 🔴 **`inbound_id` 를 대신하지 않는다.** 둘은 다른 정체성이다 —
    #:    `inbound_id` 는 *"물류가 셈하는 입고 건"*, `purchase_id` 는 *"매입 원장의
    #:    어느 행에서 왔나"* 다. B-1 대조의 열쇠는 여전히 `inbound_id` 다.
    #:
    #: ★ **여기에 매입 사실을 복제하지 않는다.** `item_id` · `grade` ·
    #:   `unit_price_krw_per_kg` 는 `purchase_items` 가 주인이다. 복사해 두면 매입이
    #:   값을 고치는 날 이쪽만 옛 값을 들고 남는다.
    #:
    #: ⚠️ **읽기 계약은 관대하다 (`None` 허용).** 이 필드가 생기기 전에 적힌 fixture
    #:    행에는 이 키가 없다 — 전역 필수로 올리면 그 행들이 통째로 파싱에 실패해
    #:    물류가 `RUNTIME_NOT_READY` 로 돌아선다.
    purchase_id: str | None = None
    item: str = Field(min_length=1)
    quantity_kg: Decimal = Field(gt=0)
    expected_arrival_date: date | None

    @field_validator("quantity_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class ItemStoragePolicyFact(BaseModel):
    """품목 자체의 보관 정책. Lot의 현재 상태와 다른 개념이다.

    `remaining_freshness_days`는 *"이 Lot이 앞으로 며칠 쓸 수 있나"*이고
    `operational_limit_days`는 *"이 품목이 원래 며칠 보관 가능한가"*다.
    새로 매입하는 물량의 기준은 후자이므로 기존 Lot의 잔여일수에서 역산하면 안 된다.
    """

    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1)
    #: DB에 값이 없으면 None을 유지한다 — 코드에서 기본값을 만들지 않는다.
    operational_limit_days: int | None = None
    #: 등급 사다리(#69)가 정리되기 전까지 의미를 재정의하지 않고 DB Fact 그대로 나른다.
    #: ★ "재정의하지 않는다" = 물류가 해석·rename·재계산을 얹지 않는다는 뜻이다.
    #:   소비자(매입)가 자기 계산에 쓰는 것을 막는 뜻이 아니다 — 값은 MVP 정책값
    #:   (DB note)이고, #69 가 정하는 것은 계수 값이 아니라 곱하는 대상 등급이다.
    medium_grade_factor: Decimal | None = None

    @field_validator("operational_limit_days", "medium_grade_factor", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class OutboundCommitment(BaseModel):
    """출고가 **이미 잡아 둔** 몫 한 줄. 아직 창고에는 있지만 남에게 팔 수 없는 양이다.

    ```text
    lot_id 있음   그 Lot 에 붙은 살아있는 할당 (ALLOCATED · PICKED)
    lot_id 없음   아직 Lot 을 안 고른 예약의 미할당 잔여
    ```

    🔴 **`SHIPPED` 는 여기 없다.** 나간 몫은 원장 OUT 이 `remaining_qty_kg` 에서 이미
       덜어냈다 — 다시 빼면 같은 수량을 두 번 차감한다 (`outbound.py` 와 같은 규율).
    """

    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1)
    #: `None` 은 **Lot 미지정 예약**이다. 없는 Lot 을 가리키는 것이 아니다.
    lot_id: str | None = None
    quantity_kg: Decimal = Field(gt=0)

    @field_validator("quantity_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class InventoryLogisticsSnapshot(BaseModel):
    """Repository가 한 호출 진입 시점에 읽어 고정한 Inventory/Logistics 사실과 정책값.

    ★ **폐지된 T0 스냅샷이 아니다.** 마스터가 전 부서 데이터를 얼려 배포하던 구조는
      정의서 v2.5 §3.2 로 폐지됐다. 이 모델은 물류가 **자기 도메인만** 1회 읽어
      호출이 끝날 때까지 고정하는 값이며, 정의서 §1.2-13 이 요구하는 것이다.
      (`repository` 모듈 docstring 참조)
    """

    model_config = ConfigDict(extra="forbid")

    #: 🔴 **폐지된 T0 스냅샷의 식별자 — 실제 유산이다.** Repository 는 항상 `None` 을
    #: 넣고, 그래서 `_snapshot_warnings` 가 매 실행마다 `SNAPSHOT_ID_UNRESOLVED` 를
    #: 낸다(상시 노이즈). 실행이력 컬럼·독립 응답 필드로도 나가 있어 그냥 지울 수
    #: 없다 — 실제 ID 를 부여할지 계약에서 걷어낼지가 **미결 안건**이다 (#121 별도).
    snapshot_id: str | None
    as_of: date
    on_hand_by_lot: list[InventoryLotSnapshot]
    #: 품목 단위 보관 정책. Lot 목록에서 역산하지 않으므로 재고 0kg인 품목도 들어온다.
    #: None은 미조회, []는 정책 0건 확인이다.
    item_storage_policies: list[ItemStoragePolicyFact] | None = None
    in_transit: list[InTransitItem] | None
    confirmed_inbound_schedule: list[ScheduledQuantity] | None
    confirmed_outbound_schedule: list[ScheduledQuantity] | None
    #: 🔴 **출고가 이미 잡아 둔 몫** (`inventory_reservations` · `inventory_allocations`).
    #: `None` 은 미조회, `[]` 는 **0건 확인**이다 — 둘을 뭉개면 예약을 못 읽은 것이
    #: *"예약이 없다"* 로 둔갑해 같은 재고가 두 번 팔린다.
    #:
    #: ⚠️ `confirmed_outbound_schedule` 과 **다른 축이다.** 저쪽은 fixture 가 적어 둔
    #:    확정 출고이고 이쪽은 WMS 표의 예약·할당이다. 실측(2026-09-05) 상 fixture 쪽은
    #:    전부 `CONFIRMED_ZERO` 라 지금은 겹치지 않는다 — fixture 에 실제 값이 들어오는
    #:    날 **한 축으로 합쳐야 한다** (지금 둘을 다 빼면 이중 차감이다).
    outbound_commitments: list[OutboundCommitment] | None = None
    used_capacity_kg: Decimal = Field(ge=0)
    guaranteed_capacity_kg: Decimal | None = Field(default=None, gt=0)
    burst_capacity_kg: Decimal | None = Field(default=None, gt=0)
    guaranteed_capacity_by_zone_kg: dict[str, Decimal] | None
    inbound_lead_days: int | None = Field(default=None, ge=0)
    daily_inbound_capacity_kg: Decimal | None = Field(default=None, gt=0)
    inbound_transport_capacity_kg: Decimal | None = Field(default=None, gt=0)
    shared_daily_outbound_capacity_kg: Decimal | None = Field(default=None, gt=0)
    #: 선택 정책 2종 (LLM 정책 결정서 §4). 없으면 해당 업무 위험 판정만 SKIPPED 되고
    #: 물류 계산은 정상 수행한다 — 필수 키로 승격 금지(행 없는 순간 전체 실패).
    capacity_tight_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    freshness_pressure_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    policy_version: PolicyVersion = POLICY_VERSION
    evidence_refs: list[str]

    @field_validator(
        "used_capacity_kg",
        "guaranteed_capacity_kg",
        "burst_capacity_kg",
        "inbound_lead_days",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
        "capacity_tight_ratio",
        "freshness_pressure_ratio",
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
    #: 선택 정책 — DB에 행이 없으면 None 이며 해당 signal 판정만 꺼진다.
    #: 값의 성격은 실업계 기준이 아니라 시뮬레이션 검증용 PROVISIONAL 이다.
    capacity_tight_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    freshness_pressure_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    policy_version: PolicyVersion
    usage_scope: Literal["AGENT_MVP_DEMO"]
    source_refs: dict[str, str]

    @field_validator(
        "guaranteed_capacity_kg",
        "burst_capacity_kg",
        "inbound_lead_days",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
        "capacity_tight_ratio",
        "freshness_pressure_ratio",
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
    policy_version: PolicyVersion = POLICY_VERSION
    runtime_status: RuntimeStatus
    #: 시나리오 집계 ⊕ 하드 제약의 최악값 결합 (2026-09-01 마스터 확정 · #121 3단계).
    #: any reject → FAIL / any conditional → REVIEW_REQUIRED / 전부 ok → PASS 에
    #: 하드 UNRESOLVED/FAIL 이 값을 낮출 수만 있다. 2026-09-01 이전 실행이력의
    #: verdict 는 하드 제약만의 판정이다.
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
    #: 사람이 읽을 미확정 항목의 무숫자 번역명. soft_warnings(원본 기계 코드)와
    #: 채널을 분리한다 — 소비자가 AI 문장을 파싱하지 않고 바로 표시할 수 있고,
    #: LLM Context의 missing_data와 같은 어휘를 쓴다.
    missing_data: list[str] = Field(default_factory=list)
    #: Rule/Scenario Engine 이 결정한 우선 조정 축(quantity/timing). 조정이 없거나
    #: 축이 혼재하면 None — LLM 이 아니라 결정론 층이 정한 값이다.
    #: reject 시나리오의 조정은 집계에서 제외된다(#121 2단계) — 그 조정은
    #: scenario_results 안의 진단 기록으로만 남는다.
    preferred_adjustment: str | None = None
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
    #: Snapshot의 정규화 등급을 그대로 나른다. 정규화 근거가 없으면 None이며,
    #: 필드를 빠뜨리는 것(키 없음)과 None(확인 불가)은 다른 상태다.
    grade: str | None = None
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
    #: PRE와 같은 채널 분리 — 원본 기계 코드는 soft_warnings, 무숫자 번역명은 여기.
    missing_data: list[str] = Field(default_factory=list)
    #: Sales 에서 Rule 이 정한 우선 조정(현행 어휘: 우선 출고 검토 문장). LLM 이 아니라
    #: 결정론 층이 정한다 — 없으면 LLM 도 추천하지 않는다(검증기 강제).
    preferred_adjustment: str | None = None


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
