"""원장 기록을 **실 PostgreSQL 로** 잰다. 값이 실제로 그렇게 되는지가 여기 몫이다.

`test_logistics_ledger.py` 는 가짜 커넥션으로 규율(커밋 금지·순서·멱등)을 재고,
여기서는 **숫자와 DB 제약**을 잰다 — CHECK 를 우회하지 않는지, 실패가 부분 결과를
남기지 않는지는 진짜 DB 가 아니면 못 잰다.

🔴 **공유 DB 의 `haetdeul` 을 건드리지 않는다.**

```text
① 임시 스키마를 만들고 그 안에 저장소 DDL 로 표를 세운다
② ledger.get_db_schema 를 그 임시 스키마로 돌려 놓는다
③ 검사한다
④ ROLLBACK — 임시 스키마도 시험 데이터도 남지 않는다
```

`haetdeul` 은 **읽지도 쓰지도 않는다** — 표 정의를 저장소 SQL 에서 뜨기 때문이다.

⚠️ 기본 실행에서 빠진다 (`addopts = -m 'not llm and not db'`). 돌리려면:
   `uv run pytest -m db tests/logistics/test_logistics_ledger_db.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.logistics import ledger
from app.logistics.db import get_connection
from app.logistics.ledger import (
    MoveIdConflict,
    MoveLine,
    OriginalQuantityExceeded,
    RemainingQuantityInsufficient,
    record_inventory_move,
)

pytestmark = pytest.mark.db

TMP_SCHEMA = "ledger_verify"
SIM_RUN_ID = "SIM-LEDGER-TEST"
ITEM_ID = "ITEM-TEST"
PURCHASE_ITEM_ID = "PI-TEST"
LOT_ID = "LOT-TEST"
MOVED_AT = date(2026, 1, 7)

_DB_DIR = Path(__file__).resolve().parents[3] / "database"

#: FK 대상만 되는 다른 도메인 표 — PK 만 있는 stub 이다.
#: ★ 이번 단계가 Sales/Purchase 스키마를 **안 고친다**는 사실이 여기서도 드러난다.
_STUBS = f"""
CREATE TABLE {TMP_SCHEMA}.items (item_id text PRIMARY KEY, item_name text);
CREATE TABLE {TMP_SCHEMA}.partners (partner_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sim_runs (sim_run_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.purchase_items (purchase_item_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sales (sale_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sale_items (sale_item_id text PRIMARY KEY);
"""


def _repo_block(table: str) -> str:
    """`10_domain_schema.sql` 의 표 하나를 CREATE·제약·주석까지 그대로 뜬다.

    ★ 손으로 다시 적지 않는다 — 다시 적으면 **검사용 표와 실제 표가 갈린다.**
      우리가 재려는 것 중 하나가 바로 DB CHECK 이라 그 순간 검사가 무의미해진다.
    """
    text = (_DB_DIR / "10_domain_schema.sql").read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE haetdeul\.{table}\s*\(.*?\n\);", text, re.DOTALL)
    assert match is not None, f"10_domain_schema.sql 에 {table} 이 없다"
    parts = [match.group(0)]
    parts += re.findall(rf"ALTER TABLE ONLY haetdeul\.{table}\s+ADD CONSTRAINT [^;]+;", text)
    return "\n".join(parts)


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection]:
    """임시 스키마에 원장 표를 세우고, 끝나면 **되돌린다**."""
    connection = get_connection()
    connection.autocommit = False
    try:
        with connection.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {TMP_SCHEMA}")
            cur.execute(_STUBS)
            for table in ("inventory_lots", "inventory_moves"):
                cur.execute(_repo_block(table).replace("haetdeul.", f"{TMP_SCHEMA}."))
            wms = (_DB_DIR / "30_logistics_wms_schema.sql").read_text(encoding="utf-8")
            wms = re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", wms)
            cur.execute(wms.replace("haetdeul.", f"{TMP_SCHEMA}."))

            cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (ITEM_ID, "배추"))
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (SIM_RUN_ID,))
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES (%s)", (PURCHASE_ITEM_ID,))
        monkeypatch.setattr(ledger, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        # 🔴 COMMIT 하지 않는다 — 공유 DB 에 시험 흔적을 남기지 않는다.
        connection.rollback()
        connection.close()


def _lot(
    conn: psycopg.Connection, *, original: str, remaining: str, status: str = "ACTIVE"
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TMP_SCHEMA}.inventory_lots (
                lot_id, sim_run_id, purchase_item_id, item_id, grade, received_at,
                original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                storage_zone, status, derivation_status
            ) VALUES (%s, %s, %s, %s, '상', %s, %s, %s, 1000, 'COLD_HUMID_0_3', %s, 'TEST')
            """,
            (
                LOT_ID,
                SIM_RUN_ID,
                PURCHASE_ITEM_ID,
                ITEM_ID,
                MOVED_AT,
                Decimal(original),
                Decimal(remaining),
                status,
            ),
        )


def _remaining(conn: psycopg.Connection) -> Decimal:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT remaining_qty_kg FROM {TMP_SCHEMA}.inventory_lots WHERE lot_id = %s",
            (LOT_ID,),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0] if not isinstance(row, dict) else row["remaining_qty_kg"]


def _status(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {TMP_SCHEMA}.inventory_lots WHERE lot_id = %s", (LOT_ID,))
        row = cur.fetchone()
        assert row is not None
        return row[0] if not isinstance(row, dict) else row["status"]


def _moves(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT move_id, move_type, quantity_kg FROM {TMP_SCHEMA}.inventory_moves"
            " ORDER BY move_id"
        )
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in cur.fetchall()]


def _기록(conn: psycopg.Connection, **바꿀것: Any):
    인자: dict[str, Any] = {
        "move_id": "MOVE-1",
        "sim_run_id": SIM_RUN_ID,
        "lot_id": LOT_ID,
        "move_type": "OUT",
        "quantity_kg": Decimal(20),
        "moved_at": MOVED_AT,
        "reason_code": "SALE_FULFILLMENT",
    }
    인자.update(바꿀것)
    return record_inventory_move(conn, **인자)


# ── 정상 경로 ────────────────────────────────────────────────────────────


def test_IN_이_잔량을_올리고_원장에_남는다(conn: psycopg.Connection):
    """입고 단계가 쓸 모양이다 — Lot 을 `remaining=0` 으로 세우고 IN 으로 채운다."""
    _lot(conn, original="100", remaining="0")

    result = _기록(conn, move_type="IN", quantity_kg=Decimal(60), reason_code="PURCHASE_RECEIPT")

    assert result.applied is True
    assert result.remaining_qty_kg == Decimal(60)
    assert _remaining(conn) == Decimal(60)
    assert _moves(conn) == [("MOVE-1", "IN", Decimal("60.000000"))]


def test_OUT_이_잔량을_내리고_원장에_남는다(conn: psycopg.Connection):
    _lot(conn, original="100", remaining="60")

    result = _기록(conn, quantity_kg=Decimal(20))

    assert result.remaining_qty_kg == Decimal(40)
    assert _remaining(conn) == Decimal(40)
    assert _moves(conn) == [("MOVE-1", "OUT", Decimal("20.000000"))]


# ── 실패는 아무것도 남기지 않는다 ─────────────────────────────────────────


def test_초과_OUT_은_Move_도_잔량도_남기지_않는다(conn: psycopg.Connection):
    _lot(conn, original="100", remaining="40")

    with pytest.raises(RemainingQuantityInsufficient):
        _기록(conn, quantity_kg=Decimal(41))

    assert _moves(conn) == []
    assert _remaining(conn) == Decimal(40)


def test_최초수량_초과_IN_은_Move_도_잔량도_남기지_않는다(conn: psycopg.Connection):
    _lot(conn, original="100", remaining="60")

    with pytest.raises(OriginalQuantityExceeded):
        _기록(conn, move_type="IN", quantity_kg=Decimal(50))

    assert _moves(conn) == []
    assert _remaining(conn) == Decimal(60)


def test_업무_검증_실패_뒤에는_트랜잭션이_살아_있다(conn: psycopg.Connection):
    """🔴 검사가 쓰기보다 앞이라 얻는 것이다.

    INSERT 뒤에 검사하면 실패한 순간 트랜잭션이 aborted 가 되어, 호출자가
    rollback 하기 전에는 **아무 판단도 못 한다.**

    ⚠️ **업무 검증 실패에 한정된 이야기다.** FK·CHECK 같은 DB 무결성 실패는 DML 중에
       터져 트랜잭션을 aborted 로 만든다 — 그때는 바깥 트랜잭션이 rollback 해야 한다
       (아래 `test_계산이_뚫려도_DB_CHECK_가_막고_부분결과가_안_남는다` 가 그 경우다).
    """
    _lot(conn, original="100", remaining="40")

    with pytest.raises(RemainingQuantityInsufficient):
        _기록(conn, quantity_kg=Decimal(41))

    result = _기록(conn, move_id="MOVE-2", quantity_kg=Decimal(10))

    assert result.remaining_qty_kg == Decimal(30)


# ── DB 제약을 우회하지 않는다 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("다음잔량", "무엇"),
    [(Decimal(-1), "remaining >= 0"), (Decimal(999), "remaining <= original")],
)
def test_계산이_뚫려도_DB_CHECK_가_막고_부분결과가_안_남는다(
    conn: psycopg.Connection, 다음잔량: Decimal, 무엇: str, monkeypatch: pytest.MonkeyPatch
):
    """검사를 강제로 무력화해 **DB 제약이 마지막 방어선**임을 확인한다.

    ★ Move INSERT 는 이미 나갔고 Lot UPDATE 가 DB CHECK 에 걸리는 상황이다 —
      사용자 요구의 *"Move 기록 뒤 Lot Update 가 실패"* 가 이 자리다.
      SAVEPOINT 로 감싸 호출자가 되돌리면 **부분 결과가 남지 않아야 한다.**
    """
    _lot(conn, original="100", remaining="40")
    monkeypatch.setattr(ledger, "_next_remaining", lambda **_: 다음잔량)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _기록(conn, quantity_kg=Decimal(10))

    assert _moves(conn) == [], f"{무엇} 위반이 Move 를 남기면 안 된다"
    assert _remaining(conn) == Decimal(40)


# ── 멱등 ─────────────────────────────────────────────────────────────────


def test_같은_Move_를_두_번_실행해도_잔량은_한_번만_바뀐다(conn: psycopg.Connection):
    _lot(conn, original="100", remaining="60")

    첫번째 = _기록(conn, quantity_kg=Decimal(20))
    두번째 = _기록(conn, quantity_kg=Decimal(20))

    assert 첫번째.applied is True
    assert 두번째.applied is False
    assert _moves(conn) == [("MOVE-1", "OUT", Decimal("20.000000"))]
    assert _remaining(conn) == Decimal(40)


def test_같은_id_에_다른_수량이면_멈추고_아무것도_안_바꾼다(conn: psycopg.Connection):
    _lot(conn, original="100", remaining="60")
    _기록(conn, quantity_kg=Decimal(20))

    with pytest.raises(MoveIdConflict):
        _기록(conn, quantity_kg=Decimal(21))

    assert _moves(conn) == [("MOVE-1", "OUT", Decimal("20.000000"))]
    assert _remaining(conn) == Decimal(40)


# ── Move Line ────────────────────────────────────────────────────────────


def test_Line_없는_Move_는_정합성_뷰에_안_걸린다(conn: psycopg.Connection):
    """Pallet 확정 전 입고가 그 상태다 — Line 0 건은 검출 대상이 아니다."""
    _lot(conn, original="100", remaining="60")

    _기록(conn, quantity_kg=Decimal(20))

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TMP_SCHEMA}.v_move_line_integrity")
        row = cur.fetchone()
    assert (row[0] if not isinstance(row, dict) else row["count"]) == 0


def _창고와_팔레트(conn: psycopg.Connection) -> None:
    """Line 을 걸 수 있는 최소 물리 구조. **Pallet 두 판**을 서로 다른 자리에 세운다.

    ⚠️ 파라미터가 붙는 문장은 한 번에 하나다 — 여러 문장을 한 execute 로 보내면
       psycopg 가 prepared statement 로 못 만든다.
    ★ `pallets` CHECK 이 *"살아 있는 Pallet 은 자리가 있다"* 를 강제하고
      `uq_pallets_location` 이 한 자리에 한 판만 허용하므로 자리도 둘이어야 한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TMP_SCHEMA}.warehouses
                (warehouse_id, warehouse_name, network_type, operation_model,
                 contract_type, geometry_basis, source_ref)
            VALUES ('WH', 'W', 'SINGLE_HUB_MVP', 'LEASED_SELF_OPERATED', 'LEASE',
                    'SIMULATION_GEOMETRY', 'TEST')
            """
        )
        cur.execute(
            f"""
            INSERT INTO {TMP_SCHEMA}.warehouse_zones
                (zone_id, warehouse_id, zone_code, zone_name, zone_kind, purpose,
                 environment_basis, source_ref)
            VALUES ('Z', 'WH', 'Z', 'Z', 'STORAGE_RACK', 'NORMAL_STORAGE',
                    'SIMULATION_ASSUMPTION', 'TEST')
            """
        )
        for location_id, position in (("L1", 1), ("L2", 2)):
            cur.execute(
                f"""
                INSERT INTO {TMP_SCHEMA}.storage_locations
                    (location_id, warehouse_id, zone_id, rack_code, bay_code, level_no,
                     position_no, location_kind)
                VALUES (%s, 'WH', 'Z', 'R', 'B', 1, %s, 'RACK_POSITION')
                """,
                (location_id, position),
            )
        for pallet_id, location_id in (("PLT-1", "L1"), ("PLT-2", "L2")):
            cur.execute(
                f"""
                INSERT INTO {TMP_SCHEMA}.pallets
                    (pallet_id, lot_id, current_location_id, status)
                VALUES (%s, %s, %s, 'ACTIVE')
                """,
                (pallet_id, LOT_ID, location_id),
            )


def _line_facts(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT pallet_id, location_id, quantity_kg, note"
            f" FROM {TMP_SCHEMA}.inventory_move_lines ORDER BY move_line_id"
        )
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in cur.fetchall()]


def test_Pallet_이_있으면_Line_이_남고_정합성_뷰가_비어_있다(conn: psycopg.Connection):
    _lot(conn, original="100", remaining="60")
    _창고와_팔레트(conn)

    result = _기록(
        conn,
        quantity_kg=Decimal(20),
        lines=[MoveLine(quantity_kg=Decimal(20), pallet_id="PLT-1", location_id="L1")],
    )

    assert result.line_count == 1
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TMP_SCHEMA}.inventory_move_lines")
        lines = cur.fetchone()
        cur.execute(f"SELECT count(*) FROM {TMP_SCHEMA}.v_move_line_integrity")
        broken = cur.fetchone()
    assert (lines[0] if not isinstance(lines, dict) else lines["count"]) == 1
    assert (broken[0] if not isinstance(broken, dict) else broken["count"]) == 0


# ── 멱등: Move Line 도 사실이다 ──────────────────────────────────────────


def _두판(conn: psycopg.Connection) -> list[MoveLine]:
    """Header 20kg 를 두 Pallet 으로 나눈 정상 묶음."""
    _lot(conn, original="100", remaining="60")
    _창고와_팔레트(conn)
    return [
        MoveLine(quantity_kg=Decimal(12), pallet_id="PLT-1", location_id="L1", note="첫 판"),
        MoveLine(quantity_kg=Decimal(8), pallet_id="PLT-2", location_id="L2"),
    ]


def test_같은_Line_재시도는_실_DB_에서도_멱등이다(conn: psycopg.Connection):
    """★ DB 왕복으로 `Decimal(12)` 이 `Decimal("12.000000")` 이 되어도 같은 사실이다."""
    lines = _두판(conn)
    _기록(conn, quantity_kg=Decimal(20), lines=lines)

    result = _기록(conn, quantity_kg=Decimal(20), lines=list(reversed(lines)))

    assert result.applied is False
    assert _moves(conn) == [("MOVE-1", "OUT", Decimal("20.000000"))]
    assert _line_facts(conn) == [
        ("PLT-1", "L1", Decimal("12.000000"), "첫 판"),
        ("PLT-2", "L2", Decimal("8.000000"), None),
    ]
    assert _remaining(conn) == Decimal(40)


def test_다른_Pallet_으로_재시도하면_멈추고_아무것도_안_바꾼다(conn: psycopg.Connection):
    lines = _두판(conn)
    _기록(conn, quantity_kg=Decimal(20), lines=lines)

    with pytest.raises(MoveIdConflict):
        _기록(
            conn,
            quantity_kg=Decimal(20),
            lines=[
                MoveLine(
                    quantity_kg=Decimal(12), pallet_id="PLT-2", location_id="L2", note="첫 판"
                ),
                MoveLine(quantity_kg=Decimal(8), pallet_id="PLT-1", location_id="L1"),
            ],
        )

    assert _moves(conn) == [("MOVE-1", "OUT", Decimal("20.000000"))]
    assert _line_facts(conn) == [
        ("PLT-1", "L1", Decimal("12.000000"), "첫 판"),
        ("PLT-2", "L2", Decimal("8.000000"), None),
    ]
    assert _remaining(conn) == Decimal(40)


def test_Line_을_빼고_재시도하면_멈춘다(conn: psycopg.Connection):
    """기존 Line 이 있는데 요청이 0건이면 다른 사실이다."""
    lines = _두판(conn)
    _기록(conn, quantity_kg=Decimal(20), lines=lines)

    with pytest.raises(MoveIdConflict):
        _기록(conn, quantity_kg=Decimal(20))

    assert len(_line_facts(conn)) == 2
    assert _remaining(conn) == Decimal(40)


# ── 멱등 키 충돌의 오류 우선순위 ─────────────────────────────────────────


def test_기존_Move_가_없는_Lot_을_가리키면_Conflict_다(conn: psycopg.Connection):
    """🔴 `LotNotFound` 가 아니다 — 부재가 아니라 **멱등 키 충돌**이다.

    Lot 을 먼저 잠그던 구조에서는 이 요청이 `LotNotFound` 로 나가, 부르는 쪽이
    *"Lot 을 만들어야 하나"* 로 읽었다. 실제로는 그 `move_id` 가 이미 쓰였다는 뜻이다.
    """
    _lot(conn, original="100", remaining="60")
    _기록(conn, quantity_kg=Decimal(20))

    with pytest.raises(MoveIdConflict) as 오류:
        _기록(conn, lot_id="LOT-NOT-EXIST", quantity_kg=Decimal(20))

    assert "lot_id" in str(오류.value)
    assert _moves(conn) == [("MOVE-1", "OUT", Decimal("20.000000"))]
    assert _remaining(conn) == Decimal(40)


def test_기존_Move_가_다른_존재하는_Lot_을_가리켜도_Conflict_다(conn: psycopg.Connection):
    _lot(conn, original="100", remaining="60")
    _기록(conn, quantity_kg=Decimal(20))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TMP_SCHEMA}.inventory_lots (
                lot_id, sim_run_id, purchase_item_id, item_id, grade, received_at,
                original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                storage_zone, status, derivation_status
            ) VALUES ('LOT-B', %s, %s, %s, '상', %s, 100, 100, 1000,
                      'COLD_HUMID_0_3', 'ACTIVE', 'TEST')
            """,
            (SIM_RUN_ID, PURCHASE_ITEM_ID, ITEM_ID, MOVED_AT),
        )

    with pytest.raises(MoveIdConflict):
        _기록(conn, lot_id="LOT-B", quantity_kg=Decimal(20))

    assert _moves(conn) == [("MOVE-1", "OUT", Decimal("20.000000"))]
    assert _remaining(conn) == Decimal(40)


# ── 동시성: 원장 쓰기 advisory lock ──────────────────────────────────────
#
# ⚠️ **여기서 재는 것은 잠금 자체와 자기-교착 없음이다.** 두 트랜잭션이 원장 쓰기를
#    끝까지 도는 실검증은 못 했다 — 임시 스키마가 미커밋이라 두 번째 커넥션이 그 표를
#    볼 수 없고, 격리된 PostgreSQL(Docker)이 이 환경에 없다. **advisory lock 은 DB
#    전역**이라 표와 무관하게 다른 세션에서 관측할 수 있어, 직렬화 장치가 실제로
#    서는지·풀리는지는 잰다.


def _try_ledger_lock(cursor: Any) -> bool:
    cursor.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s)",
        (ledger._LEDGER_LOCK_CLASSID, ledger._LEDGER_LOCK_OBJID),
    )
    row = cursor.fetchone()
    assert row is not None
    return row[0] if not isinstance(row, dict) else next(iter(row.values()))


def test_한_트랜잭션이_여러_Move_를_기록해도_자기를_막지_않는다(conn: psycopg.Connection):
    """🔴 종전 `move_id` 별 잠금이 교착을 냈던 바로 그 모양이다.

    ```text
    T1  record(MOVE-1, LOT-A)   adv(MOVE-1) · LOT-A row lock 획득
    T2  record(MOVE-2, LOT-A)   adv(MOVE-2) 획득 · LOT-A 를 기다린다
    T1  record(MOVE-2, LOT-A)   adv(MOVE-2) 를 기다린다   → 교착
    ```

    ★ 잠금이 하나뿐이고 같은 트랜잭션 안에서 재진입하므로, 한 트랜잭션이 같은 Lot 에
      여러 Move 를 연달아 기록해도 두 번째·세 번째가 스스로 멈추지 않는다.
    """
    _lot(conn, original="100", remaining="60")

    첫번째 = _기록(conn, move_id="MOVE-1", quantity_kg=Decimal(20))
    두번째 = _기록(conn, move_id="MOVE-2", quantity_kg=Decimal(15))
    세번째 = _기록(conn, move_id="MOVE-3", quantity_kg=Decimal(5))

    assert [첫번째.applied, 두번째.applied, 세번째.applied] == [True, True, True]
    assert 세번째.remaining_qty_kg == Decimal(20)
    assert _remaining(conn) == Decimal(20)
    assert _moves(conn) == [
        ("MOVE-1", "OUT", Decimal("20.000000")),
        ("MOVE-2", "OUT", Decimal("15.000000")),
        ("MOVE-3", "OUT", Decimal("5.000000")),
    ]


def test_기록_중에는_다른_세션이_원장_잠금을_못_잡는다(conn: psycopg.Connection):
    """🔴 이것이 서지 않으면 **같은 move_id + 다른 Lot** 경합이 그대로 열려 있다.

    Lot row lock 은 서로 다른 행을 잠그므로 둘을 못 세운다 — 원장 잠금이 그 자리다.
    """
    _lot(conn, original="100", remaining="60")
    _기록(conn, quantity_kg=Decimal(20))  # 잠금을 쥔 채 커밋하지 않는다

    other = get_connection()
    other.autocommit = False
    try:
        with other.cursor() as cur:
            잡혔나 = _try_ledger_lock(cur)
    finally:
        other.rollback()
        other.close()

    assert 잡혔나 is False, "원장 쓰기는 한 줄로 선다"


def test_다른_move_id_도_같은_원장_잠금을_기다린다(conn: psycopg.Connection):
    """★ **의도한 직렬화다.** MVP 는 쓰기 동시성보다 정확성을 택했다.

    종전 판은 `move_id` 가 다르면 통과시켰고, 그것이 교착의 원인이었다.
    """
    _lot(conn, original="100", remaining="60")
    _기록(conn, move_id="MOVE-1", quantity_kg=Decimal(20))

    other = get_connection()
    other.autocommit = False
    try:
        with other.cursor() as cur:
            # 다른 move_id 를 쓰려는 트랜잭션도 같은 잠금 앞에서 선다.
            잡혔나 = _try_ledger_lock(cur)
    finally:
        other.rollback()
        other.close()

    assert 잡혔나 is False, "move_id 가 달라도 통과시키지 않는다 — 그것이 교착의 뿌리였다"


def test_잠금은_트랜잭션이_끝나면_저절로_풀린다(conn: psycopg.Connection):
    """★ `_xact_` 판이라 커밋/롤백과 함께 풀린다 — 이 모듈은 unlock 을 부르지 않는다.

    ⚠️ 여기서 `conn` 을 직접 롤백한다(임시 스키마도 함께 사라진다). 뒤에서 그 표를
       건드리지 않으므로 안전하고, fixture 의 롤백은 무해하게 한 번 더 돈다.
    """
    _lot(conn, original="100", remaining="60")
    _기록(conn, quantity_kg=Decimal(20))

    other = get_connection()
    other.autocommit = False
    try:
        with other.cursor() as cur:
            잠긴동안 = _try_ledger_lock(cur)
        other.rollback()  # 관측용 트랜잭션을 닫아 이쪽 잠금도 남기지 않는다

        conn.rollback()  # ← 원장 트랜잭션 종료. 여기서 advisory lock 이 풀려야 한다

        with other.cursor() as cur:
            풀린뒤 = _try_ledger_lock(cur)
    finally:
        other.rollback()
        other.close()

    assert 잠긴동안 is False
    assert 풀린뒤 is True, "트랜잭션이 끝났는데 잠금이 남아 있으면 커넥션에 눌어붙는다"


# ── 상태 ─────────────────────────────────────────────────────────────────


def test_잔량이_0_이_되어도_상태를_바꾸지_않는다(conn: psycopg.Connection):
    """🔴 `DEPLETED` 로 바꾸는 것은 **출고 단계 판단**이라 여기서 정하지 않는다.

    ★ 그래도 기존 계약은 안 깨진다 — `repository` 가 `remaining_qty_kg > 0` 으로
      거르므로 잔량 0 Lot 은 status 와 무관하게 Snapshot 에서 빠진다.
    """
    _lot(conn, original="100", remaining="20", status="ACTIVE")

    _기록(conn, quantity_kg=Decimal(20))

    assert _remaining(conn) == Decimal(0)
    assert _status(conn) == "ACTIVE"
