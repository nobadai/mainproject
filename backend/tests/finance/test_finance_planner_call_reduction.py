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
    TOOL_BUDGET_EXHAUSTED,
    CapabilityState,
    FinanceHarness,
    FinanceToolRegistry,
)
from app.finance.application.orchestration import (
    SELECTION_FINALIZE,
    SELECTION_LLM,
    SELECTION_SINGLE,
    FinanceAgentController,
    _decide,
    _settled_action,
)
from app.finance.llm.planner import (
    DeterministicFinancePlanner,
    FinancePlannerFailure,
    ToolAction,
)
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

#: 회신에서 **업무 뜻을 지는 칸** 전부. 하나라도 빠지면 그 칸은 잠기지 않는다.
_BUSINESS_FIELDS = (
    "runtime_status",
    "business_status",
    "missing_data",
    "missing_capability",
    "needs_followup",
    "additional_validation_required",
    "judgment_fields",
    "reasoning",
)


def _evidence_view(evidences) -> tuple:
    """Evidence 를 **계약 전체로** 편다.

    🔴 claim 문자열만 맞추면 «같은 이름의 다른 숫자» 가 통과한다. 값·단위·근거 번호·
       등급까지 봐야 «Rule 이 낸 그 근거» 인지 확인된다.
    """
    return tuple(
        sorted(
            (
                item.claim,
                item.source,
                tuple(item.ref_ids),
                item.value,
                item.unit,
                item.evidence_grade,
                item.evidence_detail,
            )
            for item in evidences
        )
    )


def _adjustment_view(adjustments) -> tuple:
    return tuple(
        sorted(
            (
                item.dept,
                item.axis,
                item.target_value,
                item.unit,
                item.reason,
                tuple(item.ref_ids),
                getattr(item, "scenario_id", None),
            )
            for item in adjustments
        )
    )


def _business_view(reply) -> tuple:
    return (
        *(getattr(reply, name, None) for name in _BUSINESS_FIELDS),
        json.dumps(reply.payload, sort_keys=True, default=str),
        _evidence_view(reply.evidences),
        _adjustment_view(reply.suggested_adjustments),
    )


@pytest.mark.parametrize(
    "mode", ["PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION"]
)
def test_fewer_calls_do_not_change_a_single_business_value(mode):
    """🔴 **성능 개선의 조건이다.** 숫자가 달라지면 그것은 개선이 아니라 사고다.

    같은 입력을 결정론 Planner(= 모델이 꺼진 길)와 세는 Planner(= 모델이 켜진 길)로
    돌려 업무 값을 통째로 맞춘다.

        payload 전체 · Evidence 전체(값·단위·근거 번호·등급) · 조정안 전체
        missing_data · missing_capability · needs_followup
        additional_validation_required · judgment_fields · reasoning
        used_tools · rules_applied · tool_calls · **Tool 실행 순서**

    Tool 실행 수와 순서까지 같아야 한다 — 왕복만 줄고 일은 그대로여야 한다.
    """
    quiet, quiet_metadata = _run(mode, DeterministicFinancePlanner())
    counted, counted_metadata = _run(mode, _CountingPlanner())

    assert _business_view(counted) == _business_view(quiet)
    assert counted_metadata.used_tools == quiet_metadata.used_tools
    assert counted_metadata.rules_applied == quiet_metadata.rules_applied

    counted_trace = _trace(counted_metadata)
    quiet_trace = _trace(quiet_metadata)
    assert counted_trace["tool_calls"] == quiet_trace["tool_calls"]
    assert counted_trace["rules_applied"] == quiet_trace["rules_applied"]
    #  실행 순서까지 본다 — 같은 Tool 을 다른 차례로 돌면 관측 누적이 달라진다.
    assert counted_trace["executed_tools"] == quiet_trace["executed_tools"]


@pytest.mark.parametrize("mode", ["PRE_PURCHASE", "SCENARIO_VALIDATION"])
def test_the_business_view_actually_looks_at_something(mode):
    """★ 위 비교가 **빈 튜플끼리 맞추고 있지 않은지** 확인한다.

    🔴 동일성 검사는 볼 것이 없으면 조용히 통과한다. 그러면 «같다» 가 «아무것도 안 봤다»
       와 구별되지 않는다. 그래서 «볼 것이 있었다» 를 따로 못 박는다.
    """
    reply, _metadata = _run(mode, _CountingPlanner())
    view = _business_view(reply)

    assert view[0]  # runtime_status
    assert json.loads(view[len(_BUSINESS_FIELDS)])  # payload 가 비어 있지 않다
    assert reply.evidences  # 근거가 실제로 붙었다


def test_the_sales_branch_locks_the_failure_meaning_not_a_payload():
    """판매 검증은 이 fixture 에서 **답이 서지 않는 것이 정본**이다.

    ★ 판매 마진·결제일수·여신 정책이 이 DataPort 에 없다. 가짜 정책을 넣어 초록불을
      만들지 않는다 — 없는 것은 없는 채로 시험한다. 그래서 여기서 잠그는 동일성은
      «같은 payload» 가 아니라 **«같은 실패 의미»** 다.

    🔴 판매의 정상 판정 자체는 `test_finance_sales_validation_batch.py` 가 실제 정책
       맥락으로 잠근다. 여기서 그것까지 흉내 내면 **두 곳이 서로 다른 판매를 시험**하게 된다.
    """
    quiet, quiet_metadata = _run("SALES_VALIDATION", DeterministicFinancePlanner())
    counted, counted_metadata = _run("SALES_VALIDATION", _CountingPlanner())

    assert counted.runtime_status == quiet.runtime_status == "ERROR"
    assert counted.payload == quiet.payload == {}
    assert counted_metadata.used_tools == quiet_metadata.used_tools == ()
    assert _trace(counted_metadata)["tool_calls"] == _trace(quiet_metadata)["tool_calls"] == 0
    assert _trace(counted_metadata)["failure_kind"] == _trace(quiet_metadata)["failure_kind"]
    #  그리고 그 실패에 도달하는 동안 모델에게 한 번도 묻지 않았다.
    assert _trace(counted_metadata)["llm_calls"] == 0


def test_the_amount_is_still_owned_by_the_rules_not_the_shortcut():
    """지름길이 금액을 만들지 않는다 — 판정도 상한도 Rule 이 그대로 낸다."""
    reply, _metadata = _run("SCENARIO_VALIDATION", _CountingPlanner())

    verdict = reply.payload["verdicts"][0]
    assert verdict["verdict"] in {"ok", "conditional", "reject"}
    assert verdict["rule_id"]
    #  상한은 Rule 이 낸 값이다. 지름길은 어느 Tool 을 부를지만 정했다.
    assert Decimal(str(verdict["finance_cap_amount_krw"])) == Decimal(800)
    assert verdict["adjustability"] == "ADJUSTABLE"


# ── ④ selection_source 는 그 단계의 사실이다 ─────────────────────────────
#
# 허용값과 뜻은 넷뿐이다.
#
#     LLM                     실제 Planner 선택을 위해 provider 경로를 탔다
#     DETERMINISTIC_SINGLE    합법 Tool 이 하나뿐이라 결정론으로 집었다
#     DETERMINISTIC_FINALIZE  남은 capability 가 없어 결정론으로 끝냈다
#     None                    이 단계에서는 **아무도 Tool 을 고르지 않았다**
#                             (예: 예산 소진 · terminal guard 처럼 선택 전에 접힌 자리)
#
# 🔴 **관측용이다.** 업무 판정에 쓰면 안 된다 — 같은 판정이 선택 경로에 따라 달라진다.

_ALLOWED_SELECTION_SOURCES = {
    SELECTION_LLM,
    SELECTION_SINGLE,
    SELECTION_FINALIZE,
    None,
}


def test_a_step_that_chose_nothing_records_no_selection_source():
    """🔴 **고르지 않은 단계에 앞 단계의 출처가 남으면 이력이 거짓이 된다.**

    예산이 다하면 `_decide` 에 닿기 전에 접힌다. 그 단계에서는 아무도 Tool 을 고르지
    않았으므로 출처가 없어야 한다. 직전 값이 그대로 남으면 **부르지도 않은 선택이
    일어난 것처럼** 읽힌다 — 업무 결과는 그대로라서 더 늦게 들킨다.
    """
    planner = _CountingPlanner()
    with patch("app.finance.execution.save_finance_execution"):
        reply, metadata = FinanceAgentController(
            Port(), planner, _Finalizer(), max_tool_calls=1
        ).run(_request_for("PRE_PURCHASE"))

    steps = _trace(metadata)["steps"]
    assert len(steps) >= 2

    #  첫 단계에서는 실제로 골랐다 — 그 사실은 남아 있어야 한다.
    assert steps[0]["selection_source"] in {SELECTION_LLM, SELECTION_SINGLE}
    assert steps[0]["executed_tool"] == "project_cashflow"

    #  마지막 단계는 **선택 전에** 접혔다.
    assert steps[-1]["selection_source"] is None
    assert steps[-1]["denied_reason"] == TOOL_BUDGET_EXHAUSTED
    assert steps[-1]["executed_tool"] is None

    #  업무 의미는 예전과 같다 — 못 낸 답을 낸 척하지 않는다.
    assert reply.runtime_status == "ERROR"


def test_every_recorded_selection_source_is_in_the_allowed_set():
    """어휘를 고정한다. 새 값이 늘면 읽는 쪽이 조용히 갈라진다."""
    for mode in ("PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION"):
        _reply, metadata = _run(mode, _CountingPlanner())
        for step in _trace(metadata)["steps"]:
            assert step["selection_source"] in _ALLOWED_SELECTION_SOURCES, (mode, step)


# ── ⑤ 고를 것이 아예 없는 자리 ───────────────────────────────────────────


def test_zero_executable_tools_fails_without_asking_the_model():
    """🔴 남은 capability 는 있는데 부를 수 있는 Tool 이 없다 — **물어도 고를 것이 없다.**

    예전에는 빈 Tool 목록을 들고 모델에게 갔다. 모델은 고를 것이 없으니 아무거나
    말했고, 우리는 그 답을 반려하느라 되묻기 예산을 썼다. 결론은 같았고 왕복만 늘었다.

    ★ 문구는 `DeterministicFinancePlanner` 가 같은 자리에서 내는 것과 같다. 모델이
      켜졌느냐 꺼졌느냐로 **실패 이름이 갈리면** 같은 고장을 두 벌로 읽어야 한다.
    """
    planner = _ExplodingPlanner()
    harness = FinanceHarness(
        FinanceToolRegistry(Port()), max_tool_calls=8, max_replans=2
    )
    state = FinanceAgentState(_request_for("PRE_PURCHASE"))
    #  선행 조건이 없는 두 Tool 을 registry 에서 지워 «합법 Tool 0» 을 만든다.
    empty = CapabilityState(
        required=("finance_position",),
        completed=(),
        missing=("finance_position",),
        executable_tools=frozenset(),
        dependency_status={},
    )

    with pytest.raises(FinancePlannerFailure) as raised:
        _decide(state, planner=planner, harness=harness, capability_state=empty)

    assert planner.attempts == 0
    assert harness.llm_calls == 0
    assert harness.selection_source is None

    #  결정론 Planner 가 같은 자리에서 내는 문구와 같아야 한다.
    deterministic = DeterministicFinancePlanner()
    with pytest.raises(FinancePlannerFailure) as mirror:
        deterministic.decide(
            allowed_tools=frozenset(), missing_capabilities=("finance_position",)
        )
    assert str(raised.value) == str(mirror.value)


# ── ⑥ 분기가 여럿이어도 공유 상태가 섞이지 않는다 ────────────────────────


def test_many_scenarios_share_one_harness_without_asking_the_model():
    """🔴 Harness 는 **실행 전체에서 공유된다.** 분기가 늘어도 예산이 늘지 않는다.

    그래서 분기 사이에 `_seen`(중복 감시) · `tool_calls` · `selection_source` 가 섞이면
    한 분기의 사실이 다른 분기의 반려가 된다. 시나리오를 셋 넣어 그것을 본다.

    ★ 같은 Tool 이 분기마다 한 번씩 돈다. 중복 감시는 `branch_id` 를 서명에 넣으므로
      **정상이며 오탐이 아니어야 한다.**
    """
    planner = _CountingPlanner()
    payloads = {
        "scenarios": [
            {**scenario_payload(amount), "scenario_id": f"S-{index}"}
            for index, amount in enumerate((600, 1000, 900), start=1)
        ]
    }
    with patch("app.finance.execution.save_finance_execution"):
        reply, metadata = FinanceAgentController(Port(), planner, _Finalizer()).run(
            request("SCENARIO_VALIDATION", payloads)
        )

    trace = _trace(metadata)

    #  ① 모델에게 한 번도 묻지 않았다.
    assert planner.attempts == 0
    assert trace["llm_calls"] == 0
    assert SELECTION_LLM not in [step.get("selection_source") for step in trace["steps"]]

    #  ② 분기마다 판정이 섰다.
    verdicts = {item["scenario_id"]: item["verdict"] for item in reply.payload["verdicts"]}
    assert set(verdicts) == {"S-1", "S-2", "S-3"}
    assert all(value in {"ok", "conditional", "reject"} for value in verdicts.values())

    #  ③ 중복 감시 오탐이 없다 — 같은 Tool 이 분기마다 돌았는데 반려가 없다.
    assert trace["denials"] == []
    assert trace["replans"] == 0
    assert trace["executed_tools"].count("evaluate_purchase_scenario") == 3

    #  ④ 분기가 섞이지 않았다. 각 분기의 마지막은 자기 분기에서 끝난다.
    branch_ids = {step["branch_id"] for step in trace["steps"]}
    assert branch_ids == {"S-1", "S-2", "S-3"}
    for branch in branch_ids:
        own = [step for step in trace["steps"] if step["branch_id"] == branch]
        assert own[-1]["selection_source"] == SELECTION_FINALIZE


def test_many_scenarios_keep_every_business_value_identical():
    """분기가 늘어도 «모델을 부르지 않은 길» 과 «부를 수 있던 길» 의 답이 같다."""
    payloads = {
        "scenarios": [
            {**scenario_payload(amount), "scenario_id": f"S-{index}"}
            for index, amount in enumerate((600, 1000, 900), start=1)
        ]
    }

    def _go(planner):
        with patch("app.finance.execution.save_finance_execution"):
            return FinanceAgentController(Port(), planner, _Finalizer()).run(
                request("SCENARIO_VALIDATION", payloads)
            )

    quiet, quiet_metadata = _go(DeterministicFinancePlanner())
    counted, counted_metadata = _go(_CountingPlanner())

    assert _business_view(counted) == _business_view(quiet)
    assert counted_metadata.used_tools == quiet_metadata.used_tools
    assert _trace(counted_metadata)["executed_tools"] == _trace(quiet_metadata)["executed_tools"]


# ── ⑦ 되묻기는 «고를 것이 있던 자리» 에서만 는다 ────────────────────────


@pytest.mark.parametrize(
    "mode", ["PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION"]
)
def test_the_shortcut_never_costs_a_replan(mode):
    """🔴 결정론으로 집은 선택은 반려될 수 없다 — Harness 가 고른 것을 Harness 가 막지 않는다.

    되묻기는 **모델이 고를 수 있었던 자리**에서만 뜻이 있다. 지름길에서 되묻기가 늘면
    그것은 «합법이라던 Tool 이 합법이 아니었다» 는 뜻이라 계산이 어긋난 것이다.
    """
    _reply, metadata = _run(mode, _CountingPlanner())
    trace = _trace(metadata)

    shortcut_steps = [
        step
        for step in trace["steps"]
        if step.get("selection_source") in {SELECTION_SINGLE, SELECTION_FINALIZE}
    ]
    assert all(step["denied_reason"] is None for step in shortcut_steps), shortcut_steps
    if planner_free := (trace["llm_calls"] == 0):
        assert trace["replans"] == 0, planner_free


# ── ⑧ llm_status 는 «이번 실행» 의 사실이다 ─────────────────────────────


def test_llm_status_describes_this_run_not_the_controller_lifetime():
    """🔴 **이번 실행에서 모델을 불렀는가**를 말해야 한다 — 예전에 불렀는가가 아니다.

    같은 Controller 로 두 번 돌린다.

        1차 PRE_PURCHASE        실제로 물었다        → SUCCESS
        2차 SALES_VALIDATION    한 번도 묻지 않았다  → SKIPPED_TEMPLATE

    누적 `attempts` 를 보면 2차가 1차의 호출을 **자기 것으로 읽어** SUCCESS 가 된다.
    업무 값은 그대로라서 이 거짓은 이력에만 남는다 — 나중에 "이 실행은 모델이 답했다"
    로 읽히고, 그 판단으로 provider 비용과 장애를 되짚으면 엉뚱한 곳을 본다.

    ★ 오늘 adapter 는 요청마다 Controller 를 새로 만든다. 그래서 이것은 **지금 나는
      고장이 아니라 계약의 구멍**이다. 계약이 누적 상태에 기대면 재사용하는 날 깨진다.
    """
    controller = FinanceAgentController(Port(), _CountingPlanner(), _Finalizer())

    with patch("app.finance.execution.save_finance_execution"):
        _first, first_metadata = controller.run(_request_for("PRE_PURCHASE"))
        _second, second_metadata = controller.run(_request_for("SALES_VALIDATION"))

    assert first_metadata.llm_status == "SUCCESS"
    #  planner 3 + finalizer 0 — 설명 후보가 하나뿐이라 Finalizer 는 불리지 않는다.
    assert first_metadata.llm_attempts == 3

    #  2차는 Planner 도 Finalizer 도 부르지 않았다.
    assert _trace(second_metadata)["llm_calls"] == 0
    assert second_metadata.llm_status == "SKIPPED_TEMPLATE"
    assert second_metadata.llm_attempts == 0
