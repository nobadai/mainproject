"""
runner.py — 마스터의 호출 계층

에이전트 호출 한 번에 붙는 것을 한자리에 모은다.

    예산 확인 → 봉투 조립 → 호출 → 실패를 값으로 → 봉투 검증 → 계획 기록

★ 여기까지가 **결정론**이다 (이슈 설계 원칙 ③).
  의도 분류에는 LLM 을 쓰지만 **호출·취합·재호출 판단은 규칙**이다. 그래야
  같은 입력에 같은 실행 계획이 나오고 백테스트가 성립한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.master.budget import CallBudget
from app.master.envelope import (
    AgentName,
    AgentReply,
    AgentRequest,
    ExecutionContext,
    Mode,
    validate_reply,
)
from app.master.plan import ExecutionPlan, ExecutionStep
from app.master.ports import AgentRegistry, empty_metadata, error_reply


class MasterRunner:
    """한 요청 동안 살아 있는 호출 계층.

    ★ 요청마다 새로 만든다. 예산과 실행 계획이 요청 단위이기 때문이다.
    """

    def __init__(
        self,
        context: ExecutionContext,
        registry: AgentRegistry,
        budget: CallBudget | None = None,
    ) -> None:
        self.context = context
        self.registry = registry
        self.budget = budget or CallBudget()
        self.plan = ExecutionPlan(request_id=context.request_id, as_of=context.as_of)

    # ── 핵심 ────────────────────────────────────────────────────

    def call(
        self,
        agent: AgentName,
        mode: Mode,
        payload: Mapping[str, Any] | None = None,
        call_seq: int | None = None,
    ) -> AgentReply:
        """에이전트를 한 번 부른다.

        `call_seq` 를 주지 않으면 **이 요청에서 그 에이전트를 몇 번째로 부르는지**가
        자동으로 들어간다.

        예산이 소진되면 `BudgetExhausted` 가 올라간다 — **마스터 본체가 잡아 종료
        코드로 바꾼다.** 여기서 삼키면 상한이 있으나 마나가 된다.
        """
        seq = call_seq if call_seq is not None else self.plan.call_count(agent, mode) + 1
        self.budget.consume(agent, mode)

        request = AgentRequest(
            context=self.context,
            agent=agent,
            mode=mode,
            call_seq=seq,
            payload=dict(payload or {}),
        )
        reply, metadata = self._invoke(request)
        findings = validate_reply(request, reply, metadata)
        self.plan.record(request, reply, metadata, findings)
        return reply

    def _invoke(self, request: AgentRequest):
        """호출을 감싸 **실패를 값으로 바꾼다.**

        ★ `BaseException` 이 아니라 `Exception` 만 잡는다.
          `KeyboardInterrupt` · `SystemExit` 까지 삼키면 프로세스를 멈출 수 없다.
        """
        port = self.registry.get(request.agent)  # 미등록은 마스터 설정 오류 — 올린다
        try:
            return port(request)
        except Exception as exc:  # noqa: BLE001 — 도메인 실패를 값으로 내리는 것이 목적
            reason = f"{type(exc).__name__}: {exc}"
            reply = error_reply(request, reason)
            return reply, empty_metadata(request, reply.run_id)

    # ── 마스터가 판단할 때 보는 것 ──────────────────────────────

    def band_is_formed(self, required: tuple[AgentName, ...]) -> bool:
        """필요한 조언자가 **전부** 경계를 냈는가.

        하나라도 빠지면 밴드가 반쪽이다. 그 상태로 매입을 부르면 **제약 하나가 빠진
        시나리오**가 나오고, 그건 나중에 반드시 잘린다 — 그때는 이미 매입 LLM 호출
        비용을 쓴 뒤다. (M-1 §11-6 부분 실패 정책)
        """
        for agent in required:
            step = self.plan.last(agent, "PRE_PURCHASE")
            if step is None or not step.contributed:
                return False
        return True

    def blocking_agents(self, required: tuple[AgentName, ...]) -> tuple[AgentName, ...]:
        """밴드를 못 만들게 막고 있는 에이전트."""
        out: list[AgentName] = []
        for agent in required:
            step = self.plan.last(agent, "PRE_PURCHASE")
            if step is None or not step.contributed:
                out.append(agent)
        return tuple(out)

    def retryable(self, agent: AgentName, mode: Mode) -> bool:
        """다시 부를 가치가 있는가.

        `ERROR` 만 참이다. `RUNTIME_NOT_READY` 는 입력이 없어서 못 낸 답이므로
        다시 불러도 같고, 재시도하면 **호출 예산만 태운다.**

        ★ **정하는 것은 여기가 아니다.** 이 함수는 알려만 주고, 실제로 다시 부를지는
          `flow._collect_constraints` 가 정한다 — 경계 수집에서 **한 번만** 쓴다.

        🔴 **오랫동안 아무도 안 불렀다 (2026-08-31 실측).** 이 함수도
          `envelope.worth_retry` 도 정의와 테스트만 있고 호출자가 0이었다. 판단을
          담아 둔 자리가 배선되지 않으면, 읽는 사람이 *"재시도가 되는 줄"* 안다.

        ★ `envelope.worth_retry` 는 **회신 하나**를 보고 같은 판정을 낸다. 여기는
          **실행 계획에 실제로 기록된 것**을 본다 — 재시도는 "무슨 일이 일어났나" 를
          근거로 정해야 하므로 이쪽을 쓴다.
        """
        step = self.plan.last(agent, mode)
        return step is not None and step.runtime_status == "ERROR"

    def steps(self) -> tuple[ExecutionStep, ...]:
        return tuple(self.plan.steps)
