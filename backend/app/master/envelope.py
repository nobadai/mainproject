"""
envelope.py — 마스터 ↔ 에이전트 공용 봉투 (M-1 공통 이벤트 규약 v0.2)

정의서 v2.2 §7.1 이 정한 **Business 와 Execution 의 분리**를 타입으로 구현한다.

    AgentRequest        마스터 → 에이전트   context(request_id·as_of·mode) + 업무 입력
    AgentReply          에이전트 → 마스터   업무 결과 + 상태 2종 + Evidence + 조정안
    ExecutionMetadata   별도 저장           used_tools·tool_order·LLM 상태 (run_id 로 연결)

★ 새 어휘를 만들지 않는다.
  `RuntimeStatus` · `Verdict` · `Evidence` · `SuggestedAdjustment` 는 전부
  `contracts_core` 의 기존 타입을 그대로 쓴다. 신설은 봉투와 `Mode` 뿐이다.

★ 두 층으로 나눠 강제한다.
  ┌─ 타입 레벨 (즉시 ContractViolation) ─ 봉투가 성립하지 않는 것
  │    빈 request_id · call_seq < 1 · 에이전트가 못 받는 mode
  │    RUNTIME_NOT_READY 인데 missing_data 가 비었음
  └─ 검증 함수 (EnvelopeFinding 반환) ─ 마스터가 받아 보고 판단할 것
       바인딩 불일치 · Evidence 미첨부 · reasoning 규칙 위반

  전자는 **보낼 수 없게** 막고, 후자는 **받은 뒤 판정**한다. 후자를 예외로 터뜨리면
  에이전트 하나의 실수가 사이클 전체를 죽인다 — 마스터가 정할 몫이다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.orchestrator.contracts_core import (
    ContractViolation,
    Dept,
    Evidence,
    RuntimeStatus,
    SuggestedAdjustment,
    Verdict,
)

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 1. 어휘
# ---------------------------------------------------------------------------

AgentName = Literal["finance", "inventory", "purchase"]
"""★ `Dept` 와 다르다.

`Dept` 는 **밴드에 기여하는 조언자**(sales·inventory·finance)이고,
`AgentName` 은 **마스터가 호출하는 대상**이다. 매입은 제안자라 조언자가 아니고,
영업은 1차 구성에서 빠졌다 (정의서 v2.2 §2.1)."""

Mode = Literal[
    "PRE_PURCHASE",
    "SCENARIO_VALIDATION",
    "GENERATE_SCENARIOS",
    "STATUS_QUERY",
]
"""호출 목적 (정의서 §3.2.3).

같은 에이전트가 서로 다른 업무를 수행하므로 **무엇을 요청하는지**를 실어 보낸다.
`PRE_PURCHASE` 는 *경계*를, `SCENARIO_VALIDATION` 은 *판정*을 돌려준다."""

Trigger = Literal["ML_COMPLETE", "USER_REQUEST"]

LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]

_AGENT_MODES: dict[AgentName, frozenset[Mode]] = {
    "finance": frozenset({"PRE_PURCHASE", "SCENARIO_VALIDATION", "STATUS_QUERY"}),
    "inventory": frozenset({"PRE_PURCHASE", "SCENARIO_VALIDATION", "STATUS_QUERY"}),
    "purchase": frozenset({"GENERATE_SCENARIOS", "STATUS_QUERY"}),
}

_AGENT_DEPT: dict[AgentName, Dept] = {
    "finance": "finance",
    "inventory": "inventory",
}
"""매입은 여기 없다 — 축 조정을 제안할 권한이 없다 (제안자 ≠ 조언자)."""


def agent_allowed_modes(agent: AgentName) -> frozenset[Mode]:
    return _AGENT_MODES[agent]


# ---------------------------------------------------------------------------
# 2. 실행 컨텍스트 — 스냅샷을 대체하는 것 (정의서 §3.2.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionContext:
    """한 요청의 시점·정책을 고정한다.

    ★ T0 스냅샷은 폐지됐지만 **시점 계약은 남는다** (정의서 §1.2-6).
      각 에이전트의 Tool 이 `as_of` 로 조회를 자르지 않으면 백테스트가 성립하지 않는다.
      데이터 누수는 에러를 내지 않고 손익만 좋아지므로 계약으로 막아야 한다.
    """

    request_id: str
    as_of: date
    trigger: Trigger
    policy_version: str

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ContractViolation("request_id 는 비울 수 없다 — 검증 L1 바인딩의 근거다.")
        if not self.policy_version.strip():
            raise ContractViolation("policy_version 은 비울 수 없다 — 재현 4종의 하나다 (§3.2.4).")


# ---------------------------------------------------------------------------
# 3. AgentRequest — 마스터 → 에이전트
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRequest:
    """★ `budget_remaining` 을 싣지 않는다 (M-1 v0.2 · 재무 파트 합의).

    노출하면 판단이 오염된다 — "마지막 호출이니 보수적으로" 는 도메인 판단이 아니라
    예산 반응이다. 호출 예산은 **마스터의 관리 수단**이지 에이전트의 입력이 아니다.
    """

    context: ExecutionContext
    agent: AgentName
    mode: Mode
    call_seq: int = 1
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.call_seq < 1:
            raise ContractViolation(f"call_seq 는 1 이상이다 (받음: {self.call_seq}).")
        allowed = _AGENT_MODES[self.agent]
        if self.mode not in allowed:
            raise ContractViolation(
                f"{self.agent} 는 mode={self.mode} 를 받을 수 없다. 허용: {sorted(allowed)}"
            )


# ---------------------------------------------------------------------------
# 4. AgentReply — 에이전트 → 마스터 (Business)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentReply:
    """업무 결과만 담는다. 실행 흔적은 `ExecutionMetadata` 로 분리한다 (§7.1).

    ★ `next_agent` 필드는 없다.
      라우팅은 마스터 책임이다 (정의서 §2.3 · §3.3). 에이전트는
      `needs_followup` 으로 "추가 검증이 필요하다"까지만 말한다.
    """

    request_id: str
    as_of: date
    agent: AgentName
    mode: Mode
    run_id: str

    runtime_status: RuntimeStatus
    business_status: Verdict

    payload: Mapping[str, Any] = field(default_factory=dict)
    evidences: tuple[Evidence, ...] = ()
    suggested_adjustments: tuple[SuggestedAdjustment, ...] = ()
    reasoning: str = ""

    needs_followup: bool = False
    additional_validation_required: bool = False
    missing_data: tuple[str, ...] = ()
    missing_capability: tuple[str, ...] = ()

    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ContractViolation(
                "run_id 는 비울 수 없다 — 검증 Tool 이 ExecutionMetadata 를 찾는 키다 (§3.7.4)."
            )
        if self.runtime_status == "RUNTIME_NOT_READY" and not self.missing_data:
            raise ContractViolation(
                "RUNTIME_NOT_READY 는 missing_data 로 무엇이 없는지 밝혀야 한다. "
                "이름 없이 오면 마스터가 사용자에게 무엇을 요청할지 알 수 없다 (M-1 §5.1)."
            )
        for adj in self.suggested_adjustments:
            expected = _AGENT_DEPT.get(self.agent)
            if expected is None:
                raise ContractViolation(
                    f"{self.agent} 는 축 조정을 제안할 수 없다 (제안자는 조언자가 아니다)."
                )
            if adj.dept != expected:
                raise ContractViolation(f"{self.agent} 회신에 {adj.dept} 의 조정안이 섞였다.")

    @property
    def contributes_to_band(self) -> bool:
        """READY 가 아니면 밴드에 기여하지 않는다.

        조용히 건너뛰면 그 부서의 상한이 무한대로 남아 **무제한 매입이 통과한다.**
        마스터는 `not_ready` 로 명시 기록하고 종료 코드는 E4(미시작)로 다룬다.
        """
        return self.runtime_status == "READY"

    @property
    def worth_retry(self) -> bool:
        """`ERROR` 만 재시도 가치가 있다 (M-1 v0.2 §5.1).

        `RUNTIME_NOT_READY` 는 입력이 없어서 못 낸 답이므로 다시 불러도 같다.
        재시도하면 호출 예산만 태운다 (정의서 §1.2-12).
        """
        return self.runtime_status == "ERROR"


# ---------------------------------------------------------------------------
# 5. ExecutionMetadata — 별도 저장 (§7.1-②)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionMetadata:
    """실행 흔적. Business Reply 와 섞지 않는다.

    ★ 검증 Tool 의 ④ 실행 계획 온전성 검사가 이것을 읽는다 (정의서 §3.7.4).
      분리하되 `run_id` 로 접근할 수 있어야 한다.
    """

    run_id: str
    request_id: str
    agent: AgentName

    used_tools: tuple[str, ...] = ()
    tool_order: tuple[int, ...] = ()
    observations: tuple[str, ...] = ()
    rules_applied: tuple[str, ...] = ()
    replans: int = 0

    llm_status: LLMStatus = "DISABLED"
    llm_model: str = ""
    llm_attempts: int = 0
    llm_fallback_used: bool = False

    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if self.tool_order and len(self.tool_order) != len(self.used_tools):
            raise ContractViolation(
                f"tool_order({len(self.tool_order)}) 와 used_tools({len(self.used_tools)}) 의 "
                "길이가 다르다 — 실행 계획을 재현할 수 없다 (§1.2-11)."
            )


# ---------------------------------------------------------------------------
# 6. 검증 — 마스터가 회신을 받고 돌린다
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvelopeFinding:
    code: str
    where: str
    detail: str


# 3자리 이상 연속 숫자(천단위 구분 포함) = 수량·금액·날짜로 본다.
# "D+7" 같은 상대 표현은 통과시킨다 — 실측에서 정상 문장에 자주 쓰인다.
_BIG_NUMBER = re.compile(r"\d[\d,]{2,}")
_SENTENCE_SPLIT = re.compile(r"[.!?。]\s*|\n+")
_LABEL = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CLAIM_PATH = re.compile(r"^(?P<key>[^\[\].]+)\[(?P<sel>[^\]]+)\]\.(?P<sub>.+)$")

_MAX_REASONING_SENTENCES = 3


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_label(value: Any) -> bool:
    """판정 라벨인가 — 대문자·숫자·밑줄로만 된 문자열 (휴리스틱).

    `payment_pressure: "MEDIUM"` 은 숫자가 아니지만 매입의 행동을 바꾼다.
    근거 없이 오면 **LLM 이 만든 라벨과 구분되지 않는다.**
    """
    return isinstance(value, str) and bool(_LABEL.match(value))


def _is_item_list(value: Any) -> bool:
    """매핑들의 배열인가 — `scenarios: [{...}, {...}]`."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        return False
    return bool(value) and all(isinstance(item, Mapping) for item in value)


def required_claims(payload: Mapping[str, Any]) -> set[str]:
    """근거가 필요한 값의 **경로 집합**.

    ★ v0.3 — 배열 payload 를 지원한다 (매입 파트 요청).
      매입은 `scenarios[]` 안에 같은 이름의 필드가 2~3벌 있어서 평면 1:1 이 성립하지 않는다.
      `claim` 에 **경로 표기**를 허용하고, 여기서 그 경로를 만든다.

    ★ 요구 강도가 층마다 다르다.

      | 위치 | 숫자 | 판정 라벨 |
      |---|---|---|
      | 최상위 | 필요 | **필요** — 홀로 서서 남의 행동을 바꾸는 판단이다 |
      | 배열 항목 안 | 필요 | **면제** — 구조 식별자이거나 그 에이전트 자신의 판정이다 |

      배열 항목의 라벨까지 요구하면 시나리오마다 `label` 근거를 만들어야 해서 과하다.
      숫자는 다르다 — **어디서 왔는지 없으면 LLM 이 만든 값과 구분되지 않는다.**

    ★ 배열은 **한 겹만** 파고든다. 더 깊은 중첩의 규칙은 도메인이 정한다.
    """
    out: set[str] = set()
    for key, value in payload.items():
        if _is_item_list(value):
            for index, item in enumerate(value):
                for sub, sub_value in item.items():
                    if _is_number(sub_value):
                        out.add(f"{key}[{index}].{sub}")
        elif _is_number(value) or _is_label(value):
            out.add(key)
        elif not isinstance(value, (str, bytes, Mapping)) and isinstance(value, Sequence) and value:
            out.add(key)  # 스칼라 배열 — 통째로 하나의 근거
    return out


def canonical_claim(payload: Mapping[str, Any], claim: str) -> str | None:
    """`scenarios[공격].total_amount_krw` → `scenarios[1].total_amount_krw`.

    배열 항목은 **번호로도 이름으로도** 가리킬 수 있다. 이름은 그 항목의 문자열 필드
    아무거나와 맞으면 된다(`label` · `scenario_id` 등) — 도메인마다 식별 필드가 달라서
    하나로 못 박지 않는다.

    가리키는 곳이 없으면 `None` — 고아 근거다.
    """
    match = _CLAIM_PATH.fullmatch(claim)
    if match is None:
        return claim if claim in payload else None

    key, selector, sub = match.group("key"), match.group("sel"), match.group("sub")
    items = payload.get(key)
    if not _is_item_list(items):
        return None

    index = _select_index(items, selector)
    if index is None or sub not in items[index]:
        return None
    return f"{key}[{index}].{sub}"


def _select_index(items: Sequence[Mapping[str, Any]], selector: str) -> int | None:
    if selector.isdigit():
        index = int(selector)
        return index if index < len(items) else None
    for index, item in enumerate(items):
        if any(value == selector for value in item.values() if isinstance(value, str)):
            return index
    return None


def check_binding(request: AgentRequest, reply: AgentReply) -> list[EnvelopeFinding]:
    """회신이 그 요청의 것인가 — 검증 L1 바인딩의 봉투 층 대응 (정의서 §3.7.5).

    스냅샷 폐지로 `snapshot_id` 대조가 사라진 자리를 `request_id`·`as_of` 가 메운다.
    """
    out: list[EnvelopeFinding] = []
    ctx = request.context
    if reply.request_id != ctx.request_id:
        out.append(
            EnvelopeFinding(
                "E-BIND-REQUEST",
                "reply.request_id",
                f"요청 {ctx.request_id} 에 회신 {reply.request_id} 가 왔다.",
            )
        )
    if reply.as_of != ctx.as_of:
        out.append(
            EnvelopeFinding(
                "E-BIND-AS-OF",
                "reply.as_of",
                f"요청 as_of={ctx.as_of} 인데 회신 as_of={reply.as_of} 다 — 시점 불일치.",
            )
        )
    if reply.agent != request.agent:
        out.append(
            EnvelopeFinding(
                "E-BIND-AGENT",
                "reply.agent",
                f"{request.agent} 를 불렀는데 {reply.agent} 가 답했다.",
            )
        )
    if reply.mode != request.mode:
        out.append(
            EnvelopeFinding(
                "E-BIND-MODE",
                "reply.mode",
                f"mode={request.mode} 로 불렀는데 {reply.mode} 로 답했다.",
            )
        )
    return out


def check_evidence_coverage(reply: AgentReply) -> list[EnvelopeFinding]:
    """payload 의 숫자·판정 라벨에 근거가 붙었는가 (정의서 §1.2-5).

    ★ 이것이 §1.2-3("LLM 은 숫자를 생성하지 않는다")의 집행 수단이다.
      `Evidence.source` 는 DB Fact·ML·Policy·`tool_calc` 뿐이라
      **LLM 이 만든 값은 어느 출처에도 해당하지 않는다.**

    ★ `claim` 은 경로 표기를 쓸 수 있다 — `scenarios[공격].total_amount_krw` (§required_claims).
    """
    if not reply.contributes_to_band:
        return []  # 못 돈 회신에 근거를 요구하지 않는다

    payload = reply.payload
    required = required_claims(payload)

    covered: set[str] = set()
    orphans: list[str] = []
    for evidence in reply.evidences:
        canonical = canonical_claim(payload, evidence.claim)
        if canonical is None:
            orphans.append(evidence.claim)
        else:
            covered.add(canonical)

    out = [
        EnvelopeFinding(
            "E-EVIDENCE-MISSING",
            f"payload.{path}",
            f"{path} 에 대응하는 Evidence 가 없다.",
        )
        for path in sorted(required - covered)
    ]
    out += [
        EnvelopeFinding(
            "E-EVIDENCE-ORPHAN",
            f"evidences[{claim}]",
            f"payload 에서 '{claim}' 을 찾을 수 없다 — 무엇을 뒷받침하는지 불명.",
        )
        for claim in sorted(orphans)
    ]
    return out


def check_reasoning(reply: AgentReply) -> list[EnvelopeFinding]:
    """`reasoning` 규칙 (M-1 v0.2 §5.4).

    LLM 이 쓰는 자리다. 자유 서술이 아니라 Evidence 기반 짧은 rationale 이어야 한다.
    """
    out: list[EnvelopeFinding] = []
    text = reply.reasoning.strip()
    if not text:
        return out
    if _BIG_NUMBER.search(text):
        out.append(
            EnvelopeFinding(
                "E-REASONING-NUMERIC",
                "reply.reasoning",
                "설명문에 수량·금액·날짜로 보이는 숫자가 있다. "
                "숫자가 필요하면 Evidence 를 추가한다 (§1.2-3).",
            )
        )
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) > _MAX_REASONING_SENTENCES:
        out.append(
            EnvelopeFinding(
                "E-REASONING-TOO-LONG",
                "reply.reasoning",
                f"{len(sentences)} 문장 — {_MAX_REASONING_SENTENCES} 문장 이내로 쓴다.",
            )
        )
    return out


def validate_reply(
    request: AgentRequest,
    reply: AgentReply,
    metadata: ExecutionMetadata | None = None,
) -> tuple[EnvelopeFinding, ...]:
    """마스터가 회신을 받고 돌리는 봉투 검증 전체.

    ★ 예외를 던지지 않고 findings 를 돌려준다.
      에이전트 하나의 실수로 사이클을 죽이지 않는다 — 무엇을 할지는 마스터가 정한다.
    """
    out: list[EnvelopeFinding] = []
    out += check_binding(request, reply)
    out += check_evidence_coverage(reply)
    out += check_reasoning(reply)
    if metadata is not None:
        if metadata.run_id != reply.run_id:
            out.append(
                EnvelopeFinding(
                    "E-BIND-RUN-ID",
                    "metadata.run_id",
                    f"회신 run_id={reply.run_id} 인데 메타데이터는 {metadata.run_id} 다.",
                )
            )
        if reply.contributes_to_band and not metadata.used_tools:
            out.append(
                EnvelopeFinding(
                    "E-PLAN-EMPTY",
                    "metadata.used_tools",
                    "정상 회신인데 사용한 Tool 이 없다 — 실행 계획을 재현할 수 없다 (§1.2-11).",
                )
            )
    return tuple(out)
