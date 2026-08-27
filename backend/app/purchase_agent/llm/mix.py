"""⑤ 어댑터 — ``allocate_sourcing``에 주입하는 콜러블 (critic ``judge.py`` 대응).

노드가 아는 표면은 ``make_mix_selector()``가 돌려주는 **콜러블 하나**뿐이다.
``LLMSettings``도 ``Provider``도 노드에 노출하지 않는다 — 두 번째 소비자(⑦ 환각 대조,
E3-6 제안)가 생겨도 ``runtime.py``가 그대로 재사용된다.

★ 노드는 선택 **결과**만이 아니라 **상태**도 알아야 한다 (fallback이면 risks에 고지해야
  하므로). critic ``JudgeRunner``가 결과를 보관해 서비스가 나중에 읽는 것과 같은 이유로,
  여기서는 선택 결과와 상태를 한 객체로 함께 돌려준다.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.purchase_agent.llm.runtime import MixSelectionService, get_mix_selection_service
from app.purchase_agent.llm.schemas import (
    InterpretationResult,
    LLMStatus,
    MixCandidate,
    SanitizedLLMContext,
)

#: ⑤가 만족시켜야 하는 프로토콜. 테스트는 이 자리에 순수 함수를 꽂는다.
MixSelector = Callable[[SanitizedLLMContext, str], "MixDecision"]


@dataclass(frozen=True)
class MixDecision:
    """고른 후보 id + 왜 그렇게 됐는지.

    ``llm_status``가 ``SUCCESS``가 아니면 **LLM이 고른 게 아니다** — 규칙 기본안이다.
    ⑥이 그 사실을 risks에 적어야 하므로 상태를 결과와 함께 들고 다닌다.
    """

    candidate_id: str
    reason: str
    llm_status: LLMStatus
    llm_model: str | None
    llm_fallback_used: bool

    @property
    def applied(self) -> bool:
        """LLM 판단이 실제로 적용됐는가."""
        return self.llm_status == "SUCCESS"


def _decision(result: InterpretationResult) -> MixDecision:
    return MixDecision(
        candidate_id=result.interpretation.chosen_candidate_id,
        reason=result.interpretation.reason,
        llm_status=result.llm_status,
        llm_model=result.llm_model,
        llm_fallback_used=result.llm_fallback_used,
    )


def make_mix_selector(service: MixSelectionService | None = None) -> MixSelector:
    """``(context, default_candidate_id) -> MixDecision`` 콜러블을 만든다.

    LLM이 꺼져 있거나 실패해도 **결정론 기본안을 돌려준다** — 그래프가 LLM 때문에
    멈추지 않는다 (orchestrator ``make_selector``와 같은 계약).
    """
    selection_service = service or get_mix_selection_service()

    def selector(context: SanitizedLLMContext, default_candidate_id: str) -> MixDecision:
        result = selection_service.select(
            context, default_candidate_id=default_candidate_id
        )
        return _decision(result)

    return selector


def build_mix_context(
    item: str,
    *,
    spread_widened: bool,
    shelf_days: int | None,
    shelf_tight: bool,
    signals: list[str],
    facts: list[str],
    candidates: list[MixCandidate],
) -> SanitizedLLMContext:
    """판단 재료를 **라벨로 바꿔** Context를 만든다 — 숫자를 넘기지 않는다.

    orchestrator ``classify_clip``이 클리핑 비율을 3구간 라벨로 바꾼 것과 같은 처리다.
    스프레드 21.2%를 그대로 주면 LLM이 그 숫자를 사유에 베껴 쓰고, 그 순간 "LLM이 만든
    숫자"가 출력에 실린다. 판정은 규칙이 이미 끝냈으니 **결론만** 준다.
    """
    if shelf_days is None:
        freshness = "SHELF_UNKNOWN"
    else:
        freshness = "SHELF_TIGHT" if shelf_tight else "SHELF_AMPLE"
    return SanitizedLLMContext(
        item=item,
        spread="SPREAD_WIDE" if spread_widened else "SPREAD_NORMAL",
        freshness=freshness,
        signals=signals,
        facts=facts,
        candidates=candidates,
    )
