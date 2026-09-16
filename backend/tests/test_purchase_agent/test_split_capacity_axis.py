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

from app.purchase_agent.allocation import assign_axes, round_offsets
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import (
    PAYMENT_CONFLICT_NOTE,
    package_scenarios,
)
from app.purchase_agent.nodes.self_check import (
    check_arrival_capacity,
    check_cash_ceiling,
    check_payment_schedule,
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


def _state(
    *,
    caps: dict[str, float],
    today_free: float,
    cash: int | None = None,
    n5: int | None = None,
    critical: list[str] | None = None,
) -> dict:
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
    if cash is not None:
        state["projected_cash_min"] = cash
    # 🔴 **N5 는 ① 전에 싣는다** — 어댑터가 초기 State 에 싣는 자리와 같다. ③ 이 뒤에
    #   받으면 「지급 소요일 미확정」 고지를 이미 만든 뒤라 문면과 일정이 갈린다.
    if n5 is not None:
        state["purchase_payment_days"] = n5
    if critical is not None:
        state["critical_payment_dates"] = critical
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
    # 🔴 **검사 안에서만 ``APPROVED`` 다** (2026-09-17). 게이트가 정확히 이 값일 때만 열려
    #   흉내 문자열(``FIXTURE_ONLY``)로는 이 아래가 안 돈다. 선언 파일은 ``PROVISIONAL`` 그대로다.
    "status": "APPROVED",
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


# ── 2026-09-16 검토에서 나온 셋 ─────────────────────────────────────────────


def test_회차_수는_클립_뒤_총량으로_정한다() -> None:
    """🔴 **진입과 회차 수는 다른 수를 본다** (검토 지적 1).

    ```text
    진입   한 번에 다 들어가는가        ← 깎기 전 수요 (raw_qty_kg)
    회차   몇 번에 나눠야 들어가는가    ← 깎은 뒤 총량 (total_qty_kg)
    ```

    현금·신선도·조정안이 이미 줄여 놓은 양을 깎기 전 수요로 나누면 **필요보다 여러 번**에
    나눈다 — 실측으로 raw 12,429kg · 실제 1,090kg 인데 3회차를 골랐다.
    """
    state = _state(
        caps=_뒤로_커지는_창(2_000.0, 200_000.0), today_free=2_000.0, cash=3_000_000
    )
    facts = evaluate_split_entry(state, load_constraints())
    assert facts["entered"], "전제 — 깎기 전 수요로는 진입한다"
    최대_total = max(d["total_qty_kg"] for d in state["base_plan"]["drafts"])
    필요 = -(-최대_total // int(facts["cap_kg"]))  # ceil
    assert facts["rounds"] <= max(필요, 2), (
        f"실제 총량 {최대_total:,}kg 이면 {필요}회차면 되는데 {facts['rounds']}회차를 골랐다"
    )


def test_넓힌_안은_여유를_못_보면_되돌린다() -> None:
    """🔴 **「못 봤다」는 「선다」가 아니다** (검토 지적 2).

    ⑦ 은 여유를 못 보면 컷이 아니라 **skip** 한다. 그 태도는 **원래 서 있던 계획**에
    맞는 것이고, **분할을 전제로 넓힌 수량**은 처지가 다르다 — 넓힌 근거가 바로 그
    여유이므로, 못 보면 넓힐 근거가 사라진 것이다.
    """
    # 창을 짧게 — 뒤 회차 도착일이 창 밖으로 나간다.
    짧은_창 = {_날(offset): (500.0 if offset <= LEAD_DAYS else 200_000.0) for offset in range(6)}
    state = _펴기(_state(caps=짧은_창, today_free=500.0))
    for scenario in state["scenarios_final"]:
        single = next(
            d["single_round_cap_kg"]
            for d in state["base_plan"]["drafts"]
            if d["label"] == scenario["label"]
        )
        if single is None:
            continue
        창밖 = [
            leg.get("expected_arrival_date")
            for leg in scenario["split_plan"]
            if leg.get("expected_arrival_date") not in 짧은_창
        ]
        if 창밖:
            assert scenario["total_qty_kg"] <= single, (
                f"{scenario['label']}: 도착일 {창밖} 여유를 못 보는데 넓힌 "
                f"{scenario['total_qty_kg']:,}kg 이 그대로 나갔다"
            )


def test_되돌림이_아닌_이유로_timing_이_없으면_축을_안_좁힌다() -> None:
    """🔴 **「timing 안이 없다」만 보면 다른 탈락을 가린다** (검토 지적 3).

    timing 안이 현금·등급 등 **다른 검사에서 탈락**해서 없을 수도 있다. 그때 축을 빼면
    *"축이 둘인데 아무도 안 썼다"* 라는 사실이 조용히 사라진다. ⑥ 이 **실제로 되돌린
    라벨**을 적어 보내고, ⑦ 은 그것이 있을 때만 좁힌다.
    """
    # 되돌림이 일어나는 창(실익 없음) — 그때는 라벨이 실린다.
    같은값 = 3_569.0
    state = _펴기(_state(caps={_날(o): 같은값 for o in range(40)}, today_free=같은값))
    assert state["split_rolled_back_labels"], "되돌린 날인데 라벨이 안 실렸다"
    assert all(s["strategy_type"] != TIMING_AXIS for s in state["scenarios_final"])

    # 되돌림이 없는 날 — 목록이 비어 있어야 한다.
    넉넉 = {_날(offset): 1_000_000.0 for offset in range(40)}
    깨끗 = _펴기(_state(caps=넉넉, today_free=1_000_000.0))
    assert 깨끗["split_rolled_back_labels"] == [], (
        "되돌린 적이 없는데 라벨이 실렸다 — ⑦ 이 축을 잘못 좁힌다"
    )


def test_다른_회차_수를_볼_때_고른_비율을_재사용하지_않는다() -> None:
    """🔴 **판단자가 고른 비율은 그 회차 수 전용이다** (검토 지적 1 후속).

    2회차용 ``[0.6, 0.4]`` 를 3회차에 늘려 쓰면 판단자가 보지도 않은 배분이 «고른 것» 으로
    나간다. 다른 회차 수는 **균등**으로만 본다.
    """
    state = _펴기(_state(caps=_되돌아오는_창(), today_free=2_000.0))
    바뀐_안 = [
        s for s in state["scenarios_final"]
        if any("회 균등으로 바꿨다" in risk for risk in s["risks"])
    ]
    assert 바뀐_안, "④ 가 고른 회차 수가 안 서는 창인데 다른 회차 수를 안 봤다"
    for scenario in 바뀐_안:
        legs = scenario["split_plan"]
        # 🔴 **바꾼 뒤에도 총합은 불변이다** — 회차 수를 바꾸는 것이 수량을 바꾸는 일이
        #   되면 사중 일치(규칙 4)가 깨진다.
        assert sum(leg["qty_kg"] for leg in legs) == scenario["total_qty_kg"]
        assert sum(leg["amount_krw"] for leg in legs) == scenario["total_amount_krw"]
        # 🟡 회차 **수량**은 균등이 아닐 수 있다 — ⑥ cap_constrained_quantities 가
        #   도착일 여유로 재배분하기 때문이다. 균등인 것은 **출발 비율**이고, 그 사실은
        #   문면이 든다 ("균등으로 바꿨다").
        assert len(legs) >= 2


# ── 지급 일정 — 최종 회차 위에서 서는가 (2026-09-16) ─────────────────────────
#
# 🔴 **지급일 규칙은 새로 만들지 않는다** — 회차 ``date + N5`` (달력일 · 영업일 보정 없음 ·
#   ``payment_dates`` / ``build_payment_schedule``). 여기서 보는 것은 회차 수가 **바뀌거나
#   되돌아간 뒤에도** 그 규칙이 **최종 회차**에 걸리는가다.
#
# ⚠️ **날짜별 잔액 재계산은 매입에 없다** — 설계상 재무 ``SCENARIO_VALIDATION`` 몫이다
#   (``_payment_risks`` docstring). 매입이 날짜로 보는 현금 제약은 **재무 집중일과의 겹침
#   고지**이고, 총액 상한은 ``check_cash_ceiling`` 이다. 그 둘이 최종 일정을 보는지 잠근다.

N5 = 7


def _일괄_복귀_창() -> dict[str, float]:
    """③ 은 넓히는데 **어느 회차 수로도** 분할이 안 서는 창.

    ```text
    +0~+2    2,000   첫 도착일 (일괄 기준)
    +3~+7      500   3회차의 가운데 도착일이 여기 → 누적이 못 들어간다
    +8       1,000   2회차의 마지막 도착일 → 누적이 못 들어간다
    +9~     20,000   3회차의 마지막 도착일 → ③ 이 여기까지 넓힌다
    ```
    """

    def 여유(offset: int) -> float:
        if offset <= LEAD_DAYS:
            return 2_000.0
        if offset <= 7:
            return 500.0
        if offset == 8:
            return 1_000.0
        return 20_000.0

    return {_날(offset): 여유(offset) for offset in range(40)}


def _최종(caps: dict[str, float], *, n5: int | None = N5, critical=None) -> tuple[dict, dict]:
    state = _펴기(_state(caps=caps, today_free=2_000.0, n5=n5, critical=critical))
    return state, self_check(state)


def _공격(final: dict) -> dict:
    return next(s for s in final["scenarios_final"] if s["label"] == "공격")


def _지급_규칙(scenario: dict, n5: int) -> list[str]:
    """기존 규칙 그대로 — 최종 회차 date + N5."""
    return [
        (date.fromisoformat(leg["date"]) + timedelta(days=n5)).isoformat()
        for leg in scenario["split_plan"]
    ]


def _지급_일관성(scenario: dict, state: dict, n5: int) -> None:
    schedule = scenario["payment_schedule"]
    legs = scenario["split_plan"]
    assert [row["payment_date"] for row in schedule] == _지급_규칙(scenario, n5)
    assert [row["purchase_date"] for row in schedule] == [leg["date"] for leg in legs]
    assert [row["qty_kg"] for row in schedule] == [leg["qty_kg"] for leg in legs]
    assert [row["amount_krw"] for row in schedule] == [leg["amount_krw"] for leg in legs]
    assert sum(row["amount_krw"] for row in schedule) == scenario["total_amount_krw"]
    assert sum(leg["amount_krw"] for leg in legs) == scenario["total_amount_krw"]
    assert sum(row["qty_kg"] for row in schedule) == scenario["total_qty_kg"]
    assert check_payment_schedule(scenario, state) is None
    assert check_cash_ceiling(scenario, state, load_constraints()) is None
    # 받은 N5 로 만들었으면 「미확정이라 보류」 문장이 남으면 안 된다 — 문면과 일정이 갈린다.
    assert not [r for r in scenario["risks"] if "지급 소요일이 미확정" in r]


def test_지급_정상_다회차() -> None:
    """🟢 분할이 그대로 선 날 — 회차마다 ``date + N5`` 로 지급하고 합이 총액이다."""
    state, final = _최종(_뒤로_커지는_창(2_000.0, 20_000.0))
    공격 = _공격(final)
    assert 공격["strategy_type"] == TIMING_AXIS and len(공격["split_plan"]) == 3
    _지급_일관성(공격, state, N5)


def test_지급_3회차_실패_뒤_2회차로_전환() -> None:
    """🔴 **지급 일정이 버린 3회차가 아니라 최종 2회차를 따른다.**

    3회차 일정이 남으면 재무가 **없는 회차의 돈**을 Cashflow 에 얹는다.
    """
    caps = _되돌아오는_창()
    state, final = _최종(caps)
    공격 = _공격(final)
    assert any("회 균등으로 바꿨다" in r for r in 공격["risks"]), "전제 — 회차 수가 바뀐 날이다"
    assert len(공격["split_plan"]) == 2
    _지급_일관성(공격, state, N5)

    # 버린 3회차 일정의 지급일은 어디에도 없어야 한다.
    버린_3회 = [
        (AS_OF + timedelta(days=offset + N5)).isoformat()
        for offset in round_offsets(AS_OF.isoformat(), 공격["coverage_days"], 3, None)
    ]
    최종 = {row["payment_date"] for row in 공격["payment_schedule"]}
    assert set(버린_3회) - 최종, "전제 — 두 일정이 달라야 잴 수 있다"
    assert len(최종) == 2 and 최종 == set(_지급_규칙(공격, N5))


def test_지급_분할_실패_뒤_일괄로_복귀() -> None:
    """🔴 **일괄로 돌아오면 payment_schedule 키가 없다** — 기존 규칙 그대로다.

    일괄 안의 지급일은 ``split_plan[0].date + N5`` 하나라 같은 값을 두 벌 내지 않는다
    (``build_payment_schedule`` 의 «회차가 하나면 None»). 되돌린 뒤에도 그 규칙이 서고,
    수량·금액은 **되돌린 수량** 위에서 맞아야 한다.
    """
    state, final = _최종(_일괄_복귀_창())
    assert "공격" in state["split_rolled_back_labels"], "전제 — 되돌린 날이다"
    공격 = _공격(final)
    assert len(공격["split_plan"]) == 1 and 공격["strategy_type"] != TIMING_AXIS
    assert "payment_schedule" not in 공격
    assert check_payment_schedule(공격, state) is None
    assert check_cash_ceiling(공격, state, load_constraints()) is None
    assert 공격["split_plan"][0]["amount_krw"] == 공격["total_amount_krw"]
    assert (
        sum(x["qty_kg"] * x["grade_unit_price"] for x in 공격["sourcing_plan"])
        == 공격["total_amount_krw"]
    )


def test_재무_집중일_겹침은_최종_일정으로_본다() -> None:
    """🔴 **날짜로 보는 현금 제약이 버린 일정을 보면 안 된다.**

    3회 → 2회로 바뀐 날, 버린 3회차의 지급일과 최종 2회차의 지급일을 **둘 다** 집중일로
    준다. 고지는 **최종 일정의 날짜만** 적어야 한다.
    """
    caps = _되돌아오는_창()
    _, final = _최종(caps)
    공격 = _공격(final)
    최종 = [row["payment_date"] for row in 공격["payment_schedule"]]
    버린_3회 = [
        (AS_OF + timedelta(days=offset + N5)).isoformat()
        for offset in round_offsets(AS_OF.isoformat(), 공격["coverage_days"], 3, None)
    ]
    버린_것만 = sorted(set(버린_3회) - set(최종))
    assert 버린_것만, "전제 — 버린 일정에만 있는 날짜가 있어야 잴 수 있다"

    _, final = _최종(caps, critical=[버린_것만[0], 최종[-1]])
    고지 = [r for r in _공격(final)["risks"] if PAYMENT_CONFLICT_NOTE in r]
    assert len(고지) == 1
    assert 최종[-1] in 고지[0]
    assert 버린_것만[0] not in 고지[0], "버린 3회차의 지급일로 현금 겹침을 고지했다"


def test_일괄로_돌아오면_집중일_겹침도_일괄_지급일로_본다() -> None:
    """🔴 되돌린 안의 겹침 고지는 **일괄 지급일 하나**(``date + N5``)로만 선다."""
    일괄_지급일 = (AS_OF + timedelta(days=N5)).isoformat()
    넓힌_분할_지급일 = (AS_OF + timedelta(days=8 + N5)).isoformat()  # 3회차 마지막 회차
    _, final = _최종(_일괄_복귀_창(), critical=[일괄_지급일, 넓힌_분할_지급일])
    고지 = [r for r in _공격(final)["risks"] if PAYMENT_CONFLICT_NOTE in r]
    assert len(고지) == 1 and 일괄_지급일 in 고지[0]
    assert 넓힌_분할_지급일 not in 고지[0]


def test_N5_미확정이면_지급_일정을_만들지_않고_보류를_알린다() -> None:
    """🟢 **기존 보류 동작 유지** (규칙 3) — 회차 수가 바뀐 날에도 0 으로 채우지 않는다."""
    state, final = _최종(_되돌아오는_창(), n5=None)
    공격 = _공격(final)
    assert len(공격["split_plan"]) == 2
    assert "payment_schedule" not in 공격
    assert check_payment_schedule(공격, state) is None
    assert any("지급 소요일이 미확정" in r for r in 공격["risks"])
