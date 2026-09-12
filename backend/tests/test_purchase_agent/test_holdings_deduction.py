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
from app.purchase_agent.nodes.draft_plan import FreeStock, free_stock_for, usable_holdings_kg
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


def _with_lots_and_free_stock(
    monkeypatch: pytest.MonkeyPatch, lots: list[dict], rows: list[dict] | None
) -> None:
    """로트와 물류 **가용재고 집계**를 같이 준다. ``rows=None`` 이면 칸 자체를 안 싣는다.

    ★ 둘을 한 함수로 둔 이유 — 패치 대상이 ``ports.get_inventory`` 하나라 따로 걸면
      뒤엣것이 앞엣것을 덮는다.

    🔴 **이 입력은 합성이다 — mock 에 예약 축이 없다** (2026-09-12 실측).
      ``mocks/inventory.json`` 이 싣는 것은 ``lots``·``warehouse_free_kg``·``rental_cap_kg``
      셋뿐이고 ``inventory_by_item`` 은 **아예 없다**. 검사 폴더 전체에도 그 이름이 0건이다.

      ⚠️ 그래서 **이 파일 밖의 검사는 클램프를 타지 않는다** — 아무도 그 칸을 안 주므로
        ``free_stock_kg`` 가 ``None`` 이고 차감이 종전 그대로다. 이 판이 기존 검사를
        안 깨뜨리는 이유가 그것이다.

      🔴 **mock 파일에 넣지 않았다.** 넣으면 앵커가 움직여 이 폴더 수십 개 검사의 기준이
        같이 갈린다 — 「mock 앵커를 실측으로 쓰지 않는다」와 같은 자리다. 넣을지는 별건이다.

    ★ 물류가 싣는 모양 그대로다 — ``{"item": …, "available_qty_kg": …}`` 두 칸
      (V6 봉투 495항목 실측 · 칸이 그 둘뿐이고 **신선도 축이 없다**).
    """
    original = ports.get_inventory

    def patched(item: str, as_of: date) -> dict:
        inventory = {**original(item, as_of), "lots": lots}
        if rows is None:
            inventory.pop("inventory_by_item", None)
        else:
            inventory["inventory_by_item"] = rows
        return inventory

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

    🟡 **mock 은 가용재고를 안 보낸다**(아래 ⑦·⑧ 절 참조)라 이 날은 수 하나짜리 문면이다.
      「보유 재고」를 단언하던 종전 줄을 「보유」로 고쳤다 — 그 낱말이 빠진 것이 아니라
      **같은 수를 두 번 적던 자리가 사라졌다** (`_no_quantity_reason` docstring).
    """
    _with_lots(monkeypatch, [_lot(10**6, 30)])
    reason = _rejected(run_purchase_agent(ITEM, AS_OF), "보수")["reason"]

    assert "보유" in reason
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


# ── ④ 만료 로트 — 🔴 부호가 뒤집히지 않는다 (`#584` 회귀) ────────────────────
#
# ``min(잔여신선도, D)`` 만 쓰면 음수 신선도가 ``일평균 × 음수`` 로 들어가 **차감이
# 음수**가 되고, ③이 ``원수요 − 차감`` 을 하므로 **원수요보다 더 사게 된다.**
#
# ⚠️ **원장에 이미 있다** — 로트 26,967건 중 잔여신선도 음수 18,944건, 차감이 음수가
#   되는 셀 1,390 / 1,701 (2026-09-11 실 DB 전수).
#
# ★ **「0건이라 안전」이 아니라 「달력이 맞아떨어져 안 걸렸다」이다.** 폐기 임계가
#   ``<= 0`` 이라 제때 돌면 0에 닿은 날 걷히는데, 그건 **방어로 설계된 것이 아니라
#   가용재고 제외 기준을 재사용한 것**이다. 하루 밀리면 음수가 우리한테 온다.


def test_an_expired_lot_adds_nothing_to_the_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **이 회귀의 본체다** — 만료 로트가 섞여도 차감은 성한 로트 몫 그대로다.

    고치기 전에는 이 조합이 차감 **−16,435kg** 을 내서 사는 양이 `3,587 → 20,022kg` 으로
    부풀었다. 화면에는 「하드 제약(창고)으로 축소」로 보인다 — **아무도 못 찾는다.**
    """
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    _with_lots(monkeypatch, [_lot(1000, 30), _lot(200, -25)])
    plan = next(s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본")

    assert plan["total_qty_kg"] == demand - 1000, "만료 로트가 차감에 끼어들었다"
    assert plan["total_qty_kg"] < demand, "차감이 부호가 뒤집혀 원수요보다 더 산다"


def test_all_lots_expired_buys_the_plain_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    """전부 만료면 차감 0 — **원수요 그대로**다. 「보유가 없다」와 같은 자리다."""
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    _with_lots(monkeypatch, [_lot(800, -3), _lot(500, -40)])
    plan = next(s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본")

    assert plan["total_qty_kg"] == demand


def test_the_deduction_is_never_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """차감은 **절대 음수가 아니다.** 바깥 ``max(0.0, ...)`` 이 무는 자리다.

    ⚠️ ``available_qty_kg`` 가 음수로 오는 날을 같이 막는다 — 지금 원장엔 0건이지만
      안쪽 클램프만으로는 그 축이 안 막힌다.
    """
    daily = _daily_demand()
    for lots in (
        [_lot(200, -25)],
        [_lot(10**6, -1)],
        [_lot(-500, 30)],
        [_lot(-500, -30), _lot(100, 2)],
    ):
        for days in (2, 5, 12):
            assert usable_holdings_kg(lots, daily, days) >= 0.0, lots


def test_a_lot_that_expired_while_the_walk_skipped_a_day_deducts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 **지연 폐기** — 걷기가 안 걸은 날에 0에 닿은 로트는 다음 걸은 날 **음수**로 온다.

    물류 폐기는 걷기가 그날을 걸을 때 돈다. 잔여신선도가 `0` 에 닿은 날을 걷기가 건너뛰면
    그 로트는 걷히지 않고, **다음 걷는 날 `-1` 로 우리 봉투에 실린다.**

    ```text
    D-1  잔여 1     정상
    D    잔여 0     🔴 걷기가 이날을 안 걸었다 → 폐기가 안 돈다
    D+1  잔여 -1    ← 이 값이 차감식에 들어온다
    ```

    ★ **실측으로 갈렸다** — `SIM-CHAIN-V1` 폐기 4건은 **전부 걷은 날**(금·화·월·화)이라
      한 번도 음수가 안 왔고, `SIM-WALK-2026-V4` 는 지연이 **16건**이었다. 같은 달력이면
      발화한다. 🔴 V4 가 무해했던 것은 그때가 `#584` **이전이라 차감 자체가 없어서**다.

    ⚠️ 잰 방법의 한계 — 보유 여부를 봉투가 아니라 `inventory_moves` 로 복원했다.
    """
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    on_time = [_lot(1000, 30), _lot(900, 0)]  # 걷은 날 — 0 에서 폐기된다
    delayed = [_lot(1000, 30), _lot(900, -1)]  # 하루 밀린 날 — 음수로 온다

    assert usable_holdings_kg(on_time, daily, 5) == usable_holdings_kg(delayed, daily, 5), (
        "폐기가 하루 밀렸다는 이유만으로 차감이 달라진다"
    )

    _with_lots(monkeypatch, delayed)
    plan = next(s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본")
    assert plan["total_qty_kg"] == demand - 1000


# ── ⑤ 가용재고 클램프 — 「이미 팔린 몫을 우리 것으로 세지 않는다」 ──────────────
#
# 🔴 **고친 판의 본체다** (2026-09-12). ``lots[].available_qty_kg`` 는 물류 repository 가
#   ``remaining_qty_kg`` 를 그대로 싣는 **물리 잔량**이라 이미 팔린 몫이 섞여 있다.
#   예약을 뺀 값은 같은 봉투에 따로 오는 ``inventory_by_item`` 이고, 우리는 그것을 안 읽고
#   있었다 — 두 칸 **이름이 둘 다 ``available_qty_kg``** 라서 아무도 못 봤다.
#
# 실측(V6 봉투 171셀 · 2026-09-12): 「필요 없다」 보류 36건 중 **15건**이 이미 팔린 재고로
# 판단한 것이었고, 최악은 `2026-03-11 무` — 보유 551kg 으로 읽고 안을 0개 냈는데 물류
# 가용재고는 **0kg** 이었다.
#
# ⚠️ 아래 입력은 **전부 합성이다** — ``_with_free_stock`` docstring 참조.


def test_the_free_stock_clamp_actually_bites(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 로트는 1,000kg 인데 물류 가용재고가 300kg 이면 **차감은 300kg 이다.**

    차이가 700kg 이고 그만큼 **더 산다** — 그 700kg 은 이미 팔린 몫이라 우리가 못 쓴다.
    """
    daily = _daily_demand()
    days = _coverage("기본")
    demand = round(daily * days)

    _with_lots_and_free_stock(
        monkeypatch,
        [_lot(1000, 30)],
        [{"item": ITEM, "available_qty_kg": 300}],
    )
    plan = next(s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본")

    assert demand > 1000, "차감이 원수요를 다 덮으면 이 검사가 재는 것이 사라진다"
    assert plan["total_qty_kg"] == demand - 300


def test_without_the_clamp_the_already_sold_stock_would_be_counted_as_ours() -> None:
    """짝 검사 — 클램프를 **안 주면** 종전처럼 로트 합을 센다. 클램프가 **원인**임을 잠근다.

    🔴 이 두 줄이 갈리는 것이 이 판 전체의 동작 변화다. 같은 로트·같은 창인데 답이 다르다.
    """
    daily = _daily_demand()
    lots = [_lot(1000, 30)]

    assert usable_holdings_kg(lots, daily, 5) == 1000
    assert usable_holdings_kg(lots, daily, 5, 300) == 300


def test_the_window_still_wins_when_it_is_the_smaller_bound() -> None:
    """🔴 **집계로 창을 대신하지 않는다.** 신선도가 짧으면 가용재고가 커도 그만큼만 덮는다.

    잔여신선도 1일 로트는 창(D=5)을 하루만 덮으므로 차감이 ``일평균 × 1`` 이다. 가용재고를
    크게 줘도 그 수가 이기면 안 된다 — 이기면 **짧은 로트를 전부 덮는다고 세는 것**이다.

    ★ 실측(V6 로트 1,134개)에서 **750개가 12일보다 짧다.** 집계만 쓰는 갈래로 가면 예약이
      **없는** 109셀 중 19셀이 깎여 −16,878kg 이 된다 — 멀쩡한 셀을 건드리는 쪽이다.
    """
    daily = _daily_demand()

    one_day = usable_holdings_kg([_lot(10**6, 1)], daily, 5, 10**6)
    assert one_day == pytest.approx(daily * 1)


def test_a_missing_aggregate_is_not_read_as_zero_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 칸이 안 오면 **클램프를 걸지 않고 고지한다** (규칙 3).

    0 으로 메우면 차감이 0 이 되어 **원수요를 통째로 산다** — 모르는 것이 판정을 만드는
    자리다. 안 거는 쪽은 *아는 것만 쓰는 것*이라 값을 지어내지 않는다.

    ⚠️ 그리고 **못 본 사실이 화면에 남아야 한다.** 안 남기면 「예약을 반영한 차감」과
      「반영 못 한 차감」이 같은 모양으로 나가고, 둘을 아무도 못 가른다.
    """
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    _with_lots_and_free_stock(monkeypatch, [_lot(1000, 30)], None)
    scenario = next(
        s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본"
    )

    assert scenario["total_qty_kg"] == demand - 1000, "클램프가 안 걸려야 한다"
    assert any("이미 팔린 몫을 뺀 재고 확인 보류" in risk for risk in scenario["risks"])


def test_a_zero_aggregate_is_a_settled_zero_not_a_missing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🟢 ``0`` 으로 실려 오면 **확정된 0** 이다 — 차감이 0 이고 고지는 안 한다.

    로트는 1,000kg 인데 전량이 팔린 날이다. 그날 우리가 쓸 수 있는 것은 없으므로 원수요를
    그대로 산다. 🔴 위 검사와 **결과가 같고 뜻이 반대다** — 그래서 고지 유무로 가른다.
    """
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    _with_lots_and_free_stock(
        monkeypatch, [_lot(1000, 30)], [{"item": ITEM, "available_qty_kg": 0}]
    )
    scenario = next(
        s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본"
    )

    assert scenario["total_qty_kg"] == demand
    assert not any("이미 팔린 몫을 뺀 재고 확인 보류" in risk for risk in scenario["risks"]), (
        "0 은 확정된 답이다 — 「못 봤다」로 적으면 규칙 3 이 반대로 깨진다"
    )


def test_an_aggregate_that_omits_an_item_with_lots_is_a_contradiction_not_a_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 집계에 내 품목이 없는데 **내 로트는 있는** 날 — 모순이다. 0 으로 읽지 않는다.

    로트가 있으면 집계에도 (0 이라도) 실려야 한다. 안 실렸으면 둘 중 하나가 틀린 것이고,
    그때 조용히 0 으로 읽으면 차감이 0 이 되어 원수요를 통째로 산다.

    🟡 **원장에는 아직 0건이다** (V6 171셀 중 내 품목 항목이 없는 6셀은 로트도 전부 0건).
      방어로 둔 가지이고, 그 사실을 코드 docstring 에도 적었다.

    🔴 **그리고 「다른 품목 로트」로 발화하면 안 된다** — 봉투의 ``lots`` 는 전 품목이 섞여
      오므로 품목을 안 거르면 *"배추 집계가 없는데 무 로트가 있다"* 가 모순으로 읽힌다.
      고친 함수에 V6 봉투를 대서 실제로 그 허위 3건을 잡았다. 아래 짝 단언이 그 자리다.
    """
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    _with_lots_and_free_stock(
        monkeypatch, [_lot(1000, 30)], [{"item": "양파", "available_qty_kg": 500}]
    )
    scenario = next(
        s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본"
    )

    assert scenario["total_qty_kg"] == demand - 1000, "모순인 날 클램프를 걸면 안 된다"
    assert any("두 값이 어긋난다" in risk for risk in scenario["risks"])


def test_an_item_with_no_lots_and_no_entry_is_a_settled_zero() -> None:
    """🟢 로트도 없고 집계에도 없으면 **둘이 일치한다** — 재고가 없는 날이다.

    그때는 0 으로 읽는 것이 값을 지어내는 것이 아니다. 두 칸이 같은 말을 하고 있다.
    """
    assert free_stock_for({"lots": [], "inventory_by_item": []}, ITEM) == FreeStock(kg=0.0)


def test_another_items_lot_is_not_read_as_a_contradiction() -> None:
    """🔴 짝 검사 — **다른 품목 로트로 모순이 발화하면 안 된다.**

    봉투의 ``lots`` 는 전 품목이 섞여 오고 ``absorb_inventory`` 가 거른 뒤에야 이 품목 것이
    된다. 거르지 않으면 *"배추 집계가 없는데 무 로트가 있다"* 가 모순으로 읽혀 **고지가
    허위로 선다** — 고친 함수에 V6 봉투를 대서 실제로 그 3건을 잡았다.
    """
    envelope = {
        "lots": [{**_lot(900, 30), "item": "무"}],
        "inventory_by_item": [{"item": "무", "available_qty_kg": 900}],
    }
    assert free_stock_for(envelope, ITEM) == FreeStock(kg=0.0)

    with_own_lot = {**envelope, "lots": [*envelope["lots"], _lot(500, 30)]}
    assert free_stock_for(with_own_lot, ITEM).unknown_reason is not None, (
        "내 품목 로트가 섞이면 그때는 진짜 모순이다"
    )


# ── ⑥ 사유 문구 — 같은 수를 두 번 적지 않는다 ──────────────────────────────


def test_the_not_needed_sentence_names_what_covered_the_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 종전 문장은 **같은 수를 두 번** 적었다 — 걷기 8판 1,384건 중 1,384건.

    조건이 ``raw_qty ≤ 0`` 이고 차감이 ``일평균 × D`` 로 클램프되므로 그때
    ``round(차감) == demand_qty`` 가 **반드시** 성립한다. 우연이 아니라 구조였다::

        보유 재고 44kg이 커버 2일 수요 44kg을 이미 덮어 …

    🟢 가용재고를 받은 날은 **그 수**를 적는다. 수요와 같을 이유가 없어 동어반복이 사라진다.
    """
    daily = _daily_demand()
    days = _coverage("보수")
    demand = round(daily * days)

    _with_lots_and_free_stock(
        monkeypatch,
        [_lot(10**6, 30)],
        [{"item": ITEM, "available_qty_kg": 10**6}],
    )
    reason = _rejected(run_purchase_agent(ITEM, AS_OF), "보수")["reason"]

    assert "확정 출고 예약분을 뺀 가용재고" in reason, "봉투가 쓰는 낱말을 그대로 쓴다"
    assert f"{10**6:,}kg" in reason
    assert f"{demand:,}kg" in reason
    assert reason.count(f"{demand:,}kg") == 1, "같은 수가 두 번 나오면 동어반복으로 되돌아간다"
    for internal in ("not_needed", "blocked", "raw_qty", "free_stock", "inventory_by_item"):
        assert internal not in reason


def test_the_sentence_falls_back_to_one_number_when_the_aggregate_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🟡 못 받은 날은 **수 하나**다 — 「못 봤다」와 「덮었다」를 같은 문면으로 내지 않는다.

    ⚠️ 가용재고를 모르는데 그 자리에 다른 수를 적으면, 읽는 사람은 **물류가 확인해 준 수**로
      읽는다. 못 본 사실은 ``risks`` 가 따로 말한다.
    """
    daily = _daily_demand()
    demand = round(daily * _coverage("보수"))

    _with_lots_and_free_stock(monkeypatch, [_lot(10**6, 30)], None)
    reason = _rejected(run_purchase_agent(ITEM, AS_OF), "보수")["reason"]

    assert "가용재고" not in reason, "못 본 값을 확인된 수처럼 적으면 안 된다"
    assert reason.count("kg") == 1, "수가 하나여야 한다"
    assert f"{demand:,}kg" in reason


# ── ⑦ 대조 — 「있을 수 없는 방향」만 고지한다 ─────────────────────────────────
#
# 🔴 **「두 값이 다르다」는 고지하지 않는다.** 예약이 걸리면 로트 합(물리 잔량)과 집계(예약
#   뺀 값)가 **당연히** 다르다 — 실측으로 V6 **62 / 171셀** · V7 **50 / 171셀** 이 그렇다.
#   그 차이를 위험으로 적으면 셀 셋 중 하나에 매번 리스크 줄이 서고, 「예약이 있다」를
#   고장으로 읽게 만든다.
#
#   ★ 그리고 **문턱으로 줄지도 않는다** — 같은 실측에서 `>0` 이 62셀인데 `≥10kg` 도 61셀,
#     `≥100kg` 이 46셀이다. 갈릴 때는 크게 갈린다 (중앙값 무 309kg · 배추 846kg ·
#     양파 15kg · 최소 비율 22.3%). 걸러낼 잡음이 없으니 문턱은 수만 깎고 뜻을 안 준다.
#
# 🟢 **그래서 보는 것은 부등호의 방향이다.** 집계는 로트 합에서 예약·만료·비-ACTIVE 를 뺀
#   값이라 그 합을 넘을 수 없다. 실측 — 실행 `SIM-CHAIN-V1`~`V7` 봉투 **1,086셀 전수에서
#   0건**이다 (2026-09-12 16:3x).
#
#   ★★ **안 우는 것이 정상이고, 우는 날이 고장이다.** 그것이 이 검사가 「있는데 안 무는
#     검사」가 아니라는 근거다 — 한 번 울면 그 자체가 남의 정의가 바뀐 증거다.


def test_an_aggregate_larger_than_the_lots_is_impossible_and_is_disclosed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 집계가 로트 합보다 크면 **고지하고 클램프를 걸지 않는다.**

    그 방향이면 클램프가 상한으로 안 듣는다 — 없는 재고를 쓸 수 있다고 세게 된다.
    """
    daily = _daily_demand()
    demand = round(daily * _coverage("기본"))

    _with_lots_and_free_stock(
        monkeypatch,
        [_lot(1000, 30)],
        [{"item": ITEM, "available_qty_kg": 1500}],  # 🔴 로트 1,000 보다 크다
    )
    scenario = next(
        s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본"
    )

    assert scenario["total_qty_kg"] == demand - 1000, "클램프를 걸면 안 된다"
    assert any("로트 합보다 커서" in risk for risk in scenario["risks"])


def test_a_reserved_gap_is_normal_and_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """🟢 짝 검사 — **예약이 있어 두 값이 다른 것은 고지하지 않는다.**

    이쪽이 V6 62셀 · V7 50셀 에 해당하는 정상 상태다. 여기서 고지가 서면 그 셀 전부에
    리스크 줄이 서고, 위 검사가 잡으려는 **진짜 고장이 그 사이에 묻힌다.**
    """
    _with_lots_and_free_stock(
        monkeypatch,
        [_lot(1000, 30)],
        [{"item": ITEM, "available_qty_kg": 300}],  # 🟢 예약 700kg — 정상
    )
    scenario = next(
        s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본"
    )

    assert not any("어긋난다" in risk for risk in scenario["risks"]), (
        "예약이 있는 날마다 고지가 서면 셀 셋 중 하나가 빨간불이 된다"
    )
    assert not any("커서" in risk for risk in scenario["risks"])


def test_the_comparison_is_on_direction_not_size() -> None:
    """🟡 같은 크기 차이인데 **방향만 반대**인 두 경우가 다르게 나온다.

    ``3,587`` 과 ``1,000`` 의 차는 둘 다 ``2,587kg`` 인데, 한쪽만 있을 수 없는 방향이다.
    크기를 보는 검사라면 둘이 같게 나온다.
    """
    lots = [_lot(3587, 30)]
    envelope_ok = {"lots": lots, "inventory_by_item": [{"item": ITEM, "available_qty_kg": 1000}]}
    envelope_bad = {
        "lots": [_lot(1000, 30)],
        "inventory_by_item": [{"item": ITEM, "available_qty_kg": 3587}],
    }

    assert free_stock_for(envelope_ok, ITEM) == FreeStock(kg=1000.0)
    assert free_stock_for(envelope_bad, ITEM).unknown_reason is not None
    assert free_stock_for(envelope_bad, ITEM).kg is None


def test_an_unknown_lot_quantity_suspends_the_comparison() -> None:
    """🔴 로트 수량이 ``None`` 이면 **대조를 걸지 않는다** (규칙 3).

    ``None`` 은 합에서 빠지므로 합이 과소평가된다. 그 상태로 「집계가 크다」를 적으면
    **안 센 로트 때문에 있지도 않은 고장을 적는 것**이 된다.
    """
    envelope = {
        "lots": [_lot(100, 30), _lot(0, 30, available_qty_kg=None)],
        "inventory_by_item": [{"item": ITEM, "available_qty_kg": 900}],
    }
    assert free_stock_for(envelope, ITEM) == FreeStock(kg=900.0), (
        "모르는 로트가 섞이면 방향을 단정하지 않는다"
    )


def test_the_disclosure_says_nothing_about_internals(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 이 문장도 H1 화면과 Critic 이 읽는다 — 내부 이름을 흘리지 않는다."""
    _with_lots_and_free_stock(
        monkeypatch, [_lot(1000, 30)], [{"item": ITEM, "available_qty_kg": 1500}]
    )
    risks = next(
        s for s in run_purchase_agent(ITEM, AS_OF)["scenarios"] if s["label"] == "기본"
    )["risks"]
    disclosure = next(r for r in risks if "로트 합보다 커서" in r)

    for internal in (
        "inventory_by_item",
        "free_stock",
        "available_qty_kg",
        "FreeStock",
        "unknown_reason",
        "lots[",
    ):
        assert internal not in disclosure, internal
