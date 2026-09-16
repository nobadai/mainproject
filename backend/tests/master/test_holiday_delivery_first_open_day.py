"""휴장일로 잡힌 납품은 **그 뒤 첫 개장일에 한 번** 처리한다 (2026-09-15).

실측 SIM-CHAIN-CHECK-0915: 금요일 확정 · 토요일 납품(2026-03-07 · 04-04) 판매가
그 토요일이 휴장이라 걷기가 건너뛰었다. 출고 조회와 채권 조회가 `sale_date = as_of`
정확 일치라 **다음 개장일에도 안 잡혔다** — 예약 6건 RESERVED · 할당 0,
MISSING_RECEIVABLE 6.

```text
① 납품일이 휴장이면 다음 개장일에 잡힌다
② 그 다음 개장일에는 다시 안 잡힌다 (backorder 아님)
③ 납품일 당일 개장이면 종전과 같다
```

🔴 **실 DB 에 닿지 않는다.** 조회 문장을 메모리 SQLite 에서 **그대로** 돌린다 —
   조건을 흉내 내는 대역이면 규칙이 틀려도 대역이 같이 틀린다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, Self

import pytest

from app.master import outbound_flow
from app.master.finance_receivable import read_confirmed_sales
from app.master.outbound_flow import DueSaleItem, due_sale_items

실행 = "SIM-CHAIN-CHECK-0915"
남의_실행 = "SIM-OTHER"

금 = date(2026, 3, 6)
토_휴장 = date(2026, 3, 7)
월 = date(2026, 3, 9)
화 = date(2026, 3, 10)
수 = date(2026, 3, 11)

_DATE_COLUMNS = {"sale_date", "collection_due_date", "as_of"}


@pytest.fixture(autouse=True)
def 스키마_이름(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_SCHEMA", "haetdeul")


class _커서:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, query: Any, params: Any = None) -> None:
        text = query.as_string() if hasattr(query, "as_string") else str(query)
        # ★ 문법만 옮긴다. 조건은 한 글자도 안 바꾼다.
        text = text.replace("= ANY(%s)", "IN (SELECT value FROM json_each(%s))")
        text = text.replace("%s", "?")
        values = [
            json.dumps(list(v))
            if isinstance(v, list | tuple)
            else v.isoformat()
            if isinstance(v, date)
            else v
            for v in (params or [])
        ]
        cur = self.db.execute(text, values)
        names = [d[0] for d in cur.description]
        self.rows = [
            {
                n: date.fromisoformat(v) if n in _DATE_COLUMNS and isinstance(v, str) else v
                for n, v in zip(names, row, strict=True)
            }
            for row in cur.fetchall()
        ]

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)


class _연결:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("ATTACH DATABASE ':memory:' AS haetdeul")
        self.db.executescript(
            """
            CREATE TABLE haetdeul.sales (
                sale_id TEXT, sim_run_id TEXT, sale_date TEXT, order_status TEXT,
                customer_partner_id TEXT, collection_due_date TEXT, total_amount_krw TEXT
            );
            CREATE TABLE haetdeul.sale_items (
                sale_item_id TEXT, sale_id TEXT, item_id TEXT, quantity_kg TEXT
            );
            CREATE TABLE haetdeul.master_day_openings (
                as_of TEXT, sim_run_id TEXT, result TEXT
            );
            """
        )

    def cursor(self) -> _커서:
        return _커서(self.db)

    def 판매(self, sale_id: str, sale_date: date, *, run: str = 실행, status: str = "CONFIRMED"):
        self.db.execute(
            "INSERT INTO haetdeul.sales VALUES (?, ?, ?, ?, 'P-1', '2026-04-30', '1000')",
            (sale_id, run, sale_date.isoformat(), status),
        )
        self.db.execute(
            "INSERT INTO haetdeul.sale_items VALUES (?, ?, 'ITEM-배추', '100')",
            (f"SI-{sale_id}", sale_id),
        )

    def 개장(self, as_of: date, *, run: str = 실행, result: str = "OPENED") -> None:
        self.db.execute(
            "INSERT INTO haetdeul.master_day_openings VALUES (?, ?, ?)",
            (as_of.isoformat(), run, result),
        )


def _출고(conn: _연결, as_of: date) -> list[str]:
    return [r.sale_id for r in due_sale_items(conn, as_of=as_of, sim_run_id=실행)]


def _채권(conn: _연결, as_of: date) -> list[str]:
    return [r.sale_id for r in read_confirmed_sales(conn, as_of=as_of, sim_run_id=실행)]


@pytest.fixture
def 장부() -> _연결:
    """금요일 열고 · 토요일 휴장(건너뜀) · 월요일이 그 뒤 첫 개장일."""
    conn = _연결()
    conn.판매("SALE-토요일납품", 토_휴장)
    conn.개장(금)
    # ⚠️ 휴장일에 행이 서도 성공 개장이 아니면 안 센다.
    conn.개장(토_휴장, result="NOT_OPENED")
    # ⚠️ 남의 실행이 그 토요일을 열었어도 내 실행의 휴장은 그대로다.
    conn.개장(토_휴장, run=남의_실행)
    return conn


@pytest.mark.parametrize("읽기", [_출고, _채권], ids=["출고", "채권"])
def test_납품일이_휴장이면_다음_개장일에_잡힌다(장부: _연결, 읽기) -> None:
    assert 읽기(장부, 월) == ["SALE-토요일납품"]


@pytest.mark.parametrize("읽기", [_출고, _채권], ids=["출고", "채권"])
def test_처리한_다음_개장일에는_다시_안_잡힌다(장부: _연결, 읽기) -> None:
    """🔴 **backorder 가 아니다.** SHORT · 놓아준 예약이라 여전히 CONFIRMED 여도 안 잡는다."""
    장부.개장(월)

    assert 읽기(장부, 화) == []


@pytest.mark.parametrize("읽기", [_출고, _채권], ids=["출고", "채권"])
def test_납품일_당일_개장이면_종전과_같다(장부: _연결, 읽기) -> None:
    장부.개장(월)
    장부.판매("SALE-화요일", 화)
    장부.판매("SALE-수요일", 수)
    # ★ 금요일에 열었던 날의 미출고 판매는 월요일에 다시 안 잡힌다.
    장부.판매("SALE-금요일-SHORT", 금)
    장부.판매("SALE-남의것", 화, run=남의_실행)

    assert 읽기(장부, 화) == ["SALE-화요일"]
    assert "SALE-금요일-SHORT" not in 읽기(장부, 월)


def test_출고의_파이썬_거름이_휴장_납품을_버리지_않는다() -> None:
    """★ 조회가 고른 지난 날짜 행을 `_due_today` 가 다시 버리면 조회를 고친 것이 헛돈다."""

    def 행(sale_date: date) -> DueSaleItem:
        return DueSaleItem(
            sale_id=f"S-{sale_date}",
            sale_item_id="SI",
            item_id="ITEM",
            sim_run_id=실행,
            quantity_kg=Decimal(1),
            sale_date=sale_date,
        )

    남은 = outbound_flow._due_today([행(토_휴장), 행(월), 행(화)], 월)

    assert [r.sale_date for r in 남은] == [토_휴장, 월]
