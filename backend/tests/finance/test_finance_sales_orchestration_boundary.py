"""Finance Sales Core Phase 9 — 오케스트레이션 경계.

★ 이 파일이 지키는 것은 **부서가 서로를 직접 부르지 않는다**는 것이다.
    · 영업이 재무를 직접 부르지 않는다
    · 재무가 영업을 직접 부르지 않는다
    · 둘 사이의 순서는 마스터가 소유한다

🔴 **판매 어휘가 열린 것과 E2E 라우팅 완료는 다르다.**
  현재 저장소에는 Sales AgentName과 호출 Mode가 있지만 실제 fan-out/refeed는 아직 없다.

  ```text
  AgentName            ["finance", "inventory", "purchase", "sales"]
  판매 호출 Mode       GENERATE_SALES_PROPOSAL
  재무 검증 Mode       SALES_VALIDATION
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


def test_master_has_sales_vocabulary_without_claiming_routing_is_complete():
    """Sales 호출 어휘는 존재하지만 그것만으로 실제 routing 완료를 뜻하지 않는다."""
    from typing import get_args

    from app.master.envelope import AgentName

    assert set(get_args(AgentName)) == {"finance", "inventory", "purchase", "sales"}


def test_master_routes_financial_validation_to_the_finance_mode():
    """어휘가 들어왔고 **이제 그 어휘로 오는 길도 있다.**

    `Mode` 에 SALES_VALIDATION 이 있어야 유효한 재무 `AgentRequest` 를 만들 수 있고,
    영업 제안이 거기까지 오는 길이 `FINANCIAL_VALIDATION` capability 라우팅이다.

    🔴 **이 검사는 원래 `assert not hasattr(envelope, "Capability")` 였다** — 라우팅이
      아직 없다는 것을 이름으로 남긴 것이었다. 마스터가 라우팅을 열면서(판매 Flow 골격)
      그 전제가 사실이 아니게 됐으므로, 없다는 단언 대신 **어디로 오는가**를 잠근다.

      재무가 지키려는 것은 그대로다 — 판매 제안은 재무의 SALES_VALIDATION 으로만 와야
      하고, 다른 모드나 다른 에이전트로 새면 안 된다
      (`test_sales_validation_is_not_opened_to_other_agents`).
    """
    from typing import get_args

    from app.master.envelope import (
        CAPABILITY_ROUTING,
        Mode,
        agent_allowed_modes,
    )

    assert "SALES_VALIDATION" in get_args(Mode)
    assert "SALES_VALIDATION" in agent_allowed_modes("finance")
    assert "GENERATE_SALES_PROPOSAL" in agent_allowed_modes("sales")

    # 재무로 오는 길은 하나다 — FINANCIAL_VALIDATION 이 재무의 SALES_VALIDATION 으로.
    assert CAPABILITY_ROUTING["FINANCIAL_VALIDATION"] == ("finance", "SALES_VALIDATION")


def test_sales_validation_is_not_opened_to_other_agents():
    from app.master.envelope import agent_allowed_modes

    assert "SALES_VALIDATION" not in agent_allowed_modes("inventory")
    assert "SALES_VALIDATION" not in agent_allowed_modes("purchase")


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
