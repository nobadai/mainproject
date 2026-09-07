"""마스터 API 입출력 스키마.

★ 봉투(`envelope.py`)와 API 스키마를 따로 둔다.
  봉투는 **마스터↔에이전트 내부 계약**이고 이건 **외부 노출 계약**이다. 하나로 합치면
  화면 요구가 바뀔 때마다 에이전트 계약이 흔들린다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.contracts.core import ITEMS, EndCode
from app.master.day_gate import DayGate
from app.master.decision import DecisionOut
from app.master.envelope import AgentName, Trigger
from app.master.sales_flow import SalesEndCode

SalesBusinessMode = Literal[
    "CONTRACT_FULFILLMENT",
    "CONTRACT_PROPOSAL_NEW",
    "CONTRACT_PROPOSAL_RENEWAL",
    "SPOT_SALES",
]
"""판매 사이클의 영업 모드. **어휘의 주인은 판매다** (`app/sales/schemas.py`).

🔴 **그런데 마스터가 `app.sales.schemas` 를 import 하지 않는다.** `Capability` 때와
  같은 이유다 — 조정자가 부서 스키마에 런타임으로 묶이면 부서가 자기 모델을 고치는 날
  마스터 API 가 같이 흔들린다.

★ **대신 테스트가 양쪽을 대조한다** (`tests/master/test_sales_entrypoint.py`).
  갈려도 런타임에는 아무 소리가 안 나기 때문이다 — 판매가 모드를 하나 늘리면
  마스터 문 앞에서 422 가 나고, 그것은 *"그런 모드는 없다"* 로 읽힌다.
"""


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
            "이번 실행이 다루는 품목 (배추·무·양파). 매입은 품목 하나씩 돈다. "
            "주지 않으면 마스터가 싣지 않고, 매입이 missing_data: ['item'] 을 낸다. "
            "전 품목을 한 번에 도는 것은 미결 — 재무 cap 이 품목 공통이라 배분 규칙이 없다(M-26)."
        ),
    )

    @field_validator("item")
    @classmethod
    def _item_is_in_the_contract(cls, value: str | None) -> str | None:
        """🔴 **계약 밖 품목을 문 앞에서 거른다** (2026-09-03).

        전에는 아무도 안 걸렀다. 피마늘을 계약에서 뺀 뒤 실측하니 요청이 **Critic
        L0 까지 가서** `E-UNKNOWN-ITEM` 으로 죽었다. 막히기는 하는데 늦다 —
        그때까지 매입을 부르고 안을 만들고 세 부서를 다 돈다.

        ★ **`None` 은 통과시킨다.** 품목을 안 준 것과 없는 품목을 준 것은 다르다.
          안 주면 매입이 `missing_data: ["item"]` 으로 그 사실을 낸다 (§1.2-10).

        ★ **`app/ml/router.py:35` 와 같은 모양이다.** ML 이 이미 이렇게 거르고
          있었고, 같은 일을 다른 방식으로 하지 않는다.
        """
        if value is not None and value not in ITEMS:
            raise ValueError(
                f"지원하지 않는 품목입니다: {value}. 가능: {', '.join(ITEMS)}"
            )
        return value

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

    #: 🔴 **부서가 스스로 남긴 관측. 마스터는 읽지 않고 나른다.**
    #:
    #: 값은 처음부터 `ExecutionMetadata` → `ExecutionStep` 까지 왔는데 **여기서
    #: 끊겼다** (2026-09-02 · #165 에서 드러남). 재무가 provider 대체 사실
    #: (gemini → ollama · HTTP_429)을 여기 싣는데 응답·화면·매입 이력 어디에도
    #: 안 나갔다. `replans` · `evidences` · 조정안에 이은 네 번째 누락이다.
    #:
    #: ⚠️ **부서마다 모양이 다른 JSON 문자열이다.** 마스터도 화면도 파싱하지 않는다 -
    #: 파싱하면 부서 스키마가 한 벌 더 생기고, 부서가 필드를 바꾸는 날 이쪽만
    #: 옛말을 한다 (`AdvisorVerdicts` 가 부서 payload 를 안 펴는 것과 같은 이유).
    observations: list[str] = []


class AdjustmentOut(BaseModel):
    """부서가 낸 조정안 하나 - **봉투 표준형 그대로.**

    🔴 전에는 개수만 나갔다 (`verdicts[].suggested_adjustments`). 되먹임 계약 §3.2 의
      `constraint` 가 바로 이 객체 배열이라, 개수만 남기면 되먹임을 붙이는 순간
      나를 값이 없다 (2026-09-02).

    ★ **부서 원시형이 아니다.** 같은 사실이 `verdicts[].payload` 에도 부서 모양으로
      남아 있는데, 그쪽을 파서 쓰면 마스터가 남의 스키마를 해석하는 것이 된다
      (§3.2.2). 표준형은 그 해석을 안 하려고 있는 자리다.
    """

    dept: str = Field(description="AgentName 이 아니라 Dept 다. 어휘가 다르다 (_AGENT_DEPT).")
    axis: str = Field(description="quantity · timing · channel_mix · amount. 봉투가 강제한다.")
    target_value: float
    unit: str = Field(
        description=(
            "kg · krw · d. **닫힌 집합이 아니다** - 봉투가 검사하지 않는다. "
            "물류 타이밍 축은 봉투 as_of 로부터의 일수를 'd' 로 싣는다."
        )
    )
    reason: str = Field(description="부서가 쓴 문장 그대로. 마스터가 요약하지 않는다.")
    ref_ids: list[str]

    #: 🆕 이 조정이 어느 시나리오 대상인가 (2026-09-02 · 계약 v0.2 §5.1).
    #: 전에는 `reason` 문장 안에만 있어 기계가 읽으려면 부서 문장을 파싱해야 했다.
    #: 합쳐진 건이면 합쳐진 라벨을 다 담는다 - 건수를 안 늘리면서 사실이 드러난다.
    scenario_labels: list[str] = []

    #: 🆕 어느 회차의 상한인가 (2026-09-02 · 계약 v0.2 §5.2).
    #: **번호가 아니라 날짜다** - 물류에 회차 번호가 없어 번호 칸을 두면 없는 값을
    #: 만들게 된다. 회차 개념이 없는 축(재무 amount)은 `null` 이다.
    split_date: date | None = None


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

    #: 🔴 **개장 관문 결과** (계약 `260904_마스터_통보_개장Gate_응답모양_next_action`).
    #:
    #:   ```text
    #:   요청 진입 → open_day Gate → execution day Gate → Purchase Flow
    #:   ```
    #:
    #: ★ **화면은 `day_gate.gate` 만 보고 막는다.** `end_code` 는 *"시작 안 했다"* 까지만
    #:   말하고, **왜** 인지는 이 블록이 나른다 — 토요일은 개장을 통과하고 실행일에서
    #:   막히는데, `E4_NOT_STARTED` 하나로는 그 둘이 같아 보인다.
    #:
    #: ⚠️ `None` 은 **관문을 안 물었다**는 뜻이다 (개장 구현이 없는 경로).
    day_gate: DayGate | None = None

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

    adjustments: list[AdjustmentOut] = Field(
        default=[],
        description=(
            "부서가 낸 조정안 - **개수가 아니라 내용이다.** 마스터가 고르거나 정렬하지 "
            "않고 온 차례 그대로 싣는다. 되먹임 계약 §3.2 의 `constraint` 가 이 배열이다. "
            "비어 있는 것이 곧 실패는 아니다 - 물류는 `reject` 안의 조정을 승격하지 "
            "않으므로(#121) 0건이 정답인 날이 있다."
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


# ---------------------------------------------------------------------------
# 판매 사이클 — **매입과 대칭으로 두되 한 벌로 묶지 않는다** (설계 §1 · 2026-09-07)
#
# 🔴 두 사이클은 응답 모델도 종료 코드도 다르다. 공유하는 것은 **판정(개장 Gate)과
#   순서**이지 응답이 아니다. 억지로 묶으면 판매 종료 코드가 매입 어휘로 새거나 그
#   반대가 된다 — `SL2_NO_CANDIDATE` 를 `E2_HELD` 로 적는 날이 온다.
# ---------------------------------------------------------------------------


class SalesRunRequest(BaseModel):
    """사용자가 눌러서 시작하는 판매 요청 (설계 §2).

    ★ `ProcurementRunRequest` 와 대칭이되 **판매에만 있는 것이 셋**이다 —
      `business_mode` · `partner_id` · `user_request`.

    🔴 **`has_unmet_obligation` 은 싣지 않는다.** 그것은 매입 `E5` 판정 전용이고
      **판매가 준 사실을 매입이 쓰는 값**이다. 판매 요청에 되돌려 실으면 순환이다 —
      판매가 자기가 준 사실을 자기 입력으로 다시 받는다.
    """

    model_config = {"extra": "forbid"}

    as_of: date
    policy_version: str = Field(min_length=1)
    trigger: Trigger = "USER_REQUEST"
    request_id: str | None = Field(
        default=None,
        description="주지 않으면 마스터가 만든다. 같은 날 재실행을 구분하려면 직접 준다.",
    )

    #: 🔴 **16 이다. 매입 12 가 아니다** (설계 §3).
    #:
    #:   골격에 `SALES_BUDGET = 16` 이 있지만 **요청이 12 를 들고 오면 그 값이 이긴다.**
    #:   매입 스키마를 복사해 오면 실제로 그렇게 되고, 소진은 `SL5_BUDGET_EXHAUSTED`
    #:   로 조용히 남는다 — 판단이 안 끝난 날이 늘어나는데 아무 오류도 안 난다.
    #:
    #:   ```text
    #:   후보 3 · 되먹임 2회 최악 경우
    #:     inventory PRE_SALES            1
    #:     sales GENERATE_SALES_PROPOSAL  3   (최초 1 + 되먹임 2)
    #:     finance SALES_VALIDATION       9   (후보 3 × 회차 3)
    #:                                   13
    #:   +2  후보 범위·날짜가 바뀌어 물류를 다시 부르는 경우 (판매 v1.7 §5)
    #:   +1  S-2 (ERROR 1회 재시도) 여유
    #:                                   16
    #:   ```
    #:
    #:   ★ 산식의 주인은 `sales_flow.SALES_BUDGET` docstring 이다. 여기 적힌 것은
    #:     **왜 12 가 아닌지**를 고치는 사람이 바로 보게 하려는 사본이고, 값 자체는
    #:     기본값 하나뿐이다.
    budget: int = Field(
        default=16,
        ge=1,
        le=50,
        description=(
            "에이전트 호출 상한 (§1.2-12). 판매 기본값은 16 — 후보 3 · 되먹임 2회면 "
            "물류 1 + 판매 3 + 재무 9 = 13 이고, 물류 재조회 2 · S-2 재시도 1 을 더해 16. "
            "매입 기본값 12 를 그대로 쓰면 골격의 SALES_BUDGET 을 요청이 이긴다."
        ),
    )

    item: str | None = Field(
        default=None,
        description=(
            "이번 실행이 다루는 품목 (배추·무·양파). 주지 않으면 마스터가 싣지 않고, "
            "판매가 missing_data 로 그 사실을 낸다."
        ),
    )

    # ── 판매에만 있는 셋 ────────────────────────────────────────────
    business_mode: SalesBusinessMode = Field(
        description=(
            "무슨 판매인가 — 계약 이행 · 신규 제안 · 갱신 제안 · 현물. "
            "어휘의 주인은 판매이고 마스터는 자기 Literal 로 선언한다 (SalesBusinessMode)."
        )
    )
    partner_id: str | None = Field(
        default=None,
        description=(
            "거래처. 계약 이행·갱신에서는 사실상 필수지만 **마스터가 강제하지 않는다** — "
            "무엇이 필요한지는 판매가 정한다 (§3.2.2)."
        ),
    )
    user_request: str | None = Field(
        default=None,
        description=(
            "사용자가 말한 것 **그대로**. 마스터가 숫자로 해석해 제약에 꽂지 않는다 — "
            "해석은 판매가 한다 (매입 `prior_feedback` 과 같은 자리)."
        ),
    )

    @field_validator("item")
    @classmethod
    def _item_is_in_the_contract(cls, value: str | None) -> str | None:
        """계약 밖 품목을 문 앞에서 거른다 — **매입과 같은 규칙이다.**

        ★ `ProcurementRunRequest._item_is_in_the_contract` 와 같은 것을 판매에서도
          한다. 두 사이클이 같은 3품목 계약을 쓰므로 문 앞 판정이 갈리면 안 된다.

        ★ **`None` 은 통과시킨다.** 품목을 안 준 것과 없는 품목을 준 것은 다르다.
        """
        if value is not None and value not in ITEMS:
            raise ValueError(f"지원하지 않는 품목입니다: {value}. 가능: {', '.join(ITEMS)}")
        return value


class SalesCandidateOut(BaseModel):
    """판매 후보 하나와 그 판정 — **골격 `CandidateVerdict` 를 그대로 옮긴다.**

    🔴 **`passed` · `unvalidated` · `detail` 을 화면이 다시 계산하면 안 된다.**
      통과 판정은 허용목록(`PASSING_VERDICTS`)으로 정해지는데, 그 목록은 봉투 어휘가
      늘 때 같이 는다. 화면이 *"reject 가 아니면 통과"* 로 다시 세면 어휘가 는 날
      새 값이 통과 쪽으로 샌다 (#173 이 고친 것과 같은 실수).
      **주인은 `CandidateVerdict` 이고 여기 실린 것은 그 답이다.**
    """

    #: 판매가 낸 후보 그대로. **마스터는 고르지도 재계산하지도 않는다** (§3.2.2).
    scenario: dict[str, Any]

    #: capability → 그 검증의 회신. 키는 판매가 요구한 이름 그대로다.
    validations: dict[str, dict[str, Any]] = {}

    #: 🔴 **부를 대상이 없어 못 물어본 요구.** 비어 있지 않으면 통과로 치지 않는다.
    unroutable: list[str] = []

    passed: bool
    #: 요구한 검증이 하나도 없었다 — **통과로 나가지만 아무도 안 본 안이다.**
    unvalidated: bool
    #: 왜 탈락했나. 부서가 쓴 문장 그대로이고 마스터가 요약하지 않는다.
    detail: str


class SalesRunResponse(BaseModel):
    """판매 Flow 한 번의 결과.

    ★ **매입 응답과 닮았지만 담는 것이 다르다.** 매입은 *"시나리오 배열 + 부서별
      판정"* 이고 판매는 **후보마다 자기 판정을 들고 있다** — 부분 통과가 정상이라
      부서 축으로 접으면 어느 후보가 왜 떨어졌는지가 사라진다 (C-1).
    """

    request_id: str
    as_of: date

    #: 개장 관문 결과. `None` 은 **관문을 안 물었다**는 뜻이다.
    #:
    #: 🔴 **판매에는 실행일 관문이 없다** — 주말에도 판다 (설계 §1). 그래서 매입과
    #:   달리 이 블록이 `BLOCKED` 인 것 말고 *"안 도는 날"* 이 없다.
    day_gate: DayGate | None = None

    #: 이 실행이 이력에 남은 행의 id (`master_agent_runs.run_id`). 적재 실패면 `None`.
    history_run_id: str | None = None

    end_code: SalesEndCode
    reason: str

    candidates: list[SalesCandidateOut] = Field(
        default=[],
        description=(
            "통과·탈락을 **한 칸에** 담는다. 가르는 것은 각 후보의 `passed` 다 — "
            "두 칸으로 두면 같은 후보가 양쪽에 들어가는 날을 아무도 못 막는다. "
            "SL1 에서도 탈락 후보가 비어 있지 않을 수 있다 (사유를 동봉해 함께 낸다)."
        ),
    )

    judgment: dict[str, Any] = Field(
        default={},
        description=(
            "`scenarios` 를 뺀 제안 최상위 — 판매의 situation · business_mode · self_check. "
            "**키를 고르지 않는다** — 화이트리스트로 뽑으면 판매가 판정 필드를 늘릴 때마다 "
            "마스터를 고쳐야 하고, 빠뜨린 키는 커버리지를 감춘 상태가 된다."
        ),
    )

    supply_context: dict[str, Any] = Field(
        default={},
        description=(
            "②에서 받은 초기 물류 컨텍스트. 못 받았으면 비어 있고 사유는 `context_failure` 다."
        ),
    )
    context_failure: BlockedAgentOut | None = Field(
        default=None,
        description=(
            "🔴 물류가 컨텍스트를 못 냈다는 사실. 판매는 밴드가 없어 여기서 멈추지 않지만, "
            "멈추지 않는 것과 없던 일로 하는 것은 다르다 — 후보의 질이 왜 떨어졌는지를 "
            "나중에 읽는 사람이 볼 수 있어야 한다."
        ),
    )

    evidences: list[EvidenceOut] = Field(
        default=[],
        description="부서가 낸 근거. **마스터가 고르거나 요약하지 않는다** — 매입과 같은 규율이다.",
    )
    adjustments: list[AdjustmentOut] = Field(
        default=[],
        description="부서가 낸 조정안 표준형. 되먹임에 실린 것과 같은 값이다.",
    )

    feedback_attempts: int = Field(
        default=0,
        description=(
            "실제로 돈 되먹임 회차. **0 이면 되먹임하지 않았다** — 통과 후보가 있었거나 "
            "권위 있는 대안이 없었다."
        ),
    )

    plan: list[StepOut] = []
    plan_signature: list[tuple[str, str, int]] = Field(
        default=[],
        description="누구를 어떤 목적으로 몇 번째로 불렀는가. 같은 입력에 같은 값이어야 한다.",
    )

    report_text: str = Field(
        default="",
        description=(
            "🔴 **마스터가 판매 문장을 짓지 않는다** (설계 §5). 추천 문장과 순위는 판매 "
            "소유이고 마스터는 순위를 재계산하지 않는다 (판매 v1.7 §18). 그래서 "
            "`SL1_PRESENTED` 에서는 **비어 있다** — 그 날의 문장은 판매가 낸 것이 답이다. "
            "Flow 가 접힌 날(SL2~SL5)만 **왜 접혔는지** 한 줄이 들어간다. 업무 판단이 "
            "아니라 실행 사실이다."
        ),
    )
