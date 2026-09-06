"""회전관리 · 판매우선 · 폐기대기 · 폐기확정 (3-D1). 실제 PostgreSQL 한 트랜잭션.

```text
Lot → turnover_status → sell_priority → disposal_candidate → confirm_disposal → DISPOSE
```

끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에 아무것도 남지 않는다.

🔴 **가짜로는 못 재는 것들을 잰다.**

```text
회전 정책 없는 품목이 조회에서 사라지지 않는가   (LEFT JOIN)
회전과 신선도가 서로 독립인가
폐기대기가 재고·Capacity 를 그대로 두는가
예약·할당이 폐기를 실제로 막는가
DISPOSE 가 잔량을 줄이고 IN/OUT 경로는 여전히 막히는가
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

from app.logistics import disposal, ledger, outbound, turnover
from app.logistics.db import get_connection
from app.logistics.disposal import (
    DisposalBlocked,
    DisposalIntegrityError,
    InvalidDisposalRequest,
    confirm_disposal,
    disposal_move_id_for,
)
from app.logistics.ledger import UnsupportedMoveType, record_inventory_move
from app.logistics.outbound import (
    AllocationRequest,
    InvalidOutboundRequest,
    allocate_stock,
    recommend_fefo_candidates,
    reserve_stock,
)
from app.logistics.turnover import (
    derive_turnover_status,
    elapsed_days,
    is_disposal_candidate,
    load_lot_turnover,
    remaining_turnover_days,
    sell_priority_of,
)

pytestmark = pytest.mark.db

TMP_SCHEMA = "turnover_verify"
SIM_RUN_ID = "SIM-TURNOVER-TEST"
ITEM_ID = "ITEM-BAECHU"  # 회전 정책 있음 (목표 10 · 판매우선 3)
NO_POLICY_ITEM = "ITEM-GEONGOCHU"  # 🔴 회전 정책 **없음** — 실 DB 와 같은 모양
SALE_ID = "SALE-TEST-1"
ZONE = "COLD_HUMID_0_3"
TARGET = 10
PRIORITY_DAYS = 3
LIMIT_DAYS = 10  # item_storage_policies.operational_limit_days (Legacy 신선도)
AS_OF = date(2026, 1, 20)
DECIDED_AT = datetime(2026, 1, 20, 9, 0, tzinfo=UTC)
REASON = "QUALITY_UNSELLABLE"

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
    assert match is not None, table
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
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES ('PI-TEST')")
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.sales VALUES (%s)", (SALE_ID,))
            for item, name in ((ITEM_ID, "배추"), (NO_POLICY_ITEM, "건고추")):
                cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (item, name))
                cur.execute(
                    f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
                    " (item_id, storage_zone, operational_limit_days,"
                    " operational_policy_status) VALUES (%s, %s, %s, 'PROVISIONAL')",
                    (item, ZONE, LIMIT_DAYS),
                )
            # 🔴 회전 정책은 **한 품목에만** 넣는다 — 실 DB 도 3/5 품목뿐이다.
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.item_turnover_policies"
                " (item_id, operational_turnover_target_days, sell_priority_remaining_days,"
                "  policy_status, evidence_grade, source_ref)"
                " VALUES (%s, %s, %s, 'SIMULATION_POLICY', 'SIM_FIXED', 'TEST')",
                (ITEM_ID, TARGET, PRIORITY_DAYS),
            )
        for module in (turnover, disposal, outbound, ledger):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _lot(
    conn: psycopg.Connection,
    lot_id: str,
    *,
    qty: str = "100",
    받은날: date,
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
            (lot_id, SIM_RUN_ID, item_id, 받은날, Decimal(qty), Decimal(qty), ZONE, status),
        )
    return lot_id


def _받은날(*, 경과: int) -> date:
    return AS_OF - __import__("datetime").timedelta(days=경과)


def _회전(conn: psycopg.Connection, lot_id: str | None = None, *, as_of: date = AS_OF):
    found = load_lot_turnover(conn, sim_run_id=SIM_RUN_ID, as_of=as_of, lot_id=lot_id)
    return found[0] if lot_id else found


def _lot행(conn: psycopg.Connection, lot_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT remaining_qty_kg, status FROM {TMP_SCHEMA}.inventory_lots WHERE lot_id=%s",
            (lot_id,),
        )
        row = cur.fetchone()
    return row if isinstance(row, dict) else {"remaining_qty_kg": row[0], "status": row[1]}


def _moves(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {TMP_SCHEMA}.inventory_moves ORDER BY move_id")
        이름 = [d.name for d in cur.description]
        return [
            r if isinstance(r, dict) else dict(zip(이름, r, strict=True)) for r in cur.fetchall()
        ]


def _폐기한다(conn: psycopg.Connection, lot_id: str, qty: str, *, did: str = "DSP-1", **kw):
    return confirm_disposal(
        conn,
        disposal_id=did,
        sim_run_id=SIM_RUN_ID,
        lot_id=lot_id,
        quantity_kg=kw.pop("quantity_kg", Decimal(qty)),
        disposed_at=AS_OF,
        reason_code=kw.pop("reason_code", REASON),
        as_of=kw.pop("as_of", AS_OF),
        **kw,
    )


# ══════════════════════════════════════════════════════════════════════
# 1~12. 회전관리
# ══════════════════════════════════════════════════════════════════════


def test_1_오늘_입고면_경과_0(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", 받은날=AS_OF)

    t = _회전(conn, "LOT-A")

    assert t.elapsed_days == 0
    assert t.remaining_turnover_days == TARGET


def test_2_여유가_있으면_NORMAL(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", 받은날=_받은날(경과=3))  # 남은 7 > 3

    t = _회전(conn, "LOT-A")

    assert t.turnover_status == "NORMAL"


def test_3_판매우선_경계_직전(conn: psycopg.Connection) -> None:
    """★ 남은 4 는 아직 `NORMAL` 이다 — 경계는 `<=` 다."""
    _lot(conn, "LOT-A", 받은날=_받은날(경과=6))

    t = _회전(conn, "LOT-A")

    assert t.remaining_turnover_days == 4
    assert t.turnover_status == "NORMAL"


def test_4_판매우선_진입(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", 받은날=_받은날(경과=7))  # 남은 3 == 판매우선일

    t = _회전(conn, "LOT-A")

    assert t.remaining_turnover_days == PRIORITY_DAYS
    assert t.turnover_status == "SELL_PRIORITY"


def test_5_남은_0_이면_목표초과(conn: psycopg.Connection) -> None:
    """★ 목표를 채운 날부터 초과로 본다."""
    _lot(conn, "LOT-A", 받은날=_받은날(경과=TARGET))

    t = _회전(conn, "LOT-A")

    assert t.remaining_turnover_days == 0
    assert t.turnover_status == "STORAGE_TARGET_EXCEEDED"


def test_6_목표를_넘기면_초과(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", 받은날=_받은날(경과=TARGET + 5))

    t = _회전(conn, "LOT-A")

    assert t.remaining_turnover_days == -5
    assert t.turnover_status == "STORAGE_TARGET_EXCEEDED"


def test_7_목표초과만으로는_판매제외도_폐기후보도_아니다(conn: psycopg.Connection) -> None:
    """🔴 **이 판의 핵심 경계다.** 회전목표 초과 ≠ 판매불가 ≠ 폐기.

    회전목표 10일은 지났지만 Legacy 신선도 한계(30일)는 안 지난 Lot 을 만든다.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.item_storage_policies SET operational_limit_days = 30"
            " WHERE item_id = %s",
            (ITEM_ID,),
        )
    _lot(conn, "LOT-A", 받은날=_받은날(경과=12))

    t = _회전(conn, "LOT-A")

    assert t.turnover_status == "STORAGE_TARGET_EXCEEDED"
    assert t.remaining_turnover_days == -2
    assert t.remaining_freshness_days == 18, "신선도는 아직 남았다"
    assert t.disposal_candidate is False, "회전목표 초과가 폐기후보를 만들지 않는다"


@pytest.mark.parametrize(
    ("경과", "상태", "우선"),
    [(3, "NORMAL", False), (7, "SELL_PRIORITY", True), (12, "STORAGE_TARGET_EXCEEDED", True)],
)
def test_8_9_10_sell_priority_는_상태를_따른다(
    conn: psycopg.Connection, 경과: int, 상태: str, 우선: bool
) -> None:
    _lot(conn, "LOT-A", 받은날=_받은날(경과=경과))

    t = _회전(conn, "LOT-A")

    assert t.turnover_status == 상태
    assert t.sell_priority is 우선


def test_11_회전정책_없는_품목이_조회에서_사라지지_않는다(conn: psycopg.Connection) -> None:
    """🔴 `INNER JOIN` 이면 계약 밖 품목의 재고가 통째로 사라진다."""
    _lot(conn, "LOT-BAECHU", 받은날=_받은날(경과=3))
    _lot(conn, "LOT-GOCHU", 받은날=_받은날(경과=3), item_id=NO_POLICY_ITEM)

    전체 = _회전(conn)

    assert [t.lot_id for t in 전체] == ["LOT-BAECHU", "LOT-GOCHU"]
    없는것 = next(t for t in 전체 if t.lot_id == "LOT-GOCHU")
    assert 없는것.remaining_turnover_days is None, "0 으로 지어내지 않는다"
    assert 없는것.turnover_status is None, "NORMAL 로 지어내지 않는다"
    assert 없는것.sell_priority is False
    assert 없는것.remaining_freshness_days == 7, "Legacy 신선도는 그대로 계산된다"


def test_12_회전과_신선도는_서로_독립이다(conn: psycopg.Connection) -> None:
    """★ 요구된 조합 — 회전은 초과인데 신선도는 남아 있다."""
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.item_storage_policies SET operational_limit_days = 20"
            " WHERE item_id = %s",
            (ITEM_ID,),
        )
    _lot(conn, "LOT-A", 받은날=_받은날(경과=12))

    t = _회전(conn, "LOT-A")

    assert (t.remaining_turnover_days, t.remaining_freshness_days) == (-2, 8)
    assert t.turnover_status == "STORAGE_TARGET_EXCEEDED" and t.sell_priority is True
    assert t.disposal_candidate is False


def test_12b_미래_입고일을_0_으로_보정하지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-FUTURE", 받은날=AS_OF)

    assert elapsed_days(received_at=date(2026, 1, 25), as_of=AS_OF) == -5
    assert (
        remaining_turnover_days(target_days=TARGET, received_at=date(2026, 1, 25), as_of=AS_OF)
        == 15
    )


def test_12c_순수_함수_경계(conn: psycopg.Connection) -> None:
    assert (
        derive_turnover_status(remaining_days=1, sell_priority_remaining_days=3) == "SELL_PRIORITY"
    )
    assert derive_turnover_status(remaining_days=4, sell_priority_remaining_days=3) == "NORMAL"
    assert derive_turnover_status(remaining_days=0, sell_priority_remaining_days=3) == (
        "STORAGE_TARGET_EXCEEDED"
    )
    assert sell_priority_of(None) is False
    assert is_disposal_candidate(remaining_freshness_days=None) is False, "0 != null"
    assert is_disposal_candidate(remaining_freshness_days=1) is False
    assert is_disposal_candidate(remaining_freshness_days=0) is True


# ══════════════════════════════════════════════════════════════════════
# 13~18. 폐기대기
# ══════════════════════════════════════════════════════════════════════


def test_13_신선도가_남으면_후보가_아니다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", 받은날=_받은날(경과=LIMIT_DAYS - 1))

    assert _회전(conn, "LOT-A").disposal_candidate is False


def test_14_신선도가_소진되면_후보다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A", 받은날=_받은날(경과=LIMIT_DAYS))

    t = _회전(conn, "LOT-A")

    assert t.remaining_freshness_days == 0
    assert t.disposal_candidate is True


def test_15_16_17_후보여도_재고와_Capacity_가_그대로다(conn: psycopg.Connection) -> None:
    """🔴 **폐기대기는 아무것도 바꾸지 않는다.**

    ```text
    Lot 100kg · disposal_candidate = true
    → 창고에 100kg 그대로 · Move 없음 · 점유 100kg
    ```
    """
    _lot(conn, "LOT-A", qty="100", 받은날=_받은날(경과=LIMIT_DAYS + 2))

    t = _회전(conn, "LOT-A")

    assert t.disposal_candidate is True
    행 = _lot행(conn, "LOT-A")
    assert 행["remaining_qty_kg"] == Decimal(100), "잔량이 그대로다"
    assert 행["status"] == "ACTIVE"
    assert _moves(conn) == [], "Move 가 생기지 않는다"
    assert t.remaining_qty_kg == Decimal(100), "점유 계산 대상으로 남는다"


def test_18_목표초과만으로는_후보가_되지_않는다(conn: psycopg.Connection) -> None:
    """🔴 `turnover.py` 가 `disposal.py` 를 임포트하지 않는 것이 그 단방향의 증거다."""
    코드 = _코드만(Path(turnover.__file__).read_text(encoding="utf-8"))

    assert "STORAGE_TARGET_EXCEEDED" in 코드, "어휘는 있다"
    assert "disposal" not in 코드.replace("disposal_candidate", ""), "폐기 모듈을 안 부른다"
    assert "confirm_disposal" not in 코드


# ══════════════════════════════════════════════════════════════════════
# 19~38. confirm_disposal
# ══════════════════════════════════════════════════════════════════════


def _후보Lot(conn: psycopg.Connection, lot_id: str = "LOT-A", qty: str = "100") -> str:
    return _lot(conn, lot_id, qty=qty, 받은날=_받은날(경과=LIMIT_DAYS + 2))


def test_19_20_21_부분_폐기(conn: psycopg.Connection) -> None:
    _후보Lot(conn)

    결과 = _폐기한다(conn, "LOT-A", "30")

    assert 결과.applied is True
    assert 결과.move_id == disposal_move_id_for(disposal_id="DSP-1")
    moves = _moves(conn)
    assert len(moves) == 1
    assert moves[0]["move_type"] == "DISPOSE"
    assert moves[0]["quantity_kg"] == Decimal(30)
    assert moves[0]["reason_code"] == REASON
    assert moves[0]["sale_item_id"] is None
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(70)
    assert _lot행(conn, "LOT-A")["status"] == "ACTIVE", "부분 폐기에 DISPOSED 를 붙이지 않는다"


def test_22_다른_Lot_은_영향이_없다(conn: psycopg.Connection) -> None:
    _후보Lot(conn, "LOT-A")
    _후보Lot(conn, "LOT-B")

    _폐기한다(conn, "LOT-A", "30")

    assert _lot행(conn, "LOT-B")["remaining_qty_kg"] == Decimal(100)


def test_23_24_전량_폐기는_DISPOSED_다(conn: psycopg.Connection) -> None:
    """★ `OUT` 으로 0 이 된 Lot 과 뜻이 다르다 — 그쪽은 팔린 것이라 상태가 안 바뀐다."""
    _후보Lot(conn)

    결과 = _폐기한다(conn, "LOT-A", "100")

    assert 결과.remaining_qty_kg == 0
    assert 결과.lot_status == "DISPOSED"
    행 = _lot행(conn, "LOT-A")
    assert 행["remaining_qty_kg"] == 0 and 행["status"] == "DISPOSED"


def test_25_같은_폐기_재실행은_멱등이다(conn: psycopg.Connection) -> None:
    _후보Lot(conn)
    첫번 = _폐기한다(conn, "LOT-A", "30")

    두번 = _폐기한다(conn, "LOT-A", "30")

    assert 첫번.applied is True and 두번.applied is False
    assert len(_moves(conn)) == 1
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(70), "두 번 줄지 않는다"


def test_25b_재실행은_오늘_후보인지_다시_묻지_않는다(conn: psycopg.Connection) -> None:
    """⚠️ 어제 적은 폐기가 오늘 판정으로 뒤집히면 안 된다."""
    _후보Lot(conn)
    _폐기한다(conn, "LOT-A", "30")

    두번 = _폐기한다(conn, "LOT-A", "30", as_of=date(2026, 1, 1))

    assert 두번.applied is False


def test_26_다른_수량_재실행은_충돌이다(conn: psycopg.Connection) -> None:
    _후보Lot(conn)
    _폐기한다(conn, "LOT-A", "30")

    with pytest.raises(DisposalIntegrityError):
        _폐기한다(conn, "LOT-A", "40")

    assert len(_moves(conn)) == 1


def test_26b_부분_폐기를_여러_번_할_수_있다(conn: psycopg.Connection) -> None:
    """🔴 `move_id` 가 `lot_id` 기반이면 두 번째가 첫 번째의 재실행으로 오인된다."""
    _후보Lot(conn)

    _폐기한다(conn, "LOT-A", "30", did="DSP-1")
    _폐기한다(conn, "LOT-A", "20", did="DSP-2")

    assert len(_moves(conn)) == 2
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(50)


def test_27_후보가_아닌_Lot_은_거부된다(conn: psycopg.Connection) -> None:
    """🔴 회전목표는 넘겼지만 신선도는 남은 Lot — 폐기 근거가 아니다."""
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.item_storage_policies SET operational_limit_days = 30"
            " WHERE item_id = %s",
            (ITEM_ID,),
        )
    _lot(conn, "LOT-A", 받은날=_받은날(경과=12))
    assert _회전(conn, "LOT-A").turnover_status == "STORAGE_TARGET_EXCEEDED"

    with pytest.raises(DisposalBlocked, match="폐기대기 대상이 아니다"):
        _폐기한다(conn, "LOT-A", "10")

    assert _moves(conn) == []


@pytest.mark.parametrize("나쁜값", ["0", "-5"])
def test_28_0_이하_수량은_거부된다(conn: psycopg.Connection, 나쁜값: str) -> None:
    _후보Lot(conn)

    with pytest.raises(InvalidDisposalRequest):
        _폐기한다(conn, "LOT-A", 나쁜값)

    assert _moves(conn) == []


@pytest.mark.parametrize("나쁜값", [30.0, Decimal("NaN"), Decimal("Infinity")])
def test_29_float_과_비유한값은_거부된다(conn: psycopg.Connection, 나쁜값) -> None:
    _후보Lot(conn)

    with pytest.raises(InvalidDisposalRequest):
        _폐기한다(conn, "LOT-A", "0", quantity_kg=나쁜값)

    assert _moves(conn) == []


def test_30_잔량_초과는_거부된다(conn: psycopg.Connection) -> None:
    _후보Lot(conn, qty="50")

    with pytest.raises(InvalidDisposalRequest, match="Lot 에서 없앨 수 있는"):
        _폐기한다(conn, "LOT-A", "60")

    assert _moves(conn) == []


def test_31_살아있는_할당을_침범하지_못한다(conn: psycopg.Connection) -> None:
    """🔴 **판매 가능하던 시절에 붙은 할당은 폐기가 못 건드린다.**

    ```text
    Lot 100 · 신선할 때 30 할당 → 나중에 폐기대기가 됨
    → 전량(100) 폐기 불가 · 남은 70 은 폐기 가능
    ```
    """
    _lot(conn, "LOT-A", qty="100", 받은날=AS_OF)  # 아직 신선하다
    reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(30),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )
    allocate_stock(
        conn,
        reservation_id="RSV-1",
        requests=[AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(30))],
        decided_by="WH-1",
        decided_at=DECIDED_AT,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )
    후보날 = AS_OF + __import__("datetime").timedelta(days=LIMIT_DAYS)
    assert _회전(conn, "LOT-A", as_of=후보날).disposal_candidate is True

    with pytest.raises(InvalidDisposalRequest, match="Lot 에서 없앨 수 있는"):
        _폐기한다(conn, "LOT-A", "100", as_of=후보날)

    assert _폐기한다(conn, "LOT-A", "70", as_of=후보날).applied is True


def test_32_폐기대기_Lot_은_애초에_예약할_수_없다(conn: psycopg.Connection) -> None:
    """🔴 **이 판이 고치는 자리다.** 종전에는 판매 못 하는 재고를 예약이 다시 잡았다.

    ★ 그래서 *"할당 안 된 예약이 폐기를 막는가"* 라는 상황 자체가 생기지 않는다 —
      예약이 폐기대기 재고를 근거로 서지 못한다.
    """
    _후보Lot(conn, qty="100")

    with pytest.raises(InvalidOutboundRequest, match="가용재고가 모자라"):
        reserve_stock(
            conn,
            reservation_id="RSV-1",
            sim_run_id=SIM_RUN_ID,
            item_id=ITEM_ID,
            required_qty_kg=Decimal(10),
            sale_id=SALE_ID,
            as_of=AS_OF,
        )


def test_32b_폐기대기_Lot_은_FEFO_후보도_할당_대상도_아니다(conn: psycopg.Connection) -> None:
    """★ 예약·FEFO·할당 세 자리에서 모두 빠진다."""
    _후보Lot(conn, "LOT-STALE", qty="100")
    _lot(conn, "LOT-FRESH", qty="50", 받은날=AS_OF)

    후보 = recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)
    assert [c.lot_id for c in 후보] == ["LOT-FRESH"], "폐기대기 Lot 이 후보에 없다"

    reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(50),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )
    with pytest.raises(InvalidOutboundRequest, match="가용 Lot 이 아니다|Lot 가용량"):
        allocate_stock(
            conn,
            reservation_id="RSV-1",
            requests=[AllocationRequest(lot_id="LOT-STALE", quantity_kg=Decimal(10))],
            decided_by="WH-1",
            decided_at=DECIDED_AT,
            allocation_basis="HUMAN_OVERRIDE",
            as_of=AS_OF,
        )


def test_32c_판매제외여도_재고와_점유는_그대로다(conn: psycopg.Connection) -> None:
    """🔴 **빠지는 것은 "팔 수 있는 양" 하나뿐이다.**"""
    _후보Lot(conn, qty="100")

    행 = _lot행(conn, "LOT-A")
    assert 행["remaining_qty_kg"] == Decimal(100), "on_hand 에 그대로 있다"
    assert 행["status"] == "ACTIVE", "자동으로 DISPOSED 가 되지 않는다"
    assert _회전(conn, "LOT-A").remaining_qty_kg == Decimal(100), "점유 계산 대상이다"
    assert _moves(conn) == []


def test_33_신선한_재고의_예약은_폐기와_무관하다(conn: psycopg.Connection) -> None:
    """★ 폐기대기 Lot 을 없애도 **판매 가능 재고가 줄지 않는다** — 그래서 다른 Lot 의
    예약이 이 폐기를 막지 않는다.
    """
    _후보Lot(conn, "LOT-STALE", qty="100")
    _lot(conn, "LOT-FRESH", qty="50", 받은날=AS_OF)
    reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(50),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )

    결과 = _폐기한다(conn, "LOT-STALE", "100")

    assert 결과.applied is True
    assert _lot행(conn, "LOT-FRESH")["remaining_qty_kg"] == Decimal(50), "예약분은 그대로다"


def test_34_출고된_수량을_이중_차감하지_않는다(conn: psycopg.Connection) -> None:
    """★ `SHIPPED` 몫은 원장 OUT 이 이미 잔량에서 뺐다 — 남은 것은 전부 폐기할 수 있다."""
    _lot(conn, "LOT-A", qty="100", 받은날=AS_OF)
    reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(30),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )
    allocate_stock(
        conn,
        reservation_id="RSV-1",
        requests=[AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(30))],
        decided_by="WH-1",
        decided_at=DECIDED_AT,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )
    outbound.ship_allocated_stock(conn, reservation_id="RSV-1", shipped_at=AS_OF)
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(70)
    후보날 = AS_OF + __import__("datetime").timedelta(days=LIMIT_DAYS)

    결과 = _폐기한다(conn, "LOT-A", "70", as_of=후보날)

    assert 결과.applied is True, "남은 70 전부 폐기할 수 있어야 한다"


def test_35_DISPOSE_외_어휘를_만들지_않는다(conn: psycopg.Connection) -> None:
    _후보Lot(conn)
    _폐기한다(conn, "LOT-A", "30")

    종류 = {m["move_type"] for m in _moves(conn)}
    assert 종류 == {"DISPOSE"}
    코드 = _코드만(Path(disposal.__file__).read_text(encoding="utf-8"))
    for 금지 in ("ADJUST", "inventory_count"):
        assert 금지 not in 코드


def test_36_37_38_타파트_표를_건드리지_않는다(conn: psycopg.Connection) -> None:
    for 파일 in (turnover.__file__, disposal.__file__):
        코드 = _코드만(Path(파일).read_text(encoding="utf-8"))
        for 금지 in ("app.master", "app.sales", "app.finance", "app.purchase_agent"):
            assert 금지 not in 코드, f"{금지} — 타파트를 끌어오고 있다"


def test_Ledger_는_여전히_IN_OUT_만_받는다(conn: psycopg.Connection) -> None:
    """🔴 **DISPOSE 를 아무 데서나 쓸 수 있게 열지 않았다.**"""
    _후보Lot(conn)

    with pytest.raises(UnsupportedMoveType):
        record_inventory_move(
            conn,
            move_id="MOVE-X",
            sim_run_id=SIM_RUN_ID,
            lot_id="LOT-A",
            move_type="DISPOSE",
            quantity_kg=Decimal(10),
            moved_at=AS_OF,
            reason_code=REASON,
        )

    assert _moves(conn) == []


def test_원장_밖에서_잔량을_고치지_않는다(conn: psycopg.Connection) -> None:
    코드 = _코드만(Path(disposal.__file__).read_text(encoding="utf-8"))

    # ⚠️ **대입만 잡는다.** `_mark_disposed` 의 `WHERE ... remaining_qty_kg = 0` 은
    #    "정말 0 일 때만 DISPOSED 를 붙인다" 는 **읽기 가드**라 잡으면 안 된다.
    assert "SET remaining_qty_kg" not in 코드, "수량 감소의 정본은 원장이다"
    assert "record_disposal_move" in 코드
    # ★ 상태만 바꾸는 UPDATE 는 있다 — 수량은 건드리지 않는다.
    assert "SET status = 'DISPOSED'" in 코드


# ══════════════════════════════════════════════════════════════════════
# 39~44. 트랜잭션 / 동시성
# ══════════════════════════════════════════════════════════════════════


def test_39_44_경계와_잠금(conn: psycopg.Connection) -> None:
    _후보Lot(conn)

    _폐기한다(conn, "LOT-A", "30")

    assert conn.info.transaction_status.name in {"INTRANS", "INERROR"}
    코드 = _코드만(Path(disposal.__file__).read_text(encoding="utf-8"))
    assert "get_connection" not in 코드
    assert "commit" not in 코드
    assert "rollback" not in 코드
    # ★ 새 잠금을 만들지 않고 출고 잠금을 재사용한다 — 순서 역전이 생길 자리가 없다.
    assert "lock_outbound_writes" in 코드
    assert "pg_advisory" not in 코드, "직접 새 키를 잡지 않는다"


def test_43_잠금_뒤에_한도를_다시_센다(conn: psycopg.Connection) -> None:
    """★ 잠금 → 후보 판정 → 한도 계산 순서다."""
    코드 = _코드만(Path(disposal.__file__).read_text(encoding="utf-8"))
    본문 = 코드.split("def confirm_disposal(")[1]

    잠금 = 본문.index("lock_outbound_writes")
    후보 = 본문.index("load_lot_turnover")
    한도 = 본문.index("_lot_disposable_qty")
    assert 잠금 < 후보 < 한도, "잠금이 먼저이고 그 안에서 다시 센다"


def test_전체_시나리오_한_트랜잭션(conn: psycopg.Connection) -> None:
    """★ 요청하신 걷기 그대로 — 마지막에 롤백까지 확인한다."""
    _lot(conn, "LOT-A", qty="100", 받은날=_받은날(경과=7))  # 남은 3 → SELL_PRIORITY

    t = _회전(conn, "LOT-A")
    assert t.turnover_status == "SELL_PRIORITY" and t.sell_priority is True
    assert _moves(conn) == []

    # 회전목표 초과까지 흘러도 재고는 그대로다.
    늦은날 = AS_OF + __import__("datetime").timedelta(days=5)
    t2 = _회전(conn, "LOT-A", as_of=늦은날)
    assert t2.turnover_status == "STORAGE_TARGET_EXCEEDED"
    assert _moves(conn) == []
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(100)

    # 신선도가 소진되면 폐기대기 — 그래도 재고·점유는 그대로다.
    후보날 = AS_OF + __import__("datetime").timedelta(days=3)  # 경과 10 → 신선도 0
    t3 = _회전(conn, "LOT-A", as_of=후보날)
    assert t3.disposal_candidate is True
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(100)

    # 🔴 폐기대기가 된 뒤에는 **예약 자체가 서지 않는다** — 판매 가용에서 빠졌다.
    with pytest.raises(InvalidOutboundRequest, match="가용재고가 모자라"):
        reserve_stock(
            conn,
            reservation_id="RSV-1",
            sim_run_id=SIM_RUN_ID,
            item_id=ITEM_ID,
            required_qty_kg=Decimal(30),
            sale_id=SALE_ID,
            as_of=후보날,
        )

    # 사람이 확정해야 비로소 재고가 줄어든다.
    결과 = _폐기한다(conn, "LOT-A", "30", as_of=후보날)
    assert 결과.applied is True
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(70)

    # 재실행은 늘지 않는다.
    assert _폐기한다(conn, "LOT-A", "30", as_of=후보날).applied is False
    assert len(_moves(conn)) == 1
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(70)

    # 전량 폐기.
    결과2 = _폐기한다(conn, "LOT-A", "70", did="DSP-2", as_of=후보날)
    assert 결과2.remaining_qty_kg == 0 and 결과2.lot_status == "DISPOSED"

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", [f"{TMP_SCHEMA}.inventory_lots"])
        남았나 = cur.fetchone()
    assert (남았나[0] if not isinstance(남았나, dict) else 남았나["to_regclass"]) is None


# ── 보강: 진입점 · replay identity ─────────────────────────────────────


def test_DISPOSE_저수준_함수가_공개_API_가_아니다(conn: psycopg.Connection) -> None:
    """🔴 폐기의 업무 진입점은 `confirm_disposal()` **하나**여야 한다.

    저수준 함수가 밖에서 보이면 후보 검증·예약 보호를 건너뛰는 우회로가 생긴다.
    """
    assert not any("disposal" in name for name in ledger.__all__), ledger.__all__
    assert "record_disposal_move" not in dir(ledger), "밑줄 없는 이름이 남아 있다"
    assert hasattr(ledger, "_record_disposal_move"), "내부 helper 는 있어야 한다"

    # ★ `disposal.py` 만 그 helper 를 쓴다.
    쓰는곳 = [
        경로.name
        for 경로 in Path(disposal.__file__).parent.glob("*.py")
        if "_record_disposal_move" in 경로.read_text(encoding="utf-8")
    ]
    assert sorted(쓰는곳) == ["disposal.py", "ledger.py"], 쓰는곳


def test_같은_참조에_다른_note_면_충돌이다(conn: psycopg.Connection) -> None:
    """★ 원장 멱등 판정이 `note` 를 포함하므로 폐기 재실행도 같은 눈으로 봐야 한다."""
    _후보Lot(conn)
    _폐기한다(conn, "LOT-A", "30", note="곰팡이 발생")

    with pytest.raises(DisposalIntegrityError, match="note"):
        _폐기한다(conn, "LOT-A", "30", note="다른 설명")

    assert len(_moves(conn)) == 1


def test_같은_참조에_같은_note_면_멱등이다(conn: psycopg.Connection) -> None:
    _후보Lot(conn)
    첫번 = _폐기한다(conn, "LOT-A", "30", note="곰팡이 발생")

    두번 = _폐기한다(conn, "LOT-A", "30", note="곰팡이 발생")

    assert 첫번.applied is True and 두번.applied is False
    assert len(_moves(conn)) == 1
    assert _moves(conn)[0]["note"] == "곰팡이 발생"


def test_confirm_disposal_이후에만_remaining_이_준다(conn: psycopg.Connection) -> None:
    """🔴 폐기대기 → 실제 감소 사이에 자동 경로가 없다."""
    _후보Lot(conn, qty="100")
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(100)

    # 회전·후보 판정을 아무리 돌려도 재고는 그대로다.
    for _ in range(3):
        assert _회전(conn, "LOT-A").disposal_candidate is True
    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(100)
    assert _moves(conn) == []

    _폐기한다(conn, "LOT-A", "40")

    assert _lot행(conn, "LOT-A")["remaining_qty_kg"] == Decimal(60)
