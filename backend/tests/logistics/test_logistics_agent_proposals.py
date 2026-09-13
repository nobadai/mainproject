"""대응안 — **DB 없이 잴 수 있는 것들** (#628 Commit 5).

```text
어떤 후보가 제안이 되나      순수 함수 — 추천 · 거부 · «못 쟀다» 와 «불가»
누가 결정하나                결정론 표에서 다시 계산한다 (모델 값이 아니다)
같은 제안을 알아보나          지문
그날 상태                    과거 재현 · 미래 detail 누수
트랜잭션의 주인               제안만 남는 반쪽 상태가 없나
표를 누가 쓰나                소스를 읽어 막는다
```

🔴 **실제 PostgreSQL 로 재는 것은 여기 없다** — 원자성·동시성·제약·격리는
   `test_logistics_agent_proposals_db.py` 가 실제 표로 잰다. 이 파일은 *"가짜로도
   정직하게 잴 수 있는 것"* 만 든다.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics.agent import proposal_service as service
from app.logistics.agent import proposals as repository
from app.logistics.agent import tool_dispatch, tools
from app.logistics.agent.investigation import (
    ACTION_DECISION_OWNERS,
    EvaluatedOption,
    FinishReason,
    InvestigationResult,
    ToolCallRecord,
    ToolCallStatus,
)
from app.logistics.agent.proposals import (
    ProposalInvariantViolation,
    ProposalRow,
    project_proposal_at,
    proposal_key_for,
)
from app.logistics.agent.tools import ActionImpact, CapacityContext, LotFact, LotView

SIM = "SIM-PROPOSAL"
EXC = "EX-SIM-PROPOSAL-FRESHNESS_PRESSURE-LOT-1-20260101"
LOT = "LOT-1"
PRP = f"PRP-{EXC}-1"

D1 = date(2026, 1, 1)
D5 = D1 + timedelta(days=4)
D6 = D1 + timedelta(days=5)
D7 = D1 + timedelta(days=6)
D8 = D1 + timedelta(days=7)
D9 = D1 + timedelta(days=8)


# ── 준비 도우미 ─────────────────────────────────────────────────────────


def _impact(
    feasibility: str = "FEASIBLE", *, action: str = "SALES_PRIORITY_REQUEST"
) -> ActionImpact:
    return ActionImpact(
        sim_run_id=SIM,
        as_of=D5,
        observed_as_of=D1,
        action=action,
        feasibility=feasibility,  # type: ignore[arg-type]
        affected_kg=None,
        capacity_delta_kg=None,
        candidate_kg=Decimal(500),
        estimated_loss_krw=None,
        freshness_days_left=2,
    )


def _lot_view(**overrides: Any) -> LotView:
    """진짜 `LotView` 한 벌. 🔴 **가짜 모양으로 재면 칸 이름이 바뀌어도 안 걸린다.**

    ★ 근거 추림이 보는 것이 이 칸 이름들이라, 여기서 진짜 타입을 쓰는 것이 곧 계약이다.
    """
    fields: dict[str, Any] = {
        "lot_id": LOT,
        "item_id": "ITEM-BAECHU",
        "item": "배추",
        "grade": None,
        "storage_zone": "COLD_HUMID_0_3",
        "status": "ACTIVE",
        "received_at": D1,
        "remaining_qty_kg": Decimal(700),
        "unit_cost_krw_per_kg": Decimal(1200),
        "remaining_freshness_days": 2,
        "effective_freshness_limit_days": 10,
        "turnover_status": "SELL_PRIORITY",
        "sell_priority": True,
        "sell_priority_remaining_days": 3,
        "disposal_candidate": False,
        "committed_kg": Decimal(200),
        "uncommitted_kg": Decimal(500),
        "remaining_qty_observed_as_of": D1,
        "status_observed_as_of": D1,
    }
    fields.update(overrides)
    return LotView(
        sim_run_id=SIM,
        as_of=D5,
        observed_as_of=D1,
        uncertainties=("COMMITMENT_UNRESOLVED",),
        lot=LotFact(**fields),
    )


def _capacity_view(window: dict[date, Decimal]) -> CapacityContext:
    """진짜 `CapacityContext`. 🔴 18일 창을 통째로 들고 있는 바로 그 타입이다."""
    return CapacityContext(
        sim_run_id=SIM,
        as_of=D5,
        observed_as_of=None,
        used_kg=Decimal(900),
        guaranteed_kg=Decimal(1000),
        burst_kg=Decimal(200),
        available_kg=Decimal(100),
        window_usage_ratio=Decimal("0.90"),
        cap_by_date=window,
        inbound_lead_days=2,
        capacity_tight_ratio=Decimal("0.90"),
        capacity_basis="CURRENT_ACTIVE_POLICY",
    )


#: 🔴 «영향을 안 줬다» 와 «영향이 없다» 를 가르는 자리. 기본값을 `None` 으로 두면
#:    `impact=None` 을 시험할 방법이 사라진다.
_DEFAULT_IMPACT = object()


def _option(
    *,
    action: str = "SALES_PRIORITY_REQUEST",
    parameters: dict[str, Any] | None = None,
    impact: Any = _DEFAULT_IMPACT,
    rejected_reason: str | None = None,
    evidence_refs: tuple[int, ...] = (1,),
) -> EvaluatedOption:
    owner = ACTION_DECISION_OWNERS.get(action, "LOGISTICS")
    if impact is _DEFAULT_IMPACT:
        impact = None if rejected_reason is not None else _impact(action=action)
    return EvaluatedOption(
        action=action,
        parameters={"lot_id": LOT} if parameters is None else parameters,
        rationale="신선도 압박이라 우선 판매 후보로 올린다.",
        evidence_refs=evidence_refs,
        decision_owner=owner,
        parameters_are_hypothesis=owner != "LOGISTICS",
        impact=impact,
        rejected_reason=rejected_reason,
    )


def _result(
    *,
    options: tuple[EvaluatedOption, ...] | None = None,
    recommended_index: int | None = 0,
    observed_as_of: date | None = D1,
    as_of: date = D5,
    finish_reason: FinishReason = FinishReason.FINISHED,
    tool_calls: Any = None,
) -> InvestigationResult:
    return InvestigationResult(
        sim_run_id=SIM,
        as_of=as_of,
        exception_id=EXC,
        finish_reason=finish_reason,
        llm_status="SUCCESS",
        options=(_option(),) if options is None else options,
        recommended_index=recommended_index,
        observed_as_of=observed_as_of,
        tool_calls=tool_calls
        if tool_calls is not None
        else (
            ToolCallRecord(
                sequence=1,
                tool_name="get_lot",
                arguments={"lot_id": LOT},
                status=ToolCallStatus.SUCCESS,
                # 🔴 **그때 Tool 이 낸 답 그대로.** 근거는 여기서만 나온다 —
                #    제안을 만들며 Tool 을 다시 부르지 않는다.
                answer=_lot_view(),
                observed_as_of=D1,
                uncertainties=("COMMITMENT_UNRESOLVED",),
            ),
            ToolCallRecord(
                sequence=2,
                tool_name="get_policy",
                arguments={},
                status=ToolCallStatus.SUCCESS,
                observed_as_of=None,
            ),
        ),
    )


def _row(**overrides: Any) -> ProposalRow:
    base: dict[str, Any] = {
        "proposal_id": PRP,
        "sim_run_id": SIM,
        "exception_id": EXC,
        "status": "PROPOSED",
        "action_type": "SALES_PRIORITY_REQUEST",
        "decision_owner": "SALES",
        "parameters": {"lot_id": LOT},
        "impact": {"feasibility": "FEASIBLE"},
        "evidence_refs": (),
        "proposed_as_of": D5,
        "proposed_by": "operator",
        "observed_as_of": D1,
        "proposal_key": "key",
    }
    base.update(overrides)
    return ProposalRow(**base)


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


def _stub_repository(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, list[Any]]:
    """저장소 경계를 갈아 끼운다. **서비스의 판단만 남긴다.**"""
    calls: dict[str, list[Any]] = {"insert": [], "mark": [], "reopen": [], "transition": []}
    defaults: dict[str, Any] = {
        "exception_status": lambda *_, **__: "OPEN",
        # ★ 대체 대상 · 날짜 단조성 · 중복을 **한 번 읽은 목록**으로 다 가린다.
        "select_proposals": lambda *_, **__: (),
        "live_proposals_for": lambda *_, **__: (),
        "next_proposal_id": lambda *_, **__: PRP,
        "insert_proposal": lambda _conn, *, row: calls["insert"].append(row) or row,
        "mark_exception_proposed": lambda *_, **__: calls["mark"].append(1) or 1,
        "reopen_exception": lambda *_, **__: calls["reopen"].append(1) or 1,
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(repository, name, value)
    return calls


# ══════════════════════════════════════════════════════════════════════════
#  어떤 후보가 제안이 되나
# ══════════════════════════════════════════════════════════════════════════


class TestOptionSelection:
    """🔴 **조사가 안 고른 안을 저장이 대신 고르지 않는다.**"""

    def test_the_recommended_accepted_option_becomes_the_proposal(self) -> None:
        option, reason = service.select_proposal_option(_result())
        assert option is not None
        assert reason == ""
        assert option.action == "SALES_PRIORITY_REQUEST"

    def test_no_recommendation_means_no_proposal(self) -> None:
        option, reason = service.select_proposal_option(_result(recommended_index=None))
        assert option is None
        assert reason == service.NO_RECOMMENDED_OPTION

    def test_a_rejected_option_is_never_stored(self) -> None:
        """§15 — 결정론이 이미 버린 안을 사람 앞에 올리지 않는다."""
        rejected = _option(rejected_reason="SUBJECT_OUT_OF_SCOPE:LOT-9")
        option, reason = service.select_proposal_option(_result(options=(rejected,)))
        assert option is None
        assert reason.startswith(service.OPTION_REJECTED)

    def test_it_does_not_slide_to_the_next_option(self) -> None:
        """⚠️ 추천이 버려졌으면 **제안 0 건**이다 — 2번 후보로 미끄러지지 않는다.

        미끄러지면 그 선택의 주인이 아무도 아니게 된다: 조사도 안 골랐고 사람도 안 봤다.
        """
        options = (
            _option(rejected_reason="IMPACT_FAILED"),
            _option(action="ACCEPT_RISK", parameters={"lot_id": LOT}),
        )
        option, reason = service.select_proposal_option(
            _result(options=options, recommended_index=0)
        )
        assert option is None
        assert reason.startswith(service.OPTION_REJECTED)

    def test_an_out_of_range_recommendation_is_not_guessed(self) -> None:
        option, reason = service.select_proposal_option(_result(recommended_index=7))
        assert option is None
        assert reason.startswith(service.RECOMMENDED_INDEX_OUT_OF_RANGE)

    def test_an_unmeasured_impact_is_still_proposed(self) -> None:
        """🔴 **«못 쟀다» 는 제안할 수 있다** (§15). 숫자를 지어내 FEASIBLE 로 안 바꾼다."""
        unresolved = _option(impact=_impact("UNRESOLVED"))
        option, reason = service.select_proposal_option(_result(options=(unresolved,)))
        assert option is not None
        assert option.impact is not None
        assert option.impact.feasibility == "UNRESOLVED"
        assert reason == ""

    def test_an_impossible_action_is_not_proposed(self) -> None:
        """🔴 **«불가» 는 «못 쟀다» 와 다르다.**

        계산기가 재 보고 «안 된다» 고 답한 안을 승인 화면에 올리면, 사람이 불가능한
        일을 승인한다.
        """
        infeasible = _option(impact=_impact("INFEASIBLE"))
        option, reason = service.select_proposal_option(_result(options=(infeasible,)))
        assert option is None
        assert reason == service.IMPACT_INFEASIBLE

    def test_an_unmeasured_option_without_impact_is_not_proposed(self) -> None:
        naked = _option(impact=None)
        option, reason = service.select_proposal_option(_result(options=(naked,)))
        assert option is None
        assert reason == service.IMPACT_MISSING

    def test_an_action_outside_the_catalogue_is_not_proposed(self) -> None:
        outside = _option(action="ZONE_MOVE", impact=_impact("UNSUPPORTED", action="ZONE_MOVE"))
        option, reason = service.select_proposal_option(_result(options=(outside,)))
        assert option is None
        assert reason.startswith(service.ACTION_UNSUPPORTED)


class TestDecisionOwner:
    """🔴 **누가 결정하는가는 결정론이 정한다** (§3 · §49). 모델이 고르는 칸이 아니다."""

    @pytest.mark.parametrize(
        ("action", "owner"),
        [
            ("SALES_PRIORITY_REQUEST", "SALES"),
            ("PURCHASE_ADJUST_REQUEST", "PURCHASE"),
            ("ACCEPT_RISK", "LOGISTICS"),
            ("DISPOSAL_REQUEST", "LOGISTICS"),
        ],
    )
    def test_each_action_has_one_owner(self, action: str, owner: str) -> None:
        assert ACTION_DECISION_OWNERS[action] == owner

    def test_a_model_supplied_owner_is_overwritten(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """모델이 *"이건 물류가 정하면 됩니다"* 라고 적어도 표에는 `SALES` 가 적힌다."""
        calls = _stub_repository(monkeypatch)
        lying = EvaluatedOption(
            action="SALES_PRIORITY_REQUEST",
            parameters={"lot_id": LOT},
            rationale="",
            evidence_refs=(1,),
            decision_owner="LOGISTICS",  # 🔴 거짓말이다
            parameters_are_hypothesis=False,
            impact=_impact(),
        )
        outcome = service.create_proposal(
            conn, result=_result(options=(lying,)), as_of=D5, proposed_by="operator"
        )
        assert outcome.created
        assert calls["insert"][0].decision_owner == "SALES"


class TestProposalKey:
    """같은 뜻의 제안을 알아보는 지문 (§12)."""

    def _key(self, **overrides: Any) -> str:
        payload: dict[str, Any] = {
            "sim_run_id": SIM,
            "exception_id": EXC,
            "action_type": "SALES_PRIORITY_REQUEST",
            "parameters": {"lot_id": LOT},
        }
        payload.update(overrides)
        return proposal_key_for(**payload)

    def test_the_same_proposal_has_the_same_fingerprint(self) -> None:
        assert self._key() == self._key()

    def test_a_different_run_is_a_different_proposal(self) -> None:
        assert self._key() != self._key(sim_run_id="SIM-OTHER")

    def test_a_different_quantity_is_a_different_proposal(self) -> None:
        first = self._key(parameters={"lot_id": LOT, "qty_kg": Decimal(100)})
        second = self._key(parameters={"lot_id": LOT, "qty_kg": Decimal(200)})
        assert first != second

    def test_key_order_does_not_change_the_fingerprint(self) -> None:
        first = self._key(parameters={"lot_id": LOT, "qty_kg": Decimal(100)})
        second = self._key(parameters={"qty_kg": Decimal(100), "lot_id": LOT})
        assert first == second

    def test_a_decimal_and_its_text_are_the_same_quantity(self) -> None:
        """⚠️ `Decimal` 이 `float` 을 지나면 같은 수량이 실행마다 다른 지문이 된다."""
        assert self._key(parameters={"qty_kg": Decimal("100.5")}) == self._key(
            parameters={"qty_kg": "100.5"}
        )


# ══════════════════════════════════════════════════════════════════════════
#  그날 상태 — 순수 함수
# ══════════════════════════════════════════════════════════════════════════


class TestHistoricalProjection:
    """🔴 **지금 값을 과거로 쓰지 않는다** (§40 ~ §42)."""

    def test_a_proposal_does_not_exist_before_it_was_proposed(self) -> None:
        assert project_proposal_at(_row(), as_of=D1) is None

    def test_on_the_day_it_was_proposed_it_exists(self) -> None:
        at_date = project_proposal_at(_row(), as_of=D5)
        assert at_date is not None
        assert at_date.status == "PROPOSED"

    def test_an_approval_is_invisible_the_day_before(self) -> None:
        row = _row(status="APPROVED", approved_as_of=D8, approved_by="operator")
        at_date = project_proposal_at(row, as_of=D6)
        assert at_date is not None
        assert at_date.status == "PROPOSED"
        assert at_date.approved_by is None
        assert at_date.approved_as_of is None

    def test_an_approval_is_visible_the_day_after(self) -> None:
        row = _row(
            status="APPROVED", approved_as_of=D8, approved_by="operator", approval_note="확인"
        )
        at_date = project_proposal_at(row, as_of=D9)
        assert at_date is not None
        assert at_date.status == "APPROVED"
        assert at_date.approved_by == "operator"
        assert at_date.approval_note == "확인"

    def test_a_future_rejection_reason_does_not_leak_into_the_past(self) -> None:
        """🔴 §42 — D10 의 거절 사유가 D6 조회에 보이면 그날 없던 사실이 과거에 생긴다."""
        row = _row(
            status="REJECTED",
            rejected_as_of=D8,
            rejected_by="operator",
            rejection_reason="가격 기준 불명확",
        )
        at_date = project_proposal_at(row, as_of=D6)
        assert at_date is not None
        assert at_date.status == "PROPOSED"
        assert at_date.rejected_as_of is None
        assert at_date.rejected_by is None
        assert at_date.rejection_reason is None

    def test_the_immutable_payload_survives_the_projection(self) -> None:
        """⚠️ 제안 자체(행동 · 인자 · 영향 · 관측일)는 결정과 무관하게 그대로다 (§16)."""
        row = _row(status="REJECTED", rejected_as_of=D8, rejected_by="x", rejection_reason="y")
        at_date = project_proposal_at(row, as_of=D6)
        assert at_date is not None
        assert at_date.action_type == "SALES_PRIORITY_REQUEST"
        assert at_date.parameters == {"lot_id": LOT}
        assert at_date.impact == {"feasibility": "FEASIBLE"}
        assert at_date.observed_as_of == D1
        assert at_date.decision_owner == "SALES"

    @pytest.mark.parametrize(
        ("column", "expected"),
        [
            ("expired_as_of", "EXPIRED"),
            ("superseded_as_of", "SUPERSEDED"),
        ],
    )
    def test_every_terminal_date_projects_its_own_status(
        self, column: str, expected: str
    ) -> None:
        at_date = project_proposal_at(_row(status=expected, **{column: D8}), as_of=D9)
        assert at_date is not None
        assert at_date.status == expected

    def test_two_terminal_dates_are_refused_instead_of_guessed(self) -> None:
        """🔴 fail-closed (§41). 하나를 골라 답하면 «그날 승인됐다» 는 거짓이 남는다."""
        broken = _row(
            status="APPROVED",
            approved_as_of=D8,
            approved_by="operator",
            rejected_as_of=D8,
            rejected_by="operator",
            rejection_reason="둘 다 적혀 있다",
        )
        with pytest.raises(ProposalInvariantViolation):
            project_proposal_at(broken, as_of=D9)

    def test_an_execution_state_without_its_date_is_refused(self) -> None:
        """⚠️ 상태는 «실행됨» 인데 그날을 못 댄다 — 날짜 없이 과거로 접지 않는다."""
        with pytest.raises(ProposalInvariantViolation):
            project_proposal_at(
                _row(status="EXECUTED", approved_as_of=D6, approved_by="operator"), as_of=D9
            )


def _executed(**overrides: Any) -> ProposalRow:
    """`D5 제안 · D6 승인 · D8 실행` — Commit 6 의 **정상 흐름** 한 줄."""
    base: dict[str, Any] = {
        "status": "EXECUTED",
        "approved_as_of": D6,
        "approved_by": "operator",
        "approval_note": "신선도가 급하다",
        "executed_as_of": D8,
        "executed_by": "master-runner",
        "execution_result": {"action": "ACCEPT_RISK", "reference_id": EXC},
    }
    base.update(overrides)
    return _row(**base)


class TestExecutionProjection:
    """🔴 **승인은 끝이 아니다** — 한 행이 승인일과 실행일을 함께 든다 (Commit 6 · §20)."""

    def test_the_day_of_the_proposal_shows_only_the_proposal(self) -> None:
        at_date = project_proposal_at(_executed(), as_of=D5)
        assert at_date is not None
        assert at_date.status == "PROPOSED"
        assert at_date.approved_by is None
        assert at_date.executed_by is None

    def test_between_approval_and_execution_it_is_approved(self) -> None:
        """🔴 §77 — D7 조회에 실행 detail 이 한 칸도 안 보인다."""
        at_date = project_proposal_at(_executed(), as_of=D7)
        assert at_date is not None
        assert at_date.status == "APPROVED"
        # 승인은 이미 일어났다 — 그 사실은 보인다.
        assert at_date.approved_as_of == D6
        assert at_date.approved_by == "operator"
        # 🔴 실행은 아직 안 일어났다.
        assert at_date.executed_as_of is None
        assert at_date.executed_by is None
        assert at_date.execution_result == {}

    def test_after_execution_it_is_executed(self) -> None:
        at_date = project_proposal_at(_executed(), as_of=D9)
        assert at_date is not None
        assert at_date.status == "EXECUTED"
        assert at_date.executed_as_of == D8
        assert at_date.executed_by == "master-runner"
        assert at_date.execution_result["reference_id"] == EXC

    def test_the_approval_survives_the_execution(self) -> None:
        """★ **칸 묶음마다 자기 날짜로 가린다.**

        상태 하나만 보고 가리면 «실행됨» 조회에서 *누가 승인했나* 가 사라진다 — 그 일은
        실제로 일어났고 기록에 남아야 한다.
        """
        at_date = project_proposal_at(_executed(), as_of=D9)
        assert at_date is not None
        assert at_date.approved_by == "operator"
        assert at_date.approval_note == "신선도가 급하다"

    def test_a_failure_projects_the_same_way(self) -> None:
        failed = _executed(
            status="FAILED",
            executed_as_of=None,
            execution_result={},
            failed_as_of=D8,
            failure_code="STALE_ACTION",
            failure_reason="승인 뒤 잔량이 줄었다",
        )
        waiting = project_proposal_at(failed, as_of=D7)
        assert waiting is not None
        assert waiting.status == "APPROVED"
        assert waiting.failure_code is None
        assert waiting.failure_reason is None

        decided = project_proposal_at(failed, as_of=D9)
        assert decided is not None
        assert decided.status == "FAILED"
        assert decided.failure_code == "STALE_ACTION"

    def test_an_execution_without_an_approval_is_refused(self) -> None:
        """🔴 승인 없이 실행된 것으로 적힌 행은 «누가 진행해도 좋다고 했나» 를 못 댄다."""
        broken = _executed(approved_as_of=None, approved_by=None)
        with pytest.raises(ProposalInvariantViolation):
            project_proposal_at(broken, as_of=D9)

    def test_execution_and_failure_together_are_refused(self) -> None:
        broken = _executed(failed_as_of=D8, failure_code="X")
        with pytest.raises(ProposalInvariantViolation):
            project_proposal_at(broken, as_of=D9)

    def test_an_executed_proposal_no_longer_blocks_the_next_one(self) -> None:
        """🔴 §17 — 실행이 끝난 제안의 «끝난 날» 은 실행일이다.

        승인일을 끝으로 세면 `D6 승인 · D8 실행` 뒤에 오는 새 제안이 **D6 이후**면 된다고
        답하게 되는데, 그러면 D7 조회에서 살아 있는 제안이 둘이 된다.
        """
        latest, unorderable = repository.latest_terminal_as_of([_executed()])
        assert (latest, unorderable) == (D8, ())

    def test_an_approved_proposal_has_no_end_yet(self) -> None:
        waiting = _row(status="APPROVED", approved_as_of=D6, approved_by="operator")
        assert repository.latest_terminal_as_of([waiting]) == (None, ())


# ══════════════════════════════════════════════════════════════════════════
#  트랜잭션의 주인 — 반쪽 상태를 안 남긴다
# ══════════════════════════════════════════════════════════════════════════


class TestTransactionOwnership:
    """🔴 제안 INSERT 와 Exception UPDATE 는 **함께** 성공한다 (§24 · §25)."""

    def test_a_created_proposal_commits_once(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_repository(monkeypatch)
        outcome = service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator"
        )
        assert outcome.created
        assert calls["mark"] == [1]
        assert conn.commits == 1
        assert conn.rollbacks == 0

    def test_a_failing_exception_update_leaves_no_proposal(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§54 — Exception 이 그 사이에 닫혔다. **제안만 남기지 않는다.**"""

        def vanished(*_: Any, **__: Any) -> int:
            return 0

        _stub_repository(monkeypatch, mark_exception_proposed=vanished)
        with pytest.raises(service.ProposalStateConflict):
            service.create_proposal(conn, result=_result(), as_of=D5, proposed_by="operator")
        assert conn.commits == 0
        assert conn.rollbacks == 1

    def test_a_failing_insert_rolls_back(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("UniqueViolation")

        _stub_repository(monkeypatch, insert_proposal=broken)
        with pytest.raises(RuntimeError):
            service.create_proposal(conn, result=_result(), as_of=D5, proposed_by="operator")
        assert conn.commits == 0
        assert conn.rollbacks == 1

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"recommended_index": None}, "NO_RECOMMENDED_OPTION"),
            ({"options": (_option(rejected_reason="IMPACT_FAILED"),)}, "OPTION_REJECTED"),
        ],
    )
    def test_nothing_is_written_when_there_is_nothing_to_propose(
        self,
        conn: _FakeConn,
        monkeypatch: pytest.MonkeyPatch,
        kwargs: dict[str, Any],
        reason: str,
    ) -> None:
        """§53 — 제안 0 건이면 **Exception 도 안 건드린다.**"""
        calls = _stub_repository(monkeypatch)
        outcome = service.create_proposal(
            conn, result=_result(**kwargs), as_of=D5, proposed_by="operator"
        )
        assert outcome.status == "SKIPPED"
        assert outcome.reason.startswith(reason)
        assert calls["insert"] == []
        assert calls["mark"] == []
        assert conn.commits == 0


class TestCreatePreconditions:
    """제안이 서기 전에 확인하는 것들 (§49)."""

    def test_a_closed_exception_gets_no_proposal(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_repository(monkeypatch, exception_status=lambda *_, **__: "RESOLVED")
        outcome = service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator"
        )
        assert outcome.status == "SKIPPED"
        assert outcome.reason == f"{service.EXCEPTION_NOT_LIVE}:RESOLVED"
        assert calls["insert"] == []

    def test_a_missing_exception_gets_no_proposal(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_repository(monkeypatch, exception_status=lambda *_, **__: None)
        outcome = service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator"
        )
        assert outcome.status == "SKIPPED"
        assert outcome.reason.startswith(service.EXCEPTION_NOT_FOUND)

    def test_a_retry_returns_the_proposal_that_already_stands(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§12 — 같은 지문의 살아 있는 제안이 있으면 **새 행을 안 만든다.**"""
        key = proposal_key_for(
            sim_run_id=SIM,
            exception_id=EXC,
            action_type="SALES_PRIORITY_REQUEST",
            parameters={"lot_id": LOT},
        )
        standing = _row(proposal_key=key)
        calls = _stub_repository(monkeypatch, select_proposals=lambda *_, **__: (standing,))
        outcome = service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator"
        )
        assert outcome.status == "REUSED"
        assert outcome.proposal is standing
        assert calls["insert"] == []
        assert conn.commits == 0

    def test_a_different_live_proposal_blocks_a_new_one(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§47 · §48 — 한 문제에 승인 대기 제안이 둘이면 사람이 무엇을 승인하는지 모른다."""
        other = _row(proposal_key="아주-다른-지문", status="APPROVED")
        calls = _stub_repository(monkeypatch, select_proposals=lambda *_, **__: (other,))
        outcome = service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator"
        )
        assert outcome.status == "SKIPPED"
        assert outcome.reason == f"{service.LIVE_PROPOSAL_EXISTS}:{other.proposal_id}"
        assert calls["insert"] == []

    def test_an_anonymous_proposal_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_repository(monkeypatch)
        with pytest.raises(ValueError, match="proposed_by"):
            service.create_proposal(conn, result=_result(), as_of=D5, proposed_by="   ")

    def test_a_business_day_that_disagrees_with_the_investigation_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §20 — 다른 영업일의 조사로 오늘 제안을 세우지 않는다."""
        _stub_repository(monkeypatch)
        with pytest.raises(ValueError, match="as_of"):
            service.create_proposal(conn, result=_result(), as_of=D8, proposed_by="operator")


class TestSupersedeBoundary:
    """🔴 **남의 문제의 대응안을 닫지 않는다.**

    `_supersede` 는 `sim_run_id` 와 `proposal_id` 만 보고 `UPDATE` 한다 — 검증이 없으면
    같은 실행의 **다른 Exception** 제안을 그대로 `SUPERSEDED` 로 만들고, 그 문제는
    대응안을 잃은 채 잃었다는 사실조차 안 남는다.
    """

    def test_a_proposal_from_another_exception_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = _row(proposal_id="PRP-OTHER-1", exception_id="EX-B")
        calls = _stub_repository(
            monkeypatch,
            select_proposals=lambda *_, **__: (),
            select_proposal=lambda *_, **__: elsewhere,
        )
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.create_proposal(
                conn,
                result=_result(),
                as_of=D5,
                proposed_by="operator",
                supersedes="PRP-OTHER-1",
            )
        assert caught.value.code == service.SUPERSEDE_TARGET_MISMATCH
        # 🔴 아무것도 안 썼다 — 남의 제안도, 이 문제의 제안도.
        assert calls["insert"] == []
        assert calls["mark"] == []
        assert conn.commits == 0

    def test_a_proposal_that_does_not_exist_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_repository(monkeypatch, select_proposal=lambda *_, **__: None)
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.create_proposal(
                conn, result=_result(), as_of=D5, proposed_by="operator", supersedes="PRP-NOPE"
            )
        assert caught.value.code == service.SUPERSEDE_TARGET_MISMATCH
        assert calls["insert"] == []

    def test_an_already_finished_proposal_cannot_be_superseded(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """닫을 것이 없다 — 이미 끝난 제안을 또 닫으면 끝난 날이 둘이 된다."""
        done = _row(status="REJECTED", rejected_as_of=D5, rejected_by="x", rejection_reason="y")
        calls = _stub_repository(monkeypatch, select_proposals=lambda *_, **__: (done,))
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.create_proposal(
                conn,
                result=_result(),
                as_of=D5,
                proposed_by="operator",
                supersedes=done.proposal_id,
            )
        assert caught.value.code == service.SUPERSEDE_TARGET_MISMATCH
        assert calls["insert"] == []

    def test_a_replayed_supersede_is_a_retry_not_a_conflict(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **성공한 요청의 재시도가 예외로 튀면 안 된다.**

        첫 호출이 대체를 끝냈으므로 두 번째 호출이 보는 대상은 이미 `SUPERSEDED` 다.
        대체 검증을 중복 판정 **앞**에 두면 그 재시도가 `SUPERSEDE_TARGET_MISMATCH` 로
        돌아가고, 사람은 성공한 제안을 다시 올리려 한다.
        """
        key = proposal_key_for(
            sim_run_id=SIM,
            exception_id=EXC,
            action_type="SALES_PRIORITY_REQUEST",
            parameters={"lot_id": LOT},
        )
        closed = _row(proposal_id="PRP-1", status="SUPERSEDED", superseded_as_of=D5)
        standing = _row(proposal_id="PRP-2", proposal_key=key, previous_proposal_id="PRP-1")
        calls = _stub_repository(
            monkeypatch, select_proposals=lambda *_, **__: (closed, standing)
        )
        outcome = service.create_proposal(
            conn, result=_result(), as_of=D5, proposed_by="operator", supersedes="PRP-1"
        )
        assert outcome.status == "REUSED"
        assert outcome.proposal is standing
        assert calls["insert"] == []
        assert conn.commits == 0

    def test_a_replacement_cannot_predate_what_it_replaces(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """대체된 날이 제안된 날보다 앞설 수 없다. ⚠️ DB CHECK 도 막지만 «왜» 를 못 남긴다."""
        later = _row(proposal_id="PRP-LATER", proposal_key="다른-지문", proposed_as_of=D8)
        calls = _stub_repository(monkeypatch, select_proposals=lambda *_, **__: (later,))
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.create_proposal(
                conn,
                result=_result(as_of=D5),
                as_of=D5,
                proposed_by="operator",
                supersedes="PRP-LATER",
            )
        assert caught.value.code == service.SUPERSEDE_TARGET_MISMATCH
        assert calls["insert"] == []

    def test_the_live_proposal_of_this_exception_is_superseded(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 제대로 된 대체는 그대로 돈다 — 검증이 정상 경로를 막지 않는다."""
        standing = _row(proposal_key="다른-지문")
        moved: list[Any] = []
        calls = _stub_repository(
            monkeypatch,
            select_proposals=lambda *_, **__: (standing,),
            transition_proposal=lambda *_, **kw: moved.append(kw) or 1,
        )
        outcome = service.create_proposal(
            conn,
            result=_result(),
            as_of=D5,
            proposed_by="operator",
            supersedes=standing.proposal_id,
        )
        assert outcome.created
        assert moved[0]["to_status"] == "SUPERSEDED"
        assert calls["insert"][0].previous_proposal_id == standing.proposal_id


class TestChronology:
    """🔴 **과거 날짜로 새 제안을 세우면 그날 살아 있던 제안이 둘이 된다.**

    부분 유일 인덱스는 «지금» 상태만 보므로 이 겹침을 못 막는다 — 과거로 접었을 때만
    드러난다.
    """

    def _history(self, *rows: ProposalRow) -> Any:
        return lambda *_, **__: rows

    def test_a_backdated_proposal_is_refused(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = _row(status="REJECTED", rejected_as_of=D6, rejected_by="x", rejection_reason="y")
        calls = _stub_repository(monkeypatch, select_proposals=self._history(closed))
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.create_proposal(
                conn, result=_result(as_of=D5), as_of=D5, proposed_by="operator"
            )
        assert caught.value.code == service.PROPOSAL_HISTORY_CONFLICT
        assert calls["insert"] == []
        assert conn.commits == 0

    def test_the_day_the_previous_one_ended_is_allowed(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ 같은 날은 허용한다 — 그날 이전 것은 이미 끝났고 새 것이 그날 섰다."""
        closed = _row(status="REJECTED", rejected_as_of=D6, rejected_by="x", rejection_reason="y")
        calls = _stub_repository(monkeypatch, select_proposals=self._history(closed))
        outcome = service.create_proposal(
            conn, result=_result(as_of=D6), as_of=D6, proposed_by="operator"
        )
        assert outcome.created
        assert calls["insert"][0].proposed_as_of == D6

    def test_a_later_day_is_allowed(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = _row(status="EXPIRED", expired_as_of=D6)
        _stub_repository(monkeypatch, select_proposals=self._history(closed))
        assert service.create_proposal(
            conn, result=_result(as_of=D8), as_of=D8, proposed_by="operator"
        ).created

    def test_a_proposal_whose_end_cannot_be_dated_stops_the_line(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 `EXECUTED` 는 «언제» 됐는지 적는 칸이 없다 (Commit 6) — 앞뒤를 못 세운다."""
        executed = _row(status="EXECUTED", approved_as_of=None)
        calls = _stub_repository(monkeypatch, select_proposals=self._history(executed))
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.create_proposal(
                conn, result=_result(as_of=D8), as_of=D8, proposed_by="operator"
            )
        assert caught.value.code == service.PROPOSAL_HISTORY_CONFLICT
        assert calls["insert"] == []


class TestLatestTerminal:
    """순수 함수 — 기존 제안들이 **마지막으로 끝난 날**."""

    def test_nothing_finished_yet(self) -> None:
        assert repository.latest_terminal_as_of([_row()]) == (None, ())

    def test_the_latest_of_several(self) -> None:
        rows = [
            _row(proposal_id="A", status="REJECTED", rejected_as_of=D6, rejected_by="x",
                 rejection_reason="y"),
            _row(proposal_id="B", status="SUPERSEDED", superseded_as_of=D8),
        ]
        assert repository.latest_terminal_as_of(rows) == (D8, ())

    def test_an_undateable_end_is_named(self) -> None:
        rows = [_row(proposal_id="A", status="FAILED")]
        assert repository.latest_terminal_as_of(rows) == (None, ("A",))

    def test_an_empty_history_has_no_floor(self) -> None:
        assert repository.latest_terminal_as_of([]) == (None, ())


class TestStaleApproval:
    """🔴 **이미 닫힌 문제의 제안을 승인하지 않는다.**

    사람이 보던 목록이 낡았을 수 있다 — 그 사이 재탐지가 문제를 닫았는데 승인이
    들어가면 *"없어진 문제에 대응하기로 했다"* 가 장부에 남는다.
    """

    def _approve(self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, status: Any) -> Any:
        moved: list[Any] = []
        monkeypatch.setattr(repository, "select_proposal", lambda *_, **__: _row())
        monkeypatch.setattr(repository, "exception_status", lambda *_, **__: status)
        monkeypatch.setattr(
            repository, "transition_proposal", lambda *_, **kw: moved.append(kw) or 1
        )
        return moved

    @pytest.mark.parametrize("status", ["RESOLVED", "DISMISSED", "OPEN", None])
    def test_a_problem_that_is_not_waiting_refuses_the_approval(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, status: Any
    ) -> None:
        moved = self._approve(conn, monkeypatch, status)
        with pytest.raises(service.ProposalStateConflict) as caught:
            service.approve_proposal(
                conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, approved_by="operator"
            )
        assert caught.value.code == service.STALE_PROPOSAL
        # 🔴 UPDATE 를 **시작조차 안 했다.**
        assert moved == []
        assert conn.commits == 0

    def test_a_waiting_problem_is_approved(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        moved = self._approve(conn, monkeypatch, "PROPOSED")
        service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, approved_by="operator"
        )
        assert moved[0]["to_status"] == "APPROVED"
        # 🔴 막는 것은 읽기가 아니라 **SQL 조건**이다 — 읽고 쓰는 사이의 틈을 없앤다.
        assert moved[0]["require_exception_status"] == "PROPOSED"
        assert conn.commits == 1

    @pytest.mark.parametrize("decide", ["reject", "expire"])
    def test_a_closed_problem_can_still_be_tidied_up(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, decide: str
    ) -> None:
        """⚠️ 거절·만료는 이 검사를 **안 한다** — 막으면 그 제안이 영원히 `PROPOSED` 다."""
        moved = self._approve(conn, monkeypatch, "RESOLVED")
        monkeypatch.setattr(repository, "live_proposals_for", lambda *_, **__: ())
        monkeypatch.setattr(repository, "reopen_exception", lambda *_, **__: 0)
        if decide == "reject":
            service.reject_proposal(
                conn,
                sim_run_id=SIM,
                proposal_id=PRP,
                as_of=D8,
                rejected_by="operator",
                rejection_reason="필요 없어졌다",
            )
        else:
            service.expire_proposal(conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8)
        assert moved[0]["to_status"] == ("REJECTED" if decide == "reject" else "EXPIRED")
        # 🔴 거절·만료의 UPDATE 에는 Exception 조건이 안 실린다.
        assert moved[0]["require_exception_status"] is None

    def test_a_retry_still_returns_even_after_the_problem_closed(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 이미 같은 사람이 같은 날 승인해 둔 것을 다시 보냈다 — **새로 쓰는 것이 없다.**

        여기서 막으면 사람은 *"승인이 안 됐나"* 하고 또 누른다.
        """
        already = _row(status="APPROVED", approved_as_of=D8, approved_by="operator")
        monkeypatch.setattr(repository, "select_proposal", lambda *_, **__: already)
        monkeypatch.setattr(repository, "exception_status", lambda *_, **__: "RESOLVED")
        monkeypatch.setattr(repository, "transition_proposal", _never_transitions)
        row = service.approve_proposal(
            conn, sim_run_id=SIM, proposal_id=PRP, as_of=D8, approved_by="operator"
        )
        assert row is already
        assert conn.commits == 0


def _never_transitions(*_: Any, **__: Any) -> int:
    raise AssertionError("재시도인데 UPDATE 를 걸었다")


class TestStoredPayload:
    """무엇을 저장하나 (§13 · §22 · §43 · §51)."""

    def _stored(self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
        calls = _stub_repository(monkeypatch)
        result = _result(**kwargs)
        outcome = service.create_proposal(
            conn, result=result, as_of=result.as_of, proposed_by="operator"
        )
        assert outcome.created
        return calls["insert"][0]

    def test_the_observation_date_is_carried_verbatim(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._stored(conn, monkeypatch).observed_as_of == D1

    def test_an_unknown_observation_date_stays_unknown(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §22 — 제안일·승인일·현재시각으로 메우지 않는다."""
        stored = self._stored(conn, monkeypatch, observed_as_of=None)
        assert stored.observed_as_of is None

    def test_the_business_day_is_the_proposal_date(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._stored(conn, monkeypatch).proposed_as_of == D5

    def test_cited_evidence_is_stored_as_the_calls_themselves(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §43 — 번호만 적으면 가리킬 곳이 없는 포인터가 된다 (조사는 DB 에 안 남는다)."""
        stored = self._stored(conn, monkeypatch)
        assert [one["tool_name"] for one in stored.evidence_refs] == ["get_lot"]
        assert stored.evidence_refs[0]["sequence"] == 1
        assert stored.evidence_refs[0]["observed_as_of"] == D1.isoformat()

    def test_the_whole_tool_answer_is_not_copied(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **제안은 조사 로그 저장소가 아니다.**

        Tool 답 전체를 제안마다 복사하면 같은 데이터가 제안 수만큼 늘고, Tool 스키마가
        바뀌면 과거 payload 해석이 얽히고, «조사 기록» 과 «승인 대상» 의 책임이 한 칸에
        섞인다.
        """
        (cited,) = self._stored(conn, monkeypatch).evidence_refs
        assert "answer" not in cited, cited
        assert set(cited) == {
            "sequence",
            "tool_name",
            "arguments",
            "facts",
            "observed_as_of",
            "uncertainties",
        }

    def test_only_the_facts_the_decision_used_are_kept(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """승인 판단에 실제로 쓴 칸만 남는다 — Lot 한 줄 20칸을 통째로 옮기지 않는다."""
        (cited,) = self._stored(conn, monkeypatch).evidence_refs
        assert cited["facts"] == {
            "lot_id": LOT,
            "item_id": "ITEM-BAECHU",
            "status": "ACTIVE",
            "remaining_qty_kg": "700",
            "remaining_freshness_days": 2,
            "uncommitted_kg": "500",
        }
        # ⚠️ `Decimal` 은 문자열로 낮춘다 — `float` 을 지나면 값이 조용히 흔들린다.
        assert isinstance(cited["facts"]["remaining_qty_kg"], str)
        # 🔴 원가 · 정책 한계 · 관측일 파생값 따위는 안 옮긴다.
        assert "unit_cost_krw_per_kg" not in cited["facts"]
        assert "freshness_remaining_ratio" not in cited["facts"]

    def test_what_the_tool_could_not_see_is_preserved_too(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ *"확인했고 문제 없음"* 과 *"확인을 못 했음"* 의 구별이 근거에도 남아야 한다."""
        (cited,) = self._stored(conn, monkeypatch).evidence_refs
        assert cited["uncertainties"] == ["COMMITMENT_UNRESOLVED"]

    def test_an_unexpected_answer_shape_does_not_kill_the_proposal(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 Tool 모양이 달라졌다고 **제안을 버리지 않고, 없는 값을 지어내지도 않는다.**"""
        drifted = _result()
        moved = replace(drifted.tool_calls[0], answer={"lot_id": LOT, "잔량": 700})
        stored = self._stored(
            conn, monkeypatch, tool_calls=(moved, *drifted.tool_calls[1:])
        )
        (cited,) = stored.evidence_refs
        assert cited["facts"] == {}
        assert service.EVIDENCE_FACTS_UNAVAILABLE in cited["uncertainties"]
        # ★ 그래도 제안은 섰다.
        assert stored.action_type == "SALES_PRIORITY_REQUEST"

    def test_no_tool_is_called_again_while_storing(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **근거는 조사 때 받아 둔 답에서만 나온다.**

        지금 DB 를 다시 읽으면 **오늘 값**을 그날의 근거처럼 보여 주게 된다.
        """
        for name in tool_dispatch.TOOL_EXECUTORS:
            monkeypatch.setitem(tool_dispatch.TOOL_EXECUTORS, name, _tool_must_not_run)
        for name in ("get_lot", "get_item_lots", "get_capacity_context", "get_policy"):
            monkeypatch.setattr(tools, name, _tool_must_not_run)
        stored = self._stored(conn, monkeypatch)
        assert stored.evidence_refs[0]["facts"]["lot_id"] == LOT

    def test_the_impact_answer_is_not_copied_into_the_evidence(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §29 — 같은 숫자를 두 칸에 두면 갈라졌을 때 정본을 못 댄다.

        영향의 정본은 `impact_json` 하나다. 근거에는 **봉투만** 남는다.
        """
        probe = ToolCallRecord(
            sequence=9,
            tool_name="estimate_action_impact",
            arguments={"action": "SALES_PRIORITY_REQUEST"},
            status=ToolCallStatus.SUCCESS,
            answer=_impact(),
            observed_as_of=D1,
        )
        base = _result()
        stored = self._stored(
            conn,
            monkeypatch,
            options=(_option(evidence_refs=(9,)),),
            tool_calls=(*base.tool_calls, probe),
        )
        (cited,) = stored.evidence_refs
        assert cited["tool_name"] == "estimate_action_impact"
        assert cited["facts"] == {}
        assert "candidate_kg" not in str(cited)
        # ★ 숫자는 저쪽에 살아 있다.
        assert stored.impact["candidate_kg"] == "500"

    def test_the_capacity_window_is_not_copied_whole(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §5 — `cap_by_date` 18일 창을 통째로 옮기지 않는다. **그 하루만** 남긴다."""
        window = {D5 + timedelta(days=offset): Decimal(100 + offset) for offset in range(18)}
        probe = ToolCallRecord(
            sequence=9,
            tool_name="get_capacity_context",
            arguments={},
            status=ToolCallStatus.SUCCESS,
            answer=_capacity_view(window),
            observed_as_of=None,
        )
        base = _result()
        stored = self._stored(
            conn,
            monkeypatch,
            options=(
                _option(
                    action="PURCHASE_ADJUST_REQUEST",
                    parameters={"qty_delta_kg": Decimal(-300), "arrival_date": D8},
                    evidence_refs=(9,),
                ),
            ),
            tool_calls=(*base.tool_calls, probe),
        )
        (cited,) = stored.evidence_refs
        facts = cited["facts"]
        assert facts["arrival_date"] == D8.isoformat()
        assert facts["available_capacity_kg"] == "103"
        assert facts["cap_window_days"] == 18
        # 🔴 나머지 17일은 어디에도 없다.
        assert (D5 + timedelta(days=1)).isoformat() not in str(cited)

    def test_the_impact_is_the_tool_answer_not_a_new_number(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 §51 — `candidate_kg` 는 Tool 이 낸 값이고 문자열로 실린다 (float 금지)."""
        stored = self._stored(conn, monkeypatch)
        assert stored.impact["feasibility"] == "FEASIBLE"
        assert stored.impact["candidate_kg"] == "500"

    def test_the_investigation_outcome_is_recorded(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored = self._stored(conn, monkeypatch, finish_reason=FinishReason.BUDGET_EXCEEDED)
        assert stored.source_finish_reason == "BUDGET_EXCEEDED"
        assert stored.source_llm_status == "SUCCESS"

    def test_the_investigation_id_is_empty_because_nothing_stores_it(
        self, conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ Commit 4 의 조사는 DB 에 안 남는다 — 없는 값을 지어내 채우지 않는다 (§44)."""
        assert self._stored(conn, monkeypatch).investigation_id is None


# ══════════════════════════════════════════════════════════════════════════
#  상태 어휘와 표 경계 — 소스를 읽어 막는다
# ══════════════════════════════════════════════════════════════════════════


def _tool_must_not_run(*_: Any, **__: Any) -> Any:
    raise AssertionError("제안을 저장하며 Tool 을 다시 불렀다")


class TestTransitionVocabulary:
    def test_commit_five_does_not_open_the_execution_transitions(self) -> None:
        """🔴 §5 — `EXECUTED` · `FAILED` 는 어휘로만 있다. 옮기는 길을 안 연다."""
        for forbidden in ("EXECUTED", "FAILED", "PROPOSED"):
            with pytest.raises(ValueError, match="전이"):
                repository.transition_proposal(
                    None,
                    sim_run_id=SIM,
                    proposal_id=PRP,
                    to_status=forbidden,
                    as_of=D8,
                )

    def test_a_new_proposal_can_only_start_as_proposed(self) -> None:
        """★ «처음부터 승인된» 제안을 넣는 길을 안 열어 둔다."""
        with pytest.raises(ValueError, match="PROPOSED"):
            repository.insert_proposal(None, row=_row(status="APPROVED"))

    def test_the_status_vocabulary_matches_the_state_machine(self) -> None:
        assert repository.LIVE_PROPOSAL_STATUSES == ("PROPOSED", "APPROVED")
        # 🔴 «끝난 상태» 와 «아직 안 끝난 상태» 가 합쳐 어휘 전부다.
        assert set(repository.TERMINAL_DATE_STATUSES.values()) | {"PROPOSED", "APPROVED"} == set(
            repository.PROPOSAL_STATUSES
        )

    def test_approval_is_not_a_terminal_outcome(self) -> None:
        """🔴 **승인은 끝이 아니라 실행 대기다** (Commit 6 · §17).

        `approved_as_of` 를 «끝난 날» 로 세면 `D6 승인 · D8 실행` 인 정상 흐름에서
        새 제안의 날짜 하한이 승인일로 잡히고, 실행이 끝난 뒤에도 그 제안이 여전히
        길을 막는 것처럼 보인다.
        """
        assert "approved_as_of" not in repository.TERMINAL_DATE_STATUSES
        assert repository.TERMINAL_DATE_STATUSES["executed_as_of"] == "EXECUTED"
        assert repository.TERMINAL_DATE_STATUSES["failed_as_of"] == "FAILED"


class TestWriteBoundary:
    """🔴 **이 표를 쓰는 자리가 하나뿐이어야 한다** (§61).

    ⚠️ 소스를 읽어 막는다 — 경로를 밟아서 잡으려면 **밟지 않은 분기의 우회는 영원히
       안 보인다.** Commit 4 의 `run_tool` 단일 호출 검사와 같은 규율이다.
    """

    WRITES = ("INSERT INTO", "UPDATE ", "DELETE FROM")
    TABLE = "logistics_action_proposals"

    def _writers(self) -> set[str]:
        root = Path(repository.__file__).resolve().parents[3]
        found: set[str] = set()
        for path in (root / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                text = node.value
                if self.TABLE in text and any(verb in text for verb in self.WRITES):
                    found.add(path.name)
        return found

    def test_only_the_proposal_repository_writes_the_table(self) -> None:
        assert self._writers() == {"proposals.py"}, self._writers()

    def test_the_service_owns_the_transaction_and_the_repository_does_not(self) -> None:
        """🔴 저장소는 커밋도 롤백도 안 한다 — 그 약속의 반대편이 서비스다 (§33 · §34)."""
        source = Path(repository.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        committed = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback"}
        ]
        assert committed == []

    def test_no_llm_is_reachable_from_the_approval_path(self) -> None:
        """🔴 §35 — 승인·거절은 사람이 이미 내린 결정이다. 모델에게 되묻지 않는다."""
        for module in (repository, service):
            source = Path(module.__file__).read_text(encoding="utf-8")
            imports = [
                name.name
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom)
                for name in node.names
                if node.module is not None
            ]
            modules = [
                node.module
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom) and node.module is not None
            ]
            assert not any("llm" in one for one in modules), (module.__name__, modules)
            assert not any(one.endswith("LLMClient") for one in imports)
