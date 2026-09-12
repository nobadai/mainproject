"""운영 Exception 의 **지속성** — 실제 PostgreSQL 한 트랜잭션 (#628 Commit 2).

```text
D1  조건이 참      → 새 행 (OPEN · opened_as_of=D1)
D2  아직도 참      → 🔴 새 행이 아니라 **갱신** (opened_as_of 불변 · «이틀째»)
D3  조건이 사라짐  → RESOLVED + 무엇이 닫았나
D4  다시 참        → 새 행 + previous_exception_id  (재오픈하지 않는다)
```

🔴 **가짜로는 못 재는 것들을 잰다.**

```text
살아 있는 같은 문제가 정말 하나뿐인가        (부분 유일 인덱스)
실행 축이 갈려 있는가                        (sim_run_id)
근거 없는 Exception 이 행이 될 수 있는가     (CHECK)
갱신이 opened_as_of · status 를 안 건드리나
입고 뒤 칸이 **닫지 않는가**
```

끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에 아무것도 남지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
import pytest

from app.logistics import historical_repository, turnover
from app.logistics.agent import exceptions as exception_repo
from app.logistics.agent.detect import (
    COMMITTED,
    ESCALATED_FRESHNESS_EXPIRED,
    REDETECT,
    detect_logistics_exceptions,
)
from app.logistics.agent.exceptions import EmptyEvidence, open_exception
from app.logistics.agent.observe import observe
from app.logistics.agent.schemas import (
    CAPACITY_PRESSURE,
    FRESHNESS_PRESSURE,
    WAREHOUSE_SUBJECT_ID,
    ExceptionEvidence,
    ExceptionRow,
)
from app.logistics.db import get_connection
from app.logistics.schemas import InventoryLogisticsSnapshot

pytestmark = pytest.mark.db

TMP_SCHEMA = "logistics_agent_verify"
SIM = "SIM-AGENT-TEST"
OTHER_SIM = "SIM-AGENT-OTHER"
BAECHU = "ITEM-BAECHU"
MU = "ITEM-MU"
ZONE = "COLD_HUMID_0_3"
LIMIT_DAYS = 10
PRIORITY_DAYS = 3
#: 실 DB 정책값 그대로 — 검사에서 다른 숫자를 쓰면 경계를 재는 의미가 없다.
FRESHNESS_RATIO = Decimal("0.30")
CAPACITY_RATIO = Decimal("0.90")
#: 🔴 **작게 잡는다.** 사용률 = 물리 점유 ÷ 보장 용량이라, 900/1000 이 정확히 0.90 이다.
GUARANTEED = Decimal(1000)

D1 = date(2026, 1, 20)
D2 = D1 + timedelta(days=1)
D3 = D1 + timedelta(days=2)
D4 = D1 + timedelta(days=3)

_DB_DIR = Path(__file__).resolve().parents[3] / "database"

_STUBS = f"""
CREATE TABLE {TMP_SCHEMA}.items (item_id text PRIMARY KEY, item_name text);
CREATE TABLE {TMP_SCHEMA}.partners (partner_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sim_runs (sim_run_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.purchase_items (purchase_item_id text PRIMARY KEY);
CREATE TABLE {TMP_SCHEMA}.sales (sale_id text PRIMARY KEY, sale_date date);
CREATE TABLE {TMP_SCHEMA}.sale_items (sale_item_id text PRIMARY KEY);
"""


def _repo_block(table: str) -> str:
    text = (_DB_DIR / "10_domain_schema.sql").read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE haetdeul\.{table}\s*\(.*?\n\);", text, re.DOTALL)
    assert match is not None, table
    parts = [match.group(0)]
    parts += re.findall(rf"ALTER TABLE ONLY haetdeul\.{table}\s+ADD CONSTRAINT [^;]+;", text)
    return "\n".join(parts)


def _file(name: str) -> str:
    text = (_DB_DIR / name).read_text(encoding="utf-8")
    return re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", text)


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
            for name in ("30_logistics_wms_schema.sql", "logistics_inventory_lots_nullable.sql"):
                cur.execute(_file(name).replace("haetdeul.", f"{TMP_SCHEMA}."))
            # 🔴 이 판이 만드는 표가 검사 대상이다 — 저장소의 DDL 을 **그대로** 돌린다.
            에이전트 = _file("40_logistics_agent_schema.sql")
            cur.execute(에이전트.replace("haetdeul.", f"{TMP_SCHEMA}."))

            for 실행 in (SIM, OTHER_SIM):
                cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (실행,))
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES ('PI-TEST')")
            for item, name in ((BAECHU, "배추"), (MU, "무")):
                cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (item, name))
                cur.execute(
                    f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
                    " (item_id, storage_zone, operational_limit_days,"
                    " operational_policy_status) VALUES (%s, %s, %s, 'PROVISIONAL')",
                    (item, ZONE, LIMIT_DAYS),
                )
            # 🔴 회전 정책은 **배추에만** 넣는다 — 실 DB 도 5 중 3 품목뿐이고,
            #    정책 없는 품목이 조회에서 사라지지 않는 것이 계약이다 (LEFT JOIN).
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.item_turnover_policies"
                " (item_id, operational_turnover_target_days, sell_priority_remaining_days,"
                "  policy_status, evidence_grade, source_ref)"
                " VALUES (%s, 10, %s, 'SIMULATION_POLICY', 'SIM_FIXED', 'TEST')",
                (BAECHU, PRIORITY_DAYS),
            )
        for module in (turnover, historical_repository, exception_repo):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ── 준비 도우미 ─────────────────────────────────────────────────────────


class _Read(NamedTuple):
    """`observe` 가 실제로 읽는 것은 `read.snapshot` 하나다.

    🔴 **`get_current_logistics_read` 를 그대로 쓸 수 없다.** 그것은 자기 커넥션을
       새로 열어 `logistics_runtime_fixture` · `agent_policy_config` 를 읽는데, 이
       검사의 임시 스키마는 **아직 커밋되지 않은 트랜잭션 안**에 있어 다른 커넥션에는
       안 보인다. 그래서 스냅샷 경계만 갈아 끼운다 (`read_fn`).
    """

    snapshot: InventoryLogisticsSnapshot


def _lot_row(
    conn: psycopg.Connection,
    lot_id: str,
    *,
    item_id: str,
    qty: str,
    received: date,
    sim_run_id: str = SIM,
    status: str = "ACTIVE",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status
                ) VALUES (%s, %s, 'PI-TEST', %s, %s, %s, %s, 1000, %s, %s)""",
            (lot_id, sim_run_id, item_id, received, Decimal(qty), Decimal(qty), ZONE, status),
        )


def _snapshot(
    as_of: date,
    *,
    lots: list[dict[str, Any]],
    commitments: list[dict[str, Any]] | None = None,
) -> InventoryLogisticsSnapshot:
    """물리 점유 = Lot 합. 🔴 그래서 창 사용률이 **점유 ÷ 보장 용량**으로 떨어진다."""
    점유 = sum((Decimal(str(one["available_qty_kg"])) for one in lots), start=Decimal(0))
    return InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=as_of,
        on_hand_by_lot=lots,
        in_transit=[],
        confirmed_inbound_schedule=[],
        confirmed_outbound_schedule=[],
        outbound_commitments=[] if commitments is None else commitments,
        used_capacity_kg=점유,
        guaranteed_capacity_kg=GUARANTEED,
        burst_capacity_kg=GUARANTEED * 2,
        guaranteed_capacity_by_zone_kg=None,
        inbound_lead_days=1,
        capacity_tight_ratio=CAPACITY_RATIO,
        freshness_pressure_ratio=FRESHNESS_RATIO,
        evidence_refs=["TEST"],
    )


def _배추(*, qty: str = "500", remaining: int, received: date) -> dict[str, Any]:
    return {
        "lot_id": "LOT-BAECHU",
        "item": "배추",
        "available_qty_kg": Decimal(qty),
        "received_at": received,
        "remaining_freshness_days": remaining,
        "effective_freshness_limit_days": LIMIT_DAYS,
        "status": "ACTIVE",
        "storage_zone": ZONE,
    }


def _무(*, qty: str, received: date) -> dict[str, Any]:
    return {
        "lot_id": "LOT-MU",
        "item": "무",
        "available_qty_kg": Decimal(qty),
        "received_at": received,
        "remaining_freshness_days": 8,
        "effective_freshness_limit_days": LIMIT_DAYS,
        "status": "ACTIVE",
        "storage_zone": ZONE,
    }


def _탐지(
    conn: psycopg.Connection,
    snapshot: InventoryLogisticsSnapshot,
    *,
    phase: str = "AFTER_OUTBOUND",
    sim_run_id: str = SIM,
):
    return detect_logistics_exceptions(
        conn,
        sim_run_id=sim_run_id,
        as_of=snapshot.as_of,
        phase=phase,
        observe_fn=partial(observe, read_fn=lambda **_: _Read(snapshot=snapshot)),
    )


def _행들(conn: psycopg.Connection, *, sim_run_id: str = SIM) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {TMP_SCHEMA}.logistics_exceptions"
            " WHERE sim_run_id = %s ORDER BY opened_as_of, exception_id",
            (sim_run_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def _하루(conn: psycopg.Connection, as_of: date, *, remaining: int, 무: str = "400") -> Any:
    """배추 500kg + 무 `무`kg. 점유 = 500 + 무 → 900 이면 사용률이 정확히 0.90."""
    _lot_row(conn, "LOT-BAECHU", item_id=BAECHU, qty="500", received=D1 - timedelta(days=7))
    _lot_row(conn, "LOT-MU", item_id=MU, qty=무, received=D1 - timedelta(days=2))
    return _snapshot(
        as_of,
        lots=[
            _배추(remaining=remaining, received=D1 - timedelta(days=7)),
            _무(qty=무, received=D1 - timedelta(days=2)),
        ],
    )


# ===========================================================================
# A. 연다 — 조건이 참인 날
# ===========================================================================


def test_경계에서_두_문제가_각각_열린다(conn: psycopg.Connection) -> None:
    """신선도 비율 0.30(= 임계) · 창 사용률 0.90(= 임계). **둘 다 `<=` · `>=` 로 선다.**"""
    out = _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")

    행 = _행들(conn)
    assert out.status == "RAN"
    assert len(out.opened) == 2
    assert [(one["code"], one["subject_type"], one["subject_id"]) for one in 행] == [
        (CAPACITY_PRESSURE, "WAREHOUSE", WAREHOUSE_SUBJECT_ID),
        (FRESHNESS_PRESSURE, "LOT", "LOT-BAECHU"),
    ]
    신선도 = next(one for one in 행 if one["code"] == FRESHNESS_PRESSURE)
    # 잔여 3 <= 판매우선 경계 3 — 회전 정책이 «우선 팔라» 고 한 구간이다.
    assert 신선도["severity"] == "HIGH"
    assert 신선도["status"] == "OPEN"
    assert 신선도["opened_as_of"] == D1 and 신선도["last_detected_as_of"] == D1
    # 🔴 정책값이 근거에 섞여 있어 관측일은 «안 쟀다» 다 (§18).
    assert 신선도["observed_as_of"] is None
    assert 신선도["resolved_as_of"] is None and 신선도["previous_exception_id"] is None
    assert len(신선도["evidence_json"]) >= 6
    assert out.uncertainties == ()


def test_임계_아래면_아무_행도_안_생긴다(conn: psycopg.Connection) -> None:
    """신선도 0.4 · 사용률 0.8 — 확인했고 손댈 것이 없다."""
    out = _탐지(conn, _하루(conn, D1, remaining=4, 무="300"), phase="AFTER_INBOUND")

    assert out.status == "NOTHING_DUE"
    assert _행들(conn) == []


def test_이미_다_잡힌_Lot_은_열지_않는다(conn: psycopg.Connection) -> None:
    """🔴 이미 판매 확정·할당된 재고를 또 «빨리 파세요» 로 올리지 않는다."""
    _하루(conn, D1, remaining=3)
    snapshot = _snapshot(
        D1,
        lots=[
            _배추(remaining=3, received=D1 - timedelta(days=7)),
            _무(qty="300", received=D1 - timedelta(days=2)),
        ],
        commitments=[{"item": "배추", "lot_id": "LOT-BAECHU", "quantity_kg": Decimal(500)}],
    )

    _탐지(conn, snapshot, phase="AFTER_INBOUND")

    assert [one["code"] for one in _행들(conn)] == []


# ===========================================================================
# B. 갱신 — 같은 문제가 다음 날에도 참일 때
# ===========================================================================


def test_다음_날_같은_조건은_새_행이_아니라_갱신이다(conn: psycopg.Connection) -> None:
    """🔴 **«이틀째» 를 셀 수 있어야 한다.** 매일 새 행을 쌓으면 그 수가 사라지고,
    조사·제안이 붙은 문제가 다음 날 다른 행이 된다."""
    첫날 = _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")
    처음 = _행들(conn)

    둘째날 = _탐지(conn, _snapshot(
        D2,
        lots=[
            _배추(remaining=2, received=D1 - timedelta(days=7)),
            _무(qty="400", received=D1 - timedelta(days=2)),
        ],
    ), phase="AFTER_INBOUND")
    뒤 = _행들(conn)

    assert 둘째날.opened == () and len(둘째날.updated) == 2
    assert [one["exception_id"] for one in 뒤] == [one["exception_id"] for one in 처음]
    신선도 = next(one for one in 뒤 if one["code"] == FRESHNESS_PRESSURE)
    assert 신선도["opened_as_of"] == D1, "🔴 처음 잡힌 날이 바뀌면 «며칠째» 가 거짓이 된다"
    assert 신선도["last_detected_as_of"] == D2
    assert 신선도["status"] == "OPEN"
    assert set(첫날.opened) == {one["exception_id"] for one in 처음}


def test_살아_있는_같은_문제는_DB_가_둘째_행을_막는다(conn: psycopg.Connection) -> None:
    """응용 코드의 조회가 한 번 빠지는 날에도 장부는 안 갈려야 한다."""
    _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")
    기존 = next(one for one in _행들(conn) if one["code"] == FRESHNESS_PRESSURE)

    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction(force_rollback=True):
        open_exception(
            conn,
            row=ExceptionRow(
                exception_id="EX-DUP",
                sim_run_id=SIM,
                code=FRESHNESS_PRESSURE,
                subject_type="LOT",
                subject_id=기존["subject_id"],
                severity="HIGH",
                status="OPEN",
                opened_as_of=D2,
                last_detected_as_of=D2,
                observed_as_of=None,
                evidence=(
                    ExceptionEvidence(
                        fact="remaining_freshness_days",
                        value=Decimal(2),
                        unit="일",
                        source="inventory_lots",
                        source_id="LOT-BAECHU",
                    ),
                ),
                detector_version="v1",
            ),
        )


# ===========================================================================
# C. 닫는다 — 조건이 사라진 날
# ===========================================================================


def test_입고_뒤_칸은_닫지_않는다(conn: psycopg.Connection) -> None:
    """🔴 **그날 나갈 재고를 보기도 전에 «해결됐다» 고 적지 않는다.**"""
    _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")

    out = _탐지(conn, _snapshot(
        D2,
        lots=[
            _배추(remaining=8, received=D1 - timedelta(days=7)),
            _무(qty="300", received=D1 - timedelta(days=2)),
        ],
    ), phase="AFTER_INBOUND")

    assert out.resolved == ()
    assert {one["status"] for one in _행들(conn)} == {"OPEN"}


def test_출고_뒤_칸이_조건이_사라진_문제를_닫는다(conn: psycopg.Connection) -> None:
    """할당이 잔량을 다 덮었으면 «위험 관리 상태» · 용량이 회복했으면 그냥 회복이다."""
    _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")

    out = _탐지(conn, _snapshot(
        D3,
        lots=[
            _배추(remaining=1, received=D1 - timedelta(days=7)),
            _무(qty="300", received=D1 - timedelta(days=2)),
        ],
        commitments=[{"item": "배추", "lot_id": "LOT-BAECHU", "quantity_kg": Decimal(500)}],
    ))

    assert len(out.resolved) == 2
    닫힘 = {one["code"]: one for one in _행들(conn)}
    assert 닫힘[FRESHNESS_PRESSURE]["status"] == "RESOLVED"
    assert 닫힘[FRESHNESS_PRESSURE]["resolved_as_of"] == D3
    assert 닫힘[FRESHNESS_PRESSURE]["resolved_by"] == COMMITTED
    assert 닫힘[CAPACITY_PRESSURE]["resolved_by"] == REDETECT


def test_잔량이_남은_채_신선도가_다하면_넘어갔다고_적고_닫는다(conn: psycopg.Connection) -> None:
    """§7.1 E. 🔴 **후속 Exception 을 만들지 않는다** — 그 탐지기는 이번 판에 없다."""
    _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")

    _탐지(conn, _snapshot(
        D3,
        lots=[
            _배추(remaining=0, received=D1 - timedelta(days=7)),
            _무(qty="300", received=D1 - timedelta(days=2)),
        ],
    ))

    신선도 = next(one for one in _행들(conn) if one["code"] == FRESHNESS_PRESSURE)
    assert 신선도["status"] == "RESOLVED"
    assert 신선도["resolved_by"] == ESCALATED_FRESHNESS_EXPIRED
    assert not any(one["code"] == "FRESHNESS_EXPIRED" for one in _행들(conn))


def test_기준이_없는_날에는_닫지도_않는다(conn: psycopg.Connection) -> None:
    """🔴 **«기준이 없어 못 쟀다» 를 «해결됐다» 로 적으면 그 문제는 아무도 못 찾는다.**"""
    _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")

    기준없음 = _snapshot(
        D3,
        lots=[
            _배추(remaining=8, received=D1 - timedelta(days=7)),
            _무(qty="300", received=D1 - timedelta(days=2)),
        ],
    ).model_copy(update={"freshness_pressure_ratio": None, "capacity_tight_ratio": None})
    out = _탐지(conn, 기준없음)

    assert out.resolved == ()
    assert {one["status"] for one in _행들(conn)} == {"OPEN"}
    assert any("POLICY_UNRESOLVED" in one for one in out.uncertainties)


# ===========================================================================
# D. 재발 — 닫은 뒤 같은 조건이 다시 참일 때
# ===========================================================================


def test_닫힌_뒤_재발하면_새_행이_이전_행을_가리킨다(conn: psycopg.Connection) -> None:
    """🔴 **재오픈하지 않는다.** 닫힌 날과 다시 열린 날이 한 행에 겹치면 «며칠째» 를
    셀 수 없다 — 그래서 새 행을 열고 고리로 잇는다."""
    _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")
    _탐지(conn, _snapshot(
        D3,
        lots=[
            _배추(remaining=8, received=D1 - timedelta(days=7)),
            _무(qty="300", received=D1 - timedelta(days=2)),
        ],
    ))
    닫힌것 = next(one for one in _행들(conn) if one["code"] == FRESHNESS_PRESSURE)
    assert 닫힌것["status"] == "RESOLVED"

    _탐지(conn, _snapshot(
        D4,
        lots=[
            _배추(remaining=1, received=D1 - timedelta(days=7)),
            _무(qty="300", received=D1 - timedelta(days=2)),
        ],
    ))

    신선도들 = [one for one in _행들(conn) if one["code"] == FRESHNESS_PRESSURE]
    assert len(신선도들) == 2
    새것 = next(one for one in 신선도들 if one["status"] == "OPEN")
    assert 새것["exception_id"] != 닫힌것["exception_id"]
    assert 새것["previous_exception_id"] == 닫힌것["exception_id"]
    assert 새것["opened_as_of"] == D4
    assert 새것["severity"] == "CRITICAL", "잔여 1일이면 내일은 못 판다"


# ===========================================================================
# E. 축과 근거
# ===========================================================================


def test_실행이_다르면_남의_문제를_안_건드린다(conn: psycopg.Connection) -> None:
    """🔴 **축을 안 좁히면 남의 실행 창고가 내 장부에 들어온다.**"""
    _탐지(conn, _하루(conn, D1, remaining=3), phase="AFTER_INBOUND")
    _lot_row(
        conn,
        "LOT-OTHER",
        item_id=BAECHU,
        qty="500",
        received=D1 - timedelta(days=7),
        sim_run_id=OTHER_SIM,
    )
    다른실행 = _snapshot(
        D1,
        lots=[
            {
                "lot_id": "LOT-OTHER",
                "item": "배추",
                "available_qty_kg": Decimal(500),
                "received_at": D1 - timedelta(days=7),
                "remaining_freshness_days": 3,
                "effective_freshness_limit_days": LIMIT_DAYS,
                "status": "ACTIVE",
                "storage_zone": ZONE,
            }
        ],
    )

    _탐지(conn, 다른실행, phase="AFTER_INBOUND", sim_run_id=OTHER_SIM)

    assert [one["subject_id"] for one in _행들(conn) if one["code"] == FRESHNESS_PRESSURE] == [
        "LOT-BAECHU"
    ]
    assert [
        one["subject_id"] for one in _행들(conn, sim_run_id=OTHER_SIM)
        if one["code"] == FRESHNESS_PRESSURE
    ] == ["LOT-OTHER"]


def test_근거_없는_Exception_은_행이_될_수_없다(conn: psycopg.Connection) -> None:
    """응용 코드가 먼저 막고(어느 탐지기인지를 사유에 적는다), DB 도 막는다."""
    빈근거 = ExceptionRow(
        exception_id="EX-EMPTY",
        sim_run_id=SIM,
        code=FRESHNESS_PRESSURE,
        subject_type="LOT",
        subject_id="LOT-BAECHU",
        severity="MEDIUM",
        status="OPEN",
        opened_as_of=D1,
        last_detected_as_of=D1,
        observed_as_of=None,
        evidence=(),
        detector_version="v1",
    )
    with pytest.raises(EmptyEvidence):
        open_exception(conn, row=빈근거)

    with (
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(force_rollback=True),
        conn.cursor() as cur,
    ):
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.logistics_exceptions (
                    exception_id, sim_run_id, code, subject_type, subject_id, severity,
                    status, opened_as_of, last_detected_as_of, evidence_json,
                    detector_version
                ) VALUES ('EX-RAW', %s, %s, 'LOT', 'LOT-BAECHU', 'MEDIUM', 'OPEN',
                          %s, %s, '[]'::jsonb, 'v1')""",
            (SIM, FRESHNESS_PRESSURE, D1, D1),
        )
