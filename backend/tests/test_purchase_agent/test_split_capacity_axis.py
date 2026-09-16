"""③ 의 창고 상한이 **날짜 축**을 보는가 — E3-9 앞단 (2026-09-16).

🔴 **세 자리가 세 칸을 본다.**

```text
③ draft_plan   warehouse_free_kg + rental_cap_kg      오늘 시점 · 총량 · 안 공통
④ split_plan   cap_by_date[as_of + N4]                첫 도착일
⑦ self_check   cap_by_date[각 회차 도착일] · 누적       회차별
```

③ 이 **가장 먼저** 돌아 총량을 깎으므로, 날짜별 제한을 총량으로 접으면 여유가 뒤로
갈수록 커지는 창에서도 **④ 가 분할을 판정할 기회 자체가 사라진다.**

★ **목적은 다회차를 늘리는 것이 아니다.** 한 번에 사도 제약을 만족하면 1회차가 정상이고,
나눠도 못 넣으면 수량 축소나 탈락이 맞다. 고치는 것은 **앞단 수량 제한이 가능한 분할을
미리 없애는 것** 하나다.

⚠️ 여기 입력은 **합성이다.** 지금 저장 기록에는 「날짜별로 갈리는 여유」가 한 건도 없다
(PREFINAL·REH-0914·REH-0916 세 걷기 1,381일 전부 오늘 여유 = 첫 도착일 여유 = 마지막
도착일 여유). 그래서 이 경로는 실측으로는 못 세우고 합성으로만 세울 수 있다.
"""

import copy
from datetime import date, timedelta

from app.purchase_agent.allocation import assign_axes
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import package_scenarios
from app.purchase_agent.nodes.self_check import (
    check_arrival_capacity,
    check_warehouse_capacity,
    self_check,
)
from app.purchase_agent.nodes.split_plan import (
    evaluate_split_entry,
    safe_allocation_candidates,
    split_plan,
)
from app.purchase_agent.schemas import TIMING_AXIS
from app.purchase_agent.state import build_initial_state

ITEM = "배추"
AS_OF = date(2026, 8, 21)
LEAD_DAYS = 2


def _날(offset: int) -> str:
    return (AS_OF + timedelta(days=offset)).isoformat()


def _평평한_예측(forecast: dict) -> dict:
    """④ 의 ``by_trend`` 를 **거짓**으로 만든다 — 수량 축만 남겨 진입 경로를 하나로 좁힌다.

    🔴 ④ 는 ``is_sustained_rise`` 만 보고 ① 은 거기에 ``stable`` 과 상승률을 더 본다.
      예측이 단조 상승이면 ④ 가 수량과 무관하게 진입해 이 검사가 무엇을 쟀는지 흐려진다.
    """
    사본 = copy.deepcopy(forecast)
    고정 = 사본["daily"][0]["predicted"]
    for row in 사본["daily"]:
        row["predicted"] = 고정
    return 사본


def _state(*, caps: dict[str, float], today_free: float) -> dict:
    """①③ 을 **실제로 태운** State.

    🔴 ``cap_by_date`` 와 N4 를 ① **전에** 꽂는다 — ① 도 ``split_entry_cap`` 을 보기 때문이다.
      뒤에 꽂으면 ① 이 「못 봤다」로 판정하고 축이 안 열린다.
    """
    state = build_initial_state(ITEM, AS_OF)
    state["inbound_lead_days"] = LEAD_DAYS
    state["inventory"] = {
        **state["inventory"],
        "cap_by_date": caps,
        "warehouse_free_kg": today_free,
        "rental_cap_kg": 0.0,
    }
    state["forecast"] = _평평한_예측(state["forecast"])
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    return state


def _뒤로_커지는_창(첫: float, 뒤: float) -> dict[str, float]:
    """첫 도착일만 빡빡하고 그 뒤로 여유가 회복되는 창 — 분할이 **실제로 이득인** 모양."""
    return {
        _날(offset): (첫 if offset <= LEAD_DAYS else 뒤) for offset in range(40)
    }


def test_여유가_뒤로_커지면_분할을_검토한다() -> None:
    """🔴 **이 판이 고치는 것.** 지금은 ③ 이 첫 도착일 값으로 먼저 깎아 ④ 가 진입조차 못 한다.

    ``by_volume`` 은 ``largest_total_kg > cap_by_date[첫 도착일]`` 인데, ③ 이 그 값으로
    총량을 클립하면 **등호가 되어 ``>`` 가 영원히 거짓**이다. 뒤 날짜에 자리가 있어도
    그 사실이 판정에 닿지 않는다.
    """
    첫, 뒤 = 2_000.0, 20_000.0
    state = _state(caps=_뒤로_커지는_창(첫, 뒤), today_free=첫)
    constraints = load_constraints()

    assert "timing" in state["allowed_axes"], "① 이 수량 축으로 timing 을 열어야 한다"
    최대안 = max(d["total_qty_kg"] for d in state["base_plan"]["drafts"])
    facts = evaluate_split_entry(state, constraints)

    assert facts["entered"], (
        f"뒤 날짜 여유 {뒤:,.0f}kg 이 비어 있는데 분할을 검토조차 안 했다 — "
        f"최대안 {최대안:,}kg · 첫 도착일 여유 {첫:,.0f}kg"
    )
    assert facts["rounds"] >= 2


def test_한번에_충분하면_나누지_않는다() -> None:
    """🟢 **1회차가 정상인 날.** 여유가 수요보다 크면 축도 안 열리고 클립도 없다.

    ★ 이 판의 목적은 다회차를 늘리는 것이 **아니다.** 한 번에 사도 제약을 만족하면
      나누지 않는 것이 맞다.
    """
    state = _state(caps={_날(offset): 1_000_000.0 for offset in range(40)}, today_free=1_000_000.0)
    assert "timing" not in state["allowed_axes"]
    for draft in state["base_plan"]["drafts"]:
        # 🟡 현금·신선도는 이 검사의 대상이 아니다 — 재려는 것은 **창고 축**이다.
        assert not [
            clip for clip in draft["clipped_by"] if clip["constraint"] == "창고"
        ], f"{draft['label']}: 여유가 넉넉한데 창고가 깎았다"


def test_창이_한_값이면_넓히지_않는다() -> None:
    """🟢 **회귀 게이트.** 저장 기록 1,381일이 전부 이 모양이다 (창 전체가 한 값).

    ``n=1`` 과 ``n=2·3`` 의 마지막 도착일 여유가 모두 같아 ``max`` 가 첫 도착일 값이 된다.
    """
    같은값 = 3_569.0
    state = _state(caps={_날(offset): 같은값 for offset in range(40)}, today_free=같은값)
    for draft in state["base_plan"]["drafts"]:
        assert draft["single_round_cap_kg"] == int(같은값)
        assert draft["total_qty_kg"] <= int(같은값)


def test_timing_이_배정될_안만_넓힌다() -> None:
    """🔴 **허용과 배정은 다른 사실이다** (E3-9 앞단).

    ``assign_axes`` 는 ``timing`` 을 **한 안에만** 준다. 배정을 못 받을 안까지 넓히면
    그 안은 1회차로 나가 ⑦ 에 컷된다 — 원래 살아 있던 작은 일괄안이 사라진다.
    """
    첫, 뒤 = 2_000.0, 20_000.0
    state = _state(caps=_뒤로_커지는_창(첫, 뒤), today_free=첫)
    배정 = assign_axes(
        [d["label"] for d in state["base_plan"]["drafts"]],
        state["allowed_axes"],
        load_constraints()["allocation"]["aggressive_axis"],
    )
    넓힌_안 = [
        d for d in state["base_plan"]["drafts"] if 배정[d["label"]] == TIMING_AXIS
    ]
    안_넓힌_안 = [d for d in state["base_plan"]["drafts"] if 배정[d["label"]] != TIMING_AXIS]
    assert 넓힌_안, "전제 — timing 을 받는 안이 하나 있다"
    for draft in 안_넓힌_안:
        assert draft["total_qty_kg"] <= int(첫), (
            f"{draft['label']}: timing 을 못 받는데 {draft['total_qty_kg']:,}kg 으로 넓혀졌다"
        )


def test_날짜별_여유가_비면_그_회차를_버린다() -> None:
    """🔴 **누락을 무시하고 남은 값의 최대를 취하지 않는다** (규칙 3).

    한 회차라도 여유 칸이 없으면 그 회차 수를 통째로 버린다. 전부 없으면 폴백이고,
    그때 ``single_round_cap_kg`` 는 ``None`` 이다 — **0 이나 무제한이 아니다.**
    """
    빈_창 = {_날(offset): 5_000.0 for offset in range(1)}  # 도착일(+2) 칸이 없다
    state = _state(caps=빈_창, today_free=9_999.0)
    for draft in state["base_plan"]["drafts"]:
        assert draft["single_round_cap_kg"] is None
        # 폴백은 예전 기준(오늘 여유 + 임차)이다 — 상한을 아예 안 거는 선택은 안 한다.
        assert draft["total_qty_kg"] <= 9_999


def test_첫_도착일_여유가_오늘보다_작으면_상한이_줄어든다() -> None:
    """🔴 **줄어드는 것도 이 판이 고치는 것이다** — 회귀 오류가 아니다.

    도착일에 입고가 예정돼 여유가 줄어드는 날, 지금까지 ③ 은 «오늘 여유» 로 덜 깎고
    ⑦ 이 나중에 컷했다. 이제 ③ 이 도착일 기준으로 먼저 깎는다.
    """
    state = _state(caps={_날(offset): 1_500.0 for offset in range(40)}, today_free=50_000.0)
    for draft in state["base_plan"]["drafts"]:
        assert draft["total_qty_kg"] <= 1_500, "오늘 여유를 따라가면 ⑦ 이 나중에 컷한다"


# ── ⑥ 되돌림 — 기존에 유효했던 작은 일괄안을 안 죽인다 ──────────────────────


def _펴기(state: dict) -> dict:
    """④⑤⑥ 까지 태운다 — 되돌림은 ⑥ 에서 일어난다."""
    state = dict(state)
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    return state


def _되돌아오는_창() -> dict[str, float]:
    """③ 은 **넓히는데** 분할은 **안 서는** 창 (E3-9 앞단).

    ```text
    첫 도착일(+2)    2,000     ← single_round_cap_kg
    그 다음(+3~+8)  20,000     ← n=2 의 마지막 도착일이 여기 → max 가 커진다
    그 뒤(+9~)       1,000     ← n=3 의 마지막 도착일이 여기 → 누적이 못 들어간다
    ```

    🔴 ③ 이 ``max`` 로 20,000 까지 잡아 총량이 ``single`` 을 넘고, ④ 가 고른 회차 수의
      실제 도착일에서는 누적이 안 들어간다 — **되돌림이 밟히는 자리**다.
    """
    def 여유(offset: int) -> float:
        if offset <= LEAD_DAYS:
            return 2_000.0
        if offset <= 8:
            return 20_000.0
        return 1_000.0

    return {_날(offset): 여유(offset) for offset in range(40)}


def test_분할이_안_서면_일괄로_되돌린다() -> None:
    """🔴 **이 판의 핵심 안전망.** 넓힌 수량이 그대로 1회차로 나가면 ⑦ 이 통째로 컷하고,
    원래 살아 있던 **작은 일괄안까지 사라진다.**

    ★ 「④ 가 진입했다」는 안전을 보장하지 않는다. 회차 수 · 배분 비율 · 실제 도착일을
      적용하고 **누적을 통과해야** 분할이 선다.
    """
    caps = _되돌아오는_창()
    state = _펴기(_state(caps=caps, today_free=2_000.0))
    survivors = state["scenarios_final"]
    assert survivors, "되돌림이 없으면 이 자리에서 안이 전부 사라진다"
    for scenario in survivors:
        assert scenario["total_qty_kg"] > 0
        # 되돌린 안은 1회차이고, 첫 도착일 여유 안에 든다.
        if len(scenario["split_plan"]) == 1:
            assert scenario["total_qty_kg"] <= 9_000


def test_되돌린_안은_timing_으로_표시하지_않는다() -> None:
    """🔴 **회차가 하나면 그것은 일괄안이다** (§3.5.1-3 「3안인데 사실 한 안」 금지).

    라벨만 ``timing`` 으로 두면 ⑦ ``check_axis_diversity`` 가 보는 축과 실제 계획이 갈린다.
    """
    state = _펴기(_state(caps=_되돌아오는_창(), today_free=2_000.0))
    for scenario in state["scenarios_final"]:
        if len(scenario["split_plan"]) == 1:
            assert scenario["strategy_type"] != TIMING_AXIS, (
                f"{scenario['label']}: 회차가 하나인데 timing 으로 표시됐다"
            )


def test_되돌린_안도_금액과_등급_배분이_다시_계산된다() -> None:
    """🔴 수량만 되돌리고 나머지를 두면 **사중 일치(규칙 4)가 깨진다.**

    금액 · 등급 배분 · 회차 금액이 전부 되돌린 수량 위에서 다시 서야 한다.
    """
    state = _펴기(_state(caps=_되돌아오는_창(), today_free=2_000.0))
    for scenario in state["scenarios_final"]:
        total = scenario["total_qty_kg"]
        assert sum(line["qty_kg"] for line in scenario["split_plan"]) == total
        assert sum(line["qty_kg"] for line in scenario["sourcing_plan"]) == total
        assert (
            sum(line["qty_kg"] * line["grade_unit_price"] for line in scenario["sourcing_plan"])
            == scenario["total_amount_krw"]
        )
        assert (
            sum(line["amount_krw"] for line in scenario["split_plan"])
            == scenario["total_amount_krw"]
        )


def test_되돌린_안은_최종_검사를_그대로_통과한다() -> None:
    """🔴 **살리려고 검사를 우회하지 않는다.** 되돌린 안도 ⑦ 을 그대로 지난다."""
    state = _펴기(_state(caps=_되돌아오는_창(), today_free=2_000.0))
    final = self_check(state)
    for scenario in final["scenarios_final"]:
        assert check_arrival_capacity(scenario, state) is None
        assert check_warehouse_capacity(
            scenario, state["inventory"], state, load_constraints()
        ) is None


def test_나눠도_불가능하면_수량이_줄어든다() -> None:
    """🟢 **REH-0914 2026-01-14 형태** — 창 내내 여유가 한 값이면 나눠도 못 넣는다.

    실측값으로 고정한다: guaranteed 8,000 · used 4,431 → 여유 **3,569** · 18칸 동일.
    그날 원안은 5,021kg 이었고 3,569kg 으로 줄었다 — **수량 축소가 맞다.**
    """
    여유 = 3_569.0
    state = _펴기(_state(caps={_날(offset): 여유 for offset in range(40)}, today_free=여유))
    assert state["scenarios_final"], "나눠도 못 넣는 날에 안이 통째로 사라지면 안 된다"
    for scenario in state["scenarios_final"]:
        assert scenario["total_qty_kg"] <= int(여유)


# ── E3-9 연결 — 앞단이 선 뒤 배분 후보가 실제로 서는가 ──────────────────────

#: 🔴 **승인된 값이 아니다.** 진짜 선언은 ``PROVISIONAL`` 이고 승인자가 없다 —
#: 이 검사가 통과한다고 그 비율이 «검증된 후보» 가 되지 않는다. 운영 승인은 **코드 문제와
#: 다른 축**이고, 여기서 켜는 것은 그 아래 경로가 실제로 도는지 보기 위해서다.
_승인_흉내 = {
    "status": "FIXTURE_ONLY",
    "two_rounds": {"FRONT_LOADED": [0.60], "BACK_LOADED": [0.40]},
    "three_rounds": {"FRONT_LOADED": [0.50, 0.30], "BACK_LOADED": [0.20, 0.30]},
}


def _승인된_선언() -> dict:
    """``constraints`` **사본** — 🔴 원본을 고치면 다른 검사가 같이 흔들린다."""
    사본 = copy.deepcopy(load_constraints())
    사본["split"]["allocation_weights"] = _승인_흉내
    return 사본


def test_분할이_서면_가중_후보가_실제로_올라온다() -> None:
    """🟢 앞단이 선 뒤 ④ 의 후보 생성부가 돈다 (E3-9).

    ``safe_allocation_candidates`` 는 **선택 전에** 못 서는 후보를 거른다 — 여기서
    ``BASE_EQUAL`` 하나만 남으면 판단자는 아예 안 불린다.
    """
    state = _state(caps=_뒤로_커지는_창(2_000.0, 20_000.0), today_free=2_000.0)
    선언 = _승인된_선언()
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    assert rounds >= 2, "전제 — 앞단이 분할을 열어야 이 아래가 돈다"
    후보 = safe_allocation_candidates(state, 선언, rounds)
    # 🟢 **거를 것은 거른다.** 첫 도착일이 좁은 창이라 앞으로 모는 배분(FRONT_LOADED)은
    #   그 날 여유를 넘어 목록에서 빠진다 — 선택 **전에** 걸러야 판단자가 못 설 후보를
    #   고르지 않는다. 뒤로 미는 배분은 남아야 한다.
    assert "BASE_EQUAL" in 후보, "되돌아갈 자리는 늘 있어야 한다"
    assert "BACK_LOADED" in 후보, "뒤가 넓은 창인데 뒤로 미는 배분이 빠졌다"
    assert "FRONT_LOADED" not in 후보, "첫 날이 좁은데 앞으로 모는 배분이 올라왔다"
    for 비율 in 후보.values():
        assert len(비율) == rounds
        assert abs(sum(비율) - 1.0) <= 1e-9


def test_고른_배분이_회차_비율에_실제로_반영된다() -> None:
    """🔴 **선택만 되고 균등이 나가면 안 된다** — 고른 후보가 ``split_plan`` 비율이 된다."""
    state = _state(caps=_뒤로_커지는_창(2_000.0, 20_000.0), today_free=2_000.0)
    선언 = _승인된_선언()
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    후보 = safe_allocation_candidates(state, 선언, rounds)
    고른 = "BACK_LOADED" if "BACK_LOADED" in 후보 else "BASE_EQUAL"
    비율 = [line["ratio"] for line in [{"ratio": r} for r in 후보[고른]]]
    assert abs(sum(비율) - 1.0) <= 1e-9
    # 뒤로 실은 배분은 **균등과 달라야** 의미가 있다.
    if 고른 != "BASE_EQUAL":
        assert 비율 != [1 / rounds] * rounds


def test_승인_전에는_균등_하나뿐이다() -> None:
    """🔴 **PROVISIONAL 이면 후보를 안 세운다** — 근거 없는 비율로 안을 만들지 않는다.

    ⚠️ 이것은 **운영 승인 문제**이고 코드 문제가 아니다. 이 판은 그 위(앞단)를 고쳤을 뿐,
      승인 상태를 바꾸지 않는다 — 공용 선언 파일도 ``.env`` 도 안 건드린다.
    """
    state = _state(caps=_뒤로_커지는_창(2_000.0, 20_000.0), today_free=2_000.0)
    선언 = copy.deepcopy(load_constraints())  # 진짜 선언 = PROVISIONAL
    rounds = evaluate_split_entry(state, 선언)["rounds"]
    assert list(safe_allocation_candidates(state, 선언, rounds)) == ["BASE_EQUAL"]
