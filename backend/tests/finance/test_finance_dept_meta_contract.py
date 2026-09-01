"""Finance DeptMeta 의존 계약 — **적은 것과 실제로 읽는 것이 어긋나면 안 된다.**

🔴 `inputs_used` 가 실제보다 적으면 Critic 의 `E-GRADE-LEAK` 는 *우리가 적은 것*을
   검사한다. 매입 소유 입력이 재무 cap 에 섞여도 보고에 없으면 검사는 통과한다 —
   틀렸다는 사실만 아무도 모른다.

★ 구분: `state.tool_order` 는 **실행에서 관측된 것**이고, `_CAP_TOOL_INPUTS` 는
  **재무가 소유한 정적 의존 계약**이다. 관측된 Tool 을 계약으로 해석해 입력을 만든다.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.finance.execution import (
    _CAP_TOOL_INPUTS,
    _CONTEXT_INPUTS,
    _TOOL_INTERNAL_CALLS,
    FINANCE_CAP_CHECK_ID,
    FinanceToolDependencyMissing,
    _finance_dept_meta,
    _resolve_tool_inputs,
)
from app.finance.state import FinanceAgentState
from app.finance.tool_registry import PRE_PURCHASE_TOOLS
from app.master.critic_bridge import DEPT_CAP_CHECK_ID
from app.master.envelope import AgentRequest, ExecutionContext

FORBIDDEN_IN_FINANCE_CAP = frozenset(
    {"grade_unit_price", "qty_kg", "total_qty_kg", "avg_unit_price", "sourcing_plan"}
)


def test_finance_check_id_uses_master_canonical_contract():
    """Finance DeptMeta의 key는 Master가 합성하는 검사 id 정본을 따른다."""
    assert FINANCE_CAP_CHECK_ID == DEPT_CAP_CHECK_ID["finance"]


def _request(mode: str = "PRE_PURCHASE") -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-deps",
            as_of=date(2025, 1, 1),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode=mode,
    )


def _state(tools: list[str], *, debt: Decimal = Decimal(0)) -> FinanceAgentState:
    state = FinanceAgentState(_request())
    state.tool_order.extend(tools)
    state.context_cache = ({"current_debt_krw": debt}, None, [])
    return state


# ---------------------------------------------------------------------------
# 계약 완전성 — 드리프트 차단
# ---------------------------------------------------------------------------


def test_every_pre_purchase_tool_has_dependency_metadata():
    """Tool 을 새로 만들고 의존을 안 적으면 **여기서** 걸린다."""
    undeclared = PRE_PURCHASE_TOOLS - set(_CAP_TOOL_INPUTS)
    assert not undeclared, f"의존 계약이 없는 PRE_PURCHASE Tool: {sorted(undeclared)}"


def test_internal_call_targets_are_themselves_declared():
    """내부 호출 대상도 계약이 있어야 전이 폐포가 성립한다."""
    targets = {item for targets in _TOOL_INTERNAL_CALLS.values() for item in targets}
    assert not targets - set(_CAP_TOOL_INPUTS)


def test_unknown_executed_tool_fails_closed():
    """🔴 조용히 0개로 보고하지 않는다.

    빈 `inputs_used` 는 Critic 이 *"금지 입력이 없다"* 로 읽고 통과시킨다 — 모르는
    것이 통과가 되는 구조라 크게 실패하는 편이 낫다.
    """
    with pytest.raises(FinanceToolDependencyMissing) as raised:
        _resolve_tool_inputs("some_new_finance_tool", has_debt=False)
    assert raised.value.tool == "some_new_finance_tool"

    with pytest.raises(FinanceToolDependencyMissing):
        _finance_dept_meta("PRE_PURCHASE", {"finance_cap_amount_krw": 1}, [_state(["nope"])])


# ---------------------------------------------------------------------------
# 전이 의존 — cap 을 만든 현금흐름 입력이 빠지면 안 된다
# ---------------------------------------------------------------------------


def test_finance_cap_carries_transitive_cashflow_dependency():
    """🔴 `calculate_purchase_finance_cap` 은 투영이 없으면 `project_cashflow` 를 직접 부른다.

    결정론 Planner 처럼 `project_cashflow` 를 따로 고르지 않은 실행에서는 그 Tool 이
    `tool_order` 에 없다 — 전이 의존을 안 따라가면 **cap 을 만든 현금흐름 입력이 통째로
    보고에서 빠진다.**
    """
    inputs = _resolve_tool_inputs("calculate_purchase_finance_cap", has_debt=False)

    assert "finance_cash_events.obligations" in inputs
    assert "finance_cash_events.receivables" in inputs
    assert "finance_policy.monthly_labor_cost_krw" in inputs
    assert "finance_policy.cashflow_projection_days" in inputs
    assert "base_projection.projected_cash_by_date" in inputs


def test_debt_dependency_appears_only_when_debt_was_actually_read():
    """`_context` 는 부채가 있을 때만 상환 일정을 읽는다 — 안 읽은 것을 적지 않는다."""
    without = _resolve_tool_inputs("calculate_purchase_finance_cap", has_debt=False)
    with_debt = _resolve_tool_inputs("calculate_purchase_finance_cap", has_debt=True)

    assert "finance_cash_events.debt_service" not in without
    assert "finance_cash_events.debt_service" in with_debt


def test_context_inputs_are_shared_by_every_tool():
    """어느 Tool 이 불렀든 `_context` 는 같은 것을 읽는다."""
    for tool in sorted(PRE_PURCHASE_TOOLS):
        inputs = _resolve_tool_inputs(tool, has_debt=False)
        assert set(_CONTEXT_INPUTS) <= set(inputs), tool


def test_declared_cap_inputs_contain_no_purchase_owned_field():
    """정상 재무 cap 은 등급·수량을 읽지 않는다 — 읽게 되면 여기가 먼저 깨진다."""
    meta = _finance_dept_meta(
        "PRE_PURCHASE",
        {"finance_cap_amount_krw": 1},
        [_state(sorted(PRE_PURCHASE_TOOLS))],
    )
    inputs = set(meta["inputs_used"][FINANCE_CAP_CHECK_ID])
    assert not inputs & FORBIDDEN_IN_FINANCE_CAP


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — 산출 필드는 실제 payload 에서 나온다
# ---------------------------------------------------------------------------


def test_scenario_validation_produced_fields_are_runtime_derived():
    payload = {"verdicts": [{"verdict": "ok"}], "finance_cap_amount_krw": 100, "empty": None}
    meta = _finance_dept_meta("SCENARIO_VALIDATION", payload, [_state([])])

    assert meta["observation_type"] == "finance_dept_meta"
    # 없는 검사에 가짜 입력을 지어내지 않는다.
    assert meta["inputs_used"] == {}
    # 값이 None 인 키는 산출한 것이 아니다.
    assert meta["produced_fields"] == ["finance_cap_amount_krw", "verdicts"]


def test_dept_meta_is_absent_without_states():
    assert _finance_dept_meta("PRE_PURCHASE", {}, []) is None
    assert _finance_dept_meta("STATUS_QUERY", {"a": 1}, [_state([])]) is None


# ---------------------------------------------------------------------------
# 실제 Critic 까지 — 오염된 metadata 가 검출되는가
# ---------------------------------------------------------------------------


def _critic_verdict(observations: dict[str, tuple[str, ...]]):
    from app.critic.service import run_critic_procurement
    from app.master import critic_bridge as bridge
    from tests.master.test_critic_bridge import CONSTRAINTS, EVIDENCES, _proposal

    request = bridge.build_request(
        as_of=date(2025, 12, 31),
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
        observations=observations,
    )
    return request, run_critic_procurement(request)


def _observation(inputs: list[str], produced: list[str]) -> str:
    return json.dumps(
        {
            "observation_type": "finance_dept_meta",
            "inputs_used": {FINANCE_CAP_CHECK_ID: inputs},
            "produced_fields": produced,
        }
    )


def _scenario_observation(produced: list[str]) -> str:
    return json.dumps(
        {
            "observation_type": "finance_dept_meta",
            "inputs_used": {},
            "produced_fields": produced,
        }
    )


def test_polluted_cap_inputs_reach_critic_and_trigger_grade_leak():
    """매입 소유 입력이 재무 cap 입력에 섞이면 **실제 Critic 이** 잡는다."""
    observation = _observation(
        ["finance_state.current_cash_krw", "qty_kg"], ["finance_cap_amount_krw"]
    )
    _request_obj, verdict = _critic_verdict({"finance": (observation,)})

    assert any("E-GRADE-LEAK" in f.check_id for f in verdict.findings), verdict.findings
    assert any("qty_kg" in f.detail for f in verdict.findings)


def test_scenario_produced_fields_reach_critic_and_trigger_authority():
    """시나리오 산출 필드에 S3 전속 판정이 있으면 `E-AUTHORITY` 가 돈다.

    이것이 SCENARIO_VALIDATION DeptMeta 를 나르는 이유다 — 안 나르면 검사 자체가
    생략된다.
    """
    boundary = _observation(["finance_state.current_cash_krw"], ["finance_cap_amount_krw"])
    scenario = _scenario_observation(["verdicts", "has_unmet_obligation"])
    _request_obj, verdict = _critic_verdict({"finance": (boundary, scenario)})

    assert any("E-AUTHORITY" in f.check_id for f in verdict.findings), verdict.findings
    assert any("has_unmet_obligation" in f.detail for f in verdict.findings)


def test_scenario_observation_does_not_erase_boundary_cap_inputs():
    """🔴 합치지 않고 덮어쓰면 시나리오 관측(빈 inputs_used)이 cap 입력을 지운다.

    그러면 등급 누출 검사가 *"금지 입력 없음"* 으로 조용히 통과한다.
    """
    boundary = _observation(
        ["finance_state.current_cash_krw", "qty_kg"], ["finance_cap_amount_krw"]
    )
    scenario = _scenario_observation(["verdicts"])
    request, verdict = _critic_verdict({"finance": (boundary, scenario)})

    assert request.dept_meta is not None
    finance_meta = request.dept_meta["finance"]
    assert "qty_kg" in finance_meta.inputs_used[FINANCE_CAP_CHECK_ID]
    assert set(finance_meta.produced_fields) == {"finance_cap_amount_krw", "verdicts"}
    assert any("E-GRADE-LEAK" in f.check_id for f in verdict.findings)


def test_normal_finance_metadata_produces_no_authority_or_leak_finding():
    """정상 재무 metadata 는 두 검사를 **돌리되** finding 을 내지 않는다."""
    boundary = _observation(
        sorted(_resolve_tool_inputs("calculate_purchase_finance_cap", has_debt=True)),
        ["finance_cap_amount_krw", "available_cash"],
    )
    scenario = _scenario_observation(["verdicts", "finance_cap_amount_krw"])
    _request_obj, verdict = _critic_verdict({"finance": (boundary, scenario)})

    codes = [f.check_id for f in verdict.findings]
    assert "E-GRADE-LEAK" not in codes
    assert "E-AUTHORITY" not in codes
