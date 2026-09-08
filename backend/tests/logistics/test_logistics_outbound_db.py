"""출고 파이프라인을 **실제 PostgreSQL 에서** 끝까지 돌린다 (3-C1).

```text
Lot → Reservation → FEFO 후보 → Allocation → Shipment → Ledger OUT
```

한 트랜잭션 안에서 전부 돌고 끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에는
아무것도 남지 않는다.

🔴 **가짜 커서로는 못 재는 것들을 잰다.**

```text
예약이 잔량을 안 줄이는가        on_hand ≠ available
가용량이 남의 할당을 반영하는가   같은 100kg 을 둘이 못 쓴다
FEFO 정렬이 실제 SQL 위에서 맞나
원장 OUT 이 잔량을 줄이는가       Ledger 밖에서 UPDATE 를 복제하지 않는다
CHECK · FK · 상태 어휘
```
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from app.contracts.sales_logistics import SalesOutboundReservationRequest
from app.logistics import fefo_allocation, ledger, outbound
from app.logistics.db import get_connection
from app.logistics.fefo_allocation import allocate_reserved_stock_fefo
from app.logistics.outbound import (
    AllocationRequest,
    InvalidOutboundRequest,
    OutboundIntegrityError,
    ReservationConflict,
    allocate_stock,
    allocation_id_for,
    move_id_for_allocation,
    recommend_fefo_candidates,
    release_reservation,
    reservation_allocation_state,
    reserve_available_stock,
    reserve_stock,
    ship_allocated_stock,
)
from app.logistics.sales_outbound import reserve_confirmed_sale

pytestmark = pytest.mark.db

TMP_SCHEMA = "outbound_verify"
SIM_RUN_ID = "SIM-OUTBOUND-TEST"
ITEM_ID = "ITEM-BAECHU"
OTHER_ITEM = "ITEM-MU"
SALE_ID = "SALE-TEST-1"
SALE_ITEM_ID = "SITEM-TEST-1"
RSV = "RSV-TEST-1"
AS_OF = date(2026, 1, 20)
DECIDED_AT = datetime(2026, 1, 20, 9, 0, tzinfo=UTC)
DECIDED_BY = "WH-PLANNER-01"
ZONE = "COLD_HUMID_0_3"

_DB_DIR = Path(__file__).resolve().parents[3] / "database"

_STUBS = f"""
CREATE TABLE {TMP_SCHEMA}.items (item_id text PRIMARY KEY, item_name text);
CREATE TABLE {TMP_SCHEMA}.partners (partner_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sim_runs (sim_run_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.purchase_items (purchase_item_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sales (sale_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sale_items (sale_item_id text PRIMARY KEY);
"""


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**."""
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                코드 = 코드.replace(doc, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _repo_block(table: str) -> str:
    text = (_DB_DIR / "10_domain_schema.sql").read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE haetdeul\.{table}\s*\(.*?\n\);", text, re.DOTALL)
    assert match is not None, f"10_domain_schema.sql 에 {table} 이 없다"
    parts = [match.group(0)]
    parts += re.findall(rf"ALTER TABLE ONLY haetdeul\.{table}\s+ADD CONSTRAINT [^;]+;", text)
    return "\n".join(parts)


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection]:
    connection = get_connection()
    connection.autocommit = False
    try:
        with connection.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {TMP_SCHEMA}")
            cur.execute(_STUBS)
            for table in ("inventory_lots", "inventory_moves", "item_storage_policies"):
                cur.execute(_repo_block(table).replace("haetdeul.", f"{TMP_SCHEMA}."))
            wms = (_DB_DIR / "30_logistics_wms_schema.sql").read_text(encoding="utf-8")
            wms = re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", wms)
            cur.execute(wms.replace("haetdeul.", f"{TMP_SCHEMA}."))
            nullable = (_DB_DIR / "logistics_inventory_lots_nullable.sql").read_text(
                encoding="utf-8"
            )
            nullable = re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", nullable)
            cur.execute(nullable.replace("haetdeul.", f"{TMP_SCHEMA}."))

            cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (SIM_RUN_ID,))
            for item, name in ((ITEM_ID, "배추"), (OTHER_ITEM, "무")):
                cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (item, name))
                cur.execute(
                    f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
                    " (item_id, storage_zone, operational_limit_days,"
                    " operational_policy_status) VALUES (%s, %s, 30, 'PROVISIONAL')",
                    (item, ZONE),
                )
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES ('PI-TEST')")
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.sales VALUES (%s)", (SALE_ID,))
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.sale_items VALUES (%s)", (SALE_ITEM_ID,))
        for module in (outbound, ledger):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        # 🔴 COMMIT 하지 않는다 — 공유 DB 에 시험 흔적을 남기지 않는다.
        connection.rollback()
        connection.close()


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _lot(
    conn: psycopg.Connection,
    lot_id: str,
    *,
    qty: str,
    received_at: date,
    item_id: str = ITEM_ID,
    status: str = "ACTIVE",
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status
                ) VALUES (%s, %s, 'PI-TEST', %s, %s, %s, %s, 1000, %s, %s)""",
            (lot_id, SIM_RUN_ID, item_id, received_at, Decimal(qty), Decimal(qty), ZONE, status),
        )
    return lot_id


def _remaining(conn: psycopg.Connection, lot_id: str) -> Decimal:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT remaining_qty_kg FROM {TMP_SCHEMA}.inventory_lots WHERE lot_id = %s",
            (lot_id,),
        )
        row = cur.fetchone()
    return row[0] if not isinstance(row, dict) else row["remaining_qty_kg"]


def _moves(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {TMP_SCHEMA}.inventory_moves ORDER BY move_id")
        이름 = [d.name for d in cur.description]
        return [
            r if isinstance(r, dict) else dict(zip(이름, r, strict=True)) for r in cur.fetchall()
        ]


def _예약상태(conn: psycopg.Connection, reservation_id: str = RSV) -> str:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT status FROM {TMP_SCHEMA}.inventory_reservations WHERE reservation_id = %s",
            (reservation_id,),
        )
        row = cur.fetchone()
    return row[0] if not isinstance(row, dict) else row["status"]


def _할당(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT allocation_id, reservation_id, lot_id, allocated_qty_kg, status,"
            f" allocation_basis, decided_by, decided_at"
            f" FROM {TMP_SCHEMA}.inventory_allocations ORDER BY allocation_id"
        )
        이름 = [d.name for d in cur.description]
        return [
            r if isinstance(r, dict) else dict(zip(이름, r, strict=True)) for r in cur.fetchall()
        ]


def _예약(conn: psycopg.Connection, *, rid: str = RSV, qty: str = "100") -> None:
    reserve_stock(
        conn,
        reservation_id=rid,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(qty),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )


def _할당한다(
    conn: psycopg.Connection,
    *reqs: tuple[str, str],
    rid: str = RSV,
    basis: str = "FEFO_TOOL_CONFIRMED",
):
    """★ `allocation_basis` 에 **기본값이 없다** — 도우미가 명시해서 넘긴다."""
    return allocate_stock(
        conn,
        reservation_id=rid,
        requests=[AllocationRequest(lot_id=l, quantity_kg=Decimal(q)) for l, q in reqs],
        decided_by=DECIDED_BY,
        decided_at=DECIDED_AT,
        allocation_basis=basis,  # type: ignore[arg-type]
        as_of=AS_OF,
    )


# ── 1~7. Reservation ────────────────────────────────────────────────────


def test_1_정상_예약(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))

    결과 = reserve_stock(
        conn,
        reservation_id=RSV,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(80),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )

    assert 결과.applied is True
    assert 결과.status == "RESERVED"
    assert _예약상태(conn) == "RESERVED"


def test_2_같은_사실_재실행은_멱등이다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")

    두번 = reserve_stock(
        conn,
        reservation_id=RSV,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(80),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )

    assert 두번.applied is False


def test_3_같은_id_다른_사실은_충돌이다(conn: psycopg.Connection) -> None:
    """🔴 수량을 조용히 덮으면 앞 요청이 소리 없이 사라진다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")

    with pytest.raises(ReservationConflict):
        reserve_stock(
            conn,
            reservation_id=RSV,
            sim_run_id=SIM_RUN_ID,
            item_id=ITEM_ID,
            required_qty_kg=Decimal(90),
            sale_id=SALE_ID,
            as_of=AS_OF,
        )


def test_4_가용_초과_예약은_거부된다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))

    with pytest.raises(InvalidOutboundRequest, match="가용재고가 모자라"):
        reserve_stock(
            conn,
            reservation_id=RSV,
            sim_run_id=SIM_RUN_ID,
            item_id=ITEM_ID,
            required_qty_kg=Decimal(101),
            sale_id=SALE_ID,
            as_of=AS_OF,
        )


def test_5_다른_예약의_할당이_가용에서_빠진다(conn: psycopg.Connection) -> None:
    """🔴 **경합의 핵심.** 남이 잡아 둔 몫을 또 잡으면 같은 재고가 두 번 나간다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, rid="RSV-남", qty="80")
    _할당한다(conn, ("LOT-A", "80"), rid="RSV-남")

    with pytest.raises(InvalidOutboundRequest, match="가용재고가 모자라"):
        reserve_stock(
            conn,
            reservation_id=RSV,
            sim_run_id=SIM_RUN_ID,
            item_id=ITEM_ID,
            required_qty_kg=Decimal(30),
            sale_id=SALE_ID,
            as_of=AS_OF,
        )


def test_6_취소하면_가용이_돌아온다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, rid="RSV-남", qty="80")
    _할당한다(conn, ("LOT-A", "80"), rid="RSV-남")

    결과 = release_reservation(conn, reservation_id="RSV-남")

    assert 결과.applied is True and 결과.status == "RELEASED"
    assert all(행["status"] == "CANCELLED" for 행 in _할당(conn))
    # ★ 이제 다시 잡을 수 있다.
    _예약(conn, qty="90")
    assert _예약상태(conn) == "RESERVED"
    assert _moves(conn) == [], "취소는 원장 Move 를 만들지 않는다"


def test_7_예약만으로는_Lot_잔량이_안_변한다(conn: psycopg.Connection) -> None:
    """🔴 `on_hand ≠ available` — 물건은 아직 창고에 있다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))

    _예약(conn, qty="80")
    _할당한다(conn, ("LOT-A", "80"))

    assert _remaining(conn, "LOT-A") == Decimal(100)
    assert _moves(conn) == []


# ── 8~13. FEFO ──────────────────────────────────────────────────────────


def test_8_신선도가_짧은_Lot_이_먼저다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-NEW", qty="50", received_at=date(2026, 1, 15))
    _lot(conn, "LOT-OLD", qty="50", received_at=date(2026, 1, 1))

    후보 = recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)

    assert [c.lot_id for c in 후보] == ["LOT-OLD", "LOT-NEW"]
    assert 후보[0].remaining_freshness_days < 후보[1].remaining_freshness_days


def test_9_10_동률이면_입고일_그다음_lot_id_다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-B", qty="50", received_at=date(2026, 1, 5))
    _lot(conn, "LOT-A", qty="50", received_at=date(2026, 1, 5))

    후보 = recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)

    assert [c.lot_id for c in 후보] == ["LOT-A", "LOT-B"]


def test_11_가용_0_은_후보에서_빠진다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-B", qty="50", received_at=date(2026, 1, 2))
    _예약(conn, rid="RSV-남", qty="100")
    _할당한다(conn, ("LOT-A", "100"), rid="RSV-남")

    후보 = recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)

    assert [c.lot_id for c in 후보] == ["LOT-B"]


def test_12_후보의_가용량이_남의_할당을_반영한다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, rid="RSV-남", qty="30")
    _할당한다(conn, ("LOT-A", "30"), rid="RSV-남")

    후보 = recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)

    assert 후보[0].available_qty_kg == Decimal(70)


def test_13_FEFO_는_자동으로_할당하지_않는다(conn: psycopg.Connection) -> None:
    """🔴 **추천까지다.** 코드가 Lot 을 고르면 그 선택이 근거 없이 굳는다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="50")

    recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)

    assert _할당(conn) == []
    assert _예약상태(conn) == "RESERVED"


def test_13b_비ACTIVE_Lot_은_후보가_아니다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-HOLD", qty="100", received_at=date(2026, 1, 1), status="HOLD")

    후보 = recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)

    assert 후보 == ()


# ── 14~20. Allocation ───────────────────────────────────────────────────


def test_14_명시한_Lot_에_할당한다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")

    결과 = _할당한다(conn, ("LOT-A", "80"))

    assert 결과.applied is True
    assert 결과.reservation_status == "ALLOCATED"
    행 = _할당(conn)
    assert len(행) == 1
    assert 행[0]["allocation_id"] == allocation_id_for(reservation_id=RSV, lot_id="LOT-A")
    assert 행[0]["allocated_qty_kg"] == Decimal(80)
    assert 행[0]["allocation_basis"] == "FEFO_TOOL_CONFIRMED"


def test_15_여러_Lot_으로_나눠_할당한다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-B", qty="60", received_at=date(2026, 1, 2))
    _예약(conn, qty="100")

    결과 = _할당한다(conn, ("LOT-A", "60"), ("LOT-B", "40"))

    assert 결과.allocated_qty_kg == Decimal(100)
    assert 결과.reservation_status == "ALLOCATED"
    assert [행["lot_id"] for 행 in _할당(conn)] == ["LOT-A", "LOT-B"]


def test_15b_일부만_할당하면_PARTIALLY_ALLOCATED_다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="100")

    결과 = _할당한다(conn, ("LOT-A", "40"))

    assert 결과.reservation_status == "PARTIALLY_ALLOCATED"
    assert _예약상태(conn) == "PARTIALLY_ALLOCATED"


def test_16_Lot_가용_초과_할당은_막힌다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="50", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-B", qty="60", received_at=date(2026, 1, 2))
    _예약(conn, qty="100")

    # ★ 예약 총량(100)은 두 Lot 합(110) 안에 들어가지만, **한 Lot 의 가용량**은 50 이다.
    with pytest.raises(InvalidOutboundRequest, match="Lot 가용량"):
        _할당한다(conn, ("LOT-A", "60"))

    assert _할당(conn) == []


def test_17_예약_잔여_초과_할당은_막힌다(conn: psycopg.Connection) -> None:
    """★ `reserve_stock` 이 만든 예약은 `reserved == required` 라 임계값이 그대로 100 이다."""
    _lot(conn, "LOT-A", qty="200", received_at=date(2026, 1, 1))
    _예약(conn, qty="100")

    with pytest.raises(InvalidOutboundRequest, match="예약 확보량"):
        _할당한다(conn, ("LOT-A", "150"))


def test_17b_누적_할당도_예약을_못_넘는다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="200", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-B", qty="200", received_at=date(2026, 1, 2))
    _예약(conn, qty="100")
    _할당한다(conn, ("LOT-A", "70"))

    with pytest.raises(InvalidOutboundRequest, match="예약 확보량"):
        _할당한다(conn, ("LOT-B", "40"))


def test_18_다른_예약의_몫을_침범하지_못한다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, rid="RSV-남", qty="70")
    _할당한다(conn, ("LOT-A", "70"), rid="RSV-남")
    _lot(conn, "LOT-B", qty="100", received_at=date(2026, 1, 2))
    _예약(conn, qty="50")

    with pytest.raises(InvalidOutboundRequest, match="Lot 가용량"):
        _할당한다(conn, ("LOT-A", "50"))


def test_19_같은_할당_재실행은_멱등이다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")
    첫번 = _할당한다(conn, ("LOT-A", "80"))

    두번 = _할당한다(conn, ("LOT-A", "80"))

    assert 첫번.applied is True and 두번.applied is False
    assert len(_할당(conn)) == 1


def test_20_같은_할당에_다른_수량이면_충돌이다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="100")
    _할당한다(conn, ("LOT-A", "80"))

    with pytest.raises(ReservationConflict):
        _할당한다(conn, ("LOT-A", "20"))


# ── 21~28. Shipment ─────────────────────────────────────────────────────


def test_21_22_23_24_실출고에서만_원장_OUT_이_나간다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")
    _할당한다(conn, ("LOT-A", "80"))
    assert _moves(conn) == [], "할당까지는 원장이 없다"

    결과 = ship_allocated_stock(
        conn, reservation_id=RSV, shipped_at=AS_OF, sale_item_id=SALE_ITEM_ID
    )

    assert 결과.applied is True
    moves = _moves(conn)
    assert len(moves) == 1
    assert moves[0]["move_type"] == "OUT"
    assert moves[0]["quantity_kg"] == Decimal(80)
    assert moves[0]["reason_code"] == "SALE_FULFILLMENT"
    assert moves[0]["sale_item_id"] == SALE_ITEM_ID
    assert moves[0]["moved_at"] == AS_OF
    assert _remaining(conn, "LOT-A") == Decimal(20), "원장이 잔량을 줄인다"
    assert all(행["status"] == "SHIPPED" for 행 in _할당(conn))


def test_25_두_Lot_이면_Move_가_두_건이다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-B", qty="60", received_at=date(2026, 1, 2))
    _예약(conn, qty="100")
    _할당한다(conn, ("LOT-A", "60"), ("LOT-B", "40"))

    결과 = ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    assert len(결과.move_ids) == 2
    assert len(_moves(conn)) == 2
    assert _remaining(conn, "LOT-A") == 0
    assert _remaining(conn, "LOT-B") == Decimal(20)
    assert set(결과.move_ids) == {
        move_id_for_allocation(allocation_id=allocation_id_for(reservation_id=RSV, lot_id=lot))
        for lot in ("LOT-A", "LOT-B")
    }


def test_26_27_재출고는_중복_OUT_을_만들지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")
    _할당한다(conn, ("LOT-A", "80"))
    ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    두번 = ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    assert 두번.applied is False
    assert 두번.shipped_allocation_ids == ()
    assert len(_moves(conn)) == 1
    assert _remaining(conn, "LOT-A") == Decimal(20), "잔량이 두 번 줄지 않는다"


def test_27b_출고된_예약은_취소할_수_없다(conn: psycopg.Connection) -> None:
    """🔴 나간 재고를 예약 취소로 되돌리지 않는다 — 환입은 이 판의 범위가 아니다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")
    _할당한다(conn, ("LOT-A", "80"))
    ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    with pytest.raises(OutboundIntegrityError, match="이미 출고된"):
        release_reservation(conn, reservation_id=RSV)


def test_28_예약만_하고_출고하지_않으면_원장이_없다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")

    결과 = ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    assert 결과.applied is False
    assert _moves(conn) == []
    assert _remaining(conn, "LOT-A") == Decimal(100)


# ── 29~33. 동시성 / 트랜잭션 ───────────────────────────────────────────


def test_29_출고_전역_잠금을_먼저_잡는다(conn: psycopg.Connection) -> None:
    """★ 잠금 뒤에 가용량을 다시 센다 — 잠금 밖의 값은 이미 낡았을 수 있다."""
    코드 = _코드만(Path(outbound.__file__).read_text(encoding="utf-8"))

    assert "pg_advisory_xact_lock" in 코드
    assert "_OUTBOUND_LOCK_OBJID = 3" in 코드
    # ★ 각 쓰기 진입점이 잠금을 먼저 잡는다.
    for 함수 in ("reserve_stock", "allocate_stock", "ship_allocated_stock", "release_reservation"):
        조각 = 코드.split(f"def {함수}(")[1]
        assert "lock_outbound_writes" in 조각.split("def ")[0], f"{함수} 가 잠금을 안 잡는다"


def test_29b_잠금_키가_기존_둘과_안_겹친다(conn: psycopg.Connection) -> None:
    """★ `(…,1)` 원장 · `(…,2)` 도착 · `(…,3)` 출고."""
    from app.logistics import receipts

    assert (outbound._OUTBOUND_LOCK_CLASSID, outbound._OUTBOUND_LOCK_OBJID) == (20260905, 3)
    assert (ledger._LEDGER_LOCK_CLASSID, ledger._LEDGER_LOCK_OBJID) == (20260905, 1)
    assert (receipts._ARRIVAL_LOCK_CLASSID, receipts._ARRIVAL_LOCK_OBJID) == (20260905, 2)


def test_30_33_커밋도_롤백도_새_커넥션도_없다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")
    _할당한다(conn, ("LOT-A", "80"))
    ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    assert conn.info.transaction_status.name in {"INTRANS", "INERROR"}
    코드 = _코드만(Path(outbound.__file__).read_text(encoding="utf-8"))
    assert "get_connection" not in 코드
    assert "commit" not in 코드
    assert "rollback" not in 코드


def test_전체_흐름이_한_트랜잭션에서_되돌려진다(conn: psycopg.Connection) -> None:
    """🔴 롤백 검증 — 공유 DB 규율이 실제로 성립하는지 본다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")
    후보 = recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)
    _할당한다(conn, (후보[0].lot_id, "80"))
    ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF, sale_item_id=SALE_ITEM_ID)
    assert len(_moves(conn)) == 1

    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", [f"{TMP_SCHEMA}.inventory_reservations"])
        남았나 = cur.fetchone()
    assert (남았나[0] if not isinstance(남았나, dict) else 남았나["to_regclass"]) is None


def test_sales_boundary_request에서_명시적_lot_선택으로_inventory_OUT까지_간다(
    conn: psycopg.Connection,
) -> None:
    """Sales 사실은 예약까지만 만들고, 테스트가 명시적으로 Lot 을 선택한다."""
    _lot(conn, "LOT-NEW", qty="100", received_at=date(2026, 1, 15))
    _lot(conn, "LOT-OLD", qty="100", received_at=date(2026, 1, 1))
    request = SalesOutboundReservationRequest(
        reservation_id="RSV-SI-SALE-1-1",
        sim_run_id=SIM_RUN_ID,
        sale_id=SALE_ID,
        sale_item_id=SALE_ITEM_ID,
        item_id=ITEM_ID,
        quantity_kg=Decimal(80),
        as_of=AS_OF,
    )

    reserved = reserve_confirmed_sale(conn, request)
    candidates = recommend_fefo_candidates(
        conn,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        as_of=AS_OF,
    )
    chosen = candidates[0]
    allocated = allocate_stock(
        conn,
        reservation_id=request.reservation_id,
        requests=[AllocationRequest(lot_id=chosen.lot_id, quantity_kg=request.quantity_kg)],
        decided_by=DECIDED_BY,
        decided_at=DECIDED_AT,
        allocation_basis="FEFO_TOOL_CONFIRMED",
        as_of=AS_OF,
    )
    shipped = ship_allocated_stock(
        conn,
        reservation_id=request.reservation_id,
        shipped_at=AS_OF,
        sale_item_id=request.sale_item_id,
    )

    moves = _moves(conn)
    assert reserved.status == "RESERVED"
    assert [candidate.lot_id for candidate in candidates] == ["LOT-OLD", "LOT-NEW"]
    assert allocated.reservation_status == "ALLOCATED"
    assert shipped.applied is True
    assert _remaining(conn, "LOT-OLD") == Decimal(20)
    assert _remaining(conn, "LOT-NEW") == Decimal(100)
    assert moves[0]["move_type"] == "OUT"
    assert moves[0]["sale_item_id"] == SALE_ITEM_ID


# ── 34~38. 범위 ────────────────────────────────────────────────────────


def test_34_38_범위_밖_어휘를_쓰지_않는다(conn: psycopg.Connection) -> None:
    코드 = _코드만(Path(outbound.__file__).read_text(encoding="utf-8"))

    for 금지 in ("inventory_count", "ADJUST", "DISPOSE", "app.master", "app.sales"):
        assert 금지 not in 코드, f"{금지} — 이 판의 범위가 아니다"


def test_원장_잔량_UPDATE_를_복제하지_않는다(conn: psycopg.Connection) -> None:
    """🔴 `remaining_qty_kg` 를 바꾸는 것은 원장뿐이다."""
    코드 = _코드만(Path(outbound.__file__).read_text(encoding="utf-8"))

    assert "remaining_qty_kg =" not in 코드
    assert "record_inventory_move" in 코드, "원장을 재사용해야 한다"


def test_sale_item_id_를_지어내지_않는다(conn: psycopg.Connection) -> None:
    """★ Sales 소유 참조다 — 아직 안 넘어오면 `None` 으로 나간다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")
    _할당한다(conn, ("LOT-A", "80"))

    ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    assert _moves(conn)[0]["sale_item_id"] is None
    코드 = _코드만(Path(outbound.__file__).read_text(encoding="utf-8"))
    assert "SITEM-" not in 코드, "판매 ID 를 조립하고 있다"


# ══════════════════════════════════════════════════════════════════════════
# 예약 이중 확보 방지 (품목 예약 가능량)
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 **Lot 별 가용량의 합만 보면 같은 재고를 두 번 예약한다.**
#
#    ```text
#    Lot remaining 100 · 예약 A 80 (할당 0)
#    Lot 가용량 합 = 100   ← A 가 어느 Lot 도 안 골랐으니 안 빠진다
#    ⇒ 예약 B 80 이 통과해 버린다
#    ```


def _예약한다(conn: psycopg.Connection, rid: str, qty: str):
    return reserve_stock(
        conn,
        reservation_id=rid,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(qty),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )


def test_D1_할당_없는_예약도_다음_예약을_막는다(conn: psycopg.Connection) -> None:
    """🔴 **이 판이 고치는 자리다.** 종전에는 B 가 통과했다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "80")
    assert _할당(conn) == [], "아직 Lot 을 안 골랐다"

    with pytest.raises(InvalidOutboundRequest, match="가용재고가 모자라"):
        _예약한다(conn, "RSV-B", "80")


def test_D2_남은_몫만큼은_예약된다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "80")

    결과 = _예약한다(conn, "RSV-B", "20")

    assert 결과.applied is True
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-C", "1")


def test_D3_일부_할당해도_확보_총량은_그대로다(conn: psycopg.Connection) -> None:
    """★ 미할당 예약 50 + 할당 30 = 80 — 어느 쪽으로 세도 A 의 몫은 80 이다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "80")
    _할당한다(conn, ("LOT-A", "30"), rid="RSV-A")

    assert _예약상태(conn, "RSV-A") == "PARTIALLY_ALLOCATED"
    결과 = _예약한다(conn, "RSV-B", "20")
    assert 결과.applied is True
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-C", "1")


def test_D4_출고분은_이중_차감되지_않는다(conn: psycopg.Connection) -> None:
    """🔴 **출고 뒤 이중 차감 검사.**

    ```text
    Lot 100 · 예약 A 80 · A 중 30 할당 → 출고
    remaining 70 · A 가 아직 잡은 미출고 50
    ⇒ B 가 예약할 수 있는 양 = 20
    ```

    `SHIPPED` 를 예약 잔여에서 안 빼면 `70 − 0 − 80 = −10` 이 되어 **아무도 예약을
    못 하게 된다.**
    """
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "80")
    _할당한다(conn, ("LOT-A", "30"), rid="RSV-A")
    ship_allocated_stock(conn, reservation_id="RSV-A", shipped_at=AS_OF)

    assert _remaining(conn, "LOT-A") == Decimal(70)
    결과 = _예약한다(conn, "RSV-B", "20")
    assert 결과.applied is True
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-C", "1")


@pytest.mark.parametrize("상태", ["RELEASED", "CANCELLED"])
def test_D5_D6_놓아준_예약은_가용을_돌려준다(conn: psycopg.Connection, 상태: str) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "100")
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-B", "10")

    release_reservation(conn, reservation_id="RSV-A", status=상태)

    assert _예약한다(conn, "RSV-B", "100").applied is True


def test_D7_PARTIALLY_ALLOCATED_도_전체_몫을_지킨다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "100")
    _할당한다(conn, ("LOT-A", "40"), rid="RSV-A")

    assert _예약상태(conn, "RSV-A") == "PARTIALLY_ALLOCATED"
    with pytest.raises(InvalidOutboundRequest, match="가용재고가 모자라"):
        _예약한다(conn, "RSV-B", "1")


def test_D8_ALLOCATED_도_출고_전까지_전체_몫을_지킨다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "100")
    _할당한다(conn, ("LOT-A", "100"), rid="RSV-A")

    assert _예약상태(conn, "RSV-A") == "ALLOCATED"
    with pytest.raises(InvalidOutboundRequest, match="가용재고가 모자라"):
        _예약한다(conn, "RSV-B", "1")


def test_D9_같은_예약_재실행은_자기를_다시_차감하지_않는다(conn: psycopg.Connection) -> None:
    """🔴 자기 예약을 또 빼면 **멀쩡한 재실행이 가용 부족으로 터진다.**"""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "100")

    두번 = _예약한다(conn, "RSV-A", "100")

    assert 두번.applied is False
    assert 두번.status == "RESERVED"


def test_D10_비ACTIVE_Lot_은_양쪽에서_함께_빠진다(conn: psycopg.Connection) -> None:
    """★ `on_hand` 에 안 들어가는 Lot 의 할당을 빼면 과다 차감이 된다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-HOLD", qty="50", received_at=date(2026, 1, 1), status="HOLD")

    assert _예약한다(conn, "RSV-A", "100").applied is True
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-B", "1")


def test_D11_다른_품목의_예약은_영향을_주지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-MU", qty="100", received_at=date(2026, 1, 1), item_id=OTHER_ITEM)
    reserve_stock(
        conn,
        reservation_id="RSV-MU",
        sim_run_id=SIM_RUN_ID,
        item_id=OTHER_ITEM,
        required_qty_kg=Decimal(100),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )

    assert _예약한다(conn, "RSV-A", "100").applied is True


def test_D12_전체_시나리오를_한_트랜잭션에서_검증한다(conn: psycopg.Connection) -> None:
    """★ 요청하신 걷기 그대로.

    ```text
    Lot 100 → Reserve A 80 → Reserve B 30 실패 → Allocate A 30
    → Reserve B 30 여전히 실패 → Ship A 30 → remaining 70
    → A 미출고 50 → B reservable 20
    ```
    """
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))

    assert _예약한다(conn, "RSV-A", "80").applied is True
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-B", "30")

    _할당한다(conn, ("LOT-A", "30"), rid="RSV-A")
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-B", "30")

    ship_allocated_stock(conn, reservation_id="RSV-A", shipped_at=AS_OF)
    assert _remaining(conn, "LOT-A") == Decimal(70)

    assert _예약한다(conn, "RSV-B", "20").applied is True
    with pytest.raises(InvalidOutboundRequest):
        _예약한다(conn, "RSV-C", "1")


def test_D13_음수_예약가능량은_0_으로_보정하지_않는다(conn: psycopg.Connection) -> None:
    """🔴 음수는 **잡힌 몫이 실재 재고를 넘었다**는 뜻이라 데이터 문제다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약한다(conn, "RSV-A", "100")
    # ★ 손으로 재고를 줄여 모순 상태를 만든다.
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TMP_SCHEMA}.inventory_lots SET remaining_qty_kg = 50")

    with pytest.raises(OutboundIntegrityError, match="음수"):
        _예약한다(conn, "RSV-B", "1")


# ── allocation_basis 는 명시 입력이다 ──────────────────────────────────
#
# 🔴 **FEFO 후보를 불러 봤다는 사실과 그 추천을 따랐다는 사실은 다르다.**
#    기본값을 두면 묻지도 않고 뒤엣것을 장부에 적어, 사람이 다른 Lot 을 골랐어도
#    *"Tool 이 추천한 대로 했다"* 로 남는다.


@pytest.mark.parametrize("basis", ["FEFO_TOOL_CONFIRMED", "HUMAN_OVERRIDE"])
def test_B1_B2_명시한_할당_근거가_그대로_기록된다(conn: psycopg.Connection, basis: str) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")

    _할당한다(conn, ("LOT-A", "80"), basis=basis)

    assert _할당(conn)[0]["allocation_basis"] == basis


def test_B3_할당_근거에_기본값이_없다(conn: psycopg.Connection) -> None:
    """★ 안 주면 `TypeError` 다 — 규약이 다시 흐려지면 여기서 걸린다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")

    with pytest.raises(TypeError, match="allocation_basis"):
        allocate_stock(
            conn,
            reservation_id=RSV,
            requests=[AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(80))],
            decided_by=DECIDED_BY,
            decided_at=DECIDED_AT,
            as_of=AS_OF,
        )

    assert _할당(conn) == []


def test_B4_계약_밖_근거는_거부된다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _예약(conn, qty="80")

    with pytest.raises(InvalidOutboundRequest, match="할당 근거"):
        _할당한다(conn, ("LOT-A", "80"), basis="AUTO_PICKED")

    assert _할당(conn) == []


# ── S. 시뮬레이션 경로 — 부분 예약 · 자동 FEFO 할당 ────────────────────
#
# 🔴 **사람 경로를 덮지 않고 갈라 둔 것을 확인한다.** 위 1~38 · D · B 는 전부
#    `reserve_stock` · `allocate_stock` 을 직접 부르는 사람 경로이고, 아래는
#    `reserve_available_stock` · `allocate_reserved_stock_fefo` 다.


def _부분예약(
    conn: psycopg.Connection, *, rid: str = RSV, required: str = "100"
) -> outbound.ReservationResult:
    return reserve_available_stock(
        conn,
        reservation_id=rid,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(required),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )


def _예약행(conn: psycopg.Connection, rid: str = RSV) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT required_qty_kg, reserved_qty_kg, status"
            f" FROM {TMP_SCHEMA}.inventory_reservations WHERE reservation_id = %s",
            (rid,),
        )
        이름 = [d.name for d in cur.description]
        행 = cur.fetchone()
    return 행 if isinstance(행, dict) else dict(zip(이름, 행, strict=True))


def _예약수(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TMP_SCHEMA}.inventory_reservations")
        센것 = cur.fetchone()
    return int(센것[0] if not isinstance(센것, dict) else 센것["count"])


def _자동할당(conn: psycopg.Connection, rid: str = RSV):
    return allocate_reserved_stock_fefo(
        conn, reservation_id=rid, as_of=AS_OF, decided_at=DECIDED_AT
    )


# ── S1~S6. 부분 Reservation ─────────────────────────────────────────────


def test_S1_모자라면_확보되는_만큼만_잡는다(conn: psycopg.Connection) -> None:
    """🔴 `reserve_stock` 이면 여기서 멈춘다 — 부분 예약은 멈추지 않는다."""
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))

    결과 = _부분예약(conn, required="100")

    assert 결과.applied is True
    assert 결과.required_qty_kg == Decimal(100)
    assert 결과.reserved_qty_kg == Decimal(60)
    행 = _예약행(conn)
    # ★ **원 요구량은 그대로다.** 못 낸 40 이 요구량에서 사라지지 않는다.
    assert 행["required_qty_kg"] == Decimal(100)
    assert 행["reserved_qty_kg"] == Decimal(60)
    assert 행["status"] == "RESERVED"


def test_S2_새_가용이_없으면_재실행이_no_op_다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")

    두번 = _부분예약(conn, required="100")

    assert 두번.applied is False
    assert 두번.reserved_qty_kg == Decimal(60)
    행 = _예약행(conn)
    assert (행["required_qty_kg"], 행["reserved_qty_kg"]) == (Decimal(100), Decimal(60))
    # 🔴 행이 하나여야 한다 — 두 번 잡으면 같은 판매가 재고를 두 배로 든다.
    assert _예약수(conn) == 1


def test_S3_새_재고가_들어오면_재실행이_채운다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")

    _lot(conn, "LOT-B", qty="30", received_at=date(2026, 1, 5))
    세번째 = _부분예약(conn, required="100")

    assert 세번째.applied is True
    assert 세번째.reserved_qty_kg == Decimal(90)
    assert _예약행(conn)["reserved_qty_kg"] == Decimal(90)
    assert _예약수(conn) == 1


def test_S4_top_up_은_required_를_못_넘는다(conn: psycopg.Connection) -> None:
    """★ 남은 20 만 가져간다 — 새 재고가 50 이어도 요구량이 뚜껑이다."""
    _lot(conn, "LOT-A", qty="80", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")

    _lot(conn, "LOT-B", qty="50", received_at=date(2026, 1, 5))
    결과 = _부분예약(conn, required="100")

    assert 결과.reserved_qty_kg == Decimal(100)
    assert _예약행(conn)["reserved_qty_kg"] == Decimal(100)
    # ★ 다 찼으니 그다음은 no-op 이다.
    assert _부분예약(conn, required="100").applied is False


def test_S5_남의_확보량이_내_가용에서_빠진다(conn: psycopg.Connection) -> None:
    """🔴 부분 예약도 `item_free_stock_qty` 축에 그대로 선다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, rid="RSV-A", required="70")

    둘째 = _부분예약(conn, rid="RSV-B", required="100")

    assert 둘째.reserved_qty_kg == Decimal(30)


def test_S6_가용이_0_이면_빈_예약을_만들지_않는다(conn: psycopg.Connection) -> None:
    """🔴 잡은 것이 없는 예약 행은 *"무언가 잡혀 있다"* 로 보이면서 아무 몫도 안 든다."""
    결과 = _부분예약(conn, required="100")

    assert 결과.applied is False
    assert 결과.reserved_qty_kg == Decimal(0)
    assert _예약수(conn) == 0


def test_S6b_같은_id_에_다른_요구량이면_충돌이다(conn: psycopg.Connection) -> None:
    """★ 요구량이 달라졌다면 top-up 이 아니라 **다른 판매**다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="60")

    with pytest.raises(ReservationConflict):
        _부분예약(conn, required="80")


def test_S6c_놓아준_예약은_다시_채우지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")
    release_reservation(conn, reservation_id=RSV)

    with pytest.raises(OutboundIntegrityError, match="놓아준 예약"):
        _부분예약(conn, required="100")


# ── S7~S8. Allocation 상한은 확보량이다 ─────────────────────────────────


def test_S7_확보량을_넘는_할당은_막힌다(conn: psycopg.Connection) -> None:
    """🔴 required 100 · reserved 60 일 때 **60 까지만** 붙는다."""
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")
    _lot(conn, "LOT-B", qty="200", received_at=date(2026, 1, 2))

    with pytest.raises(InvalidOutboundRequest, match="예약 확보량"):
        _할당한다(conn, ("LOT-B", "61"))

    assert _할당(conn) == []


def test_S8_확보량까지는_붙는다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")

    결과 = _할당한다(conn, ("LOT-A", "60"))

    assert 결과.allocated_qty_kg == Decimal(60)
    # ★ 요구량(100)을 다 못 냈으므로 예약은 아직 PARTIALLY_ALLOCATED 다.
    assert 결과.reservation_status == "PARTIALLY_ALLOCATED"


# ── S9~S16. 자동 FEFO 할당 ──────────────────────────────────────────────


def test_S9_여러_Lot_에_FEFO_순서로_자동_할당한다(conn: psycopg.Connection) -> None:
    """★ 목표 80 · LOT-A 30 · LOT-B 100 → A 30 · B 50. **마지막 Lot 은 필요한 만큼만.**"""
    _lot(conn, "LOT-A", qty="30", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-B", qty="100", received_at=date(2026, 1, 2))
    _lot(conn, "LOT-C", qty="50", received_at=date(2026, 1, 3))
    _부분예약(conn, required="80")

    결과 = _자동할당(conn)

    assert 결과.applied is True
    assert 결과.allocated_qty_kg == Decimal(80)
    붙은것 = {행["lot_id"]: 행["allocated_qty_kg"] for 행 in _할당(conn)}
    assert 붙은것 == {"LOT-A": Decimal(30), "LOT-B": Decimal(50)}
    # 🔴 필요한 만큼에서 멈춘다 — LOT-C 는 손대지 않는다.
    assert "LOT-C" not in 붙은것


def test_S10_신선도가_짧은_Lot_부터_고른다(conn: psycopg.Connection) -> None:
    """★ 순서의 주인은 `recommend_fefo_candidates` 다 — 그 순서를 그대로 소비한다."""
    _lot(conn, "LOT-NEW", qty="100", received_at=date(2026, 1, 15))
    _lot(conn, "LOT-OLD", qty="40", received_at=date(2026, 1, 1))
    _부분예약(conn, required="60")

    _자동할당(conn)

    붙은것 = {행["lot_id"]: 행["allocated_qty_kg"] for 행 in _할당(conn)}
    assert 붙은것 == {"LOT-OLD": Decimal(40), "LOT-NEW": Decimal(20)}


def test_S11_자동_할당은_합의된_근거와_결정자를_적는다(conn: psycopg.Connection) -> None:
    """🔴 사람이 없다 — `FEFO_TOOL_CONFIRMED` 도 `HUMAN_OVERRIDE` 도 아니다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="80")

    _자동할당(conn)

    행 = _할당(conn)[0]
    assert 행["allocation_basis"] == "FEFO_AUTO_SELECTED"
    assert 행["decided_by"] == "LOGISTICS_FEFO_RULE"
    # ★ 상수와 장부가 갈리지 않는지도 함께 본다.
    assert fefo_allocation.ALLOCATION_BASIS == "FEFO_AUTO_SELECTED"
    assert fefo_allocation.DECIDED_BY == "LOGISTICS_FEFO_RULE"
    assert 행["decided_at"] == DECIDED_AT


def test_S12_자동_할당_재실행은_멱등이다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="80")
    _자동할당(conn)

    두번 = _자동할당(conn)

    assert 두번.applied is False
    assert 두번.allocated_qty_kg == Decimal(80)
    assert [행["allocated_qty_kg"] for 행 in _할당(conn)] == [Decimal(80)]


def test_S13_자동_할당은_남의_몫을_침범하지_않는다(conn: psycopg.Connection) -> None:
    """★ 남이 이미 붙여 둔 Lot 가용량은 후보에서 이미 빠져 있다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _lot(conn, "LOT-B", qty="100", received_at=date(2026, 1, 2))
    reserve_stock(
        conn,
        reservation_id="RSV-OTHER",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(70),
        as_of=AS_OF,
    )
    _할당한다(conn, ("LOT-A", "70"), rid="RSV-OTHER")
    _부분예약(conn, required="60")

    _자동할당(conn)

    붙은것 = {
        행["lot_id"]: 행["allocated_qty_kg"] for 행 in _할당(conn) if 행["reservation_id"] == RSV
    }
    # 🔴 LOT-A 는 30 만 남았다 — 남의 70 을 못 쓴다.
    assert 붙은것 == {"LOT-A": Decimal(30), "LOT-B": Decimal(30)}


def test_S14_자동_할당은_확보량까지만_붙인다(conn: psycopg.Connection) -> None:
    """🔴 목표는 `reserved_qty_kg` 다 — 요구량 100 이어도 확보 60 이면 60 이다."""
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")
    assert _예약행(conn)["reserved_qty_kg"] == Decimal(60)
    _lot(conn, "LOT-B", qty="200", received_at=date(2026, 1, 2))

    결과 = _자동할당(conn)

    assert 결과.allocated_qty_kg == Decimal(60)
    assert sum(행["allocated_qty_kg"] for 행 in _할당(conn)) == Decimal(60)


def test_S15_top_up_뒤_자동_할당이_이어서_붙인다(conn: psycopg.Connection) -> None:
    """★ 이미 붙인 Lot 은 건너뛰고 **다음 FEFO 후보**가 받는다 (할당 수량은 못 덮는다)."""
    _lot(conn, "LOT-A", qty="60", received_at=date(2026, 1, 1))
    _부분예약(conn, required="100")
    _자동할당(conn)

    _lot(conn, "LOT-B", qty="40", received_at=date(2026, 1, 5))
    _부분예약(conn, required="100")
    결과 = _자동할당(conn)

    assert 결과.allocated_qty_kg == Decimal(100)
    붙은것 = {행["lot_id"]: 행["allocated_qty_kg"] for 행 in _할당(conn)}
    assert 붙은것 == {"LOT-A": Decimal(60), "LOT-B": Decimal(40)}
    assert _예약상태(conn) == "ALLOCATED"


def test_S16_확보한_몫을_Lot_에서_못_찾으면_소리를_낸다(conn: psycopg.Connection) -> None:
    """🔴 부분 **예약**과 부분 **할당**은 다른 상황이다 — 뒤엣것은 무결성 문제다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="80")
    # ★ 확보해 둔 뒤 그 Lot 을 비-ACTIVE 로 돌려 Lot 축에서만 사라지게 한다.
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.inventory_lots SET status = 'HOLD' WHERE lot_id = 'LOT-A'"
        )

    with pytest.raises(OutboundIntegrityError, match="다 못 찾았다"):
        _자동할당(conn)

    assert _할당(conn) == []


# ── S17~S20. 기존 계약이 안 깨졌는지 (focused 회귀) ─────────────────────


def test_S17_자동_할당만으로는_잔량이_안_준다(conn: psycopg.Connection) -> None:
    """🔴 `remaining_qty_kg` 를 바꾸는 것은 실출고의 원장 OUT 뿐이다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="80")

    _자동할당(conn)

    assert _remaining(conn, "LOT-A") == Decimal(100)
    assert _moves(conn) == []


def test_S18_실출고에서만_OUT_이_난다(conn: psycopg.Connection) -> None:
    """★ 자동 FEFO 는 출고를 부르지 않는다 — 마스터가 따로 부른다."""
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="80")
    _자동할당(conn)

    출고 = ship_allocated_stock(conn, reservation_id=RSV, shipped_at=AS_OF)

    assert 출고.shipped_qty_kg == Decimal(80)
    assert _remaining(conn, "LOT-A") == Decimal(20)
    assert [행["move_type"] for 행 in _moves(conn)] == ["OUT"]


def test_S19_자동_FEFO_는_범위를_넘지_않는다() -> None:
    """★ 두 단계를 묶으면 *"할당은 됐는데 출고가 실패"* 를 표현할 수 없다."""
    from app.logistics import fefo_allocation

    코드 = _코드만(Path(fefo_allocation.__file__).read_text(encoding="utf-8"))

    for 금지 in (
        "ship_allocated_stock",
        "record_inventory_move",
        "remaining_qty_kg",
        # 🔴 잠금 helper 를 복제하지 않는다.
        "pg_advisory",
        # 🔴 FEFO 정렬을 다시 만들지 않는다.
        "freshness",
        "ORDER BY",
        ".sort(",
        # 🔴 마스터·판매를 임포트하지 않는다.
        "app.master",
        "app.sales",
    ):
        assert 금지 not in 코드, f"자동 FEFO 가 범위를 넘었다: {금지}"

    assert "lock_outbound_writes" in 코드
    assert "recommend_fefo_candidates" in 코드
    assert "allocate_stock" in 코드


def test_S20_예약_상태_읽기는_확보와_배정을_함께_준다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", qty="100", received_at=date(2026, 1, 1))
    _부분예약(conn, required="80")
    _자동할당(conn)

    상태 = reservation_allocation_state(conn, reservation_id=RSV)

    assert 상태.required_qty_kg == Decimal(80)
    assert 상태.reserved_qty_kg == Decimal(80)
    assert 상태.assigned_qty_kg == Decimal(80)
    assert 상태.unassigned_qty_kg == Decimal(0)
    assert 상태.assigned_lot_ids == frozenset({"LOT-A"})
