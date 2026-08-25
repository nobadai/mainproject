"""Critic API 요청·응답 스키마.

★ Critic 은 오케스트레이터 산출물을 **검증만** 한다. 숫자를 바꾸지 않는다.
  요청은 오케 procurement 와 같은 입력(부서 회신·매입 후보)을 받아, 내부에서 T3 결합·클리핑을
  재현한 뒤 그 결과를 6레이어로 검증한다. 입력 스키마는 오케와 공유한다.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.critic.llm.schemas import LLMResponseFields

# 매입/판매 후보 입력 계약은 오케와 공유한다 (같은 것을 두 벌 정의하지 않는다).
from app.orchestrator.schemas import AllocationIn, ScenarioIn

Dept = Literal["sales", "inventory", "finance"]
CriticStatus = Literal["PASS", "CONCERN", "FAIL"]
RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
CheckKind = Literal["hard", "soft"]
Verdict = Literal["ok", "conditional", "reject", "skipped"]
Severity = Literal["LOW", "MEDIUM", "HIGH"]
EvidenceGrade = Literal["OFFICIAL", "VENDOR", "SIM_FIXED", "ASSUMED"]


class EvidenceIn(BaseModel):
    """검사 1건이 딛는 근거. Critic 은 ref_ids 존재와 등급을 본다 (§1.2-5).

    ★ 이 API 층은 소스 DB 를 재조회하지 않는다. 값 대조는 회신이 제출한 evidence 안에서
      이뤄지므로, 값의 진위가 아니라 **근거의 구조·바인딩**을 검증한다.
    """

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    source: Literal["inventory", "sales", "finance", "documents", "tool_calc", "persona"] = (
        "tool_calc"
    )
    ref_ids: list[str] = Field(min_length=1)
    value: float
    unit: str = ""
    evidence_grade: EvidenceGrade = "OFFICIAL"
    evidence_detail: str = ""


class CheckIn(BaseModel):
    """부서 self-check 1건 (밴드 기여 + 근거). 오케 BandCheckIn 에 evidences 를 더한 형태다."""

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
    evidences: list[EvidenceIn] = Field(default_factory=list)


class DeptReplyIn(BaseModel):
    """부서당 1회 회신. Critic 은 as_of 로 스냅샷 바인딩을 대조한다."""

    model_config = ConfigDict(extra="forbid")

    dept: Dept
    runtime_status: RuntimeStatus = "READY"
    item: str | None = None
    reasoning: str = ""
    checks: list[CheckIn] = Field(default_factory=list)


class DeptMetaIn(BaseModel):
    """계약(CheckResult)에 없는 두 가지를 부서가 사이드카로 제출한다.

    inputs_used     : {check_id: [입력 키...]}  - E-GRADE-LEAK · E-SCENARIO-LEAK
    produced_fields : 부서가 회신에 담은 필드 이름들 - E-AUTHORITY (S3 전속 침범)
    """

    model_config = ConfigDict(extra="forbid")

    inputs_used: dict[str, list[str]] = Field(default_factory=dict)
    produced_fields: list[str] = Field(default_factory=list)


class CriticProcurementRequest(BaseModel):
    """Critic A - 매입 T3 결과 검증 요청.

    오케 procurement 와 같은 입력(scenarios + replies)을 받는다. 내부에서 밴드 결합·클리핑을
    재현한 뒤 target_scenario_id(없으면 첫 실행가능안)를 6레이어로 검증한다.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    run_seq: int = Field(default=1, ge=1)
    snapshot_id: str | None = None
    price_basis: str = "AUCTION"
    contract_price_basis: str = "AUCTION"
    inbound_lead_days: int | None = None
    items: list[str] | None = None
    spot_price_krw_per_kg: dict[str, float] | None = None
    scenarios: list[ScenarioIn] = Field(min_length=1)
    replies: list[DeptReplyIn] = Field(min_length=1)
    dept_meta: dict[Dept, DeptMetaIn] | None = None
    target_scenario_id: str | None = None
    rationale: str = ""
    """L5 가 검사할 **결정 근거** - 오케 selector 가 쓴 문장(`rationale_per_id[선택안]`).

    ★ 부서 회신(`reasoning`)이 아니다. 부서 문장은 클리핑 **이전**에 작성되므로
      클리핑 후에야 정해지는 binding_constraints 를 언급할 수 없다. 그것을 누락으로
      판정하면 정상 실행마다 CONCERN 이 붙어 소음이 된다.
      미제출이면 L5 는 검사할 문장이 없으므로 skipped 로 드러난다.
    """
    unattended: bool = False


class LotConstraintIn(BaseModel):
    """물류 B 출력의 로트 1건 - on_hand 초과·신선도 재검산 입력."""

    model_config = ConfigDict(extra="forbid")

    lot_id: str = Field(min_length=1)
    item: str = Field(min_length=1)
    available_qty_kg: float = Field(ge=0)
    remaining_freshness_days: int
    status: Literal["AVAILABLE", "RESERVED", "EXPIRED"] = "AVAILABLE"


class ArrivalLegIn(BaseModel):
    """H1 승인 약정의 도착 1회분 - overlay cap_by_date 재검산 입력."""

    model_config = ConfigDict(extra="forbid")

    date: date
    qty_kg: float = Field(gt=0)
    split_index: int = Field(default=1, ge=1)


class CommitmentIn(BaseModel):
    """H1 승인 매입 overlay. total_qty_kg 는 arrival_schedule 합으로 계산한다."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1)
    arrival_schedule: list[ArrivalLegIn] = Field(min_length=1)


class CriticSalesRequest(BaseModel):
    """Critic B - 판매 S3 결과 검증 요청.

    S3 를 재현(combine_outbound_band → clip_allocations)한 뒤 대상 배분을 L4-7~10 으로
    검증한다. overlay/로트/점유가 미제출이면 해당 검사는 skipped 로 드러낸다 (설계서 §8).
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    run_seq: int = Field(default=1, ge=1)
    snapshot_id: str | None = None
    items: list[str] | None = None
    replies: list[DeptReplyIn] = Field(min_length=1)  # 재고(cap) + 재무(soft)
    allocations: list[AllocationIn] = Field(min_length=1)
    lot_constraints: list[LotConstraintIn] = Field(default_factory=list)
    commitment: CommitmentIn | None = None  # H1 승인 overlay (L4-7)
    warehouse_free_kg: float = 0.0  # N2 (L4-7)
    confirmed_occupancy_by_date: dict[date, float] = Field(default_factory=dict)  # N15 (L4-7)
    dept_meta: dict[Dept, DeptMetaIn] | None = None
    target_allocation_id: str | None = None
    rationale: str = ""
    """L5 가 검사할 결정 근거 - S3 선정이 쓴 문장. 매입과 같은 이유로 부서 회신과 구분한다."""


# ---------------------------------------------------------------------------
# 응답
# ---------------------------------------------------------------------------
class FindingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: str
    check_id: str
    detail: str
    dept: str | None
    route: str | None


class ConcernOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str
    layer: str
    dept: str | None


class CriticVerdictOut(LLMResponseFields):
    """CriticVerdictV04 의 JSON 표현 (+ L5 LLM 상태). 커버리지는 감추지 않는다 (설계서 §8)."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["critic"] = "critic"
    cycle: Literal["A", "B"]
    as_of: date
    run_seq: int
    scenario_id: str
    runtime_status: RuntimeStatus
    status: CriticStatus
    badge: str
    coverage: dict[str, tuple[int, int]]
    coverage_ratio: tuple[int, int]
    findings: list[FindingOut]
    concerns: list[ConcernOut]
    skipped: list[str]
    end_stage: str | None
