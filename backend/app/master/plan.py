"""
plan.py — 실행 계획 기록 (마스터 본체 3/3)

정의서 §1.2-11 — **마스터가 어떤 에이전트를 어떤 순서로 호출했고 각 에이전트가 어떤 Tool 을
썼는지 남긴다.** 순서가 고정이 아니게 되었으므로 재현·감사·백테스트가 이 기록에 의존한다.

★ 시각을 담지 않는다.
  `ExecutionPlan` 은 **같은 입력에 같은 값**이어야 한다. 실행 시각을 넣으면 매번 달라져
  재현성 비교가 불가능해진다. 소요 시간은 에이전트가 준 `ExecutionMetadata.elapsed_ms`
  에만 있고, 계획 자체는 결정론이다.

★ 검증 Tool 의 ④ 실행 계획 온전성 검사(M-16)가 이것을 읽는다 (정의서 §3.7.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.master.envelope import (
    AgentName,
    AgentReply,
    AgentRequest,
    EnvelopeFinding,
    ExecutionMetadata,
    Mode,
    RuntimeStatus,
    Verdict,
)


@dataclass(frozen=True)
class ExecutionStep:
    """호출 한 번의 기록."""

    seq: int
    agent: AgentName
    mode: Mode
    call_seq: int
    run_id: str
    runtime_status: RuntimeStatus
    business_status: Verdict
    used_tools: tuple[str, ...] = ()
    finding_codes: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()

    #: 🔴 **부서가 스스로 밝힌 사유.** `missing_data` 가 *"무엇이 없어서"* 라면
    #: 이건 *"왜 터졌는지"* 다 — 어댑터 예외 메시지가 `error_reply` 를 거쳐 여기 온다.
    #:
    #: 값은 처음부터 `AgentReply.reasoning` 에 있었는데 여기로 옮기지 않아
    #: **실행 계획에 안 남았다.** 그래서 경계에서 부서가 죽으면 이력을 파도
    #: `runtime_status=ERROR` 까지만 알고 그 ERROR 의 사유는 어디에도 없었다
    #: (재현성 측정 2026-09-02 · 6회 중 2회가 왜 실패했는지 모름).
    #:
    #: ★ **봉투 검증을 우회하지 않는다.** `check_reasoning` 은 `runner.call` 이
    #:   `plan.record` **전에** 돌리므로, 여기 실린다고 규칙을 피해 가지 않는다.
    #:   나르기만 하고 판정에는 쓰지 않는다.
    #:
    #: ★ **재현성 비교에는 안 쓴다** — `plan_signature` 는 (agent, mode, call_seq) 뿐이다.
    reasoning: str = ""

    #: 🔴 **그 부서 안에서 LLM 이 실제로 돌았나.** `ExecutionMetadata` 는 처음부터
    #: 이 넷을 담고 있었는데 여기로 옮기지 않아, 마스터는 **부서가 규칙으로 답했는지
    #: 모델로 답했는지 구분하지 못했다** (실측 2026-08-31).
    #:
    #: 재무가 Tool 선택을 Planner 에게 맡기는 구조로 가면(재무 2026-08-31 질의)
    #: 이 값이 **없으면 안 된다** — Planner 가 죽어 규칙 경로로 떨어져도 산출물은
    #: 멀쩡해 보이고, 그게 오늘 하루 종일 고친 실패 방식이다.
    #:
    #: ★ **재현성 비교에는 안 쓴다.** 같은 계획이 한 번은 SUCCESS 한 번은
    #:   FALLBACK 일 수 있다 — `plan_signature` 는 (agent, mode, call_seq) 셋만 본다.
    llm_status: str = "DISABLED"
    llm_model: str = ""
    llm_attempts: int = 0
    llm_fallback_used: bool = False

    #: 🔴 **부서가 계획을 다시 세운 횟수.** `llm_attempts` 와 다르다 (재무 정정 2026-09-02).
    #:
    #: `llm_attempts` 는 **Planner + Finalizer 호출 횟수**다. capability 별로 Tool 을
    #: 하나씩 고르는 구조라 정상 실행에서도 여러 번 불린다 — 툴이 4개면 6, 3개면 8 처럼
    #: 툴 개수를 따라 움직인다. **재시도 횟수가 아니다.**
    #:
    #: 실제 재계획은 이 값이다. `ExecutionMetadata` 는 처음부터 담고 있었는데 여기로
    #: 옮기지 않아 **실행 계획에 안 남았다** — 부서가 보내 준 것을 마스터가 버리고
    #: 있었다. "그날 무엇이 오래 걸렸나" 를 이력으로 볼 수 없던 이유 하나가 이것이다.
    replans: int = 0

    #: 부서가 스스로 남긴 관측. **마스터는 읽지 않고 나른다.**
    #:
    #: 부서만 아는 사실 중에는 봉투에 자리가 없는 것이 있다 — 재무가 cap 을 낼 때
    #: 무엇을 읽었는지가 그것이다. 마스터가 Tool 이름이나 payload 키를 보고 추측하면
    #: **모르는 것이 근거가 된다.** 그래서 부서가 기계가 읽을 형태로 적어 보내고,
    #: 마스터는 그것을 그대로 검증 Tool 까지 옮긴다 (`critic_bridge`).
    observations: tuple[str, ...] = ()

    @property
    def contributed(self) -> bool:
        return self.runtime_status == "READY"


@dataclass
class ExecutionPlan:
    """한 요청의 호출 이력 전체."""

    request_id: str
    as_of: date
    steps: list[ExecutionStep] = field(default_factory=list)

    def record(
        self,
        request: AgentRequest,
        reply: AgentReply,
        metadata: ExecutionMetadata,
        findings: tuple[EnvelopeFinding, ...] = (),
    ) -> ExecutionStep:
        step = ExecutionStep(
            seq=len(self.steps) + 1,
            agent=request.agent,
            mode=request.mode,
            call_seq=request.call_seq,
            run_id=reply.run_id,
            runtime_status=reply.runtime_status,
            business_status=reply.business_status,
            used_tools=tuple(metadata.used_tools),
            finding_codes=tuple(f.code for f in findings),
            missing_data=tuple(reply.missing_data),
            reasoning=reply.reasoning,
            llm_status=metadata.llm_status,
            llm_model=metadata.llm_model,
            llm_attempts=metadata.llm_attempts,
            llm_fallback_used=metadata.llm_fallback_used,
            replans=metadata.replans,
            observations=tuple(metadata.observations),
        )
        self.steps.append(step)
        return step

    # ── 마스터가 다음 행동을 정할 때 보는 것 ──────────────────────

    def called(self, agent: AgentName, mode: Mode | None = None) -> bool:
        return any(s.agent == agent and (mode is None or s.mode == mode) for s in self.steps)

    def call_count(self, agent: AgentName, mode: Mode | None = None) -> int:
        return sum(1 for s in self.steps if s.agent == agent and (mode is None or s.mode == mode))

    def last(self, agent: AgentName, mode: Mode | None = None) -> ExecutionStep | None:
        for step in reversed(self.steps):
            if step.agent == agent and (mode is None or step.mode == mode):
                return step
        return None

    @property
    def not_ready(self) -> tuple[AgentName, ...]:
        """밴드에 기여하지 못한 에이전트.

        **조용히 건너뛰면 그 부서의 상한이 무한대로 남아 무제한 매입이 통과한다.**
        마스터는 이 목록을 명시적으로 들고 판단한다.
        """
        seen: dict[AgentName, bool] = {}
        for step in self.steps:
            seen[step.agent] = seen.get(step.agent, False) or step.contributed
        return tuple(a for a, ok in seen.items() if not ok)

    @property
    def all_findings(self) -> tuple[str, ...]:
        return tuple(code for step in self.steps for code in step.finding_codes)

    @property
    def signature(self) -> tuple[tuple[str, str, int], ...]:
        """재현성 비교용 지문 — **누구를 어떤 목적으로 몇 번째로 불렀는가.**

        같은 입력에 같은 지문이 나와야 한다. 결과값(`runtime_status` 등)은 빼는데,
        그건 에이전트 쪽 사정이지 **마스터의 계획**이 아니기 때문이다.
        """
        return tuple((s.agent, s.mode, s.call_seq) for s in self.steps)
