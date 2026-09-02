"""Finance Sales Core Phase 7 — SALES_VALIDATION mode 와 Harness 등록.

★ 이 파일이 지키는 것은 **mode 사이의 벽**이다.
    · 판매 Tool 은 매입 두 mode 어디에서도 보이지 않는다 (반대도 같다)
    · Planner 는 판매 Tool 에 숫자를 실을 자리가 없다
    · 매입 Tool 계약(이름·의존·필수 capability)은 그대로다

★ 그리고 **아직 열지 않은 문**을 사고가 아니라 결정으로 남긴다.
  `finance_agent_runs_v22.mode` 의 CHECK 제약이 두 매입 mode 만 허용해서,
  Controller 에 연결하면 실행이력 저장이 전부 깨진다. DB 마이그레이션과 Master
  capability 라우팅이 함께 와야 열 수 있다.
"""

from types import SimpleNamespace

from app.finance.adapter import _CONTROLLER_MODES
from app.finance.application.harness import (
    _ARGUMENT_SCHEMAS,
    _TOOL_DESCRIPTIONS,
    CAPABILITY_OWNER,
    PRE_PURCHASE_TOOLS,
    SALES_REQUIRED_CAPABILITIES,
    SALES_VALIDATION_TOOLS,
    SCENARIO_VALIDATION_TOOLS,
    TOOL_DEPENDENCIES,
    FinanceToolRegistry,
    dependencies_of,
    required_capabilities,
)
from app.finance.schemas import FinanceMode

# ---------------------------------------------------------------------------
# mode 어휘
# ---------------------------------------------------------------------------


def test_sales_validation_is_a_finance_mode_of_its_own():
    from typing import get_args

    assert set(get_args(FinanceMode)) == {
        "PRE_PURCHASE",
        "SCENARIO_VALIDATION",
        "SALES_VALIDATION",
    }


def test_purchase_scenario_validation_is_not_reused_for_sales():
    # 같은 mode 를 나눠 쓰면 (agent, mode, call_seq) 로 둘을 구분할 수 없다.
    assert "evaluate_purchase_scenario" not in SALES_VALIDATION_TOOLS
    assert "evaluate_sales_scenario" not in SCENARIO_VALIDATION_TOOLS


# ---------------------------------------------------------------------------
# Tool 분리
# ---------------------------------------------------------------------------


def test_sales_tools_are_not_visible_to_purchase_modes():
    registry = FinanceToolRegistry(data_port=None)  # type: ignore[arg-type]

    assert registry.names_for("PRE_PURCHASE") == PRE_PURCHASE_TOOLS
    assert registry.names_for("SCENARIO_VALIDATION") == SCENARIO_VALIDATION_TOOLS
    assert "evaluate_sales_scenario" not in registry.names_for("PRE_PURCHASE")
    assert "evaluate_sales_scenario" not in registry.names_for("SCENARIO_VALIDATION")


def test_purchase_tools_are_not_visible_to_sales_mode():
    registry = FinanceToolRegistry(data_port=None)  # type: ignore[arg-type]
    names = registry.names_for("SALES_VALIDATION")

    assert names == SALES_VALIDATION_TOOLS
    assert not names & PRE_PURCHASE_TOOLS
    assert not names & SCENARIO_VALIDATION_TOOLS


def test_purchase_tool_names_are_unchanged_by_the_sales_addition():
    assert PRE_PURCHASE_TOOLS == frozenset(
        {
            "assess_finance_position",
            "project_cashflow",
            "calculate_purchase_finance_cap",
            "analyze_payment_pressure",
        }
    )
    assert SCENARIO_VALIDATION_TOOLS == frozenset(
        {"evaluate_purchase_scenario", "validate_amount_adjustment"}
    )


# ---------------------------------------------------------------------------
# capability 계약
# ---------------------------------------------------------------------------


def test_sales_capability_has_exactly_one_owning_tool():
    owners = [
        tool for cap, tool in CAPABILITY_OWNER.items() if cap == "sales_scenario_evaluation"
    ]

    assert owners == ["evaluate_sales_scenario"]
    assert len(set(CAPABILITY_OWNER.values())) == len(CAPABILITY_OWNER)


def test_sales_tool_declares_its_dependency_contract_explicitly():
    # 계약이 없는 Tool 을 "의존 없음"으로 조용히 읽지 않는다.
    assert dependencies_of("evaluate_sales_scenario") == frozenset()
    assert "evaluate_sales_scenario" in TOOL_DEPENDENCIES


def test_sales_mode_requires_its_own_capability():
    assert required_capabilities("SALES_VALIDATION") == SALES_REQUIRED_CAPABILITIES
    assert required_capabilities("SALES_VALIDATION") == {"sales_scenario_evaluation"}


def test_purchase_required_capabilities_are_unchanged():
    assert required_capabilities("PRE_PURCHASE") == {
        "finance_position",
        "cashflow_projection",
        "finance_cap",
        "payment_pressure",
    }
    assert required_capabilities("SCENARIO_VALIDATION") == {"scenario_evaluation"}


# ---------------------------------------------------------------------------
# Planner 숫자 안전
# ---------------------------------------------------------------------------


def test_planner_cannot_pass_any_argument_to_the_sales_tool():
    schema = _ARGUMENT_SCHEMAS["evaluate_sales_scenario"]

    assert schema.model_fields == {}
    assert schema.model_config["extra"] == "forbid"


def test_planner_numeric_business_arguments_are_rejected_outright():
    import pytest
    from pydantic import ValidationError

    schema = _ARGUMENT_SCHEMAS["evaluate_sales_scenario"]

    for injected in (
        {"quantity_kg": 100},
        {"unit_price_krw": 10000},
        {"sales_amount_krw": 1000000},
        {"sales_cost_basis_krw": 700000},
        {"payment_days": 30},
        {"credit_limit_krw": 50000000},
        {"contribution_margin_rate": 0.3},
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(injected)


def test_sales_tool_description_states_the_payload_is_the_source():
    description = _TOOL_DESCRIPTIONS["evaluate_sales_scenario"]

    assert "Takes no arguments" in description
    assert "request payload" in description


def test_harness_entrypoint_discards_planner_arguments():
    from app.finance.capabilities.sales import run_sales_validation

    state = SimpleNamespace(request=SimpleNamespace(payload={"scenario_id": "SC-1"}))

    result = run_sales_validation(
        None,  # type: ignore[arg-type]
        {"quantity_kg": 999999, "unit_price_krw": 999999},
        state,
    )

    # Planner 가 실은 숫자는 결과 어디에도 남지 않는다.
    assert result["status"] == "INPUT_INCOMPLETE"
    assert result["finance_verdict"] is None
    assert "999999" not in str(result)


def test_missing_sales_policies_are_reported_not_invented():
    from app.finance.capabilities.sales import run_sales_validation

    state = SimpleNamespace(
        request=SimpleNamespace(
            payload={
                "scenario_id": "SC-1",
                "partner_id": "P-1",
                "item": "red_pepper",
                "quantity_kg": "100",
                "unit_price_krw": "10000",
                "reported_sales_amount_krw": "1000000",
                "payment_terms_type": "SINGLE",
                "source_ref": "SALES-REPLY:R-1",
            }
        )
    )

    result = run_sales_validation(None, {}, state)  # type: ignore[arg-type]

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["finance_verdict"] is None
    for policy in (
        "finance_minimum_margin_rate",
        "finance_warning_margin_rate",
        "max_finance_allowed_payment_terms_days",
        "partner_credit_limit_krw",
        "sales_collection_risk_policy",
    ):
        assert policy in result["missing_data"], policy


# ---------------------------------------------------------------------------
# 아직 열지 않은 문 — 사고가 아니라 결정이다
# ---------------------------------------------------------------------------


def test_sales_validation_is_not_yet_routed_through_the_controller():
    """🔴 여기를 열기 전에 두 가지가 먼저 와야 한다.

        · `finance_agent_runs_v22.mode` CHECK 제약에 SALES_VALIDATION 추가 (DB 마이그레이션)
        · Master capability 라우팅 FINANCIAL_VALIDATION → (finance, SALES_VALIDATION)

    지금 연결하면 실행이력 저장이 CHECK 위반으로 전부 깨진다. 이 시험은 그 경계를
    **의도된 것으로** 못 박아 둔다 — 지우려면 위 둘을 먼저 해야 한다.
    """
    assert _CONTROLLER_MODES == ("PRE_PURCHASE", "SCENARIO_VALIDATION")
    assert "SALES_VALIDATION" not in _CONTROLLER_MODES


def test_finance_port_does_not_silently_treat_sales_as_a_purchase_mode():
    import inspect

    from app.finance import adapter

    source = inspect.getsource(adapter.finance_port)

    # 판매 요청이 매입 분기로 흘러들지 않는다 — 미구현으로 떨어진다.
    assert "SALES_VALIDATION" not in source
    assert "_not_implemented(request)" in source
