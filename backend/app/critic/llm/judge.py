"""L5 Judge — `run_critic_v04(judge=...)` / `run_critic_b(...)` 에 주입하는 어댑터.

`RationaleJudge` 프로토콜은 `(payload: Mapping) -> tuple[bool, str]` 이다.
Critic 러너는 이 콜러블만 알면 되고, LLM 설정·검증·재시도는 여기서 끝난다.

★ 러너가 judge 를 호출한 뒤에야 LLM 상태를 알 수 있으므로, 어댑터가 결과를 담아 둔다.
  서비스는 러너 실행 후 `runner.result` 를 읽어 응답 필드를 채운다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.critic.llm.runtime import JudgeService, get_judge_service
from app.critic.llm.schemas import InterpretationResult, SanitizedLLMContext


def build_judge_context(payload: Mapping[str, Any], *, cycle: str) -> SanitizedLLMContext:
    """러너가 넘긴 payload → SanitizedLLMContext. 수량 dict 는 넘기지 않는다.

    payload 에는 `decision_kg` 같은 숫자가 들어 있지만 Context 에 싣지 않는다 —
    판정 대상은 "설명문이 제약과 모순되는가"이지 "수량이 맞는가"가 아니다.
    """
    binding = [str(code) for code in payload.get("binding_constraints", [])]
    dept_reasons = payload.get("dept_reasons") or {}
    facts = [f"[{dept}] {reason}" for dept, reason in dept_reasons.items() if reason]
    return SanitizedLLMContext(
        cycle="B" if cycle == "B" else "A",
        signals=binding,
        facts=facts,
        binding_constraints=binding,
        rationale=str(payload.get("rationale") or ""),
    )


class JudgeRunner:
    """호출 가능한 `RationaleJudge` + 마지막 LLM 결과 보관.

    러너가 L5 까지 도달하지 못하면 `result` 는 None 으로 남는다 —
    서비스는 그때 `SKIPPED_TEMPLATE` 기본값을 쓴다.
    """

    def __init__(self, service: JudgeService, *, cycle: str = "A", runtime_ready: bool = True):
        self.service = service
        self.cycle = cycle
        self.runtime_ready = runtime_ready
        self.result: InterpretationResult | None = None

    def __call__(self, payload: Mapping[str, Any]) -> tuple[bool, str]:
        context = build_judge_context(payload, cycle=self.cycle)
        result = self.service.judge(
            context,
            runtime_ready=self.runtime_ready,
            end_stage_reached=False,  # 러너가 L5 를 불렀다는 것은 앞 레이어를 통과했다는 뜻이다.
        )
        self.result = result
        interpretation = result.interpretation
        # ★ SUCCESS 가 아니면 판정하지 않은 것이다. FAIL 로 만들지 않고 PASS 로 통과시킨 뒤
        #   서비스가 `skipped` 에 올려 coverage 로 드러낸다 (설계서 §8).
        if result.llm_status != "SUCCESS":
            return True, interpretation.note
        return interpretation.verdict == "PASS", interpretation.note

    @property
    def ran(self) -> bool:
        """L5 가 실제 LLM 판정까지 갔는지."""
        return self.result is not None and self.result.llm_status == "SUCCESS"


def make_rationale_judge(
    service: JudgeService | None = None,
    *,
    cycle: str = "A",
    runtime_ready: bool = True,
) -> JudgeRunner:
    return JudgeRunner(service or get_judge_service(), cycle=cycle, runtime_ready=runtime_ready)
