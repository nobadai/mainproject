"""확정 주문 **조회가 터진 것**과 **조회했는데 0건인 것**을 가른다 (`#651`).

```text
조회 성공 · 0건   DERIVED    파트너 일수요 × 기간
조회 성공 · N건   MEASURED   이 실행의 확정 주문
조회 실패         MISSING    메우지 않는다
```

🔴 전 판은 `_orders_from_db` 예외를 `booked = None` 으로 받아 0건과 같은 길로 보냈다.
  파생이 성공하면 결과는 `DERIVED` 이고, **DB 장애가 명목 수요로 사는 정상 걷기**로 보였다.

★ `load_forecast` 가 조회 실패에 대해 이미 낸 결론과 같은 모양이다 (§1.2-10).

⚠️ **실 DB 를 타지 않는다.** `fetch_all` · `fetch_one` · `get_db_schema` 를 갈아 끼운다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.master import inputs

ITEM = "배추"
AS_OF = date(2025, 12, 31)
SIM_RUN_ID = "SIM-TEST-ORDERS-FAILURE"

DEMAND = {
    "daily_demand_kg": Decimal("717.300"),
    "demand_basis": "통합 Persona v1.2 적용 일수요",
    "provisional": True,
}


def _patch(monkeypatch: pytest.MonkeyPatch, *, many: Any, one: Any) -> None:
    monkeypatch.setattr(inputs, "fetch_all", many)
    monkeypatch.setattr(inputs, "fetch_one", one)
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")


def _demand_one() -> Any:
    calls = iter([DEMAND, {"order_cycle_days": 2}])
    return lambda *a: next(calls)


# ── (a) 🔴 조회가 터지면 비운다 ─────────────────────────────────────────


def test_확정_주문_조회가_터지면_명목_수요로_메우지_않는다(monkeypatch):
    """★★ **이 판의 핵심.** 파생이 살아 있어도 그 길로 가지 않는다."""

    def boom(*_a: Any) -> Any:
        raise RuntimeError("커넥션 없음")

    # 파생 경로는 **성공할 수 있게** 둔다 — 그래야 예외가 파생으로 새는지를 잰다.
    _patch(monkeypatch, many=boom, one=_demand_one())

    got = inputs.load_confirmed_orders(ITEM, AS_OF, sim_run_id=SIM_RUN_ID)

    assert got.grade == "MISSING", f"조회 실패가 {got.grade} 로 샜다: {got.note}"
    assert got.payload is None, "못 읽었는데 수요가 실렸다"
    assert got.source == "-"
    assert got.key == "confirmed_orders"
    assert "확정 주문 조회 실패" in got.note, "실패 사실이 사유에 안 남는다"
    assert "RuntimeError" in got.note, "예외 클래스가 사유에 안 남는다"
    assert "커넥션 없음" in got.note, "예외 메시지가 사유에 안 남는다"
    assert not got.usable, "서비스가 이 값을 매입에 실으면 안 된다"


# ── (b) 조회 성공 · 0건 은 그대로 파생이다 ───────────────────────────────


def test_조회가_성공하고_0건이면_그대로_파생한다(monkeypatch):
    """🔴 **숫자 불변.** 정상 경로의 등급 · 사유 · 값이 전 판과 같다."""
    _patch(monkeypatch, many=lambda *a: [], one=_demand_one())

    got = inputs.load_confirmed_orders(ITEM, AS_OF, sim_run_id=SIM_RUN_ID)

    assert got.grade == "DERIVED"
    assert got.source == "partner_item_demands · v_current_partner_demand"
    assert got.note == (
        "앞으로 납품할 확정 건이 없다 → 일수요 717.3kg × 14일, 주기 2일로 분할 "
        "(통합 Persona v1.2 적용 일수요, 잠정값) · 확정 주문이 아니다"
    )
    assert got.payload == {
        "as_of": "2025-12-31",
        "item": ITEM,
        "orders": [
            {"sale_id": None, "qty_kg": 1434.6, "due_date": f"2026-01-{day:02d}"}
            for day in (2, 4, 6, 8, 10, 12, 14)
        ],
        "total_kg": 10042.2,
    }


# ── (c) 조회 성공 · N건 은 그대로 실측이다 ───────────────────────────────


def test_조회가_성공하고_N건이면_그대로_실측이다(monkeypatch):
    rows = [
        {"sale_id": "SALE-1", "sale_date": date(2026, 1, 3), "quantity_kg": Decimal(12000)},
        {"sale_id": "SALE-2", "sale_date": date(2026, 1, 5), "quantity_kg": Decimal("30.5")},
    ]

    def one(*_a: Any) -> Any:
        raise AssertionError("실제 확정 주문이 있으면 파생 경로를 부르지 않는다")

    _patch(monkeypatch, many=lambda *a: rows, one=one)

    got = inputs.load_confirmed_orders(ITEM, AS_OF, sim_run_id=SIM_RUN_ID)

    assert got.grade == "MEASURED"
    assert got.source == "sales + sale_items"
    assert got.note == "2025-12-31 이후 14일 납품 예정"
    assert got.payload == {
        "as_of": "2025-12-31",
        "item": ITEM,
        "orders": [
            {"sale_id": "SALE-1", "qty_kg": 12000, "due_date": "2026-01-03"},
            {"sale_id": "SALE-2", "qty_kg": 30.5, "due_date": "2026-01-05"},
        ],
        "total_kg": 12030.5,
    }


# ── (d) 파생 조회가 터져도 비운다 ────────────────────────────────────────


def test_파생_조회가_터져도_지어내지_않고_비운다(monkeypatch):
    """★ 같은 규율이다. 파생 쪽 실패는 전 판도 `MISSING` 이었고 그대로다."""

    def boom(*_a: Any) -> Any:
        raise RuntimeError("뷰 없음")

    _patch(monkeypatch, many=lambda *a: [], one=boom)

    got = inputs.load_confirmed_orders(ITEM, AS_OF, sim_run_id=SIM_RUN_ID)

    assert got.grade == "MISSING"
    assert got.payload is None
    assert "파생도 실패" in got.note
    assert "뷰 없음" in got.note


# ── 받는 쪽 ─────────────────────────────────────────────────────────────


def test_비운_확정_주문은_매입이_누락으로_받는다():
    """★ `MISSING` 이면 서비스가 칸을 안 싣고, 매입 어댑터가 이름을 `missing_data` 에 올린다.

    조용히 0 수요로 사거나 노드에서 터지지 않는다는 것을 받는 쪽 한 자리에서 확인한다.
    """
    from app.purchase_agent.adapter import validate_payload

    missing = validate_payload({"item": ITEM}, AS_OF)

    assert "confirmed_orders" in missing
