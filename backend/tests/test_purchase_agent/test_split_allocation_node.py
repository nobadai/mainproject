"""④ 배분 후보가 **노드 경로에서** 실제로 서는가 (E3-9).

🔴 **단위 검사만으로는 못 잡는 자리가 있었다.** 선언이 ``PROVISIONAL`` 이라 가중 후보
생성부가 한 번도 안 돌았고, 그 아래에서 ``draft["qty_kg"]`` 와
``state["inventory"]["execution_calendar"]`` 를 읽고 있었다 — **둘 다 없는 칸**이다.
정책이 승인되는 순간 처음 터질 잠복 오류였고, ``allocation_candidates()`` 단위 검사는
그 위에서 멈추므로 영원히 안 걸린다.

★ 그래서 이 파일은 **승인 상태의 합성 선언**을 꽂고 ④ → ⑥ → ⑦ 을 실제로 태운다.

⚠️ 비율은 **fixture 전용**이다. 진짜 선언은 ``PROVISIONAL`` 이고 승인자가 없다 — 이
검사가 통과한다고 그 비율이 «검증된 후보» 가 되는 것이 아니다. **사업 효과도 아니다**:
정본 걷기(`SIM-CHAIN-V13`) 231안은 전부 1회차다.
"""

import copy
import json
from datetime import date, timedelta

import pytest

from app.purchase_agent.allocation import arrival_dates, split_quantities
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import materialize_split, package_scenarios
from app.purchase_agent.nodes.self_check import self_check
from app.purchase_agent.nodes.split_plan import (
    evaluate_split_entry,
    safe_allocation_candidates,
    split_plan,
)
from app.purchase_agent.state import build_initial_state

ITEM = "배추"
#: 지속 상승 앵커 — ④ 가 궤적으로 **진입한다** (``rounds`` 2). 진입 안 하면 후보가 없다.
AS_OF = date(2026, 8, 21)
LEAD_DAYS = 2

#: 🔴 **승인된 값이 아니다.** 잠복 오류가 드러나게 «승인 상태» 를 흉내 낸 것이다.
승인된_선언 = {
    # 🔴 **검사 안에서만 ``APPROVED`` 다** (2026-09-17). 게이트가 정확히 이 값일 때만 열려
    #   흉내 문자열(``FIXTURE_ONLY``)로는 이 아래가 안 돈다. 선언 파일은 ``PROVISIONAL`` 그대로다.
    "status": "APPROVED",
    "two_rounds": {"FRONT_LOADED": [0.60], "BACK_LOADED": [0.40]},
    "three_rounds": {"FRONT_LOADED": [0.50, 0.30], "BACK_LOADED": [0.20, 0.30]},
}


def _선언(*, 승인: bool) -> dict:
    """``constraints`` 사본. 🔴 원본을 고치면 다른 검사가 같이 흔들린다."""
    사본 = copy.deepcopy(load_constraints())
    사본["split"]["allocation_weights"] = (
        승인된_선언 if 승인 else {**승인된_선언, "status": "PROVISIONAL"}
    )
    return 사본


def _날(offset: int) -> str:
    return (AS_OF + timedelta(days=offset)).isoformat()


def _넉넉한_여유(kg: float = 1_000_000.0) -> dict[str, float]:
    """창 전체에 여유를 깐다 — 실측상 ``cap_by_date`` 는 창 전체가 한 값이다."""
    return {_날(offset): kg for offset in range(40)}


def _state(
    *, cap: dict[str, float] | None, calendar: dict | None = None, 최상위: bool = True
) -> dict:
    """①②③ 까지 실제로 태운 State. ④ 가 읽는 칸만 검사가 채운다."""
    state = build_initial_state(ITEM, AS_OF)
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    # ⚠️ N4 는 **최상위**다 — ``pending_value`` 가 보는 자리가 거기다.
    state["inbound_lead_days"] = LEAD_DAYS
    state["inventory"] = {**state["inventory"], "cap_by_date": cap}
    if calendar is not None and 최상위:
        state["execution_calendar"] = calendar
    elif calendar is not None:
        # 🔴 **틀린 자리를 흉내 낸다** — 고치기 전 코드가 읽던 곳이다. 최상위에는 안 넣는다.
        state["inventory"] = {**state["inventory"], "execution_calendar": calendar}
    return state


def _고정_선택자(고를_id: str):
    """후보 하나를 **반드시** 고르는 판단자. LLM 없이 각 후보의 결과를 본다."""
    from app.purchase_agent.llm.split_schemas import (
        SplitAllocationChoice,
        SplitAllocationResult,
    )

    def selector(context, default_candidate_id: str) -> SplitAllocationResult:
        return SplitAllocationResult(
            interpretation=SplitAllocationChoice(
                chosen_candidate_id=고를_id, reason="검사가 고정한 선택"
            ),
            llm_status="SUCCESS",
            llm_provider="fake",
            llm_model="fake",
            llm_attempts=1,
            llm_fallback_used=False,
        )

    return selector


# ── 승인되면 그 아래가 실제로 돈다 ─────────────────────────────────────


def test_승인_전에는_가중_후보_생성부가_안_돈다() -> None:
    """🔴 **이것이 잠복 오류를 숨긴 조건이다.**

    ``PROVISIONAL`` 이면 ``allocation_candidates`` 가 균등 하나만 돌려주고 그 아래가
    통째로 안 돈다 — 그래서 없는 칸을 읽고 있어도 검사가 초록이었다.
    """
    state = _state(cap=_넉넉한_여유())
    선언 = _선언(승인=False)
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    assert rounds >= 2
    assert list(safe_allocation_candidates(state, 선언, rounds)) == ["BASE_EQUAL"]


def test_승인되면_앞뒤_후보가_실제로_선다() -> None:
    """승인 상태에서는 ``safe_allocation_candidates`` 전체가 돈다 — 여기서 터지던 자리다."""
    state = _state(cap=_넉넉한_여유())
    선언 = _선언(승인=True)
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    후보 = safe_allocation_candidates(state, 선언, rounds)
    assert set(후보) == {"BASE_EQUAL", "FRONT_LOADED", "BACK_LOADED"}
    for 비율 in 후보.values():
        assert len(비율) == rounds
        assert abs(sum(비율) - 1.0) <= 1e-9


def test_안에는_qty_kg_라는_칸이_없다() -> None:
    """🔴 **읽던 이름이 실재하지 않았다.** ③ 이 내는 칸은 ``total_qty_kg`` 다."""
    state = _state(cap=_넉넉한_여유())
    for draft in state["base_plan"]["drafts"]:
        assert "qty_kg" not in draft
        assert "total_qty_kg" in draft


def test_총량을_안의_total_qty_kg_로_투영한다() -> None:
    """투영이 **그 안의 총량**에서 나오는가 — 라벨마다 총량이 다르다."""
    state = _state(cap=_넉넉한_여유())
    선언 = _선언(승인=True)
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    후보 = safe_allocation_candidates(state, 선언, rounds)
    for draft in state["base_plan"]["drafts"]:
        if draft["total_qty_kg"] <= 0:
            continue  # ⑥ 이 이 안을 떨어뜨린다 (수량 0 은 안이 될 수 없다)
        for 비율 in 후보.values():
            수량 = split_quantities(draft["total_qty_kg"], [{"ratio": r} for r in 비율])
            assert sum(수량) == draft["total_qty_kg"]


# ── 달력을 어디서 읽는가 ────────────────────────────────────────────


#: 🔴 **밀린 자리에만 여유가 있다.** 달력을 안 읽으면 도착일이 여유가 없는 날에 서고,
#: 그 후보는 통째로 빠진다 — 그래서 «어디서 읽는가» 가 **결과를 가른다.**
#:
#: 회차 오프셋은 두 안 모두 2회차다 — 기본(D=5) ``[0, 2]`` · 공격(D=12) ``[0, 6]``.
#: 그 둘을 닫으면 ``[0, 3]`` · ``[0, 7]`` 로 밀리고 도착일(+N4)이 하루씩 뒤로 간다.
_닫는_오프셋 = (2, 6)
#: 밀린 뒤 도착일만 적는다. **안 민 자리는 아예 없다** — 모르는 날은 «든다» 가 아니다 (규칙 3).
_여유_있는_날 = (0, 1, 2, 3, 5, 9)


def _밀어야만_맞는_입력() -> tuple[dict, dict[str, float]]:
    calendar = {
        "non_execution_days": [_날(offset) for offset in _닫는_오프셋],
        "horizon_end": _날(39),
    }
    cap = {_날(offset): 1_000_000.0 for offset in _여유_있는_날}
    return calendar, cap


def test_밀기가_실제로_일어나는_입력인가() -> None:
    """전제 확인 — 안 밀리는 입력이면 아래 검사가 **아무것도 안 잰다.**"""
    calendar, _ = _밀어야만_맞는_입력()
    state = _state(cap=None)
    for draft in state["base_plan"]["drafts"]:
        if draft["total_qty_kg"] <= 0:
            continue
        민_것 = arrival_dates(state["date"], draft["coverage_days"], 2, LEAD_DAYS, calendar)
        안_민_것 = arrival_dates(state["date"], draft["coverage_days"], 2, LEAD_DAYS, None)
        assert 민_것 != 안_민_것, draft["label"]


def test_달력은_State_최상위에서_읽는다() -> None:
    """🔴 ⑥·⑦ 이 읽는 자리와 **같은 자리**여야 한다.

    여유를 **밀린 도착일에만** 깔았으므로, 달력을 최상위에서 읽어야 후보가 살고 다른
    자리를 읽으면 죽는다 — 어느 쪽으로 틀려도 결과가 갈린다.

    ⚠️ 이렇게 안 짜면 못 잡는다. 전에는 *"달력이 밀기를 만든다"* 만 확인하고 ④의 결과를
    안 봐서, 읽는 자리를 ``inventory`` 로 되돌려도 검사가 **초록으로 지나갔다.**
    """
    calendar, cap = _밀어야만_맞는_입력()
    선언 = _선언(승인=True)

    최상위 = _state(cap=cap, calendar=calendar, 최상위=True)
    rounds = evaluate_split_entry(최상위, 선언)["rounds"]
    assert set(safe_allocation_candidates(최상위, 선언, rounds)) == {
        "BASE_EQUAL",
        "FRONT_LOADED",
        "BACK_LOADED",
    }

    엉뚱한_자리 = _state(cap=cap, calendar=calendar, 최상위=False)
    assert 엉뚱한_자리.get("execution_calendar") is None
    assert list(safe_allocation_candidates(엉뚱한_자리, 선언, rounds)) == ["BASE_EQUAL"]


def test_사전검사_날짜가_여섯번_materialize_날짜와_같다() -> None:
    """🔴 **같은 함수를 쓴다가 실제로 같은 값을 낸다**를 잰다.

    ④가 «선다» 고 본 도착일과 ⑥이 실제로 적는 도착일이 다르면, 그 안은 **왜 죽었는지
    설명할 수 없다.**
    """
    calendar, cap = _밀어야만_맞는_입력()
    state = _state(cap=cap, calendar=calendar)
    선언 = _선언(승인=True)
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    후보 = safe_allocation_candidates(state, 선언, rounds)

    for draft in state["base_plan"]["drafts"]:
        if draft["total_qty_kg"] <= 0:
            continue
        for 이름, 비율 in 후보.items():
            회차 = [{"ratio": r} for r in 비율]
            사전검사 = arrival_dates(
                state["date"], draft["coverage_days"], len(회차), LEAD_DAYS, calendar
            )
            실제 = [
                줄["expected_arrival_date"]
                for 줄 in materialize_split(
                    state["date"],
                    draft["total_qty_kg"],
                    회차,
                    draft["coverage_days"],
                    lead_days=LEAD_DAYS,
                    cap_by_date=cap,
                    calendar=calendar,
                )
            ]
            if len(실제) == len(사전검사):
                assert 실제 == 사전검사, (draft["label"], 이름)


def test_감당_못_하는_안은_후보를_거르는_근거가_아니다() -> None:
    """⑥ 이 1회차로 되돌릴 안에서 다회차 도착일을 재면 **쓰이지도 않을 계산**으로 후보가 죽는다.

    보수안(D=2·총량 0)이 그 자리다 — ``split_infeasible_reason`` 이 걸러 준다.
    """
    state = _state(cap=_넉넉한_여유())
    선언 = _선언(승인=True)
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    보수 = next(d for d in state["base_plan"]["drafts"] if d["label"] == "보수")
    assert 보수["total_qty_kg"] == 0  # 이 안은 ⑥ 이 떨어뜨린다
    assert len(safe_allocation_candidates(state, 선언, rounds)) == 3


# ── 어느 후보를 골라도 ⑦ 을 통과한다 ────────────────────────────────


@pytest.mark.parametrize("후보_id", ["BASE_EQUAL", "FRONT_LOADED", "BACK_LOADED"])
def test_어느_후보를_골라도_일곱번_검사를_통과한다(
    monkeypatch: pytest.MonkeyPatch, 후보_id: str
) -> None:
    """🔴 **선택 전 안전 보장이 뜻하는 것이 이것이다.**

    ⑤ 의 ``test_any_choice_keeps_the_quadruple_match`` 와 같은 자리다 — 판단자가 무엇을
    골라도 사중 일치가 서고 ⑦ 이 안을 죽이지 않아야 한다.
    """
    monkeypatch.setenv("PURCHASE_LLM_SPLIT_ALLOCATION_ENABLED", "true")
    monkeypatch.setattr(
        "app.purchase_agent.nodes.split_plan.load_constraints", lambda: _선언(승인=True)
    )
    state = _state(cap=_넉넉한_여유())
    state.update(split_plan(state, selector=_고정_선택자(후보_id)))
    결정 = state["split_plan"][0]["decision"]
    assert 후보_id in 결정["allocation_candidates"]
    assert 결정["allocation_chosen"] == 후보_id

    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    결과 = self_check(state)

    assert 결과["scenarios_final"], f"{후보_id} 에서 안이 하나도 안 남았다"
    # 🔴 ⑦ 이 낸 컷만 본다. ⑥ 의 «필요 없다»(보유가 이미 덮는다)는 배분과 무관하고
    #   후보와 상관없이 늘 같다 — ``kind`` 가 있는 줄이 그쪽이다.
    일곱번_컷 = [줄 for 줄 in 결과["rejected_reasons"] if "kind" not in 줄]
    assert 일곱번_컷 == [], f"{후보_id} 를 골랐더니 ⑦ 이 컷했다: {일곱번_컷}"
    for 안 in 결과["proposal"]["scenarios"]:
        assert sum(줄["qty_kg"] for 줄 in 안["split_plan"]) == 안["total_qty_kg"]
        assert sum(줄["qty_kg"] for 줄 in 안["sourcing_plan"]) == 안["total_qty_kg"]
        assert 안["total_amount_krw"] == sum(
            줄["qty_kg"] * 줄["grade_unit_price"] for 줄 in 안["sourcing_plan"]
        )


# ── 노드 2차 방어선 (2026-09-18) ────────────────────────────────────────────


def _후보_밖_선택자(사유: str):
    """검증기를 **우회해** 후보 밖 id 를 ``SUCCESS`` 로 들고 오는 판단자.

    🔴 ``selector`` 는 주입 가능한 콜러블이라 ``validate_choice`` 를 안 지난다 — 어댑터가
      꽂는 진짜 판단자는 그 문을 지나지만, 이 층을 믿고 노드가 검사를 생략하면
      **우회로가 열린 채로 남는다.** ⑤ 가 Codex 교차검증 P2 에서 같은 자리를 막았다.
    """
    from app.purchase_agent.llm.split_schemas import (
        SplitAllocationChoice,
        SplitAllocationResult,
    )

    def selector(context, default_candidate_id: str) -> SplitAllocationResult:
        return SplitAllocationResult(
            interpretation=SplitAllocationChoice(
                chosen_candidate_id="SIDEWAYS_LOADED", reason=사유
            ),
            llm_status="SUCCESS",
            llm_provider="fake",
            llm_model="fake",
            llm_attempts=1,
            llm_fallback_used=False,
        )

    return selector


def _우회_상태(monkeypatch: pytest.MonkeyPatch, 사유: str) -> dict:
    monkeypatch.setenv("PURCHASE_LLM_SPLIT_ALLOCATION_ENABLED", "true")
    monkeypatch.setattr(
        "app.purchase_agent.nodes.split_plan.load_constraints", lambda: _선언(승인=True)
    )
    state = _state(cap=_넉넉한_여유())
    state.update(split_plan(state, selector=_후보_밖_선택자(사유)))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    state.update(self_check(state))
    return state


def test_후보_밖_id_는_노드가_한_번_더_막는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **되돌린 사실이 보여야 한다 — 조용히 넘기지 않는다.**

    고른 후보만 균등으로 되돌리고 상태를 ``SUCCESS`` 로 두면 실행 흔적이 「판단자가 골랐다」
    로 남는데 결과는 기본안이다 — 흔적과 산출물이 서로를 부정한다.
    """
    state = _우회_상태(monkeypatch, "앞에 실으면 단가에 유리하다")
    결정 = state["split_plan"][0]["decision"]
    assert 결정["allocation_chosen"] == "BASE_EQUAL"
    판단 = 결정["allocation_judgment"]
    assert 판단 is not None, "지우지 않는다 — 지우면 되돌린 사실까지 사라진다"
    assert (판단.llm_status, 판단.llm_fallback_used) == ("FALLBACK", True)
    고지 = [
        줄
        for 안 in state["proposal"]["scenarios"]
        for 줄 in 안["risks"]
        if "회차 배분 판단 미적용" in 줄
    ]
    assert 고지, "되돌린 사실이 risks 에 안 실렸다"


def test_후보_밖_id_를_들고_온_판단자의_사유는_어디에도_안_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 **이것이 이 방어선의 본체다.**

    전에는 고른 후보만 균등으로 되돌아가고 **사유 문장은 그대로 남아**, 근거에
    「회차 배분 회차를 고르게 나눈다(BASE_EQUAL) 선택 — <남의 사유>」가 나갔다
    (2026-09-18 재현 · 앵커 두 날 × ``timing`` 안). 라벨과 문장이 어긋난 상태다.

    ★ 칸 이름을 손으로 훑지 않는다 — **산출물 전체를 뒤져** 마커가 한 군데도 없는지 본다.
      칸을 적으면 나중에 생기는 칸이 검사 밖에 남는다.
    """
    마커 = "우회사유마커"
    state = _우회_상태(monkeypatch, f"{마커} 앞에 몰면 좋다")
    assert 마커 not in json.dumps(state["proposal"], ensure_ascii=False, default=str)


def test_되돌린_뒤_배분은_규칙_기본안과_같다(monkeypatch: pytest.MonkeyPatch) -> None:
    """되돌림은 **무변화**여야 한다 — 판단자를 안 꽂았을 때와 회차 비율이 같다."""
    monkeypatch.setenv("PURCHASE_LLM_SPLIT_ALLOCATION_ENABLED", "true")
    monkeypatch.setattr(
        "app.purchase_agent.nodes.split_plan.load_constraints", lambda: _선언(승인=True)
    )
    기본 = _state(cap=_넉넉한_여유())
    기본.update(split_plan(기본, selector=None))
    우회 = _state(cap=_넉넉한_여유())
    우회.update(split_plan(우회, selector=_후보_밖_선택자("남의 사유")))
    assert [줄["ratio"] for 줄 in 우회["split_plan"]] == [
        줄["ratio"] for 줄 in 기본["split_plan"]
    ]
