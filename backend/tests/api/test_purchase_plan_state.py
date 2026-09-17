"""매입 탭이 **어느 실행의 안인지**와 **그 안이 어느 상태인지**를 싣는다 (2026-09-16).

왜 필요한가
────────────────────────────────────────────────────────────────────────────
말로 한 승인(`POST /master/ask/execute` · `SELECT_SCENARIO`)은 **어느 실행의 안인가**
(`target_request_id`)를 본문에 실어야 한다. 없으면 서버가 422 로 거절한다 — 서버가
*"가장 최근 실행"* 으로 추측하면 엉뚱한 날의 안이 승인되기 때문이다.

그 값의 출처가 지금까지 **그 채팅에서 방금 만든 안** 하나뿐이었다. 사람이 콘솔을 새로
열고 **그날 이미 서 있는 안**을 말로 고르면 짚을 데가 없어 멈췄다 — 9/11 시연이 정확히
그 모양이다. `plans[]` 에 업무 키가 없어서 라벨만으로는 어느 실행의 안인지 못 짚었다.

여기서 재는 것
────────────────────────────────────────────────────────────────────────────
.. code-block:: text

    ① request_id · history_run_id 가 실린다
    ② 못 읽은 행 id 는 None 이다 — 지어내지 않는다
    ③ state 네 갈래가 실제 상태를 가린다 (후보 · 승인됨 · 매입 기록됨)
    ④ approved 가 그대로 있다 — 읽는 자리가 있다 (대시보드 서버 · 09-17 정정)
    ⑤ 상태 어휘의 주인이 하나다 (`app/api/plan_state.py`) — 대시보드와 같은 것
    ⑥ 「승인 대기」는 요청(품목·날) 단위다 — 형제 안이 결정되면 대기가 아니다 (09-17)

🔴 **실 DB 에 안 닿는다.** `_read` 와 실매입 기록 조회를 대역으로 세우고, 「읽어 온 값을
   어떻게 싣는가」만 본다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.api import plan_state
from app.api.purchase import query as purchase_query
from app.master.purchase_record_repository import RecordedTotals

AS_OF = date(2026, 4, 13)
AXIS = "SIM-CHECK-HOLIDAY-0916"
RUN_ID = "0f2a1c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"


def _scenario(label: str, price: int = 900) -> dict[str, Any]:
    return {
        "label": label,
        "coverage_days": 2,
        "strategy_type": "quantity",
        "total_qty_kg": 574,
        "total_amount_krw": 281_834,
        "max_price": price + 100,
        "cut_unit_price": price + 50,
        "sourcing_plan": [{"grade": "특", "grade_unit_price": price, "qty_kg": 574}],
        "split_plan": [],
        "rationale": [],
        "risks": [],
    }


def _run(
    request_id: str,
    *scenarios: dict[str, Any],
    item: str = "배추",
    run_id: str | None = RUN_ID,
    sim_run_id: str | None = AXIS,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "request_id": request_id,
        "item": item,
        "end_code": "E1_APPROVED",
        "runtime_status": "READY",
        "created_at": datetime(2026, 4, 13, 1, 0, tzinfo=UTC),
        "sim_run_id": sim_run_id,
        "payload": {"scenarios": list(scenarios)},
    }
    #  ★ `None` 을 «칸이 아예 없다» 와 갈라 둔다 — ②가 재는 것이 그 자리다.
    if run_id is not None:
        run["run_id"] = run_id
    return run


def _data(runs: list[dict[str, Any]], decisions: list[dict[str, Any]] | None = None):
    return {
        "runs": runs,
        "buys": [],
        "decisions": decisions or [],
        "items": {},
        "arrivals": [],
    }


@pytest.fixture
def tab(monkeypatch):
    """`_read` 와 실매입 기록 조회를 대역으로. **DB 없이 돈다.**"""
    state: dict[str, Any] = {"data": _data([]), "records": {}}

    #  ⚠️ `**_` 다 — `build` 가 `window_days=` 를 넘긴다 (`#740`). 안 받으면 `TypeError`
    #     가 나고 `build` 의 `except Exception` 이 그것을 삼켜 조용히 예시값이 나간다.
    monkeypatch.setattr(purchase_query, "_read", lambda _as_of, **_: state["data"])
    monkeypatch.setattr(
        purchase_query, "recorded_totals_by_plan", lambda **_: state["records"]
    )

    def _build(**over: Any):
        state.update(over)
        return purchase_query.build(AS_OF, AXIS)

    return _build


def _approval(request_id: str, label: str, decision: str = "APPROVE") -> dict[str, Any]:
    return {"request_id": request_id, "decision": decision, "scenario_label": label}


def _recorded(item: str, label: str) -> dict[tuple[str, str], RecordedTotals]:
    return {(item, label): RecordedTotals(qty_kg=500.0, amount_krw=275_000, unit_price=550)}


# ══════════════════════════════════════════════════════════════════════════
#  ①  어느 실행의 안인가
# ══════════════════════════════════════════════════════════════════════════

def test_안에_업무_키가_실린다(tab):
    """🔴 이 칸이 없어서 말로 한 승인이 「어느 안을 말씀하시는지 찾지 못했습니다」로 멈췄다."""
    plan = tab(data=_data([_run("REQ-A", _scenario("기본"))])).plans[0]

    assert plan.request_id == "REQ-A"


def test_안에_실행_행_id_가_실린다(tab):
    """⚠️ 업무 키 하나에 실행이 여러 행이다 (실측 75행).

    그 사이 재실행이 있으면 업무 키만으로는 **본 것과 다른 안**이 승인된 것으로 남는다 —
    마스터 승인 경로가 `history_run_id` 를 받는 이유와 같다.
    """
    plan = tab(data=_data([_run("REQ-A", _scenario("기본"))])).plans[0]

    assert plan.history_run_id == RUN_ID


def test_업무_키와_행_id_는_다른_칸이다(tab):
    """한 칸으로 합치면 그 둘이 같은 사실로 읽힌다 — 아니다."""
    plan = tab(data=_data([_run("REQ-A", _scenario("기본"))])).plans[0]

    assert plan.request_id != plan.history_run_id


# ══════════════════════════════════════════════════════════════════════════
#  ②  못 읽은 것은 None 이다
# ══════════════════════════════════════════════════════════════════════════

def test_행_id_를_못_읽으면_지어내지_않는다(tab):
    """🔴 업무 키를 행 id 자리에 넣어 메우면 **엉뚱한 실행**이 승인된다."""
    plan = tab(data=_data([_run("REQ-A", _scenario("기본"), run_id=None)])).plans[0]

    assert plan.history_run_id is None
    assert plan.request_id == "REQ-A", "행 id 를 못 읽어도 업무 키는 그대로다"


# ══════════════════════════════════════════════════════════════════════════
#  ③  상태 네 갈래
# ══════════════════════════════════════════════════════════════════════════

def test_결정이_없으면_후보다(tab):
    plan = tab(data=_data([_run("REQ-A", _scenario("기본"))]), records={}).plans[0]

    assert plan.state == "후보"


def test_승인만_된_안은_승인됨이다(tab):
    """🔴 고치기 전 이 자리에 화면이 「승인 대기」를 찍었다 — **결정이 났는데** 그랬다."""
    plan = tab(
        data=_data([_run("REQ-A", _scenario("기본"))], [_approval("REQ-A", "기본")]),
        records={},
    ).plans[0]

    assert plan.state == "승인됨"


def test_승인하고_실매입까지_적었으면_매입_기록됨이다(tab):
    """★ `approved` 하나로는 못 가른다 — 둘 다 참이다."""
    plan = tab(
        data=_data([_run("REQ-A", _scenario("기본"))], [_approval("REQ-A", "기본")]),
        records=_recorded("배추", "기본"),
    ).plans[0]

    assert plan.state == "매입 기록됨"


def test_다른_안의_기록을_이_안에_붙이지_않는다(tab):
    """열쇠는 `(품목, 안 이름)` 둘 다다. 품목만 맞추면 보수·기본·공격이 섞인다."""
    plan = tab(
        data=_data([_run("REQ-A", _scenario("보수"))], [_approval("REQ-A", "보수")]),
        records=_recorded("배추", "공격"),
    ).plans[0]

    assert plan.state == "승인됨"


def test_승인되지_않은_안은_기록이_있어도_후보다(tab):
    """기록이 있다는 것만으로 승인을 만들어 내지 않는다."""
    plan = tab(
        data=_data([_run("REQ-A", _scenario("기본"))]),
        records=_recorded("배추", "기본"),
    ).plans[0]

    assert plan.state == "후보"


def test_축이_없으면_기록을_안_맞춘다(monkeypatch):
    """🔴 그 조회는 축이 필수다. 아무 축이나 넣으면 **다른 걷기에서 산 값**이 붙는다."""
    불렀나: list[Any] = []

    monkeypatch.setattr(
        purchase_query,
        "_read",
        lambda _as_of, **_: _data(
            [_run("REQ-A", _scenario("기본"), sim_run_id=None)], [_approval("REQ-A", "기본")]
        ),
    )
    monkeypatch.setattr(
        purchase_query, "recorded_totals_by_plan", lambda **kw: 불렀나.append(kw) or {}
    )

    plan = purchase_query.build(AS_OF).plans[0]

    assert 불렀나 == [], "축 없이 기록 표를 물으면 안 된다"
    assert plan.state == "승인됨"


def test_기록을_못_읽어도_안_목록은_산다(tab, monkeypatch):
    """⚠️ 기록 하나 때문에 매입 탭이 통째로 죽으면 안 된다."""

    def _터진다(**_: Any):
        raise RuntimeError("DB 가 죽었다")

    monkeypatch.setattr(purchase_query, "recorded_totals_by_plan", _터진다)

    tabout = tab(data=_data([_run("REQ-A", _scenario("기본"))], [_approval("REQ-A", "기본")]))

    assert tabout.plans[0].state == "승인됨"
    assert tabout.source.filled is True


# ══════════════════════════════════════════════════════════════════════════
#  ④  `approved` 를 안 지운다
# ══════════════════════════════════════════════════════════════════════════

def test_approved_가_그대로_있다(tab):
    """🔴 읽는 자리가 있다 — 대시보드 서버 `_state` 가 `plan.approved` 를 읽는다.

    ~~쓰는 화면이 있다 (`console/purchase/page.tsx`)~~ — 낡았다 (2026-09-17). 매입 화면은
    이제 `state` 를 읽는다. `state` 를 더하면서 `approved` 를 지우지 않았다.
    """
    plans = tab(
        data=_data(
            [_run("REQ-A", _scenario("보수"), _scenario("기본"))],
            [_approval("REQ-A", "기본")],
        )
    ).plans

    보수, 기본 = plans
    #  🔴 `보수.pending` 이 `True` 였다 — 같은 요청에서 기본안이 승인됐는데 보수안을
    #     「승인 대기」로 셌다 (2026-09-17 정정). 대기는 요청 단위다 · ⑥ 참조.
    assert 보수.approved is False and 보수.pending is False
    assert 기본.approved is True and 기본.pending is False


# ══════════════════════════════════════════════════════════════════════════
#  ⑤  어휘의 주인이 하나다
# ══════════════════════════════════════════════════════════════════════════

def test_상태_어휘는_공용_자리에서_온다():
    """🔴 두 벌로 짜면 한쪽만 고치는 날 **같은 안이 화면마다 다른 상태**로 뜬다."""
    from app.api.dashboard import query as dashboard_query

    assert dashboard_query.PLAN_STATES is plan_state.PLAN_STATES


def test_매입_탭이_내는_낱말은_그_넷_안이다(tab):
    plans = tab(
        data=_data(
            [_run("REQ-A", _scenario("보수"), _scenario("기본"), _scenario("공격"))],
            [_approval("REQ-A", "기본"), _approval("REQ-A", "공격")],
        ),
        records=_recorded("배추", "공격"),
    ).plans

    assert [p.state for p in plans] == ["후보", "승인됨", "매입 기록됨"]
    assert {p.state for p in plans} <= set(plan_state.PLAN_STATES)


# ══════════════════════════════════════════════════════════════════════════
#  ⑥  「승인 대기」는 요청(품목·날) 단위다 (2026-09-17)
#
#  전에는 (요청, 안 이름) 한 쌍으로만 봐서, 같은 요청에서 보수안이 승인되면 고르지 않은
#  기본안이 「승인 대기」로 남았다. 실 DB (REH-0914 · 08-31) 에서 매입 탭 통계와 대시보드
#  배지가 3건을 셌는데, 기다리는 것은 양파 1건이었다.
#
#  🔴 낱말은 안 바꾼다 — 형제 안은 「후보」 그대로이고 **대기 수에서만** 빠진다.
# ══════════════════════════════════════════════════════════════════════════

def _waiting(tabout) -> float | None:
    return next(s for s in tabout.stats if s.label == "승인 대기").raw


def test_같은_요청에_결정이_있으면_형제_안도_대기가_아니다(tab):
    tabout = tab(
        data=_data(
            [_run("REQ-A", _scenario("보수"), _scenario("기본"))],
            [_approval("REQ-A", "보수")],
        )
    )
    보수, 기본 = tabout.plans

    assert (보수.pending, 기본.pending) == (False, False)
    assert _waiting(tabout) == 0
    #  ★ 낱말의 주인은 `plan_state.py` 다 — 여기서 「후보」를 딴 말로 바꾸지 않는다
    assert (보수.state, 기본.state) == ("승인됨", "후보")


def test_결정이_없으면_그대로_대기다(tab):
    """★ 규칙 8 — 수를 상수와 대 보지 않는다. **결정 하나를 넣고 빼서** 수가 따라오는지 본다."""
    runs = [
        _run("REQ-배추", _scenario("보수"), _scenario("기본")),
        _run("REQ-양파", _scenario("공격"), item="양파"),
    ]

    결정전 = tab(data=_data(runs))
    결정후 = tab(data=_data(runs, [_approval("REQ-배추", "보수")]))

    assert [p.pending for p in 결정전.plans] == [True, True, True]
    assert [p.pending for p in 결정후.plans] == [False, False, True]
    #  ★ 결정이 난 요청의 안 둘이 **같이** 빠지고, 결정 없는 양파는 그대로 남는다
    assert (_waiting(결정전), _waiting(결정후)) == (3, 1)


def test_대기_판정과_승인_판정은_같은_결정_행에서_나온다(tab):
    """🔴 무엇을 「결정」으로 치는지가 두 곳에서 갈리면 「승인됨」과 「대기 아님」이 따로 논다.

    `decided` 는 **안 이름이 붙은 행**만 본다. 안 이름 없는 행(`REQUEST_CHANGE`)만 있는
    요청을 대기에서 빼면, 그 요청의 안은 「후보」인데 아무도 기다리지 않는 것으로 셈된다.
    """
    tabout = tab(
        data=_data(
            [_run("REQ-A", _scenario("보수"))],
            [{"request_id": "REQ-A", "decision": "REQUEST_CHANGE", "scenario_label": None}],
        )
    )
    (plan,) = tabout.plans

    assert plan.state == "후보"
    assert plan.pending is True
