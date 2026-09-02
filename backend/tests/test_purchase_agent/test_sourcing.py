"""E3-1 검사 — ⑤ allocate_sourcing 등급 배분 스코어링 (백로그 E3-1 · 상세설계 §4-⑤).

완료 조건은 백로그 E3-1 DoD다:

* ``grade_spread_wide``(9/11)에서 ``quotes_normal`` 날 대비 **중품 비중 상승**
* §4-⑤ 판단 예시 재현 — 가까운 납품(D+3)엔 중품 가능, 먼 납품(D+8)은 상품
* 기존 4시나리오 완료 판정 불변 + **사중 일치 유지**
* 중품 소진 한계(0.6)·스프레드 임계는 ``constraints.yaml``에서

**절대값을 고정하지 않는다** (§4-⑤ Epic 3 확정 2 — "DoD 검증은 상대 비교, 가중치는 튜닝
대상"). 중품 비중 0.667 같은 숫자를 assert로 박으면 가중치를 손대는 순간 테스트가
"틀렸다"고 말하는데, 사실 틀린 게 아니라 튜닝된 것이다. 그래서 **wide > normal**과
**상한을 넘지 않음**만 잠근다. 반올림 검산처럼 산술이 결정하는 값만 정확히 대조한다.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.llm.mix import MixDecision
from app.purchase_agent.nodes.allocate_sourcing import (
    _yields_positive_kg,
    allocate_sourcing,
    baseline_spread,
    evaluate_mid_grade,
    grade_spread,
    is_spread_widened,
    mid_grade_score,
    near_term_demand_kg,
    shelf_days_block_reason,
    top_grade_shelf_days,
)
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan, warehouse_cap_kg
from app.purchase_agent.nodes.package_scenarios import package_scenarios
from app.purchase_agent.nodes.self_check import check_quadruple_match, self_check
from app.purchase_agent.nodes.split_plan import split_plan
from app.purchase_agent.schemas import PurchaseProposal
from app.purchase_agent.state import build_initial_state

RISING = date(2026, 8, 21)
FALLING = date(2026, 8, 28)
UNCERTAIN = date(2026, 9, 4)
SPREAD_WIDE = date(2026, 9, 11)
ANCHORS = (RISING, FALLING, UNCERTAIN, SPREAD_WIDE)

ITEM = "배추"


def _staged(item: str = ITEM, as_of: date = RISING) -> dict:
    """③까지 돌린 상태 — ⑤가 ``base_plan``의 안별 총량을 보므로 ③이 선행해야 한다."""
    state = build_initial_state(item, as_of)
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    return state


def _mid_share(proposal: dict, label: str = "보수") -> float:
    """안 하나의 중품 비중. 절대값이 아니라 **비교용**으로만 쓴다."""
    mid_grade = load_constraints()["grade"]["mid_grade"]
    scenario = next(s for s in proposal["scenarios"] if s["label"] == label)
    mid_kg = sum(x["qty_kg"] for x in scenario["sourcing_plan"] if x["grade"] == mid_grade)
    return mid_kg / scenario["total_qty_kg"]


@pytest.fixture(scope="module")
def proposals() -> dict[date, dict]:
    return {as_of: run_purchase_agent(ITEM, as_of) for as_of in ANCHORS}


# ── E3-1 DoD ①: 스프레드가 넓은 날 중품 비중이 오른다 ──────────────────────


def test_wide_spread_day_raises_the_mid_grade_share(proposals: dict) -> None:
    """E3-1 DoD "grade_spread_wide에서 quotes_normal 날 대비 중품 비중 상승".

    **상대 비교다.** 특·상 가격이 두 날 같고 중품만 내려갔으므로(``quotes_wide._scenario``)
    비중 차이의 원인은 스프레드 하나로 좁혀진다.
    """
    for label in ("보수", "기본", "공격"):
        assert _mid_share(proposals[SPREAD_WIDE], label) > _mid_share(proposals[RISING], label)


def test_normal_days_converge_on_the_reference_grade(proposals: dict) -> None:
    """평시엔 단가 이득이 신선도 리스크에 못 미쳐 상품으로 수렴한다 (§4-⑤ Epic 3 확정 2).

    이 날들의 출력은 ⑤가 스텁이던 때와 **완전히 같다** — 비율 0을 줄로 내보내지 않기 때문이다.
    """
    reference = load_constraints()["allocation"]["reference_grade"]
    for as_of in (RISING, FALLING, UNCERTAIN):
        for scenario in proposals[as_of]["scenarios"]:
            assert [line["grade"] for line in scenario["sourcing_plan"]] == [reference]


# ── E3-1 DoD ②: §4-⑤ 판단 예시 재현 ────────────────────────────────────────


def test_near_delivery_takes_mid_grade_but_far_delivery_does_not() -> None:
    """§4-⑤ "8/24 납품 12,000kg에는 배정 가능, 8/29 납품분은 상품으로"를 그대로 재현한다.

    as_of 8/21 · 소진 한계 6일 → +3일(8/24)은 들어오고 +8일(8/29)은 빠진다.
    """
    orders = build_initial_state(ITEM, RISING)["confirmed_orders"]
    assert [o["due_date"] for o in orders["orders"]] == ["2026-08-24", "2026-08-29"]

    within_six_days = near_term_demand_kg(orders["orders"], RISING.isoformat(), 6)
    assert within_six_days == 12000  # 8/24분만
    assert within_six_days < orders["total_kg"]  # 8/29분 6,000kg은 제외됐다
    # 한계일을 늘리면 먼 납품분도 들어온다 — 필터가 실제로 날짜를 보고 있다는 증거
    assert near_term_demand_kg(orders["orders"], RISING.isoformat(), 8) == 18000


def test_mid_grade_shelf_days_comes_from_the_top_grade_lot() -> None:
    """중품 소진 한계 6일 = 상품 한계일 10일 × 0.6 (상세설계 §7 임계표).

    "상품 한계일"의 출처는 재고 로트다 — §7이 값을 주지 않아 추론한 것이고,
    그 추론이 §4-⑤ 예시의 6일과 맞는지가 여기서 확인된다.
    """
    constraints = load_constraints()
    state = _staged()
    reference = constraints["allocation"]["reference_grade"]
    assert top_grade_shelf_days(state["inventory"], reference, ITEM) == 10
    # ⚠️ ``top_shelf * constraints[...] == 6`` 로 두면 **설정으로 산술만** 하고 노드를
    #   전혀 안 본다 — 계수를 코드에 박아도 통과한다 (규칙 8). 산출을 본다.
    assert evaluate_mid_grade(state, constraints)["shelf_days"] == 6


# ── E3-1 DoD ③: 사중 일치 — 줄이 둘이 되어야 금액 축이 실제로 검증된다 ─────


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_quadruple_match_survives_multi_grade_allocation(as_of: date, proposals: dict) -> None:
    """등급이 늘어도 수량 3축 + 금액 1축이 그대로 맞는가 (규칙 4).

    줄이 하나뿐일 땐 등급 단가가 틀려도 총액이 자동으로 맞아 금액 축이 사실상 놀았다.
    """
    for scenario in proposals[as_of]["scenarios"]:
        assert check_quadruple_match(scenario) is None
    PurchaseProposal.model_validate(proposals[as_of])


def test_rounding_remainder_is_absorbed_by_the_last_line(proposals: dict) -> None:
    """반올림 나머지를 마지막 줄이 흡수해 합이 총량과 **정확히** 같아진다.

    산술이 결정하는 값이라 여기서는 정확히 대조한다 — 가중치를 바꿔도 "합이 총량"은 불변이다.
    """
    for scenario in proposals[SPREAD_WIDE]["scenarios"]:
        lines = scenario["sourcing_plan"]
        assert len(lines) == 2
        assert sum(line["qty_kg"] for line in lines) == scenario["total_qty_kg"]
        assert scenario["total_amount_krw"] == sum(
            line["qty_kg"] * line["grade_unit_price"] for line in lines
        )


def test_mid_grade_line_comes_first_so_the_reference_grade_absorbs_the_remainder() -> None:
    """중품이 첫 줄이어야 한다 — ⑥이 **마지막 줄**에 잔량을 몰아주기 때문이다.

    중품을 끝에 두면 반올림 나머지가 신선도 상한을 넘길 수 있다.
    """
    state = _staged(as_of=SPREAD_WIDE)
    ratios = allocate_sourcing(state)["sourcing_plan"]
    # 이 테스트의 목적은 **순서**다 — 등급 이름은 리터럴로 못 박는다. 설정에서 읽는지는
    # ``test_grade_pair_is_read_from_constraints`` 가 따로 잠근다 (규칙 8).
    assert ratios[0]["grade"] == "중"
    assert ratios[-1]["grade"] == "상"


def test_grade_pair_is_read_from_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 기준등급·중품 등급을 **설정에서 읽는지**를 잠근다 (규칙 8).

    앞서 이 사실은 ``ratios[0]["grade"] == constraints[...]["mid_grade"]`` 로만 확인됐는데,
    그 형태는 코드가 등급 이름을 박아도 통과한다 — 실제로 ``mid_grade`` 를 ``"중"`` 으로
    변이시켰더니 이 파일 57건이 그대로 통과했다.

    ⚠️ **두 값을 따로 흔든다.** 처음엔 둘을 한 번에 바꿨는데, 그러면 두 세계가 같은
      결과(기준등급 한 줄)로 수렴해 변이가 안 물렸다 — 검사가 있는데 아무것도 안 보는
      그 상태다. 각 값이 **혼자서** 산출을 바꾸는 자리를 골라야 한다.
    """
    state = _staged(as_of=SPREAD_WIDE)
    # 기준선: 그날 규칙은 중품을 태운다 (스프레드 21.2% ≥ 확대 임계 18.2%, 스코어 +0.083)
    assert {line["grade"] for line in allocate_sourcing(state)["sourcing_plan"]} == {"중", "상"}

    # ① 중품 등급만 특으로 → 스프레드가 (1650−1850)/1650 로 음수라 중품을 안 태운다.
    #    코드가 "중"을 박고 있으면 여전히 중품이 실려 {"중","상"} 이 나온다.
    mid_swapped = load_constraints()
    mid_swapped["grade"]["mid_grade"] = "특"
    monkeypatch.setattr(
        "app.purchase_agent.nodes.allocate_sourcing.load_constraints", lambda: mid_swapped
    )
    assert {line["grade"] for line in allocate_sourcing(state)["sourcing_plan"]} == {"상"}

    # ② 기준등급만 특으로 → 보유 로트가 상 등급뿐이라 상품 한계일을 못 재고 배분이 막힌다.
    #    코드가 "상"을 박고 있으면 종전대로 중품이 실린다.
    top_swapped = load_constraints()
    top_swapped["allocation"]["reference_grade"] = "특"
    monkeypatch.setattr(
        "app.purchase_agent.nodes.allocate_sourcing.load_constraints", lambda: top_swapped
    )
    assert {line["grade"] for line in allocate_sourcing(state)["sourcing_plan"]} == {"특"}


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_mid_grade_quantity_never_exceeds_near_term_demand(
    as_of: date, proposals: dict
) -> None:
    """중품 kg이 **근접 납품량 안에 들어가는가** — 상한이 비율이 아니라 kg 수준에서 지켜지는가.

    비율 상한이 kg 상한을 함의하는 건 커버일수 D가 확정주문 창(14일) 이하일 때뿐이다.
    D 매핑이 그 위로 올라가면 이 테스트가 먼저 깨진다.
    """
    constraints = load_constraints()
    mid_grade = constraints["grade"]["mid_grade"]
    state = _staged(as_of=as_of)
    decision = evaluate_mid_grade(state, constraints)
    for scenario in proposals[as_of]["scenarios"]:
        mid_kg = sum(x["qty_kg"] for x in scenario["sourcing_plan"] if x["grade"] == mid_grade)
        if mid_kg:
            assert mid_kg <= decision["near_qty_kg"]


# ── E3-1 DoD ④: 임계는 constraints.yaml이 정한다 (규칙 7) ───────────────────


def test_shelf_ratio_in_constraints_actually_drives_the_allocation() -> None:
    """``mid_grade_shelf_ratio``를 조이면 근접 납품분이 빠지고 중품이 사라진다.

    "constraints에서 읽는다"를 소스 검사가 아니라 **동작으로** 확인한다 — 값을 바꿨는데
    결과가 그대로면 어딘가에 하드코딩이 남아 있는 것이다.
    """
    state = _staged(as_of=SPREAD_WIDE)
    constraints = load_constraints()
    assert evaluate_mid_grade(state, constraints)["ratio"] > 0

    tightened = deepcopy(constraints)
    # 10일 × 0.2 = 2일 → +3일 납품도 못 받는다
    tightened["grade"]["mid_grade_shelf_ratio_fallback"] = 0.2
    assert evaluate_mid_grade(state, tightened)["ratio"] == 0


def test_widening_threshold_in_constraints_actually_gates_the_allocation() -> None:
    """``grade_spread_widening_ratio``를 올리면 같은 날도 "확대 아님"이 된다."""
    state = _staged(as_of=SPREAD_WIDE)
    raised = deepcopy(load_constraints())
    raised["triggers"]["grade_spread_widening_ratio"] = 1.0  # +100% 요구 → 9/11(+75%)도 미달
    assert evaluate_mid_grade(state, raised)["ratio"] == 0


def test_baseline_spread_is_read_from_constraints_for_every_item() -> None:
    """평시 기준선은 품목 4종 전부 선언돼 있다 — 없으면 판정을 못 한다 (규칙 3)."""
    constraints = load_constraints()
    for item in ("배추", "무", "양파", "피마늘"):
        assert baseline_spread(item, constraints) is not None
    assert baseline_spread("없는품목", constraints) is None


# ── 게이트가 실제로 무는가 — 합성 입력을 직접 먹인다 ────────────────────────
#
# mock에서는 두 게이트가 늘 같이 움직인다. 그러면 한쪽을 지워도 초록불이 뜬다.


def test_spread_widening_judgment_is_isolated_and_boundary_is_inclusive() -> None:
    """확대 판정 함수 단독 검사. 경계값 자신은 **확대**다 (">= 임계")."""
    assert is_spread_widened(0.180, 0.120, 0.50) is True  # 정확히 +50%
    assert is_spread_widened(0.179, 0.120, 0.50) is False
    # 못 잰 경우는 "확대 아님"이 아니라 "진입하지 않음"이다 — 사유는 blocked_by가 싣는다
    assert is_spread_widened(None, 0.120, 0.50) is False
    assert is_spread_widened(0.180, None, 0.50) is False


def test_grade_spread_is_none_when_a_grade_is_missing_not_zero() -> None:
    """등급이 없으면 None이다. 0으로 채우면 확대 판정이 조용히 통과한다 (규칙 3)."""
    both = [
        {"market": "가락", "grade": "상", "price": 1650},
        {"market": "가락", "grade": "중", "price": 1450},
    ]
    assert grade_spread(both, "상", "중") == pytest.approx((1650 - 1450) / 1650)
    assert grade_spread(both[:1], "상", "중") is None
    assert grade_spread(both[1:], "상", "중") is None


def test_score_flips_sign_on_the_freshness_risk() -> None:
    """스코어 함수 단독 검사 — 단가 이득이 같아도 신선도 리스크가 크면 음수가 된다."""
    weights = {"price_gain": 1.0, "freshness_risk": 0.30}
    assert mid_grade_score(0.21, 0.57, weights) > 0
    assert mid_grade_score(0.12, 0.57, weights) < 0
    assert mid_grade_score(0.21, 0.95, weights) < 0  # 유통기한이 짧으면 싸도 안 산다


def test_widened_spread_alone_does_not_adopt_mid_grade() -> None:
    """스프레드가 확대돼도 **스코어가 음수면 채택하지 않는다** — 두 게이트의 AND.

    상품 한계일을 2일로 낮춰 신선도 리스크만 키운다. 시세는 9/11 그대로다.
    """
    state = _staged(as_of=SPREAD_WIDE)
    state["inventory"]["lots"][0]["shelf_life_days"] = 2
    decision = evaluate_mid_grade(state, load_constraints())
    assert decision["widened"] is True
    assert decision["score"] < 0
    assert decision["ratio"] == 0


def test_positive_score_alone_does_not_adopt_mid_grade() -> None:
    """스코어가 양수여도 **평시 스프레드면 채택하지 않는다** — AND의 반대쪽.

    양파는 보관한계가 길어 신선도 리스크가 0으로 clamp된다. 스코어만 보면 평시에도
    중품 100%가 되고, 확대 게이트가 그걸 막는다.
    """
    state = _staged(item="양파", as_of=RISING)
    decision = evaluate_mid_grade(state, load_constraints())
    assert decision["freshness_risk"] == 0
    assert decision["score"] > 0
    assert decision["widened"] is False
    assert decision["ratio"] == 0


# ── 규칙 3: 못 잰 것은 계산하지 않고 그 사실이 드러난다 ─────────────────────


def test_missing_top_grade_lot_blocks_the_allocation_instead_of_guessing() -> None:
    """상 등급 로트가 없으면 상품 한계일을 모른다 — 중품 배정을 하지 않고 사유를 남긴다."""
    state = _staged(as_of=SPREAD_WIDE)
    state["inventory"]["lots"] = []
    assert top_grade_shelf_days(state["inventory"], "상", ITEM) is None

    decision = evaluate_mid_grade(state, load_constraints())
    assert decision["ratio"] == 0
    assert "상품 한계일" in decision["blocked_by"]

    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    result = package_scenarios(state)
    assert any("등급 배분 보류" in risk for risk in result["scenarios_final"][0]["risks"])


def test_zero_confirmed_orders_does_not_divide_by_zero() -> None:
    """확정주문 0kg은 상한의 분모가 없다 — 터지지 않고 배정을 접는다.

    기존 테스트 ``test_all_plans_clipped_to_zero_...``가 실제로 이 상태를 만든다.
    """
    state = _staged()
    state["confirmed_orders"] = {**state["confirmed_orders"], "total_kg": 0}
    decision = evaluate_mid_grade(state, load_constraints())
    assert decision["ratio"] == 0
    assert decision["blocked_by"]


def test_a_ratio_that_rounds_to_zero_kg_is_dropped_before_it_kills_the_proposal() -> None:
    """0kg 줄은 스키마(``qty_kg > 0``)가 **제안 전체**를 죽인다 — ⑤가 미리 접는다.

    ⚠️ 이 테스트의 첫 판은 **가장 작은 안만** 봤다. 마지막 줄은 ``총량 − round(총량 × 비율)``
    이라 큰 안에서만 0이 되는 조합이 따로 있는데 그걸 놓쳤다 (Codex 교차검증 지적).
    지금은 잔여분 비율도 같은 검사를 받는다.
    """
    tiny = {"base_plan": {"drafts": [{"total_qty_kg": 1}, {"total_qty_kg": 100}]}}
    assert _yields_positive_kg(tiny, 0.4) is False  # round(1 × 0.4) == 0
    assert _yields_positive_kg(tiny, 0.6) is True  # 중품 줄은 산다…
    assert _yields_positive_kg(tiny, 1 - 0.6) is False  # …그런데 잔여분이 0kg이다

    # 안이 여럿이면 **전부** 통과해야 한다 — 큰 안에서만 깨지는 조합
    mixed = {"base_plan": {"drafts": [{"total_qty_kg": 10}, {"total_qty_kg": 10000}]}}
    assert _yields_positive_kg(mixed, 0.04) is False  # round(10 × 0.04) == 0

    state = _staged(as_of=SPREAD_WIDE)
    # 근접 납품 비중을 0.4로 낮추고, 가장 작은 안을 1kg으로 만든다
    state["confirmed_orders"] = {
        **state["confirmed_orders"],
        "orders": [
            {"sale_id": 1, "qty_kg": 4000, "due_date": "2026-09-14"},
            {"sale_id": 2, "qty_kg": 6000, "due_date": "2026-09-21"},
        ],
        "total_kg": 10000,
    }
    state["base_plan"]["drafts"][0]["total_qty_kg"] = 1
    assert len(allocate_sourcing(state)["sourcing_plan"]) == 1

    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    assert self_check(state)["proposal"]["scenarios"]  # 제안이 살아남는다


# ── 근거 (규칙 4) ───────────────────────────────────────────────────────────


def _mid_grade_items(scenario: dict) -> list[dict]:
    """중품 배분 근거만 고른다 — **LLM이 닿지 못하는 축**으로 거른다.

    ⚠️ ``claim``의 "스프레드"로 거르면 안 된다. ⑤의 LLM 판단 근거는
    ``f"등급 조합 {id} 선택 — {mix.reason}"``이고(``package_scenarios.py``)
    ``reason``은 **LLM이 쓴 자유 문장**이라 "스프레드"라는 단어가 들어갈 수 있다.
    2026-08-31 실측에서 gemma3:4b가 실제로 *"등급 스프레드가 확대된 상황에서…"* 를 냈다.
    그러면 개수 단언이 **확률적으로** 깨진다 — 모델·온도에 따라 재현이 갈리는 flaky 다.

    ``evidence_detail``의 "소진 한계"는 규칙이 조립한 문구라(``_ratio_line``) LLM이
    건드릴 수 없다. 그래서 LLM on/off 어느 쪽에서도 같은 것을 센다.
    """
    return [
        r for r in scenario["rationale"] if "소진 한계" in (r.get("evidence_detail") or "")
    ]


def test_mid_grade_allocation_carries_its_own_rationale(proposals: dict) -> None:
    """중품을 태운 날엔 왜 태웠는지가 근거에 남는다 — ref_id 포함 (규칙 4).

    ``ASSUMED``인 이유: 소진 한계일이 재고 로트에서 **추론**한 값이라 기준선의 SIM_FIXED를
    물려받지 못한다 (IO명세 §5).
    """
    for scenario in proposals[SPREAD_WIDE]["scenarios"]:
        items = _mid_grade_items(scenario)
        assert len(items) == 1
        assert items[0]["ref_id"].strip()
        assert items[0]["evidence_grade"] == "ASSUMED"

    for scenario in proposals[RISING]["scenarios"]:
        assert not _mid_grade_items(scenario)


def _selector_saying(reason: str):
    """규칙 기본안을 그대로 고르되 **이유 문장만** 지정한다.

    수량을 바꾸지 않으므로 이 테스트가 보는 것은 "문장이 근거에 실리는 경로" 하나로
    좁혀진다. ``test_mix_llm.py``의 ``_fixed_selector``와 같은 방식이다.
    """

    def selector(context, default_candidate_id: str) -> MixDecision:
        del context
        return MixDecision(
            candidate_id=default_candidate_id,
            reason=reason,
            llm_status="SUCCESS",
            llm_model="fake-model",
            llm_fallback_used=False,
        )

    return selector


def test_llm_wording_cannot_inflate_the_mid_grade_rationale_count() -> None:
    """**2026-08-31 회귀.** LLM 문장이 "스프레드"를 써도 중품 근거는 1건 그대로다.

    LLM이 켜지면 ⑤의 판단 근거가 rationale에 **1건 늘어난다.** 그 자체는 설계대로지만,
    옛 필터(``"스프레드" in claim``)는 늘어난 항목까지 함께 세어 개수 단언을 깼다.
    conftest가 LLM을 꺼 두는 덕에 기본 실행에서만 안 보였을 뿐이다.

    아래 두 단언이 짝이다 — 새 필터가 안 흔들리는 것과, **옛 필터였다면 깨졌을 것**을
    같이 잠근다. 뒤엣것이 없으면 필터를 되돌려도 이 테스트가 통과한다.
    """
    proposal = run_purchase_agent(
        ITEM, SPREAD_WIDE, selector=_selector_saying("등급 스프레드가 확대된 상황이라 골랐다")
    )
    for scenario in proposal["scenarios"]:
        assert len(_mid_grade_items(scenario)) == 1
        assert len([r for r in scenario["rationale"] if "스프레드" in r["claim"]]) == 2


def test_deferred_arrival_date_check_is_disclosed_when_mid_grade_is_used(proposals: dict) -> None:
    """매칭이 as_of 기준 **근사**라는 사실(= N4를 못 쓴다)이 risks에 드러난다 (규칙 3).

    "확정주문 일정상 충족"이라고 쓰지 않는다 — 근사 위에서 낸 결론을 검증된 것처럼 적으면
    규칙 3이 형식만 남는다 (Codex 교차검증 지적).
    """
    assert load_constraints()["pending"]["inbound_lead_days"] is None
    for scenario in proposals[SPREAD_WIDE]["scenarios"]:
        assert any("매입일 기준 근사" in risk for risk in scenario["risks"])
        assert not any("일정상 충족" in risk for risk in scenario["risks"])


def test_decided_n4_shifts_the_window_and_removes_the_disclosure() -> None:
    """N4가 확정되면 **창이 입고일부터로 이동하고** 그 고지가 사라진다.

    NULL을 0으로 채우지 않았다는 증거는 "적어뒀다"가 아니라 "값이 오면 실제로 쓴다"다.
    지금 코드가 N4를 아예 읽지 않으면 나중에 값이 확정돼도 조용히 무시된다.
    """
    state = _staged(as_of=SPREAD_WIDE)
    decided = deepcopy(load_constraints())
    decided["pending"]["inbound_lead_days"] = 4  # 입고까지 4일 → +3일 납품분은 못 채운다

    assumed = evaluate_mid_grade(state, load_constraints())
    shifted = evaluate_mid_grade(state, decided)
    assert assumed["arrival_basis_assumed"] is True
    assert shifted["arrival_basis_assumed"] is False
    # +3일(9/14) 12,000kg이 빠지고 +8일(9/19) 6,000kg이 들어온다 — 창이 이동했다
    assert assumed["near_qty_kg"] == 12000
    assert shifted["near_qty_kg"] == 6000


# ── Codex 교차검증에서 드러난 것 — 배추 하나만 돌려서 놓쳤던 자리 ────────────
#
# E3-1 첫 판은 4품목 중 배추만 테스트했다. 양파·피마늘은 확정주문 전부가 중품 소진 창
# 안에 들어와 상한이 1.0이 되고, 기준등급 줄이 비율 0.0으로 나가 _validate_ratios가
# 터졌다. "전량 중품"은 오류가 아니라 정상 결론이었는데 코드가 그 경우를 몰랐다.


@pytest.mark.parametrize("item", ["배추", "무", "양파", "피마늘"])
@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_every_item_produces_a_valid_proposal_on_every_anchor(item: str, as_of: date) -> None:
    """4품목 × 4앵커 전부 도는가. **품목 하나만 도는 테스트는 이 버그를 못 잡는다.**"""
    proposal = run_purchase_agent(item, as_of)
    PurchaseProposal.model_validate(proposal)
    assert proposal["scenarios"]
    for scenario in proposal["scenarios"]:
        assert check_quadruple_match(scenario) is None
        for line in scenario["sourcing_plan"]:
            assert line["qty_kg"] > 0


def test_full_near_term_demand_yields_a_single_mid_grade_line() -> None:
    """확정주문 전부가 소진 창 안이면 **전량 중품**이다 — 0 비율 줄을 만들지 않는다.

    양파는 보관한계 60일이라 상한이 1.0이 된다. 기준등급 줄을 비율 0.0으로 끼워 넣으면
    ``_validate_ratios``가 터져 그날 제안이 통째로 사라진다.
    """
    constraints = load_constraints()
    state = _staged(item="양파", as_of=SPREAD_WIDE)
    assert evaluate_mid_grade(state, constraints)["cap_ratio"] == 1.0

    lines = allocate_sourcing(state)["sourcing_plan"]
    assert [line["grade"] for line in lines] == ["중"]  # 리터럴 — 규칙 8
    assert lines[0]["ratio"] == 1.0


def test_overdue_orders_are_not_counted_as_near_term_demand() -> None:
    """납기가 지난 주문은 근접 수요가 아니다 — 하한이 없으면 상한이 부풀어 오른다.

    지금은 mock 로더가 ``0 <= offset``으로 걸러 가려져 있지만, 실데이터 스냅샷에 연체
    주문이 하나 들어오는 순간 새는 자리였다.
    """
    overdue = [{"sale_id": 1, "qty_kg": 5000, "due_date": "2026-09-01"}]
    assert near_term_demand_kg(overdue, "2026-09-11", 6) == 0
    # 창이 이동하면 앞쪽도 함께 밀린다 (N4 확정 시 경로)
    upcoming = [{"sale_id": 2, "qty_kg": 5000, "due_date": "2026-09-14"}]
    assert near_term_demand_kg(upcoming, "2026-09-11", 6) == 5000
    assert near_term_demand_kg(upcoming, "2026-09-11", 6, since_days=4) == 0


def test_zero_baseline_blocks_instead_of_passing_every_spread() -> None:
    """기준선 0은 "확대" 판정을 무의미하게 만든다 (0 × 1.5 = 0). 미확정과 같이 막는다.

    막지 않으면 어떤 스프레드든 확대로 통과하고, 근거를 쓰는 쪽이 ``spread / baseline``에서
    ZeroDivisionError로 터진다.
    """
    broken = deepcopy(load_constraints())
    broken["grade"]["baseline_grade_spread"]["배추"] = 0.0
    decision = evaluate_mid_grade(_staged(as_of=SPREAD_WIDE), broken)
    assert decision["ratio"] == 0
    assert decision["widened"] is False
    assert "기준선" in decision["blocked_by"]


def test_cap_is_clamped_by_the_largest_scenario_so_kg_ceiling_holds() -> None:
    """커버일수 D가 확정주문 창을 넘어도 중품 kg이 근접 납품량을 넘지 않는다.

    비율 상한만 두면 상한이 D에 인질로 잡힌다 — 지금 D 매핑(최대 12)에선 안 드러나지만
    ``coverage_days.max``는 18이라 튜닝 한 번에 조용히 깨질 자리였다.
    """
    state = _staged(as_of=SPREAD_WIDE)
    state["base_plan"]["drafts"][-1]["total_qty_kg"] = 100_000  # D를 크게 잡은 셈
    decision = evaluate_mid_grade(state, load_constraints())
    for draft in state["base_plan"]["drafts"]:
        assert round(draft["total_qty_kg"] * decision["ratio"]) <= decision["near_qty_kg"]


def test_blocked_allocation_names_the_grade_it_actually_used() -> None:
    """기준등급 시세가 없으면 다른 등급으로 대체한다 — risks가 그 등급을 이름으로 적는다.

    "전량 기준등급으로 배정했다"고 쓰면 실제로는 특을 샀는데 상을 샀다고 적히게 된다.
    """
    state = _staged(as_of=SPREAD_WIDE)
    state["market_quotes"] = [q for q in state["market_quotes"] if q["grade"] != "상"]
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    used = state["sourcing_plan"][0]["grade"]
    assert used == "특"

    risks = package_scenarios(state)["scenarios_final"][0]["risks"]
    note = next(risk for risk in risks if "등급 배분 보류" in risk)
    assert f"전량 {used} 단일 등급" in note


def test_shelf_days_do_not_depend_on_lot_order() -> None:
    """로트가 여럿이면 **가장 짧은** 유통기한을 쓴다 — 같은 재고를 순서만 바꿔도 같은 답."""
    state = _staged(as_of=SPREAD_WIDE)
    lot = state["inventory"]["lots"][0]
    state["inventory"]["lots"] = [{**lot, "shelf_life_days": 20}, {**lot, "shelf_life_days": 4}]
    assert top_grade_shelf_days(state["inventory"], "상", ITEM) == 4

    state["inventory"]["lots"].reverse()
    assert top_grade_shelf_days(state["inventory"], "상", ITEM) == 4


# ── #76 미결 고지 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("inventory", "expected"),
    [
        ({"lots": None}, "재고 로트를 받지 못해"),
        ({"lots": []}, "보유 로트가 없어"),
        # 물류 payload 에는 shelf_life_days 자체가 없다 (#76 미결)
        (
            {"lots": [{"lot_id": "L1", "grade": "상", "available_qty_kg": 100}]},
            "어느 쪽으로도 받지 못했다",
        ),
        ({"lots": [{"lot_id": "L1", "grade": None, "shelf_life_days": 10}]}, "등급이 모두 미상"),
        ({"lots": [{"lot_id": "L1", "grade": "중", "shelf_life_days": 10}]}, "상 등급 로트가 없어"),
    ],
    ids=["로트미수신", "로트없음", "키미수신", "등급미상", "해당등급없음"],
)
def test_shelf_days_block_reason_separates_four_causes(inventory: dict, expected: str) -> None:
    """``None`` 은 하나인데 원인이 넷이라 갈라 적는다.

    전에는 원인과 무관하게 *"상 등급 로트가 없다"* 로 적었다. 물류 경로의 실제 원인은
    **키 자체가 안 실린 것**인데(#76 미결) 그렇게 쓰면 *"재고에 상 등급이 없구나"* 로
    읽힌다 — 침묵도 오답이지만 **틀린 사유는 더 나쁘다.**
    """
    assert expected in shelf_days_block_reason(inventory, "상", ITEM)


def test_missing_shelf_life_surfaces_in_risks_instead_of_passing_silently() -> None:
    """키가 없으면 조용히 넘어가지 않고 **고지로 나간다** (§3.7.6 · 규칙 3).

    사유를 안 남기면 "중품을 검토하고 안 쓴 것" 과 "검토 자체를 못 한 것" 이 같은 화면이 된다.
    """
    as_of = date(2026, 9, 11)  # 스프레드가 넓은 날 — 중품 검토가 실제로 진입하는 앵커
    state = build_initial_state("배추", as_of)
    state["inventory"]["lots"] = [
        {k: v for k, v in lot.items() if k != "shelf_life_days"}
        for lot in state["inventory"]["lots"]
    ]
    decision = evaluate_mid_grade(state, load_constraints())
    assert decision["blocked_by"] is not None
    assert "어느 쪽으로도 받지 못했다" in decision["blocked_by"]

    # 그리고 그 사유가 **고지로 나간다** — decision 안에만 있으면 사용자는 못 본다
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    notices = [
        risk
        for scenario in package_scenarios(state)["scenarios_final"]
        for risk in scenario["risks"]
    ]
    assert any("어느 쪽으로도 받지 못했다" in risk for risk in notices), notices


def test_warehouse_cap_floors_fractional_capacity() -> None:
    """창고 수용량은 **상한**이라 내린다 — 올리면 못 넣는 양을 계획하게 된다.

    물류 실측이 7,636.72kg 다. 7,637kg 을 사면 0.28kg 이 갈 곳이 없다.
    수량을 ``min()`` 으로 클립하는 것과 같은 보수 방향이다.
    """
    cap = warehouse_cap_kg({"warehouse_free_kg": 7636.72, "rental_cap_kg": 0.0})
    assert cap == 7636
    assert isinstance(cap, int)
    # 정수 입력은 그대로다 — mock 경로가 달라지지 않는다
    assert warehouse_cap_kg({"warehouse_free_kg": 12000, "rental_cap_kg": 3600}) == 15600


def test_warehouse_cap_accepts_confirmed_zero() -> None:
    """**확정된 0은 통과한다** (규칙 3).

    ``rental_cap_kg``는 2026-08-27 물류 회신 §1로 0 확정이고, 창고가 꽉 차면
    ``warehouse_free_kg``도 0이다. 둘 다 "값이 안 왔다"가 아니라 **사실**이라
    상한 0kg으로 그대로 쓴다 — 전 안이 창고에 눌리는 것이 맞는 결과다.
    """
    assert warehouse_cap_kg({"warehouse_free_kg": 0, "rental_cap_kg": 0}) == 0


def test_warehouse_cap_rejects_true_instead_of_reading_it_as_one_kg() -> None:
    """🔴 ``True``가 **1kg 상한**으로 통과하던 자리.

    ``bool``은 ``int``의 하위형이라 ``True + 0 == 1``이다. 창고 상한이 1kg이면 전 안이
    거기에 눌려 죽는데 **에러가 없어 원인이 안 보인다** — ``_positive_int``·
    ``schemas._reject_boolean``이 같은 이유로 ``bool``을 먼저 막는다.
    """
    with pytest.raises(TypeError, match="warehouse_free_kg"):
        warehouse_cap_kg({"warehouse_free_kg": True, "rental_cap_kg": 0})


@pytest.mark.parametrize(
    "bad", ["1000", [1], None, float("nan"), float("inf"), -500], ids=lambda v: repr(v)
)
def test_warehouse_cap_names_the_key_instead_of_dying_anonymously(bad: object) -> None:
    """어느 키가 왜 잘못됐는지를 **메시지가 말한다**.

    전에는 ``'1000' + 0``·``Decimal + float``이 더하는 자리에서 터졌다. 그 ``TypeError``
    에는 키 이름이 없어 *"물류가 무엇을 잘못 보냈는가"*를 알 수 없었다. 수신 payload는
    ``adapter.validate_payload``가 먼저 잡아 ``missing_data``로 사유를 내므로, 이 가드가
    실제로 터지는 자리는 mock·직접 호출 경로다.
    """
    with pytest.raises((TypeError, ValueError), match="rental_cap_kg"):
        warehouse_cap_kg({"warehouse_free_kg": 100, "rental_cap_kg": bad})


def test_warehouse_cap_mixes_decimal_and_float_without_dying() -> None:
    """출처가 둘이면 ``Decimal + float``이 ``TypeError``였다.

    물류 어댑터는 float으로 보내지만(``logistics/adapter._num``) 그것이 유일한 출처라는
    보장이 없다. 타입을 맞춰 받으므로 섞여 와도 같은 답이 나온다.
    """
    mixed = warehouse_cap_kg({"warehouse_free_kg": Decimal("7636.72"), "rental_cap_kg": 0.0})
    assert mixed == warehouse_cap_kg({"warehouse_free_kg": 7636.72, "rental_cap_kg": 0.0}) == 7636
