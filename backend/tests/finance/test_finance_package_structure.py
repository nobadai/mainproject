"""재무 패키지 구조 — **책임 분리는 유지하고 기존 진입점은 깨지 않는다.**

★ 이 파일이 지키는 것은 디렉터리 모양이 아니라 두 가지다.
    · 업무 로직이 두 곳에 살지 않는다
    · 밖에서 쓰던 import 경로가 그대로 산다 (재무 밖 코드는 고치지 않는다)
"""

from __future__ import annotations

import ast
import collections
import importlib
import pathlib

import pytest

FINANCE = pathlib.Path("app/finance")


def _modules() -> list[str]:
    out = []
    for path in sorted(FINANCE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out.append(".".join(path.with_suffix("").parts))
    return out


# ---------------------------------------------------------------------------
# 외부/기존 진입점 호환
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        # 재무 밖에서 실제로 import 하는 경로 — 절대 깨뜨리지 않는다.
        "app.finance.db",            # master · orchestrator
        "app.finance.adapter",       # main.py (finance_port)
        "app.finance.router",        # main.py
        "app.finance.schemas",       # tests/llm
        "app.finance.interpretation",  # tests/llm
        "app.finance.llm.runtime",   # tests/llm
        "app.finance.llm.schemas",   # tests/llm
    ],
)
def test_externally_imported_modules_still_resolve(module):
    importlib.import_module(module)


def test_finance_port_and_controller_still_resolve():
    from app.finance.adapter import finance_port
    from app.finance.agent import FinanceAgentController

    assert callable(finance_port)
    assert FinanceAgentController is not None


def test_router_exposes_the_same_endpoints():
    from app.finance.router import router

    paths = {route.path for route in router.routes}
    assert {"/finance/agent", "/finance/sales", "/finance/runs"} <= paths


def test_tool_registry_keeps_its_public_names():
    """재무 내부·재무 테스트가 이 경로로 들어온다."""
    from app.finance import tool_registry

    for name in (
        "PRE_PURCHASE_TOOLS",
        "SCENARIO_VALIDATION_TOOLS",
        "FinanceToolRegistry",
        "_scenario_schedule",
        "_schedule_events",
        "_calculate_schedule_cap",
    ):
        assert hasattr(tool_registry, name), name


def test_every_finance_module_imports():
    """구조를 옮긴 뒤 **한 모듈도 죽지 않았는지** 통째로 확인한다."""
    for module in _modules():
        importlib.import_module(module)


# ---------------------------------------------------------------------------
# 책임 분리
# ---------------------------------------------------------------------------


def test_registry_is_a_thin_dispatcher():
    """🔴 예전에는 이 파일 하나가 디스패치·컨텍스트·두 mode·일정·Evidence 를 다 들었다."""
    source = (FINANCE / "tool_registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    registry = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "FinanceToolRegistry"
    )
    methods = {n.name for n in registry.body if isinstance(n, ast.FunctionDef)}
    assert methods == {"__init__", "names_for", "execute"}

    # 업무 계산이 디스패처로 돌아오지 않았는지 본다.
    assert "project_cashflow(" not in source.replace('"project_cashflow"', "")
    assert "classify_base_stress" not in source


def test_capabilities_are_split_by_mode():
    from app.finance.capabilities import pre_purchase, scenario_validation

    for name in (
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    ):
        assert hasattr(pre_purchase, name), name
    for name in ("evaluate_purchase_scenario", "validate_amount_adjustment"):
        assert hasattr(scenario_validation, name), name


def test_tool_names_are_unchanged():
    """Planner 계약이다 — 이름이 바뀌면 모델이 고를 수 없다."""
    from app.finance.tool_registry import PRE_PURCHASE_TOOLS, SCENARIO_VALIDATION_TOOLS

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


def test_no_capability_is_registered_twice():
    from app.finance.tool_registry import (
        _CAPABILITIES,
        PRE_PURCHASE_TOOLS,
        SCENARIO_VALIDATION_TOOLS,
    )

    assert not PRE_PURCHASE_TOOLS & SCENARIO_VALIDATION_TOOLS
    assert set(_CAPABILITIES) == PRE_PURCHASE_TOOLS | SCENARIO_VALIDATION_TOOLS
    # 한 구현이 두 이름에 걸리면 어느 쪽을 고쳤는지 알 수 없다.
    assert len({id(fn) for fn in _CAPABILITIES.values()}) == len(_CAPABILITIES)


def test_no_duplicate_business_definitions_were_introduced():
    """같은 규칙이 두 벌이면 한쪽만 고쳐지고, 그때 갈리는 것은 판정이다."""
    seen: dict[str, list[str]] = collections.defaultdict(list)
    for path in sorted(FINANCE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef | ast.ClassDef):
                seen[node.name].append(str(path))

    duplicates = {name: paths for name, paths in seen.items() if len(paths) > 1}
    # ★ 아래 둘은 **업무 로직 중복이 아니다.**
    #   · project_cashflow : 계산(tools) 과 capability 이름이 같다 — Tool 이름은
    #     Planner 계약이라 바꿀 수 없다. capability 는 계산을 부를 뿐이다.
    #   · _read_bool       : 레거시 해석 런타임이 예전부터 자기 것을 갖고 있다.
    assert set(duplicates) <= {"project_cashflow", "_read_bool"}, duplicates


def test_deterministic_calculations_stay_in_tools():
    """계산은 `tools.py` 소유다 — capability 로 복사되지 않았다."""
    from app.finance import tools

    for name in (
        "project_cashflow",
        "calculate_finance_cap",
        "derive_cash_priority",
        "build_payroll_schedule",
    ):
        assert hasattr(tools, name), name

    capability = (FINANCE / "capabilities" / "pre_purchase.py").read_text(encoding="utf-8")
    # cap 공식이 capability 안에서 다시 구현되지 않았다.
    assert "ROUND_FLOOR" not in capability
    assert "calculate_finance_cap(" in capability


def test_rules_are_not_absorbed_into_agent_or_adapter():
    """판정은 `rules.py` 가 소유한다."""
    from app.finance import rules

    assert hasattr(rules, "classify_base_stress")
    for module in ("agent.py", "adapter.py"):
        source = (FINANCE / module).read_text(encoding="utf-8")
        assert "def classify_base_stress" not in source
