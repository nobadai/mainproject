"""FINAL 실행축에 **창고 임대·기본운영비 9건**을 `ACCRUED` 로 적는다.

```bash
# 무엇이 들어갈지 먼저 본다 (아무것도 적지 않는다)
.venv/Scripts/python.exe scripts/seed_final_operating_expenses.py --sim-run-id SIM-... --dry-run

# 실제로 적는다
.venv/Scripts/python.exe scripts/seed_final_operating_expenses.py --sim-run-id SIM-... --apply
```

🔴 **`--apply` 를 안 주면 아무것도 적지 않는다.** 공유 DB 에 원장을 넣는 일이고,
   *"돌릴 생각은 없었다"* 가 한 번이면 충분하다.

🔴 **실행 축을 추측하지 않는다.** `--sim-run-id` 는 필수다. FINAL 축이 정해진 뒤 그
   축을 명시해서 부른다 — 기본값을 두면 REH/PREFINAL 같은 남의 축에 비용이 들어간다.

🔴 **근거 없는 비용을 만들지 않는다.** `evidence_id` 가 원장에 없으면 멈춘다. 같은
   이름으로 근거를 새로 만들지도 않는다.

★ **SQL 을 새로 쓰지 않는다.** 기존 `create_expense()` 를 그대로 부른다. 직접 INSERT 로
  생명주기를 우회하면 `status='ACCRUED'` 규율과 검증이 이 자리에만 없게 된다.

★ **현금은 움직이지 않는다.** 발생만 적는다. 지급은 걷기가 `settle_due_expenses()` 로
  지급일에 한다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg import sql

from app.finance.db import get_connection, get_db_schema
from app.finance.expenses import create_expense

#: 합의된 운영비. 월 3,855,000원 × 9개월 = 34,695,000원.
#:
#: ★ 지급 예정일은 **원문 그대로** 적는다. 5월만 11일인 것은 달력 사정이고, 규칙으로
#:   바꿔 계산하면 그 사정이 사라진다.
DUE_DATES = (
    date(2026, 1, 10),
    date(2026, 2, 10),
    date(2026, 3, 10),
    date(2026, 4, 10),
    date(2026, 5, 11),
    date(2026, 6, 10),
    date(2026, 7, 10),
    date(2026, 8, 10),
    date(2026, 9, 10),
)
MONTHLY_KRW = Decimal(3855000)
EXPECTED_COUNT = len(DUE_DATES)
EXPECTED_TOTAL = MONTHLY_KRW * EXPECTED_COUNT  # 34,695,000
CATEGORY = "OTHER"
EVIDENCE_ID = "EV-SRC-LOGI-PERSONA"
NOTE = "창고 임대·기본운영비"


def _expense_date(due: date) -> date:
    """비용이 **발생한** 달의 1일.

    ★ 발생과 지급은 다른 사실이다. 임대료는 그 달에 발생하고 10일에 나간다 — 둘을 같은
      날짜로 적으면 미래 투영이 «이번 달 의무» 를 못 본다.
    """
    return due.replace(day=1)


def _evidence_exists(conn, evidence_id: str) -> bool:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT evidence_id FROM {}.evidences WHERE evidence_id = %s").format(
                schema
            ),
            [evidence_id],
        )
        return bool(cursor.fetchall())


def _already_seeded(conn, *, sim_run_id: str) -> set[date]:
    """이 축에 **이미 적힌** 같은 운영비의 지급일.

    🔴 seed 를 두 번 돌려 18건이 되는 것을 막는다. `expenses` 에는 이 조합을 막을
       자연 업무 키가 없고, 이번 작업에서 DB 제약을 새로 만들지 않기로 했으므로
       **여기서 읽고 건너뛴다.**

    ★ 분류·금액·지급일 셋으로 고른다. 같은 축에 같은 날 같은 금액의 운영비가 둘
      있어야 할 이유가 없다.
    """
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT due_date
                FROM {}.expenses
                WHERE sim_run_id = %s
                  AND expense_category = %s
                  AND amount_krw = %s
                  AND due_date = ANY(%s)
                """
            ).format(schema),
            [sim_run_id, CATEGORY, MONTHLY_KRW, list(DUE_DATES)],
        )
        return {row["due_date"] for row in cursor.fetchall()}


def _verify(conn, *, sim_run_id: str) -> tuple[int, Decimal]:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT count(*) AS count, COALESCE(sum(amount_krw), 0) AS total
                FROM {}.expenses
                WHERE sim_run_id = %s
                  AND expense_category = %s
                  AND amount_krw = %s
                  AND due_date = ANY(%s)
                """
            ).format(schema),
            [sim_run_id, CATEGORY, MONTHLY_KRW, list(DUE_DATES)],
        )
        row = cursor.fetchall()[0]
    return int(row["count"]), Decimal(str(row["total"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim-run-id", required=True, help="FINAL 실행 축. 추측하지 않는다.")
    parser.add_argument("--apply", action="store_true", help="실제로 원장에 적는다.")
    parser.add_argument("--dry-run", action="store_true", help="기본 동작. 아무것도 적지 않는다.")
    args = parser.parse_args()

    sim_run_id = args.sim_run_id.strip()
    if not sim_run_id:
        print("실행 축이 비어 있습니다.")
        return 2

    print(f"실행 축      {sim_run_id}")
    print(f"분류         {CATEGORY} · is_fixed=True · related_delivery_id=NULL")
    print(f"월 금액      {MONTHLY_KRW:,} KRW")
    print(f"건수 / 총액   {EXPECTED_COUNT} / {EXPECTED_TOTAL:,} KRW")
    print(f"근거         {EVIDENCE_ID}")
    print()

    with get_connection() as conn:
        if not _evidence_exists(conn, EVIDENCE_ID):
            #  🔴 없는 근거로 비용을 만들지 않고, 같은 id 를 새로 만들지도 않는다.
            print(f"멈춤: 필요한 evidence_id 가 현재 DB 에 없음 — {EVIDENCE_ID}")
            return 3

        seeded = _already_seeded(conn, sim_run_id=sim_run_id)
        todo = [due for due in DUE_DATES if due not in seeded]
        for due in DUE_DATES:
            mark = "이미 있음" if due in seeded else "추가"
            print(f"  {_expense_date(due)} 발생 → {due} 지급   {mark}")
        print()

        if not args.apply:
            print(f"--dry-run: 적지 않았습니다. 추가 예정 {len(todo)}건.")
            return 0

        for due in todo:
            expense_id = create_expense(
                conn,
                sim_run_id=sim_run_id,
                expense_date=_expense_date(due),
                due_date=due,
                expense_category=CATEGORY,
                amount_krw=MONTHLY_KRW,
                evidence_id=EVIDENCE_ID,
                is_fixed=True,
                related_delivery_id=None,
                note=NOTE,
            )
            print(f"  적음 {expense_id}  {due}")

        count, total = _verify(conn, sim_run_id=sim_run_id)
        print()
        print(f"검증  건수 {count} / 총액 {total:,} KRW")
        if count != EXPECTED_COUNT or total != EXPECTED_TOTAL:
            #  🔴 18건이 되면 실패다. 커밋하지 않고 되돌린다.
            conn.rollback()
            print(
                f"멈춤: 기대와 다릅니다 "
                f"(기대 {EXPECTED_COUNT} / {EXPECTED_TOTAL:,}). 되돌렸습니다."
            )
            return 4
        conn.commit()
        print("커밋했습니다. 현금은 움직이지 않았습니다 — 지급은 걷기가 지급일에 합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
