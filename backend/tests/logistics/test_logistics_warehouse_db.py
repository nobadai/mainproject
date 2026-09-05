"""Pallet · Location · Zone Capacity (3-E1). 실제 PostgreSQL 한 트랜잭션.

```text
Lot → place_lot_on_pallet → move_pallet → get_lot_position / get_zone_capacity
```

끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에 아무것도 남지 않는다.

🔴 **가짜로는 못 재는 것들을 잰다.**

```text
배치가 재고·원장을 정말로 안 건드리는가
한 자리에 Pallet 이 둘 앉지 못하는가          (uq_pallets_location)
살아 있는 Pallet 이 자리 없이 존재하지 못하는가 (ck_pallets_location_matches_status)
Zone 정책이 없는 품목을 막는가                 (추론 금지)
storage_zone 문자열이 zone_id 로 새지 않는가
```
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from app.logistics import disposal, ledger, outbound, turnover, warehouse
from app.logistics.db import get_connection
from app.logistics.warehouse import (
    InvalidPlacementRequest,
    PalletNotEmptyable,
    PlacementConflict,
    SpecItemMismatch,
    WarehouseIntegrityError,
    ZonePolicyUnresolved,
    empty_pallet,
    get_lot_position,
    get_zone_capacity,
    move_pallet,
    place_lot_on_pallet,
    required_pallet_count,
)

pytestmark = pytest.mark.db

TMP_SCHEMA = "warehouse_verify"
SIM_RUN_ID = "SIM-WAREHOUSE-TEST"

# ★ 실 DB 실측값 그대로다 — 이름도 숫자도 지어내지 않았다.
ITEM_ID = "ITEM-BAECHU"  # 기본 Zone HIGH_HUMIDITY_COLD · 350kg/PLT
OTHER_ITEM = "ITEM-YANGPA"  # 기본 Zone ONION_COOL_DRY · 450kg/PLT
NO_ZONE_ITEM = "ITEM-GEONGOCHU"  # 🔴 item_zone_assignments 에 줄이 **없다**
WAREHOUSE_ID = "WH-1"
COLD_ZONE = "HIGH_HUMIDITY_COLD"
ONION_ZONE = "ONION_COOL_DRY"
HOLD_ZONE = "HOLD_QUARANTINE"
KG_PER_PLT = Decimal(350)

# 🔴 Legacy 보관분류 문자열. `warehouse_zones.zone_id` 와 **다른 축**이다.
LEGACY_ZONE = "COLD_HUMID_0_3"

AS_OF = date(2026, 1, 20)
OCCURRED = datetime(2026, 1, 20, 9, 0, tzinfo=UTC)
BY = "WH-OPERATOR-1"

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
    """docstring 과 `#` 주석을 걷어낸 **실행되는 코드**만 남긴다."""
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                코드 = 코드.replace(doc, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _SQL만(source: str) -> str:
    """코드가 **DB 에 보내는 문자열**만 모은다.

    ★ 예외 메시지에 *"storage_zone 을 유추하지 않는다"* 라고 적은 설명까지 위반으로
      잡으면, 규칙을 적어 둔 것이 규칙 위반이 된다. 그래서 `SELECT`·`FROM` 이 든
      문자열만 본다.
    """
    조각 = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            글 = node.value
            if "SELECT" in 글 or "INSERT INTO" in 글 or "UPDATE " in 글:
                조각.append(글)
    return chr(10).join(조각)


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
            _씨앗(cur)
        for module in (warehouse, turnover, outbound, ledger, disposal):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _씨앗(cur: psycopg.Cursor) -> None:
    """실 DB 와 **같은 모양**의 창고를 세운다."""
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (SIM_RUN_ID,))
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES ('PI-TEST')")
    for item, name in ((ITEM_ID, "배추"), (OTHER_ITEM, "양파"), (NO_ZONE_ITEM, "건고추")):
        cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (item, name))
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
            " (item_id, storage_zone, operational_limit_days, operational_policy_status)"
            " VALUES (%s, %s, 10, 'PROVISIONAL')",
            (item, LEGACY_ZONE),
        )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.warehouses"
        " (warehouse_id, warehouse_name, network_type, operation_model, contract_type,"
        "  geometry_basis, source_ref)"
        " VALUES (%s, '테스트 창고', 'SINGLE_HUB_MVP', 'LEASED_SELF_OPERATED', 'LEASE',"
        "         'SIMULATION_GEOMETRY', 'TEST')",
        (WAREHOUSE_ID,),
    )
    zones = (
        (COLD_ZONE, "STORAGE_RACK", "NORMAL_STORAGE"),
        (ONION_ZONE, "STORAGE_RACK", "NORMAL_STORAGE"),
        (HOLD_ZONE, "WORK_FLOOR", "HOLD_QUARANTINE"),
    )
    for zone_id, kind, purpose in zones:
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.warehouse_zones"
            " (zone_id, warehouse_id, zone_code, zone_name, zone_kind, purpose,"
            "  environment_basis, source_ref)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'SIMULATION_ASSUMPTION', 'TEST')",
            (zone_id, WAREHOUSE_ID, zone_id, zone_id, kind, purpose),
        )
    # 자리: 냉장 3 · 양파 1 · HOLD 1
    for zone_id, n, kind in ((COLD_ZONE, 3, "RACK_POSITION"), (ONION_ZONE, 1, "RACK_POSITION")):
        for i in range(1, n + 1):
            _자리(cur, f"{zone_id}-P{i}", zone_id=zone_id, position=i, kind=kind)
    _자리(cur, f"{HOLD_ZONE}-P1", zone_id=HOLD_ZONE, position=1, kind="FLOOR_POSITION")
    # 🔴 Zone 정책은 **두 품목에만** 넣는다 — 실 DB 도 3/5 품목뿐이다.
    배정 = (
        (ITEM_ID, COLD_ZONE, True, True),
        (ITEM_ID, HOLD_ZONE, False, True),
        (ITEM_ID, ONION_ZONE, False, False),
        (OTHER_ITEM, ONION_ZONE, True, True),
    )
    for item, zone_id, 기본, 허용 in 배정:
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.item_zone_assignments"
            " (item_id, zone_id, is_default, allowed, source_ref) VALUES (%s, %s, %s, %s, 'TEST')",
            (item, zone_id, 기본, 허용),
        )
    # 포장 규격: 배추만 있다 — 환산 정본이 없는 품목도 재현한다.
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.item_packaging_specs"
        " (packaging_spec_id, item_id, package_type, nominal_unit_weight_kg,"
        "  default_kg_per_pallet, source_ref, evidence_grade, is_default)"
        " VALUES ('PKG-BAECHU', %s, 'NET', 10, %s, 'TEST', 'SIM_FIXED', TRUE)",
        (ITEM_ID, KG_PER_PLT),
    )


def _자리(cur: psycopg.Cursor, location_id: str, *, zone_id: str, position: int, kind: str) -> None:
    rack = ("R01", "B01", 1) if kind == "RACK_POSITION" else (None, None, None)
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.storage_locations"
        " (location_id, warehouse_id, zone_id, rack_code, bay_code, level_no,"
        "  position_no, location_kind) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (location_id, WAREHOUSE_ID, zone_id, *rack, position, kind),
    )


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _lot(
    conn: psycopg.Connection,
    lot_id: str = "LOT-A",
    *,
    qty: str = "100",
    item_id: str = ITEM_ID,
    받은날: date = AS_OF,
    status: str = "ACTIVE",
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status
                ) VALUES (%s, %s, 'PI-TEST', %s, %s, %s, %s, 1000, %s, %s)""",
            (lot_id, SIM_RUN_ID, item_id, 받은날, Decimal(qty), Decimal(qty), LEGACY_ZONE, status),
        )
    return lot_id


def _앉힌다(
    conn: psycopg.Connection,
    pallet_id: str = "PLT-1",
    *,
    lot_id: str = "LOT-A",
    location_id: str = f"{COLD_ZONE}-P1",
    **kw: object,
) -> warehouse.PlacementResult:
    return place_lot_on_pallet(
        conn,
        pallet_id=pallet_id,
        sim_run_id=SIM_RUN_ID,
        lot_id=lot_id,
        location_id=location_id,
        occurred_at=OCCURRED,
        recorded_by=BY,
        **kw,  # type: ignore[arg-type]
    )


def _한칸(conn: psycopg.Connection, sql: str, args: tuple = ()) -> object:
    """커넥션이 dict row 를 돌려주므로 **칸 이름**으로 읽는다."""
    with conn.cursor() as cur:
        cur.execute(sql, args)
        행들 = cur.fetchall()
    행 = 행들[0]
    이름 = next(iter(행)) if isinstance(행, dict) else 0
    return 행[이름]


def _remaining(conn: psycopg.Connection, lot_id: str = "LOT-A") -> Decimal:
    return _한칸(  # type: ignore[return-value]
        conn,
        f"SELECT remaining_qty_kg FROM {TMP_SCHEMA}.inventory_lots WHERE lot_id = %s",
        (lot_id,),
    )


def _moves(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT move_id, move_type, quantity_kg FROM {TMP_SCHEMA}.inventory_moves")
        return [(행["move_id"], 행["move_type"], 행["quantity_kg"]) for 행 in cur.fetchall()]


def _events(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT pallet_id, event_type, from_location_id, to_location_id"
            f" FROM {TMP_SCHEMA}.pallet_events ORDER BY pallet_event_id"
        )
        return [
            (행["pallet_id"], 행["event_type"], 행["from_location_id"], 행["to_location_id"])
            for 행 in cur.fetchall()
        ]


# ── 1~3 · Lot 하나가 여러 자리에 나뉜다 ─────────────────────────────────


def test_01_Lot_을_Pallet_한_장에_앉힌다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="100")

    결과 = _앉힌다(conn)

    assert 결과.applied is True
    assert (결과.pallet_id, 결과.location_id, 결과.zone_id) == (
        "PLT-1",
        f"{COLD_ZONE}-P1",
        COLD_ZONE,
    )
    assert 결과.status == "ACTIVE"
    assert _events(conn) == [("PLT-1", "CREATED", None, f"{COLD_ZONE}-P1")]


def test_02_한_Lot_이_여러_Pallet_에_나뉜다(conn: psycopg.Connection) -> None:
    """★ 스키마가 `1 Lot : N Pallet` 을 허용한다. 700kg ÷ 350 = 두 자리."""
    _lot(conn, qty="700")

    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")
    두번째 = _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2")

    assert 두번째.applied is True
    assert [p.pallet_id for p in get_lot_position(conn, sim_run_id=SIM_RUN_ID, lot_id="LOT-A")] == [
        "PLT-1",
        "PLT-2",
    ]


def test_03_자리_수_한도를_넘으면_거부한다(conn: psycopg.Connection) -> None:
    """🔴 **경계.** 700kg ÷ 350kg/PLT = 정확히 2자리. 세 번째는 막힌다.

    ⚠️ `pallets` 에 수량 칸이 없어 한도는 kg 이 아니라 **자리 수**로 잰다.
    """
    _lot(conn, qty="700")
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")
    _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2")

    with pytest.raises(InvalidPlacementRequest, match="자리 수를 넘는다"):
        _앉힌다(conn, "PLT-3", location_id=f"{COLD_ZONE}-P3")


def test_04_자투리도_한_자리를_받는다(conn: psycopg.Connection) -> None:
    """★ 올림이다 — 351kg 은 두 자리다. 반올림하면 1kg 이 갈 곳을 잃는다."""
    _lot(conn, qty="351")

    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")
    assert _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2").applied is True
    with pytest.raises(InvalidPlacementRequest, match="자리 수를 넘는다"):
        _앉힌다(conn, "PLT-3", location_id=f"{COLD_ZONE}-P3")


def test_05_환산_정본이_없으면_한도를_세지_않는다(conn: psycopg.Connection) -> None:
    """⚠️ 포장 규격이 없는 품목은 *"1 Pallet = 몇 kg"* 을 아무도 안 정했다.

    없는 근거로 막지도, 지어낸 근거로 통과시키지도 않는다 — 검사를 건너뛴다.
    """
    _lot(conn, "LOT-Y", qty="10", item_id=OTHER_ITEM)

    결과 = _앉힌다(conn, "PLT-Y", lot_id="LOT-Y", location_id=f"{ONION_ZONE}-P1")

    assert 결과.applied is True


# ── 4~5 · 배치는 재고를 만들지도 없애지도 않는다 ────────────────────────


def test_06_배치해도_Lot_잔량이_그대로다(conn: psycopg.Connection) -> None:
    """🔴 **이 판의 핵심 계약.** 물건이 어디 있는지는 물건의 양이 아니다."""
    _lot(conn, qty="700")

    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")
    _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2")

    assert _remaining(conn) == Decimal(700)


def test_07_배치는_원장_Move_를_만들지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="700")
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")

    move_pallet(
        conn,
        pallet_id="PLT-1",
        to_location_id=f"{COLD_ZONE}-P2",
        occurred_at=OCCURRED,
        recorded_by=BY,
    )

    assert _moves(conn) == []
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TMP_SCHEMA}.inventory_move_lines")
        assert cur.fetchall()[0]["n"] == 0


def test_08_배치_코드가_원장을_부르지_않는다(conn: psycopg.Connection) -> None:
    """★ 실행 경로뿐 아니라 **소스**로도 못박는다."""
    본문 = _코드만(Path(warehouse.__file__).read_text(encoding="utf-8"))

    for 금지 in ("record_inventory_move", "inventory_moves", "SET remaining_qty_kg"):
        assert 금지 not in 본문, f"배치가 원장을 건드린다: {금지}"


# ── 6~9 · 자리 배정과 이동 ──────────────────────────────────────────────


def test_09_자리를_옮긴다(conn: psycopg.Connection) -> None:
    _lot(conn)
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")

    결과 = move_pallet(
        conn,
        pallet_id="PLT-1",
        to_location_id=f"{COLD_ZONE}-P2",
        occurred_at=OCCURRED,
        recorded_by=BY,
    )

    assert (결과.applied, 결과.location_id) == (True, f"{COLD_ZONE}-P2")
    assert _events(conn)[-1] == ("PLT-1", "RELOCATED", f"{COLD_ZONE}-P1", f"{COLD_ZONE}-P2")


def test_10_Zone_을_넘는_이동도_정책을_다시_본다(conn: psycopg.Connection) -> None:
    """⚠️ 출발지에서 허용이었다고 목적지에서도 허용인 것은 아니다."""
    _lot(conn)
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")

    옮김 = move_pallet(
        conn,
        pallet_id="PLT-1",
        to_location_id=f"{HOLD_ZONE}-P1",
        occurred_at=OCCURRED,
        recorded_by=BY,
        event_type="HOLD_MOVED",
    )
    assert 옮김.zone_id == HOLD_ZONE

    with pytest.raises(InvalidPlacementRequest, match="금지된 Zone"):
        move_pallet(
            conn,
            pallet_id="PLT-1",
            to_location_id=f"{ONION_ZONE}-P1",
            occurred_at=OCCURRED,
            recorded_by=BY,
        )


def test_11_없는_자리와_없는_Pallet_을_거부한다(conn: psycopg.Connection) -> None:
    _lot(conn)

    with pytest.raises(InvalidPlacementRequest, match="없는 자리"):
        _앉힌다(conn, location_id="COLD-그런자리없음")

    _앉힌다(conn, "PLT-1")
    with pytest.raises(InvalidPlacementRequest, match="없는 Pallet"):
        move_pallet(
            conn,
            pallet_id="PLT-없음",
            to_location_id=f"{COLD_ZONE}-P2",
            occurred_at=OCCURRED,
            recorded_by=BY,
        )


def test_12_한_자리에_Pallet_은_하나다(conn: psycopg.Connection) -> None:
    """🔴 `uq_pallets_location` 이 터지기 **전에** 막는다 — UniqueViolation 을 흐름으로 안 쓴다."""
    _lot(conn, "LOT-A", qty="700")
    _lot(conn, "LOT-B", qty="700")
    _앉힌다(conn, "PLT-1", lot_id="LOT-A", location_id=f"{COLD_ZONE}-P1")

    with pytest.raises(InvalidPlacementRequest, match="이미 찬 자리"):
        _앉힌다(conn, "PLT-2", lot_id="LOT-B", location_id=f"{COLD_ZONE}-P1")

    # ★ 트랜잭션이 살아 있어야 한다 — 제약 위반이면 여기서 죽는다.
    assert _앉힌다(conn, "PLT-2", lot_id="LOT-B", location_id=f"{COLD_ZONE}-P2").applied is True


def test_13_닫힌_자리에는_앉히지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.storage_locations SET is_active = FALSE WHERE location_id = %s",
            (f"{COLD_ZONE}-P1",),
        )

    with pytest.raises(InvalidPlacementRequest, match="닫힌 자리"):
        _앉힌다(conn)


# ── 10~12 · 두 Zone 축을 섞지 않는다 ────────────────────────────────────


def test_14_storage_zone_문자열을_zone_id_로_바꾸지_않는다(conn: psycopg.Connection) -> None:
    """🔴 **이 판이 가장 조심한 자리다.**

    ```text
    inventory_lots.storage_zone   COLD_HUMID_0_3      ← Legacy 보관분류
    warehouse_zones.zone_id       HIGH_HUMIDITY_COLD  ← 물리 Zone
    ```

    둘을 잇는 표가 없다. 이름이 비슷해 보여도 옮길 근거가 아니다.
    """
    질의 = _SQL만(Path(warehouse.__file__).read_text(encoding="utf-8"))

    assert "storage_zone" not in 질의, "Legacy 보관분류를 질의가 읽고 있다"
    assert "item_storage_policies" not in 질의, "Legacy 보관정책 표를 보고 있다"
    _lot(conn)
    앉힘 = _앉힌다(conn)
    # ★ Lot 의 Legacy 문자열과 실제 배치된 Zone 이 **다른 값**인 채로 성공한다.
    assert 앉힘.zone_id == COLD_ZONE != LEGACY_ZONE


def test_15_Zone_정책이_없는_품목은_멈춘다(conn: psycopg.Connection) -> None:
    """⚠️ 모른다는 것과 *"아무 데나 된다"* 는 다르다."""
    _lot(conn, "LOT-G", qty="100", item_id=NO_ZONE_ITEM)

    with pytest.raises(ZonePolicyUnresolved, match="허용 Zone 을 모른다"):
        _앉힌다(conn, "PLT-G", lot_id="LOT-G")


def test_16_정책에_없는_Zone_은_금지다(conn: psycopg.Connection) -> None:
    """★ 정책은 있는데 이 Zone 줄이 없다 = 열거되지 않았다 = 금지."""
    _lot(conn, "LOT-Y", qty="100", item_id=OTHER_ITEM)

    with pytest.raises(InvalidPlacementRequest, match="금지된 Zone"):
        _앉힌다(conn, "PLT-Y", lot_id="LOT-Y", location_id=f"{COLD_ZONE}-P1")


def test_17_allowed_False_인_Zone_을_거부한다(conn: psycopg.Connection) -> None:
    _lot(conn)

    with pytest.raises(InvalidPlacementRequest, match="금지된 Zone"):
        _앉힌다(conn, location_id=f"{ONION_ZONE}-P1")


# ── 13~16 · Capacity ────────────────────────────────────────────────────


def test_18_Zone_정원과_점유를_자리_수로_센다(conn: psycopg.Connection) -> None:
    """🔴 **단위가 kg 이 아니다.** 자리 한 개 = Pallet 한 장이다."""
    _lot(conn, qty="700")
    빈창고 = get_zone_capacity(conn, zone_id=COLD_ZONE)
    assert (빈창고.total_positions, 빈창고.occupied_positions, 빈창고.free_positions) == (3, 0, 3)

    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")
    _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2")

    찬창고 = get_zone_capacity(conn, zone_id=COLD_ZONE)
    assert (찬창고.total_positions, 찬창고.occupied_positions, 찬창고.free_positions) == (3, 2, 1)


def test_19_정원이_차면_더_못_앉힌다(conn: psycopg.Connection) -> None:
    """🔴 **정확한 경계.** 자리 3개짜리 Zone 은 3장까지다."""
    for i in range(1, 4):
        _lot(conn, f"LOT-{i}", qty="100")
        _앉힌다(conn, f"PLT-{i}", lot_id=f"LOT-{i}", location_id=f"{COLD_ZONE}-P{i}")

    assert get_zone_capacity(conn, zone_id=COLD_ZONE).free_positions == 0
    _lot(conn, "LOT-4", qty="100")
    # ★ 남은 자리가 없으니 어느 자리를 대도 이미 차 있다.
    with pytest.raises(InvalidPlacementRequest, match="이미 찬 자리"):
        _앉힌다(conn, "PLT-4", lot_id="LOT-4", location_id=f"{COLD_ZONE}-P1")


def test_20_폐기대기_Lot_도_자리를_차지한다(conn: psycopg.Connection) -> None:
    """🔴 **Capacity 와 판매 가용은 다른 축이다.**

    ```text
    폐기대기 Lot   판매 가용 ❌ · Capacity 점유 ✅
    ```

    실제로 창고에 있으니 자리는 그대로 먹는다.
    """
    오래된날 = AS_OF - timedelta(days=30)
    _lot(conn, qty="100", 받은날=오래된날)
    _앉힌다(conn, "PLT-1")

    회전 = turnover.load_lot_turnover(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF, lot_id="LOT-A")
    assert 회전[0].disposal_candidate is True, "판매 가용에서는 빠진 Lot 이다"
    assert get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1


def test_21_예약된_Lot_도_출고_전까지_자리를_차지한다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="100")
    _앉힌다(conn, "PLT-1")
    outbound.reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(100),
        as_of=AS_OF,
    )

    assert get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1
    assert _remaining(conn) == Decimal(100)


def test_22_잔량이_없는_Lot_은_앉히지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="100")
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TMP_SCHEMA}.inventory_lots SET remaining_qty_kg = 0")

    with pytest.raises(InvalidPlacementRequest, match="잔량이 없는"):
        _앉힌다(conn)


# ── 17~18 · 멱등 · 충돌 ─────────────────────────────────────────────────


def test_23_같은_사실_재실행은_멱등이다(conn: psycopg.Connection) -> None:
    _lot(conn)
    첫번 = _앉힌다(conn)

    두번 = _앉힌다(conn)

    assert (첫번.applied, 두번.applied) == (True, False)
    assert 두번.location_id == f"{COLD_ZONE}-P1"
    assert len(_events(conn)) == 1, "이력이 두 번 남지 않는다"


def test_24_같은_pallet_id_에_다른_자리면_충돌이다(conn: psycopg.Connection) -> None:
    """🔴 조용히 덮으면 앞 배치가 소리 없이 사라진다."""
    _lot(conn)
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")

    with pytest.raises(PlacementConflict, match="current_location_id"):
        _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P2")


def test_25_같은_pallet_id_에_다른_Lot_이면_충돌이다(conn: psycopg.Connection) -> None:
    _lot(conn, "LOT-A")
    _lot(conn, "LOT-B")
    _앉힌다(conn, "PLT-1", lot_id="LOT-A")

    with pytest.raises(PlacementConflict, match="lot_id"):
        _앉힌다(conn, "PLT-1", lot_id="LOT-B")


def test_26_이미_그_자리면_이동도_멱등이다(conn: psycopg.Connection) -> None:
    _lot(conn)
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")

    결과 = move_pallet(
        conn,
        pallet_id="PLT-1",
        to_location_id=f"{COLD_ZONE}-P1",
        occurred_at=OCCURRED,
        recorded_by=BY,
    )

    assert 결과.applied is False
    assert len(_events(conn)) == 1


# ── 순수 계산 · 잠금 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("수량", "단위", "기대"),
    [("1", "350", 1), ("350", "350", 1), ("351", "350", 2), ("700", "350", 2), ("701", "350", 3)],
)
def test_27_자리_수는_올림이다(수량: str, 단위: str, 기대: int) -> None:
    assert required_pallet_count(quantity_kg=Decimal(수량), kg_per_pallet=Decimal(단위)) == 기대


def test_28_float_수량을_거부한다() -> None:
    with pytest.raises(InvalidPlacementRequest, match="Decimal 이어야"):
        required_pallet_count(quantity_kg=100.0, kg_per_pallet=Decimal(350))  # type: ignore[arg-type]


def test_29_잠금이_읽기보다_먼저다(conn: psycopg.Connection) -> None:
    """🔴 잠금 밖에서 본 자리 사정을 믿으면 둘이 같은 자리를 본다."""
    본문 = _코드만(Path(warehouse.__file__).read_text(encoding="utf-8"))
    시작 = 본문.index("def place_lot_on_pallet")
    끝 = 본문.index("def _assert_same_placement")
    몸통 = 본문[시작:끝]

    잠금 = 몸통.index("lock_warehouse_writes")
    확인 = 몸통.index("_check_free_position")
    쓰기 = 몸통.index("INSERT INTO")
    assert 잠금 < 확인 < 쓰기, "잠금 → 재확인 → 쓰기 순서여야 한다"


def test_30_커밋도_롤백도_새_커넥션도_없다() -> None:
    본문 = _코드만(Path(warehouse.__file__).read_text(encoding="utf-8"))

    for 금지 in (".commit()", ".rollback()", "get_connection"):
        assert 금지 not in 본문, f"트랜잭션 소유권을 침범한다: {금지}"


# ── 19~20 · 기존 계약 회귀 ──────────────────────────────────────────────


def test_31_Pallet_없이도_기존_입고가_끝난다(conn: psycopg.Connection) -> None:
    """🔴 **하위호환.** 배치는 뒤따르는 별도 단계지 입고의 전제가 아니다."""
    _lot(conn, qty="100")
    with conn.cursor() as cur:  # ★ 아직 절반만 들어온 Lot 을 재현한다.
        cur.execute(f"UPDATE {TMP_SCHEMA}.inventory_lots SET remaining_qty_kg = 50")
    ledger.record_inventory_move(
        conn,
        move_id="MOVE-IN-LOT-A",
        sim_run_id=SIM_RUN_ID,
        lot_id="LOT-A",
        move_type="IN",
        quantity_kg=Decimal(50),
        moved_at=AS_OF,
        reason_code="PURCHASE_RECEIPT",
    )

    assert _remaining(conn) == Decimal(100)
    assert get_lot_position(conn, sim_run_id=SIM_RUN_ID, lot_id="LOT-A") == ()


def test_32_Pallet_없이도_기존_출고가_끝난다(conn: psycopg.Connection) -> None:
    """🔴 할당의 `pallet_id` 는 여전히 NULL 이다. Pallet 강제 출고는 후속이다."""
    _lot(conn, qty="100")
    outbound.reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(30),
        as_of=AS_OF,
    )
    outbound.allocate_stock(
        conn,
        reservation_id="RSV-1",
        requests=[outbound.AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(30))],
        decided_by=BY,
        decided_at=OCCURRED,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )
    outbound.ship_allocated_stock(conn, reservation_id="RSV-1", shipped_at=AS_OF)

    assert _remaining(conn) == Decimal(70)
    with conn.cursor() as cur:
        cur.execute(f"SELECT pallet_id FROM {TMP_SCHEMA}.inventory_allocations")
        assert [행["pallet_id"] for 행 in cur.fetchall()] == [None]


def test_33_전체_시나리오_한_트랜잭션(conn: psycopg.Connection) -> None:
    """★ 배치 → 이동 → 정원 → 출고. 재고는 출고에서만 움직인다."""
    _lot(conn, qty="700")

    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")
    _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2")
    assert _remaining(conn) == Decimal(700)
    assert get_zone_capacity(conn, zone_id=COLD_ZONE).free_positions == 1

    move_pallet(
        conn,
        pallet_id="PLT-2",
        to_location_id=f"{COLD_ZONE}-P3",
        occurred_at=OCCURRED,
        recorded_by=BY,
    )
    assert _remaining(conn) == Decimal(700), "이동은 수량이 아니다"
    assert get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 2

    outbound.reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(100),
        as_of=AS_OF,
    )
    outbound.allocate_stock(
        conn,
        reservation_id="RSV-1",
        requests=[outbound.AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(100))],
        decided_by=BY,
        decided_at=OCCURRED,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )
    outbound.ship_allocated_stock(conn, reservation_id="RSV-1", shipped_at=AS_OF)

    assert _remaining(conn) == Decimal(600), "여기서 처음 줄어든다"
    assert get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 2, (
        "출고했다고 Pallet 이 스스로 사라지지 않는다"
    )


# ── 무결성 보정 4건 ─────────────────────────────────────────────────────


def _비운다(
    conn: psycopg.Connection, pallet_id: str = "PLT-1", **kw: object
) -> warehouse.EmptyResult:
    return empty_pallet(
        conn,
        pallet_id=pallet_id,
        occurred_at=OCCURRED,
        recorded_by=BY,
        **kw,  # type: ignore[arg-type]
    )


def _pallet행(conn: psycopg.Connection, pallet_id: str = "PLT-1") -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT status, current_location_id, emptied_at, packaging_spec_id"
            f" FROM {TMP_SCHEMA}.pallets WHERE pallet_id = %s",
            (pallet_id,),
        )
        return dict(cur.fetchall()[0])


def _비운다_전량(conn: psycopg.Connection, lot_id: str = "LOT-A") -> None:
    """출고로 잔량을 0 으로 만든다. **원장이 하는 일이다.**"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT remaining_qty_kg AS q FROM {TMP_SCHEMA}.inventory_lots WHERE lot_id = %s",
            (lot_id,),
        )
        남은 = cur.fetchall()[0]["q"]
    ledger.record_inventory_move(
        conn,
        move_id=f"MOVE-OUT-{lot_id}",
        sim_run_id=SIM_RUN_ID,
        lot_id=lot_id,
        move_type="OUT",
        quantity_kg=남은,
        moved_at=AS_OF,
        reason_code="SALE_FULFILLMENT",
    )


# ① 잔량 0 Lot 배치 차단 순서 ──────────────────────────────────────────


def test_34_포장규격이_없어도_빈_Lot_은_못_앉힌다(conn: psycopg.Connection) -> None:
    """🔴 **이 판이 고치는 자리다.**

    포장규격 조회를 먼저 하고 `None` 이면 돌아가 버리면, **규격이 없는 품목만** 빈
    Lot 을 자리에 다시 올릴 수 있게 된다 — 없는 재고가 Capacity 를 먹는다.

    ★ `ITEM-YANGPA` 는 기본 포장규격이 없다(씨앗 그대로).
    """
    _lot(conn, "LOT-Y", qty="100", item_id=OTHER_ITEM)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TMP_SCHEMA}.inventory_lots SET remaining_qty_kg = 0")

    with pytest.raises(InvalidPlacementRequest, match="잔량이 없는"):
        _앉힌다(conn, "PLT-Y", lot_id="LOT-Y", location_id=f"{ONION_ZONE}-P1")

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TMP_SCHEMA}.pallets")
        assert cur.fetchall()[0]["n"] == 0, "Pallet 이 들어가지 않았다"
    assert _events(conn) == [], "이력도 남지 않았다"


def test_35_잔량_검사가_포장규격_조회보다_먼저다(conn: psycopg.Connection) -> None:
    """★ 소스로도 순서를 못박는다."""
    본문 = _코드만(Path(warehouse.__file__).read_text(encoding="utf-8"))
    시작 = 본문.index("def _check_pallet_budget")
    몸통 = 본문[시작 : 본문.index("def move_pallet")]

    assert 몸통.index("remaining <= 0") < 몸통.index("_kg_per_pallet")


# ② packaging_spec_id ↔ Lot item_id 정합성 ────────────────────────────


def test_36_같은_품목의_포장규격은_통과한다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="100")

    결과 = _앉힌다(conn, packaging_spec_id="PKG-BAECHU")

    assert 결과.applied is True
    assert _pallet행(conn)["packaging_spec_id"] == "PKG-BAECHU"


def test_37_다른_품목의_포장규격을_거부한다(conn: psycopg.Connection) -> None:
    """🔴 **FK 는 존재만 본다.** 배추 Lot 에 양파 규격이 붙으면 환산이 조용히 틀린다."""
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.item_packaging_specs"
            " (packaging_spec_id, item_id, package_type, nominal_unit_weight_kg,"
            "  default_kg_per_pallet, source_ref, evidence_grade, is_default)"
            " VALUES ('PKG-YANGPA', %s, 'NET', 15, 450, 'TEST', 'SIM_FIXED', TRUE)",
            (OTHER_ITEM,),
        )
    _lot(conn, qty="100")

    with pytest.raises(SpecItemMismatch, match="다른 품목의 포장규격"):
        _앉힌다(conn, packaging_spec_id="PKG-YANGPA")

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TMP_SCHEMA}.pallets")
        assert cur.fetchall()[0]["n"] == 0
    assert _events(conn) == []


def test_38_없는_포장규격을_DML_전에_거부한다(conn: psycopg.Connection) -> None:
    """★ FK 가 뒤늦게 터지면 트랜잭션이 이미 더러워진다."""
    _lot(conn, qty="100")

    with pytest.raises(SpecItemMismatch, match="없는 포장규격"):
        _앉힌다(conn, packaging_spec_id="PKG-없음")

    # ★ 트랜잭션이 살아 있어야 한다 — 제약 위반이었다면 여기서 죽는다.
    assert _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2").applied is True


def test_39_규격을_안_준_것은_그대로_허용한다(conn: psycopg.Connection) -> None:
    """★ 규격을 아직 안 정한 것과 틀린 규격을 준 것은 다르다."""
    _lot(conn, qty="100")

    결과 = _앉힌다(conn)

    assert 결과.applied is True
    assert _pallet행(conn)["packaging_spec_id"] is None


# ③ move_pallet 의 event_type 범위 ─────────────────────────────────────


@pytest.mark.parametrize("어휘", ["CREATED", "EMPTIED", "PUTAWAY"])
def test_40_move_pallet_이_남의_사실을_적지_않는다(conn: psycopg.Connection, 어휘: str) -> None:
    """🔴 이벤트 vocabulary 와 함수 responsibility 는 다른 것이다.

    ```text
    CREATED   place_lot_on_pallet 전용
    EMPTIED   empty_pallet 전용
    PUTAWAY   이번 판에 정합한 경로가 없다 — 뜻을 억지로 만들지 않는다
    ```
    """
    _lot(conn)
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")

    with pytest.raises(InvalidPlacementRequest, match="event_type 이 아니다"):
        move_pallet(
            conn,
            pallet_id="PLT-1",
            to_location_id=f"{COLD_ZONE}-P2",
            occurred_at=OCCURRED,
            recorded_by=BY,
            event_type=어휘,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("어휘", "목적지"), [("RELOCATED", f"{COLD_ZONE}-P2"), ("HOLD_MOVED", f"{HOLD_ZONE}-P1")]
)
def test_41_실제_이동_어휘_두_개는_통과한다(
    conn: psycopg.Connection, 어휘: str, 목적지: str
) -> None:
    _lot(conn)
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")

    결과 = move_pallet(
        conn,
        pallet_id="PLT-1",
        to_location_id=목적지,
        occurred_at=OCCURRED,
        recorded_by=BY,
        event_type=어휘,  # type: ignore[arg-type]
    )

    assert 결과.applied is True
    assert _events(conn)[-1][:2] == ("PLT-1", 어휘)


# ④ empty_pallet ───────────────────────────────────────────────────────


def test_42_다_나간_Pallet_을_비운다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="100")
    _앉힌다(conn)
    _비운다_전량(conn)

    결과 = _비운다(conn)

    assert 결과.applied is True
    assert 결과.freed_location_id == f"{COLD_ZONE}-P1"
    assert 결과.zone_id == COLD_ZONE
    assert 결과.status == "EMPTIED"


def test_43_비운_뒤_상태_자리_시각이_스키마_계약을_지킨다(conn: psycopg.Connection) -> None:
    """★ `ck_pallets_location_matches_status` · `ck_pallets_emptied_at_required` 를 만족한다."""
    _lot(conn, qty="100")
    _앉힌다(conn)
    _비운다_전량(conn)

    _비운다(conn)

    행 = _pallet행(conn)
    assert 행["status"] == "EMPTIED"
    assert 행["current_location_id"] is None
    assert 행["emptied_at"] == OCCURRED


def test_44_EMPTIED_이벤트가_정확히_한_줄이다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="100")
    _앉힌다(conn)
    _비운다_전량(conn)

    _비운다(conn)

    이벤트 = _events(conn)
    assert 이벤트 == [
        ("PLT-1", "CREATED", None, f"{COLD_ZONE}-P1"),
        ("PLT-1", "EMPTIED", f"{COLD_ZONE}-P1", None),
    ]


def test_45_비우는_것만으로_원장도_잔량도_안_변한다(conn: psycopg.Connection) -> None:
    """🔴 수량 변화는 이미 OUT·DISPOSE 가 끝낸 뒤다."""
    _lot(conn, qty="100")
    _앉힌다(conn)
    _비운다_전량(conn)
    원장 = _moves(conn)

    _비운다(conn)

    assert _moves(conn) == 원장, "비우기가 Move 를 더 만들지 않았다"
    assert _remaining(conn) == Decimal(0)


def test_46_재고가_남아_있으면_비우지_못한다(conn: psycopg.Connection) -> None:
    """🔴 **Pallet 별 남은 물량을 추측하지 않는다.**

    한 Lot 이 여러 장에 나뉘어 있으면 어느 장이 비었는지 지금 스키마로는 증명할 수 없다.
    """
    _lot(conn, qty="700")
    _앉힌다(conn, "PLT-1", location_id=f"{COLD_ZONE}-P1")
    _앉힌다(conn, "PLT-2", location_id=f"{COLD_ZONE}-P2")

    with pytest.raises(PalletNotEmptyable, match="재고가 남아 있어"):
        _비운다(conn, "PLT-1")

    assert _pallet행(conn, "PLT-1")["status"] == "ACTIVE"
    assert _events(conn) == [
        ("PLT-1", "CREATED", None, f"{COLD_ZONE}-P1"),
        ("PLT-2", "CREATED", None, f"{COLD_ZONE}-P2"),
    ]


def test_47_일부만_나간_Lot_도_비우지_못한다(conn: psycopg.Connection) -> None:
    """⚠️ **덜 비우는 쪽으로 틀린다.** 자리는 늦게 돌아와도 되지만 있는 물건의 자리를
    남에게 내주면 안 된다.
    """
    _lot(conn, qty="100")
    _앉힌다(conn)
    ledger.record_inventory_move(
        conn,
        move_id="MOVE-OUT-일부",
        sim_run_id=SIM_RUN_ID,
        lot_id="LOT-A",
        move_type="OUT",
        quantity_kg=Decimal(99),
        moved_at=AS_OF,
        reason_code="SALE_FULFILLMENT",
    )

    with pytest.raises(PalletNotEmptyable, match="remaining 1"):
        _비운다(conn)


def test_48_같은_비우기_재실행은_멱등이다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="100")
    _앉힌다(conn)
    _비운다_전량(conn)
    첫번 = _비운다(conn)

    두번 = _비운다(conn)

    assert (첫번.applied, 두번.applied) == (True, False)
    assert 두번.status == "EMPTIED"
    assert len([e for e in _events(conn) if e[1] == "EMPTIED"]) == 1, "이력이 두 줄 생기지 않는다"


def test_49_EMPTIED_인데_자리가_남았으면_모순이다(conn: psycopg.Connection) -> None:
    """🔴 조용히 고치지 않는다 — 그 자리를 누가 쓰는지 사람이 봐야 한다."""
    _lot(conn, qty="100")
    _앉힌다(conn)
    _비운다_전량(conn)
    _비운다(conn)
    with conn.cursor() as cur:  # ★ 제약을 잠시 미뤄 모순 상태를 손으로 만든다.
        cur.execute(
            f"ALTER TABLE {TMP_SCHEMA}.pallets DROP CONSTRAINT ck_pallets_location_matches_status"
        )
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.pallets SET current_location_id = %s WHERE pallet_id = 'PLT-1'",
            (f"{COLD_ZONE}-P1",),
        )

    with pytest.raises(WarehouseIntegrityError, match="EMPTIED 인데 자리가 남아"):
        _비운다(conn)


def test_50_없는_Pallet_은_거부한다(conn: psycopg.Connection) -> None:
    with pytest.raises(InvalidPlacementRequest, match="없는 Pallet"):
        _비운다(conn, "PLT-없음")


# ⑤ Capacity 반환 ──────────────────────────────────────────────────────


def test_51_재고_0_만으로는_자리가_안_비고_명시적_비우기로_비운다(
    conn: psycopg.Connection,
) -> None:
    """🔴 **이 판의 핵심 계약.**

    ```text
    재고 0        ≠  자동으로 자리 비움
    empty_pallet  →  물리 자리 비움
    ```
    """
    _lot(conn, qty="100")
    _앉힌다(conn)
    assert get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1

    _비운다_전량(conn)
    assert _remaining(conn) == Decimal(0)
    쌓인채 = get_zone_capacity(conn, zone_id=COLD_ZONE)
    assert (쌓인채.occupied_positions, 쌓인채.free_positions) == (1, 2), (
        "재고가 0 이 됐다고 Pallet 이 스스로 사라지지 않는다"
    )

    _비운다(conn)

    비운뒤 = get_zone_capacity(conn, zone_id=COLD_ZONE)
    assert (비운뒤.occupied_positions, 비운뒤.free_positions) == (0, 3)


def test_52_비운_자리를_다른_Pallet_이_쓴다(conn: psycopg.Connection) -> None:
    """★ 자리가 정말로 돌아왔는지는 다음 Pallet 이 앉을 수 있느냐로 잰다."""
    for i in range(1, 4):
        _lot(conn, f"LOT-{i}", qty="100")
        _앉힌다(conn, f"PLT-{i}", lot_id=f"LOT-{i}", location_id=f"{COLD_ZONE}-P{i}")
    _lot(conn, "LOT-4", qty="100")
    with pytest.raises(InvalidPlacementRequest, match="이미 찬 자리"):
        _앉힌다(conn, "PLT-4", lot_id="LOT-4", location_id=f"{COLD_ZONE}-P1")

    _비운다_전량(conn, "LOT-1")
    _비운다(conn, "PLT-1")

    assert _앉힌다(conn, "PLT-4", lot_id="LOT-4", location_id=f"{COLD_ZONE}-P1").applied is True


def test_53_폐기로_0_이_돼도_자동으로_비지_않는다(conn: psycopg.Connection) -> None:
    """🔴 `confirm_disposal` 이 `empty_pallet` 을 부르지 않는다.

    한 Lot 이 여러 장에 나뉘면 **어느 장을 치웠는지** 원장이 알지 못한다.
    """
    오래된날 = AS_OF - timedelta(days=30)
    _lot(conn, qty="100", 받은날=오래된날)
    _앉힌다(conn)

    disposal.confirm_disposal(
        conn,
        disposal_id="DSP-1",
        sim_run_id=SIM_RUN_ID,
        lot_id="LOT-A",
        quantity_kg=Decimal(100),
        disposed_at=AS_OF,
        reason_code="QUALITY_UNSELLABLE",
        as_of=AS_OF,
    )

    assert _remaining(conn) == Decimal(0)
    assert get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1
    assert [e[1] for e in _events(conn)] == ["CREATED"], "폐기가 EMPTIED 를 적지 않았다"


def test_54_출고로_0_이_돼도_자동으로_비지_않는다(conn: psycopg.Connection) -> None:
    """🔴 `ship_allocated_stock` 도 Pallet 을 비우지 않는다."""
    _lot(conn, qty="100")
    _앉힌다(conn)
    outbound.reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(100),
        as_of=AS_OF,
    )
    outbound.allocate_stock(
        conn,
        reservation_id="RSV-1",
        requests=[outbound.AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(100))],
        decided_by=BY,
        decided_at=OCCURRED,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )
    outbound.ship_allocated_stock(conn, reservation_id="RSV-1", shipped_at=AS_OF)

    assert _remaining(conn) == Decimal(0)
    assert get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1
    assert [e[1] for e in _events(conn)] == ["CREATED"]


def test_55_자동_연결이_소스에도_없다() -> None:
    """★ 실행 경로뿐 아니라 **소스**로도 못박는다."""
    for 모듈 in (disposal, outbound, ledger):
        본문 = _코드만(Path(모듈.__file__).read_text(encoding="utf-8"))
        assert "empty_pallet" not in 본문, f"{Path(모듈.__file__).name} 이 자동으로 비운다"


def test_56_비우기도_원장을_안_부른다() -> None:
    본문 = _코드만(Path(warehouse.__file__).read_text(encoding="utf-8"))
    시작 = 본문.index("def empty_pallet")
    몸통 = 본문[시작 : 본문.index("def _zone_of")]

    for 금지 in ("record_inventory_move", "inventory_moves", "remaining_qty_kg ="):
        assert 금지 not in 몸통, f"비우기가 원장을 건드린다: {금지}"
    assert 몸통.index("lock_warehouse_writes") < 몸통.index("UPDATE"), "잠금이 먼저다"
