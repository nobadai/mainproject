"""조사 실행 기록 — **실제 PostgreSQL** (#628 Commit 7).

```text
쓰기 0        `run_investigation` 만 부르면 감사 행도 제안도 안 생기는가
감사 1        wrapper 를 불러야만 행이 서는가
연결          제안이 «자기를 낳은 조사» 를 가리키는가
축            DB 가 «남의 실행 · 남의 문제 · 없는 조사» 를 거부하는가
재조사        같은 문제를 같은 날 두 번 조사하면 두 행인가
실패 감사     NOT_FOUND · TOOL_FAILED · TIMEOUT · LLM_FAILED 도 남는가
번지지 않음   조사를 적었다고 Exception · 재고 · 판매가 움직이는가
```

🔴 **가짜 Tool 로는 «쓰기 0» 도 «축 거부» 도 못 잰다.** 스텁을 꽂으면 *"우리가 안
   썼다"* 와 *"우리가 막았다"* 까지만 확인되고, DB 가 실제로 막는지는 확인이 안 된다.

⚠️ **이 파일은 임시 스키마를 «커밋» 한다** (`…_proposals_db` 와 같은 이유). 재는 대상이
   **서비스의 커밋과 롤백 그 자체**라, 통째로 롤백하는 형제 방식을 못 쓴다 — 서비스가
   커밋하면 스키마까지 공유 DB 에 굳고, 롤백하면 스키마 생성까지 날아간다. 그래서
   스키마를 먼저 커밋해 두고 끝나면 `DROP SCHEMA … CASCADE` 로 치운다. 시작할 때도
   `DROP SCHEMA IF EXISTS` 를 한 번 돌려 앞선 실행의 잔해를 스스로 복구한다.
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
from app.logistics.agent import investigation_repository as repository
from app.logistics.agent import investigation_service as service
from app.logistics.agent import proposal_service
from app.logistics.agent import proposals as proposal_repo
from app.logistics.agent.exceptions import open_exception
from app.logistics.agent.graph import run_investigation
from app.logistics.agent.investigation import (
    FinishReason,
    InvestigationBudget,
    InvestigationOption,
    InvestigationReport,
    InvestigationStep,
)
from app.logistics.agent.schemas import (
    FRESHNESS_PRESSURE,
    ExceptionEvidence,
    ExceptionRow,
)
from app.logistics.db import get_connection
from app.master.sim_run_open import AXIS_COLUMN

pytestmark = pytest.mark.db

TMP_SCHEMA = "logistics_agent_investigation_persist_verify"
SIM = "SIM-INVESTIGATION"
OTHER_SIM = "SIM-INVESTIGATION-OTHER"
BAECHU = "ITEM-BAECHU"
ZONE = "COLD_HUMID_0_3"
LIMIT_DAYS = 10
UNIT_COST = Decimal(1200)

#: ```text
#: D1  입고 1,000kg → D5 출고 300 → 잔량 700
#: D5  조사
#: ```
D1 = date(2026, 1, 1)
D5 = date(2026, 1, 5)
D6 = date(2026, 1, 6)

EXC = "EXC-FRESHNESS-1"
OTHER_EXC = "EXC-FRESHNESS-2"
OTHER_RUN_EXC = "EXC-FRESHNESS-OTHER-RUN"
LOT = "LOT-BAECHU"
OTHER_LOT = "LOT-BAECHU-2"
OPERATOR = "operator"

DB_DIR = Path(__file__).resolve().parents[3] / "database"

#: 🔴 **조사 저장이 절대 건드리면 안 되는** 표들 (§55).
WATCHED_TABLES = (
    "inventory_lots",
    "inventory_moves",
    "inventory_reservations",
    "inventory_allocations",
    "inbound_schedules",
    "sales",
    "purchase_items",
    "logistics_exceptions",
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
            # ⚠️ 앞선 실행이 죽으며 남긴 잔해가 있으면 스스로 치운다.
            cur.execute(f"DROP SCHEMA IF EXISTS {TMP_SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {TMP_SCHEMA}")
            cur.execute(STUBS)
            for table in ("inventory_lots", "inventory_moves", "item_storage_policies"):
                cur.execute(_repo_block(table).replace("haetdeul.", f"{TMP_SCHEMA}."))
            for name in ("30_logistics_wms_schema.sql", "logistics_inventory_lots_nullable.sql"):
                cur.execute(_file(name).replace("haetdeul.", f"{TMP_SCHEMA}."))
            # 🔴 이 판이 만드는 표가 검사 대상이다 — 저장소의 DDL 을 **그대로** 돌린다.
            cur.execute(
                _file("40_logistics_agent_schema.sql").replace("haetdeul.", f"{TMP_SCHEMA}.")
            )
            for run in (SIM, OTHER_SIM):
                cur.execute(f"INSERT INTO {TMP_SCHEMA}.sim_runs VALUES (%s)", (run,))
            cur.execute(f"INSERT INTO {TMP_SCHEMA}.items VALUES (%s, %s)", (BAECHU, "배추"))
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.purchase_items VALUES (%s, 'PO-1', %s)",
                (f"PI-{BAECHU}", BAECHU),
            )
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.item_storage_policies"
                " (item_id, storage_zone, operational_limit_days,"
                " operational_policy_status) VALUES (%s, %s, %s, 'PROVISIONAL')",
                (BAECHU, ZONE, LIMIT_DAYS),
            )
            cur.execute(
                f"INSERT INTO {TMP_SCHEMA}.item_turnover_policies"
                " (item_id, operational_turnover_target_days, sell_priority_remaining_days,"
                "  policy_status, evidence_grade, source_ref)"
                " VALUES (%s, 10, 3, 'SIMULATION_POLICY', 'SIM_FIXED', 'TEST')",
                (BAECHU,),
            )
        # 🔴 **여기서 커밋한다** — 이 파일이 재는 것이 서비스의 커밋/롤백이라서다.
        connection.commit()
        for module in (
            turnover,
            historical_repository,
            exception_repo,
            inbound_schedules,
            repository,
            proposal_repo,
        ):
            monkeypatch.setattr(module, "get_db_schema", lambda: TMP_SCHEMA)
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {TMP_SCHEMA} CASCADE")
        connection.commit()
        connection.close()


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _lot(conn: psycopg.Connection, lot_id: str = LOT, *, sim_run_id: str = SIM) -> None:
    """Lot 한 줄 + **그 잔량을 만든 원장 이동들.** 원장이 정본이다."""
    ledger = [("IN", "1000", D1), ("OUT", "300", D5)]
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
            (lot_id, sim_run_id, f"PI-{BAECHU}", BAECHU, D1, Decimal(1000), remaining,
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
    conn.commit()


def _exception(
    conn: psycopg.Connection,
    *,
    exception_id: str = EXC,
    sim_run_id: str = SIM,
    subject_id: str = LOT,
) -> None:
    """⚠️ `subject_id` 를 갈라야 둘째 Exception 이 선다 — 살아 있는 같은 축의 문제는
    부분 유일 인덱스가 **하나로** 막는다.
    """
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
            opened_as_of=D1,
            last_detected_as_of=D1,
            observed_as_of=D1,
            evidence=(
                ExceptionEvidence(
                    fact="remaining_freshness_days",
                    value=Decimal(1),
                    unit="일",
                    source="inventory_lots",
                    source_id=subject_id,
                ),
            ),
            detector_version="v1",
        ),
    )
    conn.commit()


def _ready(conn: psycopg.Connection) -> None:
    _lot(conn)
    _exception(conn)


def _report(**parameters: Any) -> Any:
    option = InvestigationOption(
        action="SALES_PRIORITY_REQUEST",
        parameters={"lot_id": LOT, **parameters},
        rationale="우선 판매 후보",
        # ★ 선행 조회 전부를 인용한다 — 없는 번호는 `evaluate_options` 가 지운다.
        evidence_refs=[1, 2, 3, 4],
    )
    report = InvestigationReport(
        summary="신선도 압박", options=[option], recommended_index=0
    )
    return lambda _view: report


FINISH = InvestigationStep(action="FINISH", reason="충분하다")


def _persist(conn: psycopg.Connection, *, as_of: date = D5, **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "plan_fn": lambda _view: FINISH,
        "finalize_fn": _report(qty_kg=300),
    }
    options.update(kwargs)
    return service.run_and_persist_investigation(
        conn, sim_run_id=SIM, as_of=as_of, exception_id=EXC, **options
    )


def _investigate(conn: psycopg.Connection, *, as_of: date = D5, **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "plan_fn": lambda _view: FINISH,
        "finalize_fn": _report(qty_kg=300),
    }
    options.update(kwargs)
    return run_investigation(
        conn, sim_run_id=SIM, as_of=as_of, exception_id=EXC, **options
    )


def _stored(conn: psycopg.Connection, *, sim_run_id: str = SIM) -> list[Any]:
    return repository.select_investigations(
        conn, sim_run_id=sim_run_id, exception_id=EXC
    )


def _proposals(conn: psycopg.Connection) -> tuple[Any, ...]:
    return tuple(
        proposal_repo.select_proposals(conn, sim_run_id=SIM, exception_id=EXC)
    )


def _count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TMP_SCHEMA}.{table}")
        return cur.fetchone()["n"]


def _snapshot(conn: psycopg.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """감시 대상 표를 **통째로** 뜬다 — 줄 수만 세면 UPDATE 를 못 잡는다."""
    taken: dict[str, list[tuple[Any, ...]]] = {}
    with conn.cursor() as cur:
        for table in WATCHED_TABLES:
            cur.execute(f"SELECT * FROM {TMP_SCHEMA}.{table} ORDER BY 1")
            taken[table] = [tuple(str(value) for value in row) for row in cur.fetchall()]
    return taken


def _refuses(conn: psycopg.Connection, statement: str, error: type[Exception]) -> None:
    with pytest.raises(error), conn.cursor() as cur:
        cur.execute(statement)
    conn.rollback()


def _investigation_sql(
    *, investigation_id: str, sim_run_id: str = SIM, exception_id: str = EXC
) -> str:
    return f"""INSERT INTO {TMP_SCHEMA}.logistics_investigations (
            investigation_id, sim_run_id, exception_id, as_of,
            finish_reason, llm_status, tool_trace_json, result_json
        ) VALUES ('{investigation_id}', '{sim_run_id}', '{exception_id}', '{D5}',
            'FINISHED', 'SUCCESS', '[]'::jsonb, '{{}}'::jsonb)"""


def _proposal_sql(
    *,
    proposal_id: str = "PRP-DIRECT",
    sim_run_id: str = SIM,
    exception_id: str = EXC,
    investigation_id: str | None,
) -> str:
    pointer = "NULL" if investigation_id is None else f"'{investigation_id}'"
    return f"""INSERT INTO {TMP_SCHEMA}.logistics_action_proposals (
            proposal_id, sim_run_id, exception_id, investigation_id, proposal_key,
            action_type, decision_owner, parameters_json, impact_json,
            evidence_refs_json, status, proposed_as_of, proposed_by
        ) VALUES ('{proposal_id}', '{sim_run_id}', '{exception_id}', {pointer}, 'k',
            'ACCEPT_RISK', 'LOGISTICS', '{{}}'::jsonb, '{{}}'::jsonb, '[]'::jsonb,
            'PROPOSED', '{D5}', '{OPERATOR}')"""


# ══════════════════════════════════════════════════════════════════════════
#  §56 — wrapper 를 안 부르면 아무것도 안 남는다
# ══════════════════════════════════════════════════════════════════════════


class TestRuntimeStaysWriteFree:
    def test_the_write_detector_actually_detects(self, conn: psycopg.Connection) -> None:
        """🔴 **먼저 계측기를 믿을 수 있는지 본다.**

        *"안 썼다"* 를 주장하는 검사는 계측기가 고장 나도 초록불이다.
        """
        _ready(conn)
        with conn.cursor() as cur:
            cur.execute(_investigation_sql(investigation_id="INV-PROBE"))
        assert _count(conn, "logistics_investigations") == 1
        conn.rollback()
        assert _count(conn, "logistics_investigations") == 0

    def test_running_an_investigation_alone_stores_nothing(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §16 · §56 — 그래프에 `persist` 노드를 안 넣었다는 것의 실제 증거다."""
        _ready(conn)
        before = _snapshot(conn)
        result = _investigate(conn)
        assert result.finish_reason is FinishReason.FINISHED
        assert _count(conn, "logistics_investigations") == 0
        assert _count(conn, "logistics_action_proposals") == 0
        assert _snapshot(conn) == before


# ══════════════════════════════════════════════════════════════════════════
#  §57 · §61 · §62 · §63 — 감사 행 하나
# ══════════════════════════════════════════════════════════════════════════


class TestPersistedInvestigation:
    def test_the_wrapper_writes_exactly_one_row(self, conn: psycopg.Connection) -> None:
        _ready(conn)
        persisted = _persist(conn)
        assert persisted.status == "SAVED"
        assert persisted.investigation_id.startswith("INV-")
        (stored,) = _stored(conn)
        assert stored.investigation_id == persisted.investigation_id
        assert stored.finish_reason == "FINISHED"
        # 🔴 커밋됐다 — 롤백해도 남아 있다.
        conn.rollback()
        assert len(_stored(conn)) == 1

    def test_the_business_day_and_the_observation_date_are_verbatim(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §38 · §39 — `as_of` 는 Runtime 이 받은 값, 관측일은 조사가 낸 값 그대로."""
        _ready(conn)
        persisted = _persist(conn)
        (stored,) = _stored(conn)
        assert stored.as_of == D5
        assert stored.observed_as_of == persisted.result.observed_as_of
        assert stored.sim_run_id == SIM
        assert stored.exception_id == EXC

    def test_the_tool_answer_is_not_in_the_stored_trace(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §9 · §61 — 진짜 Tool 이 낸 진짜 답으로 잰다.

        ```text
        trace 에 있다     get_lot 을 불렀다 · 무슨 인자로 · 어떻게 끝났나
        trace 에 없다     그 Lot 의 단가 · 잔량 · 신선도 …                ← 답 전체
        ```
        """
        _ready(conn)
        _persist(conn)
        (stored,) = _stored(conn)
        called = [entry["tool_name"] for entry in stored.tool_trace]
        assert "get_lot" in called, called
        for entry in stored.tool_trace:
            assert "answer" not in entry, entry
            assert set(entry) == set(service.TRACE_FIELDS), entry
        # ★ 답 안에만 있는 값이 어디에도 안 실렸다.
        assert "unit_cost_krw_per_kg" not in str(stored.tool_trace)
        assert str(UNIT_COST) not in str(stored.tool_trace)

    def test_the_trace_keeps_what_was_asked(self, conn: psycopg.Connection) -> None:
        """🔴 §62 — 호출 metadata 는 남는다."""
        _ready(conn)
        _persist(conn)
        (stored,) = _stored(conn)
        lot_call = next(one for one in stored.tool_trace if one["tool_name"] == "get_lot")
        assert lot_call["arguments"] == {"lot_id": LOT}
        assert lot_call["status"] == "TOOL_SUCCESS"
        assert isinstance(lot_call["sequence"], int)
        assert isinstance(lot_call["uncertainties"], list)
        # 순번이 실제 실행 순서를 따라간다.
        assert [one["sequence"] for one in stored.tool_trace] == sorted(
            one["sequence"] for one in stored.tool_trace
        )

    def test_the_final_result_is_restorable(self, conn: psycopg.Connection) -> None:
        """🔴 §63 — *"무슨 결론으로 끝났나"* 를 기록만 보고 복원한다."""
        _ready(conn)
        _persist(conn)
        (stored,) = _stored(conn)
        assert stored.result["finish_reason"] == "FINISHED"
        assert stored.result["recommended_index"] == 0
        (option,) = stored.result["options"]
        assert option["action"] == "SALES_PRIORITY_REQUEST"
        assert option["decision_owner"] == "SALES"
        assert option["parameters"]["lot_id"] == LOT
        # 🔴 영향은 Commit 3 의 계산기가 낸 답 그대로다 — 여기서 새로 안 셈한다.
        assert option["impact"]["feasibility"] == "FEASIBLE"
        assert option["impact"]["affected_kg"] == "300"

    def test_storing_the_investigation_touches_no_business_table(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §54 · §55 — 조사를 적었다고 문제도 재고도 판매도 안 움직인다."""
        _ready(conn)
        before = _snapshot(conn)
        _persist(conn)
        assert _snapshot(conn) == before
        assert _count(conn, "logistics_action_proposals") == 0


# ══════════════════════════════════════════════════════════════════════════
#  §58 · §30 · §31 — 제안과의 연결
# ══════════════════════════════════════════════════════════════════════════


class TestProposalLinkage:
    def test_the_proposal_points_at_the_investigation_that_made_it(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §29 · §34 — 조사 저장이 **먼저 커밋되고**, 그 다음에 제안이 선다."""
        _ready(conn)
        persisted = _persist(conn)
        outcome = proposal_service.create_proposal(
            conn,
            result=persisted.result,
            as_of=D5,
            proposed_by=OPERATOR,
            investigation_id=persisted.investigation_id,
        )
        assert outcome.created
        (proposal,) = _proposals(conn)
        assert proposal.investigation_id == persisted.investigation_id
        # 그 ID 로 조사 기록을 실제로 찾아갈 수 있다.
        found = repository.select_investigation(
            conn, sim_run_id=SIM, investigation_id=proposal.investigation_id
        )
        assert found is not None
        assert found.exception_id == proposal.exception_id

    def test_the_investigation_stays_when_no_proposal_is_raised(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §30 · §31 — 추천이 없어 제안 0 건이어도 **조사는 실제로 돌았다.**"""
        _ready(conn)
        persisted = _persist(
            conn,
            finalize_fn=lambda _view: InvestigationReport(
                summary="추천할 대안이 없다", options=[], recommended_index=None
            ),
        )
        outcome = proposal_service.create_proposal(
            conn,
            result=persisted.result,
            as_of=D5,
            proposed_by=OPERATOR,
            investigation_id=persisted.investigation_id,
        )
        assert outcome.status == "SKIPPED"
        assert outcome.reason == proposal_service.NO_RECOMMENDED_OPTION
        # 🔴 제안은 없고 조사는 남는다.
        assert _proposals(conn) == ()
        assert len(_stored(conn)) == 1

    def test_a_refused_proposal_does_not_erase_the_investigation(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §32 — 살아 있는 제안이 이미 있어 두 번째가 안 서도 조사 둘 다 남는다."""
        _ready(conn)
        first = _persist(conn)
        assert proposal_service.create_proposal(
            conn,
            result=first.result,
            as_of=D5,
            proposed_by=OPERATOR,
            investigation_id=first.investigation_id,
        ).created

        # ★ **다른 안**이어야 한다 — 같은 수량으로 다시 조사하면 지문이 같아
        #   `REUSED`(재시도)다 — 그것도 정상이고, 여기서 보려는 것은 **충돌** 쪽이다.
        second = _persist(conn, as_of=D6, finalize_fn=_report(qty_kg=200))
        outcome = proposal_service.create_proposal(
            conn,
            result=second.result,
            as_of=D6,
            proposed_by=OPERATOR,
            investigation_id=second.investigation_id,
        )
        assert outcome.status == "SKIPPED"
        assert outcome.reason.startswith(proposal_service.LIVE_PROPOSAL_EXISTS)
        assert len(_stored(conn)) == 2
        assert len(_proposals(conn)) == 1


# ══════════════════════════════════════════════════════════════════════════
#  §28 · §59 — 축은 DB 가 지킨다
# ══════════════════════════════════════════════════════════════════════════


class TestAxisIsEnforcedByTheDatabase:
    def test_a_proposal_cannot_point_at_an_investigation_that_is_not_there(
        self, conn: psycopg.Connection
    ) -> None:
        _exception(conn)
        _refuses(
            conn,
            _proposal_sql(investigation_id="INV-DOES-NOT-EXIST"),
            psycopg.errors.ForeignKeyViolation,
        )

    def test_a_proposal_cannot_point_at_another_runs_investigation(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §28 — RUN-A 의 제안이 RUN-B 의 조사를 가리킬 수 없다."""
        _exception(conn)
        _exception(conn, exception_id=OTHER_RUN_EXC, sim_run_id=OTHER_SIM)
        with conn.cursor() as cur:
            cur.execute(
                _investigation_sql(
                    investigation_id="INV-OTHER-RUN",
                    sim_run_id=OTHER_SIM,
                    exception_id=OTHER_RUN_EXC,
                )
            )
        conn.commit()
        _refuses(
            conn,
            _proposal_sql(investigation_id="INV-OTHER-RUN"),
            psycopg.errors.ForeignKeyViolation,
        )

    def test_a_proposal_cannot_point_at_another_problems_investigation(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §28 — EX-A 의 제안이 EX-B 의 조사를 가리킬 수 없다."""
        _exception(conn)
        _exception(conn, exception_id=OTHER_EXC, subject_id=OTHER_LOT)
        with conn.cursor() as cur:
            cur.execute(
                _investigation_sql(
                    investigation_id="INV-OTHER-EXC", exception_id=OTHER_EXC
                )
            )
        conn.commit()
        _refuses(
            conn,
            _proposal_sql(investigation_id="INV-OTHER-EXC"),
            psycopg.errors.ForeignKeyViolation,
        )

    def test_the_matching_axis_passes(self, conn: psycopg.Connection) -> None:
        """★ 반대편도 본다 — 제약이 정상 행까지 막으면 연결이 통째로 죽는다."""
        _exception(conn)
        with conn.cursor() as cur:
            cur.execute(_investigation_sql(investigation_id="INV-OK"))
            cur.execute(_proposal_sql(investigation_id="INV-OK"))
        conn.commit()
        (proposal,) = _proposals(conn)
        assert proposal.investigation_id == "INV-OK"

    def test_a_proposal_without_an_investigation_still_stands(
        self, conn: psycopg.Connection
    ) -> None:
        """⚠️ §27 — 손으로 세운 제안은 가리킬 조사가 없다. nullable 을 유지한다."""
        _exception(conn)
        with conn.cursor() as cur:
            cur.execute(_proposal_sql(investigation_id=None))
        conn.commit()
        (proposal,) = _proposals(conn)
        assert proposal.investigation_id is None

    def test_an_investigation_cannot_point_at_another_runs_problem(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §25 — 조사도 «같은 실행의» 문제만 가리킨다."""
        _exception(conn)
        _refuses(
            conn,
            _investigation_sql(investigation_id="INV-CROSS", sim_run_id=OTHER_SIM),
            psycopg.errors.ForeignKeyViolation,
        )

    def test_a_broken_audit_shape_is_refused(self, conn: psycopg.Connection) -> None:
        """🔴 trace 는 배열, 결과는 객체다 — 모양이 틀린 감사 기록은 행이 안 된다."""
        _exception(conn)
        _refuses(
            conn,
            _investigation_sql(investigation_id="INV-SHAPE").replace(
                "'[]'::jsonb", "'{}'::jsonb"
            ),
            psycopg.errors.CheckViolation,
        )

    def test_a_finish_reason_outside_the_runtime_vocabulary_is_refused(
        self, conn: psycopg.Connection
    ) -> None:
        _exception(conn)
        _refuses(
            conn,
            _investigation_sql(investigation_id="INV-WORD").replace(
                "'FINISHED'", "'ALL_GOOD'"
            ),
            psycopg.errors.CheckViolation,
        )


# ══════════════════════════════════════════════════════════════════════════
#  §35 · §66 — 다시 조사하면 새 실행이다
# ══════════════════════════════════════════════════════════════════════════


class TestRepeatedInvestigation:
    def test_investigating_the_same_problem_twice_on_one_day_keeps_both(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §66 — 두 번 조사했으면 두 실행이다. 유일 제약으로 접지 않는다."""
        _ready(conn)
        first = _persist(conn)
        second = _persist(conn)
        assert first.investigation_id != second.investigation_id
        stored = _stored(conn)
        assert len(stored) == 2
        assert {one.as_of for one in stored} == {D5}
        assert len({one.investigation_id for one in stored}) == 2

    def test_a_retried_save_does_not_double_the_record(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §36 — 같은 ID 로 **같은 내용**을 다시 저장한 것은 재시도다."""
        _ready(conn)
        first = _persist(conn)
        again = service.save_investigation(
            conn, result=first.result, investigation_id=first.investigation_id
        )
        assert again.status == "REUSED"
        assert len(_stored(conn)) == 1

    def test_the_same_name_with_another_story_is_refused(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §36 — 같은 ID 로 **다른 내용**이면 재시도가 아니다. 조용히 삼키지 않는다."""
        _ready(conn)
        first = _persist(conn)
        other = _investigate(
            conn,
            finalize_fn=lambda _view: InvestigationReport(
                summary="다른 결론", options=[], recommended_index=None
            ),
        )
        conn.rollback()
        with pytest.raises(service.InvestigationStateConflict) as caught:
            service.save_investigation(
                conn, result=other, investigation_id=first.investigation_id
            )
        assert caught.value.code == service.INVESTIGATION_PAYLOAD_CONFLICT
        assert len(_stored(conn)) == 1


# ══════════════════════════════════════════════════════════════════════════
#  §60 · §64 · §65 — 실패도 기록이다
# ══════════════════════════════════════════════════════════════════════════


class TestFailureAudit:
    def test_a_problem_that_is_not_there_is_still_recorded(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §60 — 정상 조회 + 못 찾음 = `NOT_FOUND`. 조사는 실제로 돌았다.

        ⚠️ Exception 행은 있어야 한다 — 감사 행이 그것을 FK 로 가리키기 때문이다.
           «그날 살아 있지 않았다» 는 조사 Runtime 이 `as_of` 로 판정한다.
        """
        _lot(conn)
        _exception(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TMP_SCHEMA}.logistics_exceptions"
                " SET status = 'RESOLVED', resolved_as_of = %s, resolved_by = 'TEST'"
                " WHERE exception_id = %s",
                (D1, EXC),
            )
        conn.commit()
        persisted = _persist(conn)
        assert persisted.result.finish_reason is FinishReason.NOT_FOUND
        (stored,) = _stored(conn)
        assert stored.finish_reason == "NOT_FOUND"
        assert stored.result["options"] == []
        assert _count(conn, "logistics_action_proposals") == 0

    def test_a_tool_that_broke_is_recorded_as_such(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §42 · §60 — «못 봤다» 는 «없다» 가 아니다. 그 구분이 그대로 저장된다.

        ★ 이 검사가 **되감기의 실제 근거**다. 조회 자체가 DB 오류로 터지면 트랜잭션이
          abort 상태로 남아 감사 INSERT 조차 못 한다 — wrapper 가 조사 뒤 한 번 되감기
          때문에 «조사가 실패했다» 는 사실이 살아남는다.
        """
        _ready(conn)
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {TMP_SCHEMA}.logistics_exceptions RENAME TO gone")
        persisted = _persist(conn)
        assert persisted.result.finish_reason is FinishReason.TOOL_FAILED
        (stored,) = _stored(conn)
        assert stored.finish_reason == "TOOL_FAILED"
        assert stored.finish_reason != "NOT_FOUND"
        # 🔴 무엇이 터졌는지가 trace 에 남는다.
        (call,) = stored.tool_trace
        assert call["tool_name"] == "get_open_exceptions"
        assert call["status"] == "TOOL_FAILED"
        assert call["detail"] is not None

    def test_a_deadline_that_passed_is_recorded(self, conn: psycopg.Connection) -> None:
        """🔴 §65 — 못 끝낸 조사를 «성공» 으로 바꾸지 않는다."""
        _ready(conn)
        persisted = _persist(conn, budget=InvestigationBudget(timeout_seconds=0.0))
        assert persisted.result.finish_reason is FinishReason.TIMEOUT
        (stored,) = _stored(conn)
        assert stored.finish_reason == "TIMEOUT"
        assert stored.result["counts"]["tool_calls"] == 0

    def test_a_provider_failure_changes_nothing_but_the_audit(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §22 · §64 — 감사 행 하나가 남을 뿐, 문제도 제안도 재고도 안 움직인다."""

        def _boom(_view: Any) -> Any:
            raise TimeoutError("provider timed out")

        _ready(conn)
        before = _snapshot(conn)
        persisted = _persist(conn, plan_fn=_boom)
        assert persisted.result.finish_reason is FinishReason.LLM_FAILED
        (stored,) = _stored(conn)
        assert stored.finish_reason == "LLM_FAILED"
        assert stored.llm_status == "FALLBACK"
        assert stored.llm_error_kind == "TIMEOUT"
        # 🔴 audit INSERT 하나뿐이다.
        assert _snapshot(conn) == before
        assert _count(conn, "logistics_action_proposals") == 0


# ══════════════════════════════════════════════════════════════════════════
#  §67 — 리셋 축
# ══════════════════════════════════════════════════════════════════════════


class TestSimResetAxis:
    def test_the_table_is_discovered_by_the_axis_column(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §24 · §67 — `reset_sim_run_ledger` 가 **스스로 찾는다.**

        ★ Master 를 고치지 않는다 — `_axis_tables` 가 `information_schema` 에서 이 칸을
          가진 BASE TABLE 을 읽으므로, 칸만 있으면 저절로 대상이 된다. 여기서는 그 조회를
          **그대로** 흉내 내어 새 표가 실제로 걸리는지 본다.
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT col.table_name
                FROM information_schema.columns AS col
                JOIN information_schema.tables AS tab
                  ON tab.table_schema = col.table_schema
                 AND tab.table_name = col.table_name
                WHERE col.table_schema = %s
                  AND col.column_name = %s
                  AND tab.table_type = 'BASE TABLE'
                """,
                [TMP_SCHEMA, AXIS_COLUMN],
            )
            found = {row["table_name"] for row in cur.fetchall()}
        assert "logistics_investigations" in found, sorted(found)
        assert {"logistics_exceptions", "logistics_action_proposals"} <= found

    def test_the_run_axis_is_not_null(self, conn: psycopg.Connection) -> None:
        _exception(conn)
        _refuses(
            conn,
            _investigation_sql(investigation_id="INV-NO-AXIS").replace(
                f"'{SIM}'", "NULL"
            ),
            psycopg.errors.NotNullViolation,
        )
