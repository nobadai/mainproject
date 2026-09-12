"""공용 Read-only Tool 8개 — **실제 PostgreSQL 한 트랜잭션** (#628 Commit 3).

```text
look-ahead   D5 조회에 D8 의 사실이 섞이지 않는가          ← 이 파일의 존재 이유
실행 격리    남의 실행 Lot·문제가 이 조사에 들어오지 않는가
숫자 정합    Tool 값 == 기존 Production 함수 값
읽기 전용    이 층이 실제로 보낸 문장이 전부 SELECT 인가    ← 소스 검사로는 못 잰다
관측일       observed_as_of > as_of 가 나오지 않는가
```

🔴 **가짜로는 못 재는 것들을 잰다.** 과거 재현은 원장·예약·일정 표가 실제로 있어야
   의미가 있다 — 스텁으로 재면 *"as_of 를 받았다"* 까지만 확인하고 **그 값을 실제로
   쓰는지**는 한 번도 안 보게 된다.

끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에 아무것도 남지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
import pytest

from app.logistics import historical_repository, inbound_schedules, turnover
from app.logistics import tools as calc
from app.logistics.agent import exceptions as exception_repo
from app.logistics.agent import tools as agent_tools
from app.logistics.agent.exceptions import open_exception, resolve_exception
from app.logistics.agent.schemas import (
    COMMITMENT_OBSERVED_AS_OF,
    FRESHNESS_PRESSURE,
    ExceptionEvidence,
    ExceptionRow,
)
from app.logistics.agent.tools import (
    ITEM_NOT_FOUND,
    LOT_NOT_FOUND,
    estimate_action_impact,
    get_capacity_context,
    get_inbound_schedule,
    get_item_lots,
    get_lot,
    get_open_exceptions,
    get_policy,
    get_sales_commitments,
)
from app.logistics.db import get_connection
from app.logistics.schemas import (
    POLICY_VERSION,
    InventoryLogisticsSnapshot,
    LogisticsPolicy,
    OutboundCommitment,
    ScheduledQuantity,
)
from app.logistics.tools import _commitment_axes, _sellable_lot_contributions

pytestmark = pytest.mark.db

TMP_SCHEMA = "logistics_agent_tools_verify"
SIM = "SIM-TOOLS"
OTHER_SIM = "SIM-TOOLS-OTHER"
BAECHU = "ITEM-BAECHU"
MU = "ITEM-MU"
ZONE = "COLD_HUMID_0_3"
LIMIT_DAYS = 10
PRIORITY_DAYS = 3
GUARANTEED = Decimal(2000)
UNIT_COST = Decimal(1200)

#: ```text
#: D1  입고 1,000kg
#: D5  출고   300kg     → 그날 잔량 700kg
#: D8  출고   200kg     → 그날 잔량 500kg
#: ```
D1 = date(2026, 1, 1)
D5 = date(2026, 1, 5)
D7 = date(2026, 1, 7)
D8 = date(2026, 1, 8)
D10 = date(2026, 1, 10)

DB_DIR = Path(__file__).resolve().parents[3] / "database"

STUBS = f"""
CREATE TABLE {TMP_SCHEMA}.items (item_id text PRIMARY KEY, item_name text);
CREATE TABLE {TMP_SCHEMA}.partners (partner_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sim_runs (sim_run_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.purchase_items (
    purchase_item_id text PRIMARY KEY, purchase_id text, item_id text);
CREATE TABLE {TMP_SCHEMA}.sales (sale_id text PRIMARY KEY, sale_date date);
CREATE TABLE {TMP_SCHEMA}.sale_items (sale_item_id text PRIMARY KEY);
"""


def _repo_block(table: str) -> str:
    text = (DB_DIR / "10_domain_schema.sql").read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE haetdeul\.{table}\s*\(.*?\n\);", text, re.DOTALL)
    assert match is not None, table
    parts = [match.group(0)]
    parts += re.findall(rf"ALTER TABLE ONLY haetdeul\.{table}\s+ADD CONSTRAINT [^;]+;", text)
    return "\n".join(parts)


def _file(name: str) -> str:
    text = (DB_DIR / name).read_text(encoding="utf-8")
    return re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", text)


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection]:
    connection = get_connection()
    connection.autocommit = False
    try:
        with connection.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {TMP_SCHEMA}")
            cur.execute(STUBS)
            for table in ("inventory_lots", "inventory_moves", "item_storage_policies"):
                cur.execute(_repo_block(table).replace("haetdeul.", f"{TMP_SCHEMA}."))
            for name in ("30_logistics_wms_schema.sql", "logistics_inventory_lots_nullable.sql"):
                cur.execute(_file(name).replace("haetdeul.", f"{TMP_SCHEMA}."))
            agent_ddl = _file("40_logistics_agent_schema.sql")
            cur.execute(agent_ddl.replace("haetdeul.", f"{TMP_SCHEMA}."))

            for run in (SIM, OTHER_SIM):
                cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (run,))
            for item_id, item_name in ((BAECHU, "배추"), (MU, "무")):
                cur.execute(
                    f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (item_id, item_name)
                )
                cur.execute(
                    f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES (%s, 'PO-1', %s)",
                    (f"PI-{item_id}", item_id),
                )
                cur.execute(
                    f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
                    " (item_id, storage_zone, operational_limit_days,"
                    " operational_policy_status) VALUES (%s, %s, %s, 'PROVISIONAL')",
                    (item_id, ZONE, LIMIT_DAYS),
                )
            # 🔴 회전 정책은 배추에만 — 정책 없는 품목이 조회에서 사라지지 않는 것이 계약이다.
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.item_turnover_policies"
                " (item_id, operational_turnover_target_days, sell_priority_remaining_days,"
                "  policy_status, evidence_grade, source_ref)"
                " VALUES (%s, 10, %s, 'SIMULATION_POLICY', 'SIM_FIXED', 'TEST')",
                (BAECHU, PRIORITY_DAYS),
            )
        for module in (turnover, historical_repository, exception_repo, inbound_schedules):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ── 준비 도우미 ─────────────────────────────────────────────────────────


class _Read(NamedTuple):
    """`get_capacity_context` 가 읽는 것은 `read.snapshot` 하나다.

    🔴 **`get_current_logistics_read` 를 그대로 못 쓴다** — 자기 커넥션을 새로 열어
       `logistics_runtime_fixture` 를 읽는데, 이 검사의 임시 스키마는 아직 커밋되지
       않은 트랜잭션 안이라 다른 커넥션에는 안 보인다.
    """

    snapshot: InventoryLogisticsSnapshot


def _lot_row(
    conn: psycopg.Connection,
    lot_id: str,
    *,
    item_id: str = BAECHU,
    qty: str,
    original: str | None = None,
    received: date,
    sim_run_id: str = SIM,
    moves: list[tuple[str, str, date]] | None = None,
) -> None:
    """Lot 한 줄 + **그 Lot 을 그 잔량으로 만든 원장 이동들.**

    🔴 이동 없는 Lot 을 만들지 않는다 — production 에서 Lot 은 `remaining_qty_kg = 0`
       으로 서고 잔량을 올리는 길이 원장 `IN` 하나뿐이다.
    """
    original_qty = Decimal(original if original is not None else qty)
    ledger_moves = moves if moves is not None else [("IN", qty, received)]
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE')""",
            (
                lot_id,
                sim_run_id,
                f"PI-{item_id}",
                item_id,
                received,
                original_qty,
                Decimal(qty),
                UNIT_COST,
                ZONE,
            ),
        )
        for index, (move_type, quantity, moved_at) in enumerate(ledger_moves, start=1):
            cur.execute(
                f"""INSERT INTO {TMP_SCHEMA}.inventory_moves (
                        move_id, sim_run_id, lot_id, move_type,
                        quantity_kg, moved_at, reason_code
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'TEST')""",
                (
                    f"MOVE-{lot_id}-{index}",
                    sim_run_id,
                    lot_id,
                    move_type,
                    Decimal(quantity),
                    moved_at,
                ),
            )


def _moved_lot(conn: psycopg.Connection, *, sim_run_id: str = SIM) -> None:
    """D1 입고 1,000 · D5 출고 300 · D8 출고 200 → 지금 잔량 500."""
    _lot_row(
        conn,
        "LOT-BAECHU",
        qty="500",
        original="1000",
        received=D1,
        sim_run_id=sim_run_id,
        moves=[("IN", "1000", D1), ("OUT", "300", D5), ("OUT", "200", D8)],
    )


def _reservation_row(
    conn: psycopg.Connection,
    *,
    reservation_id: str = "RSV-1",
    item_id: str = BAECHU,
    sale_id: str = "SALE-1",
    sale_date: date,
    required: str = "400",
    reserved: str = "400",
    due: date | None = None,
    released: date | None = None,
    sim_run_id: str = SIM,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TMP_SCHEMA}.sales VALUES (%s, %s)"
            " ON CONFLICT (sale_id) DO NOTHING",
            (sale_id, sale_date),
        )
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_reservations (
                    reservation_id, sim_run_id, item_id, sale_id, required_qty_kg,
                    reserved_qty_kg, due_date, status, released_as_of
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                reservation_id,
                sim_run_id,
                item_id,
                sale_id,
                Decimal(required),
                Decimal(reserved),
                due or sale_date,
                "RELEASED" if released else "ALLOCATED",
                released,
            ),
        )


def _allocation_row(
    conn: psycopg.Connection,
    *,
    allocation_id: str = "ALC-1",
    reservation_id: str = "RSV-1",
    lot_id: str = "LOT-BAECHU",
    qty: str = "400",
    decided_on: date,
) -> None:
    """할당 한 줄. `decided_at` 은 **시뮬레이션 시각**이다 (`phase_instant` 와 같은 축)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_allocations (
                    allocation_id, reservation_id, lot_id, allocated_qty_kg,
                    allocation_basis, decided_by, decided_at, status
                ) VALUES (%s, %s, %s, %s, 'FEFO_AUTO_SELECTED', 'TEST', %s, 'ALLOCATED')""",
            (
                allocation_id,
                reservation_id,
                lot_id,
                Decimal(qty),
                f"{decided_on.isoformat()} 09:34:00+09:00",
            ),
        )


def _schedule_row(
    conn: psycopg.Connection,
    *,
    inbound_id: str,
    item_id: str = BAECHU,
    qty: str = "600",
    eta: date,
    created: date,
    cancelled: date | None = None,
    sim_run_id: str = SIM,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inbound_schedules (
                    inbound_id, sim_run_id, purchase_item_id, quantity_kg,
                    expected_arrival_date, created_as_of, cancelled_as_of, source_ref
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'TEST')""",
            (inbound_id, sim_run_id, f"PI-{item_id}", Decimal(qty), eta, created, cancelled),
        )


def _open_exception_row(
    conn: psycopg.Connection,
    *,
    exception_id: str,
    opened: date,
    sim_run_id: str = SIM,
    subject_id: str = "LOT-BAECHU",
) -> None:
    open_exception(
        conn,
        row=ExceptionRow(
            exception_id=exception_id,
            sim_run_id=sim_run_id,
            code=FRESHNESS_PRESSURE,
            subject_type="LOT",
            subject_id=subject_id,
            severity="HIGH",
            status="OPEN",
            opened_as_of=opened,
            last_detected_as_of=opened,
            observed_as_of=None,
            evidence=(
                ExceptionEvidence(
                    fact="remaining_qty_kg",
                    value=Decimal(500),
                    unit="kg",
                    source="inventory_lots",
                    source_id=subject_id,
                ),
            ),
            detector_version="v1",
        ),
    )


def _snapshot(as_of: date, *, lots: list[Any] | None = None) -> InventoryLogisticsSnapshot:
    """정책 축만 들어 있는 스냅샷. 🔴 **점유 축은 Tool 이 원장으로 갈아 끼운다.**"""
    return InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=as_of,
        on_hand_by_lot=lots or [],
        in_transit=[],
        confirmed_inbound_schedule=[],
        confirmed_outbound_schedule=[],
        outbound_commitments=[],
        used_capacity_kg=Decimal(0),
        guaranteed_capacity_kg=GUARANTEED,
        burst_capacity_kg=GUARANTEED * 2,
        guaranteed_capacity_by_zone_kg=None,
        inbound_lead_days=1,
        capacity_tight_ratio=Decimal("0.90"),
        freshness_pressure_ratio=Decimal("0.30"),
        evidence_refs=["TEST"],
    )


def _read_fn(as_of: date):
    return lambda **_: _Read(snapshot=_snapshot(as_of))


def _policy_fn():
    return lambda: LogisticsPolicy(
        guaranteed_capacity_kg=GUARANTEED,
        burst_capacity_kg=GUARANTEED * 2,
        inbound_lead_days=1,
        daily_inbound_capacity_kg=Decimal(5000),
        inbound_transport_capacity_kg=Decimal(5000),
        shared_daily_outbound_capacity_kg=Decimal(5000),
        cap_by_date_policy="CONFIRMED_ONLY",
        capacity_tight_ratio=Decimal("0.90"),
        freshness_pressure_ratio=Decimal("0.30"),
        policy_version=POLICY_VERSION,
        usage_scope="AGENT_MVP_DEMO",
        source_refs={"guaranteed_capacity_kg": "TEST"},
    )


# ===========================================================================
# A. look-ahead — **`as_of` 가 장식이 아니다**
# ===========================================================================


def test_get_lot_uses_the_ledger_not_the_current_cache(conn: psycopg.Connection) -> None:
    """```text
    D1 IN 1,000 · D5 OUT 300 · D8 OUT 200 · 캐시 500
    get_lot(D5) → 700kg     🔴 캐시 500 을 내면 실패다
    get_lot(D8) → 500kg
    ```"""
    _moved_lot(conn)

    at_d5 = get_lot(conn, sim_run_id=SIM, as_of=D5, lot_id="LOT-BAECHU")
    at_d8 = get_lot(conn, sim_run_id=SIM, as_of=D8, lot_id="LOT-BAECHU")

    assert at_d5.lot is not None and at_d5.lot.remaining_qty_kg == Decimal(700)
    assert at_d8.lot is not None and at_d8.lot.remaining_qty_kg == Decimal(500)
    # 🔴 D5 답에 D8 의 사실이 섞이지 않았다.
    assert at_d5.lot.remaining_qty_observed_as_of == D5
    assert at_d8.lot.remaining_qty_observed_as_of == D8


def test_get_lot_is_empty_before_the_lot_was_received(conn: psycopg.Connection) -> None:
    """🔴 **0kg 으로 답하지 않는다** — 그날 그 Lot 은 존재하지 않았다."""
    _moved_lot(conn)

    result = get_lot(conn, sim_run_id=SIM, as_of=D1 - timedelta(days=1), lot_id="LOT-BAECHU")

    assert result.lot is None
    assert f"{LOT_NOT_FOUND}:LOT-BAECHU" in result.uncertainties


def test_another_runs_lot_is_never_visible(conn: psycopg.Connection) -> None:
    """축을 안 좁히면 남의 실행 창고가 이 조사에 들어온다.

    ⚠️ **같은 `lot_id` 를 두 실행에 둘 수는 없다** — `inventory_lots_pkey` 가
       `(lot_id)` 하나라 DB 가 먼저 막는다(실행 축이 PK 에 없다). 그래서 격리는
       *"남의 실행 Lot 이 안 보이는가"* 로 잰다.
    """
    _moved_lot(conn)
    _lot_row(conn, "LOT-OTHER", qty="999", received=D1, sim_run_id=OTHER_SIM)

    mine = get_lot(conn, sim_run_id=SIM, as_of=D8, lot_id="LOT-OTHER")
    theirs = get_lot(conn, sim_run_id=OTHER_SIM, as_of=D8, lot_id="LOT-OTHER")

    assert mine.lot is None, "남의 실행 Lot 이 이 실행 조회에 보인다"
    assert f"{LOT_NOT_FOUND}:LOT-OTHER" in mine.uncertainties
    assert theirs.lot is not None and theirs.lot.remaining_qty_kg == Decimal(999)


def test_get_item_lots_is_fefo_ordered_and_run_scoped(conn: psycopg.Connection) -> None:
    """다음에 나갈 것이 맨 앞이다 — 실제 자동 출고와 **같은 키**를 쓴다."""
    _lot_row(conn, "LOT-A", qty="100", received=D1)
    _lot_row(conn, "LOT-B", qty="100", received=D1 - timedelta(days=3))
    _lot_row(conn, "LOT-MU", item_id=MU, qty="100", received=D1)
    _lot_row(conn, "LOT-OTHER", qty="100", received=D1, sim_run_id=OTHER_SIM)

    result = get_item_lots(conn, sim_run_id=SIM, as_of=D5, item_id=BAECHU)

    # 신선도가 먼저 다하는 것(= 더 일찍 들어온 것)이 앞이다.
    assert [lot.lot_id for lot in result.lots] == ["LOT-B", "LOT-A"]
    assert all(lot.item_id == BAECHU for lot in result.lots)


def test_unknown_item_is_reported_as_not_found(conn: psycopg.Connection) -> None:
    result = get_item_lots(conn, sim_run_id=SIM, as_of=D5, item_id="ITEM-NONE")

    assert result.lots == ()
    assert f"{ITEM_NOT_FOUND}:ITEM-NONE" in result.uncertainties


def test_later_created_schedules_are_invisible_in_the_past(conn: psycopg.Connection) -> None:
    """`created_as_of` 가 그 일정이 **장부에 선 날**이다."""
    _schedule_row(conn, inbound_id="INB-EARLY", eta=D8, created=D1)
    _schedule_row(conn, inbound_id="INB-LATE", eta=D10 + timedelta(days=1), created=D10)

    at_d5 = get_inbound_schedule(conn, sim_run_id=SIM, as_of=D5)
    at_d10 = get_inbound_schedule(conn, sim_run_id=SIM, as_of=D10)

    assert [one.inbound_id for one in at_d5.schedules] == ["INB-EARLY"]
    assert {one.inbound_id for one in at_d10.schedules} == {"INB-EARLY", "INB-LATE"}


def test_cancelled_schedule_still_existed_before_the_cancel_date(
    conn: psycopg.Connection,
) -> None:
    """🔴 **취소를 과거로 역류시키지 않는다** — D7 에 취소했으면 D5 에는 살아 있었다."""
    _schedule_row(conn, inbound_id="INB-CANCELLED", eta=D10, created=D1, cancelled=D7)

    before = get_inbound_schedule(conn, sim_run_id=SIM, as_of=D5)
    after = get_inbound_schedule(conn, sim_run_id=SIM, as_of=D8)

    assert [one.inbound_id for one in before.schedules] == ["INB-CANCELLED"]
    assert after.schedules == ()


def test_reservation_is_invisible_before_its_sale_date(conn: psycopg.Connection) -> None:
    """존재 축은 `sales.sale_date` 다 — 미래 납품 예약이 과거 조회에 나오면 실패."""
    _moved_lot(conn)
    _reservation_row(conn, sale_date=D10, due=D10)
    _allocation_row(conn, decided_on=D10)

    at_d5 = get_sales_commitments(conn, sim_run_id=SIM, as_of=D5, item_id=BAECHU)
    at_d10 = get_sales_commitments(conn, sim_run_id=SIM, as_of=D10, item_id=BAECHU)

    assert at_d5.live_reservations == ()
    assert [one.reservation_id for one in at_d10.live_reservations] == ["RSV-1"]
    assert at_d10.next_due_date == D10


def test_released_reservation_drops_out_from_its_release_date(conn: psycopg.Connection) -> None:
    _moved_lot(conn)
    _reservation_row(conn, sale_date=D1, due=D10, released=D7)

    before = get_sales_commitments(conn, sim_run_id=SIM, as_of=D5, item_id=BAECHU)
    after = get_sales_commitments(conn, sim_run_id=SIM, as_of=D7, item_id=BAECHU)

    assert before.live_reservations != ()
    assert after.live_reservations == ()


def test_later_opened_exception_is_invisible_in_the_past(conn: psycopg.Connection) -> None:
    """🔴 `opened_as_of=D10` 인 Exception 이 `as_of=D5` 조회에 나오면 look-ahead 다."""
    _open_exception_row(conn, exception_id="EX-EARLY", opened=D1)
    _open_exception_row(conn, exception_id="EX-LATE", opened=D10, subject_id="LOT-MU")

    at_d5 = get_open_exceptions(conn, sim_run_id=SIM, as_of=D5)
    at_d10 = get_open_exceptions(conn, sim_run_id=SIM, as_of=D10)

    assert [one.exception_id for one in at_d5.exceptions] == ["EX-EARLY"]
    assert {one.exception_id for one in at_d10.exceptions} == {"EX-EARLY", "EX-LATE"}


def test_exception_closed_later_is_live_again_on_that_day(conn: psycopg.Connection) -> None:
    """🔴 **`live_exceptions`(지금 값)만 보면 이 행이 통째로 빠진다.** 그러면 조사가
    *"그날 아무 문제 없었다"* 고 답하게 되고, 그것이 더 위험한 거짓이다."""
    _open_exception_row(conn, exception_id="EX-CLOSED", opened=D1)
    resolve_exception(conn, exception_id="EX-CLOSED", as_of=D7, resolved_by="REDETECT")

    before = get_open_exceptions(conn, sim_run_id=SIM, as_of=D5)
    on_close_day = get_open_exceptions(conn, sim_run_id=SIM, as_of=D7)

    assert [one.exception_id for one in before.exceptions] == ["EX-CLOSED"]
    # 닫힌 날 당일부터는 살아 있지 않다.
    assert on_close_day.exceptions == ()


def test_another_runs_exception_is_never_visible(conn: psycopg.Connection) -> None:
    _open_exception_row(conn, exception_id="EX-MINE", opened=D1)
    _open_exception_row(conn, exception_id="EX-OTHER", opened=D1, sim_run_id=OTHER_SIM)

    result = get_open_exceptions(conn, sim_run_id=SIM, as_of=D5)

    assert [one.exception_id for one in result.exceptions] == ["EX-MINE"]


# ===========================================================================
# B. 숫자 정합 — **Tool 이 새 정본이 되지 않는다**
# ===========================================================================


def test_window_usage_matches_the_existing_calculator(conn: psycopg.Connection) -> None:
    """🔴 같은 창고를 두고 Exception 은 «빡빡하다» 인데 Tool 은 «여유 있다» 면 실패."""
    _moved_lot(conn)
    _lot_row(conn, "LOT-MU", item_id=MU, qty="1200", received=D1)

    result = get_capacity_context(conn, sim_run_id=SIM, as_of=D8, read_fn=_read_fn(D8))

    warehouse = agent_tools._warehouse_at(conn, sim_run_id=SIM, as_of=D8)
    snapshot = agent_tools._as_of_snapshot(
        _snapshot(D8), lots=warehouse.lots, used_kg=result.used_kg
    )
    assert result.window_usage_ratio == calc.calculate_window_capacity_usage(snapshot, D8)
    assert result.cap_by_date == calc.calculate_cap_by_date(
        snapshot, calc.build_cap_window(snapshot, D8)
    )


def test_occupancy_is_the_ledger_sum_not_the_cache_sum(conn: psycopg.Connection) -> None:
    """D5 에는 700kg 이 있었다 — 지금 캐시(500kg)로 답하면 없는 여유가 생긴다."""
    _moved_lot(conn)

    at_d5 = get_capacity_context(conn, sim_run_id=SIM, as_of=D5, read_fn=_read_fn(D5))
    at_d8 = get_capacity_context(conn, sim_run_id=SIM, as_of=D8, read_fn=_read_fn(D8))

    assert at_d5.used_kg == Decimal(700)
    assert at_d8.used_kg == Decimal(500)
    assert at_d5.available_kg == GUARANTEED - Decimal(700)


def test_uncommitted_matches_the_existing_sellable_calculation(
    conn: psycopg.Connection,
) -> None:
    """🔴 `tools._sellable_lot_contributions` 와 **같은 경계**여야 한다 — 다르면
    «팔 수 있다고 센 재고» 와 «조사가 말한 재고» 가 갈린다."""
    _moved_lot(conn)
    _reservation_row(conn, sale_date=D1, reserved="400")
    _allocation_row(conn, decided_on=D1, qty="400")

    result = get_lot(conn, sim_run_id=SIM, as_of=D8, lot_id="LOT-BAECHU")
    assert result.lot is not None

    # 기존 셈: 같은 잔량·같은 할당을 스냅샷 모양으로 넣어 본다.
    warehouse = agent_tools._warehouse_at(conn, sim_run_id=SIM, as_of=D8)
    snapshot = agent_tools._as_of_snapshot(
        _snapshot(D8), lots=warehouse.lots, used_kg=Decimal(500)
    ).model_copy(
        update={
            "outbound_commitments": [
                OutboundCommitment(item="배추", lot_id="LOT-BAECHU", quantity_kg=Decimal(400))
            ]
        }
    )
    axes = _commitment_axes(snapshot)
    assert axes is not None
    existing = {lot.lot_id: share for lot, share in _sellable_lot_contributions(snapshot, axes[0])}

    assert result.lot.committed_kg == Decimal(400)
    assert result.lot.uncommitted_kg == existing["LOT-BAECHU"] == Decimal(100)


def test_purchase_adjust_feasibility_matches_cap_by_date(conn: psycopg.Connection) -> None:
    """🔴 **새 용량 판정을 만들지 않는다** — `calculate_cap_by_date` 가 답한다."""
    _moved_lot(conn)

    capacity = get_capacity_context(conn, sim_run_id=SIM, as_of=D8, read_fn=_read_fn(D8))
    tightest_cap = min(capacity.cap_by_date.values())

    feasible = estimate_action_impact(
        conn,
        sim_run_id=SIM,
        as_of=D8,
        action="PURCHASE_ADJUST_REQUEST",
        parameters={"item_id": BAECHU, "qty_delta_kg": tightest_cap},
        read_fn=_read_fn(D8),
    )
    infeasible = estimate_action_impact(
        conn,
        sim_run_id=SIM,
        as_of=D8,
        action="PURCHASE_ADJUST_REQUEST",
        parameters={"item_id": BAECHU, "qty_delta_kg": tightest_cap + Decimal(1)},
        read_fn=_read_fn(D8),
    )

    assert feasible.feasibility == "FEASIBLE"
    assert infeasible.feasibility == "INFEASIBLE"
    assert feasible.capacity_delta_kg == -tightest_cap


def test_disposal_loss_uses_only_the_book_unit_cost(conn: psycopg.Connection) -> None:
    """🔴 판매 기회손실을 지어내지 않는다 — 물류 장부에 그 값이 없다."""
    _moved_lot(conn)

    result = estimate_action_impact(
        conn,
        sim_run_id=SIM,
        as_of=D8,
        action="DISPOSAL_REQUEST",
        parameters={"lot_id": "LOT-BAECHU", "qty_kg": "200"},
    )

    assert result.feasibility == "FEASIBLE"
    assert result.affected_kg == Decimal(200)
    assert result.capacity_delta_kg == Decimal(200)
    assert result.estimated_loss_krw == Decimal(200) * UNIT_COST


def test_cannot_dispose_more_than_the_remaining_quantity(conn: psycopg.Connection) -> None:
    _moved_lot(conn)

    result = estimate_action_impact(
        conn,
        sim_run_id=SIM,
        as_of=D8,
        action="DISPOSAL_REQUEST",
        parameters={"lot_id": "LOT-BAECHU", "qty_kg": "900"},
    )

    assert result.feasibility == "INFEASIBLE"
    assert result.affected_kg == Decimal(500)


def test_sales_priority_request_never_estimates_money(conn: psycopg.Connection) -> None:
    """🔴 **판매가는 물류 장부에 없다.** 0 으로 적으면 «손실 없음» 이라는 주장이 된다."""
    _moved_lot(conn)

    result = estimate_action_impact(
        conn,
        sim_run_id=SIM,
        as_of=D8,
        action="SALES_PRIORITY_REQUEST",
        parameters={"lot_id": "LOT-BAECHU"},
    )

    assert result.feasibility == "FEASIBLE"
    assert result.affected_kg == Decimal(500)
    assert result.estimated_loss_krw is None
    assert any("판매가" in one for one in result.assumptions)


def test_accept_risk_moves_nothing(conn: psycopg.Connection) -> None:
    """§10.1 기록형 행동 — 여기서 0 은 추측이 아니라 사실이다."""
    _moved_lot(conn)

    result = estimate_action_impact(
        conn, sim_run_id=SIM, as_of=D8, action="ACCEPT_RISK", parameters={"lot_id": "LOT-BAECHU"}
    )

    assert result.feasibility == "FEASIBLE"
    assert result.affected_kg == Decimal(0) and result.capacity_delta_kg == Decimal(0)


# ===========================================================================
# C. 관측일 (§18.2)
# ===========================================================================


def test_no_tool_reports_an_observed_at_in_the_future(conn: psycopg.Connection) -> None:
    """🔴 `observed_as_of > as_of` 는 정상 경로에서 나오면 안 된다. `None` 은 허용이다."""
    _moved_lot(conn)
    _reservation_row(conn, sale_date=D1)
    _allocation_row(conn, decided_on=D1)
    _schedule_row(conn, inbound_id="INB-1", eta=D10, created=D1)
    _open_exception_row(conn, exception_id="EX-1", opened=D1)

    answers = [
        get_open_exceptions(conn, sim_run_id=SIM, as_of=D8),
        get_lot(conn, sim_run_id=SIM, as_of=D8, lot_id="LOT-BAECHU"),
        get_item_lots(conn, sim_run_id=SIM, as_of=D8, item_id=BAECHU),
        get_sales_commitments(conn, sim_run_id=SIM, as_of=D8, item_id=BAECHU),
        get_policy(conn, sim_run_id=SIM, as_of=D8, item_id=BAECHU, policy_fn=_policy_fn()),
        get_capacity_context(conn, sim_run_id=SIM, as_of=D8, read_fn=_read_fn(D8)),
        get_inbound_schedule(conn, sim_run_id=SIM, as_of=D8),
    ]

    for answer in answers:
        assert answer.observed_as_of is None or answer.observed_as_of <= D8, answer
        assert answer.sim_run_id == SIM and answer.as_of == D8


def test_commitment_axis_still_has_no_observed_at(conn: psycopg.Connection) -> None:
    """🔴 Tool 이 생겼다는 이유로 `COMMITMENT_OBSERVED_AS_OF` 를 날짜로 바꾸지 않는다 —
    `cancel_allocation` 이 취소에 업무 날짜를 안 남기는 사실은 그대로다."""
    _moved_lot(conn)
    _reservation_row(conn, sale_date=D1)
    _allocation_row(conn, decided_on=D1)

    result = get_sales_commitments(conn, sim_run_id=SIM, as_of=D8, item_id=BAECHU)

    assert COMMITMENT_OBSERVED_AS_OF is None
    assert result.observed_as_of is None


def test_policy_says_it_cannot_be_restored_to_the_past(conn: psycopg.Connection) -> None:
    """유효일 칸이 없다 — `as_of` 를 받았다고 «그날 이 정책이었다» 고 답하지 않는다."""
    result = get_policy(conn, sim_run_id=SIM, as_of=D5, item_id=BAECHU, policy_fn=_policy_fn())

    assert result.observed_as_of is None
    assert agent_tools.POLICY_NOT_HISTORICAL in result.uncertainties
    assert result.item_policy is not None
    assert result.item_policy.operational_limit_days == LIMIT_DAYS
    assert result.item_policy.sell_priority_remaining_days == PRIORITY_DAYS


def test_item_without_turnover_policy_does_not_disappear(conn: psycopg.Connection) -> None:
    """🔴 정책이 없다는 이유로 **있는 품목**을 «없다» 고 하지 않는다."""
    result = get_policy(conn, sim_run_id=SIM, as_of=D5, item_id=MU, policy_fn=_policy_fn())

    assert result.item_policy is not None
    assert result.item_policy.has_storage_policy is True
    assert result.item_policy.has_turnover_policy is False


# ===========================================================================
# D. 읽기 전용 — **실제로 보낸 문장을 본다**
# ===========================================================================


class _RecordingCursor:
    """오간 문장을 그대로 적는 커서. **막지 않고 적기만 한다.**"""

    def __init__(self, real: Any, statements: list[str]) -> None:
        self._real = real
        self._statements = statements

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        text = query.as_string(self._real) if hasattr(query, "as_string") else str(query)
        self._statements.append(text)
        return self._real.execute(query, params, **kwargs)

    def __enter__(self) -> Any:
        self._real.__enter__()
        return self

    def __exit__(self, *args: object) -> Any:
        return self._real.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _RecordingConnection:
    """커밋도 롤백도 **막는다.** 🔴 Tool 이 트랜잭션을 건드리면 즉시 실패해야 한다."""

    def __init__(self, real: psycopg.Connection) -> None:
        self._real = real
        self.statements: list[str] = []

    def cursor(self, *args: Any, **kwargs: Any) -> _RecordingCursor:
        return _RecordingCursor(self._real.cursor(*args, **kwargs), self.statements)

    def commit(self) -> None:  # pragma: no cover - 불려서는 안 된다
        raise AssertionError("Tool 계층이 commit 을 불렀다")

    def rollback(self) -> None:  # pragma: no cover - 불려서는 안 된다
        raise AssertionError("Tool 계층이 rollback 을 불렀다")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_every_statement_the_tools_send_is_a_select(conn: psycopg.Connection) -> None:
    """🔴 **소스 검사로는 못 잰다.** 부르는 기존 Reader 가 무엇을 보내는지는 실제로
    한 번 돌려 봐야 안다 — 이 검사가 read-only 계약의 마지막 잠금이다."""
    _moved_lot(conn)
    _reservation_row(conn, sale_date=D1)
    _allocation_row(conn, decided_on=D1)
    _schedule_row(conn, inbound_id="INB-1", eta=D10, created=D1)
    _open_exception_row(conn, exception_id="EX-1", opened=D1)

    recorder = _RecordingConnection(conn)
    get_open_exceptions(recorder, sim_run_id=SIM, as_of=D8)
    get_lot(recorder, sim_run_id=SIM, as_of=D8, lot_id="LOT-BAECHU")
    get_item_lots(recorder, sim_run_id=SIM, as_of=D8, item_id=BAECHU)
    get_sales_commitments(recorder, sim_run_id=SIM, as_of=D8, item_id=BAECHU)
    get_policy(recorder, sim_run_id=SIM, as_of=D8, item_id=BAECHU, policy_fn=_policy_fn())
    get_capacity_context(recorder, sim_run_id=SIM, as_of=D8, read_fn=_read_fn(D8))
    get_inbound_schedule(recorder, sim_run_id=SIM, as_of=D8)
    estimate_action_impact(
        recorder,
        sim_run_id=SIM,
        as_of=D8,
        action="DISPOSAL_REQUEST",
        parameters={"lot_id": "LOT-BAECHU", "qty_kg": "100"},
        read_fn=_read_fn(D8),
    )

    assert recorder.statements, "한 문장도 안 보냈다면 이 검사는 아무것도 잠그지 않는다"
    for statement in recorder.statements:
        assert statement.lstrip().upper().startswith("SELECT"), statement
        assert not re.search(
            r"\b(INSERT|UPDATE|DELETE|TRUNCATE)\b", statement, re.IGNORECASE
        ), statement


def test_the_ledger_is_unchanged_after_running_the_tools(conn: psycopg.Connection) -> None:
    """★ 문장 검사와 **둘 다** 둔다 — 저쪽은 모양을, 이쪽은 결과를 본다."""
    _moved_lot(conn)
    _open_exception_row(conn, exception_id="EX-1", opened=D1)

    def row_counts() -> tuple[int, ...]:
        with conn.cursor() as cur:
            counts: list[int] = []
            for table in ("inventory_lots", "inventory_moves", "logistics_exceptions"):
                cur.execute(f"SELECT count(*) AS total FROM {TMP_SCHEMA}.{table}")
                counts.append(dict(cur.fetchone())["total"])
            return tuple(counts)

    before = row_counts()
    get_lot(conn, sim_run_id=SIM, as_of=D8, lot_id="LOT-BAECHU")
    get_open_exceptions(conn, sim_run_id=SIM, as_of=D8)
    get_capacity_context(conn, sim_run_id=SIM, as_of=D8, read_fn=_read_fn(D8))

    assert row_counts() == before


def test_tools_do_not_touch_the_scheduled_axes_of_the_snapshot(
    conn: psycopg.Connection,
) -> None:
    """★ 점유 축만 갈아 끼운다 — 확정 출고·입고는 이미 `as_of` 로 잘려 온다."""
    _moved_lot(conn)
    outbound = [ScheduledQuantity(date=D10, quantity_kg=Decimal(50))]
    original = _snapshot(D8).model_copy(update={"confirmed_outbound_schedule": outbound})

    result = get_capacity_context(
        conn, sim_run_id=SIM, as_of=D8, read_fn=lambda **_: _Read(snapshot=original)
    )

    assert result.inbound_lead_days == 1
    assert result.capacity_tight_ratio == Decimal("0.90")
    assert original.confirmed_outbound_schedule[0].quantity_kg == Decimal(50)
