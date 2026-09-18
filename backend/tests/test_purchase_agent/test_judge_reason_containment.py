"""G10 — **판단자가 쓴 문장은 지정된 한 칸에만 산다.**

🔴 **왜 전수인가.** 지금까지 이 규율을 재던 것은 *"특정 문자열이 ``risks`` 에 없다"* 두
줄뿐이었다. 그 모양은 **칸이 늘 때 같이 늘지 않는다** — 새 칸이 생겨 사유가 그리로 새도
검사는 옛 칸만 보고 초록이다. 그래서 여기서는 칸 이름을 손으로 적지 않고 **산출물 전체를
훑어 사유가 나타난 자리의 경로를 모은다.** 지정 칸 밖에 하나라도 있으면 빨개진다.

★ **재는 것은 ④ 와 ⑤ 둘이다.** ⑧ 은 자유 문장을 안 돌려준다 — 모델이 고르는 것은 지적
  코드와 대상 근거 ID 이고 사람이 읽는 문장은 ``review_templates`` 가 만든다. 그래서 ⑧ 에는
  「샐 사유」가 없고, 대신 ⑧ 이 **⑤ 의 사유를 재료로 받는다**는 되먹임 통로가 있다
  (``review_rationale`` → ``claims[].claim_text`` · ``mix_reason``). 그 통로로 들어간
  문장이 산출물로 되나오지 않는지도 같이 본다.

🟢 **기준선이 있다.** 저장된 응답 11,954 안 · 근거 71,140건을 읽기 전용으로 전수해
  ``risks`` 유출이 **0** 이었다 (2026-09-18 실측). ``risks`` 에 걸린 35건은 규칙이 쓴 폴백
  고지(「판단 미적용(판단자 미사용) — 규칙 기본안으로 배분했다」)이지 판단자 문장이 아니다.
  이 파일은 **그 0 을 잠근다** — 기록을 다시 세는 것이 아니라, 0 이 나오게 한 구조를 잰다.

⚠️ 실 API 를 안 탄다. 판단자 자리에 가짜를 꽂아 결정적으로 돌린다.
"""

import json
from datetime import date, timedelta
from typing import Any

import pytest

from app.purchase_agent.adapter import _llm_calls
from app.purchase_agent.graph import build_graph
from app.purchase_agent.llm.mix import MixDecision
from app.purchase_agent.llm.review_schemas import FindingOut, ReviewOutput
from app.purchase_agent.llm.self_review import ReviewContext, ReviewResult
from app.purchase_agent.llm.split_schemas import (
    SplitAllocationChoice,
    SplitAllocationResult,
)
from app.purchase_agent.nodes import review_rationale as rr
from app.purchase_agent.state import build_initial_state

ITEMS = ("배추", "무", "양파")
ANCHORS = (
    date(2026, 8, 21),  # 지속 상승 — ④ 가 궤적으로 진입한다
    date(2026, 8, 28),  # 하락
    date(2026, 9, 4),  # 불확실
    date(2026, 9, 11),  # 등급 스프레드 확대 — ⑤ 가 불린다
)

#: 🔴 **판단자만 쓸 수 있는 문장이어야 한다.** 규칙이 만드는 어떤 문장과도 안 겹쳐야
#:   「이 자리에 있다」가 곧 「판단자 사유가 샜다」가 된다. 숫자를 안 쓴다 — 쓰면
#:   ⑤ 의 검증기가 아니라 ⑧ 의 ``sanitize_numerals`` 가 문장을 바꿔 놓아, 안 샌 것인지
#:   가려진 것인지 구분이 안 된다.
네번_사유 = "회차마커가나다 앞뒤 무게를 이렇게 본다"
다섯번_사유 = "조합마커라마바 등급을 이렇게 섞는다"

#: 판단자 사유가 **살아도 되는 유일한 칸**. ⑤ 는 ``_mix_choice_rationale`` ·
#: ④ 는 ``_allocation_choice_rationale`` 이 여기에만 싣는다.
지정_칸 = "claim"


def _경로들(값: Any, 마커: str, 경로: str = "") -> list[str]:
    """``마커`` 가 나타난 **모든 자리의 경로**를 모은다.

    🔴 칸 이름을 인자로 받지 않는다 — 받으면 목록을 유지해야 하고, 유지 안 한 날
      새 칸이 조용히 검사 밖에 남는다 (⑤ ``_pin_to_code_defaults`` 와 같은 결).
    """
    if isinstance(값, str):
        return [경로] if 마커 in 값 else []
    if isinstance(값, dict):
        return [
            자리
            for 키, 하위 in 값.items()
            for 자리 in _경로들(하위, 마커, f"{경로}.{키}" if 경로 else str(키))
        ]
    if isinstance(값, (list, tuple)):
        return [
            자리
            for i, 하위 in enumerate(값)
            for 자리 in _경로들(하위, 마커, f"{경로}[{i}]")
        ]
    return []


def _끝칸(경로: str) -> str:
    return 경로.rsplit(".", 1)[-1]


def _고정_mix(reason: str):
    def selector(context, default_candidate_id: str) -> MixDecision:
        return MixDecision(
            candidate_id=default_candidate_id,
            reason=reason,
            llm_status="SUCCESS",
            llm_model="haiku",
            llm_provider="anthropic",
            llm_fallback_used=False,
            llm_attempts=1,
        )

    return selector


def _고정_split(reason: str):
    def selector(context, default_candidate_id: str) -> SplitAllocationResult:
        return SplitAllocationResult(
            interpretation=SplitAllocationChoice(
                chosen_candidate_id=default_candidate_id, reason=reason
            ),
            llm_status="SUCCESS",
            llm_provider="anthropic",
            llm_model="haiku",
            llm_attempts=1,
            llm_fallback_used=False,
        )

    return selector


def _본다(본_것: list[ReviewContext]):
    """⑧ 을 실제로 태운다 — ``llm_calls`` 에 역할 셋이 다 서게 한다."""

    def reviewer(context: ReviewContext) -> ReviewResult:
        본_것.append(context)
        return ReviewResult(
            output=ReviewOutput(findings=[FindingOut(code="LABEL_BODY_MISMATCH")]),
            llm_status="SUCCESS",
            llm_provider="anthropic",
            llm_model="haiku",
            llm_attempts=1,
            llm_fallback_used=False,
        )

    return reviewer


def _돌린다(
    item: str,
    as_of: date,
    monkeypatch: pytest.MonkeyPatch,
    본_것: list | None = None,
    초기_고침=None,
) -> dict:
    """④⑤⑧ 셋을 다 꽂고 한 번 돌린다. 돌아오는 것은 **최종 State** 다.

    ``초기_고침`` 은 ④ 가 실제로 고를 것이 있는 판을 세울 때만 쓴다 — 어댑터 경로에서만
    오는 칸(``inbound_lead_days``·``cap_by_date``)이 mock 에는 없다.
    """
    monkeypatch.setenv("PURCHASE_LLM_SPLIT_ALLOCATION_ENABLED", "true")
    monkeypatch.setattr(rr, "enabled", lambda key, default=False: True)
    state = build_initial_state(item, as_of)
    if 초기_고침 is not None:
        초기_고침(state)
    return build_graph(
        selector=_고정_mix(다섯번_사유),
        split_allocation_selector=_고정_split(네번_사유),
        reviewer=_본다(본_것 if 본_것 is not None else []),
    ).invoke(state)


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", ITEMS)
def test_판단자_사유는_지정_칸_밖에_한_군데도_없다(
    item: str, as_of: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 **이 파일의 본체.** 산출물 전체를 훑어 사유가 나타난 자리를 모은다.

    ⚠️ 「안 나타났다」로 통과하는 것을 허용한다 — 그날 그 판단자가 안 불릴 수 있다
      (④ 는 선언이 ``PROVISIONAL`` 이라 후보가 하나인 날이 대부분이다). 재는 것은
      **「났다면 지정 칸인가」**이고, 실제로 실리는지는 아래 별도 검사가 본다.
    """
    최종 = _돌린다(item, as_of, monkeypatch)
    for 마커 in (네번_사유, 다섯번_사유):
        샌_자리 = [
            경로
            for 경로 in _경로들(최종["proposal"], 마커)
            if _끝칸(경로) != 지정_칸
        ]
        assert 샌_자리 == [], f"{item} {as_of} — 사유가 지정 칸 밖에 있다: {샌_자리}"


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", ITEMS)
def test_사유는_risks_에도_evidence_detail_에도_안_실린다(
    item: str, as_of: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 **셋을 이름으로도 한 번 더 못 박는다.**

    위 전수 검사가 이미 덮지만, 이 셋은 **틀렸을 때 나가는 자리**라 이름으로 남겨 둔다 —
    빨개졌을 때 어느 칸인지가 바로 읽힌다.

    ``risks`` 는 순수 함수가 만든 고정 문장이고(``_allocation_risks`` 머리말),
    ``evidence_detail`` 은 「규칙이 만든 후보 중 판단자가 골랐다 · 적용 여부」 를 말하는
    자리다. 판단자 문장이 거기 실리면 **우리 말과 남의 말이 섞인다.**
    """
    최종 = _돌린다(item, as_of, monkeypatch)
    for 안 in 최종["proposal"]["scenarios"]:
        for 마커 in (네번_사유, 다섯번_사유):
            assert not [줄 for 줄 in 안["risks"] if 마커 in 줄]
            assert not [
                근거
                for 근거 in 안["rationale"]
                if 마커 in (근거.get("evidence_detail") or "")
            ]


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", ITEMS)
def test_사유는_실행_흔적에도_안_실린다(
    item: str, as_of: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 ``llm_calls`` 는 **실행 흔적**이지 업무 결과가 아니다 (M-1 §6).

    역할·상태·시도·판·모델이 실리는 자리고, 판단 내용은 안 싣는다. 여기 사유가 실리면
    업무 결과와 실행 흔적이 섞여, 마스터가 흔적만 보고 판단을 읽게 된다.
    """
    최종 = _돌린다(item, as_of, monkeypatch)
    흔적 = [call.__dict__ for call in _llm_calls(최종)]
    assert 흔적, "역할 흔적이 하나도 안 남았다 — 이 검사가 볼 대상이 없다"
    for 마커 in (네번_사유, 다섯번_사유):
        assert _경로들(흔적, 마커) == []


def test_다섯번_사유는_실제로_지정_칸에_실린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **「어디에도 없다」로는 통과하면 안 된다.**

    위 전수 검사는 사유가 아예 안 나가도 초록이다 — 그 상태로는 *"샜나"* 가 아니라
    *"아무 일도 안 일어났나"* 를 재게 된다. 그러니 **실려야 할 자리에 실렸는지**를 같이
    잠근다. 이 앵커는 ⑤ 가 불리는 날이다.
    """
    최종 = _돌린다("배추", date(2026, 9, 11), monkeypatch)
    경로 = _경로들(최종["proposal"], 다섯번_사유)
    assert 경로, "⑤ 가 안 불렸다 — 이 앵커가 더 이상 그 자리가 아니다"
    assert {_끝칸(자리) for 자리 in 경로} == {지정_칸}


def test_여덟번에_들어간_사유가_산출물로_되나오지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 **되먹임 통로를 같이 본다.** ⑤ 사유는 ⑧ 의 **입력**으로 두 번 들어간다.

    ``rationale[].claim`` → ``sanitize_numerals`` → ``claims[].claim_text`` 하나,
    ``mix_reason`` 하나다. 들어가는 것은 설계이고, **나오는 것은 아니다** — ⑧ 이
    돌려주는 것은 지적 코드와 대상 근거 ID 뿐이고 문장은 ``review_templates`` 가 만든다.

    ⚠️ 이 검사가 빨개지는 모양은 「⑧ 이 자유 문장을 돌려주게 됐다」다. 그날은 이 규율을
      ⑧ 에도 다시 세워야 한다 — 통로가 열린 채로 지나가지 않게 한다.
    """
    본_것: list[ReviewContext] = []
    최종 = _돌린다("배추", date(2026, 9, 11), monkeypatch, 본_것)
    assert 본_것, "⑧ 이 안 불렸다 — 이 검사가 볼 대상이 없다"
    들어간_것 = json.dumps(
        [c.__dict__ for c in 본_것], ensure_ascii=False, default=str
    )
    assert 다섯번_사유 in 들어간_것, "⑤ 사유가 ⑧ 재료에 안 들어갔다 — 통로가 끊겼다"
    # 나온 쪽 — 지적 문장에는 그 사유가 없다
    for 안 in 최종["proposal"]["scenarios"]:
        assert not [줄 for 줄 in 안["risks"] if 다섯번_사유 in 줄]


#: 🔴 **승인된 값이 아니다.** ④ 판단자가 실제로 불리는 판을 세우려고 흉내 낸 것이다 —
#:   선언 파일은 ``PROVISIONAL`` 그대로다 (``test_split_allocation_node`` 와 같은 픽스처).
_승인된_선언 = {
    "status": "APPROVED",
    "two_rounds": {"FRONT_LOADED": [0.60], "BACK_LOADED": [0.40]},
    "three_rounds": {"FRONT_LOADED": [0.50, 0.30], "BACK_LOADED": [0.20, 0.30]},
}


def test_네번_사유도_실제로_지정_칸에만_실린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **④ 를 헛돌게 두지 않는다.**

    위 전수 검사에서 ④ 는 선언이 ``PROVISIONAL`` 이라 후보가 균등 하나뿐이고, 그러면
    판단자를 아예 안 부른다 — 사유가 안 나가니 **아무 일도 안 일어난 채로 초록**이다.
    여기서는 승인 상태를 흉내 내 ④ 를 실제로 태우고, 그 사유가 지정 칸에만 사는지 본다.
    """
    import copy

    from app.purchase_agent.config import load_constraints

    선언 = copy.deepcopy(load_constraints())
    선언["split"]["allocation_weights"] = _승인된_선언
    monkeypatch.setattr(
        "app.purchase_agent.nodes.split_plan.load_constraints", lambda: 선언
    )

    def 여유를_깐다(state: dict) -> None:
        # ⚠️ N4 는 **최상위**다 — ``pending_value`` 가 보는 자리가 거기다.
        state["inbound_lead_days"] = 2
        state["inventory"] = {
            **state["inventory"],
            "cap_by_date": {
                (date(2026, 8, 21) + timedelta(days=d)).isoformat(): 1_000_000.0
                for d in range(40)
            },
        }

    최종 = _돌린다("배추", date(2026, 8, 21), monkeypatch, 초기_고침=여유를_깐다)

    결정 = 최종["split_plan"][0]["decision"]
    assert len(결정["allocation_candidates"]) >= 2, "④ 가 고를 것이 없었다"
    판단 = 결정["allocation_judgment"]
    assert 판단 is not None and 판단.llm_status == "SUCCESS", "④ 가 안 불렸다"

    경로 = _경로들(최종["proposal"], 네번_사유)
    assert 경로, "④ 사유가 산출물에 아예 안 실렸다 — 이 검사가 볼 대상이 없다"
    assert {_끝칸(자리) for 자리 in 경로} == {지정_칸}
    # 실행 흔적에도 안 실린다 — 흔적은 상태·판·모델만 나른다
    assert _경로들([call.__dict__ for call in _llm_calls(최종)], 네번_사유) == []
