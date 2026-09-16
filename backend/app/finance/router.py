"""재무·자금 Agent API 라우터."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, status
from psycopg import sql
from pydantic import BaseModel, Field, StringConstraints

from app.finance import user_messages as messages
from app.finance.adapter import finance_port
from app.finance.cash_adjustments import (
    CashAdjustmentConflict,
    record_cash_adjustment,
)
from app.finance.collection import FinanceCollectionConflict, apply_collection_event
from app.finance.db import FinanceDataNotReady, get_connection, get_db_schema
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
    source_ref: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
    ]
    recorded_by: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    note: str | None = Field(default=None, max_length=1000)


class CreditLimitHistoryItem(BaseModel):
    """거래처 여신한도 원장의 기간 이력 한 건."""

    partner_credit_limit_id: str
    partner_id: str
    credit_limit_krw: Decimal
    effective_from: date
    effective_to: date | None
    evidence_grade: str
    source_ref: str
    recorded_by: str
    policy_version: str
    usage_scope: str
    note: str | None
    is_active: bool
    is_current: bool


@router.get("/credit-limits", response_model=list[CreditLimitHistoryItem])
def get_credit_limits(
    partner_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> list[CreditLimitHistoryItem]:
    """한 거래처의 여신한도 이력을 최신 적용일부터 반환한다."""
    schema = sql.Identifier(get_db_schema())
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT 1 FROM {}.partners WHERE partner_id = %s").format(schema),
            [partner_id],
        )
        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="거래처를 찾지 못했습니다.",
            )
        cursor.execute(
            sql.SQL("""
                SELECT partner_credit_limit_id, partner_id, credit_limit_krw,
                       effective_from, effective_to, evidence_grade, source_ref,
                       recorded_by, policy_version, usage_scope, note, is_active,
                       (is_active AND effective_from <= %s
                        AND (effective_to IS NULL OR effective_to >= %s)) AS is_current
                FROM {}.partner_credit_limits
                WHERE partner_id = %s
                ORDER BY effective_from DESC, partner_credit_limit_id DESC
            """).format(schema),
            [as_of, as_of, partner_id],
        )
        return [CreditLimitHistoryItem.model_validate(row) for row in cursor.fetchall()]


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
                row
                for row in rows
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
                        evidence_grade, source_ref, recorded_by, policy_version, usage_scope, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """).format(schema),
                [
                    credit_id,
                    change.partner_id,
                    change.credit_limit_krw,
                    change.effective_from,
                    change.evidence_grade,
                    change.source_ref,
                    change.recorded_by,
                    "manual-v1",
                    "USER_RECORDED",
                    change.note,
                ],
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


class ReceivableCollectionChange(BaseModel):
    """사용자가 확인한 실제 수금. 금액은 이번 수금분이며 누적 target은 서버가 계산한다."""

    sim_run_id: str = Field(min_length=1)
    financing_mode: str = Field(min_length=1)
    collection_date: date
    receivable_id: str = Field(min_length=1)
    collect_all: bool = False
    amount_krw: Decimal | None = Field(default=None, gt=0)
    source_ref: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
    ]
    recorded_by: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    note: str | None = Field(default=None, max_length=1000)


class CashAdjustmentChange(BaseModel):
    sim_run_id: str = Field(min_length=1)
    financing_mode: str = Field(min_length=1)
    adjustment_date: date
    direction: Literal["INFLOW", "OUTFLOW"]
    category: Literal["OWNER_INJECTION", "OWNER_WITHDRAWAL", "OTHER"]
    amount_krw: Decimal = Field(gt=0)
    source_ref: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
    ]
    recorded_by: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    note: str | None = Field(default=None, max_length=1000)


@router.post("/receivables/collections", status_code=status.HTTP_201_CREATED)
def record_receivable_collection(change: ReceivableCollectionChange) -> dict[str, object]:
    """한 채권의 실제 전액/부분 수금을 누적 전이로 기록한다."""
    if not change.collect_all and change.amount_krw is None:
        raise HTTPException(status_code=422, detail="부분 수금액을 입력해 주세요.")
    schema = sql.Identifier(get_db_schema())
    try:
        with get_connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("""SELECT original_amount_krw, received_amount_krw FROM {}.receivables
                           WHERE receivable_id = %s AND sim_run_id = %s FOR UPDATE""").format(
                    schema
                ),
                [change.receivable_id, change.sim_run_id],
            )
            row = cursor.fetchone()
            if row is None:
                raise LookupError("받을 돈을 찾지 못했습니다.")
            original = Decimal(str(row["original_amount_krw"]))
            received = Decimal(str(row["received_amount_krw"]))
            target = original if change.collect_all else received + change.amount_krw
            if target > original:
                raise ValueError("받는 금액이 남은 받을 돈보다 큽니다.")
            event_note = f"사용자 수금 · 입력자 {change.recorded_by} · 근거 {change.source_ref}"
            if change.note:
                event_note = f"{event_note} · {change.note}"
            cursor.execute(
                sql.SQL("""INSERT INTO {}.master_collection_events
                           (sim_run_id, financing_mode, collection_date, receivable_id,
                            target_received_total_krw, note)
                           VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""").format(
                    schema
                ),
                [
                    change.sim_run_id,
                    change.financing_mode,
                    change.collection_date,
                    change.receivable_id,
                    target,
                    event_note,
                ],
            )
            if cursor.rowcount != 1:
                raise ValueError("같은 기준일에 이미 수금이 기록되어 있습니다.")
            plan = apply_collection_event(
                conn,
                sim_run_id=change.sim_run_id,
                financing_mode=change.financing_mode,
                collection_date=change.collection_date,
                receivable_id=change.receivable_id,
                target_received_total_krw=target,
            )
        return {
            "receivable_id": plan.receivable_id,
            "received_delta_krw": plan.delta_received_krw,
            "outstanding_amount_krw": plan.next_outstanding_amount_krw,
            "status": plan.next_status,
        }
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, FinanceCollectionConflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FinanceDataNotReady as error:
        raise HTTPException(
            status_code=409, detail="해당 기준일의 재무 상태가 준비되지 않았습니다."
        ) from error


@router.post("/cash-adjustments", status_code=status.HTTP_201_CREATED)
def create_cash_adjustment(change: CashAdjustmentChange) -> dict[str, object]:
    """사용자 자금 입금·출금을 근거와 함께 기록한다."""
    try:
        with get_connection() as conn:
            result = record_cash_adjustment(conn, **change.model_dump())
        return {
            "cash_adjustment_id": result.cash_adjustment_id,
            "current_cash_krw": result.current_cash_krw,
        }
    except CashAdjustmentConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FinanceDataNotReady as error:
        raise HTTPException(
            status_code=409, detail="해당 기준일의 재무 상태가 준비되지 않았습니다."
        ) from error


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
