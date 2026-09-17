"""일반 운영비의 생명주기 — **발생과 지급을 다른 사실로 적는다.**

원장에는 `expenses` 표가 오래전부터 있었지만, 실제로 쓰인 것은 «이미 지급된 비용을
뒤에서 적는» 한 가지뿐이었다. 그래서 다음 주에 나갈 임차료가 장부 어디에도 없었고,
미래 현금 투영은 그 돈이 없는 것처럼 계산했다.

이 모듈이 세우는 것은 상태 하나와 날짜 둘이다.

```text
ACCRUED    비용이 발생했고 아직 안 나갔다   expense_date · due_date
  ↓ 지급
PAID       실제로 나갔다                    + paid_date · 현금 차감
  ↘ 취소
CANCELLED  나가지 않기로 했다               현금 변화 없음
```

🔴 **`PAID → CANCELLED` 는 없다.** 이미 나간 돈을 취소하면 과거의 현금유출이 장부에서
   사라진다. 그건 취소가 아니라 환입이고, 환입 정책은 이 저장소에 아직 없다. 없는
   정책을 여기서 발명하지 않는다.

🔴 **부분지급도 없다.** `expenses` 는 지급액 칸을 갖고 있지 않다. 표현할 수 없는 것을
   표현하는 척하면 «절반 나갔다» 가 «다 나갔다» 로 적힌다.

★ **근거의 정본은 `evidence_id` 다.** 다른 Finance 원장이 `source_ref` · `recorded_by`
  를 쓴다고 해서 여기에 같은 칸을 만들지 않는다 — 비용 원장의 근거 계약은 이미 정해져
  있고, 같은 사실에 이름이 둘이 되면 어느 쪽이 정본인지 아무도 말할 수 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from psycopg import sql

from app.finance.db import FinanceDataNotReady, get_db_schema

__all__ = [
    "KNOWN_EXPENSE_CATEGORIES",
    "OPERATING_EXPENSE_CATEGORIES",
    "PAYROLL_INTEREST_CATEGORIES",
    "ExpenseConflict",
    "ExpenseSettlement",
    "ExpenseStatus",
    "cancel_expense",
    "create_expense",
    "effective_paid_date",
    "settle_due_expenses",
    "settle_expense",
]

ExpenseStatus = Literal["ACCRUED", "PAID", "CANCELLED"]

#: 급여·이자 칸으로 가는 분류. **원장이 실제로 쓰는 이름이다.**
#:
#: ★ `INTEREST` 와 `LOAN_INTEREST` 가 둘 다 있다. 원장이 쓰는 이름은 `LOAN_INTEREST`
#:   이지만, 아는 이름을 지우면 예전 데이터가 다시 막힌다.
PAYROLL_INTEREST_CATEGORIES: frozenset[str] = frozenset(
    {"PAYROLL", "INTEREST", "LOAN_INTEREST"}
)

#: 일반 운영비 칸으로 가는 분류. 매입대금도 물류비도 급여·이자도 아닌 잔여다.
#:
#: 🔴 **`LABOR` 를 넣지 않았다.** 화면 이름표는 이것을 «인건비» 로 부르고 `PAYROLL` 을
#:   «급여» 로 부르는데, 둘이 같은 칸에 가야 하는지 다른 칸에 가야 하는지를 정한 문서가
#:   저장소에 없다. 모르는 것을 운영비로 밀어 넣으면 급여가 운영비로 적힌 날이 생긴다 —
#:   막히는 편이 낫다 (마감이 `daily_closing_expense_category` 로 선다).
#:
#: 🔴 **`LOGISTICS` · `TRANSPORT` · `LOGISTICS_SERVICE` 도 넣지 않았다.** 물류비는
#:   `related_delivery_id` 가 붙어 물류 칸으로 간다. 납품이 안 붙은 물류 분류는 어느
#:   칸에 가야 하는지가 정해진 적 없다.
OPERATING_EXPENSE_CATEGORIES: frozenset[str] = frozenset(
    {"RENT", "UTILITY", "COMMISSION", "PACKAGING", "DISPOSAL", "OTHER"}
)

#: 새 비용을 만들 때 받을 수 있는 분류 전부.
#:
#: ★ 조회는 모르는 분류도 그대로 보여 준다(`console_expenses`). **쓰기만 잠근다** —
#:   읽는 쪽이 과거 데이터를 거부하면 이미 적힌 사실이 화면에서 사라진다.
KNOWN_EXPENSE_CATEGORIES: frozenset[str] = (
    OPERATING_EXPENSE_CATEGORIES | PAYROLL_INTEREST_CATEGORIES
)

_ZERO = Decimal(0)


class ExpenseConflict(ValueError):
    """비용 원장의 현재 사실과 어긋나는 요청."""


@dataclass(frozen=True)
class ExpenseSettlement:
    """지급 한 번의 결과. **현금이 얼마가 됐는지까지 같이 돌려준다.**"""

    expense_id: str
    paid_date: date
    amount_krw: Decimal
    current_cash_krw: Decimal


def effective_paid_date(
    *, status: str, paid_date: date | None, expense_date: date
) -> date | None:
    """그 비용이 **실제로 현금에서 빠진 날**로 읽을 날짜.

    ```text
    PAID  + paid_date 있음   → paid_date        (정본)
    PAID  + paid_date 없음   → expense_date     (LEGACY READ COMPATIBILITY ONLY)
    그 외                     → None
    ```

    🔴 **두 번째 줄은 읽기 전용 호환이다.** 마감이 과거 실행을 다시 계산할 때 이미 적힌
       지급을 잃지 않으려고 둔 것이지, `expense_date` 가 지급일이라는 뜻이 아니다.
       원장에 `paid_date = expense_date` 로 적어 넣지 않는다 — 그러면 모르는 것이
       **아는 것처럼** 남고, 화면은 틀린 날짜를 지급일이라고 말하게 된다.

    ★ 그래서 화면 쪽에는 이 함수를 쓰지 않는다. 사용자에게는 «지급일 미상» 이라고
      말해야 한다 (`console_expenses.paid_date_known`).
    """
    if status != "PAID":
        return None
    return paid_date if paid_date is not None else expense_date


def create_expense(
    conn: Any,
    *,
    sim_run_id: str,
    expense_date: date,
    due_date: date,
    expense_category: str,
    amount_krw: Decimal,
    evidence_id: str,
    related_delivery_id: str | None = None,
    is_fixed: bool = False,
    note: str | None = None,
) -> str:
    """발생한 비용 하나를 `ACCRUED` 로 적는다. **현금은 건드리지 않는다.**

    ★ 현금은 지급할 때 빠진다. 발생 시점에 빼면 아직 나가지 않은 돈이 없는 것으로
      적히고, 그 뒤 지급하면 같은 돈이 두 번 빠진다.

    🔴 **`due_date` 는 필수다.** 지급 예정일이 없으면 미래 투영이 이 의무를 놓친다 —
       그게 바로 이 모듈이 생긴 이유다.
    """
    if not isinstance(sim_run_id, str) or not sim_run_id.strip():
        raise ExpenseConflict("실행 축(sim_run_id)이 필요합니다.")
    if not isinstance(evidence_id, str) or not evidence_id.strip():
        raise ExpenseConflict("비용을 확인할 근거 자료가 필요합니다.")
    if expense_category not in KNOWN_EXPENSE_CATEGORIES:
        #  ★ 모르는 분류를 «기타» 로 바꾸지 않는다. 바꾸면 마감이 조용히 다른 칸에 센다.
        raise ExpenseConflict(f"원장이 모르는 비용 분류입니다: {expense_category}")
    if not isinstance(amount_krw, Decimal):
        raise ExpenseConflict("비용 금액은 Decimal 이어야 합니다.")
    if amount_krw <= _ZERO:
        #  ⚠️ 0원은 «비용이 없다» 이지 «0원짜리 비용이 있다» 가 아니다.
        raise ExpenseConflict("비용 금액은 0원보다 커야 합니다.")
    if due_date < expense_date:
        raise ExpenseConflict("지급 예정일은 발생일보다 앞설 수 없습니다.")

    expense_id = f"EXP-{uuid4()}"
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.expenses (
                    expense_id, sim_run_id, expense_date, due_date, paid_date,
                    expense_category, amount_krw, is_fixed, related_delivery_id,
                    evidence_id, status, note
                ) VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, 'ACCRUED', %s)
                """
            ).format(schema),
            [
                expense_id,
                sim_run_id,
                expense_date,
                due_date,
                expense_category,
                amount_krw,
                is_fixed,
                related_delivery_id,
                evidence_id,
                note,
            ],
        )
        if cursor.rowcount != 1:
            raise ExpenseConflict("비용을 원장에 적지 못했습니다.")
    return expense_id


def settle_expense(
    conn: Any,
    *,
    expense_id: str,
    sim_run_id: str,
    financing_mode: str,
    paid_date: date,
) -> ExpenseSettlement:
    """`ACCRUED` 비용 하나를 지급하고 **같은 거래에서** 현금을 줄인다.

    ```text
    비용 행 잠금 → 상태·실행 축 확인 → 재무 상태 행 잠금 → 현금 차감 → PAID 기록
    ```

    🔴 **두 번 눌러도 한 번만 빠진다.** 같은 요청이 재시도되면 두 번째는 상태가 이미
       `PAID` 라 잠금 뒤에서 막힌다 — 별도 멱등 표를 만들지 않아도 상태 자체가 잠금
       역할을 한다. 상태를 읽고 나서 잠그면 두 요청이 같은 `ACCRUED` 를 함께 보고 둘
       다 통과하므로, **잠그고 나서 읽는** 순서를 바꾸지 않는다.

    🔴 **재무 상태는 정확히 한 행이어야 한다.** 실행·장부·기준일 셋으로 고른다. 못
       찾으면 막고, 둘 이상이면 막는다 — «최신 상태» 로 대신 고르면 다른 실행이나 다른
       장부의 현금이 줄어든다.
    """
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT expense_id, sim_run_id, status, amount_krw
                FROM {}.expenses
                WHERE expense_id = %s
                FOR UPDATE
                """
            ).format(schema),
            [expense_id],
        )
        expense_rows = cursor.fetchall()
        if not expense_rows:
            raise LookupError(f"비용을 찾을 수 없습니다: {expense_id}")
        expense = expense_rows[0]
        if str(expense["sim_run_id"]) != sim_run_id:
            #  ★ 남의 실행 비용을 이 실행의 현금에서 빼지 않는다.
            raise ExpenseConflict("다른 실행의 비용은 지급할 수 없습니다.")
        status = str(expense["status"])
        if status == "PAID":
            raise ExpenseConflict("이미 지급된 비용입니다.")
        if status != "ACCRUED":
            raise ExpenseConflict(f"지급할 수 없는 비용 상태입니다: {status}")
        amount = Decimal(str(expense["amount_krw"]))

        cursor.execute(
            sql.SQL(
                """
                SELECT finance_state_id, current_cash_krw
                FROM {}.finance_states
                WHERE sim_run_id = %s AND financing_mode = %s AND state_date = %s
                FOR UPDATE
                """
            ).format(schema),
            [sim_run_id, financing_mode, paid_date],
        )
        state_rows = cursor.fetchall()
        if not state_rows:
            raise FinanceDataNotReady("historical_finance_position")
        if len(state_rows) != 1:
            raise FinanceDataNotReady("finance_state_ambiguous")
        state = state_rows[0]
        next_cash = Decimal(str(state["current_cash_krw"])) - amount
        if next_cash < _ZERO:
            raise ExpenseConflict("지급 후 현금이 0원보다 작아질 수 없습니다.")

        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.expenses
                SET status = 'PAID', paid_date = %s
                WHERE expense_id = %s AND status = 'ACCRUED'
                """
            ).format(schema),
            [paid_date, expense_id],
        )
        if cursor.rowcount != 1:
            raise ExpenseConflict("비용 상태를 지급으로 바꾸지 못했습니다.")
        cursor.execute(
            sql.SQL(
                "UPDATE {}.finance_states SET current_cash_krw = %s WHERE finance_state_id = %s"
            ).format(schema),
            [next_cash, state["finance_state_id"]],
        )
        if cursor.rowcount != 1:
            raise ExpenseConflict("재무 상태를 갱신하지 못했습니다.")
    return ExpenseSettlement(
        expense_id=expense_id,
        paid_date=paid_date,
        amount_krw=amount,
        current_cash_krw=next_cash,
    )


def settle_due_expenses(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
) -> tuple[ExpenseSettlement, ...]:
    """`as_of` 까지 지급일이 된 `ACCRUED` 비용을 **기존 `settle_expense()` 로** 지급한다.

    🔴 **아직 비어 있다.** 마스터가 부를 자리를 먼저 세운 껍데기이고, **속은 재무가
       채운다** (2026-09-17 · 계약은 아래 여섯 줄). 지금은 빈 튜플을 돌려주므로
       걷기에서 이 단계는 «지급할 것이 없었다» 로 보인다 — 켜도 현금이 안 움직인다.

    ```text
    ①  conn 은 **부르는 쪽이 소유**한다        `settle_expense()` 와 같은 규율
                                             🔴 이 함수는 commit 하지 않는다
    ②  반환은 **기존 `ExpenseSettlement`**     새 결과형을 만들지 않는다
    ③  지급할 것이 없으면 **빈 튜플**           예외가 아니다
    ④  실패는 **올린다**                       `ExpenseConflict` · `FinanceDataNotReady`
                                             🔴 삼키지 않는다 — 부르는 쪽이 fail-closed 로 받는다
    ⑤  순서 `ORDER BY due_date, expense_id`    결정론
    ⑥  `financing_mode` 는 **이 함수가** `sim_run` 에서 읽는다   부르는 쪽이 넘기지 않는다
    ```

    🔴 **④ 를 제일 크게 적는 이유.** 지급이 실패했는데 그날이 정상 `CLOSED` 로 서면
       **현금은 줄었는데 비용은 0원**인 기록이 남는다. 그래서 여기서 삼키지 않고,
       부르는 쪽(`master.scheduler`)이 그날 마감을 막는다.

    ★ **한 트랜잭션이다.** 여러 건을 지급하다 중간에서 터지면 앞선 지급까지 같이
      되돌아가야 한다 — 그래서 ① 로 커넥션을 부르는 쪽에 둔다. 절반만 나간 상태로
      커밋되면 현금과 원장이 갈린다.

    :param conn: 부르는 쪽이 연 커넥션. **이 함수는 commit 하지 않는다.**
    :param sim_run_id: 실행 축. 🔴 **다른 실행의 비용을 지급하지 않는다.**
    :param as_of: 이 날짜까지 지급일이 된 것을 지급하고, `paid_date` 로 적는다.
    :returns: 지급한 건들. 없으면 빈 튜플.
    """
    #  🔴 재무가 채운다 (2026-09-17). 마스터는 여기에 로직을 넣지 않는다.
    return ()


def cancel_expense(conn: Any, *, expense_id: str, sim_run_id: str) -> str:
    """`ACCRUED` 비용을 «나가지 않기로 한다» 로 적는다. **현금은 변하지 않는다.**

    🔴 **이미 지급된 비용은 여기로 못 온다.** 돈이 나간 사실은 취소로 지워지지 않는다.
    """
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT expense_id, sim_run_id, status
                FROM {}.expenses
                WHERE expense_id = %s
                FOR UPDATE
                """
            ).format(schema),
            [expense_id],
        )
        rows = cursor.fetchall()
        if not rows:
            raise LookupError(f"비용을 찾을 수 없습니다: {expense_id}")
        expense = rows[0]
        if str(expense["sim_run_id"]) != sim_run_id:
            raise ExpenseConflict("다른 실행의 비용은 취소할 수 없습니다.")
        status = str(expense["status"])
        if status == "PAID":
            raise ExpenseConflict(
                "이미 지급된 비용은 취소할 수 없습니다 — 환입은 별도 정책이 필요합니다."
            )
        if status != "ACCRUED":
            raise ExpenseConflict(f"취소할 수 없는 비용 상태입니다: {status}")
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.expenses SET status = 'CANCELLED'
                WHERE expense_id = %s AND status = 'ACCRUED'
                """
            ).format(schema),
            [expense_id],
        )
        if cursor.rowcount != 1:
            raise ExpenseConflict("비용 상태를 취소로 바꾸지 못했습니다.")
    return expense_id
