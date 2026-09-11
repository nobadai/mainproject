"""③ 보유 재고 차감 — **수요 쪽이지 상한 쪽이 아니다** (상세설계 §4-③ · E4-8).

마스터 통보 「매입이 보유 재고를 안 빼고 매일 커버리지만큼 삽니다」에 대한 조항을 잠근다.
같은 폴더의 다른 검사들은 ``no_holdings`` 로 이 축을 **끄고** 돈다 — 여기가 **켜고 재는**
유일한 자리다.

```text
raw_qty = round(일평균 × D) − 차감보유
        → 그 뒤에 창고 · 현금 · 신선도 · 조정안 클립      ← 순서가 뜻이다

차감보유 = min( Σ_lot min(available_qty_kg, 일평균 × min(잔여신선도, D)),  일평균 × D )
```

🔴 **여기서 재는 것은 「식이 도는가」다.** 「식이 맞는가」는 실 DB 로만 잰다 — 완주 걷기
``SIM-WALK-2026-V4`` 봉투 168셀에 이 식을 대면 162안 중 **142안이 통째로 0**, 14안이 부분
차감, 6안이 보유 0 이다 (2026-09-11 실측). mock 으로 그 결론을 대신하지 않는다.
"""

from datetime import date
from typing import Any

import pytest

from app.purchase_agent import ports
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.nodes.draft_plan import usable_holdings_kg
from app.purchase_agent.schemas import PurchaseProposal

ITEM = "배추"
#: mock_rising 앵커. 3안이 다 서는 날이라 차감이 안별로 어떻게 갈리는지 보인다.
AS_OF = date(2026, 8, 21)

def _daily_demand() -> float:
    """mock 일평균 확정수요.

    🔴 **되잡지 않고 ③이 쓰는 그 함수를 그대로 부른다** (규칙 7·8). 여기에 `1285.7` 을
      적어 두면 mock 주문이 바뀌는 날 이 파일만 조용히 틀린다 — 차감의 분모가 일평균이라
      그 오차가 전 단언에 실린다.
    """
    from app.purchase_agent.nodes.classify_situation import estimate_daily_demand
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state(ITEM, AS_OF)
    return estimate_daily_demand(state["confirmed_orders"], load_constraints())


def _lot(qty: float, freshness: int, **over: Any) -> dict:
    """물류가 싣는 로트 모양 그대로 (``lot_id``·``item``·``grade``·``status`` 포함)."""
    return {
        "lot_id": f"LOT-TEST-{qty:.0f}-{freshness}",
        "item": ITEM,
        "grade": "상",
        "status": "ACTIVE",
        "available_qty_kg": qty,
        "remaining_freshness_days": freshness,
        **over,
    }


def _with_lots(monkeypatch: pytest.MonkeyPatch, lots: list[dict]) -> None:
    """보유를 **검사가 직접 준다.** mock 파일은 안 건드린다."""
    original = ports.get_inventory

    def patched(item: str, as_of: date) -> dict:
        return {**original(item, as_of), "lots": lots}

    monkeypatch.setattr("app.purchase_agent.ports.get_inventory", patched)


def _coverage(label: str) -> int:
    return load_constraints()["coverage_days"]["by_label"][label]


def _rejected(proposal: dict, label: str) -> dict:
    return next(r for r in proposal["rejected_reasons"] if r["label"] == label)


# ── ① 동작 변화를 수로 잠근다 ───────────────────────────────────────────────


def test_holdings_shrink_the_plan_by_exactly_what_they_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 **이 판의 본체다** — 「원안 X 였는데 보유 Y 라 Z 가 됐다」를 수로 단언한다.

    ``no_holdings`` 로 21개 검사의 결합을 끊었는데, 그것만 하고 끝내면 **보유가 수요를
    덮는다는 진짜 동작 변화를 아무도 안 재게 된다.** 그 자리가 여기다.

    보유 1,000kg · 잔여신선도 30일이라 커버 창(D=5)을 다 덮고도 남는다 — 그래서 차감이
    **로트 수량 그대로** 1,000kg 이고, 기본안은 원수요에서 딱 그만큼 준다.
    """
    daily = _daily_demand()
    days = _coverage("기본")
    demand = round(daily * days)

    _with_lots(monkeypatch, [_lot(1000, 30)])
    plan = next(s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본")

    assert demand > 1000, "차감이 원수요를 다 덮으면 이 검사는 ②의 것이 된다"
    assert plan["total_qty_kg"] == demand - 1000


def test_the_same_day_without_holdings_buys_the_whole_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """짝 검사 — 같은 날 보유가 0이면 원수요를 그대로 산다. 차감이 **원인**임을 잠근다."""
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    _with_lots(monkeypatch, [])
    plan = next(s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본")

    assert plan["total_qty_kg"] == demand


# ── ② 0 의 뜻을 가른다 ──────────────────────────────────────────────────────


def test_holdings_that_cover_the_window_say_not_needed_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 조항 §4-③-3 의 핵심 — **막힌 것과 필요 없는 것을 가른다.**

    보유가 커버 D일 수요를 다 덮으면 그 안은 «하드 제약으로 0까지 축소» 가 **아니다**.
    창고를 비우거나 한도를 늘려야 하는 상태가 아니라, 아무것도 안 해도 되는 상태다.
    """
    _with_lots(monkeypatch, [_lot(10**6, 30)])
    proposal = run_purchase_agent(ITEM, AS_OF)

    assert proposal["scenarios"] == []
    kinds = {r["label"]: r["kind"] for r in proposal["rejected_reasons"]}
    assert set(kinds.values()) == {"not_needed"}, kinds
    assert "보수" in kinds and "기본" in kinds


def test_the_not_needed_sentence_reads_as_korean_not_as_a_field_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 이 문장은 H1 화면과 Critic 이 **그대로** 읽는다 — 내부 이름을 흘리지 않는다.

    ``kind`` 만 붙이고 문장을 안 고치면 데이터에는 갈라져 있는데 **보는 사람에게는 안
    갈린 상태**가 된다. 화면·마스터 리포트가 그리는 것은 ``reason`` 뿐이다.
    """
    _with_lots(monkeypatch, [_lot(10**6, 30)])
    reason = _rejected(run_purchase_agent(ITEM, AS_OF), "보수")["reason"]

    assert "보유 재고" in reason
    assert "매입이 필요 없다" in reason
    assert "하드 제약" not in reason, "막힌 것으로 읽히면 조치가 달라진다"
    for internal in ("not_needed", "blocked", "raw_qty", "kind"):
        assert internal not in reason


def test_a_hard_cap_still_says_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """반대 방향 — 보유가 없고 창고가 0이면 그대로 ``blocked`` 다.

    갈래를 넣으면서 **원래 문장이 사라지지 않았는지**를 같이 본다. 한쪽만 잠그면
    새 갈래가 옛 갈래를 덮어도 아무도 모른다.
    """
    original = ports.get_inventory

    def no_space(item: str, as_of: date) -> dict:
        return {**original(item, as_of), "lots": [], "warehouse_free_kg": 0, "rental_cap_kg": 0}

    monkeypatch.setattr("app.purchase_agent.ports.get_inventory", no_space)
    proposal = run_purchase_agent(ITEM, AS_OF)

    assert proposal["scenarios"] == []
    reason = _rejected(proposal, "보수")
    assert reason["kind"] == "blocked"
    assert "하드 제약(창고)" in reason["reason"]


# ── ③ 부분 차감 ────────────────────────────────────────────────────────────


def test_partial_cover_leaves_the_remainder(monkeypatch: pytest.MonkeyPatch) -> None:
    """보유가 창을 **일부만** 덮으면 나머지를 산다 — 0 도 아니고 원수요도 아니다.

    실측에서 이 갈래가 162안 중 **14안**뿐이라(2026-09-11 · 완주 걷기) 가운데가 얇다.
    얇다고 안 잠그면 «전부 아니면 전무» 로 굳는 것을 아무도 못 본다.
    """
    daily = _daily_demand()
    days = _coverage("보수")
    demand = round(daily * days)
    holding = demand // 2

    _with_lots(monkeypatch, [_lot(holding, 30)])
    plan = next(s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "보수")

    assert 0 < plan["total_qty_kg"] < demand
    assert plan["total_qty_kg"] == demand - holding


# ── ④ 창을 인지한다 — 단순 합계가 아니다 ───────────────────────────────────


def test_a_lot_that_expires_first_only_covers_its_own_window() -> None:
    """🔴 **잔여신선도가 D 보다 짧은 로트는 그 창까지만 센다.**

    단순 합계는 *"5,000kg 있으니 5일치를 덮는다"* 고 세는데, 내일 상하는 물건은 내일까지
    밖에 못 덮는다. 그 둘이 갈리는 자리를 수로 잠근다.
    """
    daily = 1000.0
    days = 5
    lots = [{"available_qty_kg": 5000, "remaining_freshness_days": 1}]

    assert usable_holdings_kg(lots, daily, days) == 1000.0  # 창 인지 = 하루치
    assert sum(x["available_qty_kg"] for x in lots) == 5000  # 단순 합계는 닷새치


def test_the_deduction_never_exceeds_the_window_demand() -> None:
    """보유가 아무리 많아도 **커버 창 수요보다 더 빼지 않는다** — 음수 수요를 안 만든다."""
    huge = [{"available_qty_kg": 10**9, "remaining_freshness_days": 999}]
    assert usable_holdings_kg(huge, 100.0, 5) == 500.0


def test_a_lot_missing_either_field_is_skipped_not_zero_filled() -> None:
    """규칙 3 — 모르는 로트는 **세지 않는다.** 0으로 채우면 미결이 사실이 된다.

    ⚠️ 건너뛰는 것은 차감을 **적게 잡는 쪽**이라 안전한 방향이다. 반대로 큰 수로 채우면
      없는 재고를 빼서 살 것을 안 사게 된다.
    """
    daily, days = 100.0, 5
    assert usable_holdings_kg([{"available_qty_kg": 300}], daily, days) == 0.0
    assert usable_holdings_kg([{"remaining_freshness_days": 9}], daily, days) == 0.0
    half_known = [{"available_qty_kg": None, "remaining_freshness_days": 9}]
    assert usable_holdings_kg(half_known, daily, days) == 0.0
    assert usable_holdings_kg(None, daily, days) == 0.0


# ── ⑤ 보유는 상한이 아니다 ─────────────────────────────────────────────────


def test_holdings_never_appear_as_a_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 조항 §4-③-1 — ``caps`` 에 넣으면 ``clipped_by`` 에 「보유」가 실리고 ⑥이
    *"하드 제약(보유)으로 …"* 를 낸다. **거짓 문장이다** — 보유는 밖에서 씌운 천장이
    아니라 필요가 줄어든 것이다.

    ③의 ``clipped_by`` 를 **직접** 본다. 문장으로만 보면 *"보유"* 라는 낱말이 다른 고지에도
    쓰이므로(리드타임 고지가 그렇다) 새는 자리를 못 짚는다.
    """
    from app.purchase_agent.nodes.classify_situation import classify_situation
    from app.purchase_agent.nodes.draft_plan import draft_plan
    from app.purchase_agent.state import build_initial_state

    _with_lots(monkeypatch, [_lot(500, 30)])
    state = build_initial_state(ITEM, AS_OF)
    state.update(classify_situation(state))
    drafts = draft_plan(state)["base_plan"]["drafts"]

    assert any(d["deducted_holdings_kg"] > 0 for d in drafts), "차감이 안 걸리면 공허한 검사다"
    named = {clip["constraint"] for draft in drafts for clip in draft["clipped_by"]}
    assert named <= {"창고", "현금", "신선도", "조정안"}, named

    # 그리고 화면으로 나가는 문장에도 «하드 제약(…보유…)» 이 없어야 한다.
    for rejected in run_purchase_agent(ITEM, AS_OF)["rejected_reasons"]:
        assert "보유" not in rejected["reason"].split("하드 제약(")[-1].split(")")[0]


def test_the_proposal_still_validates_with_the_new_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kind`` 가 늘어도 출력 스키마가 그대로 선다 (``extra="forbid"`` 인 모델이다)."""
    _with_lots(monkeypatch, [_lot(10**6, 30)])
    PurchaseProposal.model_validate(run_purchase_agent(ITEM, AS_OF))


# ── ⑥ 리드타임이 미결이면 남긴다 ───────────────────────────────────────────


def test_an_unresolved_lead_time_is_disclosed_when_holdings_were_deducted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """규칙 3 — 도착일을 못 놓으면 **보유가 덮는 창과 매입이 덮는 창이 같은지 못 맞춘다.**

    차감은 하고, 그 사실을 ``risks`` 에 남긴다. 0으로 채우지 않는다.
    """
    _with_lots(monkeypatch, [_lot(500, 30)])
    scenario = run_purchase_agent(ITEM, AS_OF)["scenarios"][0]

    assert any("보유 재고를 뺀 창" in risk for risk in scenario["risks"])


def test_no_deduction_means_no_such_disclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    """보유가 0인 날엔 그 고지를 안 낸다 — **없는 일에 사과하지 않는다.**"""
    _with_lots(monkeypatch, [])
    scenario = run_purchase_agent(ITEM, AS_OF)["scenarios"][0]

    assert not any("보유 재고를 뺀 창" in risk for risk in scenario["risks"])
