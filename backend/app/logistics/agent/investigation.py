"""조사 Runtime 의 **말**(계약) — 상태 · 예산 · LLM 입출력 · 결과.

🔴 **이 모듈에는 계산이 없다.** 업무 숫자는 전부 `agent.tools` 의 8개 Tool 이 낸다
   (§9). 여기 있는 것은 *"그 숫자를 어디에 담아 나르는가"* 뿐이다.

```text
LLM 이 하는 것     다음에 어떤 Tool 을 어떤 인자로 부를지 고른다   InvestigationStep
                  모은 사실을 문장으로 잇는다                     InvestigationReport
LLM 이 못 하는 것  재고 · 용량 · 신선도 · 예약 · 원가 · 판정        ← 전부 Tool 값이다
```

★ **왜 층을 따로 두나.** 그래프(`agent.graph`)는 이 계약만 알면 되고, 가짜 planner 를
  꽂아 LLM 없이 전체를 돌릴 수 있다 (매입 `selector` 와 같은 규율). 공급자 전송은
  `agent.llm_client` 한 곳에만 산다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.logistics.agent.tools import SUPPORTED_ACTIONS, ActionImpact
from app.logistics.llm.schemas import LLMErrorKind, LLMStatus

__all__ = [
    "ACTION_DECISION_OWNERS",
    "ALLOWED_TOOL_NAMES",
    "DEFAULT_BUDGET",
    "DUPLICATE_TOOL_CALL",
    "EXCEPTION_NOT_FOUND",
    "EXCEPTION_STATUS_UNRESOLVED",
    "INVALID_ARGUMENTS",
    "LLM_CONTRACT_VIOLATION",
    "PINNED_ARGUMENT_OVERRIDE",
    "PINNED_TOOL_ARGUMENTS",
    "REPLAN_BUDGET_EXCEEDED",
    "SUBJECT_OUT_OF_SCOPE",
    "TOOL_BUDGET_EXCEEDED",
    "UNKNOWN_TOOL",
    "AgentLLMBudgetExceeded",
    "AgentLLMDisabled",
    "AllowedToolName",
    "DecisionOwner",
    "EvaluatedOption",
    "FinalizeFn",
    "FinishReason",
    "InvestigationBudget",
    "InvestigationOption",
    "InvestigationReport",
    "InvestigationRequest",
    "InvestigationResult",
    "InvestigationState",
    "InvestigationStep",
    "InvestigationView",
    "PlanFn",
    "ToolCallRecord",
    "ToolCallStatus",
    "jsonable",
]


# ══════════════════════════════════════════════════════════════════════════
#  Tool allowlist — 정확히 Commit 3 의 8개다
# ══════════════════════════════════════════════════════════════════════════

#: 🔴 **이 8개가 전부다.** `run_sql` · `execute_purchase` · `sell_inventory` ·
#:    `update_exception` 같은 이름을 모델이 지어내면 `guard` 가 거부한다.
#:    목록을 넓히려면 Tool 을 먼저 만들어야 한다 — 여기를 먼저 고치면 안 된다.
AllowedToolName = Literal[
    "get_open_exceptions",
    "get_lot",
    "get_item_lots",
    "get_sales_commitments",
    "get_policy",
    "get_capacity_context",
    "get_inbound_schedule",
    "estimate_action_impact",
]

ALLOWED_TOOL_NAMES: tuple[str, ...] = (
    "get_open_exceptions",
    "get_lot",
    "get_item_lots",
    "get_sales_commitments",
    "get_policy",
    "get_capacity_context",
    "get_inbound_schedule",
    "estimate_action_impact",
)

#: 🔴 **Runtime 이 못 박는 두 축.** 모델이 이 키를 내면 무시가 아니라 **거부**한다 —
#:    조용히 덮으면 *"모델이 다른 실행/다른 날을 물었다"* 는 사실 자체가 사라진다.
#:    두 값의 정본은 언제나 `InvestigationRequest.sim_run_id` · `.as_of` 다.
PINNED_TOOL_ARGUMENTS: tuple[str, ...] = ("sim_run_id", "as_of")


# ══════════════════════════════════════════════════════════════════════════
#  거부/실패 어휘 — 사실처럼 생긴 실패를 LLM 에게 주지 않기 위한 구분
# ══════════════════════════════════════════════════════════════════════════

#: 허용 목록에 없는 이름이다 (지어낸 Tool 포함).
UNKNOWN_TOOL = "UNKNOWN_TOOL"
#: 인자가 스키마에 안 맞는다 — 없는 키 · 모르는 키 · 타입 불일치.
INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
#: `sim_run_id` · `as_of` 를 모델이 건드리려 했다.
PINNED_ARGUMENT_OVERRIDE = "PINNED_ARGUMENT_OVERRIDE"
#: 이 조사와 상관없는 Lot/품목을 물었다 (§17).
SUBJECT_OUT_OF_SCOPE = "SUBJECT_OUT_OF_SCOPE"
#: 같은 Tool 을 같은 인자로 또 불렀다.
DUPLICATE_TOOL_CALL = "DUPLICATE_TOOL_CALL"
#: Tool 호출 예산을 다 썼다.
TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
#: 재계획 예산을 다 썼다.
REPLAN_BUDGET_EXCEEDED = "REPLAN_BUDGET_EXCEEDED"
#: 모델이 계약을 깼다 — 호출 0건이거나 2건 이상이거나 스키마 위반이다.
LLM_CONTRACT_VIOLATION = "LLM_CONTRACT_VIOLATION"
#: 그날 그 Exception 이 목록에 없다.
EXCEPTION_NOT_FOUND = "EXCEPTION_NOT_FOUND"
#: 그날 상태를 못 댄다 — `PROPOSED` 로 넘어간 날을 적는 칸이 없다 (§26).
EXCEPTION_STATUS_UNRESOLVED = "EXCEPTION_STATUS_UNRESOLVED"


class AgentLLMBudgetExceeded(RuntimeError):
    """공급자 **전송** 예산을 다 썼다.

    🔴 **재시도와 교정도 전송이다.** 노드 수(`llm_call_count`)만 세면 *"한 노드가 두 번
       보냈다"* 가 안 보이고, 그러면 «11회» 라는 약속이 최악의 경우 22회가 된다.
       약속한 숫자가 실제 상한이어야 한다.
    """


class AgentLLMDisabled(RuntimeError):
    """공급자를 **일부러 꺼 뒀다.** 🔴 실패가 아니다.

    ★ 문자열로 «disabled» 를 찾아 가르지 않는다 — 공급자가 우연히 그 단어를 담은 오류를
      내면 진짜 장애가 «설정대로» 로 둔갑한다. 타입으로 가른다.
    """


class ToolCallStatus(str, Enum):
    """🔴 **Tool 실패를 정상 사실처럼 주지 않는다** (§23).

    ```text
    TOOL_SUCCESS   Tool 이 답을 냈다 — uncertainties 가 있어도 성공이다
    TOOL_REJECTED  guard 가 막았다   — Tool 까지 가지도 않았다
    TOOL_FAILED    Tool 이 터졌다    — DB 예외 등. 숫자가 없다
    ```

    ⚠️ `POLICY_NOT_HISTORICAL` 같은 `uncertainties` 는 **실패가 아니다.** Tool 이
       *"이건 못 잰다"* 를 정확히 말한 정상 답이다 — 그 구분을 지우면 모델이 *"조회가
       실패했으니 다시 부르자"* 로 오해하고 예산을 태운다.
    """

    SUCCESS = "TOOL_SUCCESS"
    REJECTED = "TOOL_REJECTED"
    FAILED = "TOOL_FAILED"


class FinishReason(str, Enum):
    """조사가 **왜** 멈췄나. `llm_status` 와 다른 축이다.

    ```text
    llm_status     AI 가 실제로 판단했나           SUCCESS · FALLBACK · DISABLED …
    finish_reason  그래프가 어디서 끝났나          FINISHED · BUDGET_EXCEEDED · …
    ```

    ★ 둘을 한 칸에 담으면 *"규칙 제안인데 조사는 정상 종료"* 같은 흔한 경우를 못 적는다.
    """

    #: LLM finalize 까지 정상 도달했다.
    FINISHED = "FINISHED"
    #: 그날 그 Exception 이 없다 — 조사 자체가 성립하지 않는다.
    NOT_FOUND = "NOT_FOUND"
    #: Tool/LLM 예산을 다 써서 규칙 제안으로 닫았다.
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    #: 재계획을 다 써서 규칙 제안으로 닫았다.
    GUARD_EXHAUSTED = "GUARD_EXHAUSTED"
    #: 조사 전체 시간 상한을 넘겼다.
    TIMEOUT = "TIMEOUT"
    #: 공급자 전송이 실패했다 — DB 는 아무것도 바뀌지 않는다.
    LLM_FAILED = "LLM_FAILED"


DecisionOwner = Literal["LOGISTICS", "SALES", "PURCHASE"]

#: 🔴 **누가 결정하는가는 결정론이 정한다** (§9.1 역할 경계). 모델이 고르는 칸이 아니다.
#:
#: ```text
#: SALES_PRIORITY_REQUEST   SALES      판매 수량·가격은 영업이 정한다
#: PURCHASE_ADJUST_REQUEST  PURCHASE   분할 회차·도착일은 매입이 정한다
#: ACCEPT_RISK              LOGISTICS  창고가 위험을 안고 간다
#: DISPOSAL_REQUEST         LOGISTICS  폐기는 창고 소관이다
#: ```
ACTION_DECISION_OWNERS: Mapping[str, DecisionOwner] = {
    "SALES_PRIORITY_REQUEST": "SALES",
    "PURCHASE_ADJUST_REQUEST": "PURCHASE",
    "ACCEPT_RISK": "LOGISTICS",
    "DISPOSAL_REQUEST": "LOGISTICS",
}


# ══════════════════════════════════════════════════════════════════════════
#  예산 — 한 곳에 모은다. 새 Config 표를 만들지 않는다 (§21)
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, kw_only=True)
class InvestigationBudget:
    """조사 한 번의 상한. 🔴 **하네스가 든다** — 모델에게 맡기지 않는다.

    ```text
    max_tool_calls  8     성공한 Tool 호출 수
    max_replans     2     guard 가 거부한 뒤 다시 계획하는 횟수
    max_llm_calls   11    plan/replan + finalize 를 **다 합쳐서**
    timeout         120s  조사 1회 전체
    ```

    ★ `max_llm_calls` 가 따로 있는 이유: 거부가 반복되면 Tool 은 하나도 안 늘어나는데
      전송만 늘어난다. Tool 예산으로는 그 경로를 못 막는다.

    🔴 **이 수는 두 곳에서 지켜진다.** 그래프는 노드가 계획자/정리자를 부른 **횟수**를
       세고, 공급자 층(`llm_client`)은 실제로 나간 **전송**을 센다 — 재시도와 스키마
       교정까지 포함해서. 둘 중 먼저 닿는 쪽이 막는다. 앞엣것만 있으면 한 노드가 두 번
       보내는 경로 때문에 «11회» 가 최악의 경우 22회가 된다.
    """

    max_tool_calls: int = 8
    max_replans: int = 2
    max_llm_calls: int = 11
    timeout_seconds: float = 120.0


DEFAULT_BUDGET = InvestigationBudget()


# ══════════════════════════════════════════════════════════════════════════
#  LLM 입출력 — 자유 텍스트에서 Tool 이름을 뽑지 않는다 (§13)
# ══════════════════════════════════════════════════════════════════════════


class InvestigationStep(BaseModel):
    """`plan_step` 한 번의 출력. **정확히 한 수**다.

    ```text
    공급자에게 선언하는 것   AllowedToolName 8개 + finish_investigation
    허용을 판정하는 곳       guard 한 곳                    ← 여기가 정본이다
    ```

    🔴 **둘을 다 둔다.** 선언만으로는 부족하다 — 구조화 출력을 무시하는 모델이 있다
       (재무 `_gemini_tool_call` 주석과 같은 근거). 그래서 지어낸 이름도 일단 여기까지
       **데이터로** 들어와 `guard` 에서 이름째 거부된다.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["CALL_TOOL", "FINISH"]
    #: 🔴 **일부러 `AllowedToolName` 로 못 박지 않는다.** `Literal` 로 두면 모델이
    #:    지어낸 이름(`run_sql` 등)이 파싱 단계에서 터져 **«무엇을 지어냈는가»가 기록에서
    #:    사라진다.** 허용 판정은 `guard` 한 곳이 한다 — 거기서 `UNKNOWN_TOOL:run_sql`
    #:    로 이름째 남는다. 공급자에게 **선언하는** 목록은 여전히 `AllowedToolName` 8개다.
    tool_name: str | None = None
    #: 🔴 `sim_run_id` · `as_of` 를 여기 담으면 `guard` 가 **거부**한다.
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class InvestigationOption(BaseModel):
    """LLM 이 제안한 대응 후보 하나. **실행이 아니라 후보다.**

    ⚠️ `parameters` 안의 숫자는 *검토값*이지 결정이 아니다 (§26). 판매 수량은 영업이,
       도착일은 매입이 정한다 — 여기 적힌 값은 `estimate_action_impact` 로 **검증만**
       한다.
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    #: 근거로 든 Tool 호출의 `sequence` 들. 없는 번호는 `evaluate_options` 가 버린다.
    evidence_refs: list[int] = Field(default_factory=list)


class InvestigationReport(BaseModel):
    """`finalize` 의 출력. 🔴 **숫자 칸이 없다.**

    ```text
    있는 것   summary · findings · missing_or_uncertain · options · recommended_index
    없는 것   재고 kg · 비율 · 금액 · 남은 일수                ← 전부 Tool 값이다
    ```

    ★ `summary` 는 자유 문장이라 모델이 숫자를 쓸 수는 있다. 그 숫자를 **별도 칸으로
      승격시키지 않는 것**이 이 스키마의 일이다 (§37). 권위 있는 숫자는 `ToolCallRecord`
      와 `EvaluatedOption.impact` 에만 산다.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    missing_or_uncertain: list[str] = Field(default_factory=list)
    options: list[InvestigationOption] = Field(default_factory=list)
    recommended_index: int | None = None


# ══════════════════════════════════════════════════════════════════════════
#  LLM 이 보는 전부
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, kw_only=True)
class InvestigationView:
    """모델에게 보내는 **유일한** 입력 — 외부 전송 경계이기도 하다.

    🔴 **DB 행이 여기 들어오지 않는다.** 들어오는 것은 Tool 이 이미 낸 답뿐이고, 그
       답은 `agent.tools` 가 look-ahead 를 걸러 낸 뒤의 값이다.

    ⚠️ 해석기(`llm.schemas.SanitizedLLMContext`)와 달리 **숫자를 싣는다.** 목적이 달라서다 —
       해석기는 결정론 결과를 설명만 하므로 숫자가 필요 없지만, 조사는 *"다음에 무엇을
       물을까"* 를 고르는 일이라 지금까지 본 사실을 봐야 한다. 대신 모델이 그 숫자를
       **결과로 승격시키지 못하게** 하는 것이 `InvestigationReport` 스키마의 몫이다.
    """

    sim_run_id: str
    as_of: date
    exception: Mapping[str, Any]
    #: 물어봐도 되는 Lot/품목 (§17). 여기 없는 것을 물으면 `guard` 가 막는다.
    scope: Mapping[str, Any]
    observations: tuple[Mapping[str, Any], ...]
    budget: Mapping[str, Any]
    #: 직전에 왜 거부됐나. 같은 실수를 반복하지 말라고 준다.
    last_rejection: str | None
    allowed_tools: tuple[str, ...] = ALLOWED_TOOL_NAMES
    action_catalog: tuple[str, ...] = SUPPORTED_ACTIONS


PlanFn = Callable[[InvestigationView], InvestigationStep]
FinalizeFn = Callable[[InvestigationView], InvestigationReport]


class Clock(Protocol):
    def __call__(self) -> float: ...


# ══════════════════════════════════════════════════════════════════════════
#  실행 기록 · 결과
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, kw_only=True)
class ToolCallRecord:
    """Tool 호출 한 번의 전부. *"왜 이렇게 판단했나"* 에 답할 수 있어야 한다 (§20).

    ★ 이번 Commit 에서는 **메모리에만** 산다 — audit 표를 만들지 않는다.
    """

    sequence: int
    tool_name: str
    arguments: Mapping[str, Any]
    status: ToolCallStatus
    #: 성공했을 때의 Tool 답 그대로 (frozen dataclass). 실패/거부면 `None`.
    answer: Any = None
    #: 🔴 `Tool` 이 낸 값이다. 여기서 만들지 않는다.
    observed_as_of: date | None = None
    uncertainties: tuple[str, ...] = ()
    #: 거부/실패 사유. 성공이면 `None`.
    detail: str | None = None
    #: 모델이 적은 이유 (자유 문장). 판단 근거가 아니라 **기록**이다.
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class EvaluatedOption:
    """결정론이 검증한 뒤의 후보. 🔴 **숫자는 `impact` 에만 있다.**

    ```text
    LLM 이 준 것        action · parameters · rationale · evidence_refs
    결정론이 붙인 것     decision_owner · parameters_are_hypothesis · impact · rejected_reason
    ```

    ⚠️ `impact.feasibility` 가 `UNRESOLVED` 면 그대로 둔다 (§27). 모델이 *"아마 될 것
       같다"* 고 해도 Tool 이 못 댄 것은 못 댄 것이다.
    """

    action: str
    parameters: Mapping[str, Any]
    rationale: str
    evidence_refs: tuple[int, ...]
    decision_owner: DecisionOwner
    #: 🔴 `SALES`/`PURCHASE` 소관이면 참이다 — `parameters` 의 수량·도착일은 **검토값**
    #:    이지 그 부서의 결정이 아니다 (§26).
    parameters_are_hypothesis: bool
    impact: ActionImpact | None = None
    #: 카탈로그 밖 행동 · 없는 근거 번호 등으로 버려졌다면 그 사유.
    rejected_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.rejected_reason is None


@dataclass(frozen=True, kw_only=True)
class InvestigationRequest:
    """조사 한 번의 입력. 🔴 **`sim_run_id` · `as_of` 의 정본이다.**"""

    sim_run_id: str
    as_of: date
    exception_id: str
    budget: InvestigationBudget = DEFAULT_BUDGET


@dataclass(frozen=True, kw_only=True)
class InvestigationResult:
    """조사 한 번의 결과. **DB 에 아무것도 안 쓴다** — Proposal 은 Commit 5 다.

    🔴 `observed_as_of` 규칙 (§24): 성공한 Tool 답의 관측일을 모아 `max`, **하나라도
       모르면 `None`.** `as_of` · 오늘 날짜 · 모델 응답 시각으로 메우지 않는다.
    """

    sim_run_id: str
    as_of: date
    exception_id: str
    finish_reason: FinishReason
    llm_status: LLMStatus
    llm_error_kind: LLMErrorKind | None = None
    summary: str = ""
    findings: tuple[str, ...] = ()
    missing_or_uncertain: tuple[str, ...] = ()
    options: tuple[EvaluatedOption, ...] = ()
    recommended_index: int | None = None
    tool_calls: tuple[ToolCallRecord, ...] = ()
    observed_as_of: date | None = None
    uncertainties: tuple[str, ...] = ()
    tool_call_count: int = 0
    replan_count: int = 0
    llm_call_count: int = 0

    @property
    def llm_applied(self) -> bool:
        """AI 가 실제로 판단했나. 매입 `MixDecision.applied` 와 같은 규율."""
        return self.llm_status == "SUCCESS"


# ══════════════════════════════════════════════════════════════════════════
#  그래프 상태
# ══════════════════════════════════════════════════════════════════════════


class InvestigationState(TypedDict, total=False):
    """LangGraph State. **노드는 바꿀 키만 담은 dict 를 반환한다** — 런타임이 병합한다.

    ★ 저장소 관습 그대로다 (`purchase_agent.state` · `sales.state`): `TypedDict`,
      reducer 없음, checkpointer 없음. 그래서 `conn` 같은 직렬화 불가능한 값도 들 수 있다.
    """

    # ── 못 박힌 입력 ─────────────────────────────────────────────────────
    conn: Any
    request: InvestigationRequest
    plan_fn: PlanFn
    finalize_fn: FinalizeFn
    clock: Clock
    #: 시계가 이 값을 넘으면 `TIMEOUT` 이다 (단조시계 기준).
    deadline_at: float

    # ── load_exception · load_context · preload_subject ──────────────────
    exception: Any
    subject_type: str
    subject_id: str
    allowed_lot_ids: frozenset[str]
    allowed_item_ids: frozenset[str]

    # ── 반복 ─────────────────────────────────────────────────────────────
    observations: tuple[ToolCallRecord, ...]
    #: 성공한 `(tool, canonical args)` — 중복 호출 판정의 기준이다.
    executed_keys: frozenset[str]
    step: InvestigationStep | None
    last_rejection: str | None
    tool_call_count: int
    replan_count: int
    llm_call_count: int
    sequence: int
    #: 🔴 **분기 결정은 노드가 하고 라우터는 읽기만 한다.** 그래야 분기 규칙을 노드
    #:    단위로 시험할 수 있다.
    #: ⚠️ 여기 **선언하지 않은 키는 LangGraph 가 조용히 버린다** — 노드가 돌려줘도
    #:    상태에 안 실린다. 라우터가 늘 기본값으로 가는 버그가 그렇게 난다.
    route: str
    #: `guard` 가 승인한 한 수 (검증·복원된 인자 포함). `execute_tool` 만 읽는다.
    verdict: Any

    # ── finalize · evaluate_options · finish ─────────────────────────────
    report: InvestigationReport | None
    options: tuple[EvaluatedOption, ...]
    recommended_index: int | None
    finish_reason: FinishReason
    llm_status: LLMStatus
    llm_error_kind: LLMErrorKind | None
    uncertainties: tuple[str, ...]
    result: InvestigationResult


# ══════════════════════════════════════════════════════════════════════════
#  직렬화 — 모델에게 보낼 수 있는 모양으로만 낮춘다
# ══════════════════════════════════════════════════════════════════════════


def jsonable(value: Any) -> Any:
    """Tool 답을 **JSON 으로 보낼 수 있는 모양**으로 낮춘다. 값은 안 바꾼다.

    🔴 **`@property` 도 싣는다.** `dataclasses.asdict` 만 쓰면 `LotFact` 의
       `freshness_remaining_ratio` · `uncommitted_observed_as_of` 같은 **계산된 사실이
       통째로 사라진다** — 모델이 못 본 사실은 물어볼 수도 없다.

    ⚠️ `Decimal` 은 **문자열로** 낸다. `float` 로 낮추면 소수 오차가 조용히 섞이고,
       그 값이 다시 `estimate_action_impact` 로 돌아오면 `_quantity` 가 `float` 을
       거부해 이유 없는 `UNRESOLVED` 가 된다.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        payload = {field.name: jsonable(getattr(value, field.name)) for field in fields(value)}
        for name in _property_names(type(value)):
            payload[name] = jsonable(getattr(value, name))
        return payload
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {_key(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, str | bytes):
        return value.decode() if isinstance(value, bytes) else value
    if isinstance(value, Sequence | set | frozenset):
        return [jsonable(item) for item in value]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)


def _property_names(cls: type) -> tuple[str, ...]:
    """MRO 를 걸어 공개 `@property` 이름을 모은다 — 상속된 것도 놓치지 않는다."""
    names: list[str] = []
    for klass in cls.__mro__:
        for name, member in vars(klass).items():
            if isinstance(member, property) and not name.startswith("_") and name not in names:
                names.append(name)
    return tuple(names)


def _key(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def collect_ids(value: Any, *, field_names: Iterable[str]) -> frozenset[str]:
    """Tool 답 안에 실제로 실린 식별자를 긁는다 — **범위는 증거가 넓힌다** (§17).

    ★ 모델이 아무 Lot 이나 물을 수 없게 하면서도 조사상 정당한 확장은 막지 않는 가장
      단순한 규칙이다: *"이미 눈앞에 놓인 것만 더 물어볼 수 있다."* 복잡한 ACL 을 두지
      않는다 (MVP).
    """
    wanted = frozenset(field_names)
    found: set[str] = set()

    def walk(node: Any) -> None:
        if is_dataclass(node) and not isinstance(node, type):
            for field in fields(node):
                item = getattr(node, field.name)
                if field.name in wanted and isinstance(item, str) and item:
                    found.add(item)
                else:
                    walk(item)
            return
        if isinstance(node, Mapping):
            for key, item in node.items():
                if key in wanted and isinstance(item, str) and item:
                    found.add(item)
                else:
                    walk(item)
            return
        if isinstance(node, str | bytes):
            return
        if isinstance(node, Sequence | set | frozenset):
            for item in node:
                walk(item)

    walk(value)
    return frozenset(found)
