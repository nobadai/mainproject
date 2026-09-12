"""대응안과 사람의 승인 — **실제 PostgreSQL** (#628 Commit 5).

```text
원자성        제안 INSERT 와 Exception UPDATE 가 **함께** 성공하는가
승인 ≠ 실행   승인 뒤 재고·판매·매입 표가 한 줄이라도 바뀌는가
동시성        두 번 눌러도 한 번만 먹는가 (lost update)
과거 재현     D6 조회에 D8 의 결정이 새어 나오는가
제약          DB 가 «날짜 없는 승인» · «끝난 날 둘» · «빈 승인자» 를 막는가
격리          남의 실행 제안이 보이는가
```

🔴 **가짜로는 원자성을 못 잰다.** 스텁을 꽂으면 *"우리가 롤백을 불렀다"* 까지만 확인되고,
   *"롤백이 실제로 행을 지웠다"* 는 확인이 안 된다. §54 가 요구하는 것은 뒤엣것이다.

⚠️ **이 파일만 임시 스키마를 «커밋» 한다.** 형제 검사들(`…_exceptions_db` ·
   `…_investigation_db`)은 통째로 롤백해서 흔적을 안 남기는데, 여기서 재는 대상이
   **커밋과 롤백 그 자체**라 같은 방법을 못 쓴다 — 서비스가 커밋하는 순간 스키마까지
   공유 DB 에 굳어 버리고, 서비스가 롤백하면 스키마 생성까지 함께 날아간다.
   그래서 스키마를 먼저 커밋해 두고 **끝나면 `DROP SCHEMA … CASCADE`** 로 치운다.
   시작할 때도 `DROP SCHEMA IF EXISTS` 를 한 번 돌려, 앞선 실행이 죽으며 남긴 잔해가
   있어도 스스로 복구한다.
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
from app.logistics.agent import proposal_service as service
from app.logistics.agent import proposals as repository
from app.logistics.agent.exceptions import open_exception
from app.logistics.agent.graph import run_investigation
from app.logistics.agent.investigation import (
    EvaluatedOption,
    FinishReason,
    InvestigationOption,
    InvestigationReport,
    InvestigationResult,
    InvestigationStep,
    ToolCallRecord,
    ToolCallStatus,
)
from app.logistics.agent.schemas import (
    FRESHNESS_PRESSURE,
    ExceptionEvidence,
    ExceptionRow,
)
from app.logistics.agent.tools import ActionImpact
from app.logistics.db import get_connection

pytestmark = pytest.mark.db

TMP_SCHEMA = "logistics_agent_proposal_verify"
SIM = "SIM-PROPOSAL"
OTHER_SIM = "SIM-PROPOSAL-OTHER"
BAECHU = "ITEM-BAECHU"
MU = "ITEM-MU"
ZONE = "COLD_HUMID_0_3"
LIMIT_DAYS = 10
UNIT_COST = Decimal(1200)

#: ```text
#: D1  입고 1,000kg → D5 출고 300 → D8 출고 200 → 잔량 500
#: D5  조사 · 제안
#: D8  사람의 결정
#: ```
D1 = date(2026, 1, 1)
D5 = date(2026, 1, 5)
D6 = date(2026, 1, 6)
D8 = date(2026, 1, 8)
D9 = date(2026, 1, 9)

EXC = "EXC-FRESHNESS-1"
LOT = "LOT-BAECHU"
OPERATOR = "operator"

DB_DIR = Path(__file__).resolve().parents[3] / "database"

#: 🔴 **승인이 절대 건드리면 안 되는** 표들 — `APPROVED ≠ EXECUTED` 의 증거다.
WATCHED_TABLES = (
    "inventory_lots",
    "inventory_moves",
    "inventory_reservations",
    "inventory_allocations",
    "inbound_schedules",
    "sales",
    "purchase_items",
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
        # 🔴 **여기서 커밋한다** — 이 파일이 재는 것이 서비스의 커밋/롤백이라서다.
        connection.commit()
        for module in (
            turnover,
            historical_repository,
            exception_repo,
            inbound_schedules,
            repository,
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
    ledger = [("IN", "1000", D1), ("OUT", "300", D5), ("OUT", "200", D8)]
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


def _exception(
    conn: psycopg.Connection, *, exception_id: str = EXC, sim_run_id: str = SIM
) -> None:
    open_exception(
        conn,
        row=ExceptionRow(
            exception_id=exception_id,
            sim_run_id=sim_run_id,
            code=FRESHNESS_PRESSURE,
            subject_type="LOT",
            subject_id=LOT,
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
                    source_id=LOT,
                ),
            ),
            detector_version="v1",
        ),
    )
    conn.commit()


def _impact(feasibility: str = "FEASIBLE") -> ActionImpact:
    return ActionImpact(
        sim_run_id=SIM,
        as_of=D5,
        observed_as_of=D1,
        action="SALES_PRIORITY_REQUEST",
        feasibility=feasibility,  # type: ignore[arg-type]
        affected_kg=None,
        capacity_delta_kg=None,
        candidate_kg=Decimal(500),
        estimated_loss_krw=None,
        freshness_days_left=2,
    )


def _result(
    *,
    action: str = "SALES_PRIORITY_REQUEST",
    parameters: dict[str, Any] | None = None,
    recommended_index: int | None = 0,
    observed_as_of: date | None = D1,
    exception_id: str = EXC,
    sim_run_id: str = SIM,
) -> InvestigationResult:
    """조사 결과 한 벌. ★ 그래프는 `test_logistics_agent_graph` 가 잰다 — 여기서는
    **저장 계약**만 본다.
    """
    option = EvaluatedOption(
        action=action,
        parameters={"lot_id": LOT} if parameters is None else parameters,
        rationale="신선도 압박이라 우선 판매 후보로 올린다.",
        evidence_refs=(1,),
        decision_owner="SALES",
        parameters_are_hypothesis=True,
        impact=_impact(),
    )
    return InvestigationResult(
        sim_run_id=sim_run_id,
        as_of=D5,
        exception_id=exception_id,
        finish_reason=FinishReason.FINISHED,
        llm_status="SUCCESS",
        options=(option,),
        recommended_index=recommended_index,
        observed_as_of=observed_as_of,
        tool_calls=(
            ToolCallRecord(
                sequence=1,
                tool_name="get_lot",
                arguments={"lot_id": LOT},
                status=ToolCallStatus.SUCCESS,
                observed_as_of=D1,
            ),
        ),
    )


def _create(conn: psycopg.Connection, **kwargs: Any) -> service.ProposalOutcome:
    return service.create_proposal(
        conn, result=_result(**kwargs), as_of=D5, proposed_by=OPERATOR
    )


def _rows(conn: psycopg.Connection, sim_run_id: str = SIM) -> tuple[Any, ...]:
    return repository.select_proposals(conn, sim_run_id=sim_run_id)


def _exception_row(conn: psycopg.Connection, exception_id: str = EXC) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT status, proposed_as_of FROM {TMP_SCHEMA}.logistics_exceptions"
            " WHERE exception_id = %s",
            (exception_id,),
        )
        return dict(cur.fetchone())


def _snapshot(conn: psycopg.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """감시 대상 표를 **통째로** 뜬다 — 줄 수만 세면 UPDATE 를 못 잡는다."""
    taken: dict[str, list[tuple[Any, ...]]] = {}
    with conn.cursor() as cur:
        for table in WATCHED_TABLES:
            cur.execute(f"SELECT * FROM {TMP_SCHEMA}.{table} ORDER BY 1")
            taken[table] = [tuple(str(value) for value in row) for row in cur.fetchall()]
    return taken


def _refuses(conn: psycopg.Connection, statement: str, error: type[Exception]) -> None:
    """DB 가 직접 막는가. ⚠️ 터진 뒤에는 트랜잭션이 abort 라 되돌려 놔야 다음이 돈다."""
    with pytest.raises(error), conn.cursor() as cur:
        cur.execute(statement)
    conn.rollback()


# ══════════════════════════════════════════════════════════════════════════
#  생성 — 제안 한 건과 Exception 전이는 **함께** 일어난다
# ══════════════════════════════════════════════════════════════════════════


class TestCreate:
    def test_an_accepted_recommendation_becomes_one_proposed_row(
        self, conn: psycopg.Connection
    ) -> None:
        _exception(conn)
        outcome = _create(conn)
        assert outcome.created

        (row,) = _rows(conn)
        assert row.status == "PROPOSED"
        assert row.action_type == "SALES_PRIORITY_REQUEST"
        assert row.decision_owner == "SALES"
        assert row.proposed_as_of == D5
        assert row.proposed_by == OPERATOR
        # 🔴 조사가 낸 관측일 그대로 (§22).
        assert row.observed_as_of == D1
        assert row.approved_as_of is None
        assert row.approved_by is None

    def test_the_exception_moves_to_proposed_with_its_date(
        self, conn: psycopg.Connection
    ) -> None:
        """§8 · §23 — 상태만 바꾸고 날짜를 안 남기면 D5 의 상태를 못 센다."""
        _exception(conn)
        assert _exception_row(conn) == {"status": "OPEN", "proposed_as_of": None}
        _create(conn)
        assert _exception_row(conn) == {"status": "PROPOSED", "proposed_as_of": D5}

    def test_the_numbers_are_the_tool_answer_not_a_new_calculation(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §51 — `candidate_kg` 는 문자열로 실린다. `float` 을 지나면 값이 흔들린다."""
        _exception(conn)
        _create(conn)
        (row,) = _rows(conn)
        assert row.impact["candidate_kg"] == "500"
        assert row.impact["feasibility"] == "FEASIBLE"
        assert row.evidence_refs[0]["tool_name"] == "get_lot"

    def test_no_recommendation_leaves_the_exception_untouched(
        self, conn: psycopg.Connection
    ) -> None:
        """§53 — 제안 0 건이면 Exception 상태 변경도 0 이다."""
        _exception(conn)
        outcome = _create(conn, recommended_index=None)
        assert outcome.status == "SKIPPED"
        assert _rows(conn) == ()
        assert _exception_row(conn) == {"status": "OPEN", "proposed_as_of": None}

    def test_a_retry_does_not_create_a_second_row(self, conn: psycopg.Connection) -> None:
        """§12 — 같은 지문의 살아 있는 제안이 있으면 기존 것을 돌려준다."""
        _exception(conn)
        first = _create(conn)
        again = _create(conn)
        assert again.status == "REUSED"
        assert again.proposal is not None
        assert first.proposal is not None
        assert again.proposal.proposal_id == first.proposal.proposal_id
        assert len(_rows(conn)) == 1

    def test_a_different_proposal_cannot_join_a_live_one(
        self, conn: psycopg.Connection
    ) -> None:
        """§47 · §48 — 승인 대기 제안이 둘이면 사람이 무엇을 승인하는지 모른다."""
        _exception(conn)
        _create(conn)
        outcome = _create(conn, action="ACCEPT_RISK")
        assert outcome.status == "SKIPPED"
        assert outcome.reason.startswith(service.LIVE_PROPOSAL_EXISTS)
        assert len(_rows(conn)) == 1

    def test_the_database_itself_refuses_two_live_proposals(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 응용 코드의 조회가 한 번 빠지는 날에도 장부는 안 갈린다."""
        _exception(conn)
        _create(conn)
        _refuses(
            conn,
            f"""INSERT INTO {TMP_SCHEMA}.logistics_action_proposals (
                    proposal_id, sim_run_id, exception_id, proposal_key, action_type,
                    decision_owner, parameters_json, impact_json, evidence_refs_json,
                    status, proposed_as_of, proposed_by
                ) VALUES ('PRP-DUP', '{SIM}', '{EXC}', 'k', 'ACCEPT_RISK', 'LOGISTICS',
                    '{{}}'::jsonb, '{{}}'::jsonb, '[]'::jsonb, 'PROPOSED', '{D5}', 'x')""",
            psycopg.errors.UniqueViolation,
        )

    def test_a_closed_exception_gets_no_proposal(self, conn: psycopg.Connection) -> None:
        _exception(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TMP_SCHEMA}.logistics_exceptions"
                f" SET status = 'RESOLVED', resolved_as_of = '{D5}' WHERE exception_id = %s",
                (EXC,),
            )
        conn.commit()
        outcome = _create(conn)
        assert outcome.status == "SKIPPED"
        assert outcome.reason == f"{service.EXCEPTION_NOT_LIVE}:RESOLVED"
        assert _rows(conn) == ()

    def test_the_first_proposal_date_survives_the_second_proposal(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §8 — 두 번째 제안이 첫 제안이 선 날을 덮으면 그 사실이 사라진다."""
        _exception(conn)
        first = _create(conn)
        assert first.proposal is not None
        service.reject_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=first.proposal.proposal_id,
            as_of=D6,
            rejected_by=OPERATOR,
            rejection_reason="다른 안을 보고 싶다",
        )
        assert _exception_row(conn)["status"] == "OPEN"
        # ⚠️ 거절로 OPEN 이 됐어도 «처음 제안된 날» 은 남아 있다.
        assert _exception_row(conn)["proposed_as_of"] == D5

        second = service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by=OPERATOR
        )
        assert second.created
        assert _exception_row(conn) == {"status": "PROPOSED", "proposed_as_of": D5}

    def test_a_superseding_proposal_links_to_the_one_it_replaced(
        self, conn: psycopg.Connection
    ) -> None:
        """§29 — **자동 대체는 없다.** 부르는 쪽이 명시할 때만 일어난다."""
        _exception(conn)
        first = _create(conn)
        assert first.proposal is not None
        replacement = service.create_proposal(
            conn,
            result=_result(action="ACCEPT_RISK", parameters={"lot_id": LOT}),
            as_of=D5,
            proposed_by=OPERATOR,
            supersedes=first.proposal.proposal_id,
        )
        assert replacement.created
        assert replacement.proposal is not None

        old = repository.select_proposal(
            conn, sim_run_id=SIM, proposal_id=first.proposal.proposal_id
        )
        assert old is not None
        assert old.status == "SUPERSEDED"
        assert old.superseded_as_of == D5
        assert replacement.proposal.previous_proposal_id == first.proposal.proposal_id
        # ★ 대체 뒤에도 살아 있는 제안은 여전히 하나다.
        assert len(repository.live_proposals_for(conn, sim_run_id=SIM, exception_id=EXC)) == 1


class TestAtomicity:
    """§24 · §25 — **반쪽 상태를 안 남긴다.**"""

    def test_a_failing_exception_update_leaves_no_proposal(
        self, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """제안은 들어갔는데 Exception 전이가 터졌다 → **둘 다 없어야 한다.**"""
        _exception(conn)

        def broken(conn_: Any, **_: Any) -> int:
            with conn_.cursor() as cur:
                cur.execute(f"UPDATE {TMP_SCHEMA}.표가_없다 SET status = 'PROPOSED'")
            return 1

        monkeypatch.setattr(repository, "mark_exception_proposed", broken)
        with pytest.raises(psycopg.errors.UndefinedTable):
            _create(conn)

        assert _rows(conn) == ()
        assert _exception_row(conn) == {"status": "OPEN", "proposed_as_of": None}

    def test_a_failing_proposal_insert_leaves_the_exception_open(
        self, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """반대 방향 — 제안이 안 섰으면 Exception 도 안 움직인다."""
        _exception(conn)
        _create(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TMP_SCHEMA}.logistics_exceptions SET status = 'OPEN',"
                " proposed_as_of = NULL WHERE exception_id = %s",
                (EXC,),
            )
            cur.execute(
                f"UPDATE {TMP_SCHEMA}.logistics_action_proposals"
                f" SET status = 'REJECTED', rejected_as_of = '{D5}', rejected_by = 'x',"
                " rejection_reason = 'y'"
            )
        conn.commit()
        standing = _rows(conn)[0].proposal_id

        # 🔴 같은 이름으로 또 넣으려 한다 → PK 충돌.
        monkeypatch.setattr(repository, "next_proposal_id", lambda *_, **__: standing)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _create(conn)

        assert len(_rows(conn)) == 1
        assert _exception_row(conn) == {"status": "OPEN", "proposed_as_of": None}


# ══════════════════════════════════════════════════════════════════════════
#  사람의 결정
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def proposed(conn: psycopg.Connection) -> str:
    _exception(conn)
    outcome = _create(conn)
    assert outcome.proposal is not None
    return outcome.proposal.proposal_id


class TestApprove:
    def test_approval_records_who_and_when(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        row = service.approve_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            as_of=D8,
            approved_by=OPERATOR,
            note="신선도가 더 급하다",
        )
        assert row.status == "APPROVED"
        assert row.approved_as_of == D8
        assert row.approved_by == OPERATOR
        assert row.approval_note == "신선도가 더 급하다"

    def test_approval_changes_no_business_table(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """🔴 **`APPROVED` 는 `EXECUTED` 가 아니다** (§4 · §36 · §55).

        판매도 매입도 폐기도 안 일어났다 — 사람이 *"진행해도 좋다"* 고 말했을 뿐이다.
        """
        _lot(conn)
        conn.commit()
        before = _snapshot(conn)
        service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
        )
        assert _snapshot(conn) == before

    def test_approval_does_not_close_the_exception(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """§26 · §28 — 문제의 상태와 대응의 상태를 섞지 않는다."""
        service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
        )
        assert _exception_row(conn) == {"status": "PROPOSED", "proposed_as_of": D5}

    def test_the_same_approval_twice_is_one_approval(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """§38 — 네트워크 재시도를 충돌로 돌려보내면 사람이 또 누른다."""
        first = service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
        )
        second = service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
        )
        assert second.approved_as_of == first.approved_as_of
        assert len(_rows(conn)) == 1

    def test_another_persons_approval_is_a_conflict(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
        )
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.approve_proposal(
                conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by="다른사람"
            )
        assert caught.value.code == "ALREADY_APPROVED"

    def test_a_rejected_proposal_cannot_be_approved(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """§58 — 이미 끝난 결정을 뒤집지 않는다."""
        service.reject_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            as_of=D8,
            rejected_by=OPERATOR,
            rejection_reason="근거가 약하다",
        )
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.approve_proposal(
                conn, sim_run_id=SIM, proposal_id=proposed, as_of=D9, approved_by=OPERATOR
            )
        assert caught.value.code == "STATE_CONFLICT"

    def test_only_one_of_two_simultaneous_approvals_takes(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """🔴 §37 — 상태 조건이 UPDATE 안에 있어야 lost update 를 막는다.

        ⚠️ 커넥션 둘로는 못 잰다(임시 스키마가 이 커넥션의 것이다). 대신 **경합의
           본질**을 직접 잰다: 같은 UPDATE 를 두 번 돌리면 두 번째는 0 행이다.
        """
        first = repository.transition_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            to_status="APPROVED",
            as_of=D8,
            actor=OPERATOR,
        )
        second = repository.transition_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            to_status="APPROVED",
            as_of=D9,
            actor="다른사람",
        )
        conn.rollback()
        assert (first, second) == (1, 0)

    def test_a_race_that_lands_the_same_decision_is_idempotent(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """읽은 뒤 남이 **같은 결정**을 먼저 넣었다 → 충돌이 아니라 그 결과를 돌려준다."""
        seen = repository.select_proposal(conn, sim_run_id=SIM, proposal_id=proposed)
        repository.transition_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            to_status="APPROVED",
            as_of=D8,
            actor=OPERATOR,
        )
        conn.commit()
        # 🔴 뒤늦게 도착한 요청이 **낡은 행**을 들고 들어온다.
        stale = [seen]
        original = repository.select_proposal

        def once(*args: Any, **kwargs: Any) -> Any:
            return stale.pop() if stale else original(*args, **kwargs)

        repository.select_proposal = once  # type: ignore[assignment]
        try:
            row = service.approve_proposal(
                conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
            )
        finally:
            repository.select_proposal = original  # type: ignore[assignment]
        assert row.status == "APPROVED"
        assert row.approved_by == OPERATOR


class TestReject:
    def test_rejection_records_the_reason_and_reopens_the_exception(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """§27 — *"문제는 그대로인데 대응안이 없다"* 가 정확한 상태다."""
        row = service.reject_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            as_of=D8,
            rejected_by=OPERATOR,
            rejection_reason="가격 기준 불명확",
        )
        assert row.status == "REJECTED"
        assert row.rejected_as_of == D8
        assert row.rejection_reason == "가격 기준 불명확"
        assert _exception_row(conn) == {"status": "OPEN", "proposed_as_of": D5}

    def test_a_rejection_without_a_reason_is_refused(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """🔴 사유가 없으면 다음 조사가 같은 안을 또 올린다."""
        with pytest.raises(ValueError, match="rejection_reason"):
            service.reject_proposal(
                conn,
                sim_run_id=SIM,
                proposal_id=proposed,
                as_of=D8,
                rejected_by=OPERATOR,
                rejection_reason="   ",
            )
        assert _rows(conn)[0].status == "PROPOSED"

    def test_a_live_proposal_keeps_the_exception_proposed(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """⚠️ 살아 있는 제안이 남아 있으면 `OPEN` 으로 안 돌린다.

        ★ Core 에서는 부분 유일 인덱스가 «한 문제에 살아 있는 제안 하나» 를 강제하므로
          이 갈래가 production 흐름에서는 안 난다 — 그래서 방어 자체를 직접 부른다.
        """
        service._reopen_if_nothing_lives(conn, sim_run_id=SIM, exception_id=EXC)
        conn.commit()
        assert _exception_row(conn)["status"] == "PROPOSED"

    def test_a_resolved_exception_is_not_resurrected(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """🔴 재탐지가 이미 닫은 문제를 제안 거절이 다시 열지 않는다."""
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TMP_SCHEMA}.logistics_exceptions SET status = 'RESOLVED',"
                f" resolved_as_of = '{D6}', resolved_by = 'REDETECT' WHERE exception_id = %s",
                (EXC,),
            )
        conn.commit()
        service.reject_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            as_of=D8,
            rejected_by=OPERATOR,
            rejection_reason="필요 없어졌다",
        )
        assert _exception_row(conn)["status"] == "RESOLVED"

    def test_the_same_rejection_twice_is_one_rejection(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        kwargs: dict[str, Any] = {
            "sim_run_id": SIM,
            "proposal_id": proposed,
            "as_of": D8,
            "rejected_by": OPERATOR,
            "rejection_reason": "가격 기준 불명확",
        }
        first = service.reject_proposal(conn, **kwargs)
        second = service.reject_proposal(conn, **kwargs)
        assert first.rejected_as_of == second.rejected_as_of

    def test_a_rejection_with_different_content_is_a_conflict(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """§39 — 같은 결정이 아니면 재시도가 아니다."""
        service.reject_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            as_of=D8,
            rejected_by=OPERATOR,
            rejection_reason="가격 기준 불명확",
        )
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.reject_proposal(
                conn,
                sim_run_id=SIM,
                proposal_id=proposed,
                as_of=D8,
                rejected_by=OPERATOR,
                rejection_reason="아주 다른 사유",
            )
        assert caught.value.code == "ALREADY_REJECTED"


class TestExpire:
    def test_expiry_closes_the_proposal_and_reopens_the_exception(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """§30 — 시간 기반 자동 작업이 아니다. 부르는 쪽이 «오늘» 을 들고 온다."""
        row = service.expire_proposal(conn, sim_run_id=SIM, proposal_id=proposed, as_of=D9)
        assert row.status == "EXPIRED"
        assert row.expired_as_of == D9
        # ⚠️ 사람이 내린 결정이 아니므로 행위자 칸은 비어 있다.
        assert row.approved_by is None
        assert row.rejected_by is None
        assert _exception_row(conn)["status"] == "OPEN"


# ══════════════════════════════════════════════════════════════════════════
#  과거 재현 — 🔴 미래 detail 이 과거로 새지 않는다
# ══════════════════════════════════════════════════════════════════════════


class TestHistorical:
    def test_a_proposal_does_not_exist_before_the_day_it_was_proposed(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        assert service.get_proposal(conn, sim_run_id=SIM, proposal_id=proposed, as_of=D1) is None

    def test_the_day_before_approval_shows_no_approver(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """§59 — D5 제안 · D8 승인 → D6 조회는 `PROPOSED` 이고 승인자가 없다."""
        service.approve_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            as_of=D8,
            approved_by=OPERATOR,
            note="확인",
        )
        at_date = service.get_proposal(conn, sim_run_id=SIM, proposal_id=proposed, as_of=D6)
        assert at_date is not None
        assert at_date.status == "PROPOSED"
        assert at_date.approved_by is None
        assert at_date.approved_as_of is None
        assert at_date.approval_note is None

    def test_the_day_after_approval_shows_the_approver(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
        )
        at_date = service.get_proposal(conn, sim_run_id=SIM, proposal_id=proposed, as_of=D9)
        assert at_date is not None
        assert at_date.status == "APPROVED"
        assert at_date.approved_by == OPERATOR

    def test_a_rejection_detail_does_not_leak_into_the_past(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """🔴 §42 — D6 조회에 D8 의 거절 사유가 보이면 그날 없던 사실이 과거에 생긴다."""
        service.reject_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=proposed,
            as_of=D8,
            rejected_by=OPERATOR,
            rejection_reason="가격 기준 불명확",
        )
        at_date = service.get_proposal(conn, sim_run_id=SIM, proposal_id=proposed, as_of=D6)
        assert at_date is not None
        assert at_date.rejection_reason is None
        assert at_date.rejected_by is None
        assert at_date.rejected_as_of is None

    def test_the_listing_filters_by_the_status_of_that_day(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=proposed, as_of=D8, approved_by=OPERATOR
        )
        assert len(service.list_proposals(conn, sim_run_id=SIM, as_of=D1)) == 0
        waiting = service.list_proposals(conn, sim_run_id=SIM, as_of=D6, statuses=["PROPOSED"])
        assert [one.proposal_id for one in waiting] == [proposed]
        assert service.list_proposals(conn, sim_run_id=SIM, as_of=D6, statuses=["APPROVED"]) == ()
        decided = service.list_proposals(conn, sim_run_id=SIM, as_of=D9, statuses=["APPROVED"])
        assert [one.proposal_id for one in decided] == [proposed]


class TestRunIsolation:
    """§60 — 다른 실행의 제안이 섞이면 안 된다."""

    def test_another_run_sees_nothing(self, conn: psycopg.Connection, proposed: str) -> None:
        assert _rows(conn, OTHER_SIM) == ()
        assert (
            service.get_proposal(
                conn, sim_run_id=OTHER_SIM, proposal_id=proposed, as_of=D9
            )
            is None
        )

    def test_deciding_with_the_wrong_run_is_not_found(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        with pytest.raises(service.ProposalNotFound):
            service.approve_proposal(
                conn,
                sim_run_id=OTHER_SIM,
                proposal_id=proposed,
                as_of=D8,
                approved_by=OPERATOR,
            )


# ══════════════════════════════════════════════════════════════════════════
#  제약 — 응용 코드가 한 번 빠져도 DB 가 막는다
# ══════════════════════════════════════════════════════════════════════════


class TestConstraints:
    def _update(self, fields: str) -> str:
        return (
            f"UPDATE {TMP_SCHEMA}.logistics_action_proposals SET {fields}"
        )

    def test_an_approval_without_a_date_is_refused(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        _refuses(
            conn,
            self._update("status = 'APPROVED', approved_by = 'x'"),
            psycopg.errors.CheckViolation,
        )

    def test_a_rejection_without_a_reason_is_refused(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        _refuses(
            conn,
            self._update(f"status = 'REJECTED', rejected_as_of = '{D8}', rejected_by = 'x'"),
            psycopg.errors.CheckViolation,
        )

    def test_two_terminal_dates_are_refused(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """🔴 §41 의 전제 — 끝난 날이 둘이면 그날 상태를 못 고른다."""
        _refuses(
            conn,
            self._update(
                f"status = 'APPROVED', approved_as_of = '{D8}', approved_by = 'x',"
                f" expired_as_of = '{D9}'"
            ),
            psycopg.errors.CheckViolation,
        )

    def test_an_empty_actor_is_refused(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        """🔴 `''` 는 «모른다» 를 «있다» 로 위장한다."""
        _refuses(
            conn,
            self._update(f"status = 'APPROVED', approved_as_of = '{D8}', approved_by = '  '"),
            psycopg.errors.CheckViolation,
        )

    def test_a_decision_before_the_proposal_is_refused(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        _refuses(
            conn,
            self._update(f"status = 'APPROVED', approved_as_of = '{D1}', approved_by = 'x'"),
            psycopg.errors.CheckViolation,
        )

    def test_an_action_outside_the_catalogue_is_refused(
        self, conn: psycopg.Connection, proposed: str
    ) -> None:
        _refuses(conn, self._update("action_type = 'ZONE_MOVE'"), psycopg.errors.CheckViolation)

    def test_a_proposal_without_an_impact_is_refused(self, conn: psycopg.Connection) -> None:
        """★ Exception 의 «근거 없는 행은 없다» 와 같은 규율이다."""
        _exception(conn)
        _refuses(
            conn,
            f"""INSERT INTO {TMP_SCHEMA}.logistics_action_proposals (
                    proposal_id, sim_run_id, exception_id, proposal_key, action_type,
                    decision_owner, parameters_json, impact_json, evidence_refs_json,
                    status, proposed_as_of, proposed_by
                ) VALUES ('PRP-NAKED', '{SIM}', '{EXC}', 'k', 'ACCEPT_RISK', 'LOGISTICS',
                    '{{}}'::jsonb, 'null'::jsonb, '[]'::jsonb, 'PROPOSED', '{D5}', 'x')""",
            psycopg.errors.CheckViolation,
        )

    def test_a_proposed_exception_without_its_date_is_refused(
        self, conn: psycopg.Connection
    ) -> None:
        """§9 — 상태만 있고 날짜가 없으면 과거를 못 센다."""
        _exception(conn)
        _refuses(
            conn,
            f"UPDATE {TMP_SCHEMA}.logistics_exceptions SET status = 'PROPOSED'"
            f" WHERE exception_id = '{EXC}'",
            psycopg.errors.CheckViolation,
        )


# ══════════════════════════════════════════════════════════════════════════
#  조사 → 제안 — 🔴 실제 Tool · 실제 표로 한 번은 끝까지
# ══════════════════════════════════════════════════════════════════════════


class TestFromRealInvestigation:
    def _investigate(self, conn: psycopg.Connection, **parameters: Any) -> InvestigationResult:
        option = InvestigationOption(
            action="SALES_PRIORITY_REQUEST",
            parameters={"lot_id": LOT, **parameters},
            rationale="우선 판매 후보",
            evidence_refs=[1],
        )
        return run_investigation(
            conn,
            sim_run_id=SIM,
            as_of=D5,
            exception_id=EXC,
            plan_fn=lambda _view: InvestigationStep(action="FINISH", reason="충분하다"),
            finalize_fn=lambda _view: InvestigationReport(
                summary="신선도 압박", options=[option], recommended_index=0
            ),
        )

    def test_an_unmeasured_impact_still_reaches_a_human(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 §15 — 수량을 안 준 우선판매 요청은 `UNRESOLVED` 다. **그대로 저장한다.**

        ```text
        물류가 대는 것    candidate_kg = 700  ← D5 원장이 낸 미약정 잔량
                                              (입고 1,000 − D5 출고 300. D8 출고는 아직이다)
        물류가 안 대는 것  얼마를 팔지          ← 영업이 정한다 (§9.1)
        ```
        """
        _lot(conn)
        _exception(conn)
        result = self._investigate(conn)
        assert result.recommended_index == 0

        outcome = service.create_proposal(
            conn, result=result, as_of=D5, proposed_by=OPERATOR
        )
        assert outcome.created
        (row,) = _rows(conn)
        assert row.impact["feasibility"] == "UNRESOLVED"
        assert row.impact["candidate_kg"] == "700.000000"
        # 🔴 숫자를 지어내 메우지 않았다 — «못 쟀다» 가 그대로 사람에게 간다.
        assert row.parameters == {"lot_id": LOT}
        assert row.decision_owner == "SALES"

    def test_an_impossible_quantity_never_reaches_a_human(
        self, conn: psycopg.Connection
    ) -> None:
        """🔴 계산기가 **재 보고 «안 된다»** 고 답한 안은 승인 화면에 안 올린다."""
        _lot(conn)
        _exception(conn)
        result = self._investigate(conn, qty_kg=99999)
        assert result.options[0].impact is not None
        assert result.options[0].impact.feasibility == "INFEASIBLE"

        outcome = service.create_proposal(
            conn, result=result, as_of=D5, proposed_by=OPERATOR
        )
        assert outcome.status == "SKIPPED"
        assert outcome.reason == service.IMPACT_INFEASIBLE
        assert _rows(conn) == ()
        assert _exception_row(conn) == {"status": "OPEN", "proposed_as_of": None}

    def test_a_measurable_quantity_becomes_an_approvable_proposal(
        self, conn: psycopg.Connection
    ) -> None:
        _lot(conn)
        _exception(conn)
        result = self._investigate(conn, qty_kg=300)
        outcome = service.create_proposal(
            conn, result=result, as_of=D5, proposed_by=OPERATOR
        )
        assert outcome.created
        assert outcome.proposal is not None

        before = _snapshot(conn)
        approved = service.approve_proposal(
            conn,
            sim_run_id=SIM,
            proposal_id=outcome.proposal.proposal_id,
            as_of=D8,
            approved_by=OPERATOR,
        )
        assert approved.status == "APPROVED"
        assert approved.impact["feasibility"] == "FEASIBLE"
        assert approved.impact["affected_kg"] == "300"
        # 🔴 **승인 뒤에도 창고는 그대로다.**
        assert _snapshot(conn) == before
