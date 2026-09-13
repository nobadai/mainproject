"""조사 실행 기록 — **DB 없이 잴 수 있는 것들** (#628 Commit 7).

```text
무엇을 남기나        호출 trace 는 «무엇을 물었나» 까지 — Tool 답 전체는 안 남긴다
무엇을 안 만드나     저장하며 숫자를 새로 셈하지 않고 Tool 도 LLM 도 안 부른다
이름                 DB 를 안 보고 짓는다 (채번 경합 없음)
트랜잭션의 주인       조사가 터지면 가짜 행을 안 만든다
표를 누가 쓰나        소스를 읽어 막는다
DDL 계약             어휘가 Enum 과 같은가 · 같은 날 두 번 조사가 막히지 않는가
```

🔴 **실제 PostgreSQL 로 재는 것은 여기 없다** — FK 축·감사 행·재조사·업무 표 무변경은
   `test_logistics_agent_investigation_persistence_db.py` 가 실제 표로 잰다.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics.agent import (
    investigation_repository,
    proposal_service,
    proposals,
    tool_dispatch,
    tools,
)
from app.logistics.agent import investigation_repository as repository
from app.logistics.agent import investigation_service as service
from app.logistics.agent.investigation import (
    ACTION_DECISION_OWNERS,
    EvaluatedOption,
    FinishReason,
    InvestigationResult,
    ToolCallRecord,
    ToolCallStatus,
)
from app.logistics.agent.proposals import ProposalRow
from app.logistics.agent.tools import ActionImpact, CapacityContext, LotFact, LotView
from app.logistics.llm.schemas import LLMStatus
from app.master.sim_run_open import AXIS_COLUMN

SIM = "SIM-INVESTIGATION"
EXC = "EX-SIM-INVESTIGATION-FRESHNESS_PRESSURE-LOT-1-20260101"
LOT = "LOT-1"
INV = "INV-11111111-1111-1111-1111-111111111111"

D1 = date(2026, 1, 1)
D5 = D1 + timedelta(days=4)

DDL = (
    Path(__file__).resolve().parents[3] / "database" / "40_logistics_agent_schema.sql"
).read_text(encoding="utf-8")

#: «이 검사는 안 준다» 를 «저장본이 없다» 와 가르는 자리.
_MISSING = object()


def _must_not_look_up(*_: Any, **__: Any) -> Any:
    raise AssertionError("조사를 안 댔는데 조사 기록을 찾아봤다")


#: 🔴 **큰 Tool 답 안에만 있는 값.** 이 문자열이 저장 payload 어디에도 안 나와야 한다 —
#:    키 이름(`answer`)만 보면 칸 이름이 바뀌는 날 검사가 조용히 통과한다.
WINDOW_MARK = Decimal("12345.6789")


# ── 준비 도우미 ─────────────────────────────────────────────────────────


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


def _lot_view() -> LotView:
    """진짜 `LotView`. 🔴 가짜 모양으로 재면 칸 이름이 바뀌어도 안 걸린다."""
    return LotView(
        sim_run_id=SIM,
        as_of=D5,
        observed_as_of=D1,
        uncertainties=("COMMITMENT_UNRESOLVED",),
        lot=LotFact(
            lot_id=LOT,
            item_id="ITEM-BAECHU",
            item="배추",
            grade=None,
            storage_zone="COLD_HUMID_0_3",
            status="ACTIVE",
            received_at=D1,
            remaining_qty_kg=Decimal(700),
            unit_cost_krw_per_kg=Decimal(1200),
            remaining_freshness_days=2,
            effective_freshness_limit_days=10,
            turnover_status="SELL_PRIORITY",
            sell_priority=True,
            sell_priority_remaining_days=3,
            disposal_candidate=False,
            committed_kg=Decimal(200),
            uncommitted_kg=Decimal(500),
            remaining_qty_observed_as_of=D1,
            status_observed_as_of=D1,
        ),
    )


def _capacity_view() -> CapacityContext:
    """🔴 **18일 창을 통째로 들고 있는 바로 그 타입.** 이것이 복사되면 안 된다."""
    return CapacityContext(
        sim_run_id=SIM,
        as_of=D5,
        observed_as_of=None,
        used_kg=Decimal(900),
        guaranteed_kg=Decimal(1000),
        burst_kg=Decimal(200),
        available_kg=Decimal(100),
        window_usage_ratio=Decimal("0.90"),
        cap_by_date={D1 + timedelta(days=offset): WINDOW_MARK for offset in range(18)},
        inbound_lead_days=2,
        capacity_tight_ratio=Decimal("0.90"),
        capacity_basis="CURRENT_ACTIVE_POLICY",
    )


def _option(
    *,
    action: str = "SALES_PRIORITY_REQUEST",
    impact: ActionImpact | None = None,
    rejected_reason: str | None = None,
) -> EvaluatedOption:
    owner = ACTION_DECISION_OWNERS.get(action, "LOGISTICS")
    return EvaluatedOption(
        action=action,
        parameters={"lot_id": LOT},
        rationale="신선도 압박이라 우선 판매 후보로 올린다.",
        evidence_refs=(1,),
        decision_owner=owner,
        parameters_are_hypothesis=owner != "LOGISTICS",
        impact=_impact() if impact is None and rejected_reason is None else impact,
        rejected_reason=rejected_reason,
    )


def _calls() -> tuple[ToolCallRecord, ...]:
    """성공 · 실패 · 거부 셋. 🔴 **세 종류가 다 남아야 한다.**"""
    return (
        ToolCallRecord(
            sequence=1,
            tool_name="get_lot",
            arguments={"lot_id": LOT},
            status=ToolCallStatus.SUCCESS,
            # 🔴 그때 Tool 이 낸 답 그대로 — **이것이 저장되면 안 된다.**
            answer=_lot_view(),
            observed_as_of=D1,
            uncertainties=("COMMITMENT_UNRESOLVED",),
            reason="이 Lot 의 잔량을 본다",
        ),
        ToolCallRecord(
            sequence=2,
            tool_name="get_capacity_context",
            arguments={},
            status=ToolCallStatus.SUCCESS,
            answer=_capacity_view(),
            observed_as_of=None,
            reason="창고 여유를 본다",
        ),
        ToolCallRecord(
            sequence=3,
            tool_name="get_item_lots",
            arguments={"item_id": "ITEM-MU"},
            status=ToolCallStatus.REJECTED,
            detail="SUBJECT_OUT_OF_SCOPE:ITEM-MU",
            reason="다른 품목도 볼까",
        ),
        ToolCallRecord(
            sequence=4,
            tool_name="get_policy",
            arguments={},
            status=ToolCallStatus.FAILED,
            detail="TOOL_FAILED:get_policy:UndefinedTable",
            reason="정책을 본다",
        ),
    )


def _result(
    *,
    options: tuple[EvaluatedOption, ...] | None = None,
    recommended_index: int | None = 0,
    observed_as_of: date | None = D1,
    finish_reason: FinishReason = FinishReason.FINISHED,
    llm_status: LLMStatus = "SUCCESS",
    llm_error_kind: str | None = None,
    tool_calls: tuple[ToolCallRecord, ...] | None = None,
    summary: str = "잔량 500kg 중 미확정 500kg 이다.",
) -> InvestigationResult:
    return InvestigationResult(
        sim_run_id=SIM,
        as_of=D5,
        exception_id=EXC,
        finish_reason=finish_reason,
        llm_status=llm_status,
        llm_error_kind=llm_error_kind,  # type: ignore[arg-type]
        summary=summary,
        findings=("신선도가 2일 남았다",),
        missing_or_uncertain=("예약 축의 관측일을 못 댄다",),
        options=(_option(),) if options is None else options,
        recommended_index=recommended_index,
        observed_as_of=observed_as_of,
        uncertainties=("COMMITMENT_UNRESOLVED",),
        tool_calls=_calls() if tool_calls is None else tool_calls,
        tool_call_count=4,
        planned_tool_call_count=2,
        replan_count=1,
        llm_call_count=3,
    )


class _FakeCursor:
    def __init__(self, owner: _FakeConn) -> None:
        self._owner = owner
        self.rowcount = 1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: Any, **__: Any) -> None:
        self._owner.statements += 1

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeConn:
    """커밋과 롤백만 센다. 🔴 **SQL 을 흉내 내지 않는다** — 그건 실제 표가 잰다."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.statements = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def conn() -> _FakeConn:
    return _FakeConn()


def _payload(result: InvestigationResult) -> str:
    """저장될 두 칸을 **한 문자열로** 편다 — 키 이름이 아니라 값까지 뒤진다."""
    return json.dumps(
        {
            "trace": list(service.snapshot_tool_trace(result)),
            "result": dict(service.snapshot_investigation_result(result)),
        },
        ensure_ascii=False,
        default=str,
    )


def _keys(node: Any) -> set[str]:
    """중첩된 곳까지 **모든 키 이름**을 긁는다."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key))
            found |= _keys(value)
    elif isinstance(node, list | tuple):
        for value in node:
            found |= _keys(value)
    return found


# ══════════════════════════════════════════════════════════════════════════
#  Tool trace — 🔴 **무엇을 물었나까지다**
# ══════════════════════════════════════════════════════════════════════════


class TestToolTrace:
    def test_the_tool_answer_is_not_stored(self) -> None:
        """🔴 Tool 답 전체를 조사마다 복사하면 감사 기록이 조회 캐시가 된다 (§9)."""
        result = _result()
        trace = service.snapshot_tool_trace(result)
        assert "answer" not in _keys(list(trace))
        # ★ 키 이름만 보지 않는다 — 18일 창의 값 자체가 어디에도 없어야 한다.
        assert str(WINDOW_MARK) not in _payload(result)
        # 그러면서 «그 Tool 을 불렀다» 는 사실은 남는다.
        assert [entry["tool_name"] for entry in trace] == [
            "get_lot",
            "get_capacity_context",
            "get_item_lots",
            "get_policy",
        ]

    def test_the_fields_are_exactly_the_agreed_ones(self) -> None:
        """★ 칸을 손으로 고른다 — 계약이 넓어져도 이 표는 안 넓어진다."""
        for entry in service.snapshot_tool_trace(_result()):
            assert set(entry) == set(service.TRACE_FIELDS)

    def test_the_metadata_of_each_call_is_kept(self) -> None:
        first = service.snapshot_tool_trace(_result())[0]
        assert first["sequence"] == 1
        assert first["tool_name"] == "get_lot"
        assert first["arguments"] == {"lot_id": LOT}
        assert first["status"] == "TOOL_SUCCESS"
        assert first["reason"] == "이 Lot 의 잔량을 본다"
        assert first["detail"] is None
        assert first["observed_as_of"] == "2026-01-01"
        assert first["uncertainties"] == ["COMMITMENT_UNRESOLVED"]

    def test_a_rejected_call_is_kept_with_its_reason(self) -> None:
        """🔴 *"물어봤는데 막혔다"* 와 *"아예 안 물었다"* 는 다른 사실이다."""
        rejected = service.snapshot_tool_trace(_result())[2]
        assert rejected["status"] == "TOOL_REJECTED"
        assert rejected["detail"] == "SUBJECT_OUT_OF_SCOPE:ITEM-MU"

    def test_a_failed_call_is_kept_with_its_reason(self) -> None:
        failed = service.snapshot_tool_trace(_result())[3]
        assert failed["status"] == "TOOL_FAILED"
        assert failed["detail"] == "TOOL_FAILED:get_policy:UndefinedTable"

    def test_an_observation_date_the_tool_could_not_give_stays_empty(self) -> None:
        """🔴 «못 쟀다» 를 `as_of` 로 메우지 않는다 (§39)."""
        assert service.snapshot_tool_trace(_result())[1]["observed_as_of"] is None


# ══════════════════════════════════════════════════════════════════════════
#  최종 판단 — 🔴 **보존하되 새로 만들지 않는다**
# ══════════════════════════════════════════════════════════════════════════


class TestResultSnapshot:
    def test_the_recommended_action_is_restorable(self) -> None:
        stored = service.snapshot_investigation_result(_result())
        assert stored["recommended_index"] == 0
        (option,) = stored["options"]
        assert option["action"] == "SALES_PRIORITY_REQUEST"
        assert option["parameters"] == {"lot_id": LOT}
        # 🔴 누가 결정하는가는 결정론이 정한 값이고 그대로 남는다.
        assert option["decision_owner"] == "SALES"
        assert option["parameters_are_hypothesis"] is True

    def test_the_impact_is_the_tool_answer_not_a_new_number(self) -> None:
        """🔴 §8 — 저장하며 다시 셈하는 값이 하나도 없다. 문자열로 실린다(float 금지)."""
        (option,) = service.snapshot_investigation_result(_result())["options"]
        assert option["impact"]["feasibility"] == "FEASIBLE"
        assert option["impact"]["candidate_kg"] == "500"

    def test_a_rejected_candidate_keeps_why_it_was_dropped(self) -> None:
        result = _result(
            options=(_option(rejected_reason="ACTION_UNSUPPORTED:run_sql"),),
            recommended_index=None,
        )
        (option,) = service.snapshot_investigation_result(result)["options"]
        assert option["rejected_reason"] == "ACTION_UNSUPPORTED:run_sql"
        assert option["accepted"] is False

    def test_the_two_axes_are_not_merged(self) -> None:
        """🔴 §41 — `finish_reason`(어디서 끝났나)과 `llm_status`(AI 가 판단했나)."""
        stored = service.snapshot_investigation_result(
            _result(
                finish_reason=FinishReason.BUDGET_EXCEEDED,
                llm_status="SUCCESS",
            )
        )
        assert stored["finish_reason"] == "BUDGET_EXCEEDED"
        assert stored["llm_status"] == "SUCCESS"

    def test_a_provider_failure_keeps_its_kind(self) -> None:
        stored = service.snapshot_investigation_result(
            _result(
                finish_reason=FinishReason.LLM_FAILED,
                llm_status="FALLBACK",
                llm_error_kind="TIMEOUT",
            )
        )
        assert stored["finish_reason"] == "LLM_FAILED"
        assert stored["llm_error_kind"] == "TIMEOUT"

    def test_the_words_of_the_investigation_are_kept(self) -> None:
        stored = service.snapshot_investigation_result(_result())
        assert stored["summary"] == "잔량 500kg 중 미확정 500kg 이다."
        assert stored["findings"] == ["신선도가 2일 남았다"]
        assert stored["missing_or_uncertain"] == ["예약 축의 관측일을 못 댄다"]
        assert stored["uncertainties"] == ["COMMITMENT_UNRESOLVED"]

    def test_the_observation_date_is_carried_verbatim(self) -> None:
        assert service.snapshot_investigation_result(_result())["observed_as_of"] == "2026-01-01"

    def test_an_unknown_observation_date_stays_unknown(self) -> None:
        """🔴 §39 — `None` 이면 `None` 이다. `as_of` 로도 오늘로도 안 메운다."""
        stored = service.snapshot_investigation_result(_result(observed_as_of=None))
        assert stored["observed_as_of"] is None
        assert stored["as_of"] == "2026-01-05"

    def test_the_budget_counts_are_carried_not_recounted(self) -> None:
        """★ Runtime 이 이미 센 값이다 — 저장하며 trace 길이로 다시 세지 않는다."""
        stored = service.snapshot_investigation_result(_result())
        assert stored["counts"] == {
            "tool_calls": 4,
            "planned_tool_calls": 2,
            "replans": 1,
            "llm_calls": 3,
        }

    def test_the_tool_answers_do_not_sneak_in_through_the_result(self) -> None:
        """🔴 `jsonable(result)` 를 통째로 쓰면 `tool_calls[].answer` 가 여기로 온다."""
        stored = service.snapshot_investigation_result(_result())
        assert "tool_calls" not in stored
        assert "answer" not in _keys(dict(stored))


# ══════════════════════════════════════════════════════════════════════════
#  이름 — 🔴 **DB 를 안 본다**
# ══════════════════════════════════════════════════════════════════════════


class TestIdentifier:
    def test_a_new_id_never_touches_the_database(self) -> None:
        """🔴 §37 — `COUNT(*)+1` · `MAX(n)+1` 은 두 조사가 동시에 서면 같은 이름을 낸다.

        ★ 커넥션을 **아예 안 받는다** — 받을 자리가 없으면 세는 길도 없다.
        """
        assert "conn" not in repository.new_investigation_id.__code__.co_varnames
        assert repository.new_investigation_id().startswith("INV-")

    def test_two_investigations_never_share_a_name(self) -> None:
        names = {repository.new_investigation_id() for _ in range(200)}
        assert len(names) == 200

    def test_the_prefix_separates_it_from_proposals_and_exceptions(self) -> None:
        identifier = repository.new_investigation_id()
        assert identifier.startswith(repository.INVESTIGATION_ID_PREFIX)
        assert not identifier.startswith(("PRP-", "EX-"))


# ══════════════════════════════════════════════════════════════════════════
#  트랜잭션의 주인 — 🔴 **가짜 조사 기록을 안 만든다**
# ══════════════════════════════════════════════════════════════════════════


def _stub_insert(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    saved: list[Any] = []
    monkeypatch.setattr(
        repository,
        "insert_investigation",
        lambda _conn, *, row: saved.append(row) or row,
    )
    return saved


class TestTransactionOwnership:
    def test_saving_commits_once(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = _stub_insert(monkeypatch)
        outcome = service.save_investigation(conn, result=_result(), investigation_id=INV)
        assert outcome.status == "SAVED"
        assert outcome.investigation_id == INV
        assert conn.commits == 1
        assert conn.rollbacks == 0
        assert [row.investigation_id for row in saved] == [INV]

    def test_a_failing_insert_rolls_back_and_does_not_swallow(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("표가 없다")

        monkeypatch.setattr(repository, "insert_investigation", _boom)
        with pytest.raises(RuntimeError, match="표가 없다"):
            service.save_investigation(conn, result=_result())
        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_a_broken_investigation_leaves_no_row(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §21 — 결과를 못 얻었으면 `finish_reason='ERROR'` 를 지어내지 않는다."""
        saved = _stub_insert(monkeypatch)

        def _boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("커넥션이 죽었다")

        monkeypatch.setattr(service, "run_investigation", _boom)
        with pytest.raises(RuntimeError, match="커넥션이 죽었다"):
            service.run_and_persist_investigation(
                conn, sim_run_id=SIM, as_of=D5, exception_id=EXC
            )
        assert saved == []
        assert conn.commits == 0
        assert conn.rollbacks == 1

    def test_the_audit_row_survives_a_database_error_inside_the_investigation(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 감사 INSERT 앞에서 **한 번 되감는다.**

        조사가 DB 오류로 터지면(`TOOL_FAILED`) 트랜잭션이 abort 상태로 남아 감사 INSERT
        조차 못 한다 — 그러면 *"조사가 실패했다"* 는 사실만 통째로 사라진다. 조사는 쓴
        것이 없으므로(§16) 되감아도 잃는 것이 없다.
        """
        saved = _stub_insert(monkeypatch)
        monkeypatch.setattr(
            service,
            "run_investigation",
            lambda *_, **__: _result(finish_reason=FinishReason.TOOL_FAILED),
        )
        outcome = service.run_and_persist_investigation(
            conn, sim_run_id=SIM, as_of=D5, exception_id=EXC
        )
        assert outcome.row.finish_reason == "TOOL_FAILED"
        assert [row.finish_reason for row in saved] == ["TOOL_FAILED"]
        # ★ 되감기 한 번 + 커밋 한 번.
        assert conn.rollbacks == 1
        assert conn.commits == 1

    def test_the_business_day_and_the_axis_come_from_the_investigation(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §38 — 업무 날짜는 Runtime 이 받은 `as_of` 다. 오늘 날짜가 아니다."""
        saved = _stub_insert(monkeypatch)
        service.save_investigation(conn, result=_result())
        (row,) = saved
        assert row.as_of == D5
        assert row.sim_run_id == SIM
        assert row.exception_id == EXC
        assert row.observed_as_of == D1


# ══════════════════════════════════════════════════════════════════════════
#  저장이 새 판단을 만들지 않는다
# ══════════════════════════════════════════════════════════════════════════


def _must_not_run(*_: Any, **__: Any) -> Any:
    raise AssertionError("조사를 저장하며 Tool 을 불렀다")


class TestNoNewJudgement:
    def test_no_tool_is_called_while_storing(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §8 · §44 — 저장은 감사이지 조사가 아니다."""
        _stub_insert(monkeypatch)
        monkeypatch.setattr(tool_dispatch, "run_tool", _must_not_run)
        for name in tools.__all__:
            attribute = getattr(tools, name, None)
            if callable(attribute) and not isinstance(attribute, type):
                monkeypatch.setattr(tools, name, _must_not_run)
        service.save_investigation(conn, result=_result())

    def test_the_runtime_is_not_re_entered_while_storing(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ `save_investigation` 은 **이미 끝난** 결과만 옮긴다."""
        _stub_insert(monkeypatch)
        monkeypatch.setattr(service, "run_investigation", _must_not_run)
        service.save_investigation(conn, result=_result())


# ══════════════════════════════════════════════════════════════════════════
#  역할 경계 — 소스를 읽어 막는다
# ══════════════════════════════════════════════════════════════════════════


def _code_strings(module: Any) -> list[str]:
    """그 모듈의 문자열 **상수**만. 🔴 설명(docstring)은 뺀다.

    ⚠️ 빼지 않으면 *"UPDATE 를 안 한다"* 고 적은 **설명 문장 자체**에 검사가 걸린다 —
       규율을 적어 둔 것이 규율 위반으로 읽히는 자리다.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    described = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in described
    ]


class TestRoleBoundary:
    """🔴 **이 표를 쓰는 자리가 하나뿐이어야 한다.**

    ⚠️ 소스를 읽어 막는다 — 경로를 밟아서 잡으려면 **밟지 않은 분기의 우회는 영원히
       안 보인다** (`proposals` 의 같은 검사와 같은 규율).
    """

    WRITES = ("INSERT INTO", "UPDATE ", "DELETE FROM")
    TABLE = "logistics_investigations"

    def _writers(self) -> set[str]:
        root = Path(repository.__file__).resolve().parents[3]
        found: set[str] = set()
        for path in (root / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if self.TABLE in node.value and any(
                    verb in node.value for verb in self.WRITES
                ):
                    found.add(path.name)
        return found

    def test_only_the_investigation_repository_writes_the_table(self) -> None:
        assert self._writers() == {"investigation_repository.py"}, self._writers()

    def test_the_record_is_never_updated(self) -> None:
        """🔴 §23 — 조사는 **끝난 실행**이다. 다시 조사하면 새 행이지 덮어쓰기가 아니다."""
        statements = _code_strings(repository)
        assert not any("UPDATE " in one for one in statements), statements
        assert not any("DELETE FROM" in one for one in statements), statements

    def test_the_service_owns_the_transaction_and_the_repository_does_not(self) -> None:
        """🔴 §19 — 저장소는 커밋도 롤백도 안 한다."""
        tree = ast.parse(Path(repository.__file__).read_text(encoding="utf-8"))
        called = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback"}
        ]
        assert called == []

    def test_the_repository_does_not_run_investigations(self) -> None:
        """🔴 §19 — 저장하다가 조사를 다시 돌리는 길이 없어야 한다.

        ⚠️ 소스에서 글자를 찾지 않는다 — 그러면 *"여기서 조사를 안 돌린다"* 고 적은
           **설명 문장**에 걸린다. 보는 것은 **부르는 이름**이다.
        """
        tree = ast.parse(Path(repository.__file__).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any("graph" in one for one in imported), imported
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        } | {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "run_investigation" not in called, sorted(called)

    def test_no_llm_is_reachable_from_the_persistence_path(self) -> None:
        """🔴 §44 — «조사 결과를 요약해 줘» 를 모델에게 묻지 않는다. snapshot 은 결정론이다."""
        for module in (repository, service):
            source = Path(module.__file__).read_text(encoding="utf-8")
            modules = [
                node.module
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom) and node.module is not None
            ]
            assert not any("llm" in one for one in modules), (module.__name__, modules)


# ══════════════════════════════════════════════════════════════════════════
#  DDL 계약 — 어휘와 축을 파일에서 읽어 잰다
# ══════════════════════════════════════════════════════════════════════════


def _check_vocabulary(constraint: str) -> set[str]:
    """그 CHECK 안의 따옴표 어휘를 긁는다."""
    match = re.search(rf"CONSTRAINT {constraint}\s+CHECK \(([^;]*?)\)\),", DDL, re.DOTALL)
    assert match is not None, constraint
    return set(re.findall(r"'([A-Z_]+)'", match.group(1)))


class TestSchemaContract:
    def test_the_finish_reason_vocabulary_matches_the_runtime(self) -> None:
        """🔴 DB CHECK 과 `FinishReason` 이 **글자 그대로** 같아야 한다."""
        assert _check_vocabulary("ck_logistics_investigations_finish_reason") == {
            reason.value for reason in FinishReason
        }

    def test_the_llm_status_vocabulary_matches_the_contract(self) -> None:
        assert _check_vocabulary("ck_logistics_investigations_llm_status") == {
            "SUCCESS",
            "SKIPPED_TEMPLATE",
            "FALLBACK",
            "DISABLED",
        }

    def test_the_table_carries_the_reset_axis(self) -> None:
        """🔴 §24 · §67 — 이 칸이 있어서 `--reset` 이 표를 **자동으로 발견**한다.

        ★ Master 를 고치지 않는다 — `_axis_tables` 가 `information_schema` 에서 이 칸을
          가진 표를 읽으므로, 칸만 있으면 저절로 따라온다.
        """
        assert AXIS_COLUMN == "sim_run_id"
        body = DDL[DDL.index("CREATE TABLE IF NOT EXISTS haetdeul.logistics_investigations") :]
        assert f"    {AXIS_COLUMN}            TEXT NOT NULL," in body[: body.index(");")]

    def test_investigating_the_same_problem_twice_a_day_is_not_blocked(self) -> None:
        """🔴 §6 · §35 — 두 번 조사했으면 두 실행이다. 유일 제약으로 접지 않는다."""
        assert "UNIQUE (sim_run_id, exception_id, as_of)" not in DDL
        assert (
            "UNIQUE (sim_run_id, exception_id, investigation_id)" in DDL
        ), "복합 FK 가 가리킬 자리가 없다"

    def test_the_proposal_points_at_the_same_run_and_problem(self) -> None:
        """🔴 §26 — 실행·문제 축까지 묶어서 가리킨다. 홑 FK 로는 못 막는다."""
        assert DDL.count("logistics_action_proposals_investigation_axis_fkey") >= 3
        assert (
            "FOREIGN KEY (sim_run_id, exception_id, investigation_id)\n"
            "        REFERENCES haetdeul.logistics_investigations" in DDL
        )

    def test_the_existing_execution_history_table_is_left_alone(self) -> None:
        """🔴 §3 · §4 · §68 — 기존 실행이력 표를 늘려 쓰지 않았다.

        ```text
        logistics_agent_runs   Logistics API Request/Response 이력 (cycle ∈ PROCUREMENT·SALES)
        agent_runs             범용 감사로그 — **남의 표**고 exception_id 칸이 없다
        ```

        ★ 그 둘 중 어느 쪽도 이 파일이 건드리지 않는다 — 이름조차 안 나온다.
        """
        body = re.sub(r"(?m)^--.*$", "", DDL)
        # ⚠️ 설명(COMMENT)에는 «왜 저 표를 안 썼나» 가 적혀 있다 — 그것은 이 표를 건드리는
        #    문장이 아니다. 그래서 **표를 겨누는 동사**만 본다.
        touched = re.findall(
            r"(?:CREATE|ALTER|DROP|INSERT INTO|UPDATE|DELETE FROM)[^;']*agent_runs", body
        )
        assert touched == [], touched
        # ★ 이 파일이 남의 표에 하는 일은 sim_runs 를 FK 로 가리키는 것뿐이다.
        assert "DROP TABLE" not in body
        assert "DROP CONSTRAINT" not in body

    def test_the_investigation_only_points_at_its_own_run(self) -> None:
        assert (
            "CONSTRAINT logistics_investigations_exception_axis_fkey\n"
            "        FOREIGN KEY (sim_run_id, exception_id)" in DDL
        )


# ══════════════════════════════════════════════════════════════════════════
#  제안과의 연결 — 🔴 **기존 생성 로직은 안 바뀐다**
# ══════════════════════════════════════════════════════════════════════════


def _proposal(**overrides: Any) -> ProposalRow:
    """이미 서 있는 제안 한 줄. 🔴 진짜 타입을 쓴다 — 칸 이름이 곧 계약이다."""
    fields: dict[str, Any] = {
        "proposal_id": f"PRP-{EXC}-1",
        "sim_run_id": SIM,
        "exception_id": EXC,
        "investigation_id": INV,
        "proposal_key": proposals.proposal_key_for(
            sim_run_id=SIM,
            exception_id=EXC,
            action_type="SALES_PRIORITY_REQUEST",
            parameters={"lot_id": LOT},
        ),
        "status": "PROPOSED",
        "action_type": "SALES_PRIORITY_REQUEST",
        "decision_owner": "SALES",
        "parameters": {"lot_id": LOT},
        "impact": {"feasibility": "FEASIBLE"},
        "evidence_refs": (),
        "proposed_as_of": D5,
        "proposed_by": "operator",
    }
    fields.update(overrides)
    return ProposalRow(**fields)


def _stub_proposal_repository(
    monkeypatch: pytest.MonkeyPatch,
    *,
    history: tuple[ProposalRow, ...] = (),
    investigation: Any = _MISSING,
) -> dict[str, list[Any]]:
    """제안 저장소와 **조사 조회**를 함께 갈아 끼운다.

    ⚠️ `investigation` 기본값은 «저장본이 없다» 가 아니라 «이 검사는 안 준다» 이다 —
       `None` 을 기본으로 두면 *"조사가 저장돼 있지 않다"* 를 시험할 방법이 사라진다.
    """
    calls: dict[str, list[Any]] = {"insert": [], "mark": [], "transition": []}
    monkeypatch.setattr(proposals, "exception_status", lambda *_, **__: "OPEN")
    monkeypatch.setattr(proposals, "select_proposals", lambda *_, **__: history)
    monkeypatch.setattr(proposals, "live_proposals_for", lambda *_, **__: ())
    monkeypatch.setattr(proposals, "next_proposal_id", lambda *_, **__: f"PRP-{EXC}-2")
    monkeypatch.setattr(
        proposals, "insert_proposal", lambda _conn, *, row: calls["insert"].append(row) or row
    )
    monkeypatch.setattr(
        proposals, "mark_exception_proposed", lambda *_, **__: calls["mark"].append(1) or 1
    )
    # 🔴 **대체는 쓰기다.** 조사 대조가 이것보다 먼저인지 재려면 여기를 세야 한다.
    monkeypatch.setattr(
        proposals,
        "transition_proposal",
        lambda *_, **kwargs: calls["transition"].append(kwargs) or None,
    )
    stored = (
        service.investigation_row_for(_result(), investigation_id=INV)
        if investigation is _MISSING
        else investigation
    )
    monkeypatch.setattr(
        investigation_repository, "select_investigation", lambda *_, **__: stored
    )
    return calls


class TestProposalLinkage:
    def test_the_proposal_remembers_which_investigation_made_it(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_proposal_repository(monkeypatch)
        outcome = proposal_service.create_proposal(
            conn,
            result=_result(),
            as_of=D5,
            proposed_by="operator",
            investigation_id=INV,
        )
        assert outcome.created
        assert [row.investigation_id for row in calls["insert"]] == [INV]

    def test_a_proposal_raised_by_hand_has_no_investigation(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ 🔴 nullable 을 유지한다. 없는 조사를 지어내 채우지 않는다.

        ★ 조사를 안 댔으면 **대조도 안 한다** — 조회 자체가 일어나면 터지게 해 둔다.
        """
        calls = _stub_proposal_repository(monkeypatch, investigation=None)
        monkeypatch.setattr(
            investigation_repository, "select_investigation", _must_not_look_up
        )
        proposal_service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator"
        )
        assert [row.investigation_id for row in calls["insert"]] == [None]

    def test_the_matching_investigation_is_looked_up_on_this_run(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 조사를 **이 실행 축으로** 찾는다 — 남의 실행 기록이 보이면 안 된다."""
        asked: list[Any] = []
        _stub_proposal_repository(monkeypatch)
        row = service.investigation_row_for(_result(), investigation_id=INV)

        def _looked(_conn: Any, **kwargs: Any) -> Any:
            asked.append(kwargs)
            return row

        monkeypatch.setattr(investigation_repository, "select_investigation", _looked)
        proposal_service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator", investigation_id=INV
        )
        assert asked == [{"sim_run_id": SIM, "investigation_id": INV}]

    def test_the_fingerprint_does_not_change_with_the_investigation(self) -> None:
        """🔴 §76 — 조사 ID 를 지문에 섞으면 재시도를 아무것도 못 막는다."""
        arguments = {
            "sim_run_id": SIM,
            "exception_id": EXC,
            "action_type": "SALES_PRIORITY_REQUEST",
            "parameters": {"lot_id": LOT},
        }
        assert INV not in proposals.proposal_key_for(**arguments)
        source = Path(proposals.__file__).read_text(encoding="utf-8")
        body = source[source.index("def proposal_key_for(") : source.index("def next_proposal_id(")]
        assert "investigation" not in body.split('"""')[2]


# ══════════════════════════════════════════════════════════════════════════
#  🔴 조사 ↔ 결과 대조 — **FK 가 못 막는 자리**
# ══════════════════════════════════════════════════════════════════════════

#: 저장본과 **한 칸씩** 어긋나게 만드는 변주들. 한 칸만 달라도 다른 조사다.
_DIVERGENCES: dict[str, dict[str, Any]] = {
    "finish_reason": {"finish_reason": FinishReason.BUDGET_EXCEEDED},
    "llm_status": {"llm_status": "FALLBACK"},
    "observed_as_of": {"observed_as_of": None},
    "tool_trace": {"tool_calls": ()},
    "result_json": {"summary": "다른 조사가 낸 다른 요약이다."},
}


class TestInvestigationIdentity:
    """🔴 **복합 FK 는 «같은 실행 · 같은 문제» 까지만 본다.**

    ```text
    INV-A   같은 문제를 조사해서 우선판매로 끝났다
    INV-B   같은 문제를 다시 조사해서 폐기로 끝났다   ← 둘 다 정상이다 (§35)

    create_proposal(result=B, investigation_id="INV-A")   → FK 통과 🔴
    ```

    그러면 장부에는 A 라고 적혔는데 action·parameters·impact·근거는 B 에서 온 제안이
    선다 — *"이 대응안은 어느 조사에서 나왔나"* 에 DB 가 **거짓으로 답한다.**
    """

    def test_an_investigation_that_was_never_stored_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_proposal_repository(monkeypatch, investigation=None)
        with pytest.raises(proposal_service.ProposalStateConflict) as caught:
            proposal_service.create_proposal(
                conn, result=_result(), as_of=D5, proposed_by="operator", investigation_id=INV
            )
        assert caught.value.code == proposal_service.INVESTIGATION_NOT_FOUND
        assert calls["insert"] == []
        assert calls["mark"] == []
        assert conn.commits == 0

    @pytest.mark.parametrize("field", sorted(_DIVERGENCES))
    def test_one_different_field_is_already_another_investigation(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, field: str
    ) -> None:
        """🔴 한 칸만 달라도 **다른 조사다.** 일부만 보면 그만큼 거짓이 통과한다."""
        calls = _stub_proposal_repository(monkeypatch)
        with pytest.raises(proposal_service.ProposalStateConflict) as caught:
            proposal_service.create_proposal(
                conn,
                result=_result(**_DIVERGENCES[field]),
                as_of=D5,
                proposed_by="operator",
                investigation_id=INV,
            )
        assert caught.value.code == proposal_service.INVESTIGATION_RESULT_MISMATCH
        assert calls["insert"] == []
        assert calls["mark"] == []

    def test_the_same_result_passes(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 반대편도 본다 — 대조가 과하면 정상 연결이 통째로 막힌다."""
        calls = _stub_proposal_repository(monkeypatch)
        outcome = proposal_service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator", investigation_id=INV
        )
        assert outcome.created
        assert len(calls["insert"]) == 1

    def test_nothing_is_superseded_before_the_investigation_is_checked(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **대조가 모든 쓰기보다 먼저다.**

        뒤로 밀면 `_supersede` 가 남의 제안을 먼저 닫아 놓고 나서 «그 조사가 아니다» 를
        알게 된다 — 그 제안은 이미 잃었고, 잃었다는 사실은 어디에도 안 적힌다.
        """
        standing = _proposal(investigation_id=None)
        calls = _stub_proposal_repository(monkeypatch, history=(standing,))
        with pytest.raises(proposal_service.ProposalStateConflict) as caught:
            proposal_service.create_proposal(
                conn,
                result=_result(summary="다른 조사"),
                as_of=D5,
                proposed_by="operator",
                investigation_id=INV,
                supersedes=standing.proposal_id,
            )
        assert caught.value.code == proposal_service.INVESTIGATION_RESULT_MISMATCH
        assert calls["transition"] == []
        assert calls["insert"] == []
        assert conn.commits == 0

    def test_the_comparison_reuses_the_audit_snapshot(self) -> None:
        """🔴 §9 — 제안 전용 지문을 새로 셈하지 않는다. 같은 함수를 지난다."""
        source = Path(proposal_service.__file__).read_text(encoding="utf-8")
        body = source[
            source.index("def _check_investigation(") : source.index(
                "def _settle_investigation_reuse("
            )
        ]
        assert "same_investigation_audit" in body
        assert "investigation_row_for" in body
        # 대조하려고 Tool 을 다시 돌리거나 새 해시를 만들지 않는다.
        assert "sha256" not in body
        assert "run_tool" not in body


# ══════════════════════════════════════════════════════════════════════════
#  🔴 한 조사에 대응안 하나
# ══════════════════════════════════════════════════════════════════════════


class TestOneInvestigationOneProposal:
    """🔴 **한 조사는 한 번 끝난 판단이고, 그 판단이 고른 안은 하나다.**

    그 조사로 제안을 둘 만들 수 있으면 끝난 판단 하나가 승인 대상 둘을 낳는다.
    """

    def test_the_same_request_is_still_a_retry(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 기존 멱등 계약은 그대로다 — 같은 안을 한 번 더 보낸 것은 재시도다."""
        standing = _proposal()
        calls = _stub_proposal_repository(monkeypatch, history=(standing,))
        outcome = proposal_service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator", investigation_id=INV
        )
        assert outcome.status == "REUSED"
        assert outcome.proposal is standing
        assert calls["insert"] == []

    def test_a_finished_proposal_still_holds_the_investigation(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **거절된 뒤에도 그 조사는 이미 썼다.**

        «살아 있는 제안» 만 보면 `REJECTED` 는 안 보이고, 그 틈으로 같은 조사가 둘째
        제안을 낸다. 거절은 그 조사의 결론이 안 받아들여졌다는 뜻이지 조사를 안 한 것이
        아니다 — 새 안이 필요하면 **새 조사**를 돌린다.
        """
        finished = _proposal(
            status="REJECTED",
            rejected_as_of=D5,
            rejected_by="operator",
            rejection_reason="지금은 팔지 않는다",
        )
        calls = _stub_proposal_repository(monkeypatch, history=(finished,))
        with pytest.raises(proposal_service.ProposalStateConflict) as caught:
            proposal_service.create_proposal(
                conn, result=_result(), as_of=D5, proposed_by="operator", investigation_id=INV
            )
        assert caught.value.code == proposal_service.INVESTIGATION_ALREADY_USED
        assert calls["insert"] == []
        assert calls["mark"] == []

    def test_another_action_from_the_same_investigation_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 같은 조사로 **다른 안**을 세우는 것도 둘째 제안이다."""
        standing = _proposal(proposal_key="다른지문")
        calls = _stub_proposal_repository(monkeypatch, history=(standing,))
        with pytest.raises(proposal_service.ProposalStateConflict) as caught:
            proposal_service.create_proposal(
                conn, result=_result(), as_of=D5, proposed_by="operator", investigation_id=INV
            )
        assert caught.value.code == proposal_service.INVESTIGATION_ALREADY_USED
        assert calls["insert"] == []

    def test_another_investigation_is_not_blocked_by_this_rule(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 막는 것은 **같은 조사**뿐이다 — 다른 조사는 기존 규칙이 가린다."""
        finished = _proposal(
            investigation_id="INV-somewhere-else",
            status="REJECTED",
            rejected_as_of=D5,
            rejected_by="operator",
            rejection_reason="다른 조사의 안이었다",
        )
        calls = _stub_proposal_repository(monkeypatch, history=(finished,))
        outcome = proposal_service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator", investigation_id=INV
        )
        assert outcome.created
        assert len(calls["insert"]) == 1

    def test_manual_proposals_are_never_folded_together(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ 조사를 안 댄 제안들이 서로를 «같은 조사» 로 읽히면 안 된다 (NULL ≠ NULL)."""
        earlier = _proposal(
            investigation_id=None,
            status="REJECTED",
            rejected_as_of=D5,
            rejected_by="operator",
            rejection_reason="손으로 세운 안이었다",
        )
        calls = _stub_proposal_repository(monkeypatch, history=(earlier,), investigation=None)
        outcome = proposal_service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator"
        )
        assert outcome.created
        assert [row.investigation_id for row in calls["insert"]] == [None]
