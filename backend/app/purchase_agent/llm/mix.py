"""⑤ 어댑터 — ``allocate_sourcing``에 주입하는 콜러블 (critic ``judge.py`` 대응).

노드가 아는 표면은 ``make_mix_selector()``가 돌려주는 **콜러블 하나**뿐이다.
``LLMSettings``도 ``Provider``도 노드에 노출하지 않는다 — 두 번째 소비자(⑦ 환각 대조,
E3-6 제안)가 생겨도 ``runtime.py``가 그대로 재사용된다.

★ 노드는 선택 **결과**만이 아니라 **상태**도 알아야 한다 (fallback이면 risks에 고지해야
  하므로). critic ``JudgeRunner``가 결과를 보관해 서비스가 나중에 읽는 것과 같은 이유로,
  여기서는 선택 결과와 상태를 한 객체로 함께 돌려준다.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

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
    #: 어느 프로바이더로 물었나. 🔴 **전에는 안 날랐다** — 그래서 ⑤ 호출 흔적만
    #: ``provider`` 가 비어 있었고, 역할 셋 중 하나만 추적이 끊겼다.
    #: 호출을 안 했으면 ``None`` 이다 (「안 불렀다」와 「빈 이름으로 불렀다」는 다르다).
    llm_provider: str | None = None
    #: 실제 시도 횟수 (재시도 포함). 🔴 **전에는 안 날랐다** — 그래서 봉투의
    #: ``llm_attempts`` 가 LLM 이 두 번 시도한 날에도 **0** 으로 나갔다. 실행 흔적이
    #: 「안 불렀다」로 보이는 자리라 채운다.
    llm_attempts: int = 0

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
        llm_attempts=result.llm_attempts,
        llm_provider=result.llm_provider,
    )


def make_mix_selector(service: MixSelectionService | None = None) -> MixSelector:
    """``(context, default_candidate_id) -> MixDecision`` 콜러블을 만든다.

    LLM이 꺼져 있거나 실패해도 **결정론 기본안을 돌려준다** — 그래프가 LLM 때문에
    멈추지 않는다.

    🔴 **베낀 자리가 없어졌다** (2026-09-09). 원래 *"orchestrator ``make_selector`` 와
    같은 계약"* 이라고 적었는데, ``#418``(2026-09-08)이 죽은 오케스트레이터 LLM selector
    를 걷어내면서 ``app/master/cycle_llm/{runtime,selector}.py`` 가 지워졌고 그 이름이
    저장소에 **0곳**이다. 마스터가 미리 알려 줬다 (쪽지 2026-09-08).

    ★ **계약 자체는 그대로다** — 베낀 대상이 없어졌을 뿐이고, 위 두 줄이 그 계약이다.
    """
    selection_service = service or get_mix_selection_service()

    def selector(context: SanitizedLLMContext, default_candidate_id: str) -> MixDecision:
        result = selection_service.select(
            context, default_candidate_id=default_candidate_id
        )
        return _decision(result)

    return selector


def spread_label(*, spread_widened: bool) -> str:
    """스프레드 라벨 하나. 🔴 **이 판정이 사는 자리는 여기 하나다.**

    ⑤ 는 이 라벨로 묻고 ⑧ 은 *"⑤ 사유가 그 라벨과 맞나"* 를 본다. 두 곳이 각자 만들면
    같은 날의 같은 사실이 서로 다른 라벨이 되고, ⑧ 의 대조가 **자기 계산끼리의 대조**가 된다.
    """
    return "SPREAD_WIDE" if spread_widened else "SPREAD_NORMAL"


def freshness_label(*, shelf_days: float | None, shelf_tight: bool) -> str:
    """신선도 라벨 하나. ``SHELF_UNKNOWN`` 을 「넉넉하다」로 안 접는다 (규칙 3)."""
    if shelf_days is None:
        return "SHELF_UNKNOWN"
    return "SHELF_TIGHT" if shelf_tight else "SHELF_AMPLE"


def shelf_is_tight(decision: Mapping[str, Any]) -> bool:
    """중품 상한이 걸렸나 — ⑤ 가 신선도 라벨을 만들 때 쓰는 **그 판정**이다."""
    return decision.get("cap_ratio", 1.0) < 1.0


def context_labels(decision: Mapping[str, Any]) -> tuple[str, ...]:
    """⑤ 판단 dict 에서 **그때 본 라벨과 고른 후보**를 되읽는다.

    🔴 **⑧ 이 이것을 받아야 ``MIX_REASON_LABEL_MISMATCH`` 가 성립한다.** 그 지적의 뜻이
    *"사유가 **입력 라벨·선택 후보**와 안 맞는다"* 라, 라벨 없이 사유만 주면 판단자는
    비교할 대상이 없다 — 고를 수는 있는데 무엇을 보고 고르는지가 없는 코드가 된다.

    ⚠️ ⑤ 가 **실제로 고른 날**만 뜻이 있다. 안 돌았으면 부르는 쪽이 빈 목록을 넘긴다.
    """
    mix = decision.get("mix")
    labels = [
        spread_label(spread_widened=bool(decision.get("widened"))),
        freshness_label(
            shelf_days=decision.get("shelf_days"), shelf_tight=shelf_is_tight(decision)
        ),
    ]
    if mix is not None and getattr(mix, "candidate_id", None):
        labels.append(mix.candidate_id)
    return tuple(labels)


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
    # 🔴 **라벨 판정을 여기 안 적는다** — 위 두 함수가 소유한다. ⑧ 이 같은 판정을 되읽어야
    #   *"사유가 라벨과 맞나"* 가 성립하는데, 두 곳에 적으면 한쪽만 바뀌는 날이 온다.
    return SanitizedLLMContext(
        item=item,
        spread=spread_label(spread_widened=spread_widened),
        freshness=freshness_label(shelf_days=shelf_days, shelf_tight=shelf_tight),
        signals=signals,
        facts=facts,
        candidates=candidates,
    )
