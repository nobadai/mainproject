"""Finance Harness + LangChain Tool calling — **누가 무엇을 정하는가.**

이 파일이 지키는 것은 값이 아니라 **권한 경계**다.

    LLM        지금 부를 수 있는 Tool 중 하나를 고른다
    Harness    무엇이 지금 부를 수 있는 Tool 인지 정하고 강제한다
    Tool/Rule  재무 사실과 판정을 만든다

그래서 여기서 반복해서 확인하는 것은 하나다 —
**LLM 이 요청한 것 · Harness 가 허락한 것 · 실제로 돈 것**이 어긋나지 않는가.

실 LLM 을 부르지 않는다. Provider 전송 함수만 가짜다.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import user_messages as messages
from app.finance.application import harness as harness_module
from app.finance.application.harness import (
    CAPABILITY_OWNER,
    DEPENDENCY_NOT_SATISFIED,
    DUPLICATE_UNRESOLVED_TOOL_CALL,
    FINALIZE_TOOL_NAME,
    TOOL_BUDGET_EXHAUSTED,
    TOOL_DEPENDENCIES,
    TOOL_PERMISSION_DENIED,
    FinanceHarness,
    FinanceToolDenied,
    FinanceToolRegistry,
    build_planner_tool_adapter,
    validate_planner_tool_arguments,
)
from app.finance.application.orchestration import FinanceAgentController
from app.finance.capabilities.procurement import (
    FinancePreconditionMissing,
    analyze_payment_pressure,
    calculate_purchase_finance_cap,
)
from app.finance.db import FinanceDataNotReady
from app.finance.llm.planner import (
    FinancePlannerContractViolation,
    LangChainFinancePlanner,
    ToolAction,
    finance_chat_model,
)
from app.finance.schemas import FinancePolicy
from app.finance.state import FinanceAgentState
from app.finance.user_messages import explanation_keys
from app.master.envelope import AgentRequest, ExecutionContext


@contextmanager
def two_explanation_candidates():
    """설명 후보가 **둘인 상황**을 만든다.

    ★ 지금 계약에서는 모든 (mode · business_status · 조정여부) 조합의 설명 후보가
      하나뿐이라 Finalizer 가 불리지 않는다. 그래서 Finalizer 실패·숫자 주입·호출 시점
      처럼 **모델이 실제로 불릴 때만 의미가 있는** 계약은 이 자리를 빌려 시험한다.
      계약 자체는 예전 그대로다 — 바뀐 것은 «언제 부르는가» 뿐이다.
    """
    real_keys = explanation_keys

    def two_keys(mode, business_status, *, has_verified_adjustment=False):
        first = real_keys(
            mode, business_status, has_verified_adjustment=has_verified_adjustment
        )
        #  실제로 존재하는 키를 준다 — 없는 키를 주면 문장 조회가 먼저 터진다.
        second = (
            "SCENARIO_NOT_CONCLUDED"
            if first[0] != "SCENARIO_NOT_CONCLUDED"
            else "PRE_BOUNDARY"
        )
        return [first[0], second]

    with patch(
        "app.finance.application.orchestration.explanation_keys", side_effect=two_keys
    ):
        yield


PRE_ORDER = (
    "assess_finance_position",
    "project_cashflow",
    "calculate_purchase_finance_cap",
    "analyze_payment_pressure",
)


class Port:
    """Controller 가 쓰는 최소 Finance DataPort."""

    def load_finance_position(self, as_of):
        del as_of
        return {
            "finance_state_id": "FIN-STATE-HARNESS",
            "current_cash_krw": Decimal(1000),
            "current_debt_krw": Decimal(0),
        }

    def load_policy(self, as_of, policy_version):
        del as_of, policy_version
        return FinancePolicy(
            purchase_payment_days=1,
            payroll_date=10,
            monthly_labor_cost_krw=Decimal(100),
            minimum_cash_balance_krw=Decimal(100),
            cashflow_projection_days=30,
            cash_priority_reference="minimum_cash_balance_krw",
            cash_priority_high_ratio=Decimal(1),
            cash_priority_medium_ratio=Decimal(2),
            policy_version="v1.3-PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            source_refs={
                "payroll_date": "POL-PAYROLL-DATE",
                "monthly_labor_cost_krw": "FACT-PAYROLL-AMOUNT",
                "purchase_payment_days": "policy:purchase-days",
                "minimum_cash_balance_krw": "policy:min-cash",
                "cash_priority_reference": "policy:pressure",
                "cash_priority_high_ratio": "policy:pressure-high",
                "cash_priority_medium_ratio": "policy:pressure-medium",
            },
        )

    def load_payroll(self, as_of, horizon):
        del as_of, horizon
        return Decimal(100)

    def load_obligations(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_receivables(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_debt_schedule(self, as_of, horizon):
        del as_of, horizon
        return []


class ScriptedPlanner:
    model = "scripted-planner"

    def __init__(self, actions):
        self.actions = list(actions)
        self.attempts = 0
        self.seen_allowed: list[frozenset[str]] = []
        self.seen_tool_names: list[tuple[str, ...]] = []
        self.seen_observations: list[tuple[dict, ...]] = []

    def decide(self, *, allowed_tools, missing_capabilities, langchain_tools=(), **kwargs):
        del missing_capabilities
        self.attempts += 1
        self.seen_allowed.append(frozenset(allowed_tools))
        self.seen_tool_names.append(tuple(tool.name for tool in langchain_tools))
        self.seen_observations.append(tuple(kwargs.get("observations", ())))
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class Finalizer:
    model = "scripted-finalizer"

    def __init__(self):
        self.attempts = 0

    def finalize(self, *, mode, business_status, evidences, has_verified_adjustment=False):
        """★ 문장을 여기 다시 적지 않는다 — **정본을 그대로 고른다.**

        가짜가 자기 문장을 들고 있으면, 실제 실행을 도는 테스트가 사용자에게 나가는
        진짜 문장이 아니라 **가짜의 문장**을 검사하게 된다.
        """
        del evidences
        self.attempts += 1
        return messages.explanation_for(mode, business_status)


def request(mode="PRE_PURCHASE", payload=None):
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-harness",
            as_of=date(2025, 1, 1),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode=mode,
        payload=payload or {},
    )


def scenario_payload(amount: int = 1000) -> dict:
    return {
        "proposal_id": "P-1",
        "scenario_id": "S-1",
        "total_amount_krw": amount,
        "total_qty_kg": 100,
        "max_price": amount // 100,
        "split_plan": [{"seq": 1, "date": "2025-01-01", "qty_kg": 100}],
        "meta": {"as_of": "2025-01-01"},
    }


#: Planner 가 **실제로 불리는 단계**의 답만 담은 대본.
#:
#: 🔴 예전에는 네 Tool + 종료를 전부 적었다. 이제 Harness 는 고를 것이 하나뿐인
#:    단계와 종료 단계에서 Planner 를 부르지 않는다. 그 단계의 답까지 대본에 남겨
#:    두면 **이미 실행한 Tool 을 다시 요청**하게 되어 중복 반려로 예산만 깎인다.
#:
#: 남는 자리는 둘이다.
#:
#:     ① 시작    {assess_finance_position, project_cashflow}
#:     ② 현금흐름 뒤 {calculate_purchase_finance_cap, analyze_payment_pressure}
#:
#: 사이의 `project_cashflow` 와 마지막 `analyze_payment_pressure` 는 그때 유일한
#: 합법 Tool 이라 Harness 가 결정론으로 집는다. 그래서 실행 순서는 `PRE_ORDER` 그대로다.
def pre_purchase_plan():
    return [
        ToolAction("assess_finance_position"),
        ToolAction("calculate_purchase_finance_cap"),
    ]


@pytest.fixture(autouse=True)
def _no_persistence():
    with patch("app.finance.execution.save_finance_execution"):
        yield


def _new_harness(*, max_tool_calls: int = 8, max_replans: int = 2) -> FinanceHarness:
    return FinanceHarness(
        FinanceToolRegistry(Port()), max_tool_calls=max_tool_calls, max_replans=max_replans
    )


def _trace(metadata) -> dict:
    return next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_harness_trace"
    )


# ---------------------------------------------------------------------------
# capability 소유 — 남의 capability 를 대신 채우지 않는다
# ---------------------------------------------------------------------------


def test_every_capability_has_exactly_one_owning_tool():
    """🔴 예전에는 `cashflow_projection` 을 세 Tool 이 함께 채웠다.

    그중 둘은 안에서 몰래 투영을 돌렸기 때문인데, 그러면 *"현금흐름을 만든 Tool"* 을
    이력에서 짚을 수 없다.
    """
    assert CAPABILITY_OWNER == {
        "finance_position": "assess_finance_position",
        "cashflow_projection": "project_cashflow",
        "finance_cap": "calculate_purchase_finance_cap",
        "payment_pressure": "analyze_payment_pressure",
        "scenario_evaluation": "evaluate_purchase_scenario",
        "amount_adjustment_validation": "validate_amount_adjustment",
        "sales_scenario_evaluation": "evaluate_sales_scenario",
    }
    assert len(set(CAPABILITY_OWNER.values())) == len(CAPABILITY_OWNER)


def test_dependencies_are_capability_conditions_not_a_fixed_tool_order():
    """의존은 **capability 조건**이다. Tool 이름 순서를 박아 두지 않는다."""
    assert TOOL_DEPENDENCIES["calculate_purchase_finance_cap"] == {"cashflow_projection"}
    assert TOOL_DEPENDENCIES["analyze_payment_pressure"] == {"cashflow_projection"}
    assert TOOL_DEPENDENCIES["validate_amount_adjustment"] == {"scenario_evaluation"}
    # 조건이 없는 Tool 은 언제든 고를 수 있다 — 그것이 Planner 의 선택 여지다.
    assert TOOL_DEPENDENCIES["assess_finance_position"] == frozenset()
    assert TOOL_DEPENDENCIES["project_cashflow"] == frozenset()


# ---------------------------------------------------------------------------
# 동적 Tool 노출
# ---------------------------------------------------------------------------


def test_executable_tools_open_up_as_capabilities_are_filled():
    """상태가 바뀌면 **행동공간이 바뀐다.** 고정 파이프라인이 아니다."""
    harness = _new_harness()
    state = FinanceAgentState(request())

    first = harness.capability_state(state)
    assert first.executable_tools == {"assess_finance_position", "project_cashflow"}
    assert first.dependency_status["calculate_purchase_finance_cap"]["unmet"] == [
        "cashflow_projection"
    ]

    state.tool_order.append("project_cashflow")
    second = harness.capability_state(state)
    # ★ 여기서 둘 다 합법이다 — 어느 것을 먼저 고를지는 Planner 몫이다.
    assert second.executable_tools == {
        "assess_finance_position",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    }
    assert "cashflow_projection" in second.completed


def test_langchain_tools_expose_only_the_executable_set():
    """모델에게 **부를 수 없는 Tool 을 보여 주지 않는다.**"""
    harness = _new_harness()
    state = FinanceAgentState(request())

    names = {tool.name for tool in harness.langchain_tools(harness.capability_state(state))}
    assert names == {"assess_finance_position", "project_cashflow"}
    assert "calculate_purchase_finance_cap" not in names

    state.tool_order.extend(PRE_ORDER)
    complete = harness.capability_state(state)
    assert complete.missing == ()
    # capability 가 다 찼을 때만 종료 Tool 이 보인다.
    assert [tool.name for tool in harness.langchain_tools(complete)] == [FINALIZE_TOOL_NAME]


def test_finalize_tool_is_not_exposed_while_capabilities_are_missing():
    harness = _new_harness()
    state = FinanceAgentState(request())
    names = {tool.name for tool in harness.langchain_tools(harness.capability_state(state))}
    assert FINALIZE_TOOL_NAME not in names


def test_amount_adjustment_is_not_exposed_before_scenario_evaluation():
    harness = _new_harness()
    state = FinanceAgentState(request("SCENARIO_VALIDATION", scenario_payload()), branch_id="S-1")

    initial = harness.capability_state(state)
    assert initial.executable_tools == {"evaluate_purchase_scenario"}
    assert "validate_amount_adjustment" not in initial.executable_tools


# ---------------------------------------------------------------------------
# Harness 검증 — 노출은 편의이고, 막는 것은 여기다
# ---------------------------------------------------------------------------


def test_out_of_order_tool_is_blocked_before_the_registry_runs():
    """노출을 뚫고 들어와도 **Registry 에 닿지 않는다.**"""
    harness = _new_harness()
    state = FinanceAgentState(request())
    capability_state = harness.capability_state(state)

    with (
        patch.object(FinanceToolRegistry, "execute") as execute,
        pytest.raises(FinanceToolDenied) as raised,
    ):
        harness.authorize("calculate_purchase_finance_cap", {}, state, capability_state)
    assert raised.value.reason == DEPENDENCY_NOT_SATISFIED
    execute.assert_not_called()


def test_mode_permission_is_enforced():
    harness = _new_harness()
    state = FinanceAgentState(request())
    capability_state = harness.capability_state(state)

    with pytest.raises(FinanceToolDenied) as raised:
        harness.authorize("evaluate_purchase_scenario", {}, state, capability_state)
    assert raised.value.reason == TOOL_PERMISSION_DENIED


def test_duplicate_unresolved_call_is_blocked():
    harness = _new_harness()
    state = FinanceAgentState(request())
    capability_state = harness.capability_state(state)

    harness.authorize("assess_finance_position", {}, state, capability_state)
    with pytest.raises(FinanceToolDenied) as raised:
        harness.authorize("assess_finance_position", {}, state, capability_state)
    assert raised.value.reason == DUPLICATE_UNRESOLVED_TOOL_CALL


def test_tool_budget_is_enforced():
    harness = _new_harness(max_tool_calls=0)
    state = FinanceAgentState(request())
    capability_state = harness.capability_state(state)

    with pytest.raises(FinanceToolDenied) as raised:
        harness.authorize("assess_finance_position", {}, state, capability_state)
    assert raised.value.reason == TOOL_BUDGET_EXHAUSTED


def test_zero_budget_is_not_silently_replaced_by_the_default():
    """🔴 `x or default` 였다면 0 이 8 로 바뀐다. 0 은 **한 번도 부르지 말라**는 뜻이다."""
    controller = FinanceAgentController(Port(), ScriptedPlanner([]), Finalizer(), max_tool_calls=0)
    assert controller.max_tool_calls == 0
    reply, _metadata = controller.run(request())
    assert reply.runtime_status == "ERROR"


def test_tool_execution_outside_harness_control_is_refused():
    """LangChain 어댑터를 직접 불러도 승인 없이는 Registry 로 가지 않는다."""
    harness = _new_harness()
    adapter = harness._adapters["assess_finance_position"]
    with (
        patch.object(FinanceToolRegistry, "execute") as execute,
        pytest.raises(FinanceToolDenied),
    ):
        adapter.invoke({})
    execute.assert_not_called()


def test_invalid_tool_selection_replans_within_the_budget():
    planner = ScriptedPlanner(
        [ToolAction("calculate_purchase_finance_cap"), *pre_purchase_plan()]
    )
    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.replans == 1
    trace = _trace(metadata)
    assert trace["denials"] == [
        {
            "branch_id": "PRE_PURCHASE",
            "denied_tool": "calculate_purchase_finance_cap",
            "denied_reason": DEPENDENCY_NOT_SATISFIED,
        }
    ]


# ---------------------------------------------------------------------------
# 숨은 선행 호출 제거
# ---------------------------------------------------------------------------


def test_finance_cap_never_runs_project_cashflow_behind_the_harness():
    """🔴 예전에는 투영이 없으면 cap Tool 이 안에서 `project_cashflow` 를 돌렸다."""
    state = FinanceAgentState(request())
    assert state.projection is None
    with pytest.raises(FinancePreconditionMissing):
        calculate_purchase_finance_cap(Port(), {}, state)
    # 몰래 만들어 둔 투영도 없다 — 실패는 실패로 남는다.
    assert state.projection is None
    assert state.tool_order == []


def test_payment_pressure_never_runs_project_cashflow_behind_the_harness():
    state = FinanceAgentState(request())
    with pytest.raises(FinancePreconditionMissing):
        analyze_payment_pressure(Port(), {}, state)
    assert state.projection is None


def test_precondition_failure_is_not_reported_as_runtime_not_ready():
    """★ 실행 순서 오류를 **없는 재무 사실로 위장하지 않는다.**

    `RUNTIME_NOT_READY` 는 다시 불러도 같을 종류의 부재를 뜻한다. 여기서는 데이터가
    멀쩡하다 — Harness 가 막았어야 할 호출이 새어 들어온 것이다.
    """
    state = FinanceAgentState(request())
    with pytest.raises(FinancePreconditionMissing) as raised:
        FinanceToolRegistry(Port()).execute("analyze_payment_pressure", {}, state)
    # `FinanceDataNotReady` 가 아니다 — 없는 것은 재무 사실이 아니라 선행 실행이다.
    assert not isinstance(raised.value, FinanceDataNotReady)
    assert "cashflow_projection" in str(raised.value)


def test_cross_capability_execution_order_is_visible_in_the_trace():
    """Trace 의 Tool 순서 = **실제로 돈 순서.**"""
    planner = ScriptedPlanner(pre_purchase_plan())
    _reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    trace = _trace(metadata)
    assert trace["executed_tools"] == list(PRE_ORDER)
    assert metadata.used_tools == PRE_ORDER
    executed = [step["executed_tool"] for step in trace["steps"] if step["executed_tool"]]
    assert executed == list(PRE_ORDER)


# ---------------------------------------------------------------------------
# 필수 capability 는 성급한 종료를 막는다
# ---------------------------------------------------------------------------


def test_premature_finalize_is_rejected_and_replanned():
    planner = ScriptedPlanner([ToolAction(finalize=True), *pre_purchase_plan()])
    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.replans == 1
    assert metadata.used_tools == PRE_ORDER


def test_pre_purchase_requires_all_four_capabilities_before_finalizing():
    #  Planner 가 불리는 자리는 둘뿐이다 — 시작과 현금흐름 뒤. 이른 종료 요청은
    #  **남은 capability 가 있는 채로 물어보는 자리**에 놓아야 시험이 된다.
    planner = ScriptedPlanner(
        [
            ToolAction("assess_finance_position"),
            ToolAction(finalize=True),  # finance_cap · payment_pressure 가 남았다
            ToolAction("calculate_purchase_finance_cap"),
        ]
    )
    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.replans == 1
    trace = _trace(metadata)
    rejected = [
        step
        for step in trace["steps"]
        if step["denied_reason"] == "FINALIZE_BEFORE_REQUIRED_CAPABILITIES"
    ]
    assert len(rejected) == 1
    #  묻는 자리가 앞으로 당겨졌으니 그때 남아 있던 capability 도 둘이다.
    assert rejected[0]["missing_capabilities"] == ["finance_cap", "payment_pressure"]


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION
# ---------------------------------------------------------------------------


def test_scenario_evaluation_runs_first_and_adjustment_only_after_it():
    planner = ScriptedPlanner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction("validate_amount_adjustment", {"axis": "amount"}),
            ToolAction(finalize=True),
        ]
    )
    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(
        request("SCENARIO_VALIDATION", scenario_payload())
    )

    assert reply.runtime_status == "READY"
    assert metadata.used_tools[0] == "evaluate_purchase_scenario"
    #  🔴 **Planner 에게 묻지 않는 흐름이다.** SCENARIO_VALIDATION 은 단계마다 합법
    #     Tool 이 하나뿐이라 Harness 가 결정론으로 집는다. 그래서 "무엇이 노출됐나" 는
    #     Planner 대역이 아니라 **Trace 에 적힌 실행 가능 집합**에서 읽는다.
    assert planner.attempts == 0
    steps = _trace(metadata)["steps"]
    assert steps[0]["executable_tools"] == ["evaluate_purchase_scenario"]
    assert "validate_amount_adjustment" not in steps[0]["executable_tools"]


def test_adjustment_validation_receives_only_source_owned_values():
    """모델이 실은 숫자는 **여기서 끝난다.** 실행에 들어가는 값은 원천에서 다시 고른다."""
    invented = 999_999_999
    planner = ScriptedPlanner(
        [
            ToolAction("evaluate_purchase_scenario"),
                ToolAction(
                    "validate_amount_adjustment",
                    {"axis": "amount", "candidate_amount_krw": invented},
            ),
            ToolAction(finalize=True),
        ]
    )
    reply, _metadata = FinanceAgentController(Port(), planner, Finalizer()).run(
        request("SCENARIO_VALIDATION", scenario_payload())
    )

    assert reply.runtime_status == "READY"
    for evidence in reply.evidences:
        assert evidence.value != float(invented)


@pytest.mark.parametrize(
    "malformed",
    [
        {"axis": "quantity"},
        {"axis": "amount", "invented_field": 123},
    ],
)
def test_malformed_adjustment_arguments_replan_without_execution(malformed):
    #  🔴 **계약을 그 경계에서 직접 시험한다.** 예전에는 Planner 대역이 망가진 인자를
    #     들고 오게 해서 루프째로 돌렸다. 이제 이 흐름에서는 Planner 를 부르지 않으므로
    #     그 길로는 망가진 인자가 들어올 수 없다 — 검사가 **아무것도 안 하는 검사**가 된다.
    #     지켜야 하는 것은 그대로다: 계약을 어긴 인자는 **실행에 닿기 전에** 걸린다.
    with pytest.raises(FinancePlannerContractViolation):
        validate_planner_tool_arguments(
            ToolAction("validate_amount_adjustment", malformed)
        )

    #  잘 만든 인자는 그대로 통과하고, 실행은 원천에서 값을 다시 고른다.
    assert validate_planner_tool_arguments(
        ToolAction("validate_amount_adjustment", {"axis": "amount"})
    ) == {"axis": "amount"}

    planner = ScriptedPlanner([])
    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(
        request("SCENARIO_VALIDATION", scenario_payload())
    )

    trace = _trace(metadata)
    assert reply.runtime_status == "READY"
    assert reply.business_status == "reject"
    assert planner.attempts == 0
    assert metadata.replans == 0
    assert metadata.used_tools == ("evaluate_purchase_scenario", "validate_amount_adjustment")
    assert trace["failure_kind"] != "INVALID_REQUEST"
    #  묻지 않았으니 반려도 없다. 계약 위반은 위에서 경계째로 확인했다.
    assert [step for step in trace["steps"] if step["denied_reason"]] == []


# ---------------------------------------------------------------------------
# LangChain tool calling
# ---------------------------------------------------------------------------


def _tool_call_transport(script):
    calls = iter(script)

    def transport(*, model, system_prompt, user_payload, tool_declarations):
        del model, system_prompt
        name, args = next(calls)
        transport.seen.append(
            {
                "declared": [item["name"] for item in tool_declarations],
                "missing": list(user_payload.get("missing_capabilities", [])),
                "observations": len(user_payload.get("observations", [])),
            }
        )
        return [{"name": name, "args": args}]

    transport.seen = []
    return transport


def test_langchain_planner_drives_the_finance_tools_end_to_end(monkeypatch):
    """LangChain tool calling 으로 고르고, **Harness 승인 뒤에** 실행된다."""
    transport = _tool_call_transport(
        [(name, {}) for name in PRE_ORDER] + [(FINALIZE_TOOL_NAME, {})]
    )
    monkeypatch.setattr("app.finance.llm.planner._gemini_tool_call", transport)
    planner = LangChainFinancePlanner(finance_chat_model("gemini", model="gemini-test"))

    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.used_tools == PRE_ORDER
    #  Finalizer 는 안 불렸다(설명 후보 1개). 이번 실행에서 답한 모델은 Planner 다.
    assert metadata.llm_model == "gemini-test"
    assert planner.model == "gemini-test"
    #  5 → 3. 줄어든 둘은 **고를 것이 하나뿐이던 단계와 종료 단계**다.
    #  실행 순서(`PRE_ORDER`)는 그대로다 — Harness 가 같은 Tool 을 집기 때문이다.
    assert planner.attempts == 3


def test_langchain_planner_only_sees_currently_executable_tools(monkeypatch):
    transport = _tool_call_transport(
        [(name, {}) for name in PRE_ORDER] + [(FINALIZE_TOOL_NAME, {})]
    )
    monkeypatch.setattr("app.finance.llm.planner._gemini_tool_call", transport)
    planner = LangChainFinancePlanner(finance_chat_model("gemini", model="gemini-test"))

    FinanceAgentController(Port(), planner, Finalizer()).run(request())

    declared = [step["declared"] for step in transport.seen]
    assert declared[0] == ["assess_finance_position", "project_cashflow"]
    assert "calculate_purchase_finance_cap" not in declared[0]
    #  🔴 **종료 Tool 은 이제 모델에게 가지 않는다.** 남은 capability 가 없으면 고를 것이
    #     종료뿐이라 Harness 가 결정론으로 끝낸다 — 물어볼 것이 없는 자리다.
    assert all(FINALIZE_TOOL_NAME not in step for step in declared)
    #  묻는 자리에는 늘 둘 이상이 놓인다. 하나뿐이면 애초에 묻지 않는다.
    assert all(len(step) >= 2 for step in declared)


def test_tool_observations_reach_the_next_langchain_planner_step(monkeypatch):
    transport = _tool_call_transport(
        [(name, {}) for name in PRE_ORDER] + [(FINALIZE_TOOL_NAME, {})]
    )
    monkeypatch.setattr("app.finance.llm.planner._gemini_tool_call", transport)
    planner = LangChainFinancePlanner(finance_chat_model("gemini", model="gemini-test"))

    FinanceAgentController(Port(), planner, Finalizer()).run(request())

    # 첫 호출은 관측이 없고, 이후에는 그때까지의 Tool 결과가 그대로 들어간다.
    #
    # ★ 묻지 않고 지나간 단계의 결과도 빠짐없이 실린다. 2 는 결정론으로 돈
    #   `project_cashflow` 까지 누적됐다는 뜻이다 — **묻지 않았다고 관측이 비지 않는다.**
    assert [step["observations"] for step in transport.seen] == [0, 2, 3]


def test_langchain_planner_over_ollama_uses_the_same_tool_set(monkeypatch):
    """가용성 대체 경로도 **같은 Tool 목록**을 본다 — Provider 로 권한이 갈리지 않는다."""
    transport = _tool_call_transport(
        [(name, {}) for name in PRE_ORDER] + [(FINALIZE_TOOL_NAME, {})]
    )
    monkeypatch.setattr("app.finance.llm.planner._ollama_tool_call", transport)
    planner = LangChainFinancePlanner(finance_chat_model("ollama", model="gemma3:4b"))

    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.used_tools == PRE_ORDER
    assert next(step["declared"] for step in transport.seen) == [
        "assess_finance_position",
        "project_cashflow",
    ]


def test_langchain_free_text_answer_is_a_recoverable_contract_violation(monkeypatch):
    """Tool 을 하나도 안 부른 답은 **되물을 수 있는 잘못**이다."""

    def transport(*, model, system_prompt, user_payload, tool_declarations):
        del model, system_prompt, user_payload, tool_declarations
        return []

    monkeypatch.setattr("app.finance.llm.planner._gemini_tool_call", transport)
    planner = LangChainFinancePlanner(finance_chat_model("gemini", model="gemini-test"))

    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "ERROR"
    assert metadata.replans == 2  # 상한까지만 되묻는다
    assert metadata.llm_status == "FALLBACK"


def test_provider_outage_is_not_hidden_by_a_replan(monkeypatch):
    def transport(*, model, system_prompt, user_payload, tool_declarations):
        del model, system_prompt, user_payload, tool_declarations
        raise TimeoutError("provider is down")

    monkeypatch.setattr("app.finance.llm.planner._gemini_tool_call", transport)
    planner = LangChainFinancePlanner(finance_chat_model("gemini", model="gemini-test"))

    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "ERROR"
    assert metadata.replans == 0


def test_langchain_tool_call_outside_the_exposed_set_never_executes(monkeypatch):
    """전송 계층 강제(`allowedFunctionNames`)를 무시한 모델은 **실행에 닿지 못한다.**

    노출 밖 Tool 이름은 Planner 사후 검증에서 먼저 걸리고, 그 잘못은 회복 가능하므로
    상한 안에서 되묻는다 — Registry 는 한 번도 그 이름을 보지 못한다.
    """
    transport = _tool_call_transport(
        [
            ("calculate_purchase_finance_cap", {}),  # 아직 노출되지 않은 Tool
            ("assess_finance_position", {}),
            ("calculate_purchase_finance_cap", {}),
        ]
    )
    monkeypatch.setattr("app.finance.llm.planner._gemini_tool_call", transport)
    planner = LangChainFinancePlanner(finance_chat_model("gemini", model="gemini-test"))

    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.replans == 1
    #  사이의 `project_cashflow` 와 마지막 `analyze_payment_pressure` 는 Harness 가
    #  결정론으로 집는다. 그래서 실행 순서는 여전히 `PRE_ORDER` 다.
    assert metadata.used_tools == PRE_ORDER
    rejected = [
        step
        for step in _trace(metadata)["steps"]
        if step["denied_reason"] == "PLANNER_CONTRACT_VIOLATION"
    ]
    assert len(rejected) == 1
    assert rejected[0]["executed_tool"] is None


# ---------------------------------------------------------------------------
# Trace — 요청 · 허가 · 실행을 구분할 수 있는가
# ---------------------------------------------------------------------------


def test_trace_separates_request_permission_and_execution():
    planner = ScriptedPlanner(
        [ToolAction("analyze_payment_pressure"), *pre_purchase_plan()]
    )
    _reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    trace = _trace(metadata)
    denied = trace["steps"][0]
    assert denied["requested_tool"] == "analyze_payment_pressure"
    assert denied["executed_tool"] is None
    assert denied["denied_reason"] == DEPENDENCY_NOT_SATISFIED
    assert denied["executable_tools"] == ["assess_finance_position", "project_cashflow"]
    assert denied["dependency_status"]["analyze_payment_pressure"]["unmet"] == [
        "cashflow_projection"
    ]

    executed = [step for step in trace["steps"] if step["executed_tool"]]
    assert [step["executed_tool"] for step in executed] == list(PRE_ORDER)
    assert executed[0]["requested_tool"] == executed[0]["executed_tool"]


def test_trace_reports_budget_consumption_and_capability_completion():
    planner = ScriptedPlanner(pre_purchase_plan())
    _reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    trace = _trace(metadata)
    assert trace["tool_calls"] == 4
    #  Tool 은 넷 그대로인데 provider 왕복은 둘이다. 빠진 셋은 **물어볼 것이 없던
    #  자리** — 합법 Tool 이 하나뿐인 두 단계와 종료 단계다.
    assert trace["llm_calls"] == 2
    assert trace["replans"] == 0
    assert trace["max_tool_calls"] == 8
    assert trace["max_replans"] == 2
    assert trace["runtime_status"] == "READY"
    assert trace["llm_status"] == "SUCCESS"
    assert trace["rules_applied"] == []
    assert trace["steps"][-1]["missing_capabilities"] == []
    assert sorted(trace["steps"][-1]["completed_capabilities"]) == [
        "cashflow_projection",
        "finance_cap",
        "finance_position",
        "payment_pressure",
    ]


def test_harness_module_owns_no_finance_arithmetic():
    """★ Harness 는 숫자를 만들지 않는다. 여기서 계산이 시작되면 소유가 무너진다.

    ★ Registry 디스패치와 실행 계약 guard 가 같은 파일로 들어왔어도 그대로다 —
      Harness 는 capability 를 **부를 뿐** 재무 값을 만들지 않는다.
    """
    with open(harness_module.__file__, encoding="utf-8") as handle:
        text = handle.read()
    # ★ `Decimal(` 자체는 금지어가 아니다 — 요청 계약 검증(`_validate_finance_payload`)이
    #   제출된 금액이 양수·유한인지 **읽기 위해** 쓴다. 금지되는 것은 재무 값을
    #   **만드는** 것이고, 그건 아래 이름들로 나타난다.
    for forbidden in (
        "calculate_finance_cap",
        "classify_base_stress",
        "derive_cash_priority",
        "project_cashflow(",
        "minimum_cash_balance_krw",
        "ROUND_FLOOR",
    ):
        assert forbidden not in text, forbidden


def test_out_of_order_adjustment_is_a_harness_denial_not_runtime_not_ready():
    """🔴 순서 오류를 **없는 재무 사실로 위장하지 않는다.**

    `validate_amount_adjustment` 는 인자 원천을 시나리오 판정 결과에서 찾는다. 승인
    보다 인자 확인이 먼저 돌면, 아직 부를 수도 없는 Tool 의 원천이 없다는 이유로
    `RUNTIME_NOT_READY` 가 나간다 — 마스터는 그것을 *"데이터가 없다"* 로 읽는다.
    실제로는 Harness 가 막고 되물었어야 할 호출이다.
    """
    #  🔴 **Harness 에 직접 묻는다.** 이 흐름에서는 Planner 를 부르지 않으므로 순서를
    #     어긴 요청이 루프로 들어올 길이 없다. 그래도 지켜야 하는 계약은 그대로다 —
    #     아직 부를 수 없는 Tool 은 **선행 조건 반려**이지 «재무 사실 없음» 이 아니다.
    harness = _new_harness()
    state = FinanceAgentState(
        request("SCENARIO_VALIDATION", scenario_payload()), branch_id="S-1"
    )
    with pytest.raises(FinanceToolDenied) as raised:
        harness.authorize(
            "validate_amount_adjustment",
            {"axis": "amount"},
            state,
            harness.capability_state(state),
        )
    assert raised.value.reason == DEPENDENCY_NOT_SATISFIED

    planner = ScriptedPlanner([])
    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(
        request("SCENARIO_VALIDATION", scenario_payload())
    )

    #  그리고 실제 실행은 순서를 어길 기회 없이 끝난다 — 단계마다 합법 Tool 이
    #  하나뿐이라 Harness 가 그것을 집기 때문이다. 반려도 되묻기도 없다.
    assert reply.runtime_status == "READY"
    assert reply.missing_data == ()
    assert metadata.replans == 0
    assert metadata.used_tools == ("evaluate_purchase_scenario", "validate_amount_adjustment")
    assert _trace(metadata)["denials"] == []


def test_tool_budget_stops_the_run_instead_of_answering_short():
    """상한에 걸리면 **못 낸 답을 낸 척하지 않는다.**"""
    planner = ScriptedPlanner(pre_purchase_plan())
    reply, metadata = FinanceAgentController(
        Port(), planner, Finalizer(), max_tool_calls=2
    ).run(request())

    assert reply.runtime_status == "ERROR"
    assert reply.payload == {}
    trace = _trace(metadata)
    assert trace["tool_calls"] == 2
    assert trace["denials"][-1]["denied_reason"] == TOOL_BUDGET_EXHAUSTED


@pytest.mark.parametrize(("limit", "executed"), [(0, 0), (1, 1)])
def test_controller_tool_budget_zero_and_one_are_exact(limit, executed):
    """0/1은 기본값이나 '무제한'으로 바뀌지 않는 실행 전체 상한이다."""
    reply, metadata = FinanceAgentController(
        Port(), ScriptedPlanner(pre_purchase_plan()), Finalizer(), max_tool_calls=limit
    ).run(request())

    trace = _trace(metadata)
    assert reply.runtime_status == "ERROR"
    assert trace["max_tool_calls"] == limit
    assert trace["tool_calls"] == executed
    assert len(metadata.used_tools) == executed
    assert trace["denials"][-1]["denied_reason"] == TOOL_BUDGET_EXHAUSTED


@pytest.mark.parametrize(("limit", "expected_status", "expected_replans"), [
    (0, "ERROR", 0),
    (1, "READY", 1),
])
def test_controller_replan_budget_zero_and_one_are_exact(
    limit, expected_status, expected_replans
):
    """한 번의 recoverable denial만 허용했을 때 재계획도 정확히 한 번이다."""
    planner = ScriptedPlanner(
        [ToolAction("calculate_purchase_finance_cap"), *pre_purchase_plan()]
    )
    reply, metadata = FinanceAgentController(
        Port(), planner, Finalizer(), max_replans=limit
    ).run(request())

    trace = _trace(metadata)
    assert reply.runtime_status == expected_status
    assert trace["max_replans"] == limit
    assert trace["replans"] == expected_replans
    assert metadata.replans == expected_replans


def test_duplicate_terminal_call_stops_without_second_execution_and_is_traced():
    """같은 branch/tool/raw arguments 재요청은 재계획하지 않고 terminal denial 이다."""
    planner = ScriptedPlanner(
        [ToolAction("assess_finance_position"), ToolAction("assess_finance_position")]
    )
    reply, metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    trace = _trace(metadata)
    assert reply.runtime_status == "ERROR"
    #  둘째 요청은 «묻는 자리» 인 세 번째 단계에서 온다. 그 사이의 `project_cashflow`
    #  는 그때 유일한 합법 Tool 이라 Harness 가 결정론으로 돌렸다 — **중복 요청 자체는
    #  한 번도 실행되지 않는다.**
    assert metadata.used_tools == ("assess_finance_position", "project_cashflow")
    assert trace["tool_calls"] == 2
    assert trace["replans"] == 0
    assert trace["denials"][-1]["denied_reason"] == DUPLICATE_UNRESOLVED_TOOL_CALL
    assert trace["steps"][-1]["executed_tool"] is None


def _deterministic_run(mode="PRE_PURCHASE"):
    payload = scenario_payload() if mode == "SCENARIO_VALIDATION" else None
    plan = (
        pre_purchase_plan()
        if mode == "PRE_PURCHASE"
        else [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction("validate_amount_adjustment", {"axis": "amount"}),
            ToolAction(finalize=True),
        ]
    )
    return FinanceAgentController(Port(), ScriptedPlanner(plan), Finalizer()).run(
        request(mode, payload)
    )


def test_a_b_a_runs_keep_harness_state_and_run_ids_isolated():
    first, first_meta = _deterministic_run()
    middle, middle_meta = _deterministic_run("SCENARIO_VALIDATION")
    last, last_meta = _deterministic_run()

    assert [first.runtime_status, middle.runtime_status, last.runtime_status] == [
        "READY", "READY", "READY"
    ]
    assert len({first.run_id, middle.run_id, last.run_id}) == 3
    assert _trace(first_meta)["tool_calls"] == _trace(last_meta)["tool_calls"] == 4
    assert _trace(middle_meta)["tool_calls"] == 2
    assert first_meta.used_tools == last_meta.used_tools == PRE_ORDER


def test_concurrent_deterministic_runs_have_no_cross_request_state_leakage():
    modes = ["PRE_PURCHASE", "SCENARIO_VALIDATION"] * 4
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_deterministic_run, modes))

    run_ids = {reply.run_id for reply, _metadata in results}
    assert len(run_ids) == len(results)
    for (reply, metadata), mode in zip(results, modes, strict=True):
        assert reply.runtime_status == "READY"
        assert _trace(metadata)["replans"] == 0
        assert metadata.used_tools == (
            PRE_ORDER
            if mode == "PRE_PURCHASE"
            else ("evaluate_purchase_scenario", "validate_amount_adjustment")
        )


def test_fifty_repeated_pre_purchase_and_scenario_runs_are_stable():
    """실 Provider 없이 50회씩 반복해 결정론·예산·종료를 함께 고정한다."""
    for mode, expected_tools in (
        ("PRE_PURCHASE", PRE_ORDER),
        ("SCENARIO_VALIDATION", ("evaluate_purchase_scenario", "validate_amount_adjustment")),
    ):
        results = [_deterministic_run(mode) for _ in range(50)]
        replies = [reply for reply, _metadata in results]
        traces = [_trace(metadata) for _reply, metadata in results]
        assert len({reply.run_id for reply in replies}) == 50
        assert {reply.runtime_status for reply in replies} == {"READY"}
        assert {reply.business_status for reply in replies} == {replies[0].business_status}
        assert {json.dumps(reply.payload, sort_keys=True, default=str) for reply in replies} == {
            json.dumps(replies[0].payload, sort_keys=True, default=str)
        }
        evidence_sets = {
            json.dumps(
                [vars(item) for item in reply.evidences], sort_keys=True, default=str
            )
            for reply in replies
        }
        assert evidence_sets == {
            json.dumps(
                [vars(item) for item in replies[0].evidences],
                sort_keys=True,
                default=str,
            )
        }
        assert all(metadata.used_tools == expected_tools for _reply, metadata in results)
        assert all(trace["replans"] == 0 and not trace["denials"] for trace in traces)


def test_finalizer_explains_a_result_that_already_exists():
    """★ 설명은 **결과가 확정된 뒤에만** 만들어진다. 순서가 곧 계약이다."""
    seen: list[str] = []

    class _Recording(Finalizer):
        def finalize(self, *, mode, business_status, evidences, has_verified_adjustment=False):
            # Finalizer 가 불릴 때 이미 결정론 Evidence 가 다 있다.
            seen.extend(item.claim for item in evidences)
            return super().finalize(
                mode=mode, business_status=business_status, evidences=evidences
            )

    planner = ScriptedPlanner(pre_purchase_plan())
    #  Finalizer 가 **불릴 때** 무엇을 이미 들고 있는가를 보는 검사다.
    with two_explanation_candidates():
        reply, _metadata = FinanceAgentController(Port(), planner, _Recording()).run(
            request()
        )

    assert reply.runtime_status == "READY"
    assert {"finance_cap_amount_krw", "base_projected_cash_min", "available_cash"} <= set(seen)


def test_finalizer_cannot_change_the_deterministic_verdict():
    """설명이 판정을 뒤집지 못한다 — 판정은 Rule 이 이미 정했다."""

    class _LyingFinalizer(Finalizer):
        def finalize(self, *, mode, business_status, evidences, has_verified_adjustment=False):
            del mode, business_status, evidences
            self.attempts += 1
            return "검증된 재무 근거가 보고된 시나리오 판정을 뒷받침합니다."

    planner = ScriptedPlanner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction("validate_amount_adjustment", {"axis": "amount"}),
            ToolAction(finalize=True),
        ]
    )
    reply, _metadata = FinanceAgentController(Port(), planner, _LyingFinalizer()).run(
        request("SCENARIO_VALIDATION", scenario_payload())
    )

    assert reply.runtime_status == "READY"
    # 설명은 "수용" 문장을 골랐지만 업무 판정은 결정론 Rule 이 낸 그대로다.
    assert reply.business_status == "reject"
    assert reply.payload["verdict"] == "reject"


def test_tool_arguments_the_finance_contract_does_not_declare_are_refused():
    """★ 모르는 인자를 **조용히 버리지 않는다.**

    값을 받지 않는 Tool 에 값이 왔다는 사실 자체가 신호다. 버리면 모델이 무엇을
    보냈는지가 기록에서 사라지고, 다음에 같은 일이 나도 알 수 없다.

    ★ 재무 결과가 바뀌는 자리가 아니다 — 정상 실행의 인자는 언제나 빈 dict 이거나
      `source_owned_arguments` 가 원천에서 다시 고른 값이다.
    """
    planner = ScriptedPlanner(
        [ToolAction("assess_finance_position", {"finance_cap_amount_krw": 999})]
    )
    reply, _metadata = FinanceAgentController(Port(), planner, Finalizer()).run(request())

    assert reply.runtime_status == "ERROR"
    assert reply.payload == {}


def test_adjustment_axis_is_declared_as_a_single_allowed_value():
    """금액 축만 조정한다는 불변식이 **모델이 보는 스키마에도** 적혀 있다."""
    tool = build_planner_tool_adapter("validate_amount_adjustment", lambda *_a, **_k: {})

    assert tool.args["axis"]["const"] == "amount"
    assert set(tool.args) == {"axis"}
