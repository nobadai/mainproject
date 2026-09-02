"""마스터 API 입출력 스키마.

★ 봉투(`envelope.py`)와 API 스키마를 따로 둔다.
  봉투는 **마스터↔에이전트 내부 계약**이고 이건 **외부 노출 계약**이다. 하나로 합치면
  화면 요구가 바뀔 때마다 에이전트 계약이 흔들린다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.master.decision import DecisionOut
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
    item: str | None = Field(
        default=None,
        description=(
            "이번 실행이 다루는 품목 (배추·무·양파·피마늘). 매입은 품목 하나씩 돈다. "
            "주지 않으면 마스터가 싣지 않고, 매입이 missing_data: ['item'] 을 낸다. "
            "4품목을 한 번에 도는 것은 미결 — 재무 cap 이 품목 공통이라 배분 규칙이 없다(M-26)."
        ),
    )

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
    prior_feedback: dict[str, Any] | None = Field(
        default=None,
        description=(
            "사람이 조건을 붙여 다시 요청한 경우 그 조건. 매입 `prior_feedback` 계약으로 "
            "**사용자의 말 그대로** 넘어간다 — 마스터가 숫자로 해석하지 않는다. "
            "⚠️ 매입은 현재 이 값을 `is_refeed`·`attempt` 메타로만 쓰고 안을 바꾸지 않는다."
        ),
    )


class EvidenceOut(BaseModel):
    """부서가 낸 근거 하나. **숫자가 어디서 왔는가.**

    🔴 **전에는 검증까지만 가고 화면에서 끊겼다** (2026-09-02 배선).
      `flow` 가 모아 검증 Tool 에 넘기는데 응답 스키마에 자리가 없었다. 그래서 화면은
      *"재무 상한 2,000만원"* 은 보여주면서 그 숫자의 출처는 못 보여줬다.
      `verdicts[].reasoning` 은 부서가 쓴 **설명 문장**이지 출처가 아니다.

    ★ **마스터는 고르지도 요약하지도 않는다.** 부서가 낸 것을 그대로 옮긴다 (§3.2.2).
      고르는 것이 곧 판단이고, 그 순간 화면의 근거가 마스터의 의견이 된다.

    ★ `evidence_grade` 가 값만큼 중요하다. 같은 숫자라도 `OFFICIAL` 과 `ASSUMED` 는
      판단의 무게가 다르다 - `input_sources` 를 등급까지 실은 것과 같은 이유다.
    """

    agent: AgentName
    #: 어느 호출에서 나온 근거인가. `PRE_PURCHASE` 는 경계("상한이 왜 그 값인가"),
    #: `SCENARIO_VALIDATION` 은 판정("이 안이 왜 ok 인가") - 답하는 질문이 다르다.
    mode: str

    #: 무엇에 대한 근거인가 (예: `finance_cap_amount_krw`).
    claim: str
    #: 어디서 온 값인가 - inventory · sales · finance · documents · tool_calc · persona.
    source: str
    #: 🔴 **`float | str` 이다.** 계약(`contracts_core.Evidence.value`)은 `float` 인데
    #: 실제로는 문자열이 오는 근거가 있다 - 재무 `policy_version_used` 가
    #: `"v1.3-PROVISIONAL"` 을 싣는다 (`finance/capabilities/procurement.py:175`).
    #: `Evidence` 가 dataclass 라 런타임 검증이 없어 지금까지 아무도 몰랐고,
    #: 근거를 화면으로 내보내려다 처음 드러났다 (2026-09-02).
    #:
    #: **마스터는 값을 고치지도 버리지도 않는다.** 고치면 남의 값을 덮어쓰는 것이고
    #: (§3.2.2), 버리면 근거를 고르는 것이다. 원본을 나르고 **어긋난 사실은
    #: `concerns` 로 드러낸다** - 재호출로 안 고쳐지는 남의 계약 문제라 정확히
    #: concerns 의 자리다.
    value: float | str
    unit: str
    #: OFFICIAL · VENDOR · SIM_FIXED · ASSUMED · INVALID_FOR_HARD.
    evidence_grade: str
    #: SIM_FIXED 는 여기에 승인 회차가 적힌다.
    evidence_detail: str = ""
    #: 원본 레코드 참조. **비어 있을 수 없다** - 봉투가 막는다 (§1.2-5).
    ref_ids: list[str] = []


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

    #: 🔴 **부서가 밝힌 사유.** `missing_data` 가 *"무엇이 없어서"* 라면 이건
    #: *"왜 터졌는지"* 다. 없으면 이력을 파도 `runtime_status=ERROR` 까지만 알고
    #: 그 ERROR 의 사유는 어디에도 없다 (재현성 측정 2026-09-02).
    reasoning: str = ""

    #: 🔴 **그 부서 안에서 LLM 이 돌았나.** 없으면 부서가 규칙으로 답한 것과
    #: 모델로 답한 것이 화면에서 같아 보인다 — 오늘 마스터에서 고친 것과 같은 종류다.
    llm_status: str = "DISABLED"
    llm_model: str = ""
    llm_attempts: int = 0
    llm_fallback_used: bool = False

    #: 🔴 **부서가 계획을 다시 세운 횟수.** `llm_attempts` 와 뜻이 다르다.
    #: `llm_attempts` 는 Planner + Finalizer 호출 수라 툴 개수를 따라 커지고
    #: 재시도가 아니다 (재무 정정 2026-09-02). 재계획은 이 값이다.
    replans: int = 0


class BlockedAgentOut(BaseModel):
    """기여하지 못한 부서 하나 — **이름 옆에 사유가 있다.**

    🔴 `blocked_by` 는 이름만 든다. 그것만 받은 사람이 할 수 있는 것은
      *"다시 돌려 본다"* 뿐이고, 그건 조사가 아니라 추측이다 (2026-09-02).
    """

    agent: AgentName
    runtime_status: str = Field(
        description="ERROR · RUNTIME_NOT_READY, 그리고 아예 안 불린 경우 NOT_CALLED."
    )
    reasoning: str = ""
    missing_data: list[str] = []
    detail: str = Field(
        description=(
            "사람이 읽는 한 줄. **서버가 한 곳에서 만든다** — `reason` 문장에 들어간 "
            "것과 같은 값이라 화면이 다시 조립하다 갈리지 않는다."
        )
    )


class ProcurementRunResponse(BaseModel):
    request_id: str
    as_of: date

    #: 🔴 **이 실행이 이력에 남은 행의 id** (2026-08-30 신설).
    #:
    #: `plan[].run_id` 와 **다른 것이다** — 저쪽은 그 *부서 호출* 의 id 이고,
    #: 이것은 *마스터 실행 한 번* 의 id 다. 이름이 겹쳐 헷갈리므로 여기서는
    #: `history_run_id` 로 부른다 (DB 컬럼은 `master_agent_runs.run_id`).
    #:
    #: 화면이 승인할 때 이 값을 되돌려 준다 — 그래야 *"내가 본 그것을 승인했다"* 가
    #: 기록된다. 없으면 서버가 최신 실행을 고르는데, 그 사이 재실행이 있었으면
    #: **사람이 본 것과 다른 안이 승인된 것으로 남는다.**
    #:
    #: 적재에 실패하면 `None` 이다 — 이력이 없어도 계산 결과는 돌려준다.
    history_run_id: str | None = None

    end_code: EndCode
    reason: str

    scenarios: list[dict[str, Any]] = []
    judgment: dict[str, Any] = Field(
        default={},
        description=(
            "매입 제안의 판정부 — `scenarios` 를 뺀 제안 최상위 전부 "
            "(situation · allowed_axes · confidence · meta · no_proposal_reason …). "
            '"왜 3안인지/2안인지"의 근거이며 프론트 판정 헤더가 소비한다. '
            "`verdicts`(조언자·검증 판정)와 다르다 — 이건 **매입 자신의** 판정이다."
        ),
    )
    constraints: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, dict[str, Any]] = {}

    evidences: list[EvidenceOut] = Field(
        default=[],
        description=(
            "부서가 낸 근거 - 시나리오 숫자의 출처. **마스터가 고르거나 요약하지 않는다.** "
            "`mode` 로 경계 근거(PRE_PURCHASE)와 판정 근거(SCENARIO_VALIDATION)를 가른다. "
            "비어 있으면 '근거가 완비됐다' 가 아니라 **부서가 근거를 안 냈다**는 뜻이다."
        ),
    )

    blocked_by: list[AgentName] = []
    blocked_failures: list[BlockedAgentOut] = Field(
        default=[],
        description=(
            "막은 부서가 **왜** 막았는가. `blocked_by` 와 같은 부서를 가리키되 사유를 "
            "함께 든다. 비어 있는데 `blocked_by` 가 차 있으면 Flow 밖에서 막힌 것이다 "
            "(어댑터 미등록)."
        ),
    )
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

    report_text: str = Field(
        default="",
        description=(
            "사람이 읽는 리포트 (마스터 역할 ⑥). **규칙만으로 만든다** — 숫자·결론은 "
            "종료 코드와 부서 값에서 그대로 온다. 발화문 경로에서는 여기에 LLM 이 쓴 "
            "한 문장이 앞에 얹힌다."
        ),
    )

    input_sources: dict[str, str] = Field(
        default={},
        description=(
            "마스터가 실어 준 입력 3종의 출처 — `등급:소스` (§3.2.5 · `inputs.py`). "
            "등급은 MEASURED(실 DB 그대로) · DERIVED(실 DB 값에서 파생) · MOCK · MISSING. "
            "**같은 값이라도 어디서 왔느냐로 판단의 무게가 다르다** — 값만 실으면 "
            "리포트를 읽는 사람이 전부 실측으로 읽는다."
        ),
    )
    mocked_inputs: list[str] = Field(
        default=[],
        description=(
            "🔴 mock 에서 온 입력. **비어 있지 않으면 이 실행의 결론을 실측으로 읽으면 "
            "안 된다.** 검증 커버리지를 분수로 내는 것과 같은 이유로 감추지 않는다."
        ),
    )


class ReportOut(BaseModel):
    """`GET /master/runs/{request_id}/report` — 들고 나갈 수 있는 매입안 문서.

    ★ **화면이 만들지 않는다.** 서버가 낸 Markdown 을 그대로 내려받는다 —
      화면이 문서를 조립하기 시작하면 **화면과 문서가 다른 숫자**를 말하게 된다.
    """

    request_id: str
    filename: str
    #: Markdown 전문. 붙여 넣기·메신저·이슈 어디에도 그대로 들어간다.
    markdown: str


class DailyClosingOut(BaseModel):
    """하루치 마감 한 줄. **번인 구간의 실제 값이고 에이전트가 만든 것이 아니다.**"""

    close_date: date
    day_no: int
    #: 🔴 **무차입 기준 현금.** 재무가 답하는 `available_cash` 와 다르다 —
    #: 그쪽은 대출 실행분이 더해진 값이다. 둘을 같은 줄에 두면 화면이 거짓말을 한다.
    base_cash_balance_krw: float | None = None
    loan_cash_balance_krw: float | None = None
    receivables_balance_krw: float | None = None
    inventory_qty_kg: float | None = None
    sales_recognized_krw: float | None = None
    collection_cash_in_krw: float | None = None
    purchase_cash_out_krw: float | None = None
    #: 마감되지 않은 날이 섞이면 그 사실이 답의 일부다 — 지우지 않는다.
    closed: bool = False


class BurnInOut(BaseModel):
    """`GET /master/burn-in` — **에이전트가 판단하기 전에 회사가 어떻게 왔는가.**

    🔴 에이전트가 12-31 에 *"살 안이 없다"* 고 답하는데, 그 앞 30일을 안 보면
    **시스템이 고장 난 것처럼 읽힌다.** 결론 옆에 경로를 둔다.

    ★ **읽기 전용이다.** 하루를 진행시키는 것은 승인이 발주로 흘러가야 성립하고,
      그건 각 파트의 상태 전이 로직이다 (아직 없다 · 별도 이슈).
    """

    sim_run_id: str
    run_type: str
    period_start: date
    period_end: date
    #: 에이전트가 **처음 판단하는 날**. 번인의 마지막 날과 같다.
    as_of: date
    status: str
    financing_mode: str | None = None
    note: str | None = None
    closings: list[DailyClosingOut] = []


class RunHistoryOut(BaseModel):
    """`GET /master/runs/{request_id}` — 그 요청이 어떻게 됐나.

    ★ `plan` 은 응답 원문 안이 아니라 **별도 컬럼**에서 온다. 검증 Tool 의
      ④ 실행 계획 온전성 검사(M-16)가 이것만 읽기 때문이다.
    """

    request_id: str
    #: 🔴 **돌려주는 이 행의 id.** 같은 업무 키에 실행이 여러 행이라, 이게 없으면
    #: 화면이 *"지금 보는 계획이 어느 실행 것인가"* 와 *"이 결정이 그 실행을
    #: 가리키나"* 를 대조할 수 없다.
    run_id: str | None = None
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

    decisions: list[DecisionOut] = Field(
        default=[],
        description=(
            "사람의 결정 이력. 최신 하나가 `is_current` 다. 비어 있으면 아직 미결정 — "
            '"그 요청 어떻게 됐냐"에 한 번의 호출로 답하기 위해 여기 싣는다.'
        ),
    )


class TriggerAck(BaseModel):
    """ML 완료 이벤트 수신 확인."""

    accepted: bool
    request_id: str
    as_of: date
    note: Literal["queued", "executed"] = "executed"
