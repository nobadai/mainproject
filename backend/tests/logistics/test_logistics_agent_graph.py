"""조사 Runtime — **LLM 없이** 그래프·guard·예산·역할 경계를 검사한다.

🔴 **실 Provider 를 요구하지 않는다.** 계획자/정리자는 평범한 함수로 꽂고, Tool 은
   가짜로 바꾼다 (매입 `selector` · 물류 `FakeProvider` 와 같은 팀 관례).

여기서 지키는 것:

```text
① guard        지어낸 Tool · 틀린 인자 · 못 박은 축 · 범위 밖 · 중복 · 예산
② 예산         Tool 8회 · 재계획 2회 · LLM 11회 · 시간 상한
③ 숫자 주인    모델이 «99999kg» 이라고 해도 권위 있는 칸에 안 오른다
④ 역할 경계    판매 수량은 영업이 · 도착일은 매입이 정한다
⑤ 관측일       하나라도 모르면 None — as_of 로 메우지 않는다
```
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.logistics.agent import graph as graph_module
from app.logistics.agent import llm_client
from app.logistics.agent.investigation import (
    DUPLICATE_TOOL_CALL,
    INVALID_ARGUMENTS,
    PINNED_ARGUMENT_OVERRIDE,
    SUBJECT_OUT_OF_SCOPE,
    TOOL_BUDGET_EXCEEDED,
    UNKNOWN_TOOL,
    AgentLLMBudgetExceeded,
    AgentLLMDisabled,
    FinishReason,
    InvestigationBudget,
    InvestigationOption,
    InvestigationReport,
    InvestigationState,
    InvestigationStep,
    InvestigationView,
    ToolCallStatus,
    collect_ids,
    jsonable,
)
from app.logistics.agent.llm_client import (
    AgentLLMClient,
    AgentLLMSettings,
    PlannerContractError,
)
from app.logistics.agent.schemas import CAPACITY_PRESSURE, FRESHNESS_PRESSURE
from app.logistics.agent.tool_dispatch import call_key, guard_step
from app.logistics.agent.tools import (
    ActionImpact,
    CapacityContext,
    ExceptionFact,
    InboundPlan,
    InboundScheduleFact,
    ItemLots,
    LotFact,
    LotView,
    OpenExceptions,
    PolicyView,
    SalesCommitments,
)

AS_OF = date(2026, 8, 21)
RUN = "SIM-RUN-1"
LOT = "LOT-BAECHU-1"
ITEM = "ITEM-BAECHU"
EXC = "EXC-1"


# ══════════════════════════════════════════════════════════════════════════
#  가짜 사실 — Tool 이 낼 법한 모양 그대로
# ══════════════════════════════════════════════════════════════════════════


def _lot_fact(*, lot_id: str = LOT, item_id: str = ITEM, observed: date | None = None) -> LotFact:
    return LotFact(
        lot_id=lot_id,
        item_id=item_id,
        item="배추",
        grade="상",
        storage_zone="A",
        status="ACTIVE",
        received_at=date(2026, 8, 15),
        remaining_qty_kg=Decimal(500),
        unit_cost_krw_per_kg=Decimal(1200),
        remaining_freshness_days=1,
        effective_freshness_limit_days=7,
        turnover_status="SELL_PRIORITY",
        sell_priority=True,
        sell_priority_remaining_days=1,
        disposal_candidate=False,
        committed_kg=Decimal(100),
        uncommitted_kg=Decimal(400),
        remaining_qty_observed_as_of=observed,
        status_observed_as_of=observed,
    )


def _exception_fact(
    *, code: str = FRESHNESS_PRESSURE, subject_type: str = "LOT", subject_id: str = LOT
) -> ExceptionFact:
    return ExceptionFact(
        exception_id=EXC,
        code=code,
        subject_type=subject_type,
        subject_id=subject_id,
        opened_as_of=date(2026, 8, 19),
        detector_version="v1",
        previous_exception_id=None,
        open_days=3,
        status="OPEN",
        detail_known=True,
        severity="HIGH",
        evidence=(),
        last_detected_as_of=AS_OF,
        note=None,
        evidence_observed_as_of=None,
        unresolved_details=(),
    )


def _answer(cls: type, *, observed: date | None = None, **fields: Any) -> Any:
    return cls(
        sim_run_id=RUN, as_of=AS_OF, observed_as_of=observed, uncertainties=(), **fields
    )


def _default_answers(*, observed: date | None = None) -> dict[str, Any]:
    """8개 Tool 각각이 낼 기본 답. 시험마다 필요한 것만 갈아 끼운다."""
    return {
        "get_open_exceptions": _answer(OpenExceptions, exceptions=(_exception_fact(),)),
        "get_lot": _answer(LotView, observed=observed, lot=_lot_fact(observed=observed)),
        "get_item_lots": _answer(
            ItemLots, observed=observed, item_id=ITEM, lots=(_lot_fact(observed=observed),)
        ),
        "get_sales_commitments": _answer(
            SalesCommitments,
            observed=observed,
            item_id=ITEM,
            live_reservations=(),
            unallocated_kg=Decimal(0),
            next_due_date=None,
            confirmed_outbound_by_date={},
        ),
        "get_policy": _answer(
            PolicyView,
            observed=observed,
            item_id=None,
            item_policy=None,
            agent_policy={},
            policy_source_refs={},
            policy_version="v1",
        ),
        "get_capacity_context": _answer(
            CapacityContext,
            observed=observed,
            used_kg=Decimal(9500),
            guaranteed_kg=Decimal(10000),
            burst_kg=Decimal(12000),
            available_kg=Decimal(500),
            window_usage_ratio=Decimal("0.95"),
            cap_by_date={date(2026, 8, 25): Decimal(500)},
            inbound_lead_days=3,
            capacity_tight_ratio=Decimal("0.95"),
            capacity_basis="CURRENT_ACTIVE_POLICY",
        ),
        "get_inbound_schedule": _answer(
            InboundPlan,
            observed=observed,
            days=18,
            schedules=(
                InboundScheduleFact(
                    inbound_id="IN-1",
                    purchase_id="PO-1",
                    item_id=ITEM,
                    item="배추",
                    quantity_kg=Decimal(800),
                    expected_arrival_date=date(2026, 8, 25),
                    created_as_of=date(2026, 8, 20),
                    has_receipt=False,
                    stock_applied=False,
                ),
            ),
        ),
        "estimate_action_impact": ActionImpact(
            sim_run_id=RUN,
            as_of=AS_OF,
            observed_as_of=observed,
            uncertainties=(),
            action="SALES_PRIORITY_REQUEST",
            feasibility="UNRESOLVED",
            affected_kg=None,
            capacity_delta_kg=None,
            candidate_kg=Decimal(400),
            estimated_loss_krw=None,
            freshness_days_left=1,
        ),
    }


class _ToolSpy:
    """Tool 을 대신 받는다. 무엇이 어떤 인자로 불렸는지 **순서대로** 남긴다."""

    def __init__(self, answers: Mapping[str, Any], *, failures: Mapping[str, Exception] = {}):
        self.answers = dict(answers)
        self.failures = dict(failures)
        self.calls: list[tuple[str, dict[str, Any], str, date]] = []

    def __call__(
        self,
        conn: Any,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        sim_run_id: str,
        as_of: date,
    ) -> Any:
        del conn
        self.calls.append((tool_name, dict(arguments), sim_run_id, as_of))
        if tool_name in self.failures:
            raise self.failures[tool_name]
        return self.answers[tool_name]

    @property
    def names(self) -> list[str]:
        return [name for name, _, _, _ in self.calls]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _ToolSpy:
    """Tool 층을 통째로 갈아 끼운다 — DB 도 Provider 도 필요 없다."""
    answers = _default_answers()
    tools = _ToolSpy(answers)
    monkeypatch.setattr(graph_module, "run_tool", tools)
    monkeypatch.setattr(
        graph_module,
        "get_open_exceptions",
        lambda conn, *, sim_run_id, as_of: answers["get_open_exceptions"],
    )
    return tools


def _plan(*steps: InvestigationStep) -> Any:
    """정해진 계획을 순서대로 내는 계획자. 다 쓰면 마지막 것을 반복한다."""
    queue = list(steps)

    def plan_fn(view: Any) -> InvestigationStep:
        del view
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return plan_fn


def _report(**fields: Any) -> Any:
    report = InvestigationReport(**fields)
    return lambda view: report


def _call(tool_name: str, **arguments: Any) -> InvestigationStep:
    return InvestigationStep(action="CALL_TOOL", tool_name=tool_name, arguments=arguments)


FINISH = InvestigationStep(action="FINISH", reason="충분하다")


def _run(spy: _ToolSpy, *, plan_fn: Any, finalize_fn: Any = None, **kwargs: Any) -> Any:
    del spy
    return graph_module.run_investigation(
        conn=None,
        sim_run_id=RUN,
        as_of=AS_OF,
        exception_id=EXC,
        plan_fn=plan_fn,
        finalize_fn=finalize_fn or _report(summary="정리"),
        **kwargs,
    )


# ══════════════════════════════════════════════════════════════════════════
#  ① guard — 순수 판정
# ══════════════════════════════════════════════════════════════════════════


class TestGuard:
    def _verdict(self, step: InvestigationStep, **overrides: Any) -> Any:
        options: dict[str, Any] = {
            "allowed_lot_ids": frozenset({LOT}),
            "allowed_item_ids": frozenset({ITEM}),
            "executed_keys": frozenset(),
            "tool_call_count": 0,
            "max_tool_calls": 8,
        }
        options.update(overrides)
        return guard_step(step, **options)

    def test_an_invented_tool_name_is_rejected_by_name(self) -> None:
        """🔴 지어낸 Tool 은 **이름째** 거부된다 — 무엇을 지어냈는지가 기록에 남아야 한다."""
        verdict = self._verdict(_call("run_sql", query="DROP TABLE"))
        assert not verdict.approved
        assert verdict.rejection == f"{UNKNOWN_TOOL}:run_sql"

    @pytest.mark.parametrize("name", ["execute_purchase", "sell_inventory", "update_exception"])
    def test_no_write_shaped_tool_can_ever_pass(self, name: str) -> None:
        assert self._verdict(_call(name)).rejection == f"{UNKNOWN_TOOL}:{name}"

    def test_a_model_cannot_ask_about_another_run(self) -> None:
        """🔴 `sim_run_id` 는 조용히 덮지 않고 **거부**한다."""
        verdict = self._verdict(_call("get_lot", lot_id=LOT, sim_run_id="OTHER"))
        assert not verdict.approved
        assert verdict.rejection == f"{PINNED_ARGUMENT_OVERRIDE}:sim_run_id"

    def test_a_model_cannot_ask_about_another_day(self) -> None:
        verdict = self._verdict(_call("get_lot", lot_id=LOT, as_of="2030-01-01"))
        assert verdict.rejection == f"{PINNED_ARGUMENT_OVERRIDE}:as_of"

    def test_both_pinned_axes_are_named_together(self) -> None:
        verdict = self._verdict(
            _call("get_lot", lot_id=LOT, sim_run_id="OTHER", as_of="2030-01-01")
        )
        assert verdict.rejection == f"{PINNED_ARGUMENT_OVERRIDE}:sim_run_id,as_of"

    def test_an_unknown_argument_key_never_reaches_the_tool(self) -> None:
        verdict = self._verdict(_call("get_lot", lot_id=LOT, colour="red"))
        assert not verdict.approved
        assert verdict.rejection.startswith(INVALID_ARGUMENTS)

    def test_a_missing_required_argument_is_caught_here(self) -> None:
        assert self._verdict(_call("get_lot")).rejection.startswith(INVALID_ARGUMENTS)

    def test_a_wrong_type_is_caught_here(self) -> None:
        assert self._verdict(_call("get_inbound_schedule", days="곧")).rejection.startswith(
            INVALID_ARGUMENTS
        )

    def test_an_action_outside_the_catalog_is_caught_here(self) -> None:
        """카탈로그는 닫혀 있다 — `UNSUPPORTED` 답을 받으려고 예산을 태우지 않는다."""
        verdict = self._verdict(_call("estimate_action_impact", action="SHIP_EVERYTHING"))
        assert verdict.rejection.startswith(INVALID_ARGUMENTS)

    def test_an_unrelated_lot_is_out_of_scope(self) -> None:
        verdict = self._verdict(_call("get_lot", lot_id="LOT-RANDOM-999"))
        assert verdict.rejection == f"{SUBJECT_OUT_OF_SCOPE}:lot_id=LOT-RANDOM-999"

    def test_the_subject_lot_itself_is_in_scope(self) -> None:
        assert self._verdict(_call("get_lot", lot_id=LOT)).approved

    def test_the_subject_item_lots_are_a_legitimate_widening(self) -> None:
        """★ 같은 품목의 다른 Lot 은 조사상 정당하다 — 막지 않는다 (§17)."""
        assert self._verdict(_call("get_item_lots", item_id=ITEM)).approved

    def test_an_unrelated_item_is_out_of_scope(self) -> None:
        verdict = self._verdict(_call("get_item_lots", item_id="ITEM-MU"))
        assert verdict.rejection == f"{SUBJECT_OUT_OF_SCOPE}:item_id=ITEM-MU"

    def test_warehouse_context_needs_no_subject(self) -> None:
        """용량·입고·정책·목록은 대상 인자가 없다 — 언제나 창고 문맥이다."""
        for step in (
            _call("get_capacity_context"),
            _call("get_inbound_schedule"),
            _call("get_open_exceptions"),
            _call("get_policy"),
        ):
            assert self._verdict(step).approved, step.tool_name

    def test_an_impact_on_a_foreign_lot_is_out_of_scope(self) -> None:
        verdict = self._verdict(
            _call("estimate_action_impact", action="ACCEPT_RISK", parameters={"lot_id": "LOT-X"})
        )
        assert verdict.rejection == f"{SUBJECT_OUT_OF_SCOPE}:lot_id=LOT-X"

    def test_the_same_call_twice_is_a_duplicate(self) -> None:
        first = self._verdict(_call("get_lot", lot_id=LOT))
        again = self._verdict(_call("get_lot", lot_id=LOT), executed_keys=frozenset({first.key}))
        assert again.rejection == f"{DUPLICATE_TOOL_CALL}:get_lot"

    def test_the_ninth_tool_call_is_refused(self) -> None:
        verdict = self._verdict(_call("get_lot", lot_id=LOT), tool_call_count=8)
        assert verdict.rejection == f"{TOOL_BUDGET_EXCEEDED}:8"

    def test_argument_order_does_not_make_a_new_call(self) -> None:
        """🔴 키 순서가 달라도 **같은 질문**이다 — 안 그러면 중복 guard 를 쉽게 우회한다."""
        assert call_key("get_lot", {"a": 1, "b": 2}) == call_key("get_lot", {"b": 2, "a": 1})

    def test_a_json_date_string_is_restored_for_the_tool(self) -> None:
        """⚠️ Tool 은 `date` 만 `cap_by_date` 키와 맞춘다 — 전송이 낮춘 표현을 되돌린다."""
        verdict = self._verdict(
            _call(
                "estimate_action_impact",
                action="PURCHASE_ADJUST_REQUEST",
                parameters={"qty_delta_kg": -100, "arrival_date": "2026-08-25"},
            )
        )
        assert verdict.approved
        assert verdict.arguments["parameters"]["arrival_date"] == date(2026, 8, 25)

    def test_a_json_float_is_restored_as_decimal(self) -> None:
        """🔴 `tools._quantity` 는 `float` 을 거부한다 — 그대로 보내면 이유 없는 UNRESOLVED 다."""
        verdict = self._verdict(
            _call(
                "estimate_action_impact",
                action="DISPOSAL_REQUEST",
                parameters={"lot_id": LOT, "qty_kg": 120.5},
            )
        )
        assert verdict.arguments["parameters"]["qty_kg"] == Decimal("120.5")


# ══════════════════════════════════════════════════════════════════════════
#  ② 그래프 — 정해진 계획으로 끝까지 돈다
# ══════════════════════════════════════════════════════════════════════════


class TestGraphRun:
    def test_a_fixed_plan_runs_the_tools_in_order(self, spy: _ToolSpy) -> None:
        """결정론 선행 3건 → 모델이 고른 2건 → 정리. **순서와 인자**를 그대로 검사한다."""
        result = _run(
            spy,
            plan_fn=_plan(_call("get_item_lots", item_id=ITEM), FINISH),
        )
        assert spy.names == [
            "get_policy",
            "get_lot",
            "get_sales_commitments",
            "get_item_lots",
        ]
        assert result.finish_reason is FinishReason.FINISHED
        assert result.llm_status == "SUCCESS"
        assert result.tool_call_count == 1

    def test_the_preload_does_not_eat_the_model_budget(self, spy: _ToolSpy) -> None:
        """★ 예산 8회는 **모델이 스스로 고른 호출**을 센다 — 선행 조회는 안 센다."""
        result = _run(spy, plan_fn=_plan(FINISH))
        assert result.tool_call_count == 0
        assert len(spy.names) == 3

    def test_every_tool_is_pinned_to_the_request_axes(self, spy: _ToolSpy) -> None:
        """🔴 모든 호출이 요청의 `sim_run_id` · `as_of` 로 간다 — 예외가 없다."""
        _run(spy, plan_fn=_plan(_call("get_item_lots", item_id=ITEM), FINISH))
        assert {(run, day) for _, _, run, day in spy.calls} == {(RUN, AS_OF)}

    def test_a_missing_exception_ends_the_investigation(self, spy: _ToolSpy) -> None:
        """🔴 모델에게 *"이게 있을까"* 를 묻지 않는다 — 첫 노드가 결정론으로 닫는다."""
        result = graph_module.run_investigation(
            conn=None,
            sim_run_id=RUN,
            as_of=AS_OF,
            exception_id="EXC-NOPE",
            plan_fn=_plan(FINISH),
            finalize_fn=_report(summary="여기 오면 안 된다"),
        )
        assert result.finish_reason is FinishReason.NOT_FOUND
        assert result.options == ()
        assert "EXCEPTION_NOT_FOUND:EXC-NOPE" in result.uncertainties
        # 계획자·정리자를 아예 부르지 않았다.
        assert result.llm_call_count == 0
        assert result.llm_status == "SKIPPED_TEMPLATE"

    def test_a_warehouse_subject_preloads_capacity_and_inbound(
        self, spy: _ToolSpy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = _default_answers()
        answers["get_open_exceptions"] = _answer(
            OpenExceptions,
            exceptions=(
                _exception_fact(
                    code=CAPACITY_PRESSURE, subject_type="WAREHOUSE", subject_id="WAREHOUSE"
                ),
            ),
        )
        monkeypatch.setattr(
            graph_module,
            "get_open_exceptions",
            lambda conn, *, sim_run_id, as_of: answers["get_open_exceptions"],
        )
        spy.answers = answers
        _run(spy, plan_fn=_plan(FINISH))
        assert spy.names == ["get_policy", "get_capacity_context", "get_inbound_schedule"]

    def test_a_tool_failure_does_not_stop_the_graph(
        self, spy: _ToolSpy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ Tool 예외는 관찰에 **실패로** 적고 계속한다 — 사실이 사라지는 편이 더 나쁘다."""
        failing = _ToolSpy(_default_answers(), failures={"get_item_lots": RuntimeError("db down")})
        monkeypatch.setattr(graph_module, "run_tool", failing)
        result = _run(spy, plan_fn=_plan(_call("get_item_lots", item_id=ITEM), FINISH))
        failed = [r for r in result.tool_calls if r.status is ToolCallStatus.FAILED]
        assert [r.tool_name for r in failed] == ["get_item_lots"]
        assert result.finish_reason is FinishReason.FINISHED
        # 🔴 실패는 성공 예산을 안 먹는다.
        assert result.tool_call_count == 0


# ══════════════════════════════════════════════════════════════════════════
#  ③ 예산 — 그래프 안에서
# ══════════════════════════════════════════════════════════════════════════


class TestBudget:
    def test_the_ninth_model_chosen_call_never_runs(self, spy: _ToolSpy) -> None:
        """8회를 다 쓰면 9번째는 Tool 까지 안 간다. 🔴 그래도 **정리는 한다.**"""
        # 같은 인자는 중복이 되므로 매번 다른 인자를 준다 — 여기서는 **예산만** 시험한다.
        varying = [_call("get_inbound_schedule", days=index) for index in range(9)]
        result = _run(spy, plan_fn=_plan(*varying))
        assert result.tool_call_count == 8
        executed = [r for r in result.tool_calls if r.status is ToolCallStatus.SUCCESS]
        # 결정론 선행 4 (목록 · 정책 · Lot · 약정) + 모델이 고른 8
        assert len(executed) == 12
        rejected = [r for r in result.tool_calls if r.status is ToolCallStatus.REJECTED]
        assert rejected[-1].detail == f"{TOOL_BUDGET_EXCEEDED}:8"
        assert result.finish_reason is FinishReason.BUDGET_EXCEEDED
        assert result.llm_status == "SUCCESS"

    def test_a_third_rejection_ends_the_investigation(self, spy: _ToolSpy) -> None:
        """재계획 2회까지다. 세 번째 거부는 규칙 제안으로 닫는다."""
        result = _run(spy, plan_fn=_plan(_call("run_sql", query="x")))
        assert result.replan_count == 3
        assert result.finish_reason is FinishReason.GUARD_EXHAUSTED
        assert "REPLAN_BUDGET_EXCEEDED:2" in result.uncertainties
        assert result.llm_status == "FALLBACK"
        assert len(result.options) == 1

    def test_a_repeated_identical_call_is_guarded_in_the_graph(self, spy: _ToolSpy) -> None:
        """```text
        get_lot LOT · get_lot LOT · get_lot LOT …   ← 같은 질문을 반복하지 못한다
        ```"""
        result = _run(spy, plan_fn=_plan(_call("get_lot", lot_id=LOT)))
        rejections = [r.detail for r in result.tool_calls if r.status is ToolCallStatus.REJECTED]
        # 선행이 이미 `get_lot` 을 돌았으므로 첫 요청부터 중복이다.
        assert rejections == [f"{DUPLICATE_TOOL_CALL}:get_lot"] * 3
        assert result.finish_reason is FinishReason.GUARD_EXHAUSTED

    def test_the_llm_call_ceiling_is_eleven(self, spy: _ToolSpy) -> None:
        """plan/replan + finalize 를 **다 합쳐** 11회다."""
        budget = InvestigationBudget(max_tool_calls=8, max_replans=99, max_llm_calls=3)
        result = _run(spy, plan_fn=_plan(_call("run_sql", query="x")), budget=budget)
        assert result.llm_call_count == 3
        assert result.finish_reason is FinishReason.BUDGET_EXCEEDED
        assert result.llm_status == "FALLBACK"

    def test_an_expired_investigation_stops_without_options(self, spy: _ToolSpy) -> None:
        """시간이 없으면 **더 캐지 않는다.** 없는 근거로 제안을 만들지 않는다."""
        result = _run(
            spy,
            plan_fn=_plan(FINISH),
            budget=InvestigationBudget(timeout_seconds=0.0),
        )
        assert result.finish_reason is FinishReason.TIMEOUT
        assert result.options == ()
        assert result.llm_call_count == 0


# ══════════════════════════════════════════════════════════════════════════
#  ④ 공급자 실패 — DB 는 아무것도 안 바뀐다
# ══════════════════════════════════════════════════════════════════════════


class TestProviderFailure:
    def _boom(self, view: Any) -> Any:
        raise TimeoutError("provider timed out")

    def test_a_planner_failure_degrades_to_a_rule_proposal(self, spy: _ToolSpy) -> None:
        result = _run(spy, plan_fn=self._boom)
        assert result.finish_reason is FinishReason.LLM_FAILED
        assert result.llm_status == "FALLBACK"
        assert result.llm_error_kind == "TIMEOUT"
        assert not result.llm_applied
        assert len(result.options) == 1

    def test_a_finalizer_failure_degrades_the_same_way(self, spy: _ToolSpy) -> None:
        """🔴 `finalize` 가 터졌는데 `llm_status` 를 SUCCESS 로 두면

        규칙 제안이 AI 판단으로 보인다.
        """
        result = _run(spy, plan_fn=_plan(FINISH), finalize_fn=self._boom)
        assert result.llm_status == "FALLBACK"
        assert result.finish_reason is FinishReason.LLM_FAILED
        assert len(result.options) == 1

    def test_a_disabled_provider_is_not_a_failure(self, spy: _ToolSpy) -> None:
        """꺼 둔 것은 장애가 아니다 — 결정론으로 끝까지 가고 규칙 제안 하나를 낸다."""

        def off(view: Any) -> Any:
            raise AgentLLMDisabled("Logistics agent LLM is turned off")

        result = _run(spy, plan_fn=off)
        assert result.llm_status == "DISABLED"
        assert result.finish_reason is FinishReason.FINISHED
        assert result.llm_error_kind is None
        assert len(result.options) == 1

    def test_a_provider_failure_writes_nothing(self, spy: _ToolSpy) -> None:
        """🔴 조사에 쓰기 경로가 **아예 없다** — Tool 8개가 전부 읽기다."""
        _run(spy, plan_fn=self._boom)
        assert all(name.startswith(("get_", "estimate_")) for name in spy.names)


# ══════════════════════════════════════════════════════════════════════════
#  ⑤ 숫자의 주인 · 역할 경계
# ══════════════════════════════════════════════════════════════════════════


class TestNumbersBelongToTools:
    def test_a_hallucinated_number_never_becomes_an_authoritative_field(
        self, spy: _ToolSpy
    ) -> None:
        """모델이 «재고는 99999kg» 이라고 해도 그 값은 **문장에만** 산다."""
        result = _run(
            spy,
            plan_fn=_plan(FINISH),
            finalize_fn=_report(
                summary="재고는 99999kg 입니다",
                findings=["잔량이 99999kg 이라 여유가 충분하다"],
                options=[
                    InvestigationOption(
                        action="SALES_PRIORITY_REQUEST",
                        parameters={"lot_id": LOT, "qty_kg": 99999},
                        rationale="다 팔자",
                        evidence_refs=[2],
                    )
                ],
                recommended_index=0,
            ),
        )
        option = result.options[0]
        # 권위 있는 숫자는 Tool 이 낸 impact 뿐이다.
        assert option.impact is not None
        assert option.impact.candidate_kg == Decimal(400)
        assert option.impact.feasibility == "UNRESOLVED"
        # 모델이 적은 수량은 **검토값**으로만 남는다.
        assert option.parameters_are_hypothesis is True
        assert "99999" in result.summary

    def test_an_unresolved_impact_is_never_talked_up(self, spy: _ToolSpy) -> None:
        """🔴 모델이 *"아마 될 것 같다"* 고 해도 Tool 이 `UNRESOLVED` 면 그대로다 (§27)."""
        result = _run(
            spy,
            plan_fn=_plan(FINISH),
            finalize_fn=_report(
                summary="가능합니다",
                options=[
                    InvestigationOption(
                        action="SALES_PRIORITY_REQUEST",
                        parameters={"lot_id": LOT},
                        rationale="확실히 가능",
                        evidence_refs=[],
                    )
                ],
            ),
        )
        assert result.options[0].impact.feasibility == "UNRESOLVED"

    def test_an_action_outside_the_catalog_is_dropped(self, spy: _ToolSpy) -> None:
        result = _run(
            spy,
            plan_fn=_plan(FINISH),
            finalize_fn=_report(
                summary="",
                options=[
                    InvestigationOption(action="SHIP_EVERYTHING", parameters={}, rationale="")
                ],
            ),
        )
        assert result.options[0].accepted is False
        assert result.options[0].rejected_reason == "ACTION_UNSUPPORTED:SHIP_EVERYTHING"
        assert result.recommended_index is None

    def test_an_invented_evidence_reference_is_pruned(self, spy: _ToolSpy) -> None:
        """근거 번호는 **이번 run 에 실제로 있는 것**만 남는다."""
        result = _run(
            spy,
            plan_fn=_plan(FINISH),
            finalize_fn=_report(
                summary="",
                options=[
                    InvestigationOption(
                        action="ACCEPT_RISK",
                        parameters={"lot_id": LOT},
                        rationale="",
                        evidence_refs=[2, 999],
                    )
                ],
            ),
        )
        assert result.options[0].evidence_refs == (2,)


class TestRoleBoundary:
    def test_the_freshness_rule_never_decides_a_sale_quantity(self, spy: _ToolSpy) -> None:
        """🔴 규칙 제안도 판매 수량을 정하지 않는다 (§9.1).

        ```text
        내는 것   lot_id                         «이 Lot 을 우선 판매 후보로»
        안 내는 것 qty_kg                         ← 영업이 정한다
        받는 것   candidate_kg (Tool 값)          «댈 수 있는 양은 이만큼»
        ```
        """
        result = _run(spy, plan_fn=lambda view: (_ for _ in ()).throw(TimeoutError("x")))
        option = result.options[0]
        assert option.action == "SALES_PRIORITY_REQUEST"
        assert "qty_kg" not in option.parameters
        assert option.decision_owner == "SALES"
        assert option.parameters_are_hypothesis is True
        assert option.impact.candidate_kg == Decimal(400)

    def test_the_capacity_rule_leaves_the_arrival_date_to_purchase(
        self, spy: _ToolSpy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """매입 조정 후보의 수량·도착일은 **Tool 이 낸 그 일정의 값**이고 검토값이다."""
        answers = _default_answers()
        answers["get_open_exceptions"] = _answer(
            OpenExceptions,
            exceptions=(
                _exception_fact(
                    code=CAPACITY_PRESSURE, subject_type="WAREHOUSE", subject_id="WAREHOUSE"
                ),
            ),
        )
        monkeypatch.setattr(
            graph_module,
            "get_open_exceptions",
            lambda conn, *, sim_run_id, as_of: answers["get_open_exceptions"],
        )
        spy.answers = answers
        result = _run(spy, plan_fn=lambda view: (_ for _ in ()).throw(TimeoutError("x")))
        option = result.options[0]
        assert option.action == "PURCHASE_ADJUST_REQUEST"
        assert option.decision_owner == "PURCHASE"
        assert option.parameters_are_hypothesis is True
        # 🔴 초과분을 따로 셈하지 않는다 — 그 일정의 수량 그대로다.
        assert option.parameters["qty_delta_kg"] == Decimal(-800)
        assert option.parameters["arrival_date"] == date(2026, 8, 25)

    def test_a_logistics_owned_action_is_not_a_hypothesis(self, spy: _ToolSpy) -> None:
        result = _run(
            spy,
            plan_fn=_plan(FINISH),
            finalize_fn=_report(
                summary="",
                options=[
                    InvestigationOption(
                        action="DISPOSAL_REQUEST",
                        parameters={"lot_id": LOT, "qty_kg": 10},
                        rationale="",
                    )
                ],
            ),
        )
        assert result.options[0].decision_owner == "LOGISTICS"
        assert result.options[0].parameters_are_hypothesis is False


# ══════════════════════════════════════════════════════════════════════════
#  ⑥ 관측일 — 하나라도 모르면 None
# ══════════════════════════════════════════════════════════════════════════


class TestObservedAsOf:
    def test_one_unknown_axis_makes_the_whole_thing_unknown(self, spy: _ToolSpy) -> None:
        """🔴 `as_of` · 오늘 날짜 · 모델 응답 시각으로 **절대** 메우지 않는다."""
        result = _run(spy, plan_fn=_plan(FINISH))
        assert result.observed_as_of is None

    def test_all_dated_evidence_gives_the_latest_date(
        self, spy: _ToolSpy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dated = _default_answers(observed=date(2026, 8, 18))
        dated["get_open_exceptions"] = _answer(
            OpenExceptions, observed=date(2026, 8, 17), exceptions=(_exception_fact(),)
        )
        dated["get_inbound_schedule"] = _answer(
            InboundPlan, observed=date(2026, 8, 20), days=18, schedules=()
        )
        monkeypatch.setattr(graph_module, "run_tool", _ToolSpy(dated))
        monkeypatch.setattr(
            graph_module,
            "get_open_exceptions",
            lambda conn, *, sim_run_id, as_of: dated["get_open_exceptions"],
        )
        result = _run(spy, plan_fn=_plan(_call("get_inbound_schedule", days=3), FINISH))
        assert result.observed_as_of == date(2026, 8, 20)

    def test_a_rejected_call_carries_no_date(self, spy: _ToolSpy) -> None:
        """거부된 수는 사실을 안 냈다 — 관측일에도 들면 안 된다."""
        result = _run(spy, plan_fn=_plan(_call("run_sql", query="x")))
        rejected = [r for r in result.tool_calls if r.status is ToolCallStatus.REJECTED]
        assert rejected and all(r.observed_as_of is None for r in rejected)


# ══════════════════════════════════════════════════════════════════════════
#  ⑦ 전송 예산 — «11회» 가 실제 상한이어야 한다
# ══════════════════════════════════════════════════════════════════════════


def _schema_violation(settings: Any, **kwargs: Any) -> str:
    """🔴 `action` 이 없는 후보 — `_parse_report` 가 건너뛰지 않고 **실제로** 스키마를 어긴다.

    ⚠️ `options: [1]` 같은 모양은 파서가 조용히 걸러 내 검증을 통과한다 — 그걸로 시험하면
       «교정 재전송» 경로를 한 번도 안 밟는다.
    """
    return '{"summary": "x", "options": [{"rationale": "no action key"}]}'


class TestProviderSendBudget:
    """🔴 **재시도와 스키마 교정도 전송이다.**

    그래프는 노드가 계획자/정리자를 부른 **횟수**를 센다. 그것만으로는 한 노드가 두 번
    보내는 경로(전송 재시도 · 스키마 1회 교정)를 못 막아 «11회» 가 최악 22회가 된다.
    공급자 층이 **나간 전송**을 따로 세는 이유다.
    """

    def _client(self, *, max_sends: int = 11) -> Any:
        settings = AgentLLMSettings(
            enabled=True,
            provider="ollama",
            model="m",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        )
        return AgentLLMClient(settings, max_sends=max_sends)

    def _view(self) -> Any:
        return InvestigationView(
            sim_run_id=RUN,
            as_of=AS_OF,
            exception={},
            scope={},
            observations=(),
            budget={},
            last_rejection=None,
        )

    def test_one_finalize_can_send_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """스키마를 어기면 **1회 교정**을 요구한다 — 그래서 한 노드가 2회 전송한다."""
        client = self._client()
        monkeypatch.setattr(llm_client, "_ollama_json", _schema_violation)
        with pytest.raises(PlannerContractError):
            client.finalize(self._view())
        assert client.sends == 2

    def test_the_send_ceiling_actually_stops_at_the_promised_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 약속한 숫자가 **실제 상한**이어야 한다 — 11번째에서 멈춘다."""
        client = self._client(max_sends=11)
        monkeypatch.setattr(llm_client, "_ollama_json", _schema_violation)
        for _ in range(20):
            try:
                client.finalize(self._view())
            except AgentLLMBudgetExceeded:
                break
            except PlannerContractError:
                continue
        else:  # pragma: no cover - 예산이 안 걸리면 시험 자체가 무의미하다
            pytest.fail("send budget never stopped the client")
        assert client.sends == 11

    def test_a_transport_retry_also_spends_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """전송 실패 재시도도 한 칸이다 — 세지 않으면 장애 때 예산이 조용히 두 배가 된다."""
        client = self._client(max_sends=2)

        def dead(settings: Any, **kwargs: Any) -> str:
            raise TimeoutError("provider timed out")

        monkeypatch.setattr(llm_client, "_ollama_json", dead)
        with pytest.raises(TimeoutError):
            client.finalize(self._view())
        assert client.sends == 2

    def test_the_graph_gives_the_client_its_own_budget(self, spy: _ToolSpy) -> None:
        """⚠️ 조사 한 번에 client 하나 — 재사용하면 두 번째 조사가 첫 번째 예산을 물려받는다."""
        made: list[int] = []

        def build(settings: Any = None, *, max_sends: int | None = None) -> tuple[Any, Any]:
            made.append(max_sends or 0)
            return _plan(FINISH), _report(summary="정리")

        import app.logistics.agent.llm_client as module

        original = module.build_agent_llm
        module.build_agent_llm = build
        try:
            graph_module.run_investigation(
                conn=None,
                sim_run_id=RUN,
                as_of=AS_OF,
                exception_id=EXC,
                budget=InvestigationBudget(max_llm_calls=5),
            )
        finally:
            module.build_agent_llm = original
        assert made == [5]


# ══════════════════════════════════════════════════════════════════════════
#  ⑧ 상태 계약 — 선언하지 않은 칸은 LangGraph 가 **조용히 버린다**
# ══════════════════════════════════════════════════════════════════════════


class TestStateContract:
    """🔴 **이 버그는 실제로 났다.** `guard` 가 `route` 를 돌려주는데 `InvestigationState`

    에 그 칸이 없어서 LangGraph 가 버렸고, 라우터는 늘 기본 갈래(`fallback`)로 갔다.
    아무 예외도 안 나고 테스트는 «규칙 제안이 나왔다» 며 초록불이었다.

    ⚠️ 소스를 읽어 막는다 — 경로를 하나씩 밟아서 잡으려면 **밟지 않은 분기의 칸은
       영원히 안 보인다.**
    """

    #: 🔴 노드가 아니면서 **노드 반환에 `**` 로 섞이는** 도우미들. 이것들이 만든 칸도
    #:    결국 상태로 가므로 같이 본다.
    SPREAD_HELPERS = ("_preload", "_rejection_record")

    def _checked_functions(self) -> set[str]:
        """검사 대상 = 그래프에 실제로 등록된 노드 + `**` 도우미.

        ★ 노드 목록을 **컴파일된 그래프에서** 가져온다 — 손으로 적으면 노드를 추가할 때
          이 검사만 조용히 뒤처진다.
        """
        compiled = graph_module.build_investigation_graph()
        nodes = {name for name in compiled.get_graph().nodes if not name.startswith("__")}
        assert len(nodes) >= 8, nodes
        return nodes | set(self.SPREAD_HELPERS)

    def _node_returns(self) -> dict[str, set[str]]:
        """**`return` 한 dict 리터럴**의 키만 긁는다 (모든 분기).

        ⚠️ 함수 안의 dict 를 전부 긁으면 안 된다 — Tool 인자(`{"lot_id": ...}`)까지 섞여
           «선언 안 된 칸» 으로 잘못 잡힌다.
        """
        wanted = self._checked_functions()
        tree = ast.parse(Path(graph_module.__file__).read_text(encoding="utf-8"))
        found: dict[str, set[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
                continue
            keys: set[str] = set()
            for child in ast.walk(node):
                if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
                    continue
                for key in child.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
            found[node.name] = keys
        return found

    def test_every_node_returns_only_declared_keys(self) -> None:
        declared = set(InvestigationState.__annotations__)
        returns = self._node_returns()
        assert returns, "노드를 하나도 못 찾았다 — 이 검사가 헛돌고 있다"
        undeclared = {
            name: sorted(keys - declared) for name, keys in returns.items() if keys - declared
        }
        assert not undeclared, f"State 에 없는 칸을 돌려주는 노드: {undeclared}"

    def test_the_router_keys_are_declared(self) -> None:
        """`route` 와 `verdict` 는 노드끼리만 쓰는 칸이라 놓치기 쉽다 — 이름으로 못 박는다."""
        declared = set(InvestigationState.__annotations__)
        assert {"route", "verdict", "recommended_index"} <= declared


# ══════════════════════════════════════════════════════════════════════════
#  ⑨ 직렬화 — 모델이 못 본 사실은 물어볼 수도 없다
# ══════════════════════════════════════════════════════════════════════════


class TestView:
    def test_computed_facts_survive_serialization(self) -> None:
        """🔴 `asdict` 만 쓰면 `@property` 로 된 계산 사실이 통째로 사라진다."""
        payload = jsonable(_lot_fact())
        assert payload["remaining_qty_kg"] == "500"
        assert "freshness_remaining_ratio" in payload
        assert "uncommitted_observed_as_of" in payload

    def test_decimals_never_become_floats(self) -> None:
        """⚠️ `float` 로 낮추면 다시 Tool 로 돌아올 때 `_quantity` 가 거부한다."""
        payload = jsonable({"qty": Decimal("120.5")})
        assert payload["qty"] == "120.5"

    def test_date_keys_are_stringified(self) -> None:
        payload = jsonable({date(2026, 8, 25): Decimal(500)})
        assert payload == {"2026-08-25": "500"}

    def test_scope_grows_only_from_what_the_evidence_showed(self) -> None:
        """★ *"이미 눈앞에 놓인 것만 더 물어볼 수 있다"* — 그것이 범위 규칙의 전부다."""
        answer = _default_answers()["get_item_lots"]
        assert collect_ids(answer, field_names=("lot_id",)) == frozenset({LOT})
        assert collect_ids(answer, field_names=("item_id",)) == frozenset({ITEM})
