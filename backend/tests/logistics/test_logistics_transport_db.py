"""고정 Route 운송 계산 (3-E2). 실제 PostgreSQL 한 트랜잭션.

```text
logistics_contracts → vehicle_specs → vehicle_rate_table → TransportPlan
```

끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에 아무것도 남지 않는다.

🔴 **가짜로는 못 재는 것들을 잰다.**

```text
운임이 정말 표에서 오는가 (코드에 박힌 숫자가 아닌가)
거리구간 경계가 "초과 ~ 이하" 인가
계약이 0/1/2+ 일 때 셋 다 다르게 구는가
계산이 재고·원장·할당을 건드리지 않는가
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

from app.logistics import ledger, outbound, transport, turnover
from app.logistics.db import get_connection
from app.logistics.transport import (
    AmbiguousRate,
    AmbiguousRoute,
    InvalidTransportRequest,
    RateNotFound,
    RouteNotFound,
    load_vehicle_specs,
    plan_fixed_route_transport,
    resolve_fixed_route,
    select_vehicle,
    trip_count_for,
)

pytestmark = pytest.mark.db

TMP_SCHEMA = "transport_verify"
SIM_RUN_ID = "SIM-TRANSPORT-TEST"
ITEM_ID = "ITEM-BAECHU"
SALE_ID = "SALE-TEST-1"
LEGACY_ZONE = "COLD_HUMID_0_3"
AS_OF = date(2026, 1, 20)
DECIDED_AT = datetime(2026, 1, 20, 9, 0, tzinfo=UTC)

CONTRACT = "LOGI-BASE-5PL"
PERSONA = "PERSONA-V1.3"
# ★ 실 DB 실측값 그대로다. 코드에도 테스트에도 새 숫자를 지어내지 않았다.
DISTANCE = Decimal("30.000")
CONTRACT_FEE = Decimal("130000.000000")
CONTRACT_VEHICLE = "2.5t 냉장/냉동"  # 🔴 vehicle_specs 에 **없는** 문자열이다
BODY = "REEFER"
운영적재 = {"1t": Decimal(800), "1.4t": Decimal(1200), "2.5t": Decimal(2000)}
# (26, 36] 구간 운임 — 거리 30km 가 여기 든다.
RATE_2636 = {"1t": Decimal("100000.00"), "1.4t": Decimal("120000.00"), "2.5t": Decimal("140000.00")}

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
        for module in (transport, turnover, outbound, ledger):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _씨앗(cur: psycopg.Cursor) -> None:
    """실 DB 실측값을 그대로 옮긴다 — 차량 3종 · 운임 구간 4개 · 계약 1건."""
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (SIM_RUN_ID,))
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES ('PI-TEST')")
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.sales VALUES (%s)", (SALE_ID,))
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.company_personas VALUES (%s)", (PERSONA,))
    cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, '배추')", (ITEM_ID,))
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
        " (item_id, storage_zone, operational_limit_days, operational_policy_status)"
        " VALUES (%s, %s, 10, 'PROVISIONAL')",
        (ITEM_ID, LEGACY_ZONE),
    )
    차량 = (
        ("1t", Decimal(1000), 운영적재["1t"], 2),
        ("1.4t", Decimal(1400), 운영적재["1.4t"], 2),
        ("2.5t", Decimal(2500), 운영적재["2.5t"], 3),
    )
    for 등급, 최대, 운영, 단수 in 차량:
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.vehicle_specs"
            " (vehicle_class, body_type, max_payload_kg, operational_payload_kg,"
            "  max_pallet_floor_count, source_ref, evidence_grade)"
            " VALUES (%s, %s, %s, %s, %s, 'TEST', 'ASSUMED')",
            (등급, BODY, 최대, 운영, 단수),
        )
    구간 = ((0, 11), (11, 26), (26, 36), (36, 46))
    운임 = {
        "1t": (Decimal(80000), Decimal(90000), RATE_2636["1t"], Decimal(105000)),
        "1.4t": (Decimal(100000), Decimal(110000), RATE_2636["1.4t"], Decimal(125000)),
        "2.5t": (Decimal(120000), Decimal(130000), RATE_2636["2.5t"], Decimal(145000)),
    }
    for 등급, 값들 in 운임.items():
        for (부터, 까지), 금액 in zip(구간, 값들, strict=True):
            _운임(cur, 등급, 부터=부터, 까지=까지, 금액=금액)
    _계약(cur, CONTRACT, 거리=DISTANCE)


def _운임(
    cur: psycopg.Cursor,
    등급: str,
    *,
    부터: int,
    까지: int,
    금액: Decimal,
    body: str = BODY,
    active: bool = True,
    rate_id: str | None = None,
) -> str:
    rate_id = rate_id or f"RATE-{등급}-{부터}{까지}-{body}"
    cur.execute(
        f"INSERT INTO {TMP_SCHEMA}.vehicle_rate_table"
        " (rate_id, vehicle_class, body_type, distance_from_km, distance_to_km,"
        "  base_rate_krw, rate_type, evidence_grade, source_ref, is_active)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'SIMULATION_BASELINE', 'ASSUMED', 'TEST', %s)",
        (rate_id, 등급, body, 부터, 까지, 금액, active),
    )
    return rate_id


def _계약(cur: psycopg.Cursor, contract_id: str, *, 거리: Decimal | None) -> None:
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
                      54000, 5000, %s, %s, %s, 0.08, 432000, 360000, 1950000, 219360,
                      2961360, 0.2, 0.1, 'BASELINE_ONLY', TRUE)""",
        (contract_id, PERSONA, CONTRACT_VEHICLE, 거리, CONTRACT_FEE),
    )


def _lot(conn: psycopg.Connection, lot_id: str = "LOT-A", *, qty: str = "1000") -> str:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status
                ) VALUES (%s, %s, 'PI-TEST', %s, %s, %s, %s, 1000, %s, 'ACTIVE')""",
            (lot_id, SIM_RUN_ID, ITEM_ID, AS_OF, Decimal(qty), Decimal(qty), LEGACY_ZONE),
        )
    return lot_id


def _계획(conn: psycopg.Connection, qty: str, **kw: object) -> transport.TransportPlan:
    return plan_fixed_route_transport(
        conn,
        shipment_qty_kg=Decimal(qty),
        **kw,  # type: ignore[arg-type]
    )


# ── 1~3 · Route 조회 ────────────────────────────────────────────────────


def test_01_고정_Route_를_정확히_한_건_찾는다(conn: psycopg.Connection) -> None:
    route = resolve_fixed_route(conn)

    assert route.logistics_contract_id == CONTRACT
    assert route.distance_km == DISTANCE
    assert route.contract_baseline_cost_krw == CONTRACT_FEE
    assert route.contract_status == "BASELINE_ONLY"
    assert route.provisional is True


def test_02_계약이_없으면_RouteNotFound(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.logistics_contracts")

    with pytest.raises(RouteNotFound, match="고정 Route 계약이 없다"):
        resolve_fixed_route(conn)


def test_03_계약이_둘_이상이면_AmbiguousRoute(conn: psycopg.Connection) -> None:
    """🔴 자동으로 하나를 고르지 않는다."""
    with conn.cursor() as cur:
        _계약(cur, "LOGI-SECOND", 거리=Decimal("12.000"))

    with pytest.raises(AmbiguousRoute, match="둘 이상"):
        resolve_fixed_route(conn)


def test_04_계약_ID_를_주면_모호하지_않다(conn: psycopg.Connection) -> None:
    """★ 어느 계약을 쓸지는 **호출자가 정한다.**"""
    with conn.cursor() as cur:
        _계약(cur, "LOGI-SECOND", 거리=Decimal("12.000"))

    route = resolve_fixed_route(conn, logistics_contract_id="LOGI-SECOND")

    assert route.distance_km == Decimal("12.000")


def test_05_모르는_계약_ID_는_RouteNotFound(conn: psycopg.Connection) -> None:
    with pytest.raises(RouteNotFound):
        resolve_fixed_route(conn, logistics_contract_id="LOGI-없음")


def test_06_거리가_비면_0_으로_보정하지_않는다(conn: psycopg.Connection) -> None:
    """🔴 0 으로 채우면 가장 싼 구간이 조용히 잡힌다."""
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TMP_SCHEMA}.logistics_contracts SET delivery_distance_km = NULL")

    with pytest.raises(RouteNotFound, match="거리가 없다"):
        resolve_fixed_route(conn)


# ── 4~5 · 차량 선택 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("수량", "기대차량", "기대trip"),
    [
        ("1", "1t", 1),
        ("800", "1t", 1),  # 🔴 경계는 "이하" 다
        ("801", "1.4t", 1),
        ("1200", "1.4t", 1),
        ("1201", "2.5t", 1),
        ("2000", "2.5t", 1),
    ],
)
def test_07_실을_수_있는_가장_작은_차량(
    conn: psycopg.Connection, 수량: str, 기대차량: str, 기대trip: int
) -> None:
    """🔴 한 대로 되면 큰 차를 부르지 않는다 — 과대 배차는 그대로 비용이다."""
    spec, trips = select_vehicle(load_vehicle_specs(conn), shipment_qty_kg=Decimal(수량))

    assert (spec.vehicle_class, trips) == (기대차량, 기대trip)


def test_08_운영_Payload_로_고른다(conn: psycopg.Connection) -> None:
    """⚠️ `max_payload_kg`(1000)로 골랐다면 900kg 이 1t 에 실렸을 것이다."""
    spec, _ = select_vehicle(load_vehicle_specs(conn), shipment_qty_kg=Decimal(900))

    assert spec.vehicle_class == "1.4t"
    assert spec.operational_payload_kg == 운영적재["1.4t"]
    assert spec.max_payload_kg == Decimal(1400)


@pytest.mark.parametrize(
    ("수량", "기대trip"),
    [("2001", 2), ("4000", 2), ("4001", 3), ("6000", 3), ("12000", 6)],
)
def test_09_가장_큰_차보다_크면_나눠_싣는다(
    conn: psycopg.Connection, 수량: str, 기대trip: int
) -> None:
    """★ 가장 큰 차(운영 2000kg) × ceil(qty / 2000)."""
    spec, trips = select_vehicle(load_vehicle_specs(conn), shipment_qty_kg=Decimal(수량))

    assert (spec.vehicle_class, trips) == ("2.5t", 기대trip)


@pytest.mark.parametrize(
    ("수량", "적재", "기대"), [("1", "2000", 1), ("2000", "2000", 1), ("2001", "2000", 2)]
)
def test_10_trip_수는_올림이다(수량: str, 적재: str, 기대: int) -> None:
    assert trip_count_for(shipment_qty_kg=Decimal(수량), payload_kg=Decimal(적재)) == 기대


def test_11_차량_제원이_없으면_계획을_안_세운다(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.vehicle_rate_table")
        cur.execute(f"DELETE FROM {TMP_SCHEMA}.vehicle_specs")

    with pytest.raises(RouteNotFound, match="차량 제원이 없다"):
        _계획(conn, "100")


# ── 6~8 · 운임 ──────────────────────────────────────────────────────────


def test_12_운임은_구간표에서_온다(conn: psycopg.Connection) -> None:
    """🔴 거리 30km · 2.5t → (26,36] 구간. 계약의 130,000 이 아니다."""
    계획 = _계획(conn, "2000")

    assert 계획.vehicle_class == "2.5t"
    assert 계획.fixed_fee_per_trip_krw == RATE_2636["2.5t"]
    assert 계획.trip_count == 1
    assert 계획.estimated_cost_krw == RATE_2636["2.5t"]


def test_13_비용은_회당_운임_곱하기_trip_수다(conn: psycopg.Connection) -> None:
    계획 = _계획(conn, "12000")

    assert 계획.trip_count == 6
    assert 계획.estimated_cost_krw == RATE_2636["2.5t"] * 6


def test_14_차량이_바뀌면_운임도_바뀐다(conn: psycopg.Connection) -> None:
    """★ 운임은 차량 등급마다 다르다 — 그래서 과대 배차가 비용이다."""
    작은 = _계획(conn, "800")
    큰 = _계획(conn, "2000")

    assert 작은.fixed_fee_per_trip_krw == RATE_2636["1t"]
    assert 큰.fixed_fee_per_trip_krw == RATE_2636["2.5t"]
    assert 작은.estimated_cost_krw < 큰.estimated_cost_krw


@pytest.mark.parametrize(
    ("거리", "기대"), [("26.000", "130000.00"), ("26.001", "140000.00"), ("36.000", "140000.00")]
)
def test_15_구간_경계는_초과부터_이하까지다(conn: psycopg.Connection, 거리: str, 기대: str) -> None:
    """🔴 **정확한 경계.** DDL 주석: *"(0,11] = 문서의 ~11km"*.

    양쪽을 이하로 잡으면 26km 에서 두 구간이 겹친다.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.logistics_contracts SET delivery_distance_km = %s", (거리,)
        )

    assert _계획(conn, "2000").fixed_fee_per_trip_krw == Decimal(기대)


def test_16_구간_밖_거리는_RateNotFound(conn: psycopg.Connection) -> None:
    """🔴 가장 가까운 구간으로 대체하지 않는다 — 값을 지어내는 것과 같다."""
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TMP_SCHEMA}.logistics_contracts SET delivery_distance_km = 999")

    with pytest.raises(RateNotFound, match="운임 구간이 없다"):
        _계획(conn, "2000")


def test_17_내린_운임표는_안_본다(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TMP_SCHEMA}.vehicle_rate_table SET is_active = FALSE"
            " WHERE vehicle_class = '2.5t' AND distance_from_km = 26"
        )

    with pytest.raises(RateNotFound):
        _계획(conn, "2000")


def test_18_구간이_겹치면_AmbiguousRate(conn: psycopg.Connection) -> None:
    """🔴 싼 쪽·비싼 쪽을 임의로 고르지 않는다."""
    with conn.cursor() as cur:
        _운임(cur, "2.5t", 부터=25, 까지=35, 금액=Decimal(999), rate_id="RATE-겹침")

    with pytest.raises(AmbiguousRate, match="겹친다"):
        _계획(conn, "2000")


# ── 9~11 · 지어내지 않는다 ──────────────────────────────────────────────


def test_19_소요시간의_정본이_없어_None_이다(conn: psycopg.Connection) -> None:
    """⚠️ 스키마 어디에도 소요시간 칸이 없다. 거리÷속도로 지어내지 않는다."""
    assert _계획(conn, "2000").standard_minutes is None


def test_20_거리도_단가도_시간도_코드에_박혀_있지_않다() -> None:
    """🔴 숫자를 코드에 박으면 표를 고쳐도 견적이 안 바뀐다."""
    본문 = _코드만(Path(transport.__file__).read_text(encoding="utf-8"))

    숫자들 = set(re.findall(r"\b\d{3,}\b", 본문))
    assert 숫자들 <= {"20260905"}, f"코드에 박힌 숫자가 있다: {sorted(숫자들)}"
    for 금지 in ("km_per_hour", "won_per_km", "average_speed", "toll", "fuel"):
        assert 금지 not in 본문, f"없는 모델을 만들었다: {금지}"


def test_21_지도_교통_GPS_를_부르지_않는다() -> None:
    본문 = Path(transport.__file__).read_text(encoding="utf-8")
    실행부 = _코드만(본문)

    for 금지 in ("requests", "httpx", "urllib", "google", "kakao", "naver", "aiohttp"):
        assert 금지 not in 실행부.lower(), f"외부 경로 API 를 부른다: {금지}"


def test_22_계약의_차량_문자열을_번역하지_않는다(conn: psycopg.Connection) -> None:
    """🔴 **실측된 어휘 드리프트.**

    ```text
    logistics_contracts.vehicle_class  '2.5t 냉장/냉동'
    vehicle_specs.vehicle_class        '2.5t'
    ```

    FK 도 매핑 표도 없다. 계약값은 날것 그대로 돌려주고 실을 차량은 수량으로 고른다.
    """
    계획 = _계획(conn, "100")

    assert 계획.contract_vehicle_class == CONTRACT_VEHICLE
    assert 계획.vehicle_class == "1t", "계약 문자열이 차량 선택에 새지 않는다"
    assert 계획.contract_vehicle_class != 계획.vehicle_class


def test_23_계약_baseline_운임을_함께_돌려준다(conn: psycopg.Connection) -> None:
    """⚠️ 계약 130,000 ≠ 운임표 140,000 (실측 불일치). 조용히 맞추지 않는다."""
    계획 = _계획(conn, "2000")

    assert 계획.contract_baseline_cost_krw == CONTRACT_FEE
    assert 계획.fixed_fee_per_trip_krw == RATE_2636["2.5t"]
    assert 계획.contract_baseline_cost_krw != 계획.fixed_fee_per_trip_krw


# ── 12~15 · 운송과 재고를 분리한다 ──────────────────────────────────────


def test_24_계산만으로_원장이_생기지_않는다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="1000")

    _계획(conn, "1000")

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TMP_SCHEMA}.inventory_moves")
        assert cur.fetchall()[0]["n"] == 0


def test_25_계산만으로_잔량이_안_변한다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="1000")

    _계획(conn, "1000")

    with conn.cursor() as cur:
        cur.execute(f"SELECT remaining_qty_kg AS q FROM {TMP_SCHEMA}.inventory_lots")
        assert cur.fetchall()[0]["q"] == Decimal(1000)


def test_26_계산만으로_할당_상태가_안_변한다(conn: psycopg.Connection) -> None:
    _lot(conn, qty="1000")
    outbound.reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(500),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )
    outbound.allocate_stock(
        conn,
        reservation_id="RSV-1",
        requests=[outbound.AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(500))],
        decided_by="WH-1",
        decided_at=DECIDED_AT,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )

    _계획(conn, "500")

    with conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {TMP_SCHEMA}.inventory_allocations")
        assert [행["status"] for 행 in cur.fetchall()] == ["ALLOCATED"]


def test_27_운송_코드가_쓰기를_하지_않는다() -> None:
    """★ 실행 경로뿐 아니라 **소스**로도 못박는다."""
    본문 = _코드만(Path(transport.__file__).read_text(encoding="utf-8"))

    for 금지 in ("INSERT INTO", "UPDATE ", "DELETE FROM", ".commit()", ".rollback()"):
        assert 금지 not in 본문, f"운송이 쓰기를 한다: {금지}"


def test_28_Shipment_표를_새로_만들지_않는다() -> None:
    """🔴 실출고 사실은 여전히 *할당 SHIPPED + 원장 OUT* 이다."""
    본문 = _코드만(Path(transport.__file__).read_text(encoding="utf-8"))

    assert "CREATE TABLE" not in 본문
    assert "deliveries" not in 본문, "판매 쪽 표에 물류가 줄을 만들지 않는다"


def test_29_결정론이다(conn: psycopg.Connection) -> None:
    """★ 같은 입력·같은 표면 같은 답. 시계도 난수도 안 쓴다."""
    첫번 = _계획(conn, "3500")
    두번 = _계획(conn, "3500")

    assert 첫번 == 두번
    본문 = _코드만(Path(transport.__file__).read_text(encoding="utf-8"))
    for 금지 in ("random", "now()", "datetime.now", "uuid"):
        assert 금지 not in 본문, f"결정론을 깬다: {금지}"


def test_30_float_수량을_거부한다(conn: psycopg.Connection) -> None:
    with pytest.raises(InvalidTransportRequest, match="Decimal 이어야"):
        plan_fixed_route_transport(conn, shipment_qty_kg=1000.0)  # type: ignore[arg-type]


def test_31_0_이하_수량을_거부한다(conn: psycopg.Connection) -> None:
    with pytest.raises(InvalidTransportRequest, match="0보다 커야"):
        _계획(conn, "0")


def test_32_전체_시나리오_한_트랜잭션(conn: psycopg.Connection) -> None:
    """★ 출고 확정 → 운송계획. 재고는 출고에서만 움직인다."""
    _lot(conn, qty="5000")
    outbound.reserve_stock(
        conn,
        reservation_id="RSV-1",
        sim_run_id=SIM_RUN_ID,
        item_id=ITEM_ID,
        required_qty_kg=Decimal(4500),
        sale_id=SALE_ID,
        as_of=AS_OF,
    )
    outbound.allocate_stock(
        conn,
        reservation_id="RSV-1",
        requests=[outbound.AllocationRequest(lot_id="LOT-A", quantity_kg=Decimal(4500))],
        decided_by="WH-1",
        decided_at=DECIDED_AT,
        allocation_basis="HUMAN_OVERRIDE",
        as_of=AS_OF,
    )
    출고 = outbound.ship_allocated_stock(conn, reservation_id="RSV-1", shipped_at=AS_OF)
    assert 출고.shipped_qty_kg == Decimal(4500)

    계획 = _계획(conn, "4500")

    assert 계획.vehicle_class == "2.5t"
    assert 계획.trip_count == 3, "운영 2000kg × 3 회"
    assert 계획.estimated_cost_krw == RATE_2636["2.5t"] * 3
    assert 계획.standard_minutes is None
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TMP_SCHEMA}.inventory_moves")
        assert cur.fetchall()[0]["n"] == 1, "운송이 원장을 더 만들지 않았다"
