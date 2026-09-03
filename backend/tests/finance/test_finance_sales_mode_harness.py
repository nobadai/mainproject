"""Finance Sales Core Phase 7 — SALES_VALIDATION mode 와 Harness 등록.

★ 이 파일이 지키는 것은 **mode 사이의 벽**이다.
    · 판매 Tool 은 매입 두 mode 어디에서도 보이지 않는다 (반대도 같다)
    · Planner 는 판매 Tool 에 숫자를 실을 자리가 없다
    · 매입 Tool 계약(이름·의존·필수 capability)은 그대로다

★ 그리고 **열린 문의 순서**를 못 박는다. 실행이력 `mode` CHECK 에 SALES_VALIDATION 을
  넣은 뒤에 Controller 를 열었다 — 반대로 하면 판정은 되는데 저장이 전부 깨진다.
  그래서 DDL/마이그레이션과 Controller 허용목록은 같이 움직인다.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

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
from app.finance.db import FinanceDataNotReady
from app.finance.schemas import FinanceMode

# ---------------------------------------------------------------------------
# 판매 Capability 를 직접 부를 때 쓰는 최소 도구
#
# ★ 거래처 채권은 이제 실 원장에서 온다. 그래서 Port 없이 이 Capability 를 부를 수
#   없다 — `None` 을 넘기던 예전 모양은 "조회가 아예 없다" 는 뜻이었다.
# ---------------------------------------------------------------------------

_AS_OF = date(2025, 12, 31)


def _sales_state(**over):
    payload = {
        "scenario_id": "SC-1",
        "partner_id": "P-1",
        "item": "red_pepper",
        "quantity_kg": "100",
        "unit_price_krw": "10000",
        "reported_sales_amount_krw": "1000000",
        "payment_terms_type": "SINGLE",
        "source_ref": "SALES-REPLY:R-1",
    }
    payload.update(over)
    return SimpleNamespace(
        request=SimpleNamespace(payload=payload, context=SimpleNamespace(as_of=_AS_OF))
    )


def _receivable_port(*receivables):
    """실 조회를 대신하는 최소 Port. **행을 주는 일만 한다.**"""

    class _Port:
        def load_partner_receivables(self, as_of, partner_id):
            del as_of, partner_id
            return list(receivables)

    return _Port()


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
    """실 채권을 읽고 난 뒤에도 **없는 것은 여전히 없다.**

    ★ 채권이 실데이터로 들어왔다고 여신한도까지 생기지 않는다. 한도는 거래처가
      소유한 별개의 사실이고 권위 있는 저장 위치가 아직 없다 — 채권 잔액으로
      한도를 역산하는 순간 없는 값이 판정에 들어간다.
    """
    from app.finance.capabilities.sales import run_sales_validation

    result = run_sales_validation(_receivable_port(), {}, _sales_state())

    assert result["status"] == "RUNTIME_NOT_READY"
    assert result["finance_verdict"] is None
    assert result["data_quality"] == "INCOMPLETE"
    # ★ 남는 것은 **여신한도 하나**다. 마진 임계값·최대 결제일수·회수위험 판정
    #   방식은 MVP 정책이, 거래처 채권은 실 원장이 공급한다.
    assert "partner_credit_limit_krw" in result["missing_data"]
    for supplied in (
        "finance_minimum_margin_rate",
        "finance_warning_margin_rate",
        "max_finance_allowed_payment_terms_days",
        "partner_receivable_facts",
    ):
        assert supplied not in result["missing_data"], supplied


def test_an_empty_ledger_is_a_fact_not_a_missing_fact():
    """🔴 조회가 성공했고 미회수 행이 0건인 것은 **채권이 0원이라는 사실**이다.

    신규 거래처를 "자료 미비" 로 다루면 영영 아무 판정도 받지 못한다.
    """
    from app.finance.capabilities.sales import run_sales_validation

    result = run_sales_validation(_receivable_port(), {}, _sales_state())

    assert result["financial_summary"]["current_partner_ar_krw"] == Decimal(0)
    assert result["financial_summary"]["overdue_ar_krw"] == Decimal(0)


def test_a_failed_lookup_is_not_an_empty_ledger():
    """🔴 못 읽은 것은 0원이 아니다 — 실행 자체가 서야 한다."""
    from app.finance.capabilities.sales import run_sales_validation

    class _BrokenPort:
        def load_partner_receivables(self, as_of, partner_id):
            raise FinanceDataNotReady("partner_receivables")

    with pytest.raises(FinanceDataNotReady):
        run_sales_validation(_BrokenPort(), {}, _sales_state())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 아직 열지 않은 문 — 사고가 아니라 결정이다
# ---------------------------------------------------------------------------


def test_sales_validation_is_routed_through_the_controller():
    """실행이력 `mode` CHECK 에 SALES_VALIDATION 이 들어간 뒤에 열었다.

    ★ 순서가 중요하다. 제약보다 먼저 열면 판정은 되는데 **저장이 전부 실패한다.**
      그래서 DDL/마이그레이션과 이 허용목록은 같이 움직인다.
    """
    assert _CONTROLLER_MODES == (
        "PRE_PURCHASE",
        "SCENARIO_VALIDATION",
        "SALES_VALIDATION",
    )


def test_controller_mode_allowlist_stays_closed():
    # 모르는 mode 가 실행이력에 새어 들어가지 않는다.
    assert "GENERATE_SCENARIOS" not in _CONTROLLER_MODES
    assert "STATUS_QUERY" not in _CONTROLLER_MODES


def test_finance_port_dispatches_sales_explicitly_not_by_falling_through():
    import inspect

    from app.finance import adapter

    source = inspect.getsource(adapter.finance_port)

    # 판매는 **자기 분기**로 간다 — 매입 분기로 흘러들지도, 미구현으로 떨어지지도 않는다.
    assert '"SALES_VALIDATION"' in source
    assert "_controller_sales_validation(request)" in source
    # 모르는 mode 는 여전히 닫힌다.
    assert "_not_implemented(request)" in source


def test_sales_controller_does_not_reuse_the_purchase_scenario_path():
    import ast
    import inspect

    from app.finance import adapter

    tree = ast.parse(inspect.getsource(adapter._controller_sales_validation))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    # 매입 제안 스키마로 판매 payload 를 검증하지 않는다.
    assert "_purchase_proposal" not in called
    assert "PurchaseProposal" not in called
    # 경계 확인과 Controller 실행만 한다.
    assert {"_controller_boundary", "_controller_run"} <= called
