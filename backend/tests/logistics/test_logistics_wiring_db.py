"""Logistics 최종 Wiring — WMS core 가 외부 계약까지 닿는가 (3-F). 실제 PostgreSQL.

```text
Purchase 참조 → 도착 → Receipt → 검수 → Lot → 원장 IN
              → Snapshot / inventory_by_item
              → 예약 → FEFO → 할당 → 원장 OUT
              → 회전 signal → 폐기대기 → 폐기확정
              → Pallet 배치 → 이동 → empty_pallet
              → 운송 견적
```

끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에 아무것도 남지 않는다.

🔴 **이 파일은 새 업무기능을 만들지 않는다.** 이미 있는 명시적 API 를 **순서대로 직접**
   부른다 — 하나의 자동 workflow 로 묶지 않는다. 묶는 순간 *"조회가 재고를 움직였다"*
   같은 사고가 숨을 곳이 생긴다.

★ 잰다.

```text
inventory_by_item 이 outbound 와 **같은 답**을 내는가
읽기 요청이 쓰기를 만들지 않는가
as_of 를 caller 가 준 대로만 쓰는가
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

from app.logistics import disposal, ledger, outbound, repository, transport, turnover, warehouse
from app.logistics.db import get_connection
from app.logistics.schemas import InventoryLogisticsSnapshot, OutboundCommitment
from app.logistics.tools import build_inventory_by_item

pytestmark = pytest.mark.db

TMP_SCHEMA = "wiring_verify"
SIM_RUN_ID = "SIM-WIRING-TEST"
ITEM_ID = "ITEM-BAECHU"
ITEM_NAME = "배추"
SALE_ID = "SALE-WIRING-1"
LEGACY_ZONE = "COLD_HUMID_0_3"
LIMIT_DAYS = 10
AS_OF = date(2026, 1, 20)
DECIDED_AT = datetime(2026, 1, 20, 9, 0, tzinfo=UTC)
BY = "WH-OPERATOR-1"

WAREHOUSE_ID = "WH-1"
COLD_ZONE = "HIGH_HUMIDITY_COLD"
KG_PER_PLT = Decimal(350)

CONTRACT = "LOGI-BASE-5PL"
PERSONA = "PERSONA-V1.3"
BODY = "REEFER"

_DB_DIR = Path(__file__).resolve().parents[3] / "database"

_STUBS = f"""
CREATE TABLE {TMP_SCHEMA}.items (item_id text PRIMARY KEY, item_name text);
CREATE TABLE {TMP_SCHEMA}.partners (partner_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sim_runs (sim_run_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.purchase_items (purchase_item_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sales (sale_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sale_items (sale_item_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.company_personas (persona_id text PRIMARY KEY);
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
            for table in (
                "inventory_lots",
                "inventory_moves",
                "item_storage_policies",
                "logistics_contracts",
            ):
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
        for module in (repository, turnover, outbound, ledger, disposal, warehouse, transport):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA, raising=False)

        # 🔴 Repository 는 자기 connection 을 연다 — 이 트랜잭션 안에서 재우려고
        #    `fetch_all` 을 이 커넥션으로 갈아끼운다. 커밋은 여전히 없다.
        def _fetch_all(query: object, params: object = None) -> list[dict]:
            with connection.cursor() as cur:
                cur.execute(query, params)  # type: ignore[arg-type]
                return [dict(행) for 행 in cur.fetchall()]

        monkeypatch.setattr(repository, "fetch_all", _fetch_all)
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _씨앗(cur: psycopg.Cursor) -> None:
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (SIM_RUN_ID,))
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES ('PI-TEST')")
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.sales VALUES (%s)", (SALE_ID,))
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.company_personas VALUES (%s)", (PERSONA,))
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (ITEM_ID, ITEM_NAME))
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
        " (item_id, storage_zone, operational_limit_days, operational_policy_status)"
        " VALUES (%s, %s, %s, 'PROVISIONAL')",
        (ITEM_ID, LEGACY_ZONE, LIMIT_DAYS),
    )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.item_turnover_policies"
        " (item_id, operational_turnover_target_days, sell_priority_remaining_days,"
        "  policy_status, evidence_grade, source_ref)"
        " VALUES (%s, 10, 3, 'SIMULATION_POLICY', 'SIM_FIXED', 'TEST')",
        (ITEM_ID,),
    )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.warehouses"
        " (warehouse_id, warehouse_name, network_type, operation_model, contract_type,"
        "  geometry_basis, source_ref)"
        " VALUES (%s, '테스트 창고', 'SINGLE_HUB_MVP', 'LEASED_SELF_OPERATED', 'LEASE',"
        "         'SIMULATION_GEOMETRY', 'TEST')",
        (WAREHOUSE_ID,),
    )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.warehouse_zones"
        " (zone_id, warehouse_id, zone_code, zone_name, zone_kind, purpose,"
        "  environment_basis, source_ref)"
        " VALUES (%s, %s, %s, %s, 'STORAGE_RACK', 'NORMAL_STORAGE',"
        "         'SIMULATION_ASSUMPTION', 'TEST')",
        (COLD_ZONE, WAREHOUSE_ID, COLD_ZONE, COLD_ZONE),
    )
    for i in (1, 2):
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.storage_locations"
            " (location_id, warehouse_id, zone_id, rack_code, bay_code, level_no,"
            "  position_no, location_kind)"
            " VALUES (%s, %s, %s, 'R01', 'B01', 1, %s, 'RACK_POSITION')",
            (f"{COLD_ZONE}-P{i}", WAREHOUSE_ID, COLD_ZONE, i),
        )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.item_zone_assignments"
        " (item_id, zone_id, is_default, allowed, source_ref)"
        " VALUES (%s, %s, TRUE, TRUE, 'TEST')",
        (ITEM_ID, COLD_ZONE),
    )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.item_packaging_specs"
        " (packaging_spec_id, item_id, package_type, nominal_unit_weight_kg,"
        "  default_kg_per_pallet, source_ref, evidence_grade, is_default)"
        " VALUES ('PKG-BAECHU', %s, 'NET', 10, %s, 'TEST', 'SIM_FIXED', TRUE)",
        (ITEM_ID, KG_PER_PLT),
    )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.vehicle_specs"
        " (vehicle_class, body_type, max_payload_kg, operational_payload_kg,"
        "  max_pallet_floor_count, source_ref, evidence_grade)"
        " VALUES ('1t', %s, 1000, 800, 2, 'TEST', 'ASSUMED')",
        (BODY,),
    )
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.vehicle_rate_table"
        " (rate_id, vehicle_class, body_type, distance_from_km, distance_to_km,"
        "  base_rate_krw, rate_type, evidence_grade, source_ref)"
        " VALUES ('RATE-1T-2636', '1t', %s, 26, 36, 100000,"
        "         'SIMULATION_BASELINE', 'ASSUMED', 'TEST')",
        (BODY,),
    )
    cur.execute(
        f"""INSERT INTO {TMP_SCHEMA}.logistics_contracts (
                logistics_contract_id, company_persona_id, logistics_model, own_warehouse,
                own_vehicle_count, required_capacity_plt, guaranteed_capacity_plt,
                effective_kg_per_pallet, equivalent_capacity_ton,
                storage_rate_per_plt_month_krw, handling_rate_per_plt_event_krw,
                vehicle_class, delivery_distance_km, transport_cost_per_delivery_krw,
                management_fee_rate, monthly_storage_cost_krw, monthly_handling_cost_krw,
                monthly_transport_cost_krw, monthly_management_fee_krw,
                monthly_total_logistics_cost_krw, safety_stock_ratio,
                capacity_safety_margin_rate, contract_status, provisional
            ) VALUES (%s, %s, '5PL_NETWORK_ORCHESTRATION', FALSE, 0, 7, 8, 800, 6.4,
                      54000, 5000, '2.5t 냉장/냉동', 30, 130000, 0.08, 432000, 360000,
                      1950000, 219360, 2961360, 0.2, 0.1, 'BASELINE_ONLY', TRUE)""",
        (CONTRACT, PERSONA),
    )


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _lot(
    conn: psycopg.Connection, lot_id: str = "LOT-A", *, qty: str = "700", 받은날: date = AS_OF
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status
                ) VALUES (%s, %s, 'PI-TEST', %s, %s, %s, %s, 1000, %s, 'ACTIVE')""",
            (lot_id, SIM_RUN_ID, ITEM_ID, 받은날, Decimal(qty), Decimal(qty), LEGACY_ZONE),
        )
    return lot_id


def _스냅샷(conn: psycopg.Connection, *, as_of: date = AS_OF) -> InventoryLogisticsSnapshot:
    """Repository 가 읽는 Lot·예약·할당으로 최소 Snapshot 을 세운다.

    ★ fixture·policy 는 이 판의 검사 대상이 아니라 고정값으로 채운다. 검사하는 것은
      **예약·할당 축이 Snapshot 까지 실려 오는가** 다.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT l.lot_id, i.item_name, l.grade, l.received_at, l.remaining_qty_kg,
                       l.status, l.storage_zone, p.operational_limit_days, p.medium_grade_factor
                FROM {TMP_SCHEMA}.inventory_lots l
                JOIN {TMP_SCHEMA}.items i ON i.item_id = l.item_id
                JOIN {TMP_SCHEMA}.item_storage_policies p ON p.item_id = l.item_id
                WHERE l.sim_run_id = %s AND l.received_at <= %s AND l.remaining_qty_kg > 0
                ORDER BY l.lot_id""",
            (SIM_RUN_ID, as_of),
        )
        lots = [repository._inventory_lot_from_row(dict(행), as_of=as_of) for 행 in cur.fetchall()]
    return InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=as_of,
        on_hand_by_lot=lots,
        item_storage_policies=[],
        in_transit=[],
        confirmed_inbound_schedule=[],
        confirmed_outbound_schedule=[],
        outbound_commitments=repository.get_outbound_commitments(sim_run_id=SIM_RUN_ID),
        used_capacity_kg=sum((lot.available_qty_kg for lot in lots), start=Decimal(0)),
        guaranteed_capacity_by_zone_kg=None,
        evidence_refs=["TEST"],
    )


def _가용(conn: psycopg.Connection, *, as_of: date = AS_OF) -> Decimal:
    """`outbound` 가 보는 예약 가능량."""
    from psycopg import sql

    with conn.cursor() as cur:
        outbound.lock_outbound_writes(cur)
    return outbound.item_free_stock_qty(
        conn, sql.Identifier(TMP_SCHEMA), sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=as_of
    )


def _품목가용(snapshot: InventoryLogisticsSnapshot) -> Decimal:
    행들 = build_inventory_by_item(snapshot)
    assert 행들 is not None
    return next((행.available_qty_kg for 행 in 행들 if 행.item == ITEM_NAME), Decimal(0))


def _예약(conn: psycopg.Connection, rid: str, qty: str) -> None:
    outbound.reserve_stock(
        conn,
        reservation_id=rid,
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(qty),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )


def _할당(conn: psycopg.Connection, rid: str, lot_id: str, qty: str) -> None:
    outbound.allocate_stock(
        conn,
        reservation_id=rid,
        requests=[outbound.AllocationRequest(lot_id=lot_id, quantity_kg=Decimal(qty))],
        decided_by=BY,
        decided_at=DECIDED_AT,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )


# ── 1. 예약·할당 축이 외부 집계까지 닿는가 ──────────────────────────────


def test_01_예약_전에는_두_축이_같은_전량을_본다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="700")

    assert _품목가용(_스냅샷(conn)) == Decimal(700) == _가용(conn)


def test_02_할당한_몫이_품목_가용에서도_빠진다(conn: psycopg.Connection) -> None:
    """🔴 **이 판이 잇는 자리다.** 종전에는 예약·할당이 `inventory_by_item` 에 안 보였다.

    ```text
    Lot 700 · 예약 300 · 그중 300 할당
    outbound 가용        400
    inventory_by_item    종전 700 🔴 → 이제 400
    ```
    """
    _lot(conn, qty="700")
    _예약(conn, "RSV-1", "300")
    _할당(conn, "RSV-1", "LOT-A", "300")

    assert _품목가용(_스냅샷(conn)) == Decimal(400) == _가용(conn)


def test_03_Lot_을_안_고른_예약도_품목_가용에서_빠진다(conn: psycopg.Connection) -> None:
    """★ 미할당 예약은 어느 Lot 에도 안 붙어 있어 Lot 축만 보면 안 빠진다."""
    _lot(conn, qty="700")
    _예약(conn, "RSV-1", "300")

    assert _품목가용(_스냅샷(conn)) == Decimal(400) == _가용(conn)


def test_04_일부만_할당해도_확보_총량은_그대로다(conn: psycopg.Connection) -> None:
    """★ 미할당 200 + 할당 100 = 300 — 어느 쪽으로 세도 잡힌 몫은 300 이다."""
    _lot(conn, qty="700")
    _예약(conn, "RSV-1", "300")
    _할당(conn, "RSV-1", "LOT-A", "100")

    assert _품목가용(_스냅샷(conn)) == Decimal(400) == _가용(conn)


def test_05_출고하면_두_축이_함께_줄어든다(conn: psycopg.Connection) -> None:
    """🔴 이중 차감 검사. 출고분은 이미 `remaining_qty_kg` 에서 빠졌다."""
    _lot(conn, qty="700")
    _예약(conn, "RSV-1", "300")
    _할당(conn, "RSV-1", "LOT-A", "300")
    outbound.ship_allocated_stock(conn, reservation_id="RSV-1", shipped_at=AS_OF)

    assert _품목가용(_스냅샷(conn)) == Decimal(400) == _가용(conn)


def test_06_놓아준_예약은_품목_가용으로_돌아온다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="700")
    _예약(conn, "RSV-1", "300")
    assert _품목가용(_스냅샷(conn)) == Decimal(400)

    outbound.release_reservation(conn, reservation_id="RSV-1")

    assert _품목가용(_스냅샷(conn)) == Decimal(700) == _가용(conn)


def test_07_폐기대기_Lot_은_양쪽에서_함께_빠진다(conn: psycopg.Connection) -> None:
    """★ 신선도 규칙이 두 축에서 같은 답을 낸다 — Legacy 식 하나를 공유한다."""
    후보날 = AS_OF + timedelta(days=LIMIT_DAYS)
    _lot(conn, qty="700")

    assert _품목가용(_스냅샷(conn, as_of=후보날)) == Decimal(0)
    assert _가용(conn, as_of=후보날) == Decimal(0)


# ── 2. 3상태 계약 보존 ──────────────────────────────────────────────────


def test_08_예약을_못_읽으면_None_이다(conn: psycopg.Connection) -> None:
    """🔴 **`None`(미조회) 과 `[]`(0건 확인) 을 뭉개지 않는다.**

    못 읽은 축을 0 으로 놓으면 이미 팔린 재고를 다시 팔 수 있다고 답하게 된다.
    """
    _lot(conn, qty="700")
    스냅 = _스냅샷(conn)

    assert build_inventory_by_item(스냅.model_copy(update={"outbound_commitments": None})) is None
    assert build_inventory_by_item(스냅.model_copy(update={"outbound_commitments": []})) is not None


def test_09_0건_확인은_전량_가용이다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="700")

    스냅 = _스냅샷(conn)

    assert 스냅.outbound_commitments == [], "예약이 없으면 0건 확인이다"
    assert _품목가용(스냅) == Decimal(700)


def test_10_미할당_예약은_lot_id_가_None_이다(conn: psycopg.Connection) -> None:
    """★ `lot_id=None` 은 *"Lot 미지정"* 이지 *"없는 Lot"* 이 아니다."""
    _lot(conn, qty="700")
    _예약(conn, "RSV-1", "300")
    _할당(conn, "RSV-1", "LOT-A", "100")

    잡힌것 = repository.get_outbound_commitments(sim_run_id=SIM_RUN_ID)

    assert sorted((c.lot_id or "", c.quantity_kg) for c in 잡힌것) == [
        ("", Decimal(200)),
        ("LOT-A", Decimal(100)),
    ]
    assert all(c.item == ITEM_NAME for c in 잡힌것)


def test_11_어휘를_outbound_와_한_곳에서_가져온다() -> None:
    """🔴 글자가 갈리면 두 축이 조용히 다른 답을 낸다."""
    본문 = _코드만(Path(repository.__file__).read_text(encoding="utf-8"))

    assert "_HOLDING_ALLOCATION" in 본문
    assert "_ASSIGNED_ALLOCATION" in 본문
    assert "_HOLDING_RESERVATION" in 본문
    # ★ 같은 객체여야 한다 — 복사본이면 한쪽만 고쳐지는 날이 온다.
    assert repository._HOLDING_ALLOCATION is outbound._HOLDING_ALLOCATION
    assert repository._ASSIGNED_ALLOCATION is outbound._ASSIGNED_ALLOCATION


# ── 3. 읽기 요청은 쓰기를 만들지 않는다 ─────────────────────────────────


def _쓰기흔적(conn: psycopg.Connection) -> tuple[int, ...]:
    with conn.cursor() as cur:
        수 = []
        for 표 in (
            "inventory_moves",
            "inventory_reservations",
            "inventory_allocations",
            "pallets",
            "pallet_events",
        ):
            cur.execute(f"SELECT count(*) AS n FROM {TMP_SCHEMA}.{표}")
            수.append(cur.fetchall()[0]["n"])
    return tuple(수)


def test_12_조회_경로가_쓰기를_만들지_않는다(conn: psycopg.Connection) -> None:
    """🔴 조회 요청에서 side-effect 가 생기면 안 된다."""
    _lot(conn, qty="700")
    _예약(conn, "RSV-1", "300")
    _할당(conn, "RSV-1", "LOT-A", "100")
    이전 = _쓰기흔적(conn)

    스냅 = _스냅샷(conn)
    build_inventory_by_item(스냅)
    repository.get_outbound_commitments(sim_run_id=SIM_RUN_ID)
    turnover.load_lot_turnover(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)
    outbound.recommend_fefo_candidates(conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF)
    warehouse.get_zone_capacity(conn, zone_id=COLD_ZONE)
    warehouse.get_lot_position(conn, sim_run_id=SIM_RUN_ID, lot_id="LOT-A")
    transport.plan_fixed_route_transport(conn, shipment_qty_kg=Decimal(500))

    assert _쓰기흔적(conn) == 이전


def test_13_읽기_함수에_쓰기_SQL_이_없다() -> None:
    """★ 실행 경로뿐 아니라 **소스**로도 못박는다."""
    본문 = _코드만(Path(repository.__file__).read_text(encoding="utf-8"))

    for 금지 in ("INSERT INTO", "UPDATE ", "DELETE FROM", ".commit()", ".rollback()"):
        assert 금지 not in 본문, f"Repository 가 쓰기를 한다: {금지}"


def test_14_자동_폐기_자동_Pallet_경로가_없다() -> None:
    """🔴 `freshness <= 0` 이나 회전초과가 **스스로** 폐기·배치를 부르지 않는다."""
    for 모듈 in (repository, turnover):
        본문 = _코드만(Path(모듈.__file__).read_text(encoding="utf-8"))
        for 금지 in ("confirm_disposal", "place_lot_on_pallet", "empty_pallet", "move_pallet"):
            assert 금지 not in 본문, f"{Path(모듈.__file__).name} 이 자동으로 부른다: {금지}"


# ── 4. as_of 는 caller 것 하나다 ────────────────────────────────────────


def test_15_시계를_몰래_읽지_않는다() -> None:
    """🔴 내부에서 오늘을 읽으면 같은 `as_of` 인데 답이 달라진다."""
    for 이름 in (
        "repository.py",
        "tools.py",
        "turnover.py",
        "outbound.py",
        "disposal.py",
        "warehouse.py",
        "transport.py",
    ):
        본문 = _코드만((Path(repository.__file__).parent / 이름).read_text(encoding="utf-8"))
        for 금지 in ("date.today()", "datetime.now(", "utcnow()", "CURRENT_DATE"):
            assert 금지 not in 본문, f"{이름} 이 시계를 읽는다: {금지}"


def test_16_같은_as_of_면_같은_답이다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="700")

    assert _품목가용(_스냅샷(conn)) == _품목가용(_스냅샷(conn))
    assert _가용(conn) == _가용(conn)


def test_17_as_of_가_다르면_신선도_판정이_다르다(conn: psycopg.Connection) -> None:
    """★ 날짜 축이 실제로 흐르는지 확인한다 — 고정값이 아니다."""
    _lot(conn, qty="700")
    후보날 = AS_OF + timedelta(days=LIMIT_DAYS)

    assert (
        turnover.load_lot_turnover(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)[0].disposal_candidate
        is False
    )
    assert (
        turnover.load_lot_turnover(conn, sim_run_id=SIM_RUN_ID, as_of=후보날)[0].disposal_candidate
        is True
    )


# ── 5. 명시적 API 를 순서대로 부르는 내부 시나리오 ──────────────────────


def test_18_전체_시나리오_한_트랜잭션(conn: psycopg.Connection) -> None:
    """★ 하나의 자동 workflow 가 아니라 **명시적 호출 12번**이다.

    ```text
    Lot → 원장 IN → Snapshot → 예약 → FEFO → 할당 → 원장 OUT
        → 회전 → 폐기대기 → 폐기확정 → Pallet → empty → 운송견적
    ```
    """
    # ① 재고가 선다 (입고 파이프라인의 끝 상태를 그대로 만든다).
    _lot(conn, qty="700")
    assert _품목가용(_스냅샷(conn)) == Decimal(700)

    # ② Pallet 을 자리에 앉힌다 — 재고는 안 움직인다.
    warehouse.place_lot_on_pallet(
        conn,
        pallet_id="PLT-1",
        sim_run_id=SIM_RUN_ID,
        lot_id="LOT-A",
        location_id=f"{COLD_ZONE}-P1",
        occurred_at=DECIDED_AT,
        recorded_by=BY,
    )
    assert _품목가용(_스냅샷(conn)) == Decimal(700), "배치는 수량이 아니다"
    assert warehouse.get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1

    # ③ 예약 → FEFO 추천 → 할당.
    _예약(conn, "RSV-1", "300")
    assert _품목가용(_스냅샷(conn)) == Decimal(400)
    후보 = outbound.recommend_fefo_candidates(
        conn, sim_run_id=SIM_RUN_ID, item_id=ITEM_ID, as_of=AS_OF
    )
    assert [c.lot_id for c in 후보] == ["LOT-A"]
    _할당(conn, "RSV-1", "LOT-A", "300")
    assert _품목가용(_스냅샷(conn)) == Decimal(400) == _가용(conn)

    # ④ 실출고 — 여기서 처음 잔량이 준다.
    출고 = outbound.ship_allocated_stock(conn, reservation_id="RSV-1", shipped_at=AS_OF)
    assert 출고.shipped_qty_kg == Decimal(300)
    assert _품목가용(_스냅샷(conn)) == Decimal(400)

    # ⑤ 운송 견적 — 재고를 안 건드린다.
    이전 = _쓰기흔적(conn)
    계획 = transport.plan_fixed_route_transport(conn, shipment_qty_kg=Decimal(300))
    assert (계획.vehicle_class, 계획.trip_count) == ("1t", 1)
    assert 계획.standard_minutes is None
    assert _쓰기흔적(conn) == 이전

    # ⑥ 시간이 흘러 폐기대기가 된다 — 자동으로 아무 일도 안 일어난다.
    후보날 = AS_OF + timedelta(days=LIMIT_DAYS)
    회전 = turnover.load_lot_turnover(conn, sim_run_id=SIM_RUN_ID, as_of=후보날, lot_id="LOT-A")[0]
    assert (회전.turnover_status, 회전.disposal_candidate) == ("STORAGE_TARGET_EXCEEDED", True)
    assert _품목가용(_스냅샷(conn, as_of=후보날)) == Decimal(0), "판매 가용에서만 빠진다"
    assert warehouse.get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1

    # ⑦ 사람이 폐기를 확정해야 재고가 준다.
    disposal.confirm_disposal(
        conn,
        disposal_id="DSP-1",
        sim_run_id=SIM_RUN_ID,
        lot_id="LOT-A",
        quantity_kg=Decimal(400),
        disposed_at=후보날,
        reason_code="QUALITY_UNSELLABLE",
        as_of=후보날,
    )
    assert warehouse.get_zone_capacity(conn, zone_id=COLD_ZONE).occupied_positions == 1, (
        "폐기가 Pallet 을 자동으로 치우지 않는다"
    )

    # ⑧ 사람이 Pallet 을 치워야 자리가 돌아온다.
    warehouse.empty_pallet(conn, pallet_id="PLT-1", occurred_at=DECIDED_AT, recorded_by=BY)
    assert warehouse.get_zone_capacity(conn, zone_id=COLD_ZONE).free_positions == 2


def test_19_Snapshot_이_원장_사실을_그대로_읽는다(conn: psycopg.Connection) -> None:
    """★ Snapshot 이 fixture 목록이 아니라 **살아 있는 Lot 표**를 본다."""
    _lot(conn, qty="700")
    ledger.record_inventory_move(
        conn,
        move_id="MOVE-OUT-1",
        sim_run_id=SIM_RUN_ID,
        lot_id="LOT-A",
        move_type="OUT",
        quantity_kg=Decimal(200),
        moved_at=AS_OF,
        reason_code="SALE_FULFILLMENT",
    )

    assert _품목가용(_스냅샷(conn)) == Decimal(500)


def test_20_OutboundCommitment_는_0_이하를_담지_않는다() -> None:
    """★ 잡힌 몫이 0 이면 줄이 없어야 한다 — 0 짜리 줄은 사실이 아니라 잡음이다."""
    with pytest.raises(ValueError, match="greater_than|greater than"):
        OutboundCommitment(item=ITEM_NAME, lot_id="LOT-A", quantity_kg=Decimal(0))
