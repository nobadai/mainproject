"""T3-5 Selector — `graph.node_t3_select` 에 주입하는 어댑터.

원본 `selector_llm.py`(설계 산출물)의 `build_payload` · `make_selector` 를 옮겨온 것이다.
API 경로(`service.run_procurement`)는 응답 객체에서 Context 를 만들지만,
그래프 경로는 `PipelineState` 를 갖고 있으므로 여기서 같은 Context 로 변환한다.

★ 두 경로 모두 같은 `SelectionService` 를 통과한다 — 검증·재시도·상태 판정이 한 벌이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.orchestrator.llm.runtime import SelectionService, get_selection_service
from app.orchestrator.llm.schemas import CandidateContext, SanitizedLLMContext

if TYPE_CHECKING:  # 런타임 순환 임포트를 만들지 않는다.
    from app.orchestrator.contracts_core import PipelineState

_MINOR_CLIP_THRESHOLD = 0.95


def classify_clip(clip_ratio: float, *, clipped: bool) -> str:
    """클리핑 강도를 숫자 대신 3구간 라벨로 바꾼다 (Context 에서 숫자를 지우기 위한 것)."""
    if not clipped:
        return "FULL"
    return "MINOR_CLIP" if clip_ratio >= _MINOR_CLIP_THRESHOLD else "MAJOR_CLIP"


def build_state_context(state: PipelineState) -> SanitizedLLMContext:
    """PipelineState → SanitizedLLMContext. 숫자는 넘기지 않는다."""
    feasible = [r for r in state.clip_results if not r.infeasible]
    candidates = [
        CandidateContext(
            scenario_id=r.scenario_id,
            clip_magnitude=classify_clip(r.clip_ratio, clipped=r.clipped),
            binding_constraints=list(r.binding_constraints),
        )
        for r in feasible
    ]
    signals: list[str] = []
    facts: list[str] = []
    for dept, reply in state.replies.items():
        for check in reply.soft_warnings:
            signals.append(f"{dept}:{check.check_id}")
            if check.reason:
                facts.append(f"[{dept}] {check.reason}")
    return SanitizedLLMContext(
        cycle="PROCUREMENT",
        signals=list(dict.fromkeys(signals)),
        facts=facts,
        candidates=candidates,
    )


def make_selector(service: SelectionService | None = None):
    """`graph.Selector` 프로토콜을 만족하는 콜러블을 만든다.

    반환 콜러블은 `(state) -> list[str]` 이며, LLM 이 실패하거나 꺼져 있어도
    결정론 순서를 돌려준다 — 그래프가 LLM 때문에 멈추지 않는다.
    """
    selection_service = service or get_selection_service()

    def selector(state: PipelineState) -> list[str]:
        context = build_state_context(state)
        deterministic = [c.scenario_id for c in context.candidates]
        result = selection_service.select(
            context,
            runtime_ready=not state.band.not_ready,
            deterministic_ranking=deterministic,
        )
        ranked = result.interpretation.ranked_scenario_ids or deterministic
        note = result.interpretation.conflict_note
        state.log.note(
            f"T3-5 선정({result.llm_status}): "
            + " > ".join(ranked)
            + (f" / 충돌: {note}" if note else "")
        )
        return ranked

    return selector
