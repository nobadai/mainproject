"""매입 탭이 **어느 걷기를 보는가** — `sim_run_id` 축.

🔴 **상황을 검사가 주입한다.** 실 DB 로는 이 규칙을 증명할 수 없다 (2026-09-10 실측)::

    같은 (날, 품목) 에서 축 없는 실행이 더 최신인 자리   **0곳**

축이 `2026-09-08` 에 생겨서 축 있는 행이 언제나 더 새것이고, ``_pick`` 은 최신 하나를
고른다. 그래서 실 데이터에서는 **축을 걸든 안 걸든 고른 결과가 같다.** 값을 대 보는
검사는 통과하는데 축 조건을 지워도 안 죽는다 — 규칙 8 이 말하는 그 구멍이다.

★ 그래서 여기서는 **축 있는 행을 일부러 더 오래된 것으로** 주입한다. 그 상황에서만
«④ 축이 ⑤ 최신 하나보다 앞이다» 가 판정으로 드러난다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.api.purchase import query as purchase_query

AS_OF = date(2026, 1, 22)
AXIS = "SIM-BURNIN-202512"
OTHER = "SIM-WALK-202601-BASE"

#: 기준일 `2026-01-22` 는 실측으로 골랐다 — 그날 3품목이 다 축 있는 실행으로 서고,
#: 축 없는 실행이 하나 남아 «뺀 건수» 가 눈에 보인다 (축 걸기 전 4건 · 후 3건).
#: 🔴 `tests/api/test_screen_api.py` 의 `AS_OF = 2026-01-06` 은 **축을 걸면 0건이 되는
#:   날**이라 쓰지 않는다.


def _scenario(label: str, price: int) -> dict[str, Any]:
    return {
        "label": label,
        "coverage_days": 2,
        "strategy_type": "quantity",
        "total_qty_kg": 1000,
        "total_amount_krw": price * 1000,
        "max_price": price + 100,
        "cut_unit_price": price + 50,
        "sourcing_plan": [{"grade": "특", "grade_unit_price": price, "qty_kg": 1000}],
        "split_plan": [{"buy_date": "2026-01-22", "qty_kg": 1000,
                        "expected_arrival_date": "2026-01-24"}],
        "rationale": [{"source": "예측", "claim": "예측 -1%", "ref_id": "FC-1"}],
        "risks": [],
    }


def _run(request_id: str, item: str, sim_run_id: str | None, minute: int,
         price: int = 800) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "item": item,
        "end_code": "E1_APPROVED",
        "runtime_status": "READY",
        #  DB 가 tz 를 붙여 준다 (실측: Etc/UTC).
        "created_at": datetime(2026, 9, 9, 1, minute, tzinfo=UTC),
        "sim_run_id": sim_run_id,
        "payload": {"scenarios": [_scenario("보수", price)]},
    }


def _buy(purchase_id: str, sim_run_id: str | None, amount: int = 800_000) -> dict[str, Any]:
    return {
        "purchase_id": purchase_id,
        "purchase_date": date(2026, 1, 22),
        "payment_due_date": date(2026, 1, 22),
        "settlement_status": "UNSETTLED",
        "sim_run_id": sim_run_id,
        "item_id": "ITEM-BAECHU",
        "grade": "특",
        "quantity_kg": Decimal(1000),
        "unit_price_krw_per_kg": Decimal(800),
        "line_amount_krw": Decimal(amount),
    }


def _data(runs: list[dict], buys: list[dict] | None = None,
          arrivals: list[dict] | None = None) -> dict[str, Any]:
    return {
        "runs": sorted(runs, key=lambda r: r["created_at"], reverse=True),
        "buys": buys or [],
        "decisions": [],
        "items": {"ITEM-BAECHU": "배추"},
        "arrivals": arrivals or [],
    }


@pytest.fixture
def inject(monkeypatch):
    """``_read`` 를 대신 세운다. **DB 없이 돈다.**"""

    def _inject(data: dict[str, Any]):
        monkeypatch.setattr(purchase_query, "_read", lambda as_of: data)

    return _inject


# ══════════════════════════════════════════════════════════════════════════
#  ① 축이 «최신 하나» 보다 앞이다
# ══════════════════════════════════════════════════════════════════════════

#: 🔴 축 있는 것이 **더 오래된** 배치. 실 DB 에 없는 상황이라 주입한다.
_OLDER_ON_AXIS = [
    _run("REQ-축있음-오래됨", "배추", AXIS, minute=10, price=800),
    _run("REQ-축없음-최신", "배추", None, minute=50, price=900),
]


def test_축을_주면_더_오래된_것이라도_그_축을_고른다(inject):
    """④ 가 ⑤ 뒤로 가면 «최신 하나» 가 먼저 다른 걷기 행을 집고 축에서 떨어진다.

    그러면 같은 축의 조금 오래된 행이 **같이 사라져** 화면이 0건이 된다.
    """
    inject(_data(_OLDER_ON_AXIS))

    tab = purchase_query.build(AS_OF, AXIS)

    assert [p.key for p in tab.plans] == ["배추 · 보수"]
    assert tab.plans[0].unit_price == 800, "축 있는(오래된) 실행의 단가여야 한다"


def test_축을_안_주면_지금_그대로_최신을_고른다(inject):
    """기본값은 **안 거른다** — 축이 붙기 전 실행 1,202건이 안 사라져야 한다."""
    inject(_data(_OLDER_ON_AXIS))

    tab = purchase_query.build(AS_OF)

    assert tab.plans[0].unit_price == 900, "축을 안 줬으면 최신(축 없는 것)이다"


# ══════════════════════════════════════════════════════════════════════════
#  ② 없는 축 · 빈 축
# ══════════════════════════════════════════════════════════════════════════

def test_없는_축을_주면_0건이고_안이_죽은_것이_아니라고_적는다(inject):
    """🔴 «안 돌았다» 와 «돌았는데 죽었다» 는 다른 사실이다.

    축으로 걸러 0건인데 다른 걷기의 컷 사유를 붙이면, 읽는 사람이 **없는 원인**을
    고치려 든다 (실측 2026-09-10 — 단가 상한 초과 문구가 그렇게 붙었다).
    """
    runs = [_run("REQ-다른축", "배추", OTHER, minute=10)]
    runs[0]["payload"]["judgment"] = {
        "rejected_reasons": [{"label": "보수", "reason": "매입단가 1,100원이 상한 1,082원 초과"}]
    }
    inject(_data(runs))

    tab = purchase_query.build(AS_OF, AXIS)

    assert tab.plans == []
    assert "돌지 않았습니다" in tab.plans_note.text
    assert "상한" not in tab.plans_note.text, "다른 걷기의 컷 사유를 가져오면 안 된다"


def test_빈_축은_전부가_아니다(inject):
    """``is None`` 이라야 한다.

    ``not sim_run_id`` 로 두면 빈 문자열이 «안 줬다» 로 읽혀, **오타 하나가 조용히
    전체 조회**가 된다. 축은 잘못 주면 0건이 나와야 잘못 준 줄 안다.
    """
    inject(_data(_OLDER_ON_AXIS))

    tab = purchase_query.build(AS_OF, "")

    assert tab.plans == [], "빈 축은 «전부» 가 아니라 «맞는 것이 없다» 다"


# ══════════════════════════════════════════════════════════════════════════
#  ③ 거른 것을 조용히 없애지 않는다
# ══════════════════════════════════════════════════════════════════════════

def test_뺀_건수와_축_이름을_화면_글에_적는다(inject):
    """계약 밖 품목을 거를 때와 **같은 규율**이다."""
    inject(_data([
        _run("REQ-축있음", "배추", AXIS, minute=50),
        _run("REQ-다른축-1", "배추", OTHER, minute=40),
        _run("REQ-축없음", "무", None, minute=30),
    ]))

    tab = purchase_query.build(AS_OF, AXIS)

    assert "다른 걷기의 실행 2건은 뺐습니다" in tab.plans_note.text
    assert AXIS in tab.plans_note.text, "어느 걷기를 보는지가 화면에 있어야 한다"


def test_축을_안_주면_뺐다는_말이_안_붙는다(inject):
    inject(_data(_OLDER_ON_AXIS))

    tab = purchase_query.build(AS_OF)

    assert "뺐습니다" not in tab.plans_note.text


# ══════════════════════════════════════════════════════════════════════════
#  ④ 원장과 도착일도 같은 축을 본다
# ══════════════════════════════════════════════════════════════════════════

def test_확정_매입도_축을_따르고_뺀_줄을_적는다(inject):
    """🔴 안 그러면 이번 주 매입액이 **두 세상의 합**이 된다."""
    inject(_data(
        [_run("REQ-축있음", "배추", AXIS, minute=50)],
        buys=[_buy("PUR-이축", AXIS, 800_000), _buy("PUR-다른축", OTHER, 500_000)],
    ))

    tab = purchase_query.build(AS_OF, AXIS)

    assert [r["approval"] for r in tab.committed.rows] == ["PUR-이축"]
    assert tab.stats[2].raw == 800_000, "다른 걷기 금액이 주간 합계에 안 섞여야 한다"
    assert "다른 걷기의 줄 1개는 뺐습니다" in tab.committed_note.text


def test_도착일도_같은_축에서만_맞춘다(inject):
    """다른 걷기가 우연히 같은 금액을 낸 날 **엉뚱한 도착일**이 붙으면 안 된다.

    ⚠️ 실측(2026-09-10)에서 `01-22` 까지 원장 네 줄 중 둘이 축 없는 실행에서 도착일을
    받아 오고 있었다. 축을 걸면 공란이 되는데, **그것이 맞다** — 금액 맞춤은 추정이고
    축이 다르면 그 추정을 받칠 근거가 없다.
    """
    arrival = {
        "as_of": AS_OF, "item": "배추", "sim_run_id": OTHER,
        "scenarios": [_scenario("보수", 800)],
    }
    inject(_data(
        [_run("REQ-축있음", "배추", AXIS, minute=50)],
        buys=[_buy("PUR-이축", AXIS, 800_000)],
        arrivals=[arrival],
    ))

    tab = purchase_query.build(AS_OF, AXIS)

    assert tab.committed.rows[0]["arrive"] is None, "다른 걷기의 도착일을 쓰면 안 된다"
    assert "도착일을 못 맞춘 줄 1개" in tab.committed_note.text


def test_같은_축이면_도착일을_맞춘다(inject):
    """축을 거는 것이 **맞추기 자체를 끄는 것은 아니다.**"""
    arrival = {
        "as_of": AS_OF, "item": "배추", "sim_run_id": AXIS,
        "scenarios": [_scenario("보수", 800)],
    }
    inject(_data(
        [_run("REQ-축있음", "배추", AXIS, minute=50)],
        buys=[_buy("PUR-이축", AXIS, 800_000)],
        arrivals=[arrival],
    ))

    tab = purchase_query.build(AS_OF, AXIS)

    assert tab.committed.rows[0]["arrive"] == "2026-01-24"
