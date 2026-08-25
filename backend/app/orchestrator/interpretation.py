"""Orchestrator deterministic result projection into the optional LLM layer.

★ LLM 이 어떤 상태로 끝나든 `ranked_ids` 는 항상 채워진다 —
  SUCCESS 면 LLM 순위, 그 외에는 결정론 순위다. Core 숫자는 건드리지 않는다.
"""

from app.orchestrator.llm.runtime import SelectionService, get_selection_service
from app.orchestrator.llm.schemas import CandidateContext, SanitizedLLMContext
from app.orchestrator.llm.selector import classify_clip
from app.orchestrator.schemas import ProcurementResponse, SalesResponse

_DEADLOCK_FACT = "부서 제약이 서로 맞물려 실행 가능한 구간이 없습니다."
_COLLAPSE_FACT = "후보안들이 같은 결과로 수렴해 실질적인 선택지가 줄었습니다."


def build_orchestrator_context(
    response: ProcurementResponse | SalesResponse,
) -> SanitizedLLMContext:
    """응답 → Context. 수량·금액은 넘기지 않고 라벨·코드만 넘긴다."""
    candidates = [
        CandidateContext(
            scenario_id=clip.scenario_id,
            clip_magnitude=classify_clip(clip.clip_ratio, clipped=clip.clipped),
            binding_constraints=list(clip.binding_constraints),
        )
        for clip in response.clip_results
        if not clip.infeasible
    ]

    signals = _unique(response.soft_warnings)
    facts = list(signals)
    if isinstance(response, ProcurementResponse):
        cycle = "PROCUREMENT"
        if response.deadlock is not None:
            signals.append(response.deadlock.code)
            facts.append(_DEADLOCK_FACT)
    else:
        cycle = "SALES"
        if response.variant_collapsed:
            signals.append("VARIANT_COLLAPSED")
            facts.append(_COLLAPSE_FACT)

    return SanitizedLLMContext(
        cycle=cycle,
        signals=_unique(signals),
        facts=facts,
        candidates=candidates,
    )


def enrich_orchestrator_response[OrchestratorResponse: (ProcurementResponse, SalesResponse)](
    response: OrchestratorResponse,
    selection_service: SelectionService | None = None,
) -> OrchestratorResponse:
    service = selection_service or get_selection_service()
    context = build_orchestrator_context(response)
    result = service.select(
        context,
        runtime_ready=response.runtime_status == "READY",
        deterministic_ranking=list(response.ranked_ids),
    )

    ranked = result.interpretation.ranked_scenario_ids or list(response.ranked_ids)
    update = result.model_dump(exclude={"interpretation"})
    update["interpretation"] = result.interpretation
    update["ranked_ids"] = ranked
    update["recommended_id"] = ranked[0] if ranked else None
    return response.model_copy(update=update)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
