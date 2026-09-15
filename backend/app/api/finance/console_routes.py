"""Finance operations-console reads.  Every query carries its runtime axis."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.finance.aging import AgingBucket
from app.finance.console_credit import ConsoleCreditResponse, get_console_credit
from app.finance.console_expenses import ConsoleExpensesResponse, get_console_expenses
from app.finance.console_payables import ConsolePayablesResponse, get_console_payables
from app.finance.console_receivables import ConsoleReceivablesResponse, get_console_receivables
from app.finance.console_runs import (
    ConsoleFinanceRun,
    ConsoleFinanceRunsResponse,
    get_console_finance_latest_run,
    get_console_finance_runs,
)
from app.finance.dashboard import get_finance_cashflow, get_finance_dashboard
from app.finance.schemas import FinanceCashflowResponse, FinanceDashboardResponse

router = APIRouter(prefix="/console/finance", tags=["console:finance"])


@router.get("/summary", response_model=FinanceDashboardResponse)
def summary(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> FinanceDashboardResponse:
    """Stored finance facts only; no Burn-in default and no projection recomputation."""
    return get_finance_dashboard(sim_run_id=sim_run_id, as_of=as_of)


#: 자금 흐름 한 번에 볼 수 있는 최대 일수.
#:
#: ★ **30 에서 넓혔다** (2026-09-14). 실행이 한 분기(71일)를 도는데 상한이 30 이라
#:   화면이 «전체 기간» 을 보여 줄 방법이 없었다. 실측에서 최근 30일만 보면 잔액
#:   변동폭이 9,041,102 원으로 보이는데 전 기간은 20,832,701 원이다 — 같은 실행을
#:   보고도 변동이 절반 이하로 읽힌다.
#:
#: 🔴 **읽기만 넓혔다.** `load_cashflow` 는 `LIMIT` 하나로 도는 SELECT 라 계약도
#:    계산도 바뀌지 않는다. 화면이 기간을 늘려 숫자를 새로 만드는 것이 아니다.
MAX_CASHFLOW_DAYS = 400


@router.get("/cashflow", response_model=FinanceCashflowResponse)
def cashflow(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    days: Annotated[int, Query(ge=1, le=MAX_CASHFLOW_DAYS)] = 30,
) -> FinanceCashflowResponse:
    return get_finance_cashflow(sim_run_id=sim_run_id, as_of=as_of, days=days)


@router.get("/credit", response_model=ConsoleCreditResponse)
def credit(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> ConsoleCreditResponse:
    """거래처 여신 현황. 한도·미수·가용여신·수금 예정을 **기준일 시점으로** 읽는다."""
    return get_console_credit(sim_run_id=sim_run_id, as_of=as_of)


@router.get("/receivables", response_model=ConsoleReceivablesResponse)
def receivables(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    aging_bucket: AgingBucket | None = None,
    partner_id: str | None = None,
) -> ConsoleReceivablesResponse:
    return get_console_receivables(
        sim_run_id=sim_run_id, as_of=as_of, aging_bucket=aging_bucket, partner_id=partner_id
    )


@router.get("/payables", response_model=ConsolePayablesResponse)
def payables(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    status: str | None = None,
    due_within_days: Annotated[int | None, Query(ge=0)] = None,
) -> ConsolePayablesResponse:
    """What this run still owes, aged against `as_of` rather than today's clock."""
    return get_console_payables(
        sim_run_id=sim_run_id, as_of=as_of, status=status, due_within_days=due_within_days
    )


@router.get("/expenses", response_model=ConsoleExpensesResponse)
def expenses(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    from_date: date | None = None,
    to_date: date | None = None,
    category: str | None = None,
) -> ConsoleExpensesResponse:
    """Stored expense rows; the ledger's own category is the one that groups them."""
    return get_console_expenses(
        sim_run_id=sim_run_id,
        as_of=as_of,
        from_date=from_date,
        to_date=to_date,
        category=category,
    )


@router.get("/runs", response_model=ConsoleFinanceRunsResponse)
def runs(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    runtime_status: str | None = None,
    verdict: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConsoleFinanceRunsResponse:
    """Finance execution history for one simulation run.  Reads only."""
    return get_console_finance_runs(
        sim_run_id=sim_run_id,
        as_of=as_of,
        from_date=from_date,
        to_date=to_date,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )


@router.get("/runs/latest", response_model=ConsoleFinanceRun | None)
def latest_run(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date | None = None,
) -> ConsoleFinanceRun | None:
    """The newest stored Finance run inside this run, or null.  Never global."""
    return get_console_finance_latest_run(sim_run_id=sim_run_id, as_of=as_of)
