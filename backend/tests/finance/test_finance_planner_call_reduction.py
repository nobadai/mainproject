"""Planner 를 **언제 부르는가** — provider 왕복이 실제로 필요한 자리인가.

Harness 는 이번 단계에 부를 수 있는 Tool 집합을 이미 결정론으로 계산해 둔다. 그
집합이 하나뿐이면 고를 여지가 없고, 비어 있으면(= 남은 capability 없음) 남은 행동은
종료뿐이다. **답이 정해진 자리에 모델을 부르면 선택은 그대로인데 왕복만 는다.**

이 파일이 잠그는 것은 셋이다.

    ① 어느 단계에서 Planner 를 부르고 어느 단계에서 부르지 않는가
    ② 부르지 않은 단계에서 provider 로 아무것도 나가지 않는가
    ③ 그렇게 줄여도 업무 결과가 한 칸도 달라지지 않는가

★ ③ 이 없으면 ①②는 «빨라졌다» 가 아니라 «다른 답을 낸다» 일 수 있다.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from app.finance.application.harness import (
    FinanceHarness,
    FinanceToolRegistry,
)
from app.finance.application.orchestration import (
    SELECTION_FINALIZE,
    SELECTION_LLM,
    SELECTION_SINGLE,
    FinanceAgentController,
    _settled_action,
)
from app.finance.llm.planner import DeterministicFinancePlanner, ToolAction
from app.finance.state import FinanceAgentState
from tests.finance.test_finance_harness_langchain import (
    Port,
    request,
    scenario_payload,
)


def _sales_payload() -> dict[str, Any]:
    return {
        "scenario_id": "SC-001",
        "partner_id": "P-100",
        "item": "red_pepper",
        "quantity_kg": "100",
        "unit_price_krw": "10000",
        "reported_sales_amount_krw": "1000000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": "2026-01-05",
        "source_ref": "SALES-REPLY:R-9",
    }


def _request_for(mode: str):
    if mode == "SCENARIO_VALIDATION":
        return request(mode, {"scenarios": [scenario_payload()]})
    if mode == "SALES_VALIDATION":
        return request(mode, _sales_payload())
    return request(mode)


class _CountingPlanner:
    """결정론 선택을 그대로 쓰되 **불린 횟수와 그때의 선택지 수**를 적는다.

    ★ `DeterministicFinancePlanner` 를 그대로 주입하면 Controller 가 그것을
      «LLM 꺼짐» 으로 읽는다(`llm_enabled`). 감싸야 켜진 경로가 시험된다.
    """

    model = "counting-planner"

    def __init__(self) -> None:
        self._inner = DeterministicFinancePlanner()
        self.attempts = 0
        self.options_per_call: list[int] = []

    def decide(self, **kwargs: Any) -> ToolAction:
        self.attempts += 1
        self.options_per_call.append(len(kwargs.get("allowed_tools") or ()))
        self._inner.attempts = 0
        return self._inner.decide(**kwargs)


class _ExplodingPlanner:
    """불리면 터진다. **부르지 않는다는 말을 증명하는 대역이다.**"""

    model = "exploding-planner"

    def __init__(self) -> None:
        self.attempts = 0

    def decide(self, **_kwargs: Any) -> ToolAction:
        raise AssertionError("Planner 를 불렀다 — 고를 것이 없는 자리였다")


class _Finalizer:
    model = "scripted-finalizer"

    def __init__(self) -> None:
        self.attempts = 0

    def finalize(self, *, mode, business_status, evidences, has_verified_adjustment=False):
        del mode, evidences, has_verified_adjustment
        self.attempts += 1
        return f"설명 {business_status}"


def _run(mode: str, planner):
    with patch("app.finance.execution.save_finance_execution"):
        return FinanceAgentController(Port(), planner, _Finalizer()).run(
            _request_for(mode)
        )


def _trace(metadata) -> dict[str, Any]:
    return next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_harness_trace"
    )


def _selection_sources(metadata) -> list[str | None]:
    return [step.get("selection_source") for step in _trace(metadata)["steps"]]


# ── ① 고를 것이 하나뿐인 단계 ────────────────────────────────────────────


def test_a_single_legal_tool_is_chosen_without_asking_the_model():
    """합법 Tool 이 하나면 그것이 답이다 — 물어볼 것이 없다."""
    state = FinanceAgentState(_request_for("SCENARIO_VALIDATION"), branch_id="S-1")
    harness = FinanceHarness(
        FinanceToolRegistry(Port()), max_tool_calls=8, max_replans=2
    )
    capability_state = harness.capability_state(state)

    assert len(capability_state.executable_tools) == 1
    action, source = _settled_action(capability_state)

    assert source == SELECTION_SINGLE
    assert action.tool_name == "evaluate_purchase_scenario"
    assert action.finalize is False
    #  🔴 **숫자를 만들지 않는다.** 인자는 그대로 원천에서 다시 고른다.
    assert action.arguments == {}


def test_a_completed_capability_set_finalizes_without_asking_the_model():
    """남은 capability 가 없으면 남은 행동은 종료뿐이다."""
    state = FinanceAgentState(_request_for("PRE_PURCHASE"))
    state.tool_order = [
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    ]
    harness = FinanceHarness(
        FinanceToolRegistry(Port()), max_tool_calls=8, max_replans=2
    )
    capability_state = harness.capability_state(state)

    assert capability_state.missing == ()
    action, source = _settled_action(capability_state)

    assert source == SELECTION_FINALIZE
    assert action.finalize is True
    assert action.tool_name is None


def test_two_or_more_legal_tools_are_left_to_the_model():
    """실제로 고를 것이 있으면 결정론이 가로채지 않는다."""
    state = FinanceAgentState(_request_for("PRE_PURCHASE"))
    harness = FinanceHarness(
        FinanceToolRegistry(Port()), max_tool_calls=8, max_replans=2
    )
    capability_state = harness.capability_state(state)

    assert len(capability_state.executable_tools) >= 2
    assert _settled_action(capability_state) is None


# ── ② mode 별 Planner 호출 수 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected_planner_calls"),
    [
        ("PRE_PURCHASE", 3),
        ("SCENARIO_VALIDATION", 0),
        ("SALES_VALIDATION", 0),
    ],
)
def test_the_planner_is_called_only_where_there_is_a_real_choice(
    mode, expected_planner_calls
):
    """🔴 **자리 수를 고정한다.** 여기가 다시 늘면 provider 왕복이 조용히 돌아온 것이다.

    PRE_PURCHASE 만 선택지가 남는다. 네 capability 중 둘은 선행 조건이 없어 처음부터
    같이 열리고, 현금흐름이 차면 남은 셋이 한꺼번에 열린다. 나머지 두 mode 는 단계마다
    합법 Tool 이 하나뿐이라 **모델에게 물을 것이 없다.**
    """
    planner = _CountingPlanner()
    _reply, metadata = _run(mode, planner)

    assert planner.attempts == expected_planner_calls
    assert _trace(metadata)["llm_calls"] == expected_planner_calls
    #  물은 자리에는 늘 둘 이상이 놓여 있었다.
    assert all(count >= 2 for count in planner.options_per_call), planner.options_per_call


@pytest.mark.parametrize("mode", ["SCENARIO_VALIDATION", "SALES_VALIDATION"])
def test_a_dead_planner_does_not_matter_where_nothing_is_chosen(mode):
    """🔴 이 두 흐름은 provider 가 죽어 있어도 **닿지 않는다.**

    대체 경로를 타는 것이 아니라 애초에 부르지 않는다. 터지는 대역을 넣어도 실행이
    끝까지 간다는 것이 그 증거다.
    """
    reply, metadata = _run(mode, _ExplodingPlanner())

    assert _trace(metadata)["llm_calls"] == 0
    assert reply.runtime_status in {"READY", "RUNTIME_NOT_READY", "ERROR"}
    assert SELECTION_LLM not in _selection_sources(metadata)


def test_the_trace_says_who_chose_each_step():
    """어느 단계에서 왕복이 일어났는지 이력만 보고 알 수 있어야 한다."""
    _reply, metadata = _run("PRE_PURCHASE", _CountingPlanner())

    sources = [item for item in _selection_sources(metadata) if item]
    assert sources[-1] == SELECTION_FINALIZE
    assert sources.count(SELECTION_LLM) == 3
    assert sources.count(SELECTION_SINGLE) == 1
    #  종료는 한 번뿐이다 — 두 번 적히면 같은 실행이 두 번 끝난 것이다.
    assert sources.count(SELECTION_FINALIZE) == 1


# ── ③ 줄여도 답은 같다 ───────────────────────────────────────────────────

_BUSINESS_FIELDS = (
    "runtime_status",
    "business_status",
    "missing_data",
)


def _business_view(reply) -> tuple:
    return (
        *(getattr(reply, name, None) for name in _BUSINESS_FIELDS),
        json.dumps(reply.payload, sort_keys=True, default=str),
        tuple(sorted(item.claim for item in reply.evidences)),
    )


@pytest.mark.parametrize(
    "mode", ["PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION"]
)
def test_fewer_calls_do_not_change_a_single_business_value(mode):
    """🔴 **성능 개선의 조건이다.** 숫자가 달라지면 그것은 개선이 아니라 사고다.

    같은 입력을 결정론 Planner(= 모델이 꺼진 길)와 세는 Planner(= 모델이 켜진 길)로
    돌려 업무 값을 통째로 맞춘다. Tool 실행 수까지 같아야 한다 — 왕복만 줄고 일은
    그대로여야 한다.
    """
    quiet, quiet_metadata = _run(mode, DeterministicFinancePlanner())
    counted, counted_metadata = _run(mode, _CountingPlanner())

    assert _business_view(counted) == _business_view(quiet)
    assert counted_metadata.used_tools == quiet_metadata.used_tools
    assert _trace(counted_metadata)["tool_calls"] == _trace(quiet_metadata)["tool_calls"]


def test_the_amount_is_still_owned_by_the_rules_not_the_shortcut():
    """지름길이 금액을 만들지 않는다 — 판정도 상한도 Rule 이 그대로 낸다."""
    reply, _metadata = _run("SCENARIO_VALIDATION", _CountingPlanner())

    verdict = reply.payload["verdicts"][0]
    assert verdict["verdict"] in {"ok", "conditional", "reject"}
    assert verdict["rule_id"]
    #  상한은 Rule 이 낸 값이다. 지름길은 어느 Tool 을 부를지만 정했다.
    assert Decimal(str(verdict["finance_cap_amount_krw"])) == Decimal(800)
    assert verdict["adjustability"] == "ADJUSTABLE"
