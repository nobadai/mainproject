"""마스터 API 입출력 스키마.

★ 봉투(`envelope.py`)와 API 스키마를 따로 둔다.
  봉투는 **마스터↔에이전트 내부 계약**이고 이건 **외부 노출 계약**이다. 하나로 합치면
  화면 요구가 바뀔 때마다 에이전트 계약이 흔들린다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.master.envelope import AgentName, Trigger
from app.orchestrator.contracts_core import EndCode


class ProcurementRunRequest(BaseModel):
    """사용자 요청 또는 ML 완료 Trigger."""

    model_config = {"extra": "forbid"}

    as_of: date
    policy_version: str = Field(min_length=1)
    trigger: Trigger = "USER_REQUEST"
    request_id: str | None = Field(
        default=None,
        description="주지 않으면 마스터가 만든다. 같은 날 재실행을 구분하려면 직접 준다.",
    )
    has_unmet_obligation: bool = Field(
        default=False,
        description=(
            "판매 Rule 이 주는 사실 — B2B 확정 납품을 채울 수 없는 날이면 참. E5 판정에만 쓴다."
        ),
    )
    budget: int = Field(default=12, ge=1, le=50, description="에이전트 호출 상한 (§1.2-12)")

    # ── §3.2.5 의 명시적 예외 — 마스터가 실어 주는 값 ───────────────
    forecast: dict[str, Any] | None = Field(
        default=None,
        description=(
            "ML 예측. ML 은 호출 구조 밖 독립 실행이라 '해당 에이전트에게 요청'이 성립하지 "
            "않는다. `generated_at` 이 as_of 이후면 마스터가 싣지 않는다."
        ),
    )
    confirmed_orders: dict[str, Any] | None = Field(
        default=None,
        description="계약 납품 요구량. 1차 판매는 에이전트가 아니라 마스터 관할 Rule 이다.",
    )
    policy_values: dict[str, Any] | None = Field(
        default=None,
        description="계약 판매단가·마진 방어선 등 정책 테이블 값. 운반 주체는 미결(M-19).",
    )


class StepOut(BaseModel):
    """실행 계획의 한 걸음. **시각을 담지 않는다** — 재현성 비교 대상이다."""

    seq: int
    agent: AgentName
    mode: str
    call_seq: int
    run_id: str
    runtime_status: str
    business_status: str
    used_tools: list[str] = []
    finding_codes: list[str] = []
    missing_data: list[str] = []


class ProcurementRunResponse(BaseModel):
    request_id: str
    as_of: date
    end_code: EndCode
    reason: str

    scenarios: list[dict[str, Any]] = []
    constraints: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, dict[str, Any]] = {}

    blocked_by: list[AgentName] = []
    findings: list[str] = Field(
        default=[],
        description="**매입 재호출을 유발한** 발견. 다시 만들면 달라질 수 있는 것만 여기 든다.",
    )
    concerns: list[str] = Field(
        default=[],
        description=(
            "사실이지만 **재호출로 고쳐지지 않는** 것 — 조언자의 계약 위반 · 마스터 "
            "배선 문제. 사람이 봐야 한다 (§3.4)."
        ),
    )
    skipped_checks: list[str] = Field(
        default=[],
        description=(
            "검증 Tool 이 **판정하지 못한** 검사와 사유. 비어 있는 findings 를 "
            "'전부 통과'로 읽지 않게 한다 (§3.7.6 커버리지를 감추지 않는다)."
        ),
    )
    verification_skipped: bool = False
    purchase_attempts: int = 0

    presentable: bool = False
    single_option: bool = False

    plan: list[StepOut] = []
    plan_signature: list[tuple[str, str, int]] = Field(
        default=[],
        description="누구를 어떤 목적으로 몇 번째로 불렀는가. 같은 입력에 같은 값이어야 한다.",
    )
    missing_adapters: list[AgentName] = Field(
        default=[],
        description="어댑터가 아직 등록되지 않은 에이전트. 비어 있지 않으면 end_code 는 E4 다.",
    )


class RunHistoryOut(BaseModel):
    """`GET /master/runs/{request_id}` — 그 요청이 어떻게 됐나.

    ★ `plan` 은 응답 원문 안이 아니라 **별도 컬럼**에서 온다. 검증 Tool 의
      ④ 실행 계획 온전성 검사(M-16)가 이것만 읽기 때문이다.
    """

    request_id: str
    as_of: date
    agent: str
    cycle: str
    runtime_status: str
    elapsed_ms: int | None = None
    created_at: datetime

    plan: list[dict[str, Any]] = []
    plan_signature: list[tuple[str, str, int]] = []

    request_payload: dict[str, Any] = {}
    response_payload: dict[str, Any] = {}


class TriggerAck(BaseModel):
    """ML 완료 이벤트 수신 확인."""

    accepted: bool
    request_id: str
    as_of: date
    note: Literal["queued", "executed"] = "executed"
