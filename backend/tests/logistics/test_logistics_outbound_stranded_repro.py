"""07-08 배추 재현: 실 정책 · 실 Lot 7개 · 그날 이미 묶인 예약 2건을 그대로 복제한다.
예약 781kg 성공 → 할당 → 출고까지 되는가. (§3 결과 A/B 판정)"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest

from app.logistics.fefo_allocation import allocate_reserved_stock_fefo
from app.logistics.outbound import reserve_available_stock, ship_allocated_stock
from app.master.outbound_flow import ALLOCATE_PHASE, phase_instant
from tests.logistics.test_logistics_outbound_db import (
    ITEM_ID,
    RSV,
    SALE_ID,
    SALE_ITEM_ID,
    SIM_RUN_ID,
    TMP_SCHEMA,
    _lot,
    conn,
)

pytestmark = pytest.mark.db
__all__ = ["conn"]
AS_OF = date(2026, 7, 8)


def _policy_like_real(c: psycopg.Connection) -> None:
    with c.cursor() as cur:
        cur.execute(
            f"""UPDATE {TMP_SCHEMA}.item_storage_policies
                        SET operational_limit_days=10, disposal_candidate_days=2,
                            medium_grade_factor=0.6
                        WHERE item_id=%s""",
            (ITEM_ID,),
        )


def _lot_graded(c, lot_id, *, qty, received_at, grade="특"):
    _lot(c, lot_id, qty=qty, received_at=received_at)
    with c.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.inventory_lots SET grade=%s WHERE lot_id=%s", (grade, lot_id)
        )


def _stuck_reservation(c, rid, sale_id, kg):
    with c.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.sales VALUES (%s) ON CONFLICT DO NOTHING", (sale_id,)
        )
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_reservations
                        (reservation_id, sim_run_id, item_id, sale_id,
                         required_qty_kg, reserved_qty_kg, status)
                        VALUES (%s,%s,%s,%s,%s,%s,'RESERVED')""",
            (rid, SIM_RUN_ID, ITEM_ID, sale_id, Decimal(kg), Decimal(kg)),
        )


def _allocs(c):
    with c.cursor() as cur:
        cur.execute(
            f"SELECT lot_id, allocated_qty_kg, status FROM {TMP_SCHEMA}.inventory_allocations"
            " WHERE reservation_id=%s",
            (RSV,),
        )
        return [dict(r) for r in cur.fetchall()]


def test_07_08_배추_실조건_복제(conn: psycopg.Connection) -> None:
    _policy_like_real(conn)
    실_로트 = [
        (date(2026, 6, 30), "781"),
        (date(2026, 7, 1), "1298"),
        (date(2026, 7, 2), "137"),
        (date(2026, 7, 3), "687"),
        (date(2026, 7, 4), "748"),
        (date(2026, 7, 7), "1435"),
        (date(2026, 7, 8), "654"),
    ]
    for i, (recv, kg) in enumerate(실_로트, start=1):
        _lot_graded(conn, f"LOT-BC-{i}", qty=kg, received_at=recv)
    _stuck_reservation(conn, "RSV-STUCK-0210", "SALE-STUCK-0210", "3586")
    _stuck_reservation(conn, "RSV-STUCK-0603", "SALE-STUCK-0603", "719")

    r = reserve_available_stock(
        conn,
        reservation_id=RSV,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(781),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )
    assert r.applied and r.reserved_qty_kg == Decimal(781), r
    a = allocate_reserved_stock_fefo(
        conn, reservation_id=RSV, as_of=AS_OF, decided_at=phase_instant(AS_OF, ALLOCATE_PHASE)
    )
    rows = _allocs(conn)
    assert a.applied is True, (a, rows)
    assert sum(Decimal(x["allocated_qty_kg"]) for x in rows) == Decimal(781), rows
    s = ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF, sale_item_id=SALE_ITEM_ID)
    assert Decimal(getattr(s, "shipped_qty_kg", 0)) == Decimal(781), s


# ── 고아 예약 방지 — 실제 release_reservation 이 그날 놓아주고, 가용재고가 돌아온다 ──


class _Nested:
    """`ship_due_sales` 의 commit/rollback 을 **savepoint** 로 받는다.

    🔴 fixture 의 바깥 트랜잭션(임시 스키마)을 커밋하면 공유 DB 에 흔적이 남는다.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.c = conn
        self.c.execute("SAVEPOINT flow")

    def commit(self) -> None:
        self.c.execute("RELEASE SAVEPOINT flow")
        self.c.execute("SAVEPOINT flow")

    def rollback(self) -> None:
        self.c.execute("ROLLBACK TO SAVEPOINT flow")

    def close(self) -> None:  # 바깥 fixture 가 닫는다
        pass

    def __getattr__(self, name: str):
        return getattr(self.c, name)


def test_할당이_터진_예약은_그날_놓아주고_가용재고가_돌아온다(conn: psycopg.Connection) -> None:
    from psycopg import sql

    from app.logistics import outbound
    from app.master.outbound_flow import DueSaleItem, ship_due_sales

    _lot(conn, "LOT-A", qty="500", received_at=date(2026, 7, 1))  # AS_OF(07-08) 기준 신선
    schema = sql.Identifier(TMP_SCHEMA)
    row = DueSaleItem(
        sale_id=SALE_ID,
        sale_item_id=SALE_ITEM_ID,
        item_id=ITEM_ID,
        sim_run_id=SIM_RUN_ID,
        quantity_kg=Decimal(120),
        sale_date=AS_OF,
    )

    def 터지는_할당(*a, **k):
        raise RuntimeError("걷기 당일에만 있던 일시적 조건")

    out = ship_due_sales(
        AS_OF,
        sim_run_id=SIM_RUN_ID,
        connect=lambda: _Nested(conn),
        due_fn=lambda _c, *, as_of, sim_run_id: (row,),
        allocate_fn=터지는_할당,
    )
    assert out.status == "RAN"
    (one,) = out.items
    assert one.status == "FAILED" and "놓아줬다" in one.reason, one

    with conn.cursor() as cur:
        cur.execute(f"SELECT status, released_as_of FROM {TMP_SCHEMA}.inventory_reservations")
        rows = [dict(r) if isinstance(r, dict) else r for r in cur.fetchall()]
    assert (
        len(rows) == 1 and rows[0]["status"] == "RELEASED" and rows[0]["released_as_of"] == AS_OF
    ), rows
    # 🔴 핵심: 고아가 안 남아 이 품목 가용재고가 전량 돌아온다
    free = outbound.item_free_stock_qty(
        conn, schema, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF
    )
    assert free == Decimal(500), free
