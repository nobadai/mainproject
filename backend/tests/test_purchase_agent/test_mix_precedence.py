"""⑤ 등급 조합 우열표 — **규칙이 정하고 판단자가 설명한다** (E3-12).

🔴 **왜 생겼나.** ⑤ 의 지시문이 서로 반대인 두 줄을 나란히 준다 — *"SPREAD_WIDE 면 단가
이득이 크다"* 와 *"SHELF_TIGHT 면 중품 비중을 낮추는 쪽이 유리하다"*. 둘이 부딪힐 때
무엇이 이기는지는 아무 데도 안 적혀 있었고, 그 빈칸을 판단자가 매번 새로 메웠다 —
같은 입력에 ``MID_HALF`` 5 : ``MID_CAPPED`` 5 였다 (2026-09-15 · 앵커 N=10 · gemini).

★ 이 파일이 지키는 것 셋.

① **선언이 판정을 소유한다** — 값을 바꾸면 결과가 따라 바뀐다 (규칙 8)
② **좁힌 날에도 판단자를 부른다** — 안 부르면 근거 한 줄이 사라지고 「판단 미적용」
   위험 한 줄이 대신 뜬다. 규칙이 정한 것과 «판단을 못 한 것» 은 다른 사실이다
③ **fallback 도 좁힌 쪽으로 간다** — 우열은 규칙이지 판단자가 아니므로, LLM 이 꺼졌든
   전면 실패했든 같은 답이 나와야 한다
"""

from datetime import date

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.llm.mix import MixDecision, shelf_is_tight
from app.purchase_agent.llm.runtime import needs_llm
from app.purchase_agent.llm.schemas import (
    MIX_PRECEDENCE_SIGNAL,
    MixCandidate,
    SanitizedLLMContext,
)
from app.purchase_agent.nodes.allocate_sourcing import (
    apply_mix_precedence,
    build_mix_candidates,
    evaluate_mid_grade,
)
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.state import build_initial_state

#: 부딪히는 날 — 스프레드가 벌어졌고 중품 상한도 걸린 칸. 앵커 15칸 중 둘이 그렇고
#: (배추·무 ``2026-09-11``) 이것이 그중 하나다. 다른 검사 파일과 같은 이름·같은 날짜다.
SPREAD_WIDE = date(2026, 9, 11)
부딪히는_칸 = ("배추", SPREAD_WIDE)


def _staged(item: str, as_of: date) -> dict:
    """③ 까지 돈 State — ``evaluate_mid_grade`` 가 ``base_plan`` 을 읽는다."""
    state = build_initial_state(item, as_of)
    state |= classify_situation(state)
    state |= draft_plan(state)
    return state


# ── ① 선언이 판정을 소유한다 (규칙 8) ────────────────────────────────────────


def _부딪히는_후보들():
    state = _staged(*부딪히는_칸)
    constraints = load_constraints()
    facts = evaluate_mid_grade(state, constraints)
    assert facts["widened"] and shelf_is_tight(facts), "이 칸이 더 이상 부딪히지 않는다"
    후보 = build_mix_candidates(state, facts["cap_ratio"], constraints)
    return 후보, facts, constraints


def test_부딪히는_날만_좁힌다() -> None:
    """라벨 둘이 **같이 뜬 날에만** 후보가 하나로 준다."""
    후보, facts, constraints = _부딪히는_후보들()
    선언 = constraints["grade"]["mix_precedence"]
    assert len(후보) >= 2, "좁히기 전에 고를 것이 있어야 이 검사가 뜻을 갖는다"

    좁힌 = apply_mix_precedence(
        후보, spread_widened=True, shelf_tight=True, cap_ratio=facts["cap_ratio"], declaration=선언
    )
    assert [cid for cid, _, _ in 좁힌] == [선언["narrowed_to"]]

    for 스프레드, 신선도 in ((True, False), (False, True), (False, False)):
        그대로 = apply_mix_precedence(
            후보,
            spread_widened=스프레드,
            shelf_tight=신선도,
            cap_ratio=facts["cap_ratio"],
            declaration=선언,
        )
        assert 그대로 == 후보, f"안 부딪히는 날({스프레드}·{신선도})인데 좁혔다"


def test_선언을_PROVISIONAL_로_내리면_안_좁힌다() -> None:
    """🔴 **규칙 8** — 값 비교가 아니라 **선언을 바꿔 판정이 따라 바뀌는지** 본다."""
    후보, facts, constraints = _부딪히는_후보들()
    내린_선언 = {**constraints["grade"]["mix_precedence"], "status": "PROVISIONAL"}
    assert (
        apply_mix_precedence(
            후보,
            spread_widened=True,
            shelf_tight=True,
            cap_ratio=facts["cap_ratio"],
            declaration=내린_선언,
        )
        == 후보
    )


def test_narrowed_to_를_바꾸면_남는_후보가_따라_바뀐다() -> None:
    """🔴 **규칙 8** — 남길 후보를 선언이 정한다.

    ⚠️ ``winner`` 도 같이 바꾼다. 둘은 **한 쌍**이고, 방향이 어긋나면 아래 검사가 터진다.
    """
    후보, facts, constraints = _부딪히는_후보들()
    바꾼_선언 = {
        **constraints["grade"]["mix_precedence"],
        "winner": "SPREAD_WIDE",
        "narrowed_to": "MID_CAPPED",
    }
    좁힌 = apply_mix_precedence(
        후보,
        spread_widened=True,
        shelf_tight=True,
        cap_ratio=facts["cap_ratio"],
        declaration=바꾼_선언,
    )
    assert [cid for cid, _, _ in 좁힌] == ["MID_CAPPED"]


def test_winner_와_narrowed_to_가_어긋나면_터진다() -> None:
    """🔴 **``winner`` 가 아무도 안 읽는 설명 칸이 되지 않게 한다.**

    ``winner`` 를 «왜» 로만 두면 뒤집어도 판정이 안 따라 바뀌어 **죽은 선언**이 된다.
    그래서 방향 정합성을 코드가 검사한다 — 신선도가 이기는데 상한 후보를 남기라는
    선언은 스스로 모순이고, 조용히 한쪽을 따르면 YAML 이 말하는 것과 코드가 하는 것이
    갈린다.
    """
    후보, facts, constraints = _부딪히는_후보들()
    어긋난_선언 = {
        **constraints["grade"]["mix_precedence"],
        "winner": "SPREAD_WIDE",
        "narrowed_to": "MID_HALF",
    }
    with pytest.raises(ValueError, match="어긋난다"):
        apply_mix_precedence(
            후보,
            spread_widened=True,
            shelf_tight=True,
            cap_ratio=facts["cap_ratio"],
            declaration=어긋난_선언,
        )


def test_모르는_winner_는_터진다() -> None:
    """오타 난 라벨을 «부딪히지 않음» 으로 읽으면 우열표가 조용히 꺼진다."""
    후보, facts, constraints = _부딪히는_후보들()
    with pytest.raises(ValueError, match="아는 라벨이 아니다"):
        apply_mix_precedence(
            후보,
            spread_widened=True,
            shelf_tight=True,
            cap_ratio=facts["cap_ratio"],
            declaration={**constraints["grade"]["mix_precedence"], "winner": "SHELF_TIGH"},
        )


def test_후보에_없는_id_로_좁히라면_안_좁힌다() -> None:
    """🔴 **안을 못 내는 것보다 낫다.** 우열은 «어느 안을 낼까» 이지 «안을 낼까» 가 아니다."""
    후보, facts, constraints = _부딪히는_후보들()
    assert (
        apply_mix_precedence(
            후보,
            spread_widened=True,
            shelf_tight=True,
            cap_ratio=facts["cap_ratio"],
            declaration={**constraints["grade"]["mix_precedence"], "narrowed_to": "없는후보"},
        )
        == 후보
    )


# ── ② 좁힌 날에도 판단자를 부른다 ────────────────────────────────────────────


def _문맥(*, candidates: list[str], signals: list[str]) -> SanitizedLLMContext:
    return SanitizedLLMContext(
        item="배추",
        spread="SPREAD_WIDE",
        freshness="SHELF_TIGHT",
        signals=signals,
        facts=["등급 스프레드가 평시보다 확대됐다."],
        candidates=[MixCandidate(candidate_id=cid, summary="설명") for cid in candidates],
    )


def test_좁힌_날은_후보가_하나여도_부른다() -> None:
    """``needs_llm`` 이 신호를 본다 — 안 보면 근거 한 줄이 조용히 사라진다."""
    assert needs_llm(_문맥(candidates=["MID_HALF"], signals=[MIX_PRECEDENCE_SIGNAL]))


def test_그냥_후보가_하나면_여전히_안_부른다() -> None:
    """🔴 두 경우를 한 조건으로 합치지 않는다.

    «규칙이 정해서 하나» 와 «애초에 고를 게 없어서 하나» 는 다른 사실이고, 뒤쪽에서
    부르면 없던 근거 줄이 생긴다.
    """
    assert not needs_llm(_문맥(candidates=["MID_HALF"], signals=[]))


def test_좁힌_날의_문맥에_이유가_실린다() -> None:
    """판단자에게 **왜 하나뿐인가**를 준다 — 안 주면 사유가 라벨과 어긋난 말을 쓴다."""
    본_것: list[SanitizedLLMContext] = []

    def 본다(context, default_candidate_id):
        본_것.append(context)
        return MixDecision(
            candidate_id=default_candidate_id,
            reason="규칙이 정한 배분을 설명한다",
            llm_status="SUCCESS",
            llm_model="fake",
            llm_fallback_used=False,
        )

    run_purchase_agent("배추", SPREAD_WIDE, selector=본다)
    assert 본_것, "부딪히는 픽스처인데 판단자가 안 불렸다"
    문맥 = 본_것[0]
    assert [c.candidate_id for c in 문맥.candidates] == ["MID_HALF"]
    assert MIX_PRECEDENCE_SIGNAL in 문맥.signals
    assert any("우열" in 사실 for 사실 in 문맥.facts), "왜 하나뿐인지가 안 실렸다"
    # 🔴 숫자를 넣지 않는다 (규칙 6) — 넣으면 판단자가 사유에 베껴 쓴다.
    assert not any(any(글자.isdigit() for 글자 in 사실) for 사실 in 문맥.facts)


def test_좁힌_날에도_근거_한_줄이_남고_미적용_고지가_안_뜬다() -> None:
    """🔴 **화면이 갈리는 자리다.**

    ``package_scenarios`` 가 ⑤ 의 상태를 셋으로 가른다 — 안 부르면 근거에서
    「등급 조합 … 선택」이 빠지고 위험에 「판단 미적용」이 대신 뜬다. 우열표를 넣었다고
    화면 문면이 바뀌면 안 된다.
    """

    def 설명만(context, default_candidate_id):
        del context
        return MixDecision(
            candidate_id=default_candidate_id,
            reason="규칙이 정한 배분을 설명한다",
            llm_status="SUCCESS",
            llm_model="fake",
            llm_fallback_used=False,
        )

    안 = run_purchase_agent("배추", SPREAD_WIDE, selector=설명만)["scenarios"][0]
    근거 = [r for r in 안["rationale"] if "등급 조합" in r["claim"] and "선택" in r["claim"]]
    assert len(근거) == 1
    assert "MID_HALF" in 근거[0]["claim"]
    assert not [x for x in 안["risks"] if "등급 조합 판단 미적용" in x]


# ── ③ fallback 도 좁힌 쪽으로 간다 ───────────────────────────────────────────


def test_판단자가_없어도_좁힌_쪽이_나온다() -> None:
    """🔴 **우열은 규칙이지 판단자가 아니다.** LLM 이 꺼진 날에도 같은 답이어야 한다."""
    꺼짐 = run_purchase_agent("배추", SPREAD_WIDE, selector=None)["scenarios"][0]
    등급별 = {line["grade"]: line["qty_kg"] for line in 꺼짐["sourcing_plan"]}
    assert 등급별.get("중", 0) > 0, "중품을 아예 안 쓰는 날이 아니다"
    assert 등급별["중"] < 등급별["상"], (
        "우열표가 «중품을 덜 싣는» 쪽인데 중품이 더 많다 — 규칙 경로가 우열을 안 탔다"
    )


def test_판단자가_후보_밖을_고르면_좁힌_쪽으로_되돌린다() -> None:
    """전면 실패와 같은 자리다 — 되돌아갈 기본안이 **좁힌 쪽**이어야 한다."""

    def 딴_것을_고른다(context, default_candidate_id):
        del context, default_candidate_id
        return MixDecision(
            candidate_id="MID_CAPPED",  # 좁힌 목록에 없다
            reason="후보 밖",
            llm_status="SUCCESS",
            llm_model="fake",
            llm_fallback_used=False,
        )

    민_것 = run_purchase_agent("배추", SPREAD_WIDE, selector=딴_것을_고른다)["scenarios"][0]
    꺼짐 = run_purchase_agent("배추", SPREAD_WIDE, selector=None)["scenarios"][0]
    assert 민_것["sourcing_plan"] == 꺼짐["sourcing_plan"]
    assert [x for x in 민_것["risks"] if "등급 조합 판단 미적용" in x], (
        "되돌린 사실이 조용히 사라지면 안 된다"
    )
