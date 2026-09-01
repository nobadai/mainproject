"""Finance Agent 실행이력 조회 응답 계약."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.finance.contracts.vocabulary import FinalVerdict, FinanceCycle, RuntimeStatus


class FinanceAgentRunResponse(BaseModel):
    """UI 조회용 Finance Agent 실행이력 응답."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    cycle: FinanceCycle
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime
