"""오케스트레이터 API 요청·응답 스키마.

★ 오케스트레이터는 원본 DB 를 직접 읽지 않는다 (§5.1). 모든 입력은 요청 본문으로 온다 —
  부서 회신(밴드 기여)·매입/판매 후보를 받아 T3(매입)·S3(판매) 결합·클리핑을 수행한다.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator.llm.schemas import LLMResponseFields

RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
Dept = Literal["sales", "inventory", "finance"]
StrategyType = Literal["quantity", "timing", "mix"]
Verdict = Literal["ok", "conditional", "reject", "skipped"]
CheckKind = Literal["hard", "soft"]
Severity = Literal["LOW", "MEDIUM", "HIGH"]
EndCode = Literal["E1_APPROVED", "E2_HELD", "E3_REJECTED", "E4_NOT_STARTED", "E5_NO_FEASIBLE_PLAN"]


# ---------------------------------------------------------------------------
# 공통 입력 — 부서 회신 (밴드 기여)
# ---------------------------------------------------------------------------
class BandCheckIn(BaseModel):
    """부서 self-check 1건. 밴드 기여 방향은 dept 로 정해진다 (§3.4.5-④).

    영업 → floor_kg / 재고 → cap_kg·cap_total_kg·cap_by_date_kg / 재무 → cap_amount_krw.
    허용되지 않은 필드를 채우면 계약이 막고 API 는 422 를 낸다.
    """

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1)
    kind: CheckKind = "hard"
    verdict: Verdict = "ok"
    reason: str = ""
    floor_kg: dict[str, float] | None = None
    cap_kg: dict[str, float] | None = None
    cap_total_kg: float | None = None
    cap_amount_krw: float | None = None
    cap_by_date_kg: dict[date, float] | None = None
    allow_loose_cap: bool = False
    severity: Severity = "MEDIUM"


class DeptReplyIn(BaseModel):
    """부서당 1회 회신 (§3.6.1). 품목별이면 item 을 채운다 (영업)."""

    model_config = ConfigDict(extra="forbid")

    dept: Dept
    runtime_status: RuntimeStatus = "READY"
    item: str | None = None
    reasoning: str = ""
    checks: list[BandCheckIn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 매입(T3) 입력 — 시나리오
# ---------------------------------------------------------------------------
class SplitLegIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset_days: int = Field(ge=0)
    qty_kg: dict[str, float]
    expected_arrival_date: date | None = None
    amount_krw: dict[str, float] | None = None
    """회차별 금액. `contracts.core.SplitLeg.amount_krw` 와 **같은 모양**이다 -
       품목별 매핑인 근거는 그쪽에 적혀 있다.

       ⚠️ 선택 필드다. 없으면 `SplitLeg.amount_krw` 가 None 으로 남고
       `check_triple_identity` 의 split 금액 변은 통째로 건너뛴다 - 그것이 위반이 아니다.
       🔴 `0.0` 이나 빈 매핑으로 채우지 않는다 - 없는 것과 0 원은 다르다."""


class SourcingLotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1)
    grade: str = Field(min_length=1)
    qty_kg: float = Field(gt=0)
    unit_price_krw_per_kg: float = Field(gt=0)
    ref_ids: list[str] = Field(default_factory=list)
    min_lot_kg: float | None = None


class ScenarioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    strategy_type: StrategyType = "quantity"
    stance: str = "기준"
    qty_kg: dict[str, float]
    unit_price_krw_per_kg: dict[str, float]
    split_plan: list[SplitLegIn] = Field(default_factory=list)
    sourcing_plan: list[SourcingLotIn] = Field(default_factory=list)
    # ── 매입 명세 v1.1 수신·보존 필드 (선택, 밴드·클리핑에 영향 없음) ──
    total_amount_krw: float | None = None
    """사중일치 금액축(명세 §2). 수신·보존만 한다 - 클리핑 금액은 qty×unit_price 파생(동일값)."""
    margin_warning: bool | None = None
    """3값 자문 표시 - true 경고 / false 정상 / null 미계산. 컷 아님, 검증에 영향 없음."""


class ProcurementRequest(BaseModel):
    """T3 — 매입 시나리오 밴드 결합·클리핑 요청."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    run_seq: int = Field(default=1, ge=1)
    snapshot_id: str | None = None
    items: list[str] | None = None
    spot_price_krw_per_kg: dict[str, float] | None = None
    replies: list[DeptReplyIn] = Field(min_length=1)
    scenarios: list[ScenarioIn] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 판매(S3) 입력 — 배분 후보
# ---------------------------------------------------------------------------
class ChannelLegIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1)  # KIMCHI_FACTORY | SCHOOL_MEAL | SPOT | HOLD
    item: str = Field(min_length=1)
    qty_kg: float = Field(ge=0)
    unit_price_krw_per_kg: float = Field(ge=0)
    lot_ids: list[str] = Field(default_factory=list)
    due_date: date | None = None


class OutboundLegIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    qty_kg: float = Field(ge=0)


class AllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocation_id: str = Field(min_length=1)
    strategy_type: str = "균형"
    legs: list[ChannelLegIn] = Field(min_length=1)
    expected_contribution_krw: float = 0.0
    outbound_by_date: list[OutboundLegIn] = Field(default_factory=list)
    estimation_confidence: str = ""


class SalesRequest(BaseModel):
    """S3 — 판매 배분 후보 공용 출고 결합·클리핑 요청.

    replies 는 재고(cap 밴드) + 재무(soft 신호). 재무는 밴드를 움직이지 않는다 (§3.1).
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    run_seq: int = Field(default=1, ge=1)
    snapshot_id: str | None = None
    items: list[str] | None = None
    replies: list[DeptReplyIn] = Field(min_length=1)
    allocations: list[AllocationIn] = Field(min_length=1)


class DayRequest(BaseModel):
    """하루 전체 — 매입(T3) → 판매(S3) 코어를 순차로 돌린다."""

    model_config = ConfigDict(extra="forbid")

    procurement: ProcurementRequest
    sales: SalesRequest | None = None


# ---------------------------------------------------------------------------
# 응답
# ---------------------------------------------------------------------------
class BandOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor_kg: dict[str, float]
    cap_kg: dict[str, float | None]
    cap_total_kg: float | None
    cap_amount_krw: float | None
    cap_by_date_kg: dict[date, float]
    contributors: dict[str, str]
    not_ready: list[str]
    usable: bool


class OutboundBandOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cap_kg: dict[str, float | None]
    cap_total_kg: float | None
    cap_total_effective_kg: float | None
    contributors: dict[str, str]
    soft_notes: list[str]


class DeadlockOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str
    item: str | None
    shortfall: float
    unit: str
    responsible_checks: list[str]


class ClipResultOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    clipped_qty_kg: dict[str, float]
    total_kg: float
    original_total_kg: float
    clip_ratio: float
    clipped: bool
    over_clipped: bool
    binding_constraints: list[str]
    identity_problems: list[str]
    infeasible: bool
    clipped_amount_krw: float


class ProcurementResponse(LLMResponseFields):
    """T3 결과 — 그날의 매입 제약 밴드 + 후보별 클리핑 + 순위 (+ LLM 선정)."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["orchestrator"] = "orchestrator"
    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    band: BandOut
    deadlock: DeadlockOut | None
    clip_results: list[ClipResultOut]
    ranked_ids: list[str]
    recommended_id: str | None
    soft_warnings: list[str]


class SalesResponse(LLMResponseFields):
    """S3 결과 — 공용 출고 밴드 + 후보별 클리핑 + 순위 (+ LLM 선정)."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["orchestrator"] = "orchestrator"
    cycle: Literal["SALES"] = "SALES"
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    outbound_band: OutboundBandOut
    clip_results: list[ClipResultOut]
    ranked_ids: list[str]
    recommended_id: str | None
    variant_collapsed: bool
    soft_warnings: list[str]


class DayResponse(BaseModel):
    """하루 전체 결과. end_code 는 코어 기준의 간이 판정이다 —
    파산선·미충족 의무 판정은 전체 T0Snapshot 이 필요해 후속 작업이다."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["orchestrator"] = "orchestrator"
    as_of: date
    end_code: EndCode
    reason: str = ""
    procurement: ProcurementResponse
    sales: SalesResponse | None
