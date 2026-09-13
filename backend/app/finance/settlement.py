"""매입대금 **실제 지급** — 인식(#615)과 다른 축이다.

```text
#615 인식   이 채무를 어느 날 현금곡선에 실었나   daily_closings.purchase_cash_out_krw
#637 지급   그 돈이 실제로 나갔나                 payables · finance_states
```

🔴 **둘을 합치지 않는다.** 인식은 *"곡선에 얹는다"* 이고 지급은 *"장부에서 뺀다"* 다.
   합치면 곡선을 다시 그릴 때마다 돈이 또 나가거나, 돈을 빼려면 곡선을 건드려야 한다.

⚠️ **#637 이전에는 지급이 아예 없었다.** 인식은 되는데 `payables` 는 `OPEN` 그대로였고
   `finance_states.current_cash_krw` 도 안 줄었다. 그래서 실측
   (`SIM-CHAIN-V9` · dev@0e635f8)에서 다음이 어긋났다.

   ```text
   Δ잔액    19,647,762
   Σ순현금  -6,444,153
   차이     26,091,915   = 매입유출 전액 · 어긋난 날 55
   ```

★ **멱등은 장부 자신이 말한다.** 새 표를 만들지 않는다 — 다 갚은 채무는
  `outstanding_amount_krw = 0` 이고 `SETTLED` 라, 다시 돌려도 낼 돈이 없다. 「이미
  냈나」를 따로 적어 두면 그 표와 장부가 갈리는 날이 온다.

★ `collection.py` 와 같은 모양이다. 저쪽은 채권이 들어오고 이쪽은 채무가 나간다 —
  같은 규율(두 행을 `FOR UPDATE` 로 잠그고 한 트랜잭션에서 함께 움직인다)을 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.finance.db import FinanceDataNotReady, get_db_schema, money_amount

__all__ = ["PayableSettlement", "SettlementResult", "settle_recognized_payables"]

_ZERO = Decimal(0)

#: 아직 낼 돈이 남아 있는 채무 상태.
_PAYABLE_OPEN_STATUSES = ["OPEN", "PARTIAL"]


@dataclass(frozen=True)
class PayableSettlement:
    """채무 한 건에서 실제로 나간 돈."""

    payable_id: str
    paid_krw: Decimal
    next_paid_amount_krw: Decimal
    next_outstanding_amount_krw: Decimal
    next_status: str


@dataclass(frozen=True)
class SettlementResult:
    """이 마감에서 나간 돈 전부."""

    settled: tuple[PayableSettlement, ...]

    @property
    def total_paid_krw(self) -> Decimal:
        return sum((row.paid_krw for row in self.settled), start=_ZERO)


class FinanceSettlementConflict(RuntimeError):
    """잠근 행이 한 행이 아니었다. **조용히 넘어가지 않는다.**"""


def settle_recognized_payables(
    conn: Any, *, sim_run_id: str, as_of: date
) -> SettlementResult:
    """이 날 현금곡선에 실린 채무를 **실제로 지급한다.**

    다섯 사실이 한 번에 움직인다. 하나만 움직이면 장부가 서로 다른 말을 한다.

    ```text
    payables.paid_amount_krw          늘어난다
    payables.outstanding_amount_krw   줄어든다
    payables.status                   OPEN/PARTIAL → PARTIAL/SETTLED
    finance_states.current_cash_krw                  줄어든다
    finance_states.unsettled_purchase_payables_krw   줄어든다
    ```

    🔴 **인식 원장을 고치지 않는다.** 어느 채무를 언제 곡선에 실었나는 #615 의 사실이고,
       여기서는 그것을 **읽어서** 그날 나갈 돈을 정한다.

    ★ **미래 채무를 미리 내지 않는다.** 인식이 `recognized_date = as_of` 인 것만 본다 —
      인식 자체가 이미 기일과 주말 이월(`effective_cash_date`)을 지나온 결과다.

    ★ **다시 돌려도 두 번 나가지 않는다.** 첫 실행 뒤 `outstanding_amount_krw = 0` 이라
      두 번째 실행은 낼 돈이 0 이고, 그러면 상태도 안 건드린다.
    """
    if not isinstance(sim_run_id, str) or not sim_run_id.strip():
        raise ValueError("sim_run_id must be a non-blank string")

    schema = sql.Identifier(get_db_schema())
    settled: list[PayableSettlement] = []
    with conn.cursor() as cursor:
        #  🔴 **인식된 것만, 그리고 아직 낼 돈이 남은 것만.** 두 조건이 함께여야 한다 —
        #     인식 없이 내면 곡선에 없는 현금이 나가고, 잔액을 안 보면 두 번 낸다.
        cursor.execute(
            sql.SQL(
                """
                SELECT p.payable_id, p.paid_amount_krw, p.outstanding_amount_krw,
                       e.recognized_amount_krw
                FROM {schema}.finance_payable_closing_events e
                JOIN {schema}.payables p
                  ON p.payable_id = e.payable_id AND p.sim_run_id = e.sim_run_id
                WHERE e.sim_run_id = %s
                  AND e.recognized_date = %s
                  AND p.status = ANY(%s)
                  AND p.outstanding_amount_krw > 0
                ORDER BY p.payable_id
                FOR UPDATE OF p
                """
            ).format(schema=schema),
            [sim_run_id, as_of, _PAYABLE_OPEN_STATUSES],
        )
        rows = cursor.fetchall()

        for row in rows:
            payable_id = row["payable_id"]
            if not isinstance(payable_id, str) or not payable_id.strip():
                raise FinanceDataNotReady("payable_id")
            outstanding = money_amount(row["outstanding_amount_krw"], "payable_outstanding")
            recognized = money_amount(row["recognized_amount_krw"], "payable_recognized_amount")
            #  ⚠️ 인식액이 남은 잔액보다 클 수 있다 — 인식 뒤에 다른 경로가 일부를 갚거나
            #    취소했으면 그렇다. **장부에 남은 만큼만** 낸다. 넘겨 내면 음수 잔액이 선다.
            paid_now = min(recognized, outstanding)
            if paid_now <= 0:
                continue
            next_paid = money_amount(row["paid_amount_krw"], "payable_paid") + paid_now
            next_outstanding = outstanding - paid_now
            next_status = "SETTLED" if next_outstanding == 0 else "PARTIAL"
            cursor.execute(
                sql.SQL(
                    """
                    UPDATE {}.payables
                       SET paid_amount_krw = %s,
                           outstanding_amount_krw = %s,
                           status = %s,
                           settled_date = %s
                     WHERE payable_id = %s
                    """
                ).format(schema),
                [
                    next_paid,
                    next_outstanding,
                    next_status,
                    #  ★ 다 갚은 날만 적는다. 부분 지급은 아직 «끝난 날» 이 아니다.
                    as_of if next_status == "SETTLED" else None,
                    payable_id,
                ],
            )
            if cursor.rowcount != 1:
                raise FinanceSettlementConflict(
                    "payable update did not affect exactly one row"
                )
            settled.append(
                PayableSettlement(
                    payable_id=payable_id,
                    paid_krw=paid_now,
                    next_paid_amount_krw=next_paid,
                    next_outstanding_amount_krw=next_outstanding,
                    next_status=next_status,
                )
            )

        result = SettlementResult(settled=tuple(settled))
        if result.total_paid_krw > 0:
            _apply_state_payment(
                cursor,
                schema=schema,
                sim_run_id=sim_run_id,
                as_of=as_of,
                paid_krw=result.total_paid_krw,
            )
    return result


def _apply_state_payment(
    cursor: Any,
    *,
    schema: sql.Identifier,
    sim_run_id: str,
    as_of: date,
    paid_krw: Decimal,
) -> None:
    """나간 돈만큼 그날 재무 상태에서 현금과 미지급 채무를 뺀다.

    🔴 **상태를 다시 계산하지 않는다.** 나간 금액만큼 **빼는** 것이다 — 다시 세면 그 날
       다른 경로가 만든 값(수금·차입)이 조용히 덮인다.

    ⚠️ `unsettled_purchase_payables_krw` 는 0 아래로 내려가지 않게 막는다. 음수가 되면
      그것은 *"채무가 마이너스"* 라는 없는 사실이고, 그 값으로 다음 판단이 돈다.
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT finance_state_id, current_cash_krw, unsettled_purchase_payables_krw
            FROM {}.finance_states
            WHERE sim_run_id = %s AND state_date = %s
            ORDER BY finance_state_id
            FOR UPDATE
            """
        ).format(schema),
        [sim_run_id, as_of],
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        #  🔴 축이 둘이면 어느 장부에서 돈이 나갔는지 고르지 않는다. 고르는 순간
        #     무차입 장부의 현금이 대출 장부의 지급으로 줄어들 수 있다.
        raise FinanceDataNotReady("finance_state_for_settlement")
    row = rows[0]
    current_cash = money_amount(row["current_cash_krw"], "current_cash_krw")
    unsettled = money_amount(
        row["unsettled_purchase_payables_krw"], "unsettled_purchase_payables_krw"
    )
    cursor.execute(
        sql.SQL(
            """
            UPDATE {}.finance_states
               SET current_cash_krw = %s,
                   unsettled_purchase_payables_krw = %s
             WHERE finance_state_id = %s
            """
        ).format(schema),
        [
            #  ★ 현금은 음수가 될 수 있다. 그것은 «돈이 모자랐다» 는 사실이고,
            #    막으면 가장 위험한 날의 장부가 사라진다.
            current_cash - paid_krw,
            max(_ZERO, unsettled - paid_krw),
            row["finance_state_id"],
        ],
    )
    if cursor.rowcount != 1:
        raise FinanceSettlementConflict(
            "finance state update did not affect exactly one row"
        )
