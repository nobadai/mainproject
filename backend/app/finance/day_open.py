"""Finance implementation of Master's structural ``DayOpening`` boundary.

Master owns the calendar and transaction. Finance only knows how to determine whether the
required Finance state exists on an exact date and how to carry the exact preceding state
forward. The implementation deliberately does not import Master types.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg import sql

from app.finance.db import FinanceDataNotReady, get_db_schema, load_inventory_snapshot_as_of
from app.finance.state_identity import daily_finance_state_id

__all__ = ["FinanceDayOpening"]

# ``DAY`` is already Finance's generic daily state vocabulary (as opposed to the seeded
# ``DAY0``/``DAY30`` snapshots and the approval-specific ``H1_COMMITMENT`` state).
DAY_OPEN_STATE_TYPE = "DAY"


class FinanceDayOpening:
    """Open an exact calendar-day Finance state using the caller's connection only.

    🔴 **어느 실행을 여는지는 부르는 쪽이 안다.** 예전에는 `v_current_finance_state`
       전체에 대고 *"지금 축이 하나뿐인가"* 를 물어 축을 골랐다. 실행이 하나일 때는
       같은 답이지만, 번인과 새 걷기가 **공존하는 순간** 그 질문은 늘 *"둘"* 이라고
       답한다 — 실측으로 `SIM-BURNIN-202512` 와 `SIM-WALK-202601-LOAN` 이 함께 서자
       새 걷기의 첫 개장이 `finance_runtime_axis_ambiguous` 로 막혔다.

    ★ 그래서 실행은 **생성 때 받는다.** 마스터는 이미 그 값을 들고 있고
      (`sim_run_binding.SimRunBound`), 재무는 받은 실행만 본다. `financing_mode` 는
      여전히 재무가 정한다 — 마스터가 고르지 않는다.
    """

    def __init__(self, *, sim_run_id: str | None = None) -> None:
        self.sim_run_id = sim_run_id

    def is_open(self, conn: Any, *, as_of: date) -> bool:
        """Return whether the active runtime axis has a state exactly on ``as_of``."""
        sim_run_id, financing_mode = self._runtime_axis(conn)
        schema = sql.Identifier(get_db_schema())
        query = self._exact_state_query(schema)
        with conn.cursor() as cursor:
            cursor.execute(
                query,
                {
                    "sim_run_id": sim_run_id,
                    "financing_mode": financing_mode,
                    "state_date": as_of,
                },
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise FinanceDataNotReady("finance_state_ambiguous")
        return bool(rows)

    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:
        """Carry the exact ``carry_from`` state to ``as_of`` idempotently.

        Finance 고유 값은 이어 가되 재고가치는 ``as_of`` 시점의 Inventory Ledger에서
        다시 계산한다. ``financial_limit_krw``는 PostgreSQL 생성 컬럼이므로 insert에서
        제외한다.
        """
        sim_run_id, financing_mode = self._runtime_axis(conn)
        if self._has_exact_state(
            conn,
            as_of=as_of,
            sim_run_id=sim_run_id,
            financing_mode=financing_mode,
        ):
            return
        if not self._has_exact_state(
            conn,
            as_of=carry_from,
            sim_run_id=sim_run_id,
            financing_mode=financing_mode,
        ):
            raise FinanceDataNotReady("historical_finance_position")

        schema = sql.Identifier(get_db_schema())
        inventory = load_inventory_snapshot_as_of(
            conn,
            sim_run_id=sim_run_id,
            as_of=as_of,
        )
        with conn.cursor() as cursor:
            cursor.execute(
                self._carry_forward_query(schema),
                {
                    "finance_state_id": daily_finance_state_id(
                        sim_run_id=sim_run_id,
                        financing_mode=financing_mode,
                        state_date=as_of,
                    ),
                    "sim_run_id": sim_run_id,
                    "financing_mode": financing_mode,
                    "as_of": as_of,
                    "carry_from": carry_from,
                    "state_type": DAY_OPEN_STATE_TYPE,
                    "inventory_book_value_krw": inventory.inventory_book_value_krw,
                    "operational_inventory_value_krw": (
                        inventory.operational_inventory_value_krw
                    ),
                    "note": f"하루 넘김이 {carry_from} 재무 상태에서 물려받아 세운 행",
                },
            )
            inserted = cursor.rowcount
        if not inserted and not self._has_exact_state(
            conn,
            as_of=as_of,
            sim_run_id=sim_run_id,
            financing_mode=financing_mode,
        ):
            raise FinanceDataNotReady("historical_finance_position")

    def _runtime_axis(self, conn: Any) -> tuple[str, str]:
        """이 개장이 서 있는 재무 축. **주어진 실행 안에서만 고른다.**

        ★ 축이 모호하다는 말은 *"같은 실행 안에서 조달 방식이 갈렸다"* 여야 한다.
          다른 실행이 하나 더 서 있다는 사실은 이 실행을 모호하게 만들지 않는다.
        """
        schema = sql.Identifier(get_db_schema())
        if self.sim_run_id is None:
            query = sql.SQL(
                "SELECT DISTINCT sim_run_id, financing_mode"
                " FROM {}.v_current_finance_state"
            ).format(schema)
            params: list[object] = []
        else:
            query = sql.SQL(
                "SELECT DISTINCT sim_run_id, financing_mode"
                " FROM {}.v_current_finance_state WHERE sim_run_id = %s"
            ).format(schema)
            params = [self.sim_run_id]
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        if not rows:
            # 🔴 **없으면 없는 것이다.** 다른 실행의 축으로 대신하지 않는다.
            raise FinanceDataNotReady("historical_finance_position")
        if len(rows) != 1:
            raise FinanceDataNotReady("finance_runtime_axis_ambiguous")
        row = rows[0]
        if isinstance(row, dict):
            return str(row["sim_run_id"]), str(row["financing_mode"])
        return str(row[0]), str(row[1])

    @staticmethod
    def _has_exact_state(
        conn: Any,
        *,
        as_of: date,
        sim_run_id: str,
        financing_mode: str,
    ) -> bool:
        schema = sql.Identifier(get_db_schema())
        query = FinanceDayOpening._exact_state_query(schema)
        with conn.cursor() as cursor:
            cursor.execute(
                query,
                {
                    "sim_run_id": sim_run_id,
                    "financing_mode": financing_mode,
                    "state_date": as_of,
                },
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise FinanceDataNotReady("finance_state_ambiguous")
        return bool(rows)

    @staticmethod
    def _exact_state_query(schema: sql.Identifier) -> sql.Composed:
        return sql.SQL(
            """
            SELECT finance_state_id
            FROM {}.finance_states
            WHERE sim_run_id = %(sim_run_id)s
              AND financing_mode = %(financing_mode)s
              AND state_date = %(state_date)s
            LIMIT 2
            """
        ).format(schema)

    @staticmethod
    def _carry_forward_query(schema: sql.Identifier) -> sql.Composed:
        return sql.SQL(
            """
            INSERT INTO {schema}.finance_states (
                finance_state_id, sim_run_id, state_date, state_type, financing_mode,
                current_cash_krw, minimum_operating_cash_krw, committed_outflows_krw,
                unsettled_purchase_payables_krw, receivables_krw,
                inventory_book_value_krw, operational_inventory_value_krw,
                current_debt_krw, recommended_loan_amount_krw, note
            )
            SELECT
                %(finance_state_id)s, base.sim_run_id, %(as_of)s, %(state_type)s,
                base.financing_mode,
                base.current_cash_krw, base.minimum_operating_cash_krw,
                base.committed_outflows_krw, base.unsettled_purchase_payables_krw,
                base.receivables_krw, %(inventory_book_value_krw)s,
                %(operational_inventory_value_krw)s, base.current_debt_krw,
                base.recommended_loan_amount_krw, %(note)s
            FROM {schema}.finance_states base
            WHERE base.sim_run_id = %(sim_run_id)s
              AND base.financing_mode = %(financing_mode)s
              AND base.state_date = %(carry_from)s
            ON CONFLICT (finance_state_id) DO NOTHING
            """
        ).format(schema=schema)
