"""Finance Agent 공개 호환 표면.

★ **구현은 여기 없다.** 수명주기는 `application.controller`, Tool 선택 루프는
  `application.planner_loop`, 계약/상한 guard 는 `application.guards`, 업무 결과 확정은
  `application.finalization` 이 소유한다.

★ 이 모듈이 남아 있는 이유는 **호환**이다. `app.finance.agent` 로 들어오던 기존
  import(어댑터·재무 테스트)를 그대로 살려 둔다 — 옮기면서 부르는 쪽을 깨뜨리지 않는
  것이 이번 정리의 조건이었다.

★ 재노출뿐이다. 여기에 업무 로직을 다시 넣지 않는다.
"""

from __future__ import annotations

from app.finance.application.controller import (
    DEFAULT_MAX_REPLANS,
    DEFAULT_MAX_TOOL_CALLS,
    FinanceAgentController,
    _BranchOutcome,
    _Explanation,
)
from app.finance.application.finalization import (
    _NON_PAYLOAD_RESULT_KEYS,
    build_business_result,
    fallback_reasoning,
    scenario_result,
)
from app.finance.application.guards import (
    _scenario_identity,
    _short_reason,
    _validate_finance_payload,
    _validate_ready_reasoning,
    guard_replan,
    source_owned_arguments,
    validate_finance_scenario_output,
)
from app.finance.application.planner_loop import branch_requests, execute_loop
from app.finance.evidence import _indexed_verdict_evidence
from app.finance.execution import FINANCE_CAP_CHECK_ID, _finance_dept_meta
from app.finance.llm.config import finance_llm_enabled
from app.finance.llm.contracts import (
    FinanceFinalizer,
    FinanceMode,
    FinancePlanner,
    FinancePlannerContractViolation,
    FinancePlannerFailure,
    ToolAction,
)
from app.finance.llm.finalizer import DeterministicFinanceFinalizer
from app.finance.llm.planner import DeterministicFinancePlanner
from app.finance.run_repository import save_finance_execution
from app.finance.state import FinanceAgentState, ScenarioPayment
from app.finance.tool_registry import (
    PRE_PURCHASE_TOOLS,
    SCENARIO_VALIDATION_TOOLS,
    FinanceToolRegistry,
    _scenario_schedule,
)

__all__ = [
    "DEFAULT_MAX_REPLANS",
    "DEFAULT_MAX_TOOL_CALLS",
    "FINANCE_CAP_CHECK_ID",
    "PRE_PURCHASE_TOOLS",
    "SCENARIO_VALIDATION_TOOLS",
    "_NON_PAYLOAD_RESULT_KEYS",
    "DeterministicFinanceFinalizer",
    "DeterministicFinancePlanner",
    "FinanceAgentController",
    "FinanceAgentState",
    "FinanceFinalizer",
    "FinanceMode",
    "FinancePlanner",
    "FinancePlannerContractViolation",
    "FinancePlannerFailure",
    "FinanceToolRegistry",
    "ScenarioPayment",
    "ToolAction",
    "_BranchOutcome",
    "_Explanation",
    "_finance_dept_meta",
    "_indexed_verdict_evidence",
    "_scenario_identity",
    "_scenario_schedule",
    "_short_reason",
    "_validate_finance_payload",
    "_validate_ready_reasoning",
    "branch_requests",
    "build_business_result",
    "execute_loop",
    "fallback_reasoning",
    "finance_llm_enabled",
    "guard_replan",
    "save_finance_execution",
    "scenario_result",
    "source_owned_arguments",
    "validate_finance_scenario_output",
]
