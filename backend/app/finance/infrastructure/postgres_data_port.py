"""`FinanceAsOfDataPort` 의 현재 Schema 구현."""

from datetime import date
from decimal import Decimal

from app.finance.infrastructure.finance_state_repository import (
    FINANCE_POLICY_VERSION,
    _fetch_scheduled_rows,
    _get_current_finance_state_row,
    _rows_to_events,
    get_active_finance_debt_policy,
    get_active_finance_policy,
)
from app.finance.ports.finance_data import FinanceDataNotReady
from app.finance.schemas import CashEvent, FinancePolicy
from app.finance.tools import build_debt_service_schedule


class PostgresFinanceAsOfDataPort:
    """명시적인 재현성 보호 장치를 둔 현재 Schema용 Adapter.

    현재 DB에는 완전한 이중 시간 상태 저장소가 아니라 현재 상태 View만 있다.
    따라서 View의 state_date가 as_of와 정확히 일치할 때만 안전하다. 이전 요청은
    오늘 상태를 읽지 않고 준비되지 않은 것으로 보고한다.
    """

    def __init__(self) -> None:
        self._position_cache: tuple[date, dict[str, object]] | None = None
        self._policy_cache: tuple[date, str, FinancePolicy] | None = None

    def load_finance_position(self, as_of: date) -> dict[str, object]:
        if self._position_cache is not None and self._position_cache[0] == as_of:
            return self._position_cache[1]
        row = _get_current_finance_state_row()
        if row.get("state_date") != as_of:
            raise FinanceDataNotReady("historical_finance_position")
        self._position_cache = (as_of, row)
        return row

    def load_policy(self, as_of: date, policy_version: str) -> FinancePolicy:
        if self._policy_cache is not None and self._policy_cache[:2] == (
            as_of,
            policy_version,
        ):
            return self._policy_cache[2]
        if policy_version != FINANCE_POLICY_VERSION:
            raise FinanceDataNotReady("finance_policy_version")
        try:
            policy = get_active_finance_policy()
        except (LookupError, TypeError, ValueError) as exc:
            raise FinanceDataNotReady("finance_policy") from exc
        self._policy_cache = (as_of, policy_version, policy)
        return policy

    def load_obligations(self, as_of: date, horizon: date) -> list[CashEvent]:
        position = self.load_finance_position(as_of)
        payable_rows = _fetch_scheduled_rows(
            table="payables",
            columns=("payable_id", "due_date", "outstanding_amount_krw"),
            sim_run_id=str(position["sim_run_id"]),
            as_of=as_of,
            horizon_end=horizon,
            status_column="status",
            active_status="OPEN",
        )
        expense_rows = _fetch_scheduled_rows(
            table="expenses",
            columns=("expense_id", "expense_date", "amount_krw"),
            sim_run_id=str(position["sim_run_id"]),
            as_of=as_of,
            horizon_end=horizon,
            status_column="status",
            excluded_status="PAID",
        )
        return [
            *_rows_to_events(
                payable_rows,
                id_column="payable_id",
                date_column="due_date",
                amount_column="outstanding_amount_krw",
                event_type="PURCHASE_PAYABLE",
                direction="OUTFLOW",
            ),
            *_rows_to_events(
                expense_rows,
                id_column="expense_id",
                date_column="expense_date",
                amount_column="amount_krw",
                event_type="COMMITTED_OUTFLOW",
                direction="OUTFLOW",
            ),
        ]

    def load_receivables(self, as_of: date, horizon: date) -> list[CashEvent]:
        position = self.load_finance_position(as_of)
        rows = _fetch_scheduled_rows(
            table="receivables",
            columns=("receivable_id", "due_date", "outstanding_amount_krw"),
            sim_run_id=str(position["sim_run_id"]),
            as_of=as_of,
            horizon_end=horizon,
            status_column="status",
            active_status="OPEN",
        )
        return _rows_to_events(
            rows,
            id_column="receivable_id",
            date_column="due_date",
            amount_column="outstanding_amount_krw",
            event_type="RECEIVABLE",
            direction="INFLOW",
        )

    def load_payroll(self, as_of: date, horizon: date) -> Decimal | None:
        del horizon
        if self._policy_cache is None or self._policy_cache[0] != as_of:
            raise FinanceDataNotReady("finance_policy_context")
        policy = self._policy_cache[2]
        return policy.monthly_labor_cost_krw

    def load_debt_schedule(self, as_of: date, horizon: date) -> list[CashEvent]:
        position = self.load_finance_position(as_of)
        try:
            debt = get_active_finance_debt_policy()
        except (LookupError, TypeError, ValueError) as exc:
            raise FinanceDataNotReady("debt_policy") from exc
        if abs(
            debt.debt_principal_krw - Decimal(str(position["current_debt_krw"]))
        ) > Decimal("0.000001"):
            raise FinanceDataNotReady("debt_policy_consistency")
        return list(build_debt_service_schedule(debt_policy=debt, as_of=as_of, horizon_end=horizon))
