"""재무·자금 Agent API 라우터."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, status
from psycopg import sql
from pydantic import BaseModel, Field

from app.finance import user_messages as messages
from app.finance.adapter import finance_port
from app.finance.db import get_connection, get_db_schema
from app.finance.execution import get_finance_execution, get_finance_run, list_finance_runs
from app.finance.schemas import (
    FinalVerdict,
    FinanceAgentRunResponse,
    FinanceCycle,
    RuntimeStatus,
)
from app.master.envelope import AgentReply, AgentRequest

router = APIRouter(prefix="/finance", tags=["finance"])


class CreditLimitChange(BaseModel):
    """사용자가 등록하는 거래처 여신한도 이력 한 건."""

    partner_id: str = Field(min_length=1)
    credit_limit_krw: Decimal = Field(ge=0)
    effective_from: date
    evidence_grade: str = Field(pattern="^(OFFICIAL|VENDOR|SIM_FIXED)$")
    recorded_by: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


@router.post("/credit-limits", status_code=status.HTTP_201_CREATED)
def register_credit_limit(change: CreditLimitChange) -> dict[str, object]:
    """열린 한도 기간을 끝내고 새 기간을 추가한다; 과거 금액은 덮어쓰지 않는다."""
    schema = sql.Identifier(get_db_schema())
    try:
        with get_connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("""
                    SELECT 1 FROM {}.partners WHERE partner_id = %s
                """).format(schema),
                [change.partner_id],
            )
            if cursor.fetchone() is None:
                raise LookupError("거래처를 찾지 못했습니다.")
            cursor.execute(
                sql.SQL("""
                    SELECT partner_credit_limit_id, effective_from, effective_to
                    FROM {}.partner_credit_limits
                    WHERE partner_id = %s AND is_active
                    ORDER BY effective_from
                    FOR UPDATE
                """).format(schema),
                [change.partner_id],
            )
            rows = cursor.fetchall()
            future_or_overlap = [
                row for row in rows
                if row["effective_from"] >= change.effective_from
                or row["effective_to"] is None
                or row["effective_to"] >= change.effective_from
            ]
            if len(future_or_overlap) > 1:
                raise ValueError("여신한도 기간이 겹치거나 미래 이력이 있어 변경할 수 없습니다.")
            if future_or_overlap:
                current = future_or_overlap[0]
                if current["effective_from"] >= change.effective_from:
                    raise ValueError("새 적용일은 현재 한도 적용일보다 뒤여야 합니다.")
                cursor.execute(
                    sql.SQL("""
                        UPDATE {}.partner_credit_limits
                        SET effective_to = %s, updated_at = now()
                        WHERE partner_credit_limit_id = %s
                    """).format(schema),
                    [change.effective_from - timedelta(days=1), current["partner_credit_limit_id"]],
                )
            credit_id = f"CREDIT-{uuid4()}"
            cursor.execute(
                sql.SQL("""
                    INSERT INTO {}.partner_credit_limits (
                        partner_credit_limit_id, partner_id, credit_limit_krw, effective_from,
                        evidence_grade, source_ref, policy_version, usage_scope, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """).format(schema),
                [credit_id, change.partner_id, change.credit_limit_krw, change.effective_from,
                 change.evidence_grade, f"manual:{change.recorded_by}", "manual-v1",
                 "USER_RECORDED", change.note],
            )
        return {
            "partner_credit_limit_id": credit_id,
            "partner_id": change.partner_id,
            "effective_from": change.effective_from,
            "credit_limit_krw": change.credit_limit_krw,
        }
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post("/agent", summary="Finance v2.2 Tool-Using Agent")
def run_finance_agent(request: AgentRequest) -> AgentReply:
    """Master와 동일한 Finance Port를 통해 Agent를 실행한다."""
    reply, _metadata = finance_port(request)
    return reply


@router.get("/agent/runs/{run_id}", summary="Finance v2.2 execution metadata")
def get_finance_execution_by_id(run_id: UUID) -> dict[str, object]:
    try:
        return get_finance_execution(run_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=messages.RUN_NOT_FOUND) from error


@router.get(
    "/runs",
    response_model=list[FinanceAgentRunResponse],
    summary="Finance Agent 실행이력 목록 조회",
)
def get_finance_runs(
    cycle: FinanceCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[FinanceAgentRunResponse]:
    """cycle, as_of, runtime_status 필터로 최근 실행이력을 반환한다."""
    return list_finance_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )


@router.get(
    "/runs/{run_id}",
    response_model=FinanceAgentRunResponse,
    summary="Finance Agent 실행이력 단건 조회",
)
def get_finance_run_by_id(run_id: UUID) -> FinanceAgentRunResponse:
    """run_id에 해당하는 실행이력을 반환한다."""
    try:
        return get_finance_run(run_id)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=messages.RUN_NOT_FOUND,
        ) from error
