"""Finance Sales Core Phase 9 — 오케스트레이션 경계.

★ 이 파일이 지키는 것은 **부서가 서로를 직접 부르지 않는다**는 것이다.
    · 영업이 재무를 직접 부르지 않는다
    · 재무가 영업을 직접 부르지 않는다
    · 둘 사이의 순서는 마스터가 소유한다

🔴 **E2E(Sales → Master → Finance → Master → Sales Refeed)는 아직 못 만든다.**
  현재 저장소에 공통 계약이 없다.

  ```text
  AgentName            ["finance", "inventory", "purchase"] — sales 없음
  capability 어휘      FINANCIAL_VALIDATION 이 어디에도 없음
  Feedback Envelope    최종 필드명 미확정 (팀 결정 대기)
  ```

  이름을 지어내서 E2E 를 만들면 그 이름이 곧 계약이 된다 — 나중에 진짜 계약이
  오면 두 벌이 되고, 그때 어느 쪽이 맞는지 아무도 모른다. 그래서 여기서는
  **경계까지만** 시험하고 막힌 지점을 이름으로 남긴다.
"""

import ast
import pathlib

FINANCE = pathlib.Path("app/finance")
SALES = pathlib.Path("app/sales")


def _imported_modules(root: pathlib.Path) -> set[str]:
    modules: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


# ---------------------------------------------------------------------------
# 직접 호출이 없다
# ---------------------------------------------------------------------------


def test_finance_never_imports_the_sales_agent():
    offenders = {name for name in _imported_modules(FINANCE) if name.startswith("app.sales")}

    assert offenders == set(), offenders


def test_sales_never_imports_the_finance_agent():
    offenders = {name for name in _imported_modules(SALES) if name.startswith("app.finance")}

    assert offenders == set(), offenders


def test_finance_touches_master_only_through_shared_contract_modules():
    """재무가 마스터에서 아는 것은 **계약 모듈뿐**이다.

    ★ `critic_bridge` 는 이 작업 이전부터 있던 계약 통로다(판정 근거를 Critic 형태로
      옮긴다). 여기서 넓히지 않는다 — 늘어나면 라우팅이 재무 안으로 새는 신호다.
    """
    master_modules = {
        name for name in _imported_modules(FINANCE) if name.startswith("app.master")
    }

    assert master_modules == {"app.master.envelope", "app.master.critic_bridge"}


def test_the_sales_capability_takes_a_payload_not_a_sales_client():
    """판매 Capability 는 payload 를 받는다 — 영업을 부르지 않는다."""
    import inspect

    from app.finance.capabilities import sales

    signature = inspect.signature(sales.evaluate_sales_scenario)
    assert next(iter(signature.parameters)) == "payload"

    source = inspect.getsource(sales)
    for forbidden in ("requests.", "httpx.", "urlopen", "app.sales"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# 아직 막힌 것 — 지어내지 않고 이름으로 남긴다
# ---------------------------------------------------------------------------


def test_master_still_has_no_sales_agent_name():
    """🔴 `AgentName` 에 sales 가 들어오기 전에는 E2E 를 만들 수 없다."""
    from typing import get_args

    from app.master.envelope import AgentName

    assert set(get_args(AgentName)) == {"finance", "inventory", "purchase"}
    assert "sales" not in get_args(AgentName)


def test_master_has_no_capability_vocabulary_to_route_financial_validation():
    """🔴 `FINANCIAL_VALIDATION → (finance, SALES_VALIDATION)` 라우팅의 전제가 없다.

    마스터에 capability 어휘 자체가 없다. 여기에 라우팅을 만들면 그것은 연동이
    아니라 **마스터 재설계**다 — 이 작업의 범위가 아니다.
    """
    from app.master import envelope

    source = pathlib.Path(envelope.__file__).read_text(encoding="utf-8")

    assert "FINANCIAL_VALIDATION" not in source
    assert "SALES_VALIDATION" not in source


def test_finance_side_of_the_contract_is_nevertheless_complete():
    """재무 쪽 절반은 다 되어 있다 — 막힌 것은 공통 계약이지 재무가 아니다."""
    from app.finance.adapter import (
        SALES_VERDICT_TO_BUSINESS_STATUS,
        build_sales_validation_payload,
        map_sales_finance_verdict,
    )
    from app.finance.application.harness import SALES_VALIDATION_TOOLS
    from app.finance.capabilities.sales import evaluate_sales_scenario

    assert callable(evaluate_sales_scenario)
    assert callable(map_sales_finance_verdict)
    assert callable(build_sales_validation_payload)
    assert SALES_VALIDATION_TOOLS == frozenset({"evaluate_sales_scenario"})
    assert SALES_VERDICT_TO_BUSINESS_STATUS["REVIEW_REQUIRED"] == "conditional"
