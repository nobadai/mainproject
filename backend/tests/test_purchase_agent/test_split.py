"""E3-3 검사 — ④ split_plan 조건부 진입 + 유형 선택 (백로그 E3-3 · 상세설계 §4-④).

백로그 E3-3 DoD: **"임계 초과 or 상승 궤적에서만 진입"**.

🔴 **임계가 선언에서 물류 값으로 바뀌었다** (`#308` · 2026-09-12). 수량 가지는 이제
`cap_by_date[as_of + N4]` — **그날 창고에 들어갈 자리**와 견준다.

⚠️ **수량 가지는 mock에서 한 번도 서지 않는다.** 전에는 «8,727kg < 임계 20,000kg» 이라
안 섰고, 지금은 **mock 에 N4 도 `cap_by_date` 도 없어 판정 자체를 안 한다** (규칙 3 —
미결을 0으로 채우면 모든 날 열린다). 두 시절 다 mock만 돌리면 `by_volume`을 지워도 초록불이
뜬다. 그래서 수량 가지는 **`inject_arrival_cap` 으로 따로** 시험한다.

3품목 × 4앵커 전횡단을 기본으로 깐다 — E3-1에서 배추만 돌려 양파 크래시를 놓친 교훈이다.
"""

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest
from _injection import drop_holdings, inject_arrival_cap

from app.purchase_agent import mocks
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import (
    arrival_dates,
    cap_constrained_quantities,
    materialize_split,
    package_scenarios,
    split_infeasible_reason,
    split_offsets,
    with_round_amounts,
)
from app.purchase_agent.nodes.self_check import (
    check_axis_diversity,
    check_payment_schedule,
    check_quadruple_match,
    check_split_amounts,
    check_split_dates,
    self_check,
)
from app.purchase_agent.nodes.split_plan import (
    choose_rounds,
    effective_allowed_axes,
    equal_ratios,
    evaluate_split_entry,
    largest_total_kg,
    split_decision,
    split_plan,
)
from app.purchase_agent.schemas import TIMING_AXIS, PurchaseProposal
from app.purchase_agent.state import build_initial_state
from tests.test_purchase_agent._ast_helpers import references

RISING = date(2026, 8, 21)
FALLING = date(2026, 8, 28)
UNCERTAIN = date(2026, 9, 4)
SPREAD_WIDE = date(2026, 9, 11)
ANCHORS = (RISING, FALLING, UNCERTAIN, SPREAD_WIDE)

ITEM = "배추"

#: ㉣ 검사가 훑는 자리 — 옛 고정 임계가 코드로 되돌아오면 운다.
_PACKAGE = Path(mocks.__file__).resolve().parent.parent
_NODES = _PACKAGE / "nodes"


def _staged(item: str = ITEM, as_of: date = RISING) -> dict:
    """③까지 돌린 상태 — ④가 ``base_plan``의 안별 총량을 보므로 ③이 선행해야 한다."""
    state = build_initial_state(item, as_of)
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    return state


def _flatten_trend(state: dict) -> None:
    """지속 상승 궤적을 깬다 — 궤적 가지를 끄고 수량 가지만 남길 때 쓴다."""
    daily = deepcopy(state["forecast"]["daily"])
    daily[1]["predicted"] = daily[0]["predicted"]  # 단조 증가가 아니게 된다
    state["forecast"] = {**state["forecast"], "daily": daily}


@pytest.fixture(scope="module")
def proposals() -> dict[date, dict]:
    """앵커일별 제안.

    🔴 **보유는 뗀다** (``drop_holdings``) — 이 파일의 검사 중 보유를 재는 것이 없다.
      켜 두면 mock 앵커의 보수안이 «필요 없다» 로 사라져(§4-③) 등급·분할 단언이
      **보유 때문에** 무너진다. 보유 자체는 ``test_holdings_deduction`` 이 잰다.
    """
    with pytest.MonkeyPatch.context() as mp:
        drop_holdings(mp)
        return {as_of: run_purchase_agent(ITEM, as_of) for as_of in ANCHORS}


# ── E3-3 DoD: 진입 조건 ─────────────────────────────────────────────────────


def test_rising_day_splits_only_the_timing_scenario(proposals: dict) -> None:
    """8/21 공격안(D=12, 8,727kg)에 분할이 뜬다. **보수·기본은 단일 회차다.**

    전 안에 걸면 세 안의 split 구조가 같아져 timing 축이 라벨로만 남는다
    (§4-④ E3-3 확정 1 · §3.5.1-3의 분할 판).
    """
    by_label = {s["label"]: s for s in proposals[RISING]["scenarios"]}
    assert by_label["공격"]["strategy_type"] == TIMING_AXIS
    assert len(by_label["공격"]["split_plan"]) == 2
    assert len(by_label["보수"]["split_plan"]) == 1
    assert len(by_label["기본"]["split_plan"]) == 1


def test_days_without_the_timing_axis_never_split(proposals: dict) -> None:
    """하락·불확실 날엔 timing 축이 없어 ④가 진입하지 않는다."""
    for as_of in (FALLING, UNCERTAIN):
        assert TIMING_AXIS not in proposals[as_of]["scenarios"][0]["strategy_type"]
        for scenario in proposals[as_of]["scenarios"]:
            assert len(scenario["split_plan"]) == 1


def test_entry_is_driven_by_trend_not_volume_in_the_mocks() -> None:
    """**mock에서는 수량 가지가 한 번도 서지 않는다** — 이 사실 자체를 잠근다.

    🔴 **이유가 바뀌었다** (`#308`). 전에는 «8,727kg < 임계 20,000kg» 이라 **미달**이었고,
    지금은 mock 에 N4 도 ``cap_by_date`` 도 없어 **판정을 안 한다.** 둘은 다른 사실이라
    ``cap_unknown_reason`` 까지 같이 잠근다 — 미달로 읽히면 규칙 3이 깨진 것을 못 본다.
    """
    constraints = load_constraints()
    for as_of in (RISING, SPREAD_WIDE):
        decision = evaluate_split_entry(_staged(as_of=as_of), constraints)
        assert decision["entered"] is True
        assert decision["by_volume"] is False  # ← 수량 가지는 죽어 있다
        assert decision["cap_unknown_reason"]  # ← 미달이 아니라 **못 봤다**
        assert decision["cap_kg"] is None
        assert decision["by_trend"] is True


def test_volume_trigger_enters_on_its_own_without_any_trend() -> None:
    """**합성 입력** — 궤적을 죽이고 총량을 도착일 여유까지 올리면 수량 단독으로 진입한다.

    임계가 **검사가 주입한 물류 값**이라, 선언을 안 건드리고도 경계 양쪽을 잴 수 있다.
    """
    constraints = load_constraints()
    state = _staged()
    _flatten_trend(state)
    cap = 9_000
    inject_arrival_cap(state, cap)

    state["base_plan"]["drafts"][-1]["total_qty_kg"] = cap
    decision = evaluate_split_entry(state, constraints)
    assert decision["by_trend"] is False
    assert decision["by_volume"] is True
    assert decision["cap_kg"] == cap
    assert decision["entered"] is True

    state["base_plan"]["drafts"][-1]["total_qty_kg"] = cap - 1  # 경계 바로 아래
    blocked = evaluate_split_entry(state, constraints)
    assert blocked["by_volume"] is False
    assert blocked["entered"] is False


def test_the_volume_branch_never_opens_on_an_unknown_arrival_cap() -> None:
    """🔴 **모르면 안 연다** (규칙 3 · `#308`). 여유를 0으로 채우면 **모든 날** 열린다.

    ``cap_by_date`` 를 못 받은 날 ``0`` 으로 읽으면 ``총량 ≥ 0`` 이 늘 참이라 수량 가지가
    매일 선다 — 미결이 판정을 막는 게 아니라 **판정을 만드는** 상태다. 그래서 총량을
    아무리 키워도 안 열리는 것을 잠근다.
    """
    constraints = load_constraints()
    state = _staged()
    _flatten_trend(state)
    inject_arrival_cap(state, None)  # N4 는 있고 그날 여유만 없다
    state["base_plan"]["drafts"][-1]["total_qty_kg"] = 10**9

    decision = evaluate_split_entry(state, constraints)
    assert decision["by_volume"] is False
    assert decision["entered"] is False
    assert decision["arrival_date"]  # 도착일은 계산됐다 — 없는 것은 그날 여유뿐이다
    assert "여유" in decision["cap_unknown_reason"]


def test_trend_trigger_enters_on_its_own_below_the_arrival_cap() -> None:
    """궤적 가지 단독 진입 — mock의 실제 경로다. 궤적을 꺾으면 닫힌다."""
    constraints = load_constraints()
    state = _staged()
    roomy = largest_total_kg(state["base_plan"]) * 10  # 여유가 넉넉해 수량 가지는 안 선다
    inject_arrival_cap(state, roomy)
    assert evaluate_split_entry(state, constraints)["by_volume"] is False
    assert evaluate_split_entry(state, constraints)["entered"] is True

    _flatten_trend(state)
    assert evaluate_split_entry(state, constraints)["entered"] is False


def test_timing_axis_gates_both_triggers() -> None:
    """축이 닫히면 두 트리거가 다 서도 진입하지 않는다 (§4-④ "timing ∈ allowed_axes AND …")."""
    constraints = load_constraints()
    state = _staged()
    state["allowed_axes"] = ["quantity"]
    inject_arrival_cap(state, 9_000)
    state["base_plan"]["drafts"][-1]["total_qty_kg"] = 50_000
    decision = evaluate_split_entry(state, constraints)
    assert decision["by_volume"] and decision["by_trend"]
    assert decision["entered"] is False


# ── 회차 수 — 고정 목록에서 선택 (§4-④ "생성 말고 선택") ────────────────────


def test_rounds_come_from_the_fixed_list_and_are_clamped() -> None:
    """``clamp(ceil(총량 / 도착일 여유), 목록 경계)``. 진입 시 하한 2는 **목록에서 유도**한다."""
    constraints = load_constraints()
    cap = 20_000
    types = sorted(constraints["split"]["types"])
    smallest_split, largest_split = min(t for t in types if t > 1), max(types)

    assert choose_rounds(8_727, cap, constraints) == smallest_split  # ceil(0.44)=1 → 하한
    assert choose_rounds(cap, cap, constraints) == smallest_split
    assert choose_rounds(cap * 2 + 1, cap, constraints) == 3
    assert choose_rounds(cap * 99, cap, constraints) == largest_split  # 목록 상한을 안 넘는다


def test_a_zero_arrival_cap_means_the_largest_split_not_a_crash() -> None:
    """🔴 여유 **0** 은 나눗셈이 아니라 «아무리 나눠도 그날엔 안 들어간다» 다 (`#308`).

    ``ceil(총량 / 0)`` 은 ∞ 라 목록 최대로 클램프한다. 0으로 나누기를 피한 것이 아니라
    뜻을 옮긴 것이고, 그 안은 뒤에서 ⑦ ``check_arrival_capacity`` 가 어차피 컷한다.

    ⚠️ 관통에서 **도착일 여유 0 인 날이 1,620셀**이다 (2026-09-12 · 전부 보류일이라
    실제로 열릴 안은 없다). 드문 값이 아니라 흔한 값이다.
    """
    constraints = load_constraints()
    largest_split = max(constraints["split"]["types"])
    assert choose_rounds(1_000, 0, constraints) == largest_split


def test_rounds_fall_back_to_the_minimum_when_the_cap_was_never_seen() -> None:
    """여유를 못 봤는데 궤적으로 진입한 날 — 수량으로는 회차를 못 정하니 **하한**이다."""
    constraints = load_constraints()
    smallest_split = min(t for t in sorted(constraints["split"]["types"]) if t > 1)
    assert choose_rounds(10**9, None, constraints) == smallest_split


def test_equal_ratios_sum_to_one_exactly_enough() -> None:
    """마지막을 ``1 − Σ앞``으로 구성해 ⑥의 합계 검사(1e-9)를 통과한다."""
    for rounds in (2, 3):
        ratios = equal_ratios(rounds)
        assert len(ratios) == rounds
        assert all(ratio > 0 for ratio in ratios)
        assert abs(sum(ratios) - 1.0) <= 1e-9


# ── 회차 날짜 — ⑥이 안별 D로 만든다 ────────────────────────────────────────


def test_offsets_start_at_as_of_and_stay_inside_the_coverage_window() -> None:
    """1회차는 as_of(0), 나머지는 커버 구간 안에서 앞으로만 간다."""
    assert split_offsets(12, 2) == [0, 6]
    assert split_offsets(12, 3) == [0, 4, 8]
    for coverage in range(2, 19):
        for rounds in (2, 3):
            if rounds > coverage:
                continue
            offsets = split_offsets(coverage, rounds)
            assert offsets[0] == 0
            assert offsets == sorted(set(offsets))  # 중복도 역행도 없다
            assert offsets[-1] < coverage


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_split_dates_pass_the_self_check(as_of: date, proposals: dict) -> None:
    """1회차 = as_of · seq 연속 · **날짜 단조증가** (IO명세 §2)."""
    for scenario in proposals[as_of]["scenarios"]:
        assert check_split_dates(scenario, as_of.isoformat()) is None


def test_self_check_rejects_repeated_or_backwards_round_dates() -> None:
    """"2분할인데 같은 날 두 번"은 분할이 아니다 — 회차가 하나뿐일 땐 없던 구멍이다.

    사중 일치는 멀쩡히 통과하므로 날짜를 따로 보지 않으면 잡히지 않는다.
    """
    as_of = "2026-08-21"
    same_day = {
        "split_plan": [
            {"seq": 1, "date": as_of, "qty_kg": 5},
            {"seq": 2, "date": as_of, "qty_kg": 5},
        ]
    }
    assert "앞으로 가지 않음" in check_split_dates(same_day, as_of)

    backwards = deepcopy(same_day)
    backwards["split_plan"][1]["date"] = "2026-08-20"
    assert check_split_dates(backwards, as_of) is not None

    forwards = deepcopy(same_day)
    forwards["split_plan"][1]["date"] = "2026-08-27"
    assert check_split_dates(forwards, as_of) is None


def test_schema_backstops_the_date_order(proposals: dict) -> None:
    """⑦이 컷하는 것과 별개로 스키마가 출력 경계에서 한 번 더 막는다."""
    broken = deepcopy(proposals[RISING])
    scenario = next(s for s in broken["scenarios"] if len(s["split_plan"]) == 2)
    scenario["split_plan"][1]["date"] = scenario["split_plan"][0]["date"]
    with pytest.raises(ValueError, match="strictly increase"):
        PurchaseProposal.model_validate(broken)


# ── 사중 일치 — 회차가 둘이 되며 Σ split 축이 실제로 검증되기 시작한다 ──────


@pytest.mark.parametrize("item", mocks.ITEMS)
@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_every_item_and_anchor_stays_consistent(
    item: str, as_of: date, no_holdings: None
) -> None:
    """전 품목 × 4앵커 전횡단 — 품목 하나만 도는 테스트는 E3-1에서 크래시를 놓쳤다.

    🔴 **목록을 여기 적지 않고 ``mocks.ITEMS`` 를 읽는다.** 적어 두면 mock 이 좁아진
    날 이 줄만 남아 없는 품목을 돌린다 — 실제로 그렇게 깨졌다 (아래 ``ALL_ITEMS`` 주석).

    🟡 **보유는 이 검사의 대상이 아니다** — 안이 서 있어야 횡단이 성립한다.
    """
    proposal = run_purchase_agent(item, as_of)
    PurchaseProposal.model_validate(proposal)
    assert proposal["scenarios"]
    for scenario in proposal["scenarios"]:
        assert check_quadruple_match(scenario) is None
        assert check_split_dates(scenario, as_of.isoformat()) is None
        for round_ in scenario["split_plan"]:
            assert round_["qty_kg"] > 0
        assert len(scenario["split_plan"]) <= scenario["coverage_days"]


def test_rounding_remainder_lands_on_the_last_round(proposals: dict) -> None:
    """균등 분할의 나머지를 마지막 회차가 흡수한다 — 산술이 정하는 값이라 정확히 대조한다."""
    aggressive = next(s for s in proposals[RISING]["scenarios"] if s["label"] == "공격")
    quantities = [round_["qty_kg"] for round_ in aggressive["split_plan"]]
    assert quantities == [4364, 4363]
    assert sum(quantities) == aggressive["total_qty_kg"] == 8727


# ── 일괄 전환(fallback) — 라벨과 행동의 불일치를 드러낸다 ───────────────────


def test_infeasible_split_reports_why() -> None:
    """감당 못 하는 조건 둘: 0kg 회차, 겹치는 날짜."""
    two = [{"ratio": 0.5}, {"ratio": 0.5}]
    assert split_infeasible_reason(8727, two, 12) is None
    assert "최소 수량 미달" in split_infeasible_reason(1, two, 12)
    assert "회차 수" in split_infeasible_reason(8727, two, 1)

    three = [{"ratio": 1 / 3}, {"ratio": 1 / 3}, {"ratio": 1 / 3}]
    assert "회차 수" in split_infeasible_reason(8727, three, 2)  # 보수안(D=2)엔 3분할 불가


def test_materialize_split_falls_back_to_a_single_round() -> None:
    """감당 못 하면 단일 회차로 되돌린다 — 0kg 회차는 스키마가 **제안 전체**를 죽인다."""
    two = [{"ratio": 0.5}, {"ratio": 0.5}]
    assert len(materialize_split("2026-08-21", 1, two, 12)) == 1
    assert len(materialize_split("2026-08-21", 8727, two, 12)) == 2
    assert materialize_split("2026-08-21", 8727, None, 12)[0]["qty_kg"] == 8727


def test_fallback_is_disclosed_in_risks() -> None:
    """timing 라벨인데 회차가 하나면 **그 사실을 적는다** — 조용히 넘기면 라벨/행동 불일치를
    소비자가 추적할 수 없다 (§4-④ E3-3 확정 4)."""
    state = _staged()
    state["base_plan"]["drafts"][-1]["total_qty_kg"] = 1  # 공격안을 1kg으로 — 2분할 불가
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    result = package_scenarios(state)

    aggressive = next(s for s in result["scenarios_final"] if s["label"] == "공격")
    assert aggressive["strategy_type"] == TIMING_AXIS
    assert len(aggressive["split_plan"]) == 1
    assert any("일괄 전환" in risk for risk in aggressive["risks"])
    assert any("최소 수량 미달" in risk for risk in aggressive["risks"])


# ── 규칙 3: 미결로 건너뛴 검사가 드러난다 ───────────────────────────────────


def test_round_level_arrival_check_is_disclosed_as_deferred(proposals: dict) -> None:
    """§5.5 회차별 창고 여유 검사를 **하지 않았다**는 사실이 risks에 실린다.

    "총량 기준 단일 도착일로 뭉치면 분할의 창고 부담 분산 효과가 사라진다"가 §5.5의 요지라,
    안 했다는 사실을 적지 않으면 분할이 그 효과를 검증받은 것처럼 읽힌다.

    mock 경로는 날짜별 입고 여유도 입고 소요일도 없다. 먼저 걸리는 쪽(여유 미수신)이
    사유로 나간다 — 둘 다 적으면 무엇을 먼저 받아야 하는지가 흐려진다.
    """
    assert load_constraints()["pending"]["inbound_lead_days"] is None
    aggressive = next(s for s in proposals[RISING]["scenarios"] if s["label"] == "공격")
    assert any(
        "회차별 창고 여유 검사를 하지 않았다" in risk and "받지 못했다" in risk
        for risk in aggressive["risks"]
    )

    conservative = next(s for s in proposals[RISING]["scenarios"] if s["label"] == "보수")
    assert not any("cap_by_date" in risk for risk in conservative["risks"])


def _forced_state(
    as_of: date, orders_kg: int, warehouse_kg: int, cash: int, cap_kg: float | None = None
) -> dict:
    """하드 제약을 직접 흔들어 ⑥까지 돌린 **상태 전체**.

    mock으로는 안 나오는 조합을 시험할 때 쓴다.

    ``cap_kg`` 는 분할 진입이 보는 **도착일 여유**다 (`#308`). 안 주면 mock 그대로 미결이라
    수량 가지가 판정되지 않는다 — 궤적으로만 진입하는 날을 재는 검사는 안 줘도 된다.
    """
    state = build_initial_state(ITEM, as_of)
    state["confirmed_orders"] = {**state["confirmed_orders"], "total_kg": orders_kg}
    state["inventory"] = {
        **state["inventory"],
        "warehouse_free_kg": warehouse_kg,
        "rental_cap_kg": 0,
    }
    state["projected_cash_min"] = cash
    if cap_kg is not None:
        inject_arrival_cap(state, cap_kg)
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    return state


def _forced(
    as_of: date, orders_kg: int, warehouse_kg: int, cash: int, cap_kg: float | None = None
) -> dict:
    """위 상태에서 공격안 하나를 꺼낸다."""
    state = _forced_state(as_of, orders_kg, warehouse_kg, cash, cap_kg)
    return next(s for s in state["scenarios_final"] if s["label"] == "공격")


def test_split_plan_is_never_none_so_the_decision_always_travels() -> None:
    """일괄도 **1회차 비율 목록**으로 낸다 — ``None``이면 미진입 사유가 ⑥까지 못 간다."""
    for as_of in ANCHORS:
        chosen = split_plan(_staged(as_of=as_of))["split_plan"]
        assert chosen is not None
        assert "decision" in chosen[0]
        assert abs(sum(line["ratio"] for line in chosen) - 1.0) <= 1e-9


def test_a_timing_axis_that_never_splits_is_withdrawn_not_labelled() -> None:
    """①이 **클립 전** 추정으로 축을 열고 ④가 **클립 후** 총량으로 닫는 경우.

    §4-④ E3-3 확정 2가 "정상"이라 한 상황이다. 🔴 **처방이 바뀌었다** (`#308`).

    ```text
    전   timing 라벨을 그대로 달고 회차 하나 → 불일치를 **고지**했다
    후   그 안의 축에서 timing 을 **걷는다** → 불일치 자체가 없다
    ```

    ⚠️ **고지는 그대로 남는다.** 출력 ``allowed_axes`` 에는 여전히 분할 축이 들어 있어서,
      아무 안도 그 축을 안 쓴 이유가 없으면 되물을 자리가 사라진다.
    """
    # 확정주문 30,000kg → ①의 추정 총량 25,714kg이 도착일 여유 10,000kg을 넘어 timing이
    # 열린다. 창고 5,000kg이 안별 총량을 깎아 ④의 판정에서는 여유 미만이 된다.
    # 예측은 하락이라 궤적 가지도 서지 않는다.
    aggressive = _forced(
        FALLING, orders_kg=30_000, warehouse_kg=5_000, cash=10**12, cap_kg=10_000
    )
    assert aggressive["strategy_type"] != TIMING_AXIS  # ← 걷혔다
    assert len(aggressive["split_plan"]) == 1
    note = next(risk for risk in aggressive["risks"] if "분할 축이 열렸지만" in risk)
    assert "도착 여유 10,000kg" in note
    assert "지속 상승 궤적 아님" in note


def test_a_day_without_an_arrival_cap_says_so_instead_of_going_quiet() -> None:
    """🟡 **「못 봤다」를 고지로 남긴다 — 「안 걸렸다」와 가른다** (`#308` · 규칙 3).

    판정을 안 한 날은 축이 안 열리는데, 아무 말도 안 하면 화면에서는 «여유가 넉넉해서
    안 열렸다» 와 구별이 안 된다. 둘은 고칠 것이 다르다 — 앞은 물류 배선이고 뒤는 정상이다.

    ⚠️ **N4 미결은 여기서 안 센다.** 그쪽은 이미 제 문장이 있고, 한 원인을 두 줄로 내면
      읽는 사람이 둘로 센다.
    """
    state = build_initial_state(ITEM, RISING)
    state.update(classify_situation(state))
    inject_arrival_cap(state, None)  # N4 는 있고 그날 여유만 없다
    notes = draft_plan(state)["base_plan"]["deferred_checks"]

    note = next(n for n in notes if "분할 진입" in n)
    assert "여유를 0으로 가정하지 않았다" in note
    assert "입고 소요일" not in note, "N4 는 제 문장이 따로 있다"


def test_the_notice_is_absent_when_the_cap_was_actually_read() -> None:
    """반대 방향 — **본 날은 아무 말도 안 한다.** 늘 붙으면 고지가 배경이 된다."""
    state = build_initial_state(ITEM, RISING)
    state.update(classify_situation(state))
    inject_arrival_cap(state, 9_000)
    notes = draft_plan(state)["base_plan"]["deferred_checks"]

    assert not [n for n in notes if "분할 진입" in n]


def test_the_old_fixed_threshold_is_gone_from_both_the_declaration_and_the_code() -> None:
    """㉣ **선언과 코드 양쪽에서 걷혔다** (`#308`).

    🔴 값을 남겨 두면 기준이 둘이 된다. `#379` 가 그 모양이었다 — 선언은 남고 읽는 코드가
    0곳이라, 같은 사실이 두 곳에 적힌 채 한쪽만 바뀌기를 기다리는 상태였다.

    ★ ``references`` 는 **코드가 쓰는 문자열만** 본다. `constraints.yaml` 의 묘비 주석과
      이 파일의 설명은 안 세므로, *"왜 걷었나"* 를 적어 두는 것과 충돌하지 않는다.
    """
    assert "split_entry_qty_kg" not in load_constraints()["triggers"]
    for name in ("classify_situation.py", "split_plan.py", "draft_plan.py", "package_scenarios.py"):
        assert not references(_NODES / name, "split_entry_qty_kg"), name
    assert not references(_PACKAGE / "adapter.py", "split_entry_qty_kg")


def test_the_withdrawn_axis_does_not_make_self_check_reject_the_whole_day() -> None:
    """🔴 **⑥만 고치면 ⑦이 그날 안을 통째로 죽인다** (`#308`). 그 짝을 잠근다.

    ④가 안 나눠 ⑥이 timing 을 걷으면 전 안이 ``quantity`` 가 된다. ⑦
    ``check_axis_diversity`` 는 *"허용 축이 둘 이상인데 전 안 동일 축"* 을 반려하므로,
    ①이 연 목록을 그대로 넘기면 **살아 있던 안이 전부 사라진다** — 관통 실측 21셀
    (2026-09-12 · 안이 있는 672셀 기준).

    ★ 그래서 **아래 두 줄이 이 검사의 핵심**이다. 같은 안 목록에 두 축 목록을 각각
      먹여, 안 걷은 쪽은 반려 문장이 나오고 걷은 쪽은 ``None`` 인 것을 나란히 본다 —
      «⑦이 실효 축을 안 보면 죽는다» 를 값으로 보인다.
    """
    state = _forced_state(FALLING, orders_kg=30_000, warehouse_kg=5_000, cash=10**12, cap_kg=10_000)
    assert TIMING_AXIS in state["allowed_axes"], "전제 — ①이 총량으로 축을 열었다"
    assert not split_decision(state["split_plan"])["entered"], "전제 — ④는 안 나눴다"

    survivors = self_check(state)["scenarios_final"]
    assert survivors, "실효 축을 안 보면 이 자리에서 그날 안이 전부 사라진다"
    assert {s["strategy_type"] for s in survivors} == {"quantity"}

    assert check_axis_diversity(survivors, state["allowed_axes"]), "①의 목록이면 반려된다"
    effective = effective_allowed_axes(state["allowed_axes"], state["split_plan"])
    assert check_axis_diversity(survivors, effective) is None, "실효 축이면 통과한다"


def test_effective_axes_keep_timing_when_the_split_actually_happened() -> None:
    """반대 방향 — **나눈 날은 안 걷는다.** 늘 걷으면 timing 축이 영영 안 선다."""
    state = _forced_state(RISING, orders_kg=60_000, warehouse_kg=10**9, cash=10**12, cap_kg=20_000)
    assert split_decision(state["split_plan"])["entered"] is True
    assert effective_allowed_axes(state["allowed_axes"], state["split_plan"]) == (
        state["allowed_axes"]
    )
    assert TIMING_AXIS in {s["strategy_type"] for s in state["scenarios_final"]}


def test_volume_only_entry_cites_the_arrival_cap_not_the_forecast() -> None:
    """수량 단독 진입의 근거는 **판정을 가른 수**를 가리킨다 — 이제 물류 도착일 여유다.

    🔴 **출처가 「주문」에서 「재고」로 옮겼다** (`#308`). 전에는 우리 선언(고정 임계)이
    기준이라 총량 쪽 ref(``SO-``)가 맞았다. 지금 기준은 물류가 낸 값이라 그대로 두면
    **결정한 수를 못 찾는 근거**가 된다 — Codex 교차검증 P2 와 같은 자리다.

    등급은 **낮은 쪽**이다: 여유는 물류 정본이지만 비교 대상인 총량이 수요 파생값이라
    ``ASSUMED`` (IO명세 §5 "수요에서 파생된 것은 SIM_FIXED 자격을 잃는다").
    """
    aggressive = _forced(
        FALLING, orders_kg=60_000, warehouse_kg=10**9, cash=10**12, cap_kg=20_000
    )
    assert len(aggressive["split_plan"]) > 1
    items = [r for r in aggressive["rationale"] if "분할" in r["claim"]]
    assert len(items) == 1
    assert items[0]["source"] == "재고"
    assert items[0]["ref_id"].startswith("CAP-")
    assert items[0]["evidence_grade"] == "ASSUMED"
    assert "도착 여유 20,000kg" in items[0]["claim"]
    assert not any(r["ref_id"].startswith("FC-") for r in items)


def test_both_triggers_produce_one_rationale_each() -> None:
    """수량·궤적이 둘 다 서면 근거도 둘이다 — 출처가 다르니 한 건으로 뭉치지 않는다."""
    aggressive = _forced(
        RISING, orders_kg=60_000, warehouse_kg=10**9, cash=10**12, cap_kg=20_000
    )
    items = [r for r in aggressive["rationale"] if "분할" in r["claim"]]
    assert {item["source"] for item in items} == {"재고", "예측"}


def test_split_carries_its_own_rationale(proposals: dict) -> None:
    """분할한 안엔 왜 나눴는지가 근거에 남는다 — ref_id 포함 (규칙 4)."""
    aggressive = next(s for s in proposals[RISING]["scenarios"] if s["label"] == "공격")
    items = [r for r in aggressive["rationale"] if "분할" in r["claim"]]
    assert len(items) == 1
    assert "지속 상승 궤적" in items[0]["claim"]
    assert items[0]["ref_id"].strip()

    conservative = next(s for s in proposals[RISING]["scenarios"] if s["label"] == "보수")
    assert not [r for r in conservative["rationale"] if "분할" in r["claim"]]


# ── #58 회차별 도착일 수용량 ────────────────────────────────────────────────
#
# ⚠️ **mock 재고에는 ``cap_by_date``도 ``inbound_lead_days``도 없다.** 물류 어댑터
# 경로에서만 오는 값이라, 기존 픽스처로는 아래 경로가 한 번도 안 밟힌다. 전부 합성
# 입력으로 시험한다 — E3-3의 수량 트리거와 같은 상황이다.

AS_OF = "2025-12-31"


def test_arrival_dates_are_purchase_dates_plus_n4() -> None:
    """도착일 = 회차일 + N4. 회차일은 ``split_offsets``를 **재사용**한다."""
    assert arrival_dates(AS_OF, 12, 2, 2) == ["2026-01-02", "2026-01-08"]
    # 오프셋 0·6 (= split_offsets(12, 2)) 에 N4 2를 더한 값이다
    assert split_offsets(12, 2) == [0, 6]


def test_arrival_dates_are_none_when_n4_is_missing() -> None:
    """N4가 없으면 **계산하지 않는다**. 0으로 채우면 "오늘 승인분이 오늘 도착"이 된다."""
    assert arrival_dates(AS_OF, 12, 2, None) is None


def test_arrival_dates_accept_zero_lead_as_a_real_value() -> None:
    """0은 미결이 아니라 "당일 도착"이라는 확정값이다 (규칙 3)."""
    assert arrival_dates(AS_OF, 12, 2, 0) == ["2025-12-31", "2026-01-06"]


# ── 4갈래 폴백 ─────────────────────────────────────────────────────────────


def test_fallback_when_logistics_sent_no_cap() -> None:
    quantities, note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], None)
    assert quantities == [50, 50]
    assert "날짜별 입고 여유를 받지 못했다" in note


def test_fallback_when_n4_is_missing() -> None:
    quantities, note = cap_constrained_quantities([50, 50], None, {"2026-01-02": 10})
    assert quantities == [50, 50]
    assert "입고 소요일이 정해지지 않아" in note


def test_fallback_when_an_arrival_falls_outside_the_query_window() -> None:
    """🔴 **이 작업의 급소.** 창 밖은 0이 아니라 "안 봤다"다.

    물류는 ``as_of + N4``부터 18일만 계산해 보낸다. 창 밖 날짜를 ``.get(d, 0)``으로
    읽으면 그 회차가 **수용량 0**이 되어 통째로 죽는다.
    """
    cap = {"2026-01-02": 10_000}  # 2회차 도착일(01-08)은 창 밖
    quantities, note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], cap)
    assert quantities == [50, 50], "창 밖 회차를 0으로 눌러 죽이면 안 된다"
    assert "2026-01-08" in note
    assert "받지 못했다" in note


def test_zero_capacity_inside_the_window_is_distinguished_from_a_missing_date() -> None:
    """**있는 0**과 **없는 값**은 다르다 — 사유 문구가 갈린다.

    다만 결과는 둘 다 균등 유지다. 0kg 회차는 ``SplitPlanItem.qty_kg > 0``이라
    제안 전체를 죽이므로 재배분을 포기한다.
    """
    zero = {"2026-01-02": 0, "2026-01-08": 100}
    _, zero_note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], zero)
    _, missing_note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], {})
    assert "물량이 0인 회차" in zero_note, "0은 값이 있는 것이라 미수신으로 뭉뚱그리지 않는다"
    assert "받지 못했다" in missing_note


def test_a_round_never_comes_out_zero_or_negative() -> None:
    """🔴 재배분이 **최소 수량 검사를 사후에 깬다.**

    ``split_infeasible_reason``은 균등 분할 수량으로 검사를 마쳤는데, 그 뒤 재배분이
    0kg·음수 회차를 새로 만들 수 있다. 총합은 맞으므로 **사중 일치는 통과하고**
    스키마 검증에서야 제안 전체가 터진다 — ``_validate_ratios``가 막는 것과 같은
    "조용히 지나가는 구간"이다.
    """
    # 음수 수용량이 섞이면 앞 회차가 음수가 되고 뒤 회차가 그만큼 부푼다
    mixed = {"2026-01-02": -30, "2026-01-08": 1000}
    quantities, note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], mixed)
    assert quantities == [50, 50]
    assert "지킬 수 없다" in note
    assert all(quantity > 0 for quantity in quantities)

    # 창 안의 0도 같은 이유로 되돌린다
    zeroed = {"2026-01-02": 0, "2026-01-08": 100}
    quantities, _ = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], zeroed)
    assert all(quantity > 0 for quantity in quantities)


def test_fallback_when_total_does_not_fit_under_the_caps() -> None:
    """모든 회차가 1kg 이상인데도 총량이 안 들어가는 경우.

    누적이라 2회차 여유는 40 − 30 = 10 뿐이고, 60kg 이 남는다.
    """
    cap = {"2026-01-02": 30, "2026-01-08": 40}
    quantities, note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], cap)
    assert quantities == [50, 50], "총량을 줄이면 사중 일치가 깨진다"
    assert "지킬 수 없다" in note and "넣을 자리가 없다" in note


# ── 재배분 ─────────────────────────────────────────────────────────────────


def test_overflow_is_pushed_to_the_later_round() -> None:
    # 2회차 상한은 **누적 기준**이다 — 1회차 30kg이 아직 창고에 있으므로
    # 100 안에서 70만 더 들어간다.
    cap = {"2026-01-02": 30, "2026-01-08": 100}
    quantities, note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], cap)
    assert quantities == [30, 70]
    assert sum(quantities) == 100, "총합 불변"
    assert note and "맞춰 옮겼다" in note


def test_no_note_when_equal_split_already_fits() -> None:
    cap = {"2026-01-02": 100, "2026-01-08": 100}
    quantities, note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], cap)
    assert quantities == [50, 50]
    assert note is None


def test_capacity_is_floored_not_rounded() -> None:
    """수용량은 상한이라 내림한다 — 올림하면 못 넣는 양을 계획하게 된다."""
    cap = {"2026-01-02": 30.9, "2026-01-08": 100}
    quantities, _ = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], cap)
    assert quantities == [30, 70]  # 30.9 → 30 (내림) · 2회차는 100 − 30 = 70


def test_iso_string_keys_are_what_logistics_sends() -> None:
    """물류는 ``d.isoformat()``으로 직렬화해 보낸다 (`logistics/adapter.py` 의 `cap_by_date` 조립).

    ``date`` 객체를 키로 조회하면 **전부 미스**가 되어, 수용량이 와 있는데도
    "창 밖"으로 빠진다.
    """
    iso_cap = {"2026-01-02": 30, "2026-01-08": 100}
    date_cap = {date(2026, 1, 2): 30, date(2026, 1, 8): 100}  # 같은 값, 키 타입만 다름
    arrivals = arrival_dates(AS_OF, 12, 2, 2)

    assert cap_constrained_quantities([50, 50], arrivals, iso_cap)[0] == [30, 70]
    # 같은 값인데 키 타입만 다르면 조정이 일어나지 않는다 — 그 사실이 고지로 드러난다
    quantities, note = cap_constrained_quantities([50, 50], arrivals, date_cap)  # type: ignore[arg-type]
    assert quantities == [50, 50]
    assert "받지 못했다" in note


# ── ⑥ 통합 ─────────────────────────────────────────────────────────────────


def test_materialize_split_applies_the_cap() -> None:
    chosen = [{"ratio": 0.5}, {"ratio": 0.5}]
    rounds = materialize_split(
        AS_OF,
        100,
        chosen,
        12,
        lead_days=2,
        cap_by_date={"2026-01-02": 30, "2026-01-08": 100},
    )
    assert [line["qty_kg"] for line in rounds] == [30, 70]
    assert sum(line["qty_kg"] for line in rounds) == 100
    # 매입일은 도착일이 아니다 — 회차 date 는 오프셋 그대로다
    assert [line["date"] for line in rounds] == ["2025-12-31", "2026-01-06"]


def test_materialize_split_is_unchanged_without_logistics_values() -> None:
    """부재가 **정상 경로**다 — 회귀 픽스처 전량이 이 길로 간다."""
    chosen = [{"ratio": 0.5}, {"ratio": 0.5}]
    assert materialize_split(AS_OF, 100, chosen, 12) == materialize_split(
        AS_OF, 100, chosen, 12, lead_days=None, cap_by_date=None
    )


# ── 누적 (Codex 교차검증 지적) ──────────────────────────────────────────────


def test_earlier_rounds_still_occupy_the_warehouse_on_later_dates() -> None:
    """🔴 **앞 회차가 아직 창고에 있다.**

    ``cap_by_date[d]``는 물류가 **기존 일정만** 재생해 낸 그날의 여유다
    (`logistics/tools.py` ``calculate_cap_by_date``:
    ``guaranteed_capacity_kg − projected_occupancy``). 우리가 새로 넣을 회차는
    거기 없으므로, 날짜마다 독립으로 비교하면 1회차가 남아 있는데도 2회차가
    그날 여유를 통째로 쓰는 계획이 나온다 — **총합은 맞고 하드 제약은 깨진다.**
    """
    # 독립 비교였다면 [30, 70] 이 나오고 01-08 누적이 100 > 80 으로 여유를 넘는다.
    cap = {"2026-01-02": 30, "2026-01-08": 80}
    quantities, note = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], cap)

    cumulative = [sum(quantities[: index + 1]) for index in range(len(quantities))]
    assert cumulative[-1] > cap["2026-01-08"], "이 입력은 애초에 담을 수 없다"
    assert quantities == [50, 50], "담을 수 없으면 조정하지 않고 균등을 유지한다"
    assert "지킬 수 없다" in note, "담지 못한 사실을 성공으로 고지하면 안 된다"


def test_adjusted_rounds_respect_the_cap_cumulatively() -> None:
    """조정이 성립하는 경우, **누적**이 각 날짜 여유 안에 든다."""
    cap = {"2026-01-02": 30, "2026-01-08": 1_000}
    quantities, _ = cap_constrained_quantities([50, 50], ["2026-01-02", "2026-01-08"], cap)
    for index, day in enumerate(["2026-01-02", "2026-01-08"]):
        assert sum(quantities[: index + 1]) <= cap[day], f"{day} 누적 초과"


# ── expected_arrival_date — 산출물로 나가는 도착일 (마스터 PR #138 합의) ─────


def test_each_round_carries_its_own_arrival_date() -> None:
    """회차마다 **자기 도착일**이 붙는다 — 총량 단일 도착일로 뭉치지 않는다.

    뭉치면 Critic ``E-ARRIVAL-COLLAPSE``가 *"분산 효과가 사라진다"*로 잡는다
    (`critic/critic_v0_4.py`).
    """
    rounds = materialize_split(AS_OF, 100, [{"ratio": 0.5}, {"ratio": 0.5}], 12, lead_days=2)

    assert [line["date"] for line in rounds] == ["2025-12-31", "2026-01-06"]
    assert [line["expected_arrival_date"] for line in rounds] == ["2026-01-02", "2026-01-08"]


def test_arrival_dates_are_absent_when_n4_is_undecided() -> None:
    """🔴 N4가 없으면 **전 회차 ``None``**이다. 0으로 채우지 않는다 (규칙 3).

    마스터 약정이 ``null``을 *"N4 미결로 매입도 못 냈다"*로 읽고 **자기도 계산하지
    않는다**(`master/commitment.py`). 0으로 채우면 "오늘 승인분이 오늘 도착"이 된다.
    """
    rounds = materialize_split(AS_OF, 100, [{"ratio": 0.5}, {"ratio": 0.5}], 12, lead_days=None)

    assert [line["expected_arrival_date"] for line in rounds] == [None, None]


@pytest.mark.parametrize(
    ("chosen", "why"),
    [(None, "일괄 — 진입 자체를 안 한 경로"), ([{"ratio": 0.5}, {"ratio": 0.5}], "분할 불가 전환")],
)
def test_single_round_paths_also_carry_the_arrival_date(chosen, why) -> None:
    """⚠️ ``materialize_split``의 반환 경로가 셋인데 **일괄 둘도 채워야 한다.**

    한 곳이라도 빠지면 *"회차 N건 중 도착일이 있는 것은 M건뿐"*이 되어 Critic이 운다.
    분할 불가 경로는 총량 1kg으로 만든다 — 회차당 1kg 미만이라 일괄로 되돌아간다.
    """
    total = 100 if chosen is None else 1
    rounds = materialize_split(AS_OF, total, chosen, 12, lead_days=2)

    assert len(rounds) == 1, why
    assert rounds[0]["date"] == AS_OF
    assert rounds[0]["expected_arrival_date"] == "2026-01-02"


def test_arrival_dates_are_all_or_nothing() -> None:
    """🔴 **부분 공급이 나올 수 없다.**

    마스터는 1회차만 실리고 N4도 없으면 **실린 값까지 버린다**
    (`master/commitment.py` — `lead is None`이면 일정 자체를 안 만든다).
    우리가 그 입력을 만들 수 있는지가 쟁점인데, ``_rounds``가 ``arrival_dates()``의
    결과를 통째로 쓰거나 통째로 ``None``으로 채우므로 **섞인 목록이 구조적으로 안 나온다.**
    """
    for lead in (2, None):
        rounds = materialize_split(AS_OF, 100, [{"ratio": 0.4}, {"ratio": 0.6}], 12, lead_days=lead)
        etas = [line["expected_arrival_date"] for line in rounds]
        assert all(e is None for e in etas) or all(e is not None for e in etas), etas


def test_arrival_date_follows_the_round_not_the_as_of() -> None:
    """★ 변이 방어 — ``as_of + N4``를 전 회차에 박으면 2회차가 틀린다.

    도착일은 **회차일** + N4다. ``split_offsets`` 재사용이 그것을 보장하는데, 그 재사용을
    끊고 ``as_of``로 고정해도 1회차는 우연히 맞는다 — 2회차가 유일한 증거다.
    """
    rounds = materialize_split(AS_OF, 100, [{"ratio": 0.5}, {"ratio": 0.5}], 12, lead_days=2)
    fixed = (date.fromisoformat(AS_OF) + timedelta(days=2)).isoformat()

    assert rounds[0]["expected_arrival_date"] == fixed  # 1회차는 구분이 안 된다
    assert rounds[1]["expected_arrival_date"] != fixed  # 2회차가 갈라준다
    assert rounds[1]["expected_arrival_date"] == "2026-01-08"


def test_arrival_dates_match_the_helper_used_for_cap_checks() -> None:
    """🔴 **산출물과 cap 대조가 같은 값을 본다.**

    ⑥은 ``arrival_dates()``로 회차 수량을 재배분하고(같은 함수), 그 도착일을 그대로
    싣는다. 둘이 갈라지면 "검사한 날짜"와 "약정에 적힌 날짜"가 달라진다.
    """
    rounds = materialize_split(AS_OF, 100, [{"ratio": 0.5}, {"ratio": 0.5}], 12, lead_days=2)

    assert [line["expected_arrival_date"] for line in rounds] == arrival_dates(AS_OF, 12, 2, 2)


# ── 회차 금액 (#265 · 마스터가 원장에 쓴다) ────────────────────────────────
#
# 마스터 `commitment.py:_legs` 가 `split_plan[].amount_krw` 를 읽어
# `ArrivalLeg.amount_krw` 로 옮기고, 그것이 `purchase_items.line_amount_krw` 가 된다.
# 그 값이 총액과 어긋나면 **재무 cap 검증을 통과한 안이 cap 을 넘는 원장을 만든다.**


#: 🔴 **품목 목록을 여기 적지 않는다 — ``mocks.ITEMS`` 가 정본이다.**
#:
#:   전에는 ``ALL_ITEMS = ("배추", "무", "피마늘", "양파")`` 로 적어 뒀다. mock 에서
#:   피마늘을 빼자 **이 줄만 남아 없는 품목을 돌았고 8건이 깨졌다** (2026-09-05).
#:   목록이 두 곳에 있으면 한쪽만 바뀐다 — 규칙 7 이 임계에 대해 말하는 것과 같다.
ALL_ITEMS = mocks.ITEMS


@pytest.mark.parametrize("as_of", ANCHORS)
@pytest.mark.parametrize("item", ALL_ITEMS)
def test_every_round_carries_an_amount(item: str, as_of: date, no_holdings: None) -> None:
    """🔴 **전 회차에 실린다** — 일괄이든 분할이든, 전 품목·전 앵커에서.

    마스터 `_legs` 가 `amount_filled == len(amounts)` 일 때만 금액을 나른다.
    한 회차라도 비면 그 안은 금액 변 검증이 **통째로 건너뛰어진다** — 조용히.

    🟡 **보유는 이 검사의 대상이 아니다** — 안이 서 있어야 회차를 셀 수 있다.
    """
    proposal = run_purchase_agent(item, as_of)
    assert proposal["scenarios"], f"{item}/{as_of} 에 안이 없어 검사가 공허해진다"
    for scenario in proposal["scenarios"]:
        loaded = [line.get("amount_krw") for line in scenario["split_plan"]]
        assert all(a is not None for a in loaded), f"{scenario['label']}: 빈 회차 {loaded}"


@pytest.mark.parametrize("as_of", ANCHORS)
@pytest.mark.parametrize("item", ALL_ITEMS)
def test_round_amounts_sum_to_the_scenario_total(item: str, as_of: date) -> None:
    """`Σ split_plan[].amount_krw == total_amount_krw` — 금액의 회차 변."""
    for scenario in run_purchase_agent(item, as_of)["scenarios"]:
        total = sum(line["amount_krw"] for line in scenario["split_plan"])
        assert total == scenario["total_amount_krw"], scenario["label"]


def test_a_single_round_carries_the_whole_amount() -> None:
    """일괄(1회차)이면 회차 금액 == 총액.

    ⚠️ **중복이지만 일부러 싣는다** — 근거는 `with_round_amounts` docstring 에 있다.
    빼면 마스터의 *"전 회차 실림"* 이 성립하지 않아 금액이 통째로 안 실린다.
    """
    scenario = next(
        s
        for s in run_purchase_agent(ITEM, FALLING)["scenarios"]
        if len(s["split_plan"]) == 1
    )
    assert scenario["split_plan"][0]["amount_krw"] == scenario["total_amount_krw"]


def test_a_split_scenario_splits_the_amount_too() -> None:
    """분할이면 회차마다 다른 금액이고 합은 총액이다. **총액을 회차 수로 나눈 값이 아니다.**"""
    scenario = next(
        s
        for s in run_purchase_agent(ITEM, RISING)["scenarios"]
        if len(s["split_plan"]) > 1
    )
    amounts = [line["amount_krw"] for line in scenario["split_plan"]]

    assert sum(amounts) == scenario["total_amount_krw"]
    assert len(set(amounts)) > 1, (
        "회차 금액이 전부 같다 — 총액을 회차 수로 나눈 것과 구분되지 않는다"
    )


def test_the_payment_schedule_reads_the_round_amounts() -> None:
    """🔴 `payment_schedule[i].amount_krw == split_plan[i].amount_krw`.

    같은 숫자가 두 필드에 산다. 갈라지면 **마스터는 원장에, 재무는 Cashflow 에
    다른 금액을 쓴다.**
    """
    rounds = with_round_amounts(
        materialize_split(AS_OF, 1000, [{"ratio": 0.5}, {"ratio": 0.5}], 12),
        [
            {"market": "가락", "grade": "중", "qty_kg": 600, "grade_unit_price": 1300},
            {"market": "가락", "grade": "상", "qty_kg": 400, "grade_unit_price": 1650},
        ],
    )
    scenario = {
        "label": "공격",
        "total_qty_kg": 1000,
        "total_amount_krw": sum(line["amount_krw"] for line in rounds),
        "max_price": 1800,
        "split_plan": rounds,
        "payment_schedule": [
            {
                "seq": line["seq"],
                "purchase_date": line["date"],
                "payment_date": line["date"],
                "qty_kg": line["qty_kg"],
                "amount_krw": line["amount_krw"],
                "amount_max_krw": line["qty_kg"] * 1800,
                "basis": "as_of_unit_price",
            }
            for line in rounds
        ],
    }
    state = {"purchase_payment_days": 0}

    assert check_split_amounts(scenario) is None
    assert check_payment_schedule(scenario, state) is None


# ── 변이 — 검사가 실제로 무는가 (규칙 8) ──────────────────────────────────


def test_a_shifted_round_amount_is_caught() -> None:
    """🔴 한 회차 금액을 흔들면 ⑦이 운다.

    합만 보면 못 잡는 이동이 아니라 **합 자체가 깨지는** 변이다 — 마스터가 원장에
    쓰는 값이 총액과 어긋나는 그 상태다.
    """
    scenario = {
        "total_amount_krw": 1_000_000,
        "split_plan": [
            {"seq": 1, "date": AS_OF, "qty_kg": 500, "amount_krw": 400_000},
            {"seq": 2, "date": "2026-01-06", "qty_kg": 500, "amount_krw": 600_000},
        ],
    }
    assert check_split_amounts(scenario) is None

    shifted = deepcopy(scenario)
    shifted["split_plan"][0]["amount_krw"] += 1
    reason = check_split_amounts(shifted)
    assert reason is not None and "회차 금액 합" in reason


def test_an_empty_round_amount_is_caught_by_the_node() -> None:
    """🔴 ⑦ 은 ``None`` 을 **위반으로 본다** — 스키마와 규칙이 다르다.

    스키마는 재무·물류가 ``PurchaseAgentOutput`` 으로 쓰는 공유 계약이라 *"아무 회차에도
    없음"* 을 허용한다. 이 노드는 **우리 산출물만** 보므로 그 관용이 필요 없다 —
    ⑥이 ``with_round_amounts`` 를 지났으면 전 회차에 값이 있어야 한다.

    ⚠️ 이 판이 없으면 ⑦의 빈 회차 검사를 지우는 변이가 아무도 안 잡는다 —
      우리 산출물이 늘 채워져 있어 그 경로를 지나는 검사가 없기 때문이다
      (2026-09-04 변이 실측 · 규칙 8).
    """
    scenario = {
        "total_amount_krw": 1_000_000,
        "split_plan": [
            {"seq": 1, "date": AS_OF, "qty_kg": 500, "amount_krw": 400_000},
            {"seq": 2, "date": "2026-01-06", "qty_kg": 500, "amount_krw": 600_000},
        ],
    }
    assert check_split_amounts(scenario) is None

    blank = deepcopy(scenario)
    blank["split_plan"][1]["amount_krw"] = None
    reason = check_split_amounts(blank)
    assert reason is not None and "회차 금액이 비었다" in reason and "seq 2" in reason

    missing_key = deepcopy(scenario)
    del missing_key["split_plan"][0]["amount_krw"]
    assert check_split_amounts(missing_key) is not None, "키가 없는 것도 같은 위반이다"


def test_a_round_amount_that_diverges_from_the_payment_schedule_is_caught() -> None:
    """🔴 회차 금액과 지급 계획이 갈라지면 ⑦이 운다.

    합은 그대로 두고 **한 행만** 어긋내는 변이다 — `check_split_amounts` 도
    `Σ qty` 검사도 통과한다. 이 대조가 없으면 아무도 안 잡는다.
    """
    rounds = [
        {"seq": 1, "date": AS_OF, "qty_kg": 500, "amount_krw": 400_000},
        {"seq": 2, "date": "2026-01-06", "qty_kg": 500, "amount_krw": 600_000},
    ]
    scenario = {
        "label": "공격",
        "total_qty_kg": 1000,
        "total_amount_krw": 1_000_000,
        "max_price": 1800,
        "split_plan": rounds,
        "payment_schedule": [
            {
                "seq": line["seq"],
                "purchase_date": line["date"],
                "payment_date": line["date"],
                "qty_kg": line["qty_kg"],
                "amount_krw": line["amount_krw"],
                "amount_max_krw": line["qty_kg"] * 1800,
                "basis": "as_of_unit_price",
            }
            for line in rounds
        ],
    }
    state = {"purchase_payment_days": 0}
    assert check_payment_schedule(scenario, state) is None

    diverged = deepcopy(scenario)
    diverged["payment_schedule"][0]["amount_krw"] = 399_999
    diverged["payment_schedule"][1]["amount_krw"] = 600_001  # 합은 그대로 둔다

    assert check_split_amounts(diverged) is None, "합은 안 깨졌다 — 그래서 이 대조가 필요하다"
    reason = check_payment_schedule(diverged, state)
    assert reason is not None and "금액이 분할과 다르다" in reason
