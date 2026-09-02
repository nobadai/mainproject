"""Finance 영속 계층 — 연결 · 경계 계약 · 조회 구현.

이 파일이 소유하는 것
    PostgreSQL 연결/조회 헬퍼 · `FinanceAsOfDataPort` 경계 계약 ·
    `FinanceDataNotReady` · Finance State/Policy/부채 조회 · as-of DataPort 구현

여기 **없는 것**
    금액 공식 · 판정 · 실행 통제 · 사람이 읽는 문장

★ 이 경로(`app.finance.db`)는 **재무 밖 도메인(master · orchestrator)이 이미 import
  한다.** 그래서 옮길 수 없고, 옮길 수 없으므로 여기가 정본이다 — 같은 일을 하는
  모듈을 옆에 새로 만들면 어느 쪽이 진짜인지 알 수 없게 된다.

★ **경계는 폴더가 아니라 규율이다.** 아래 `FinanceAsOfDataPort` 절은 구현을 알지 못한다
  — 계약이 먼저 오고 구현이 뒤에 온다는 순서가 그 규율을 눈으로 확인시킨다.

★ as-of 재현성 보호는 그대로다. 과거 시점을 오늘 상태로 대신 답하지 않는다.
"""

import os
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

from app.finance.schemas import (
    CashEvent,
    FinanceDebtPolicy,
    FinancePolicy,
    FinanceRuntimeContext,
    FinanceSnapshot,
)
from app.finance.tools import build_debt_service_schedule

# ---------------------------------------------------------------------------
# PostgreSQL 연결과 조회 헬퍼 (재무 밖 도메인도 쓴다)
# ---------------------------------------------------------------------------

Query = str | sql.Composed
Params = Sequence[object] | Mapping[str, object] | None

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
_CONNECTION_ENV_KEYS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _load_environment() -> None:
    load_dotenv(_ENV_FILE)


def _required_environment(keys: tuple[str, ...]) -> dict[str, str]:
    _load_environment()
    values = {key: os.getenv(key, "") for key in keys}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required database environment variables: {', '.join(missing)}")
    return values


def get_db_schema() -> str:
    """설정된 PostgreSQL Schema 이름을 반환한다."""
    return _required_environment(("DB_SCHEMA",))["DB_SCHEMA"]


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    """환경변수 설정으로 새 PostgreSQL Connection을 생성한다."""
    config = _required_environment(_CONNECTION_ENV_KEYS)
    return psycopg.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        row_factory=dict_row,
    )


def fetch_one(query: Query, params: Params = None) -> dict[str, Any] | None:
    """Parameter binding을 사용해 단건 SELECT 결과를 반환한다."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()


def fetch_all(query: Query, params: Params = None) -> list[dict[str, Any]]:
    """Parameter binding을 사용해 다건 SELECT 결과를 반환한다."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def execute_returning_one(query: Query, params: Params = None) -> dict[str, Any]:
    """변경 SQL을 실행하고 RETURNING으로 생성된 단건 결과를 반환한다."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Database write did not return a row")
        return row


# ---------------------------------------------------------------------------
# 데이터 경계 계약 — 구현을 import 하지 않는다
# ---------------------------------------------------------------------------

class FinanceDataNotReady(RuntimeError):
    """필수 Finance 사실/Policy가 없거나 과거 시점으로 재현할 수 없다."""

    def __init__(self, key: str):
        # `key` 는 `missing_data` 식별자다 — 기계가 읽으므로 번역하지 않는다.
        # 문장만 사람이 읽는 설명이다 (Controller 경로에서 `reasoning` 이 된다).
        self.key = key
        super().__init__(f"재무 데이터가 준비되지 않았습니다: {key}")


class FinanceAsOfDataPort(Protocol):
    """v2.2 Repository 경계. 모든 변경 가능 읽기에는 ``as_of``를 전달한다."""

    def load_finance_position(self, as_of: date) -> dict[str, object]: ...
    def load_obligations(self, as_of: date, horizon: date) -> list[CashEvent]: ...
    def load_receivables(self, as_of: date, horizon: date) -> list[CashEvent]: ...
    def load_payroll(self, as_of: date, horizon: date) -> Decimal | None: ...
    def load_policy(self, as_of: date, policy_version: str) -> FinancePolicy: ...
    def load_debt_schedule(self, as_of: date, horizon: date) -> list[CashEvent]: ...


# ---------------------------------------------------------------------------
# Finance State · Policy · 부채 조회
# ---------------------------------------------------------------------------

FINANCE_POLICY_VERSION = "v1.3-PROVISIONAL"
FINANCE_POLICY_USAGE_SCOPE = "AGENT_MVP_DEMO"
_NUMERIC_POLICY_KEYS = {
    "purchase_payment_days",
    "payroll_date",
    "margin_defense_floor_rate",
    "monthly_labor_cost_krw",
    "minimum_cash_balance_krw",
    "cashflow_projection_days",
    "cash_priority_high_ratio",
    "cash_priority_medium_ratio",
}
_TEXT_POLICY_KEYS = {"cash_priority_reference"}
_OPTIONAL_POLICY_KEYS = {
    "purchase_payment_days",
    "margin_defense_floor_rate",
    "monthly_labor_cost_krw",
}
_REQUIRED_POLICY_KEYS = (_NUMERIC_POLICY_KEYS | _TEXT_POLICY_KEYS) - _OPTIONAL_POLICY_KEYS
_KNOWN_POLICY_KEYS = _REQUIRED_POLICY_KEYS | _OPTIONAL_POLICY_KEYS
_DEBT_NUMERIC_POLICY_KEYS = {
    "debt_principal_krw",
    "debt_annual_rate",
    "debt_term_months",
    "debt_grace_months",
}
_DEBT_TEXT_POLICY_KEYS = {
    "debt_runtime_status",
    "debt_execution_date",
    "debt_grace_payment_mode",
    "debt_repayment_method",
    "debt_payment_frequency",
    "debt_payment_day_rule",
    "debt_first_payment_rule",
    "debt_interest_method",
}
_REQUIRED_DEBT_POLICY_KEYS = _DEBT_NUMERIC_POLICY_KEYS | _DEBT_TEXT_POLICY_KEYS


class FinanceState(TypedDict):
    finance_state_id: str
    sim_run_id: str
    state_date: date
    state_type: str
    financing_mode: str
    current_cash_krw: Decimal
    minimum_operating_cash_krw: Decimal
    committed_outflows_krw: Decimal
    unsettled_purchase_payables_krw: Decimal
    financial_limit_krw: Decimal


def get_current_finance_state() -> FinanceState:
    """DB View가 지정한 현재 Finance State 한 건을 조회한다."""
    return cast(FinanceState, _get_current_finance_state_row())


def get_current_finance_snapshot() -> FinanceSnapshot:
    """현재 View의 Finance State를 T0 ID 미확정 Snapshot으로 변환한다."""
    return FinanceSnapshot(snapshot_id=None, **_get_current_finance_state_row())


def get_current_finance_runtime_context() -> FinanceRuntimeContext:
    """Snapshot, Policy, 확정 일정을 DB 경계에서 한 번 고정한다."""
    snapshot = get_current_finance_snapshot()
    policy = get_active_finance_policy()
    horizon_end = snapshot.state_date + timedelta(days=policy.cashflow_projection_days)
    events: list[CashEvent] = []
    unresolved: list[str] = []

    payable_rows = _fetch_scheduled_rows(
        table="payables",
        columns=("payable_id", "due_date", "outstanding_amount_krw"),
        sim_run_id=snapshot.sim_run_id,
        as_of=snapshot.state_date,
        horizon_end=horizon_end,
        status_column="status",
        active_status="OPEN",
    )
    events.extend(
        _rows_to_events(
            payable_rows,
            id_column="payable_id",
            date_column="due_date",
            amount_column="outstanding_amount_krw",
            event_type="PURCHASE_PAYABLE",
            direction="OUTFLOW",
        )
    )
    if snapshot.unsettled_purchase_payables_krw != 0 and not payable_rows:
        unresolved.append("PURCHASE_PAYABLE")

    expense_rows = _fetch_scheduled_rows(
        table="expenses",
        columns=("expense_id", "expense_date", "amount_krw"),
        sim_run_id=snapshot.sim_run_id,
        as_of=snapshot.state_date,
        horizon_end=horizon_end,
        status_column="status",
        excluded_status="PAID",
    )
    events.extend(
        _rows_to_events(
            expense_rows,
            id_column="expense_id",
            date_column="expense_date",
            amount_column="amount_krw",
            event_type="COMMITTED_OUTFLOW",
            direction="OUTFLOW",
        )
    )
    if snapshot.committed_outflows_krw != 0 and not expense_rows:
        unresolved.append("COMMITTED_OUTFLOW")

    receivable_rows = _fetch_scheduled_rows(
        table="receivables",
        columns=("receivable_id", "due_date", "outstanding_amount_krw"),
        sim_run_id=snapshot.sim_run_id,
        as_of=snapshot.state_date,
        horizon_end=horizon_end,
        status_column="status",
        active_status="OPEN",
    )
    events.extend(
        _rows_to_events(
            receivable_rows,
            id_column="receivable_id",
            date_column="due_date",
            amount_column="outstanding_amount_krw",
            event_type="RECEIVABLE",
            direction="INFLOW",
        )
    )
    if snapshot.receivables_krw != 0 and not receivable_rows:
        unresolved.append("RECEIVABLE")

    # 🔴 **부채가 없으면 부채 정책을 요구하지 않는다.**
    #
    #    예전에는 `current_debt_krw` 와 무관하게 부채 정책을 읽고, 행이 없으면
    #    `DEBT_SERVICE` 를 unresolved 로 올렸다. 그러면 **빚이 없는 회사가 "부채 원천을
    #    확인하지 못했다"** 고 말하게 된다 — 확인할 부채가 애초에 없는데도.
    #    그 unresolved 는 아래로 흘러 *"재무가 뭔가 못 읽었다"* 로 읽히고, 실제로는
    #    아무 문제가 없다. 없는 의무를 증명하라고 요구한 셈이다.
    #
    # ★ 부채가 있으면 규율은 그대로다 — 정책이 없거나 원금이 상태와 어긋나면
    #   fail-closed 다. 부채 상환은 현금흐름에서 **가장 확실한 유출**이라, 그것을
    #   빠뜨린 투영은 틀린 게 아니라 낙관적으로 틀린다.
    debt_policy = None
    if snapshot.current_debt_krw > 0:
        try:
            debt_policy = get_active_finance_debt_policy()
        except (LookupError, TypeError, ValueError):
            unresolved.append("DEBT_SERVICE")
        if debt_policy is not None:
            if abs(
                debt_policy.debt_principal_krw - snapshot.current_debt_krw
            ) > Decimal("0.000001"):
                unresolved.append("DEBT_SERVICE")
                debt_policy = None
            else:
                events.extend(
                    build_debt_service_schedule(
                        debt_policy=debt_policy,
                        as_of=snapshot.state_date,
                        horizon_end=horizon_end,
                    )
                )

    return FinanceRuntimeContext(
        snapshot=snapshot,
        policy=policy,
        debt_policy=debt_policy,
        cash_events=tuple(events),
        unresolved_sources=tuple(unresolved),
    )


def get_active_finance_policy() -> FinancePolicy:
    """현재 Finance MVP 범위의 active policy를 typed contract로 조회한다."""
    query = sql.SQL(
        """
        SELECT
            policy_key,
            value_kind,
            value_numeric,
            value_text,
            value_json,
            source_ref,
            policy_version,
            usage_scope
        FROM {}.agent_policy_config
        WHERE domain = %s
          AND policy_version = %s
          AND usage_scope = %s
          AND is_active = TRUE
        """
    ).format(sql.Identifier(get_db_schema()))
    rows = fetch_all(
        query,
        ["finance", FINANCE_POLICY_VERSION, FINANCE_POLICY_USAGE_SCOPE],
    )
    return _build_finance_policy(rows)


def get_active_finance_debt_policy() -> FinanceDebtPolicy:
    """현재 Finance MVP 범위의 SIM_FIXED debt contract를 조회한다."""
    return _build_finance_debt_policy(_fetch_active_finance_policy_rows())


def _fetch_active_finance_policy_rows() -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT
            policy_key,
            value_kind,
            value_numeric,
            value_text,
            value_json,
            evidence_grade,
            source_ref,
            policy_version,
            usage_scope
        FROM {}.agent_policy_config
        WHERE domain = %s
          AND policy_version = %s
          AND usage_scope = %s
          AND is_active = TRUE
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(
        query,
        ["finance", FINANCE_POLICY_VERSION, FINANCE_POLICY_USAGE_SCOPE],
    )


def _build_finance_policy(rows: list[dict[str, object]]) -> FinancePolicy:
    values: dict[str, object] = {}
    source_refs: dict[str, str] = {}

    for row in rows:
        key = row.get("policy_key")
        if key not in _KNOWN_POLICY_KEYS:
            continue
        if key in values:
            raise ValueError(f"Duplicate Finance policy key: {key}")
        if row.get("policy_version") != FINANCE_POLICY_VERSION:
            raise ValueError(f"Finance policy_version mismatch: {key}")
        if row.get("usage_scope") != FINANCE_POLICY_USAGE_SCOPE:
            raise ValueError(f"Finance policy usage_scope mismatch: {key}")

        kind = row.get("value_kind")
        expected_kind = "NUMERIC" if key in _NUMERIC_POLICY_KEYS else "TEXT"
        if kind != expected_kind:
            raise ValueError(f"Invalid value_kind for Finance policy {key}: {kind}")
        selected_column = "value_numeric" if kind == "NUMERIC" else "value_text"
        unused_columns = {"value_numeric", "value_text", "value_json"} - {selected_column}
        value = row.get(selected_column)
        if value is None and key in _OPTIONAL_POLICY_KEYS:
            if any(row.get(column) is not None for column in unused_columns):
                raise ValueError(f"Inconsistent value columns for Finance policy: {key}")
            values[key] = None
            source_ref = row.get("source_ref")
            if isinstance(source_ref, str) and source_ref:
                source_refs[key] = source_ref
            continue
        if value is None or any(row.get(column) is not None for column in unused_columns):
            raise ValueError(f"Inconsistent value columns for Finance policy: {key}")
        if kind == "NUMERIC" and (isinstance(value, bool) or not isinstance(value, Decimal)):
            raise TypeError(f"Invalid Python NUMERIC value for Finance policy: {key}")
        if kind == "TEXT" and not isinstance(value, str):
            raise TypeError(f"Invalid Python TEXT value for Finance policy: {key}")

        source_ref = row.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise ValueError(f"Missing source_ref for Finance policy: {key}")
        values[key] = value
        source_refs[key] = source_ref

    missing = _REQUIRED_POLICY_KEYS - values.keys()
    if missing:
        raise LookupError(f"Required Finance policies were not found: {', '.join(sorted(missing))}")

    values.setdefault("purchase_payment_days", None)
    values.setdefault("margin_defense_floor_rate", None)
    values.setdefault("monthly_labor_cost_krw", None)
    for key in ("purchase_payment_days", "payroll_date", "cashflow_projection_days"):
        numeric = values[key]
        if numeric is None:
            continue
        assert isinstance(numeric, Decimal)
        if numeric != numeric.to_integral_value():
            raise ValueError(f"Finance policy must be an integer: {key}")
        values[key] = int(numeric)

    return FinancePolicy(
        **values,
        policy_version=FINANCE_POLICY_VERSION,
        usage_scope=FINANCE_POLICY_USAGE_SCOPE,
        source_refs=source_refs,
    )


def _build_finance_debt_policy(rows: list[dict[str, object]]) -> FinanceDebtPolicy:
    values: dict[str, object] = {}
    source_refs: dict[str, str] = {}
    for row in rows:
        key = row.get("policy_key")
        if key not in _REQUIRED_DEBT_POLICY_KEYS:
            continue
        if key in values:
            raise ValueError(f"Duplicate Finance debt policy key: {key}")
        if row.get("policy_version") != FINANCE_POLICY_VERSION:
            raise ValueError(f"Finance debt policy_version mismatch: {key}")
        if row.get("usage_scope") != FINANCE_POLICY_USAGE_SCOPE:
            raise ValueError(f"Finance debt policy usage_scope mismatch: {key}")
        if row.get("evidence_grade") != "SIM_FIXED":
            raise ValueError(f"Finance debt policy must be SIM_FIXED: {key}")

        kind = row.get("value_kind")
        expected_kind = "NUMERIC" if key in _DEBT_NUMERIC_POLICY_KEYS else "TEXT"
        if kind != expected_kind:
            raise ValueError(f"Invalid value_kind for Finance debt policy {key}: {kind}")
        selected_column = "value_numeric" if kind == "NUMERIC" else "value_text"
        unused_columns = {"value_numeric", "value_text", "value_json"} - {selected_column}
        value = row.get(selected_column)
        if value is None or any(row.get(column) is not None for column in unused_columns):
            raise ValueError(f"Inconsistent value columns for Finance debt policy: {key}")
        if kind == "NUMERIC" and (isinstance(value, bool) or not isinstance(value, Decimal)):
            raise TypeError(f"Invalid Python NUMERIC value for Finance debt policy: {key}")
        if kind == "TEXT" and not isinstance(value, str):
            raise TypeError(f"Invalid Python TEXT value for Finance debt policy: {key}")
        source_ref = row.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise ValueError(f"Missing source_ref for Finance debt policy: {key}")
        values[key] = value
        source_refs[key] = source_ref

    missing = _REQUIRED_DEBT_POLICY_KEYS - values.keys()
    if missing:
        raise LookupError(
            f"Required Finance debt policies were not found: {', '.join(sorted(missing))}"
        )
    for key in ("debt_term_months", "debt_grace_months"):
        numeric = values[key]
        assert isinstance(numeric, Decimal)
        if numeric != numeric.to_integral_value():
            raise ValueError(f"Finance debt policy must be an integer: {key}")
        values[key] = int(numeric)
    return FinanceDebtPolicy(
        **values,
        policy_version=FINANCE_POLICY_VERSION,
        usage_scope=FINANCE_POLICY_USAGE_SCOPE,
        source_refs=source_refs,
    )


def _fetch_scheduled_rows(
    *,
    table: str,
    columns: tuple[str, str, str],
    sim_run_id: str,
    as_of: date,
    horizon_end: date,
    status_column: str,
    active_status: str | None = None,
    excluded_status: str | None = None,
) -> list[dict[str, object]]:
    status_clause = sql.SQL("{} = %s").format(sql.Identifier(status_column))
    status_value = active_status
    if excluded_status is not None:
        status_clause = sql.SQL("{} <> %s").format(sql.Identifier(status_column))
        status_value = excluded_status
    assert status_value is not None
    query = sql.SQL(
        """
        SELECT {}, {}, {}
        FROM {}.{}
        WHERE sim_run_id = %s
          AND {} > %s
          AND {} <= %s
          AND {}
        ORDER BY {}, {}
        """
    ).format(
        *(sql.Identifier(column) for column in columns),
        sql.Identifier(get_db_schema()),
        sql.Identifier(table),
        sql.Identifier(columns[1]),
        sql.Identifier(columns[1]),
        status_clause,
        sql.Identifier(columns[1]),
        sql.Identifier(columns[0]),
    )
    return fetch_all(query, [sim_run_id, as_of, horizon_end, status_value])


def _rows_to_events(
    rows: list[dict[str, object]],
    *,
    id_column: str,
    date_column: str,
    amount_column: str,
    event_type: str,
    direction: str,
) -> list[CashEvent]:
    events: list[CashEvent] = []
    for row in rows:
        ref_id = row.get(id_column)
        event_date = row.get(date_column)
        amount = row.get(amount_column)
        if not isinstance(ref_id, str) or not isinstance(event_date, date):
            raise TypeError(f"Invalid scheduled cash event identity: {event_type}")
        if isinstance(amount, bool) or not isinstance(amount, Decimal) or amount < 0:
            raise TypeError(f"Invalid scheduled cash event amount: {ref_id}")
        events.append(
            CashEvent(
                event_date=event_date,
                event_type=event_type,
                amount_krw=amount,
                direction=direction,
                ref_id=ref_id,
            )
        )
    return events


def _get_current_finance_state_row() -> dict[str, object]:
    query = sql.SQL(
        """
        SELECT
            finance_state_id,
            sim_run_id,
            state_date,
            state_type,
            financing_mode,
            current_cash_krw,
            minimum_operating_cash_krw,
            committed_outflows_krw,
            unsettled_purchase_payables_krw,
            receivables_krw,
            current_debt_krw,
            financial_limit_krw
        FROM {}.v_current_finance_state
        """
    ).format(sql.Identifier(get_db_schema()))
    row = fetch_one(query)
    if row is None:
        raise LookupError("Current Finance State was not found")
    _reject_negative_debt(row)
    return row


def _reject_negative_debt(row: Mapping[str, object]) -> None:
    """부채는 음수일 수 없다. **원천 행에서 한 번 막는다.**

    🔴 여기서 막지 않으면 음수 부채가 **"빚 없음"으로 읽힌다.** 부채 축의 판단은
       `current_debt_krw > 0` 인지로 갈리는데, 음수는 그 분기에서 0 과 같은 쪽에
       떨어진다 — 잘못된 DB 상태가 *"확인할 부채가 없다"* 는 정상 응답으로 둔갑하고,
       부채 정책 검증도 상환 일정도 통째로 건너뛴다.

    ★ **원천 행에 두는 이유**: 이 함수가 두 런타임 경로의 유일한 공통 입구다.

        _get_current_finance_state_row
          ├─ get_current_finance_snapshot → get_current_finance_runtime_context
          └─ PostgresFinanceAsOfDataPort.load_finance_position → load_debt_schedule

      `FinanceSnapshot` 검증만 믿으면 AsOf DataPort 는 **원시 dict 를 그대로** 쓰므로
      그 경로로 음수가 빠져나간다. 스키마 제약은 이중 방어이지 대체가 아니다.

    ★ 못 믿을 상태이지 프로그램 오류가 아니므로 `RUNTIME_NOT_READY` 로 접힌다.
    """
    debt = row.get("current_debt_krw")
    if debt is not None and Decimal(str(debt)) < 0:
        raise FinanceDataNotReady("finance_state_debt_invalid")


# ---------------------------------------------------------------------------
# as-of 재현성을 지키는 DataPort 구현
# ---------------------------------------------------------------------------

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
