"""Act + Verify — **DB 없이 잴 수 있는 것들** (#628 Commit 6).

```text
무엇을 돌리나        명시적 표 — eval · reflection · 동적 import · LLM routing 없음
못 돌리는 것          경계가 없으면 **실행하지 않고 승인 상태로 남긴다**
누가 돌릴 수 있나      APPROVED 만. 이미 돌았으면 그 결과를 그대로 준다
경계 침범             판매·매입·재고를 이 파일이 직접 쓰는가 (소스로 막는다)
```

🔴 **실제 장부가 움직이는지는 여기서 못 잰다** — 폐기 Move · 잔량 · 위험 수용 되읽기는
   `test_logistics_agent_proposals_db.py` 가 실제 표로 잰다.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics.agent import execution
from app.logistics.agent import proposals as repository
from app.logistics.agent.execution import (
    ExecutionUnsupported,
    execute_approved_proposal,
)
from app.logistics.agent.investigation import ACTION_DECISION_OWNERS
from app.logistics.agent.proposal_service import ProposalNotFound, ProposalStateConflict
from app.logistics.agent.proposals import ProposalRow
from app.logistics.agent.tools import SUPPORTED_ACTIONS

SIM = "SIM-EXECUTE"
EXC = "EXC-FRESHNESS-1"
PRP = f"PRP-{EXC}-1"
LOT = "LOT-1"
RUNNER = "master-runner"

D1 = date(2026, 1, 1)
D5 = D1 + timedelta(days=4)
D6 = D1 + timedelta(days=5)
D8 = D1 + timedelta(days=7)


def _row(**overrides: Any) -> ProposalRow:
    base: dict[str, Any] = {
        "proposal_id": PRP,
        "sim_run_id": SIM,
        "exception_id": EXC,
        "status": "APPROVED",
        "action_type": "ACCEPT_RISK",
        "decision_owner": "LOGISTICS",
        "parameters": {"lot_id": LOT},
        "impact": {"feasibility": "FEASIBLE"},
        "evidence_refs": (),
        "proposed_as_of": D5,
        "proposed_by": "operator",
        "approved_as_of": D6,
        "approved_by": "operator",
    }
    base.update(overrides)
    return ProposalRow(**base)


class _FakeCursor:
    def __init__(self) -> None:
        self.rowcount = 1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: Any, **__: Any) -> None:
        raise AssertionError("Act 층이 SQL 을 직접 돌렸다")

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeConn:
    """커밋과 롤백만 센다. 🔴 SQL 을 흉내 내지 않는다 — 그건 실제 표가 잰다."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def conn() -> _FakeConn:
    return _FakeConn()


def _stub(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, list[Any]]:
    """저장소 경계를 갈아 끼운다. **진입점의 판단만 남긴다.**"""
    calls: dict[str, list[Any]] = {"risk": [], "record": []}
    defaults: dict[str, Any] = {
        "select_proposal": lambda *_, **__: _row(),
        "exception_status": lambda *_, **__: "PROPOSED",
        "accept_exception_risk": lambda *_, **kw: calls["risk"].append(kw) or 1,
        "exception_risk_accepted_as_of": lambda *_, **__: D8,
        "record_execution_outcome": lambda *_, **kw: calls["record"].append(kw) or 1,
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(repository, name, value)
    return calls


# ══════════════════════════════════════════════════════════════════════════
#  무엇을 돌리나
# ══════════════════════════════════════════════════════════════════════════


class TestDispatcher:
    def test_every_catalogue_action_has_exactly_one_executor(self) -> None:
        """🔴 카탈로그와 실행 표가 어긋나면 «승인은 되는데 아무도 안 돌리는» 행동이 생긴다."""
        assert set(execution.ACTION_EXECUTORS) == set(SUPPORTED_ACTIONS)

    def test_the_owner_table_is_still_the_only_source(self) -> None:
        """★ 누가 결정하는가는 Commit 5 의 결정론 표 그대로다 — 실행이 바꾸지 않는다."""
        assert set(ACTION_DECISION_OWNERS) == set(SUPPORTED_ACTIONS)

    def test_nothing_is_dispatched_dynamically(self) -> None:
        """🔴 §7 — `eval` · reflection · 동적 import · 문자열 SQL 로 고르지 않는다."""
        tree = ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not called & {"eval", "exec", "compile", "__import__", "globals", "vars"}
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "importlib" not in imported


class TestActionsWithoutAnExecutionBoundary:
    """🔴 **없는 계약을 만들어 «실행됨» 이라고 적지 않는다** (§4 · 최종 판단 규칙).

    두 행동은 타 부서 소유인데, 그쪽에 «이 요청을 받아라» 는 공식 진입점이 현재
    HEAD 에 **없다.** 그래서 실행하지 않고 제안을 `APPROVED` 로 남긴다.
    """

    @pytest.mark.parametrize(
        "action", ["SALES_PRIORITY_REQUEST", "PURCHASE_ADJUST_REQUEST"]
    )
    def test_the_handler_refuses_to_invent_a_contract(self, action: str) -> None:
        handler = execution.ACTION_EXECUTORS[action]
        with pytest.raises(ExecutionUnsupported) as caught:
            handler(None, proposal=_row(action_type=action), as_of=D8, executed_by=RUNNER)
        assert caught.value.code == execution.ACTION_NOT_EXECUTABLE

    @pytest.mark.parametrize(
        "action", ["SALES_PRIORITY_REQUEST", "PURCHASE_ADJUST_REQUEST"]
    )
    def test_the_proposal_stays_approved_and_nothing_is_written(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        """★ `FAILED` 로 적으면 제안이 terminal 이 되어, 계약이 생겨도 영영 못 돌린다."""
        calls = _stub(monkeypatch, select_proposal=lambda *_, **__: _row(action_type=action))
        outcome = execute_approved_proposal(
            conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by=RUNNER
        )
        assert outcome.status == "BLOCKED"
        assert outcome.failure_code == execution.ACTION_NOT_EXECUTABLE
        assert outcome.proposal is not None
        assert outcome.proposal.status == "APPROVED"
        # 🔴 제안 상태도 안 적고 커밋도 안 한다.
        assert calls["record"] == []
        assert conn.commits == 0

    def test_the_reason_names_the_missing_contract(self) -> None:
        """⚠️ *"안 된다"* 만으로는 다음 사람이 무엇을 만들어야 할지 모른다."""
        handler = execution.ACTION_EXECUTORS["PURCHASE_ADJUST_REQUEST"]
        with pytest.raises(ExecutionUnsupported) as caught:
            handler(None, proposal=_row(), as_of=D8, executed_by=RUNNER)
        assert "record_schedule" in str(caught.value)
        assert "cancel_schedule" in str(caught.value)


# ══════════════════════════════════════════════════════════════════════════
#  누가 돌릴 수 있나
# ══════════════════════════════════════════════════════════════════════════


class TestEntrypointPreconditions:
    def test_an_approved_proposal_executes(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub(
            monkeypatch,
            select_proposal=_once(
                _row(), _row(status="EXECUTED", executed_as_of=D8, executed_by=RUNNER)
            ),
        )
        outcome = execute_approved_proposal(
            conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by=RUNNER
        )
        assert outcome.status == "EXECUTED"
        assert calls["record"][0]["executed"] is True
        assert conn.commits == 1

    @pytest.mark.parametrize(
        "status", ["PROPOSED", "REJECTED", "EXPIRED", "SUPERSEDED", "FAILED"]
    )
    def test_only_approved_may_execute(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        """🔴 §78 — 그 밖의 상태에서는 **부작용 0.**"""
        calls = _stub(monkeypatch, select_proposal=lambda *_, **__: _row(status=status))
        with pytest.raises(ProposalStateConflict):
            execute_approved_proposal(
                conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by=RUNNER
            )
        assert calls["risk"] == []
        assert calls["record"] == []
        assert conn.commits == 0

    def test_an_already_executed_proposal_returns_its_result(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§27 — 같은 요청이 한 번 더 와도 **실제 행동은 한 번**이다."""
        done = _row(
            status="EXECUTED",
            executed_as_of=D8,
            executed_by=RUNNER,
            execution_result={"result": "RISK_ACCEPTED"},
        )
        calls = _stub(monkeypatch, select_proposal=lambda *_, **__: done)
        outcome = execute_approved_proposal(
            conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by=RUNNER
        )
        assert outcome.status == "ALREADY_EXECUTED"
        assert outcome.executed
        assert outcome.result == {"result": "RISK_ACCEPTED"}
        assert calls["risk"] == []
        assert conn.commits == 0

    def test_a_missing_proposal_is_not_found(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, select_proposal=lambda *_, **__: None)
        with pytest.raises(ProposalNotFound):
            execute_approved_proposal(
                conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by=RUNNER
            )

    def test_an_anonymous_execution_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch)
        with pytest.raises(ValueError, match="executed_by"):
            execute_approved_proposal(
                conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by="  "
            )

    def test_execution_cannot_predate_the_approval(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch)
        with pytest.raises(ValueError, match="승인일"):
            execute_approved_proposal(
                conn, sim_run_id=SIM, proposal_id=PRP, as_of=D5, executed_by=RUNNER
            )

    @pytest.mark.parametrize("status", ["RESOLVED", "DISMISSED", None])
    def test_a_closed_problem_makes_the_action_stale(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, status: Any
    ) -> None:
        """🔴 §11 — 승인 때 본 사실이 아직 참인지 **실행 직전에** 다시 본다."""
        calls = _stub(monkeypatch, exception_status=lambda *_, **__: status)
        outcome = execute_approved_proposal(
            conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by=RUNNER
        )
        assert outcome.status == "FAILED"
        assert outcome.failure_code == execution.STALE_ACTION
        # 🔴 업무 표에 손대기 전에 멈췄다.
        assert calls["risk"] == []
        assert calls["record"][0]["executed"] is False

    def test_a_race_that_already_executed_is_idempotent(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §28 — 남이 먼저 같은 제안을 실행했다. 되돌리고 다시 읽어 뜻을 정한다."""
        done = _row(status="EXECUTED", executed_as_of=D8, executed_by=RUNNER)
        _stub(
            monkeypatch,
            select_proposal=_once(_row(), done),
            record_execution_outcome=lambda *_, **__: 0,
        )
        outcome = execute_approved_proposal(
            conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, executed_by=RUNNER
        )
        assert outcome.status == "ALREADY_EXECUTED"
        assert conn.rollbacks == 1
        assert conn.commits == 0


def _once(first: Any, *rest: Any) -> Any:
    """첫 호출과 그 뒤를 다르게 답하는 읽기 — 실행 전후를 흉내 낸다."""
    queue = [first, *rest]

    def read(*_: Any, **__: Any) -> Any:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return read


# ══════════════════════════════════════════════════════════════════════════
#  경계 — 🔴 소스를 읽어 막는다
# ══════════════════════════════════════════════════════════════════════════


class TestRoleBoundary:
    """🔴 **물류가 남의 장부를 직접 쓰지 않는다** (§84 · §85).

    ⚠️ 소스로 막는다 — 경로를 밟아서 잡으려면 **밟지 않은 분기의 우회는 영원히 안 보인다.**
    """

    def _tree(self) -> ast.Module:
        return ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))

    def _imported_modules(self) -> set[str]:
        return {
            node.module
            for node in ast.walk(self._tree())
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

    def test_sales_is_never_imported(self) -> None:
        assert not {one for one in self._imported_modules() if one.startswith("app.sales")}

    def test_purchase_is_never_imported(self) -> None:
        assert not {
            one for one in self._imported_modules() if one.startswith("app.purchase_agent")
        }

    def test_no_other_department_is_imported(self) -> None:
        forbidden = ("app.master", "app.finance", "app.sales", "app.purchase_agent")
        assert not {
            one for one in self._imported_modules() if one.startswith(forbidden)
        }

    def test_no_llm_is_reachable(self) -> None:
        """🔴 §8 — Act 도 Verify 도 결정론이다."""
        assert not {one for one in self._imported_modules() if "llm" in one}

    def test_this_layer_writes_no_sql_of_its_own(self) -> None:
        """🔴 §43 — 재고를 고치는 SQL 을 여기서 만들지 않는다. 정본 함수를 통한다."""
        writes = ("INSERT INTO", "UPDATE ", "DELETE FROM")
        tree = self._tree()
        # ⚠️ docstring 은 뺀다 — 이 파일은 *"이런 SQL 을 만들지 않는다"* 를 **설명**하느라
        #    금지 문장을 본문에 적고 있고, 그 설명까지 위반으로 세면 검사가 헛돈다.
        explained = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        }
        offending = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in explained
            and any(verb in node.value.upper() for verb in writes)
        ]
        assert offending == [], offending

    def test_the_inventory_ledger_is_reached_only_through_its_owner(self) -> None:
        """★ 폐기는 `disposal.confirm_disposal` 하나로만 간다 — 원장을 직접 안 연다."""
        modules = self._imported_modules()
        assert "app.logistics.disposal" in modules
        assert "app.logistics.ledger" not in modules


class TestQuantity:
    """🔴 **값을 만들지 않는다** — 없으면 없다고 답한다."""

    @pytest.mark.parametrize("value", [None, "", "abc", True, [], {}])
    def test_an_unusable_value_is_not_invented(self, value: Any) -> None:
        assert execution._quantity(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"), [("300", Decimal(300)), (300, Decimal(300))]
    )
    def test_a_usable_value_keeps_its_exact_amount(self, value: Any, expected: Any) -> None:
        assert execution._quantity(value) == expected
