"""조사 Runtime — **실제 PostgreSQL 한 트랜잭션** (#628 Commit 4).

```text
쓰기 0        조사 전후로 업무 표가 한 줄도 안 바뀌는가        ← 이 파일의 존재 이유
과거 재현     as_of 가 원장까지 실제로 내려가는가
실행 격리     남의 실행 Lot 을 물으면 정말 막히는가
역할 경계     규칙 제안이 판매 수량을 안 정하는가
```

🔴 **가짜 Tool 로는 «쓰기 0» 을 못 잰다.** 스텁을 꽂으면 *"우리가 안 썼다"* 가 아니라
   *"가짜가 안 썼다"* 를 확인하게 된다. 실제 표를 앞뒤로 재야 의미가 있다.

⚠️ **용량 경로는 여기서 못 잰다.** `get_capacity_context` 의 기본 `read_fn` 은
   `get_current_logistics_read` 이고 그것은 **자기 커넥션을 새로 연다** — 이 검사의 임시
   스키마는 아직 커밋되지 않은 트랜잭션 안이라 그 커넥션에는 안 보인다
   (`test_logistics_agent_tools_db` 가 같은 한계를 이미 적어 뒀다). 창고 subject 경로는
   `test_logistics_agent_graph` 가 가짜 Tool 로 덮는다.

끝나면 **통째로 롤백한다** — 공유 `haetdeul` 에 아무것도 남지 않는다.
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

from app.logistics import historical_repository, inbound_schedules, turnover
from app.logistics.agent import exceptions as exception_repo
from app.logistics.agent.exceptions import open_exception
from app.logistics.agent.graph import run_investigation
from app.logistics.agent.investigation import (
    PINNED_ARGUMENT_OVERRIDE,
    SUBJECT_OUT_OF_SCOPE,
    AgentLLMDisabled,
    FinishReason,
    InvestigationBudget,
    InvestigationOption,
    InvestigationReport,
    InvestigationStep,
    ToolCallStatus,
)
from app.logistics.agent.schemas import (
    FRESHNESS_PRESSURE,
    ExceptionEvidence,
    ExceptionRow,
)
from app.logistics.db import get_connection

pytestmark = pytest.mark.db

TMP_SCHEMA = "logistics_agent_investigation_verify"
SIM = "SIM-INVESTIGATE"
OTHER_SIM = "SIM-INVESTIGATE-OTHER"
BAECHU = "ITEM-BAECHU"
MU = "ITEM-MU"
ZONE = "COLD_HUMID_0_3"
LIMIT_DAYS = 10
UNIT_COST = Decimal(1200)

#: ```text
#: D1  입고 1,000kg
#: D5  출고   300kg   → 그날 잔량 700kg
#: D8  출고   200kg   → 그날 잔량 500kg
#: ```
D1 = date(2026, 1, 1)
D5 = date(2026, 1, 5)
D8 = date(2026, 1, 8)
D10 = date(2026, 1, 10)

EXC = "EXC-FRESHNESS-1"
LOT = "LOT-BAECHU"
OTHER_LOT = "LOT-BAECHU-OTHER-RUN"

DB_DIR = Path(__file__).resolve().parents[3] / "database"

#: 조사가 **절대 건드리면 안 되는** 표들. 앞뒤로 통째로 재서 비교한다.
WATCHED_TABLES = (
    "logistics_exceptions",
    "inventory_lots",
    "inventory_moves",
    "inventory_reservations",
    "inventory_allocations",
    "inbound_schedules",
)

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
            cur.execute(
                _file("40_logistics_agent_schema.sql").replace("haetdeul.", f"{TMP_SCHEMA}.")
            )
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
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.item_turnover_policies"
                " (item_id, operational_turnover_target_days, sell_priority_remaining_days,"
                "  policy_status, evidence_grade, source_ref)"
                " VALUES (%s, 10, 3, 'SIMULATION_POLICY', 'SIM_FIXED', 'TEST')",
                (BAECHU,),
            )
        for module in (turnover, historical_repository, exception_repo, inbound_schedules):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _lot(
    conn: psycopg.Connection,
    lot_id: str = LOT,
    *,
    item_id: str = BAECHU,
    sim_run_id: str = SIM,
    moves: list[tuple[str, str, date]] | None = None,
) -> None:
    """Lot 한 줄 + **그 잔량을 만든 원장 이동들.** 원장이 정본이다."""
    ledger = moves or [("IN", "1000", D1), ("OUT", "300", D5), ("OUT", "200", D8)]
    remaining = sum(
        (Decimal(qty) if kind == "IN" else -Decimal(qty)) for kind, qty, _ in ledger
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {TMP_SCHEMA}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE')""",
            (lot_id, sim_run_id, f"PI-{item_id}", item_id, D1, Decimal(1000), remaining,
             UNIT_COST, ZONE),
        )
        for index, (kind, qty, moved_at) in enumerate(ledger, start=1):
            cur.execute(
                f"""INSERT INTO {TMP_SCHEMA}.inventory_moves (
                        move_id, sim_run_id, lot_id, move_type,
                        quantity_kg, moved_at, reason_code
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'TEST')""",
                (f"MOVE-{lot_id}-{index}", sim_run_id, lot_id, kind, Decimal(qty), moved_at),
            )


def _exception(conn: psycopg.Connection, *, opened: date = D1) -> None:
    open_exception(
        conn,
        row=ExceptionRow(
            exception_id=EXC,
            sim_run_id=SIM,
            code=FRESHNESS_PRESSURE,
            subject_type="LOT",
            subject_id=LOT,
            severity="HIGH",
            status="OPEN",
            opened_as_of=opened,
            last_detected_as_of=opened,
            observed_as_of=opened,
            evidence=(
                ExceptionEvidence(
                    fact="remaining_freshness_days",
                    value=Decimal(1),
                    unit="일",
                    source="inventory_lots",
                    source_id=LOT,
                ),
            ),
            detector_version="v1",
        ),
    )


def _snapshot(conn: psycopg.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """감시 대상 표를 **통째로** 뜬다 — 줄 수만 세면 UPDATE 를 못 잡는다."""
    taken: dict[str, list[tuple[Any, ...]]] = {}
    with conn.cursor() as cur:
        for table in WATCHED_TABLES:
            cur.execute(f"SELECT * FROM {TMP_SCHEMA}.{table} ORDER BY 1")
            taken[table] = [tuple(str(value) for value in row) for row in cur.fetchall()]
    return taken


def _writes(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    """이 트랜잭션이 **어느 표에 몇 줄을 썼는가** — 엔진이 직접 센다.

    🔴 행을 떠서 비교하는 `_snapshot` 보다 **강하다.** 저것은 내가 고른 표만 보지만
       이것은 **이 트랜잭션이 건드린 모든 표**를 본다 — 감시 목록에 없는 표에 쓰거나,
       썼다가 같은 값으로 되돌려도 잡힌다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del"
            " FROM pg_stat_xact_user_tables"
            " WHERE n_tup_ins + n_tup_upd + n_tup_del > 0 ORDER BY relname"
        )
        return [tuple(row.values()) for row in cur.fetchall()]


def _plan(*steps: InvestigationStep) -> Any:
    queue = list(steps)

    def plan_fn(view: Any) -> InvestigationStep:
        del view
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return plan_fn


def _call(tool_name: str, **arguments: Any) -> InvestigationStep:
    return InvestigationStep(action="CALL_TOOL", tool_name=tool_name, arguments=arguments)


FINISH = InvestigationStep(action="FINISH", reason="충분하다")


def _report(**fields: Any) -> Any:
    report = InvestigationReport(**fields)
    return lambda view: report


def _investigate(conn: psycopg.Connection, *, as_of: date = D10, **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "plan_fn": _plan(FINISH),
        "finalize_fn": _report(summary="정리"),
    }
    options.update(kwargs)
    return run_investigation(
        conn, sim_run_id=SIM, as_of=as_of, exception_id=EXC, **options
    )


# ══════════════════════════════════════════════════════════════════════════
#  쓰기 0 — 이 파일의 존재 이유
# ══════════════════════════════════════════════════════════════════════════


class TestNoWrite:
    def test_the_write_detector_actually_detects(self, conn: psycopg.Connection) -> None:
        """🔴 **먼저 계측기를 믿을 수 있는지 본다.**

        *"안 썼다"* 를 주장하는 검사는 계측기가 고장 나도 초록불이다. 진짜 UPDATE 를
        한 번 걸어 계측기가 그것을 보는지 확인한 뒤에야 아래 검사가 의미를 갖는다.
        """
        _lot(conn)
        _exception(conn)
        before = _writes(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TMP_SCHEMA}.logistics_exceptions SET status = 'PROPOSED'"
                " WHERE exception_id = %s",
                (EXC,),
            )
        assert _writes(conn) != before

    def test_the_engine_reports_zero_writes_for_the_whole_investigation(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 조사가 도는 동안 **어떤 표에도** 한 줄이 안 들어가고 안 바뀌고 안 지워진다.

        감시 목록을 내가 고르지 않는다 — 엔진이 «이 트랜잭션이 건드린 표» 를 통째로 센다.
        """
        _lot(conn)
        _exception(conn)
        before = _writes(conn)
        result = _investigate(
            conn,
            plan_fn=_plan(_call("get_item_lots", item_id=BAECHU), FINISH),
            finalize_fn=_report(
                summary="신선도 압박",
                options=[
                    InvestigationOption(
                        action="SALES_PRIORITY_REQUEST",
                        parameters={"lot_id": LOT},
                        rationale="우선 판매 후보",
                        evidence_refs=[3],
                    )
                ],
                recommended_index=0,
            ),
        )
        assert result.finish_reason is FinishReason.FINISHED
        assert _writes(conn) == before

    def test_an_investigation_changes_no_business_row(self, conn: psycopg.Connection) -> None:
        """🔴 조사 전후로 6개 표가 **글자 하나** 안 바뀐다.

        ```text
        logistics_exceptions   상태도 note 도 안 바뀐다 — PROPOSED 전이는 Commit 5 다
        inventory_lots · moves 잔량도 이동도 안 생긴다
        reservations · allocations · inbound_schedules 도 그대로다
        ```
        """
        _lot(conn)
        _exception(conn)
        before = _snapshot(conn)
        result = _investigate(
            conn,
            plan_fn=_plan(_call("get_item_lots", item_id=BAECHU), FINISH),
            finalize_fn=_report(
                summary="신선도 압박",
                options=[
                    InvestigationOption(
                        action="SALES_PRIORITY_REQUEST",
                        parameters={"lot_id": LOT},
                        rationale="우선 판매 후보",
                        evidence_refs=[3],
                    )
                ],
                recommended_index=0,
            ),
        )
        assert result.finish_reason is FinishReason.FINISHED
        assert _snapshot(conn) == before

    def test_a_provider_failure_also_changes_nothing(self, conn: psycopg.Connection) -> None:
        """🔴 §28 — 공급자가 터져도 DB 수정 0 · Exception 상태 변경 0 · Proposal 0."""
        _lot(conn)
        _exception(conn)
        before = _snapshot(conn)

        def boom(view: Any) -> Any:
            raise TimeoutError("provider timed out")

        result = _investigate(conn, plan_fn=boom)
        assert result.llm_status == "FALLBACK"
        assert result.finish_reason is FinishReason.LLM_FAILED
        assert _snapshot(conn) == before

    def test_a_disabled_provider_also_changes_nothing(self, conn: psycopg.Connection) -> None:
        _lot(conn)
        _exception(conn)
        before = _snapshot(conn)

        def off(view: Any) -> Any:
            raise AgentLLMDisabled("Logistics agent LLM is turned off")

        result = _investigate(conn, plan_fn=off)
        assert result.llm_status == "DISABLED"
        assert _snapshot(conn) == before

    def test_the_exception_row_keeps_its_status(self, conn: psycopg.Connection) -> None:
        """조사는 Exception 을 `PROPOSED` 로 넘기지 않는다 — 그 전이는 Commit 5 다."""
        _lot(conn)
        _exception(conn)
        _investigate(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT status, note, resolved_as_of FROM {TMP_SCHEMA}.logistics_exceptions"
                " WHERE exception_id = %s",
                (EXC,),
            )
            row = cur.fetchone()
        assert row is not None
        # 🔴 상태는 `OPEN` 그대로 · 닫은 날도 사람이 적을 note 도 안 생겼다.
        assert row["status"] == "OPEN"
        assert row["resolved_as_of"] is None
        assert not row["note"]


# ══════════════════════════════════════════════════════════════════════════
#  과거 재현 — as_of 가 원장까지 내려간다
# ══════════════════════════════════════════════════════════════════════════


class TestHistorical:
    def test_the_preload_reads_the_ledger_at_that_day(self, conn: psycopg.Connection) -> None:
        """```text
        D1 입고 1,000 · D5 출고 300 · D8 출고 200
        as_of=D5 조회의 잔량은 700 이다 — 지금 값 500 이 아니다
        ```"""
        _lot(conn)
        _exception(conn)
        result = _investigate(conn, as_of=D5)
        lot_record = next(r for r in result.tool_calls if r.tool_name == "get_lot")
        assert lot_record.answer.lot.remaining_qty_kg == Decimal(700)

    def test_today_sees_the_later_moves(self, conn: psycopg.Connection) -> None:
        _lot(conn)
        _exception(conn)
        result = _investigate(conn, as_of=D10)
        lot_record = next(r for r in result.tool_calls if r.tool_name == "get_lot")
        assert lot_record.answer.lot.remaining_qty_kg == Decimal(500)

    def test_a_ledger_backed_lot_carries_a_real_observed_date(
        self, conn: psycopg.Connection
    ) -> None:
        """Lot 잔량 축에는 **진짜 날짜**가 붙는다 — 마지막 이동일이다."""
        _lot(conn)
        _exception(conn)
        result = _investigate(conn, as_of=D10)
        lot = next(r for r in result.tool_calls if r.tool_name == "get_lot").answer.lot
        assert lot.remaining_qty_observed_as_of == D8

    def test_the_investigation_observed_at_stays_unknown(self, conn: psycopg.Connection) -> None:
        """🔴 정책 축이 `None` 이라 전체도 `None` 이다 — `as_of` 로 메우지 않는다 (§24)."""
        _lot(conn)
        _exception(conn)
        result = _investigate(conn, as_of=D10)
        assert result.observed_as_of is None


# ══════════════════════════════════════════════════════════════════════════
#  실행 격리 · 못 박은 축
# ══════════════════════════════════════════════════════════════════════════


class TestBoundary:
    def test_another_runs_lot_is_out_of_scope(self, conn: psycopg.Connection) -> None:
        """남의 실행 Lot 은 존재하더라도 **물을 수 없다** (§17)."""
        _lot(conn)
        _lot(conn, OTHER_LOT, sim_run_id=OTHER_SIM)
        _exception(conn)
        result = _investigate(conn, plan_fn=_plan(_call("get_lot", lot_id=OTHER_LOT)))
        rejected = [r for r in result.tool_calls if r.status is ToolCallStatus.REJECTED]
        assert rejected
        assert rejected[0].detail == f"{SUBJECT_OUT_OF_SCOPE}:lot_id={OTHER_LOT}"
        # 🔴 그 Lot 을 읽은 성공 기록이 **하나도** 없다.
        assert not [
            r
            for r in result.tool_calls
            if r.status is ToolCallStatus.SUCCESS and r.arguments.get("lot_id") == OTHER_LOT
        ]

    def test_a_model_cannot_reach_another_run_through_arguments(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 `sim_run_id` 를 인자로 넣어도 Tool 까지 안 간다."""
        _lot(conn)
        _lot(conn, OTHER_LOT, sim_run_id=OTHER_SIM)
        _exception(conn)
        result = _investigate(
            conn,
            plan_fn=_plan(_call("get_item_lots", item_id=BAECHU, sim_run_id=OTHER_SIM)),
        )
        rejected = [r for r in result.tool_calls if r.status is ToolCallStatus.REJECTED]
        assert rejected[0].detail == f"{PINNED_ARGUMENT_OVERRIDE}:sim_run_id"

    def test_an_allowed_widening_actually_runs(self, conn: psycopg.Connection) -> None:
        """★ 같은 품목의 Lot 전부는 정당한 확장이다 — 실제로 읽힌다."""
        _lot(conn)
        _lot(conn, "LOT-BAECHU-2", moves=[("IN", "200", D1)])
        _exception(conn)
        result = _investigate(conn, plan_fn=_plan(_call("get_item_lots", item_id=BAECHU), FINISH))
        answer = next(r for r in result.tool_calls if r.tool_name == "get_item_lots").answer
        assert {lot.lot_id for lot in answer.lots} == {LOT, "LOT-BAECHU-2"}

    def test_a_missing_exception_ends_before_any_other_read(
        self, conn: psycopg.Connection
    ) -> None:
        _lot(conn)
        result = run_investigation(
            conn,
            sim_run_id=SIM,
            as_of=D10,
            exception_id="EXC-NOPE",
            plan_fn=_plan(FINISH),
            finalize_fn=_report(summary=""),
        )
        assert result.finish_reason is FinishReason.NOT_FOUND
        assert [r.tool_name for r in result.tool_calls] == ["get_open_exceptions"]


# ══════════════════════════════════════════════════════════════════════════
#  역할 경계 — 실제 Tool 이 낸 숫자로
# ══════════════════════════════════════════════════════════════════════════


class TestRoleBoundary:
    def test_the_rule_proposal_asks_sales_for_the_quantity(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 규칙 제안도 판매 수량을 안 정한다.

        ```text
        보내는 것  lot_id 만
        받는 것    UNRESOLVED + candidate_kg = 500 (원장이 낸 미약정 잔량)
        ```
        """
        _lot(conn)
        _exception(conn)

        def boom(view: Any) -> Any:
            raise TimeoutError("provider timed out")

        result = _investigate(conn, plan_fn=boom)
        option = result.options[0]
        assert option.action == "SALES_PRIORITY_REQUEST"
        assert "qty_kg" not in option.parameters
        assert option.decision_owner == "SALES"
        assert option.parameters_are_hypothesis is True
        assert option.impact.feasibility == "UNRESOLVED"
        assert option.impact.candidate_kg == Decimal(500)
        assert "IMPACT_INPUT_MISSING:qty_kg" in option.impact.uncertainties

    def test_a_supplied_quantity_is_only_validated(self, conn: psycopg.Connection) -> None:
        """수량을 주면 물류는 *"댈 수 있나"* 만 본다 — 그 값을 만들지 않는다."""
        _lot(conn)
        _exception(conn)
        result = _investigate(
            conn,
            finalize_fn=_report(
                summary="",
                options=[
                    InvestigationOption(
                        action="SALES_PRIORITY_REQUEST",
                        parameters={"lot_id": LOT, "qty_kg": 300},
                        rationale="",
                    )
                ],
            ),
        )
        impact = result.options[0].impact
        assert impact.feasibility == "FEASIBLE"
        assert impact.affected_kg == Decimal(300)
        assert impact.candidate_kg == Decimal(500)

    def test_an_over_supply_is_refused_by_the_tool(self, conn: psycopg.Connection) -> None:
        _lot(conn)
        _exception(conn)
        result = _investigate(
            conn,
            finalize_fn=_report(
                summary="",
                options=[
                    InvestigationOption(
                        action="SALES_PRIORITY_REQUEST",
                        parameters={"lot_id": LOT, "qty_kg": 99999},
                        rationale="다 팔자",
                    )
                ],
            ),
        )
        assert result.options[0].impact.feasibility == "INFEASIBLE"
        # 🔴 모델이 적은 99999 는 어떤 권위 있는 칸에도 안 올랐다.
        assert result.options[0].impact.candidate_kg == Decimal(500)


# ══════════════════════════════════════════════════════════════════════════
#  예산 — 실제 Tool 로
# ══════════════════════════════════════════════════════════════════════════


class TestBudgetOverRealTools:
    def test_a_duplicate_call_never_hits_the_database_twice(
        self, conn: psycopg.Connection
    ) -> None:
        """선행이 이미 읽은 `get_lot` 을 다시 물으면 막힌다."""
        _lot(conn)
        _exception(conn)
        result = _investigate(conn, plan_fn=_plan(_call("get_lot", lot_id=LOT)))
        successes = [
            r
            for r in result.tool_calls
            if r.tool_name == "get_lot" and r.status is ToolCallStatus.SUCCESS
        ]
        assert len(successes) == 1
        assert result.finish_reason is FinishReason.GUARD_EXHAUSTED

    def test_the_tool_budget_holds_over_real_reads(self, conn: psycopg.Connection) -> None:
        _lot(conn)
        _exception(conn)
        budget = InvestigationBudget(max_tool_calls=2, max_replans=2, max_llm_calls=11)
        result = _investigate(
            conn,
            budget=budget,
            plan_fn=_plan(*[_call("get_inbound_schedule", days=day) for day in (1, 2, 3, 4)]),
        )
        assert result.tool_call_count == 2
        assert result.finish_reason is FinishReason.BUDGET_EXCEEDED
