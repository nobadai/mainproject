"""ARRIVED Receipt 를 **실제 PostgreSQL 에 세워 본다** (3-B4-G).

가짜 커서로는 못 재는 것 하나를 재려는 것이다 — *"이 행이 정말 들어가는가"*.

```text
NOT NULL 아홉 칸을 다 채웠나        item_id · arrived_at · receipt_status · fact_source …
CHECK 어휘를 맞췄나                 ARRIVED · SCENARIO_SIMULATED
CHECK 수량을 안 어겼나              COALESCE(...,0) >= 0
FK 넷을 다 만족했나                 sim_runs · purchase_items · items · storage_locations
같은 트랜잭션 재실행이 멱등한가      잠금 재진입 + 잠금 안 재조회
```

🔴 **공유 `haetdeul` 스키마를 건드리지 않는다.** 임시 스키마에 표를 세우고 끝나면
   **통째로 롤백한다** — `test_logistics_ledger_db.py` 와 같은 규율이다.

⚠️ **두 커넥션 동시성 시험은 넣지 않았다.** 그러려면 임시 스키마를 **커밋**해서
   다른 커넥션에 보이게 해야 하는데, 그것은 공유 DB 에 DDL 을 남기는 일이다
   (이 판의 약속은 `DDL 0 · DML 0`). 잠금이 조회보다 먼저이고 조회가 잠금 **안**에서
   일어난다는 순서는 `test_logistics_arrival_receipt.py` 가 가짜 커넥션으로 재고,
   여기서는 **같은 트랜잭션 재진입**(잠금이 자기를 막지 않는가)을 실물로 잰다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from app.logistics import receipts
from app.logistics.arrival import DueInbound
from app.logistics.db import get_connection
from app.logistics.purchase_detail import PurchaseDetail
from app.logistics.receipts import check_receipt_state, create_arrived_receipt
from app.logistics.schemas import InTransitItem

pytestmark = pytest.mark.db

TMP_SCHEMA = "receipt_verify"
SIM_RUN_ID = "SIM-RECEIPT-TEST"
ITEM_ID = "ITEM-TEST"
PURCHASE_ITEM_ID = "PI-TEST"
INBOUND_ID = "INB-H1-THRU-20260105-BAECHU-1-1"
ETA = date(2026, 1, 7)

_DB_DIR = Path(__file__).resolve().parents[3] / "database"

#: FK 대상만 되는 다른 도메인 표 — PK 만 있는 stub 이다.
#: ★ 이번 단계가 Purchase 스키마를 **안 고친다**는 사실이 여기서도 드러난다.
_STUBS = f"""
CREATE TABLE {TMP_SCHEMA}.items (item_id text PRIMARY KEY, item_name text);
CREATE TABLE {TMP_SCHEMA}.partners (partner_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sim_runs (sim_run_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.purchase_items (purchase_item_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sales (sale_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sale_items (sale_item_id text PRIMARY KEY);
"""


def _repo_block(table: str) -> str:
    """`10_domain_schema.sql` 의 표 하나를 CREATE·제약까지 그대로 뜬다.

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
    """임시 스키마에 입고 표를 세우고, 끝나면 **되돌린다**."""
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
        monkeypatch.setattr(receipts, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        # 🔴 COMMIT 하지 않는다 — 공유 DB 에 시험 흔적을 남기지 않는다.
        connection.rollback()
        connection.close()


def _due(*, eta: date = ETA, inbound_id: str = INBOUND_ID) -> DueInbound:
    return DueInbound(
        item=InTransitItem(
            inbound_id=inbound_id,
            purchase_id="PUR-THRU-20260105-BAECHU-D1-S1",
            item="배추",
            quantity_kg=Decimal("3587.0"),
            expected_arrival_date=eta,
        ),
        inbound_id=inbound_id,
        purchase_id="PUR-THRU-20260105-BAECHU-D1-S1",
        expected_arrival_date=eta,
        overdue=eta < ETA,
    )


def _detail() -> PurchaseDetail:
    return PurchaseDetail(
        purchase_item_id=PURCHASE_ITEM_ID,
        item_id=ITEM_ID,
        grade=None,
        quantity_kg=Decimal("3587.000000"),
        unit_price_krw_per_kg=Decimal("854.000000"),
    )


def _row(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {TMP_SCHEMA}.inbound_receipts")
        rows = cur.fetchall()
    assert len(rows) == 1, f"행이 {len(rows)} 개다"
    row = rows[0]
    if isinstance(row, dict):
        return row
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = 'inbound_receipts'"
            " AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            [TMP_SCHEMA],
        )
        칸 = [r[0] if not isinstance(r, dict) else r["attname"] for r in cur.fetchall()]
    return dict(zip(칸, row, strict=True))


# ── 실물 제약을 통과하는가 ──────────────────────────────────────────────


def test_ARRIVED_Receipt_가_실제로_들어간다(conn: psycopg.Connection) -> None:
    """🔴 **가짜 커서로는 못 재는 것이다.** NOT NULL 아홉 칸 · CHECK 셋 · FK 넷을
    실제 PostgreSQL 이 받아 주는지 본다.
    """
    결과 = create_arrived_receipt(
        conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail()
    )

    assert 결과.applied is True
    행 = _row(conn)
    assert 행["receipt_id"] == f"RCPT-{SIM_RUN_ID}-{INBOUND_ID}"
    assert 행["receipt_status"] == "ARRIVED"
    assert 행["fact_source"] == "SCENARIO_SIMULATED"
    assert 행["arrived_at"] == ETA
    assert 행["ordered_qty_kg"] == Decimal("3587.000000")
    assert 행["item_id"] == ITEM_ID
    assert 행["purchase_item_id"] == PURCHASE_ITEM_ID


def test_모르는_칸은_NULL_로_남는다(conn: psycopg.Connection) -> None:
    """🔴 **0 으로 채우지 않는다.** DDL 주석이 *"미입력(NULL)은 0 으로 보지 않는다"* 다 —
    0 을 넣으면 *"검수했고 수용 0kg"* 이 된다.
    """
    create_arrived_receipt(conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail())

    행 = _row(conn)
    for 칸 in (
        "accepted_qty_kg",
        "hold_qty_kg",
        "rejected_qty_kg",
        "receiving_location_id",
        "estimated_pallet_count",
        "actual_pallet_count",
        "received_by",
        "note",
    ):
        assert 행[칸] is None, f"{칸} 을 지어냈다"


def test_created_at_은_DB_가_채운다(conn: psycopg.Connection) -> None:
    """★ 우리가 시계를 읽지 않는다 — `DEFAULT now()` 에 맡긴다."""
    create_arrived_receipt(conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail())

    행 = _row(conn)
    assert 행["created_at"] is not None
    assert 행["updated_at"] is not None


def test_연체분도_예정일로_들어간다(conn: psycopg.Connection) -> None:
    """🔴 `as_of` 로 옮기면 그 로트가 더 신선한 것처럼 보인다."""
    create_arrived_receipt(
        conn,
        sim_run_id=SIM_RUN_ID,
        inbound=_due(eta=date(2026, 1, 5)),
        purchase_detail=_detail(),
    )

    assert _row(conn)["arrived_at"] == date(2026, 1, 5)


# ── 같은 트랜잭션에서 두 번 불러도 멱등한가 ────────────────────────────


def test_같은_트랜잭션에서_두_번_불러도_한_행이다(conn: psycopg.Connection) -> None:
    """🔴 **잠금 재진입 + 잠금 안 재조회를 실물로 잰다.**

    ★ `pg_advisory_xact_lock` 은 같은 세션이 같은 키를 다시 잡아도 자기를 막지
      않는다 — 두 번째 호출이 스스로 멈추면 여기서 영원히 걸린다.

    ★ 두 번째 호출이 `applied=False` 로 갈라지는 것은 **잠금 안에서 다시 조회하기
      때문**이다. 재조회가 없으면 두 번째가 INSERT 로 가서 UNIQUE 가 터진다.
    """
    첫번 = create_arrived_receipt(
        conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail()
    )
    두번 = create_arrived_receipt(
        conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail()
    )

    assert 첫번.applied is True
    assert 두번.applied is False
    assert 두번.receipt_id == 첫번.receipt_id
    assert 두번.receipt_status == "ARRIVED"
    _row(conn)  # 행이 정확히 하나임을 확인한다


def test_다른_입고를_이어_처리해도_교착이_없다(conn: psycopg.Connection) -> None:
    """🔴 **입고별 잠금이었다면 여기가 위험한 자리다** (`ledger` 가 겪은 그 교착).

    전역 잠금 하나라 한 트랜잭션이 여러 입고를 이어 처리해도 자기를 막지 않는다.
    """
    가 = create_arrived_receipt(
        conn,
        sim_run_id=SIM_RUN_ID,
        inbound=_due(inbound_id="INB-A-1"),
        purchase_detail=_detail(),
    )
    나 = create_arrived_receipt(
        conn,
        sim_run_id=SIM_RUN_ID,
        inbound=_due(inbound_id="INB-B-1"),
        purchase_detail=_detail(),
    )

    assert 가.applied is True and 나.applied is True
    assert 가.receipt_id != 나.receipt_id
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TMP_SCHEMA}.inbound_receipts")
        found = cur.fetchone()
    assert (found[0] if not isinstance(found, dict) else found["count"]) == 2


def test_UNIQUE_그물이_아직_살아_있다(conn: psycopg.Connection) -> None:
    """★ 잠금은 애플리케이션 방어이고, `uq_inbound_receipts_inbound_id` 는 **최종
    안전망**이다. 손으로 같은 열쇠를 밀어 넣으면 DB 가 막아야 한다.

    ⚠️ 그물이 우리 흐름에서 터지면 그것은 버그다 — 정상 멱등 제어는 잠금 + 재조회다.
    """
    create_arrived_receipt(conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail())

    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TMP_SCHEMA}.inbound_receipts (
                receipt_id, sim_run_id, inbound_id, item_id,
                arrived_at, receipt_status, fact_source
            ) VALUES (%s, %s, %s, %s, %s, 'ARRIVED', 'SCENARIO_SIMULATED')
            """,
            ("RCPT-DUPLICATE", SIM_RUN_ID, INBOUND_ID, ITEM_ID, ETA),
        )


# ── 조회가 같은 행을 되읽는가 ───────────────────────────────────────────


def test_쓴_행을_조회가_그대로_되읽는다(conn: psycopg.Connection) -> None:
    """★ 쓰기와 읽기가 **같은 정체성 축**을 쓰는지 실물로 확인한다."""
    create_arrived_receipt(conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail())

    존재 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 존재.status == "ALREADY_EXISTS"
    assert 존재.receipt_id == f"RCPT-{SIM_RUN_ID}-{INBOUND_ID}"
    assert 존재.receipt_status == "ARRIVED"


def test_다른_실행의_같은_입고는_안_보인다(conn: psycopg.Connection) -> None:
    """★ `sim_run_id` 로 범위를 잡는 이유다."""
    create_arrived_receipt(conn, sim_run_id=SIM_RUN_ID, inbound=_due(), purchase_detail=_detail())

    존재 = check_receipt_state(conn, sim_run_id="SIM-DIFFERENT", inbound_id=INBOUND_ID)

    assert 존재.status == "NEW"
