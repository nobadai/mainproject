"""입고 파이프라인 **끝까지** 실제 PostgreSQL 에서 돌린다 (3-B4-I).

```text
Receipt ARRIVED → Inspection → Lot → Ledger IN → Receipt PUTAWAY_DONE → 일정 정리
```

한 트랜잭션 안에서 전부 돌고, 끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에는
아무것도 남지 않는다.

🔴 **가짜 커서로는 못 재는 것들을 여기서 잰다.**

```text
Lot NOT NULL 열 칸 · CHECK 넷 · FK 넷
잔량이 원장으로만 움직이는가          Lot 은 0 으로 서고 IN 이 accepted 로 올린다
잠금 셋이 한 트랜잭션에서 안 엉키나    도착 전역 → fixture 행 → 원장 전역 → Lot 행
JSONB 일정에서 그 건만 빠지는가
```

★ **이관판을 임시 스키마에 적용해서 돈다.** `database/logistics_inventory_lots_nullable.sql`
  이 그것이고, 공유 DB 에는 **적용하지 않는다** (통합 실행 전 별도 적용 필요).
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from app.logistics import inbound_stock, inspections, receipts
from app.logistics.arrival import DueInbound
from app.logistics.db import get_connection
from app.logistics.inbound_stock import (
    LotConflict,
    LotIntegrityError,
    ScheduleIntegrityError,
    lot_id_for,
    materialize_inspected_inbound,
    move_id_for,
)
from app.logistics.inspections import InspectionOutcome, record_inspection
from app.logistics.purchase_detail import PurchaseDetail
from app.logistics.receipts import create_arrived_receipt
from app.logistics.schemas import InTransitItem

pytestmark = pytest.mark.db

TMP_SCHEMA = "inbound_stock_verify"
SIM_RUN_ID = "SIM-INBOUND-TEST"
ITEM_ID = "ITEM-BAECHU"
PURCHASE_ITEM_ID = "PI-TEST"
INBOUND_ID = "INB-H1-THRU-20260105-BAECHU-1-1"
ETA = date(2026, 1, 7)
AS_OF = date(2026, 1, 7)
USAGE_SCOPE = "AGENT_MVP_DEMO"
INSPECTED_AT = datetime(2026, 1, 7, 9, 30, tzinfo=UTC)
INSPECTOR = "WH-INSPECTOR-01"
QTY = Decimal("3587.000000")
UNIT_COST = Decimal("854.000000")
#: 🟢 `item_storage_policies` 가 이 칸의 주인이다 (실측: 기존 80 Lot 이 품목마다 이 값).
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
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"DISPOSE 로 바꾸지 않는다"* 고 **설명하는 문장**이 위반으로
       잡힌다. 설명과 실행문은 다른 것이다.
    """
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                코드 = 코드.replace(doc, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _repo_block(table: str) -> str:
    """`10_domain_schema.sql` 의 표 하나를 그대로 뜬다 — 손으로 다시 적지 않는다."""
    text = (_DB_DIR / "10_domain_schema.sql").read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE haetdeul\.{table}\s*\(.*?\n\);", text, re.DOTALL)
    assert match is not None, f"10_domain_schema.sql 에 {table} 이 없다"
    parts = [match.group(0)]
    parts += re.findall(rf"ALTER TABLE ONLY haetdeul\.{table}\s+ADD CONSTRAINT [^;]+;", text)
    return "\n".join(parts)


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection]:
    """임시 스키마에 입고 파이프라인 표를 전부 세우고, 끝나면 **되돌린다**."""
    connection = get_connection()
    connection.autocommit = False
    try:
        with connection.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {TMP_SCHEMA}")
            cur.execute(_STUBS)
            for table in (
                "inventory_lots",
                "inventory_moves",
                "item_storage_policies",
                "logistics_runtime_fixture",
            ):
                cur.execute(_repo_block(table).replace("haetdeul.", f"{TMP_SCHEMA}."))
            wms = (_DB_DIR / "30_logistics_wms_schema.sql").read_text(encoding="utf-8")
            wms = re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", wms)
            cur.execute(wms.replace("haetdeul.", f"{TMP_SCHEMA}."))
            # ★ 이관판을 여기서만 적용한다. 공유 DB 에는 적용하지 않는다.
            nullable = (_DB_DIR / "logistics_inventory_lots_nullable.sql").read_text(
                encoding="utf-8"
            )
            nullable = re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", nullable)
            cur.execute(nullable.replace("haetdeul.", f"{TMP_SCHEMA}."))

            cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (ITEM_ID, "배추"))
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (SIM_RUN_ID,))
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES (%s)", (PURCHASE_ITEM_ID,))
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
                " (item_id, storage_zone, operational_policy_status) VALUES (%s, %s, %s)",
                (ITEM_ID, ZONE, "PROVISIONAL"),
            )
        for module in (receipts, inspections, inbound_stock):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        from app.logistics import ledger

        monkeypatch.setattr(ledger, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        # 🔴 COMMIT 하지 않는다 — 공유 DB 에 시험 흔적을 남기지 않는다.
        connection.rollback()
        connection.close()


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _due() -> DueInbound:
    item = InTransitItem(
        inbound_id=INBOUND_ID,
        purchase_id="PUR-THRU-20260105-BAECHU-D1-S1",
        item="배추",
        quantity_kg=QTY,
        expected_arrival_date=ETA,
    )
    return DueInbound(
        item=item,
        inbound_id=INBOUND_ID,
        purchase_id="PUR-THRU-20260105-BAECHU-D1-S1",
        expected_arrival_date=ETA,
        overdue=False,
    )


def _detail(*, grade: str | None = None) -> PurchaseDetail:
    return PurchaseDetail(
        purchase_item_id=PURCHASE_ITEM_ID,
        item_id=ITEM_ID,
        grade=grade,
        quantity_kg=QTY,
        unit_price_krw_per_kg=UNIT_COST,
    )


def _일정(conn: psycopg.Connection) -> None:
    """그날 fixture 행을 세운다 — 이번 입고 한 건이 두 칸에 짝으로 들어 있다."""
    운송 = [
        {
            "inbound_id": INBOUND_ID,
            "item": "배추",
            "quantity_kg": str(QTY),
            "expected_arrival_date": ETA.isoformat(),
        }
    ]
    확정 = [
        {
            "inbound_id": INBOUND_ID,
            "item": "배추",
            "quantity_kg": str(QTY),
            "date": ETA.isoformat(),
        }
    ]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TMP_SCHEMA}.logistics_runtime_fixture (
                fixture_id, sim_run_id, as_of, in_transit_status, in_transit_json,
                confirmed_inbound_status, confirmed_inbound_json,
                confirmed_outbound_status, confirmed_outbound_json,
                usage_scope, evidence_grade, source_ref, approved_by, is_active
            ) VALUES (%s, %s, %s, 'CONFIRMED', %s, 'CONFIRMED', %s,
                      'CONFIRMED_ZERO', '[]'::jsonb, %s, 'SIM_FIXED', 'TEST', 'HUMAN', TRUE)
            """,
            (
                "FIX-TEST-1",
                SIM_RUN_ID,
                AS_OF,
                json.dumps(운송),
                json.dumps(확정),
                USAGE_SCOPE,
            ),
        )


def _영수와_검수(
    conn: psycopg.Connection,
    *,
    verdict: str = "PASS",
    accepted: str = "3587.000000",
    hold: str = "0",
    reject: str = "0",
    grade: str | None = None,
) -> str:
    receipt_id = create_arrived_receipt(
        conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail(grade=grade)
    ).receipt_id
    record_inspection(
        conn,
        receipt_id=receipt_id,
        inspected_at=INSPECTED_AT,
        inspector=INSPECTOR,
        outcome=InspectionOutcome(
            verdict=verdict,  # type: ignore[arg-type]
            inspected_qty_kg=QTY,
            accepted_qty_kg=Decimal(accepted),
            hold_qty_kg=Decimal(hold),
            reject_qty_kg=Decimal(reject),
        ),
    )
    return receipt_id


def _lots(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {TMP_SCHEMA}.inventory_lots ORDER BY lot_id")
        이름 = [d.name for d in cur.description]
        return [
            r if isinstance(r, dict) else dict(zip(이름, r, strict=True)) for r in cur.fetchall()
        ]


def _moves(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {TMP_SCHEMA}.inventory_moves ORDER BY move_id")
        이름 = [d.name for d in cur.description]
        return [
            r if isinstance(r, dict) else dict(zip(이름, r, strict=True)) for r in cur.fetchall()
        ]


def _fixture(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT in_transit_json, in_transit_status, confirmed_inbound_json,"
            f" confirmed_inbound_status FROM {TMP_SCHEMA}.logistics_runtime_fixture"
        )
        이름 = [d.name for d in cur.description]
        row = cur.fetchone()
    return row if isinstance(row, dict) else dict(zip(이름, row, strict=True))


def _영수상태(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute(f"SELECT receipt_status FROM {TMP_SCHEMA}.inbound_receipts")
        row = cur.fetchone()
    return row[0] if not isinstance(row, dict) else row["receipt_status"]


def _돌린다(conn: psycopg.Connection, receipt_id: str, *, grade: str | None = None):
    return materialize_inspected_inbound(
        conn,
        as_of=AS_OF,
        receipt_id=receipt_id,
        purchase_detail=_detail(grade=grade),
        usage_scope=USAGE_SCOPE,
    )


# ── 1~9. 재고화 ─────────────────────────────────────────────────────────


def test_1_PASS_전량수용이면_Lot_하나가_선다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    결과 = _돌린다(conn, receipt_id)

    assert 결과.applied is True
    lots = _lots(conn)
    assert len(lots) == 1
    lot = lots[0]
    assert lot["lot_id"] == lot_id_for(receipt_id=receipt_id)
    assert lot["status"] == "ACTIVE"
    assert lot["inspection_status"] == "PASS"
    assert lot["original_qty_kg"] == QTY
    assert lot["remaining_qty_kg"] == QTY, "원장이 잔량을 올렸어야 한다"
    assert lot["storage_zone"] == ZONE
    assert lot["inbound_receipt_id"] == receipt_id
    assert lot["derivation_status"] is None, "정상 입고는 Burn-in 파생이 아니다"


def test_2_HOLD_면_수용분만_Lot_이_된다(conn: psycopg.Connection) -> None:
    """🔴 보류 물량은 Lot 이 되지 않고 폐기되지도 않는다 — 검수·Receipt 에만 남는다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn, verdict="HOLD", accepted="3000", hold="587", reject="0")

    _돌린다(conn, receipt_id)

    lots = _lots(conn)
    assert len(lots) == 1
    assert lots[0]["original_qty_kg"] == Decimal(3000)
    assert lots[0]["remaining_qty_kg"] == Decimal(3000)
    assert len(_moves(conn)) == 1
    assert _moves(conn)[0]["quantity_kg"] == Decimal(3000)


def test_3_REJECT_면_Lot_도_Move_도_없다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn, verdict="REJECT", accepted="0", hold="0", reject=str(QTY))

    결과 = _돌린다(conn, receipt_id)

    assert _lots(conn) == []
    assert _moves(conn) == []
    assert 결과.lot_id is None and 결과.move_id is None
    assert 결과.accepted_qty_kg == 0


def test_4_5_원장_IN_이_잔량을_올리고_사유가_PURCHASE_RECEIPT_다(conn: psycopg.Connection) -> None:
    """🔴 Lot 은 `remaining=0` 으로 서고 **원장만이** 잔량을 바꾼다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    결과 = _돌린다(conn, receipt_id)

    moves = _moves(conn)
    assert len(moves) == 1
    assert moves[0]["move_id"] == 결과.move_id == move_id_for(lot_id=결과.lot_id)
    assert moves[0]["move_type"] == "IN"
    assert moves[0]["reason_code"] == "PURCHASE_RECEIPT"
    assert moves[0]["quantity_kg"] == QTY
    assert moves[0]["moved_at"] == ETA, "도착일이 이동일이다"
    assert moves[0]["sale_item_id"] is None
    assert _lots(conn)[0]["remaining_qty_kg"] == QTY


def test_6_7_id_가_결정론이다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    결과 = _돌린다(conn, receipt_id)

    assert 결과.lot_id == f"LOT-{receipt_id}"
    assert 결과.move_id == f"MOVE-IN-LOT-{receipt_id}"


def test_8_grade_가_None_이면_None_그대로다(conn: psycopg.Connection) -> None:
    """★ 이관판이 적용돼야 통과한다 — 그것을 여기서 실제로 확인한다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    _돌린다(conn, receipt_id)

    assert _lots(conn)[0]["grade"] is None


def test_8b_grade_에_값이_있으면_그_값_그대로다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn, grade="상품")

    _돌린다(conn, receipt_id, grade="상품")

    assert _lots(conn)[0]["grade"] == "상품"


def test_9_매입_단가가_Lot_원가가_된다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    _돌린다(conn, receipt_id)

    assert _lots(conn)[0]["unit_cost_krw_per_kg"] == UNIT_COST


def test_9b_보관_Zone_은_품목_정책에서_온다(conn: psycopg.Connection) -> None:
    """🟢 `item_storage_policies` 가 주인이다 — 품목명 하드코딩이 아니다."""
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.item_storage_policies SET storage_zone = %s WHERE item_id = %s",
            ("COLD_DRY_0", ITEM_ID),
        )
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    _돌린다(conn, receipt_id)

    assert _lots(conn)[0]["storage_zone"] == "COLD_DRY_0", "정책표를 따라가야 한다"


def test_9c_보관_정책이_없으면_멈춘다(conn: psycopg.Connection) -> None:
    """🔴 기본 Zone 을 고르지 않는다 — 그 추측이 로트의 보관 조건이 된다."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.item_storage_policies")
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    with pytest.raises(LotIntegrityError, match="보관 정책"):
        _돌린다(conn, receipt_id)


# ── 10~12. 재실행 ───────────────────────────────────────────────────────


def test_10_12_같은_입고를_다시_돌려도_Lot_도_Move_도_하나다(conn: psycopg.Connection) -> None:
    """🔴 **전체 멱등이다.** 일정은 이미 걷혔고 Receipt 는 역행하지 않는다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    첫번 = _돌린다(conn, receipt_id)
    두번 = _돌린다(conn, receipt_id)

    assert 첫번.applied is True
    assert 두번.applied is False
    assert len(_lots(conn)) == 1
    assert len(_moves(conn)) == 1
    assert 두번.receipt_status == "PUTAWAY_DONE"
    assert 두번.schedule_cleared is False, "이미 걷혔다"
    assert _lots(conn)[0]["remaining_qty_kg"] == QTY, "잔량이 두 번 늘지 않는다"


def test_11_사실이_다른_기존_Lot_이면_충돌이다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.inventory_lots SET unit_cost_krw_per_kg = 999"
            " WHERE inbound_receipt_id = %s",
            (receipt_id,),
        )
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.inbound_receipts SET receipt_status = 'INSPECTED'"
            " WHERE receipt_id = %s",
            (receipt_id,),
        )

    with pytest.raises(LotConflict):
        _돌린다(conn, receipt_id)


def test_20_accepted_0_도_정상_완료된다(conn: psycopg.Connection) -> None:
    """★ REJECT 재실행도 멱등해야 한다 — 0kg Move 를 만들지 않는다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn, verdict="REJECT", accepted="0", hold="0", reject=str(QTY))

    첫번 = _돌린다(conn, receipt_id)
    두번 = _돌린다(conn, receipt_id)

    assert 첫번.receipt_status == "PUTAWAY_DONE"
    assert 첫번.schedule_cleared is True
    assert 두번.applied is False
    assert _moves(conn) == [], "0kg Move 를 만들지 않는다"
    assert _fixture(conn)["in_transit_status"] == "CONFIRMED_ZERO"


# ── 13~17. 일정 정리 ────────────────────────────────────────────────────


def test_13_16_두_칸에서_함께_빠지고_비면_CONFIRMED_ZERO_다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    결과 = _돌린다(conn, receipt_id)

    fx = _fixture(conn)
    assert 결과.schedule_cleared is True
    assert fx["in_transit_json"] == []
    assert fx["confirmed_inbound_json"] == []
    assert fx["in_transit_status"] == "CONFIRMED_ZERO"
    assert fx["confirmed_inbound_status"] == "CONFIRMED_ZERO"


def test_17_다른_일정이_남으면_CONFIRMED_를_지킨다(conn: psycopg.Connection) -> None:
    남의것_운송 = {
        "inbound_id": "INB-OTHER-9",
        "item": "무",
        "quantity_kg": "120.5",
        "expected_arrival_date": "2026-01-09",
    }
    남의것_확정 = {
        "inbound_id": "INB-OTHER-9",
        "item": "무",
        "quantity_kg": "120.5",
        "date": "2026-01-09",
    }
    운송 = [
        {
            "inbound_id": INBOUND_ID,
            "item": "배추",
            "quantity_kg": str(QTY),
            "expected_arrival_date": ETA.isoformat(),
        },
        남의것_운송,
    ]
    확정 = [
        {
            "inbound_id": INBOUND_ID,
            "item": "배추",
            "quantity_kg": str(QTY),
            "date": ETA.isoformat(),
        },
        남의것_확정,
    ]
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.logistics_runtime_fixture (
                fixture_id, sim_run_id, as_of, in_transit_status, in_transit_json,
                confirmed_inbound_status, confirmed_inbound_json,
                confirmed_outbound_status, confirmed_outbound_json,
                usage_scope, evidence_grade, source_ref, approved_by, is_active
            ) VALUES (%s, %s, %s, 'CONFIRMED', %s, 'CONFIRMED', %s,
                      'CONFIRMED_ZERO', '[]'::jsonb, %s, 'SIM_FIXED', 'TEST', 'HUMAN', TRUE)""",
            ("FIX-TEST-1", SIM_RUN_ID, AS_OF, json.dumps(운송), json.dumps(확정), USAGE_SCOPE),
        )
    receipt_id = _영수와_검수(conn)

    _돌린다(conn, receipt_id)

    fx = _fixture(conn)
    assert fx["in_transit_json"] == [남의것_운송], "남의 일정은 그대로 있어야 한다"
    assert fx["confirmed_inbound_json"] == [남의것_확정]
    assert fx["in_transit_status"] == "CONFIRMED"
    assert fx["confirmed_inbound_status"] == "CONFIRMED"


def test_14_한쪽_일정만_있으면_무결성_오류다(conn: psycopg.Connection) -> None:
    """🔴 한쪽만 지우면 그 불일치를 덮는 것이 된다."""
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.logistics_runtime_fixture (
                fixture_id, sim_run_id, as_of, in_transit_status, in_transit_json,
                confirmed_inbound_status, confirmed_inbound_json,
                confirmed_outbound_status, confirmed_outbound_json,
                usage_scope, evidence_grade, source_ref, approved_by, is_active
            ) VALUES (%s, %s, %s, 'CONFIRMED', %s, 'CONFIRMED_ZERO', '[]'::jsonb,
                      'CONFIRMED_ZERO', '[]'::jsonb, %s, 'SIM_FIXED', 'TEST', 'HUMAN', TRUE)""",
            (
                "FIX-TEST-1",
                SIM_RUN_ID,
                AS_OF,
                json.dumps(
                    [
                        {
                            "inbound_id": INBOUND_ID,
                            "item": "배추",
                            "quantity_kg": str(QTY),
                            "expected_arrival_date": ETA.isoformat(),
                        }
                    ]
                ),
                USAGE_SCOPE,
            ),
        )
    receipt_id = _영수와_검수(conn)

    with pytest.raises(ScheduleIntegrityError, match="짝을 이루지"):
        _돌린다(conn, receipt_id)


def test_15_B1_불일치면_무결성_오류다(conn: psycopg.Connection) -> None:
    """🔴 어긋난 상태를 조용히 지우면 어긋나 있었다는 사실조차 안 남는다."""
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.logistics_runtime_fixture (
                fixture_id, sim_run_id, as_of, in_transit_status, in_transit_json,
                confirmed_inbound_status, confirmed_inbound_json,
                confirmed_outbound_status, confirmed_outbound_json,
                usage_scope, evidence_grade, source_ref, approved_by, is_active
            ) VALUES (%s, %s, %s, 'CONFIRMED', %s, 'CONFIRMED', %s,
                      'CONFIRMED_ZERO', '[]'::jsonb, %s, 'SIM_FIXED', 'TEST', 'HUMAN', TRUE)""",
            (
                "FIX-TEST-1",
                SIM_RUN_ID,
                AS_OF,
                json.dumps(
                    [
                        {
                            "inbound_id": INBOUND_ID,
                            "item": "배추",
                            "quantity_kg": str(QTY),
                            "expected_arrival_date": ETA.isoformat(),
                        }
                    ]
                ),
                json.dumps(
                    [
                        {
                            "inbound_id": INBOUND_ID,
                            "item": "배추",
                            "quantity_kg": "999",  # 🔴 수량이 다르다
                            "date": ETA.isoformat(),
                        }
                    ]
                ),
                USAGE_SCOPE,
            ),
        )
    receipt_id = _영수와_검수(conn)

    with pytest.raises(ScheduleIntegrityError, match="사실이 다르다"):
        _돌린다(conn, receipt_id)


# ── 18~21. Receipt 상태와 범위 ──────────────────────────────────────────


def test_18_19_PUTAWAY_DONE_까지만_간다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    결과 = _돌린다(conn, receipt_id)

    assert 결과.receipt_status == "PUTAWAY_DONE"
    assert _영수상태(conn) == "PUTAWAY_DONE"
    assert _영수상태(conn) != "CLOSED"


def test_검수_전_Receipt_는_대상이_아니다(conn: psycopg.Connection) -> None:
    """⚠️ 검수 사실 없이 재고를 만들면 수용량을 우리가 정하는 셈이 된다."""
    _일정(conn)
    receipt_id = create_arrived_receipt(
        conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail()
    ).receipt_id

    with pytest.raises(LotIntegrityError, match="재고화할 수 없는"):
        _돌린다(conn, receipt_id)

    assert _lots(conn) == []


def test_22_적치_칸과_Pallet_은_그대로_비어_있다(conn: psycopg.Connection) -> None:
    """★ 원장이 `lines=()` 를 허용한다 — Pallet 확정 전에도 IN 이 선다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    _돌린다(conn, receipt_id)

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TMP_SCHEMA}.pallets")
        pallets = cur.fetchone()
        cur.execute(f"SELECT count(*) FROM {TMP_SCHEMA}.inventory_move_lines")
        lines = cur.fetchone()
    assert (pallets[0] if not isinstance(pallets, dict) else pallets["count"]) == 0
    assert (lines[0] if not isinstance(lines, dict) else lines["count"]) == 0

    lot = _lots(conn)[0]
    assert lot["packaging_spec_id"] is None
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT receiving_location_id, estimated_pallet_count, actual_pallet_count"
            f" FROM {TMP_SCHEMA}.inbound_receipts"
        )
        행 = cur.fetchone()
    assert list(행.values() if isinstance(행, dict) else 행) == [None, None, None]


# ── 23~28. 경계 ─────────────────────────────────────────────────────────


def test_23_25_커밋도_롤백도_새_커넥션도_없다(conn: psycopg.Connection) -> None:
    """★ 한 트랜잭션 안에서 전부 돌았다는 것 자체가 증거다 — 중간에 커밋했다면
    이 fixture 의 최종 `rollback()` 이 되돌리지 못한다.
    """
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    _돌린다(conn, receipt_id)

    assert conn.info.transaction_status.name in {"INTRANS", "INERROR"}


def test_전체_흐름이_한_트랜잭션에서_되돌려진다(conn: psycopg.Connection) -> None:
    """🔴 **롤백 검증.** 공유 DB 규율이 실제로 성립하는지 본다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    assert len(_lots(conn)) == 1

    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", [f"{TMP_SCHEMA}.inventory_lots"])
        남았나 = cur.fetchone()
    assert (남았나[0] if not isinstance(남았나, dict) else 남았나["to_regclass"]) is None


def test_26_28_재고실사도_ADJUST_도_DISPOSE_도_없다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn, verdict="HOLD", accepted="3000", hold="587", reject="0")

    _돌린다(conn, receipt_id)

    종류 = {m["move_type"] for m in _moves(conn)}
    assert 종류 == {"IN"}, "보류 물량을 폐기나 조정으로 바꾸지 않는다"
    코드 = _코드만(Path(inbound_stock.__file__).read_text(encoding="utf-8"))
    for 금지 in ("inventory_count", "ADJUST", "DISPOSE"):
        assert 금지 not in 코드, f"{금지} — 이 단계 범위가 아니다"


# ══════════════════════════════════════════════════════════════════════════
# 정체성 · 완료상태 방어 (3-B4-I 보강)
# ══════════════════════════════════════════════════════════════════════════


def _완료로_돌린다(conn: psycopg.Connection, receipt_id: str, 상태: str = "PUTAWAY_DONE") -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.inbound_receipts SET receipt_status = %s WHERE receipt_id = %s",
            (상태, receipt_id),
        )


# ── 1~2. Receipt ↔ PurchaseDetail 정체성 ──────────────────────────────


def test_R1_purchase_item_id_가_다르면_DML_전에_멈춘다(conn: psycopg.Connection) -> None:
    """🔴 두 출처를 섞으면 **다른 매입 줄의 단가·등급**으로 Lot 이 선다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    다른상세 = PurchaseDetail(
        purchase_item_id="PI-OTHER",
        item_id=ITEM_ID,
        grade="상품",
        quantity_kg=QTY,
        unit_price_krw_per_kg=Decimal("99999.000000"),
    )

    with pytest.raises(LotIntegrityError, match="purchase_item_id"):
        materialize_inspected_inbound(
            conn,
            as_of=AS_OF,
            receipt_id=receipt_id,
            purchase_detail=다른상세,
            usage_scope=USAGE_SCOPE,
        )

    assert _lots(conn) == [], "잘못된 상세로 Lot 이 서면 안 된다"
    assert _moves(conn) == []
    assert _영수상태(conn) == "INSPECTED", "상태도 안 움직인다"


def test_R2_item_id_가_다르면_DML_전에_멈춘다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    다른상세 = PurchaseDetail(
        purchase_item_id=PURCHASE_ITEM_ID,
        item_id="ITEM-MU",
        grade="상품",
        quantity_kg=QTY,
        unit_price_krw_per_kg=Decimal("99999.000000"),
    )

    with pytest.raises(LotIntegrityError, match="item_id"):
        materialize_inspected_inbound(
            conn,
            as_of=AS_OF,
            receipt_id=receipt_id,
            purchase_detail=다른상세,
            usage_scope=USAGE_SCOPE,
        )

    assert _lots(conn) == []
    assert _moves(conn) == []


def test_R2b_잘못된_상세의_단가와_등급이_새어들지_않는다(conn: psycopg.Connection) -> None:
    """★ 위 두 검사의 핵심 — 막지 않으면 그 값이 **재고 원가**가 된다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    with pytest.raises(LotIntegrityError):
        materialize_inspected_inbound(
            conn,
            as_of=AS_OF,
            receipt_id=receipt_id,
            purchase_detail=PurchaseDetail(
                purchase_item_id="PI-OTHER",
                item_id=ITEM_ID,
                grade="특",
                quantity_kg=QTY,
                unit_price_krw_per_kg=Decimal("1.000000"),
            ),
            usage_scope=USAGE_SCOPE,
        )

    assert not any(lot["unit_cost_krw_per_kg"] == Decimal(1) for lot in _lots(conn))
    assert not any(lot["grade"] == "특" for lot in _lots(conn))


def test_R2c_같은_상세면_통과한다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    결과 = _돌린다(conn, receipt_id)

    assert 결과.applied is True


# ── 3. accepted=0 인데 Lot 이 있으면 모순 ──────────────────────────────


def test_R3_수용_0_인데_Lot_이_있으면_오류다(conn: psycopg.Connection) -> None:
    """🔴 검수가 하나도 안 받았다는데 재고가 선 것이라 모순이다.

    ★ 기존 Lot 조회를 `accepted > 0` 분기 안에 두면 **이 모순을 못 잡는다.**
    """
    _일정(conn)
    receipt_id = _영수와_검수(conn, verdict="REJECT", accepted="0", hold="0", reject=str(QTY))
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status, inbound_receipt_id
                ) VALUES (%s, %s, %s, %s, %s, 10, 10, %s, %s, 'ACTIVE', %s)""",
            (
                lot_id_for(receipt_id=receipt_id),
                SIM_RUN_ID,
                PURCHASE_ITEM_ID,
                ITEM_ID,
                ETA,
                UNIT_COST,
                ZONE,
                receipt_id,
            ),
        )

    with pytest.raises(LotIntegrityError, match="수용 수량이 0"):
        _돌린다(conn, receipt_id)

    assert _moves(conn) == [], "0kg Move 를 만들지 않는다"


def test_R3b_수용_0_에_Lot_이_없으면_정상이다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn, verdict="REJECT", accepted="0", hold="0", reject=str(QTY))

    결과 = _돌린다(conn, receipt_id)

    assert 결과.receipt_status == "PUTAWAY_DONE"
    assert _lots(conn) == [] and _moves(conn) == []


# ── 4~7. 완료 상태에서는 복구하지 않는다 ──────────────────────────────


@pytest.mark.parametrize("상태", ["PUTAWAY_DONE", "CLOSED"])
def test_R4_완료상태인데_Lot_이_없으면_오류다(conn: psycopg.Connection, 상태: str) -> None:
    """🔴 없는 재고를 새로 만들어 조용히 복구하지 않는다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _완료로_돌린다(conn, receipt_id, 상태)

    with pytest.raises(LotIntegrityError, match="Lot 이 없다"):
        _돌린다(conn, receipt_id)

    assert _lots(conn) == []
    assert _moves(conn) == []


@pytest.mark.parametrize("상태", ["PUTAWAY_DONE", "CLOSED"])
def test_R5_완료상태인데_Move_가_없으면_오류다(conn: psycopg.Connection, 상태: str) -> None:
    """🔴 `record_inventory_move` 를 무조건 부르면 **없는 Move 를 새로 만든다** —
    완료 상태에서 그것은 조용한 복구다. 읽어서 확인만 한다.
    """
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.inventory_moves")
    _완료로_돌린다(conn, receipt_id, 상태)

    with pytest.raises(LotIntegrityError, match="입고 Move 가 없다"):
        _돌린다(conn, receipt_id)

    assert _moves(conn) == [], "새로 만들지 않는다"


@pytest.mark.parametrize("상태", ["PUTAWAY_DONE", "CLOSED"])
def test_R6_R7_완료상태에_Lot_과_Move_가_다_있으면_멱등이다(
    conn: psycopg.Connection, 상태: str
) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    _완료로_돌린다(conn, receipt_id, 상태)

    결과 = _돌린다(conn, receipt_id)

    assert 결과.applied is False
    assert 결과.receipt_status == 상태, "CLOSED 를 PUTAWAY_DONE 으로 되돌리지 않는다"
    assert len(_lots(conn)) == 1
    assert len(_moves(conn)) == 1
    assert _lots(conn)[0]["remaining_qty_kg"] == QTY


def test_R7b_완료상태의_Move_사실이_다르면_오류다(conn: psycopg.Connection) -> None:
    """★ 존재만 보지 않고 수량·사유·이동일까지 대조한다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TMP_SCHEMA}.inventory_moves SET reason_code = 'SOMETHING_ELSE'")

    with pytest.raises(LotIntegrityError, match="다르다"):
        _돌린다(conn, receipt_id)


@pytest.mark.parametrize("상태", ["PUTAWAY_DONE", "CLOSED"])
def test_R7c_완료상태_수용0은_Lot_도_Move_도_없는_것이_정상이다(
    conn: psycopg.Connection, 상태: str
) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn, verdict="REJECT", accepted="0", hold="0", reject=str(QTY))
    _돌린다(conn, receipt_id)
    _완료로_돌린다(conn, receipt_id, 상태)

    결과 = _돌린다(conn, receipt_id)

    assert 결과.applied is False
    assert 결과.receipt_status == 상태
    assert _lots(conn) == [] and _moves(conn) == []


# ── 8. INSPECTED 는 만들 수 있다 ──────────────────────────────────────


def test_R8_INSPECTED_는_Lot_과_Move_를_새로_만든다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    assert _영수상태(conn) == "INSPECTED"

    결과 = _돌린다(conn, receipt_id)

    assert 결과.applied is True
    assert len(_lots(conn)) == 1 and len(_moves(conn)) == 1


def test_R8b_INSPECTED_에_Lot_만_있으면_Move_를_이어_만든다(conn: psycopg.Connection) -> None:
    """★ 반쪽 트랜잭션의 정상 resume — 아직 완료를 주장하지 않는 상태다."""
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.inventory_moves")
        cur.execute(f"UPDATE {TMP_SCHEMA}.inventory_lots SET remaining_qty_kg = 0")
    _완료로_돌린다(conn, receipt_id, "INSPECTED")

    결과 = _돌린다(conn, receipt_id)

    assert 결과.applied is True
    assert len(_lots(conn)) == 1, "Lot 을 또 만들지 않는다"
    assert len(_moves(conn)) == 1
    assert _lots(conn)[0]["remaining_qty_kg"] == QTY


# ── 9~10. 보관 정책과 재실행 ──────────────────────────────────────────


def test_R9_기존_Lot_재실행은_정책이_지워져도_성공한다(conn: psycopg.Connection) -> None:
    """🔴 이미 선 Lot 의 Zone 은 **확정된 역사적 사실**이다 — 정책이 나중에 바뀌거나
    지워졌다고 과거 입고 재실행이 실패하면 안 된다.
    """
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.item_storage_policies")

    결과 = _돌린다(conn, receipt_id)

    assert 결과.applied is False
    assert _lots(conn)[0]["storage_zone"] == ZONE, "과거 Zone 이 그대로다"


def test_R9b_정책이_바뀌어도_기존_Lot_은_안_바뀐다(conn: psycopg.Connection) -> None:
    _일정(conn)
    receipt_id = _영수와_검수(conn)
    _돌린다(conn, receipt_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TMP_SCHEMA}.item_storage_policies SET storage_zone = 'FROZEN_DRY_-3'")

    _돌린다(conn, receipt_id)

    assert _lots(conn)[0]["storage_zone"] == ZONE


def test_R10_신규_Lot_은_정책이_없으면_실패한다(conn: psycopg.Connection) -> None:
    """★ 신규 Lot 의 Zone 권위 출처는 계속 `item_storage_policies` 다."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.item_storage_policies")
    _일정(conn)
    receipt_id = _영수와_검수(conn)

    with pytest.raises(LotIntegrityError, match="보관 정책"):
        _돌린다(conn, receipt_id)

    assert _lots(conn) == []
