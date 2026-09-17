"""매입 탭 확정 매입 표는 **최신순**이다 (2026-09-17).

🔴 오래된 순이면 오늘 산 줄이 수백 줄 맨 아래에 깔린다 — REH-0914 08-31 에서 291줄,
FINAL-0918 09-14 에서 495줄이었다. 화면은 10줄씩 나눠 보이므로(`page.tsx`) 첫 쪽이
최신이어야 한다.

★ 순서는 SQL 이 정한다. DB 없이 순서를 잴 수 없으니 둘로 나눠 본다::

    ① 원장 조회가 최신순으로 나가나 — 질의 문면을 본다
    ② `_committed` 가 그 순서를 **다시 섞지 않나** — 주입한 순서가 그대로 나오나
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.api.purchase import query as purchase_query

AS_OF = date(2026, 8, 31)
AXIS = "SIM-ORDER-TEST"


def _buy(purchase_id: str, purchase_date: date) -> dict[str, Any]:
    return {
        "purchase_id": purchase_id,
        "purchase_date": purchase_date,
        "payment_due_date": purchase_date,
        "settlement_status": "OPEN",
        "sim_run_id": AXIS,
        "item_id": "ITEM-CABBAGE",
        "grade": "특",
        "quantity_kg": Decimal(10),
        "unit_price_krw_per_kg": Decimal(900),
        "line_amount_krw": Decimal(9_000),
    }


def test_원장_조회가_최신순으로_나간다(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.finance.db as finance_db

    monkeypatch.setenv("DB_SCHEMA", "haetdeul")
    문면: list[str] = []

    def _record(query: Any, params: Any = None) -> list[dict[str, Any]]:
        문면.append(query.as_string(None))
        return []

    monkeypatch.setattr(finance_db, "fetch_all", _record)

    purchase_query._read(AS_OF, sim_run_id=AXIS)

    (원장,) = [t for t in 문면 if "purchase_items" in t]
    assert "ORDER BY p.purchase_date DESC, i.purchase_item_id DESC" in 원장


def test_확정_매입_표는_받은_순서를_다시_섞지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 규칙 8 — 순서를 **두 가지로 넣어** 나오는 순서가 따라오는지 본다."""
    최신순 = [_buy("PUR-C", date(2026, 8, 31)), _buy("PUR-B", date(2026, 8, 20)),
              _buy("PUR-A", date(2026, 8, 3))]

    def _tab(buys: list[dict[str, Any]]):
        data = {"runs": [], "buys": buys, "decisions": [],
                "items": {"ITEM-CABBAGE": "배추"}, "arrivals": []}
        monkeypatch.setattr(purchase_query, "_read", lambda as_of, **_kwargs: data)
        return purchase_query.build(AS_OF, AXIS)

    앞 = [r["approval"] for r in _tab(최신순).committed.rows]
    뒤 = [r["approval"] for r in _tab(list(reversed(최신순))).committed.rows]

    assert 앞 == ["PUR-C", "PUR-B", "PUR-A"]
    assert 뒤 == ["PUR-A", "PUR-B", "PUR-C"]
