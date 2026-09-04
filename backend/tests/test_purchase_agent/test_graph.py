"""Epic 2 골격 검사 — state · 그래프 · 노드 ①③⑥⑦ (백로그 E2-1~E2-4).

완료 조건은 CLAUDE.md "작업 방식"의 mock 시나리오 4종이다. 여기서는 "그래프가 돌았다"가
아니라 **"각 시나리오가 뜻하는 결론이 실제로 나오는가"**를 본다.

임계는 전부 ``load_constraints()``에서 읽는다 (규칙 7).
"""

from datetime import date

import pytest
from _injection import INJECTED_THRESHOLD, forced_proposals, swap_threshold
from pydantic import ValidationError

from app.purchase_agent import ports
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import NODES, build_graph, route_after_classify, run_purchase_agent
from app.purchase_agent.nodes._guards import require_positive
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation, compute_ci_width
from app.purchase_agent.nodes.draft_plan import (
    draft_plan,
    fixed_market_quotes,
    reference_unit_price,
)
from app.purchase_agent.nodes.package_scenarios import (
    assign_axes,
    compute_margin,
    materialize_sourcing,
    package_scenarios,
)
from app.purchase_agent.nodes.self_check import (
    check_axis_allowed,
    check_axis_diversity,
    check_cash_ceiling,
    check_max_price,
    check_prices_exist,
    check_quadruple_match,
    check_split_dates,
    check_warehouse_capacity,
    self_check,
)
from app.purchase_agent.nodes.split_plan import split_plan
from app.purchase_agent.schemas import PurchaseProposal, revalidate_for_output
from app.purchase_agent.state import build_initial_state

RISING = date(2026, 8, 21)
FALLING = date(2026, 8, 28)
UNCERTAIN = date(2026, 9, 4)
SPREAD_WIDE = date(2026, 9, 11)
#: 통합 시연 앵커 (#73). 성격은 rising 과 같고 **날짜만 다르다** — 재무·물류 DB 데이터가
#: 이 날에만 있어서(2026-08-28 실측) 세 파트가 함께 도는 유일한 날이다. 기대는 rising 군과
#: 같은 자리에 넣는다: 같은 forecast·quotes 를 쓰므로 다른 값이 나오면 그게 회귀다.
INTEGRATION = date(2025, 12, 31)
ANCHORS = (INTEGRATION, RISING, FALLING, UNCERTAIN, SPREAD_WIDE)

ITEM = "배추"


@pytest.fixture(scope="module")
def proposals() -> dict[date, dict]:
    """앵커일별 제안. 그래프를 앵커당 한 번만 돌린다.

    **실제 분류 경로를 그대로 돈다** — 상황이 단언에 안 들어가는 검사(사중 일치·시세
    실재·컷 없음 …)를 먹인다. 상황 자체가 단언인 검사는 아래 ``forced`` 를 쓴다.
    """
    return {as_of: run_purchase_agent(ITEM, as_of) for as_of in ANCHORS}


@pytest.fixture(scope="module")
def forced() -> dict[str, dict[date, dict]]:
    """``{상황: {앵커: 제안}}`` — ③ 상황 주입 (현서님 §1.4-③).

    상황이 **단언의 일부인** 검사는 여기서 받는다. 그러면 "그날 mock 밴드가 선언 임계를
    넘느냐"에 안 매달린다 — 임계 0.08 과 0.15 에서 산출물이 **완전히 같음을 실측했다**
    (2026-09-04).
    """
    return forced_proposals(run_purchase_agent, ITEM, ANCHORS)


# ── E2-1: 그래프 골격 ───────────────────────────────────────────────────────


def test_graph_compiles_with_all_seven_nodes() -> None:
    """E2-1 DoD "LangGraph State + 그래프 골격(7노드) — 컴파일·통과 실행"."""
    assert len(NODES) == 7
    nodes = build_graph().get_graph().nodes
    assert set(NODES) <= set(nodes)


def test_stable_days_skip_the_context_loop() -> None:
    """§4-②: "stable한 날은 이 노드를 건너뛴다" — 문서를 읽을지 말지부터가 판단이다.

    🟢 **여기엔 주입이 필요 없다.** 분기는 ``situation`` 한 필드만 보는 **순수 함수**라
      그 필드를 직접 주면 된다. 예전엔 앵커를 골라 ①을 돌려 상황을 *만들어* 왔는데,
      그건 분기를 재면서 **mock 밴드와 선언 임계를 함께** 재는 것이었다 — 임계를 0.15 로
      올리자 이 검사가 깨졌다. 분기 자체는 그때도 멀쩡했다.

    미지값도 함께 본다. ``draft_plan`` 으로 떨어지는 것이 안전한 쪽이다 — 상황이 깨졌을 때
    문서를 읽으러 가는 것보다 안 읽고 진행하는 편이 덜 위험하다.
    """
    assert route_after_classify({"situation": "uncertain"}) == "collect_context"
    assert route_after_classify({"situation": "stable"}) == "draft_plan"
    assert route_after_classify({"situation": "알수없음"}) == "draft_plan"


# ── E2-2: ① 상황 분류 + 허용 축 ─────────────────────────────────────────────

SITUATION_BY_ANCHOR = (
    (INTEGRATION, "stable", ["quantity", "timing"]),
    (RISING, "stable", ["quantity", "timing"]),
    (FALLING, "stable", ["quantity"]),
    (UNCERTAIN, "uncertain", ["quantity"]),
    (SPREAD_WIDE, "stable", ["quantity", "timing"]),
)


@pytest.mark.parametrize(("as_of", "situation", "axes"), SITUATION_BY_ANCHOR, ids=lambda v: str(v))
def test_classify_matches_each_mock_scenario(
    as_of: date, situation: str, axes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2-2 DoD "mock 3종 정확 분류" + 그날 허용 축.

    임계는 **검사가 주입한다** (②). 이 검사가 재는 것은 *"그날 밴드가 선언 임계 어느
    쪽이냐"* 가 아니라 **"주입한 임계 기준으로 상황과 축이 시나리오 이름대로 나오나"** 다.
    선언값이 ``#127`` 로 움직이면 축 배분까지 같이 무너지던 자리다.
    """
    swap_threshold(monkeypatch, INJECTED_THRESHOLD)
    state = build_initial_state(ITEM, as_of)
    result = classify_situation(state)
    assert result["situation"] == situation
    assert result["allowed_axes"] == axes


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_mix_axis_is_gated_out_by_item_concentration(as_of: date) -> None:
    """배추 편중 81.2% > 0.70이라 mix 축은 매일 제외된다 (정의서 §3.5.1).

    편중이 완화되면 **코드 변경 없이** 부활해야 하므로, 임계와 비중을 파일에서 읽는다.
    """
    state = build_initial_state(ITEM, as_of)
    threshold = load_constraints()["concentration"]["item_threshold"]
    assert max(state["item_mix_ratio"].values()) >= threshold
    assert "mix" not in classify_situation(state)["allowed_axes"]


# ── E2-3: ③⑥ 수량과 패키징 ─────────────────────────────────────────────────

#: 안 개수는 **상황의 파생값**이다 (§4.2.2 "하나의 신뢰도 판정이 개수·허용 축·분할 진입
#: 셋을 동시에 결정한다"). 예전엔 앵커별 표였는데, 그러면 같은 규칙을 앵커 수만큼 적어
#: 두고 **어느 앵커가 어느 상황인지**까지 이 표가 떠안는다 — 임계가 움직이는 순간 표가
#: 통째로 거짓말이 된다. 상황을 키로 두면 앵커 다섯에 **전부** 걸린다(5→10건).
SCENARIO_COUNT = (("stable", 3), ("uncertain", 2))


@pytest.mark.parametrize(("situation", "count"), SCENARIO_COUNT, ids=lambda v: str(v))
@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_scenario_count_per_situation(
    as_of: date, situation: str, count: int, forced: dict
) -> None:
    """E2-3 DoD "stable→3안, uncertain→2안(공격 차단)"."""
    assert len(forced[situation][as_of]["scenarios"]) == count


def test_uncertain_never_produces_an_aggressive_scenario(forced: dict) -> None:
    """구간이 넓은 날엔 공격안을 만들지 않는다 (규칙 4 · §4-③).

    ③ 로 상황을 고정한다 — 이 검사의 주제는 *"9/4 가 uncertain 이다"* 가 아니라
    **"uncertain 이면 공격안이 안 나온다"** 다.
    """
    labels = [s["label"] for s in forced["uncertain"][UNCERTAIN]["scenarios"]]
    assert "공격" not in labels
    assert labels == ["보수", "기본"]


def test_coverage_days_follow_the_constraints_mapping(proposals: dict) -> None:
    """D는 constraints.yaml의 3안 매핑을 그대로 쓴다 (규칙 7)."""
    mapping = load_constraints()["coverage_days"]["by_label"]
    for scenario in proposals[RISING]["scenarios"]:
        assert scenario["coverage_days"] == mapping[scenario["label"]]


def test_quantities_are_distinct_and_ordered(proposals: dict) -> None:
    """보수 < 기본 < 공격. 셋이 같아지면 "3안"이 사실상 1안이다.

    현금 상한이 전 안을 같은 값으로 눌러 실제로 이런 일이 있었다 — mock 현금과 주문의
    자릿수가 맞지 않아서였고, cash.json에 경위를 남겼다.
    """
    quantities = [s["total_qty_kg"] for s in proposals[RISING]["scenarios"]]
    assert quantities == sorted(quantities)
    assert len(set(quantities)) == len(quantities)


def test_daily_demand_excludes_the_legacy_safety_stock() -> None:
    """일평균 = total_kg ÷ order_window_days. **안전재고를 곱하지 않는다** (§4-③).

    D 방식이 "확정주문 + 안전재고 20%"의 일반화이므로 둘 다 적용하면 이중 계상이다.
    """
    constraints = load_constraints()
    state = build_initial_state(ITEM, RISING)
    state.update(classify_situation(state))
    daily = draft_plan(state)["base_plan"]["daily_demand_kg"]
    expected = state["confirmed_orders"]["total_kg"] / constraints["demand"]["order_window_days"]
    assert daily == expected
    assert daily != expected * (1 + constraints["demand"]["safety_stock_ratio"])


# ── E2-4: ⑦ self_check ─────────────────────────────────────────────────────


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_quadruple_match_holds_for_every_scenario(as_of: date, proposals: dict) -> None:
    """사중 일치 — 수량 3축 + 금액 1축 (규칙 4)."""
    for scenario in proposals[as_of]["scenarios"]:
        assert check_quadruple_match(scenario) is None
        assert scenario["total_qty_kg"] == sum(x["qty_kg"] for x in scenario["split_plan"])
        assert scenario["total_qty_kg"] == sum(x["qty_kg"] for x in scenario["sourcing_plan"])
        assert scenario["total_amount_krw"] == sum(
            x["qty_kg"] * x["grade_unit_price"] for x in scenario["sourcing_plan"]
        )


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_sourcing_prices_exist_in_the_same_day_quotes(as_of: date, proposals: dict) -> None:
    """등급·단가는 당일 시세에 실재하는 값만 (규칙 4). 지어낸 단가를 막는다."""
    quotes = ports.get_market_quotes(ITEM, as_of)
    for scenario in proposals[as_of]["scenarios"]:
        assert check_prices_exist(scenario, quotes) is None


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_every_rationale_item_carries_a_ref_id(as_of: date, proposals: dict) -> None:
    """모든 근거에 ref_id 필수 (규칙 4) — 근거 없는 주장을 막는 최소 장치."""
    for scenario in proposals[as_of]["scenarios"]:
        assert scenario["rationale"]
        for item in scenario["rationale"]:
            assert item["ref_id"].strip()
            assert item["evidence_grade"] in {"OFFICIAL", "VENDOR", "SIM_FIXED", "ASSUMED"}


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_first_split_round_lands_on_as_of(as_of: date, proposals: dict) -> None:
    for scenario in proposals[as_of]["scenarios"]:
        assert scenario["split_plan"][0]["date"] == as_of.isoformat()
        assert scenario["split_plan"][0]["seq"] == 1


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_total_quantity_stays_within_warehouse_capacity(as_of: date, proposals: dict) -> None:
    inventory = ports.get_inventory(ITEM, as_of)
    cap = inventory["warehouse_free_kg"] + inventory["rental_cap_kg"]
    for scenario in proposals[as_of]["scenarios"]:
        assert scenario["total_qty_kg"] <= cap


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_purchase_amount_stays_within_the_cash_ceiling(as_of: date, proposals: dict) -> None:
    constraints = load_constraints()
    budget = ports.get_projected_cash_min(as_of, constraints["cash"]["horizon_days"])
    budget *= constraints["cash"]["max_purchase_ratio"]
    for scenario in proposals[as_of]["scenarios"]:
        assert scenario["total_amount_krw"] <= budget


def test_cash_ceiling_actually_binds_on_the_aggressive_plan() -> None:
    """제약이 **작동하는지** 확인한다 — 아무것도 안 걸리면 검사가 있는 줄도 모른다.

    보수·기본은 통과하고 공격만 눌리는 게 mock의 의도다 (cash.json ``_현재값_근거``).
    """
    state = build_initial_state(ITEM, RISING)
    state.update(classify_situation(state))
    drafts = {d["label"]: d for d in draft_plan(state)["base_plan"]["drafts"]}
    assert drafts["보수"]["clipped_by"] == []
    assert drafts["기본"]["clipped_by"] == []
    assert [c["constraint"] for c in drafts["공격"]["clipped_by"]] == ["현금"]


# ── 규칙 3: 미결값은 계산을 막고, 그 사실이 드러난다 ────────────────────────


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_deferred_n4_check_is_disclosed_in_risks(as_of: date, proposals: dict) -> None:
    """N4가 NULL인 동안 입고일 검사를 하지 않는다는 사실이 **risks**에 드러난다.

    ``rejected_reasons``가 아니라 risks인 이유: 소비자는 rejected_reasons를 "컷된 안의
    이력"으로 읽는다. "검사를 건너뛰었다"는 다른 의미라 섞으면 계약이 오염된다.
    """
    assert load_constraints()["pending"]["inbound_lead_days"] is None
    for scenario in proposals[as_of]["scenarios"]:
        assert any("입고 소요일" in risk for risk in scenario["risks"])
    assert not any("입고 소요일" in r["reason"] for r in proposals[as_of]["rejected_reasons"])


def test_item_without_shelf_life_defers_the_freshness_check() -> None:
    """무·양파는 품목 보관한계가 미확정이라 신선도 상한을 **계산하지 않는다**.

    0으로 채우면 매입량이 눌리고 큰 수로 채우면 검사가 있었던 것처럼 보인다 — 둘 다 거짓이다.
    """
    assert load_constraints()["shelf_life_days"]["무"] is None
    state = build_initial_state("무", RISING)
    state.update(classify_situation(state))
    deferred = draft_plan(state)["base_plan"]["deferred_checks"]
    assert any("신선도 상한 검사 보류" in note for note in deferred)


# ── 축 검사 이관 (정의서 §3.5.1-3 — self_check 소유) ────────────────────────


def test_schema_no_longer_judges_axis_diversity() -> None:
    """스키마는 그날 allowed_axes를 모르므로 이 판정을 내릴 자격이 없다.

    Epic 1에서 스키마에 두었다가, mock_falling(허용 축 quantity 하나)에서 3안이 통째로
    거부되어 제안 자체를 만들지 못하는 것으로 반증됐다.
    """
    single_axis = run_purchase_agent(ITEM, FALLING)
    assert {s["strategy_type"] for s in single_axis["scenarios"]} == {"quantity"}
    assert len(single_axis["scenarios"]) == 3
    PurchaseProposal.model_validate(single_axis)  # 스키마는 더 이상 막지 않는다


def test_self_check_exempts_days_with_a_single_allowed_axis() -> None:
    """축이 하나뿐이면 전 안이 같은 축인 게 정상이다 — 이번 결정의 핵심."""
    scenarios = [{"label": "보수", "strategy_type": "quantity"}] * 3
    assert check_axis_diversity(scenarios, ["quantity"]) is None


def test_self_check_rejects_one_axis_when_more_were_available() -> None:
    """축이 여럿 열렸는데 전부 같은 축이면 "3안인데 사실 한 안"이다."""
    scenarios = [{"label": "보수", "strategy_type": "quantity"}] * 3
    reason = check_axis_diversity(scenarios, ["quantity", "timing"])
    assert reason is not None
    assert "동일 축" in reason


def test_multi_axis_day_spreads_scenarios_across_axes(proposals: dict) -> None:
    """축이 열린 날은 겹치지 않게 배분한다 — 공격안이 timing을 가져간다."""
    axes = {s["label"]: s["strategy_type"] for s in proposals[RISING]["scenarios"]}
    assert axes["공격"] == "timing"
    assert len(set(axes.values())) >= 2


def test_assign_axes_uses_only_allowed_values() -> None:
    labels = ["보수", "기본", "공격"]
    aggressive = load_constraints()["allocation"]["aggressive_axis"]
    for allowed in (["quantity"], ["quantity", "timing"]):
        assert set(assign_axes(labels, allowed, aggressive).values()) <= set(allowed)


# ── 출력 계약 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_output_survives_the_contract_revalidation(as_of: date, proposals: dict) -> None:
    """⑦이 부르는 ``revalidate_for_output``을 밖에서 한 번 더 확인한다 (출력 경계)."""
    proposal = PurchaseProposal.model_validate(proposals[as_of])
    assert revalidate_for_output(proposal).model_dump(mode="json") == proposals[as_of]


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_meta_reports_as_of_and_item(as_of: date, proposals: dict) -> None:
    meta = proposals[as_of]["meta"]
    assert meta["as_of"] == as_of.isoformat()
    assert meta["item"] == ITEM
    assert meta["is_refeed"] is False
    assert meta["feedback_attempt"] == 0


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_normal_run_cuts_nothing(as_of: date, proposals: dict) -> None:
    """정상 경로에서는 컷이 없다. 컷이 생기면 그건 우리 계산이 틀린 것이다."""
    assert proposals[as_of]["rejected_reasons"] == []
    assert "no_proposal_reason" not in proposals[as_of]


def test_situation_and_confidence_travel_together(forced: dict) -> None:
    """신뢰도는 상황을 따라간다 — 둘이 갈리면 소비자가 어느 쪽을 믿을지 알 수 없다.

    ③ 로 상황을 고정한다. 앵커가 아니라 **상황이** 신뢰도를 정한다는 것이 주제다.
    """
    assert forced["stable"][RISING]["confidence"] == "high"
    assert forced["uncertain"][UNCERTAIN]["confidence"] == "medium"


def test_context_docs_used_lists_only_documents_actually_loaded(forced: dict) -> None:
    """② 구현(E3-4) 후: uncertain에서만 문서가 실리고, stable한 날은 비어 있다.

    스텁 시절엔 "언제나 빈 목록"을 검사했다. 이제 **비어 있음이 노드를 안 돌았다는 뜻**이라,
    같은 필드가 두 상태를 구분한다 — 그게 이 필드의 존재 이유다.

    ③ 로 상황을 고정하므로 **날짜가 아니라 상황이** 두 상태를 가른다.

    🔴 ``["DOC-3","DOC-4","DOC-5"]`` 는 **9/4 의 성질이 아니다** — 8/5~9/4 아무 날이나
      같다 (코퍼스 실측: 8/21·8/28·9/4 전부 같고, 9/11 은 DOC-6 이 하나 더 보인다).
      여기서 잠그는 것은 **루프이지 날짜가 아니다.**
    """
    assert forced["uncertain"][UNCERTAIN]["context_docs_used"] == ["DOC-3", "DOC-4", "DOC-5"]
    # INTEGRATION(12-31)도 여기 든다. documents.json 은 절대 날짜(2026-08~09)라 그날
    # 가시 문서가 0건이지만, **stable 이라 ② 자체를 안 탄다** — 0건이어서 비는 것과
    # 안 돌아서 비는 것은 다르고, 여기서 검사하는 것은 뒤쪽이다.
    for as_of in (INTEGRATION, RISING, FALLING, SPREAD_WIDE):
        assert forced["stable"][as_of]["context_docs_used"] == []


def test_rejected_scenario_is_recorded_with_label_and_reason(proposals: dict) -> None:
    """컷 사유는 ``{label, reason}``으로 남는다 (§3 State v1.1 정정 · 출력 스키마와 동형).

    사중 일치를 깬 안 하나를 ⑦에 직접 먹여, 컷되고 사유가 남고 남은 안이 없으면
    ``no_proposal_reason``이 서는 것까지 한 번에 확인한다.
    """
    state = build_initial_state(ITEM, RISING)
    state.update(classify_situation(state))

    broken = dict(proposals[RISING]["scenarios"][0])
    broken["total_qty_kg"] += 1  # 수량 축을 깬다 — split·sourcing 합과 어긋난다
    state.update({"scenarios_final": [broken], "confidence": "high"})

    result = self_check(state)
    assert result["scenarios_final"] == []
    assert len(result["rejected_reasons"]) == 1
    cut = result["rejected_reasons"][0]
    assert set(cut) == {"label", "reason"}
    assert cut["label"] == broken["label"]
    assert "수량 불일치" in cut["reason"]
    assert result["proposal"]["no_proposal_reason"]


def test_assembled_proposal_raises_when_contract_is_broken() -> None:
    """조립 후에도 계약을 어기면 **직렬화하지 않고 터진다** — 조용한 통과가 없다."""
    proposal = run_purchase_agent(ITEM, RISING)
    broken = {**proposal, "situation": "uncertain"}  # uncertain인데 3안 + 공격안
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(broken)


# ── 검사가 실제로 무는가 — 위반 입력을 직접 먹인다 ──────────────────────────
#
# 정상 mock만 통과시키는 검사는 삭제해도 초록불이 뜬다. 아래는 전부 "걸려야 하는" 입력이다.


def _line(market: str = "가락", grade: str = "상", price: int = 1650, qty: int = 100) -> dict:
    return {"market": market, "grade": grade, "qty_kg": qty, "grade_unit_price": price}


def test_price_check_rejects_another_market_relabelled_as_garak() -> None:
    """다른 시장 가격에 ``market="가락"``을 붙여도 통과하면 규칙 4가 형식만 남는다."""
    scenario = {"sourcing_plan": [_line(price=999)]}
    reason = check_prices_exist(scenario, [{"market": "부산", "grade": "상", "price": 999}])
    assert reason is not None
    assert "당일 시세에 없는 단가" in reason


def test_price_check_rejects_a_non_garak_market_in_output() -> None:
    scenario = {"sourcing_plan": [_line(market="부산")]}
    reason = check_prices_exist(scenario, [{"market": "부산", "grade": "상", "price": 1650}])
    assert reason is not None
    assert "허용되지 않은 시장" in reason


def test_price_check_rejects_an_invented_price() -> None:
    scenario = {"sourcing_plan": [_line(price=1)]}
    reason = check_prices_exist(scenario, ports.get_market_quotes(ITEM, RISING))
    assert reason is not None


def test_max_price_is_a_hard_cut_but_contract_price_is_only_a_warning() -> None:
    """혼동하기 쉬운 두 값을 갈라놓는다 (규칙 5).

    ``max_price`` 초과는 컷, ``contract_price`` 초과는 margin_warning 표시일 뿐이다.
    """
    over_ceiling = {"sourcing_plan": [_line(price=2000)], "max_price": 1900}
    assert check_max_price(over_ceiling) is not None

    within = {"sourcing_plan": [_line(price=1650)], "max_price": 1900}
    assert check_max_price(within) is None

    # 계약단가(2,293)를 넘는 단가여도 max_price 안이면 컷되지 않는다
    above_contract = {"sourcing_plan": [_line(price=2400)], "max_price": 2500}
    assert check_max_price(above_contract) is None


def test_contract_price_excess_only_raises_margin_warning(proposals: dict) -> None:
    """정상 경로에서는 매입단가 < 계약단가라 경고가 서지 않는다."""
    for scenario in proposals[RISING]["scenarios"]:
        assert scenario["margin_warning"] is False
        assert scenario["expected_margin_rate"] > 0


def test_warehouse_and_cash_checks_reject_violating_scenarios() -> None:
    """③이 클립한 결과만 보면 ⑦의 검사를 지워도 통과한다 — 직접 위반 입력을 먹인다."""
    inventory = ports.get_inventory(ITEM, RISING)
    cap = inventory["warehouse_free_kg"] + inventory["rental_cap_kg"]
    assert check_warehouse_capacity({"total_qty_kg": cap}, inventory) is None
    assert check_warehouse_capacity({"total_qty_kg": cap + 1}, inventory) is not None

    constraints = load_constraints()
    state = build_initial_state(ITEM, RISING)
    budget = int(state["projected_cash_min"] * constraints["cash"]["max_purchase_ratio"])
    assert check_cash_ceiling({"total_amount_krw": budget}, state, constraints) is None
    assert check_cash_ceiling({"total_amount_krw": budget + 1}, state, constraints) is not None


def test_axis_and_split_checks_reject_violating_scenarios() -> None:
    assert check_axis_allowed({"strategy_type": "mix"}, ["quantity", "timing"]) is not None
    assert check_axis_allowed({"strategy_type": "timing"}, ["quantity", "timing"]) is None

    as_of = RISING.isoformat()
    assert check_split_dates({"split_plan": [{"seq": 1, "date": as_of}]}, as_of) is None
    assert check_split_dates({"split_plan": [{"seq": 1, "date": "2026-08-20"}]}, as_of) is not None
    two_rounds = {"split_plan": [{"seq": 1, "date": as_of}, {"seq": 3, "date": as_of}]}
    assert check_split_dates(two_rounds, as_of) is not None


def test_quadruple_match_catches_each_axis() -> None:
    base = {
        "total_qty_kg": 100,
        "total_amount_krw": 165000,
        "split_plan": [{"seq": 1, "date": "2026-08-21", "qty_kg": 100}],
        "sourcing_plan": [_line(qty=100)],
    }
    assert check_quadruple_match(base) is None
    off_total = {**base, "total_qty_kg": 101}
    assert "split" in check_quadruple_match(off_total)
    off_sourcing = {**base, "split_plan": [{"seq": 1, "date": "2026-08-21", "qty_kg": 101}]}
    assert check_quadruple_match({**off_sourcing, "total_qty_kg": 101}) is not None
    assert "금액" in check_quadruple_match({**base, "total_amount_krw": 1})


# ── 망가진 입력은 조용히 통과하지 않는다 ────────────────────────────────────


def test_empty_market_quotes_stops_with_a_named_error() -> None:
    """빈 시세에서 ``max()``가 터지면 무엇이 없어서인지 알 수 없다."""
    with pytest.raises(ValueError, match="market_quotes"):
        fixed_market_quotes([])
    with pytest.raises(ValueError, match="market_quotes"):
        reference_unit_price([{"market": "부산", "grade": "상", "price": 1650}], "상")


def test_short_forecast_horizon_stops_with_a_named_error() -> None:
    """지평이 판정일보다 짧으면 IndexError 대신 무엇이 모자란지 말한다."""
    short = {"daily": [{"upper": 2, "lower": 1, "predicted": 1}] * 13}
    with pytest.raises(ValueError, match=r"D\+14"):
        compute_ci_width(short, 14)


def test_zero_denominators_are_rejected_before_dividing() -> None:
    for value in (0, -1, None):
        with pytest.raises(ValueError, match="예시값"):
            require_positive(value, "예시값")


def test_sourcing_ratios_must_be_positive_and_sum_to_one() -> None:
    """검증이 없으면 작은 비율은 0kg이 되고 합이 1을 넘으면 마지막이 음수가 된다.

    마지막 줄이 잔량을 흡수하므로 사중 일치는 통과하고 스키마에서야 터진다 — 조용한 구간이다.
    """
    good = [{"market": "가락", "grade": "상", "ratio": 1.0, "grade_unit_price": 1650}]
    assert materialize_sourcing(100, good)[0]["qty_kg"] == 100

    with pytest.raises(ValueError, match="sum to 1"):
        materialize_sourcing(100, [{**good[0], "ratio": 0.5}])
    with pytest.raises(ValueError, match="ratio"):
        materialize_sourcing(100, [{**good[0], "ratio": 0.0}, {**good[0], "ratio": 1.0}])


def _staged_state(as_of: date = RISING, **overrides: object) -> dict:
    """③까지 돌린 상태에 ④ 스텁과 ⑤ 배분 결과를 얹은 것 — ⑥⑦만 따로 시험할 때 쓴다."""
    state = build_initial_state(ITEM, as_of)
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    state.update(split_plan(state))  # ④ 스텁 — 일괄
    # ⑤ — 등급 **비율**. 평시(RISING)엔 중품 스코어가 음수라 전량 상품 한 줄이다.
    state.update(allocate_sourcing(state))
    state.update(overrides)
    return state


def test_missing_contract_price_nulls_both_margin_fields() -> None:
    """계약단가 미수령이면 마진 두 값이 **함께 null**이다 (IO명세 §2 동기화 규칙).

    0.0·False로 채우지 않는다 — "마진 0%"와 "확인했더니 정상"은 둘 다 거짓이 된다 (규칙 3).
    """
    result = package_scenarios(_staged_state(contract_price=None))
    assert result["scenarios_final"]
    for scenario in result["scenarios_final"]:
        assert scenario["margin_warning"] is None
        assert scenario["expected_margin_rate"] is None


def test_zero_contract_price_still_stops() -> None:
    """``None``(미수령)과 ``0``(0원 계약가)은 다르다 — 후자는 잘못된 값이라 멈춘다.

    0을 미수령처럼 넘겨버리면 0과 NULL의 구분이 무너진다 (규칙 3).
    """
    with pytest.raises(ValueError, match="contract_price"):
        package_scenarios(_staged_state(contract_price=0))


def test_null_margin_pair_reaches_the_serialized_proposal() -> None:
    """⑦ 조립과 ``revalidate_for_output``을 지나서도 두 null이 살아남는가."""
    state = _staged_state(contract_price=None)
    state.update(package_scenarios(state))
    proposal = self_check(state)["proposal"]

    assert proposal["scenarios"]
    for scenario in proposal["scenarios"]:
        assert "margin_warning" in scenario
        assert scenario["margin_warning"] is None
        assert "expected_margin_rate" in scenario
        assert scenario["expected_margin_rate"] is None
    PurchaseProposal.model_validate(proposal)


def test_margin_pair_is_computed_when_contract_price_is_present(proposals: dict) -> None:
    """정상 경로에서는 둘 다 값이 있다 — 한쪽만 채워지는 경로가 없다."""
    for scenario in proposals[RISING]["scenarios"]:
        assert isinstance(scenario["margin_warning"], bool)
        assert isinstance(scenario["expected_margin_rate"], float)


def test_compute_margin_flags_excess_without_cutting() -> None:
    """계약단가 초과는 경고일 뿐 컷이 아니다 (규칙 5).

    역마진이면 실제 마진율은 음수지만 스키마가 ``ge=0``이라 0.0으로 깎인다 —
    그 사실은 ``margin_warning=True``가 전달한다.
    """
    assert compute_margin(1650, 2293) == (False, pytest.approx((2293 - 1650) / 2293))
    warning, rate = compute_margin(2400, 2293)
    assert warning is True
    assert rate == 0.0


# ── 규칙 3: NULL과 확정 0을 섞지 않는다 ─────────────────────────────────────


def test_missing_inventory_is_not_reported_as_zero_stock() -> None:
    """``lots=None``(미수신)과 ``lots=[]``(없음이 확정)은 다른 근거·등급으로 나간다."""
    from app.purchase_agent.nodes.package_scenarios import _inventory_claim

    missing_claim, _, missing_grade = _inventory_claim(None)
    empty_claim, _, empty_grade = _inventory_claim([])
    assert missing_grade == "ASSUMED"
    assert "미결" in missing_claim
    assert empty_grade == "SIM_FIXED"
    assert "확정" in empty_claim
    assert missing_claim != empty_claim


def test_all_plans_clipped_to_zero_yield_no_proposal_with_reasons() -> None:
    """하드 제약이 전량을 깎으면 예외가 아니라 "제안 불가"가 나와야 한다.

    0으로 나누며 중단되면 컷 사유도 ``no_proposal_reason``도 만들어지지 않는다.
    """
    state = build_initial_state(ITEM, RISING)
    state.update(classify_situation(state))
    state["confirmed_orders"] = {**state["confirmed_orders"], "total_kg": 0}
    state.update(draft_plan(state))
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))

    assert state["scenarios_final"] == []
    assert {item["label"] for item in state["rejected_reasons"]} == {"보수", "기본", "공격"}
    result = self_check(state)
    assert result["proposal"]["scenarios"] == []
    assert "제안 불가" in result["proposal"]["no_proposal_reason"]
