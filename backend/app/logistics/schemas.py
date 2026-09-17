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
from app.logistics.outbound import AllocationBasis, AllocationStatus, ReservationStatus
from app.logistics.turnover import TurnoverStatus
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

#: *"그 축을 확인한 적이 없다"* 를 뜻하는 값. 🔴 **이 문자열을 여기 말고 어디에도
#: 다시 적지 않는다** — 판정하는 자리가 둘(`repository._schedule_source` 화면 축 ·
#: `inbound_stock.load_in_transit_for_receiving` 도착 축)이라, 한쪽만 고쳐지는 날
#: 같은 날 같은 입고를 두 경로가 다르게 읽는다.
UNRESOLVED_SOURCE: RuntimeSourceStatus = "UNRESOLVED"
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
    #: 이 Lot 이 창고에 들어온 날. **FIFO 배부 순서의 축**이다.
    #:
    #: ★ 신선도 계산에 이미 쓰던 사실이라 새로 만드는 값이 아니다 — 그동안 스냅샷에
    #:   싣지 않았을 뿐이다.
    received_at: date | None = None
    #: 이 Lot 의 **실제 취득단가**(원/kg). `inventory_lots.unit_cost_krw_per_kg` 그대로다.
    #:
    #: 🔴 **재계산하지 않는다.** 매입 평균단가나 최근 단가로 추정하면 그 순간 장부에
    #:    없는 원가가 판정에 들어간다. 못 읽으면 `None` 이고, 그때 원가 기준은 서지
    #:    않는다 — 0원으로 메우지 않는다.
    unit_cost_krw_per_kg: Decimal | None = Field(default=None, ge=0)
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
    #: 출고 준비에 걸리는 날 수 (WP-4 M4). `as_of + 이 값` 이 가장 이른 납기일이다.
    #:
    #: 🔴 **선택 정책이지만 «없으면 1» 이 아니다.** 행이 없으면 `None` 이고, PRE_SALES
    #:    는 그때 `delivery_feasibility.status = "UNRESOLVED"` 로 **답을 안 낸다** —
    #:    코드 상수 `1` 로 메우면 DB 에 정책이 없는데도 납기가 확정된 것처럼 나간다.
    #:
    #: ⚠️ **필수로 올리지 않았다.** 올리면 행이 없는 순간 물류가 통째로
    #:    `RUNTIME_NOT_READY` 가 되어 입고·Capacity 처럼 납기와 **무관한 경로까지**
    #:    멈춘다 (`_OPTIONAL_NUMERIC_POLICY_KEYS` 주석이 같은 이유를 적고 있다).
    #:    fail-closed 는 그 값을 실제로 쓰는 자리에서 건다.
    outbound_prep_lead_days: int | None = Field(default=None, ge=0)
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
        "outbound_prep_lead_days",
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


class InventoryCostBasisSnapshot(BaseModel):
    """확정 판매 물량에 FEFO 로 배부된 **예상 재고 취득원가**.

    ★ **물류가 소유하는 모양이다.** 재무 `InventoryCostBasis` 를 import 하지 않는다 —
      실행 계층에서 두 Agent 를 붙이면 마스터가 중개할 자리가 사라지고, 재무가 판정
      필드를 하나 바꾸는 날 물류 계산이 조용히 따라 바뀐다. 칸 이름만 같게 둔다.

    🔴 **PRE_SALES 시점의 예상이지 출고 사실이 아니다.** 이 값이 서는 자리는 판매 제안
       **전**이고, 그 판매의 예약도 할당도 아직 없다 (승인 → 예약 →
       `fefo_allocation` → 출고 순서다). 그래서 여기 담긴 Lot 은 *"지금 출고한다면
       FEFO 가 집을 Lot"* 이다.

       ★ **그래서 순서만이라도 실제와 같아야 한다.** 정렬 키는 실제 자동 출고와
         같은 `turnover.fefo_sort_key` 하나다.

    🔴 **`allocation_method` 와 `cost_method` 는 다른 축이다.** 앞은 *"어느 Lot 을 어떤
       순서로 고르나"*(FEFO)이고 뒤는 *"그 Lot 의 단가가 무엇이었나"*(ACTUAL)다. 하나로
       합치면 «FEFO 로 골랐으니 원가도 FEFO 다» 같은, 장부에 없는 원가가 생긴다.

    🔴 **`source_refs` 가 정본이다.** `source_ref` 는 하위 호환용 대표 하나일 뿐이라
       두 Lot 에 걸친 배부 근거를 그것만으로는 따라갈 수 없다.
    """

    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1)
    #: 이 원가가 덮는 양. 확정 물량과 **정확히 같을 때만** 기준이 선다.
    quantity_kg: Decimal = Field(ge=0)
    amount_krw: Decimal = Field(ge=0)
    #: Lot 선택 순서. 실제 자동 출고와 같은 FEFO 한 가지다 —
    #: 없는 방식을 이름으로 만들지 않는다.
    allocation_method: Literal["FEFO"] = "FEFO"
    #: 단가의 성격. 장부 실단가를 그대로 썼다는 사실이다.
    cost_method: Literal["ACTUAL"] = "ACTUAL"
    included_components: tuple[str, ...] = ("inventory_acquisition_cost",)
    #: 이 금액을 배부하는 데 **쓴 Lot 근거**, FEFO 배부 순서 그대로.
    #:
    #: ⚠️ **«출고된 Lot» 도 «헐어 쓴 Lot» 도 아니다.** PRE_SALES 는 할당 전이라
    #:    출고 사실이 아직 없다. 대표 하나로 줄이지 않는다.
    source_refs: tuple[str, ...] = Field(min_length=1)
    evidence_grade: str = Field(min_length=1)

    @property
    def source_ref(self) -> str:
        """하위 호환용 대표 ref. **배부 근거 전체가 아니다** — 전체는 `source_refs` 다."""
        return self.source_refs[0]

    @field_validator("quantity_kg", "amount_krw", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


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


# ═══════════════════════════════════════════════════════════════════════════
# 화면 조회 read model (`console_service` 가 만들고 `api/logistics/query.py` 가 읽는다)
#
# 🔴 **프론트까지 안 나간다.** `query.py` 가 이 값을 `Pane` · `Card` · `Stat` 로 옮겨
#    담아 `api/logistics/schema.LogisticsTab` 을 만든다. 그래서 이것은 화면 DTO 가
#    아니라 **물류 내부 read model** 이고, 도메인 쪽에 있어야 의존이 한 방향으로 선다
#    (2026-09-15 · 물류 문서 28).
#
# ```text
# api/logistics/query.py  →  logistics/console_service.py  →  historical_repository · outbound
# console_service 결과     →  query.py 가 변환              →  api/logistics/schema.LogisticsTab
# ```
#
#    ⚠️ 종전에는 `app/logistics/console_schemas.py` 라는 따로 선 파일이었다. 이름이
#       «console» 이라 화면 계약으로 읽혔고, 실제로 그 혼동에서 화면의 시간축이 섞였다.
# ═══════════════════════════════════════════════════════════════════════════

#: `available_qty_kg` 를 못 낸 이유. `tools.build_inventory_by_item` 이 `None` 을
#: 돌려주는 경로와 1:1 이다 — 어느 축을 못 읽었는지 화면이 알아야 한다.
#:
#: 🔴 **`CONFIRMED_OUTBOUND_*` 두 값이 WP-3 에서 빠졌다.** 판매가능량의 차감 축이
#:    예약·할당 한 벌로 좁혀져(이중 차감 제거) 확정 출고 축은 이 판정에 안 들어온다.
AvailableQtyUnresolvedReason = Literal[
    "OUTBOUND_COMMITMENTS_UNRESOLVED",
    #: 그날의 Agent Runtime Snapshot(`logistics_runtime_fixture`)이 없다. 재고 수량은
    #: 원장으로 되살아나지만 판매가능량은 그 스냅샷의 확정 출고 축이 있어야 선다.
    #: 🔴 없는 축을 0 으로 메우고 «다 팔 수 있다» 고 답하지 않는다.
    "RUNTIME_SNAPSHOT_UNAVAILABLE",
]

#: 이 칸의 값이 **어느 시간축**에서 나왔나. 전환기 표시다.
#:
#: ```text
#: HISTORICAL_AS_OF  요청한 as_of 시점 사실 (원장 · 사건 재생)
#: CURRENT_ROW       지금 행 값 — 되살릴 정본이 없거나, 그 값이 Runtime 축이다
#: ```
#:
#: 🔴 **둘을 한 숫자 안에 섞지 않는다.** 섞이면 «과거인 척하는 현재» 가 되고,
#:    그것이 이번 재설계가 없애려는 결함이다. 못 되살리는 축은 그렇다고 말한다.
TimeBasis = Literal["HISTORICAL_AS_OF", "CURRENT_ROW"]

class ConsoleModel(BaseModel):
    """콘솔 계약 공통 설정. 계약에 없는 칸을 조용히 흘리지 않는다."""

    model_config = ConfigDict(extra="forbid")


# ── 재고 ────────────────────────────────────────────────────────────────


class ConsoleInventoryItem(ConsoleModel):
    """품목 한 줄. **현재고와 판매가능량은 다른 값이다.**"""

    item_id: str
    item_name: str
    #: 물리 실재량. 만료 Lot 도 창고에 있으면 여기 들어간다.
    on_hand_qty_kg: Decimal
    #: 판매가능량 — `tools.build_inventory_by_item` 정본.
    #: 🔴 못 읽은 축이 있으면 `None` 이다. **0 으로 바꾸지 않는다.**
    available_qty_kg: Decimal | None
    #: 🔴 **지금 실제로 재고를 잡고 있는 양**이다 — 예약 행에 적힌 확보량의 합이 아니다.
    #:
    #:    ```text
    #:    reserved_qty_kg = allocated_qty_kg + unallocated_reserved_qty_kg
    #:    ```
    #:
    #:    항등식인 것이 계약이다. 전량 출고가 끝난 예약은 셋 다 0 이 된다
    #:    (`ship_allocated_stock` 이 예약 행의 `reserved_qty_kg` 를 안 줄이기 때문에
    #:    원래 값을 합하면 나간 재고를 아직 잡고 있는 것으로 보인다).
    #:
    #:    ⚠️ `ConsoleReservation.reserved_qty_kg` 와 **뜻이 다르다** — 저쪽은 예약
    #:       행에 적힌 DB 값 그대로다.
    reserved_qty_kg: Decimal
    #: 그 예약들이 **아직 안 내보낸** Lot 할당량 (ALLOCATED · PICKED).
    allocated_qty_kg: Decimal
    #: 잡아 뒀지만 아직 Lot 을 안 고른 몫. 이미 배정한 몫(SHIPPED 포함)을 뺀 값이다.
    unallocated_reserved_qty_kg: Decimal
    #: 🔴 **잡고 있는 양이 0 보다 큰 예약 수**다. 상태가 `ALLOCATED` 로 남아 있어도
    #:    전량 출고가 끝났으면 세지 않는다 — 기준은 상태 어휘가 아니라 수량이다.
    active_reservation_count: int
    #: `turnover.sell_priority` 가 참인 Lot 수. **폐기와 무관하다.**
    sell_priority_lot_count: int
    #: `remaining_freshness_days <= 0` 이며 잔량이 남은 Lot 수.
    expired_lot_count: int
    expired_qty_kg: Decimal
    #: `turnover.is_disposal_candidate` 가 참인 Lot 수. 만료 기준과 같은 판정이라
    #: `expired_lot_count` 와 같은 값이 나온다 — 두 이름을 화면이 함께 쓰기에 둘 다 싣는다.
    disposal_candidate_lot_count: int


class ConsoleInventoryLot(ConsoleModel):
    """Lot 한 줄. 파생값은 전부 `turnover.load_lot_turnover` 가 만든 것이다."""

    lot_id: str
    item_id: str
    item_name: str | None
    #: 🔴 `repository._normalize_grade` 를 지난 값이다. 정규화표에 없는 raw 등급은
    #:    `None` 이 된다 — 임의 치환(`상품 → 상`)을 하지 않는다.
    grade: str | None
    #: 🔴 **`inventory_lots.remaining_qty_kg` 가 아니다.** `as_of` 까지의 원장
    #:    (`IN − OUT − DISPOSE`) 누계다. 그 컬럼은 Current Cache 라 과거를 못 말한다.
    remaining_qty_kg: Decimal
    received_at: date
    #: 🔴 **`inventory_lots.status` 컬럼이 아니라 유도값이다** (`ACTIVE` · `DEPLETED` ·
    #:    `DISPOSED`). `HOLD` 는 writer 가 없어 되살릴 사건이 없다 —
    #:    `historical_repository.HistoricalLotState` 가 그 어휘의 주인이다.
    status: str | None
    #: 🔴 **`ConsoleZone.zone_id` 와 다른 어휘다. 조인하지 않는다.**
    #:    이 칸의 주인은 `item_storage_policies.storage_zone` 이고(실측 `COLD_HUMID_0_3`
    #:    계열), 창고 Zone 의 주인은 `warehouse_zones.zone_id` 다(실측
    #:    `HIGH_HUMIDITY_COLD` 계열). Lot 의 물리 위치는 `lot_locations[].zone_id` 로 본다.
    storage_zone: str | None
    remaining_freshness_days: int | None
    remaining_turnover_days: int | None
    #: 회전 정책이 없는 품목이면 `None`. **`NORMAL` 로 채우지 않는다.**
    turnover_status: TurnoverStatus | None
    sell_priority: bool
    disposal_candidate: bool


class ConsoleCapacity(ConsoleModel):
    """창고 kg Capacity. **Pallet Position 축과 다른 단위다.**"""

    #: 🔴 만료 Lot 도 잔량이 남아 있으면 여기 포함된다 — 판매불가 != 창고에서 사라짐.
    #: ★ `as_of` 시점 원장 합이다 — `remaining_qty_kg` 합이 아니다.
    used_capacity_kg: Decimal
    guaranteed_capacity_kg: Decimal | None
    burst_capacity_kg: Decimal | None
    #: 🔴 **한도 두 값은 과거로 되살린 것이 아니다.** `agent_policy_config` 에 유효일
    #:    컬럼이 없어 «그날 그 정책이었나» 를 알 수 없다. 유효일 컬럼을 새로 만들지
    #:    않기로 했으므로(`07 §15`) 지금 활성 정책을 쓰되 그 사실을 여기 적는다.
    capacity_basis: Literal["CURRENT_ACTIVE_POLICY"] = "CURRENT_ACTIVE_POLICY"


class ConsoleInventoryResponse(ConsoleModel):
    sim_run_id: str
    as_of: date
    items: list[ConsoleInventoryItem]
    lots: list[ConsoleInventoryLot]
    capacity: ConsoleCapacity
    #: `available_qty_kg` 가 전부 `None` 일 때만 채워진다. 그 외에는 `None`.
    available_qty_unresolved_reason: AvailableQtyUnresolvedReason | None = None
    #: 🔴 **`on_hand_qty_kg` 와 같은 시간축이다 (#760 · LOG-HIST-002).** 판매가능량도
    #:    이제 `as_of` 로 되살린다 — 화면(콘솔) 경로가 그날 Lot(`lot_state_at`)과 그날
    #:    예약(`reservation_state_at`)으로 세운 스냅샷을 정본 `tools.build_inventory_by_item`
    #:    에 먹인다. 종전에는 `repository.get_outbound_commitments`(현재 행)를 쓴 Runtime
    #:    스냅샷이라 «지금» 축이었다.
    #:
    #:    ⚠️ **Agent Runtime 경로는 그대로 «지금» 축이다.** 그쪽은 *"지금 더 팔 수
    #:       있나"* 를 묻는 자리라 현재 스냅샷을 쓴다(`ConsoleInventoryResponse` 를 안
    #:       거친다) — 바뀐 것은 화면(콘솔)뿐이다.
    available_qty_time_basis: TimeBasis = "HISTORICAL_AS_OF"
    #: 현재고 · Lot 상태 · 신선도 · 회전 · `used_capacity_kg` · 예약·판매가능량의 시간축.
    on_hand_time_basis: TimeBasis = "HISTORICAL_AS_OF"


# ── 입고 ────────────────────────────────────────────────────────────────


class ConsoleInTransitItem(ConsoleModel):
    inbound_id: str | None
    #: 🔴 마스터 규약이 이 값을 안 넘긴다 — `None` 이 **확정된 정상 상태**다
    #:    (`transition.build_next_inventory`). 그래서 도착일이 와도 `blocked` 로 갈린다.
    purchase_id: str | None
    item: str
    quantity_kg: Decimal
    expected_arrival_date: date | None


class ConsoleInboundReceipt(ConsoleModel):
    """Receipt + 검수 + 재고반영을 한 줄로 모은 것."""

    inbound_id: str | None
    receipt_id: str
    item_id: str
    item_name: str | None
    arrived_at: date
    #: 🔴 넷 다 nullable 이다. **NULL 을 0 으로 바꾸지 않는다** (DDL 주석).
    ordered_qty_kg: Decimal | None
    accepted_qty_kg: Decimal | None
    hold_qty_kg: Decimal | None
    rejected_qty_kg: Decimal | None
    #: 🔴 **`inbound_receipts.receipt_status` 컬럼이 아니라 유도값이다** (`ARRIVED` ·
    #:    `INSPECTED` · `PUTAWAY_DONE`). 근거는 사건 셋 — `arrived_at` ·
    #:    검수 `inspected_at` · 그 Receipt 의 Lot 과 원장 `IN`.
    #:    `INSPECTING` · `CLOSED` 는 사건이 아니라 진행 표시라 되살리지 않는다.
    receipt_status: str
    fact_source: str
    inspection_id: str | None
    inspection_verdict: str | None
    inspected_qty_kg: Decimal | None
    lot_id: str | None
    in_move_id: str | None
    #: Lot 과 원장 IN 이 **둘 다** 있을 때만 참.
    stock_applied: bool
    #: 그날까지 **수용 0 으로 재고 없이 입고 처리가 끝났나** (#805).
    #:
    #: 🔴 **이 판정을 여기서 만들지 않는다.** 정본은
    #:    `inbound_schedules.InboundScheduleView.settled_without_stock` 하나이고,
    #:    콘솔은 그 일정 한 벌에서 `inbound_id` 로 받아 적기만 한다. 같은 규칙을 두 벌
    #:    두면(예: 화면이 `accepted_qty_kg == 0` 으로 다시 판정) 경계에서 갈린다.
    #:
    #: 🔴 **`None` 은 «모른다» 다 — `False` 가 아니다.** 그날 입고 일정을 못 읽었으면
    #:    「반영 대기」인지 「반영할 재고 없음」인지 가릴 수 없다. 0 과 공란을 안 섞는
    #:    이 계약의 규율 그대로다.
    #:
    #: ★ `stock_applied` 와 **다른 사실이다.** 재고가 선 완료와 «만들 재고가 0 이라
    #:   끝난 완료» 는 둘 다 완료지만 재고는 한쪽에만 생긴다.
    settled_without_stock: bool | None = None


class ConsoleArrivalSummary(ConsoleModel):
    """`arrival.select_due_inbound` 의 네 갈래 건수.

    ⚠️ `due_count = 0` 이 정상일 수 있다 — `due` 는 `purchase_id` 까지 있어야 하는데
       마스터가 그 값을 안 넘기기로 확정했다. 도착일이 온 건은 `blocked` 로 나온다.
    """

    source_status: RuntimeSourceStatus
    due_count: int
    blocked_count: int
    not_due_count: int
    unresolved_count: int
    overdue_count: int


class ConsoleInboundResponse(ConsoleModel):
    sim_run_id: str
    as_of: date
    #: Receipt · 검수 · 재고반영의 시간축. 세 사건에서 되살린 값이다.
    receipt_time_basis: TimeBasis = "HISTORICAL_AS_OF"
    in_transit_status: RuntimeSourceStatus
    #: 🔴 `None`(미확인) 과 `[]`(0건 확인)은 다른 사실이다. `in_transit_status` 가 가른다.
    in_transit: list[ConsoleInTransitItem] | None
    receipts: list[ConsoleInboundReceipt]
    arrival_summary: ConsoleArrivalSummary


# ── 출고 ────────────────────────────────────────────────────────────────


class ConsoleAllocation(ConsoleModel):
    allocation_id: str
    lot_id: str
    pallet_id: str | None
    allocated_qty_kg: Decimal
    allocation_basis: AllocationBasis
    decided_by: str
    decided_at: datetime
    #: 🔴 **`as_of` 시점으로 유도한 값이다** — 저장된 `status` 컬럼이 아니다.
    #:    원장 OUT 이면 `SHIPPED`, 그 예약이 놓아준 뒤면 `CANCELLED`, 그 밖은
    #:    `ALLOCATED` 다 (`historical_repository.HistoricalAllocationState`).
    status: AllocationStatus
    note: str | None


class ConsoleReservation(ConsoleModel):
    """예약 한 줄과 그 아래 할당들.

    🔴 **`allocated_qty_kg` 와 `unallocated_qty_kg` 의 분모가 다르다. 일부러다.**

    ```text
    allocated_qty_kg    ALLOCATED · PICKED           아직 창고에서 안 나간 몫
    unallocated_qty_kg  reserved − (ALLOCATED · PICKED · SHIPPED)
                                                     아직 Lot 을 안 고른 몫
    ```

       `SHIPPED` 를 앞에서는 빼고 뒤에서는 넣는다 — 나간 몫은 이미 원장 OUT 이
       잔량에서 덜어냈고(그래서 '잡고 있는 양'이 아니다), 그 예약이 더 이상 새로
       잡아 둘 필요도 없다. `outbound.item_free_stock_qty` 와 **같은 규율**이다.
    """

    reservation_id: str
    item_id: str
    item_name: str | None
    sale_id: str | None
    required_qty_kg: Decimal
    #: ⚠️ **«이 예약이 확보했던 양» 이다 — 그날 잡고 있던 양이 아니다.**
    #:    `ConsoleInventoryItem.reserved_qty_kg`(지금 잡고 있는 양)와 뜻이 다르다.
    #:
    #:    ```text
    #:    전량 출고 뒤    안 줄어든다     나간 것은 «확보했던» 사실을 안 지운다
    #:    놓아준 뒤        안 줄어든다     WP-3 보정 2 — 과거 확보량을 지우지 않는다
    #:    ```
    #:
    #:    🔴 그래서 이 값으로 *"지금/그날 몇 kg 잡고 있나"* 를 읽으면 안 된다.
    #:       그 물음의 답은 `status`(그날 유도값) · `allocated_qty_kg` ·
    #:       `unallocated_qty_kg` 다 — 놓아준 날부터 뒤의 둘은 0 이 된다.
    reserved_qty_kg: Decimal
    allocated_qty_kg: Decimal
    unallocated_qty_kg: Decimal
    due_date: date | None
    #: 🔴 **`as_of` 시점으로 유도한 값이다** — 저장된 `status` 컬럼이 아니다.
    #:    놓아준 뒤(`released_as_of <= as_of`)에만 저장된 `RELEASED`/`CANCELLED` 를
    #:    쓰고, 그 전 날짜에는 할당 진행도로 다시 센다
    #:    (`historical_repository._reservation_status_at`).
    status: ReservationStatus
    allocations: list[ConsoleAllocation]


class ConsoleOutboundResponse(ConsoleModel):
    sim_run_id: str
    as_of: date
    #: 0건이면 `[]` 다. **더미를 만들지 않는다.**
    reservations: list[ConsoleReservation]
    #: 🔴 **예약·할당 축을 `as_of` 로 되살린다 (WP-3).**
    #:
    #:    ```text
    #:    예약 존재   sales.order_date <= as_of        ← 확정일부터 (납품일이 아니다)
    #:    예약 소멸   released_as_of <= as_of              ← M3 가 세운 칸
    #:    할당 존재   decided_at < timestamp_cutoff(as_of)
    #:    출고        MOVE-OUT-{allocation_id} · moved_at <= as_of
    #:    ```
    #:
    #:    유도의 주인은 `historical_repository.reservation_state_at` 하나다.
    #:    저장된 `inventory_reservations.status` · `inventory_allocations.status`
    #:    는 **지금** 값이라 과거 정본으로 쓰지 않는다.
    #:
    #:    ⚠️ **`reserved_qty_kg` 만 지금 값이다.** 확보량 변경 이력이 없어서인데,
    #:       예약을 세우는 유일한 경로(마스터 `outbound_flow`)가 그 판매의 납품일
    #:       하루에만 돌아 날짜를 넘긴 top-up 이 production 에 없다. 그 전제가
    #:       깨지면 이 칸부터 다시 본다.
    reservation_time_basis: TimeBasis = "HISTORICAL_AS_OF"


class ConsoleFefoCandidate(ConsoleModel):
    lot_id: str
    #: 🔴 *"이 Lot 에서 아직 다른 할당에 안 묶인 물리량"* 이다.
    #:    **추가로 예약할 수 있는 양이 아니다** (`recommend_fefo_candidates` 주석).
    available_qty_kg: Decimal
    remaining_freshness_days: int | None
    received_at: date
    #: DB raw 등급 그대로다 — FEFO 후보는 정규화하지 않는다.
    grade: str | None
