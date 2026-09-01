"""Finance Service 표면.

여기 남은 **구현**은 Agent 실행이력 조회뿐이다 — UI 가 `/finance/runs` 로 읽는다.
Agent 이전의 결정론 Finance A/B 실행은 `app.finance.legacy.deterministic_service`
가 가지며, 기존 임포트 경로 유지를 위해 여기서 다시 내보낸다.
"""

from datetime import date
from uuid import UUID

from app.finance.legacy.deterministic_service import (
    run_finance_procurement,
    run_finance_procurement_with_context,
    run_finance_procurement_with_snapshot,
    run_finance_sales,
    run_finance_sales_with_context,
    run_finance_sales_with_snapshot,
)
from app.finance.run_repository import get_finance_agent_run, list_finance_agent_runs
from app.finance.schemas import (
    FinalVerdict,
    FinanceAgentRunResponse,
    FinanceCycle,
    RuntimeStatus,
)

__all__ = [
    "get_finance_run",
    "list_finance_runs",
    "run_finance_procurement",
    "run_finance_procurement_with_context",
    "run_finance_procurement_with_snapshot",
    "run_finance_sales",
    "run_finance_sales_with_context",
    "run_finance_sales_with_snapshot",
]


def get_finance_run(run_id: UUID) -> FinanceAgentRunResponse:
    """UI 조회용 Finance Agent 실행이력 한 건을 반환한다."""
    return FinanceAgentRunResponse.model_validate(get_finance_agent_run(run_id))


def list_finance_runs(
    *,
    cycle: FinanceCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: int = 100,
) -> list[FinanceAgentRunResponse]:
    """UI 조회용 Finance Agent 실행이력 목록을 반환한다."""
    rows = list_finance_agent_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )
    return [FinanceAgentRunResponse.model_validate(row) for row in rows]
