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
