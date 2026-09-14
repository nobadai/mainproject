"""기준일 시점의 채권 상태를 복원한다. **미래 수금은 과거 화면에 들어가지 않는다.**

🔴 **`receivables` 행은 현재 상태만 들고 있다.** 수금이 적용되면 그 행의
   `received_amount_krw` · `outstanding_amount_krw` · `status` 가 **덮여** 쓰인다.
   그래서 `issued_date <= as_of` 로 행만 고르고 그 세 칸을 그대로 읽으면,
   2026-01-26 화면이 2026-02-06 에 들어온 돈을 이미 받은 것으로 보여 준다.

   V13 실측이다.

   ```text
   as_of        채권   행을 그대로 읽은 미수        그날의 사실
   2026-01-26     8건                   0        10,196,778
   2026-03-10    24건             508,182         8,612,378
   2026-03-31    42건           9,762,999         9,762,999   <- 오늘이라 일치한다
   ```

★ **사실은 `master_collection_events` 에 있다.** 그 표는 덮어쓰이지 않고 날짜를 들고
  있어, `collection_date <= as_of` 인 사건만 쓰면 그날의 상태가 복원된다.

★ **`target_received_total_krw` 는 누적값이다** — 증분이 아니다. 정본은
  `app.finance.collection.build_collection_transition` 이고, 그 함수가
  `delta = target - current` 로 전이를 만든다. 그래서 여기서는 **합이 아니라
  기준일 이하의 마지막 누적값**을 쓴다.

🔴 **판매에도 같은 규칙이 있다** (`app.sales.receivable_history`). 두 도메인은 서로를
   import 하지 않는 것이 계약이라(`test_sales_never_imports_the_finance_agent`) 규칙을
   한 벌로 둘 자리가 지금은 없다. 대신 **두 벌이 갈리는 순간 빨간불이 뜨도록**
   `tests/finance/test_receivable_history.py` 가 두 모듈의 SQL 과 상태 규칙을 대조한다.
   제자리는 공용 계약 패키지 승격이고, 그건 이 판의 소유 범위 밖이다.
"""

from decimal import Decimal

from psycopg import sql

_ZERO = Decimal(0)

#: 기준일 이하의 마지막 누적 수금액을 붙이는 조각. **`r` 이라는 채권 별칭을 전제한다.**
#:
#: ⚠️ 같은 날 사건이 여럿이면 **누적값이 가장 큰 것**을 고른다. 누적은 되돌아가지
#:   않는 값이라(`cumulative collection cannot regress`) 그날의 마지막 상태와 같다.
#:   날짜만으로 정렬하면 어느 행이 뽑힐지 실행마다 달라진다.
_HISTORY_JOIN = """
        LEFT JOIN LATERAL (
            SELECT event.target_received_total_krw
            FROM {schema}.master_collection_events AS event
            WHERE event.sim_run_id = r.sim_run_id
              AND event.receivable_id = r.receivable_id
              AND event.collection_date <= %s
            ORDER BY event.collection_date DESC, event.target_received_total_krw DESC
            LIMIT 1
        ) AS collected ON TRUE
"""

#: 복원한 세 칸. 행이 이미 고른 원금과 짝지어 읽는다.
_HISTORY_COLUMNS = """
               COALESCE(collected.target_received_total_krw, 0) AS received_amount_krw,
               r.original_amount_krw - COALESCE(collected.target_received_total_krw, 0)
                   AS outstanding_amount_krw
"""


def history_join(schema: str) -> sql.Composed:
    """기준일 이하 마지막 누적 수금액을 붙이는 JOIN. **`%s` 하나를 소비한다.**"""
    return sql.SQL(_HISTORY_JOIN).format(schema=sql.Identifier(schema))


def history_columns() -> sql.SQL:
    """복원한 `received_amount_krw` · `outstanding_amount_krw` 두 칸."""
    return sql.SQL(_HISTORY_COLUMNS)


def projected_status(*, original_amount_krw: Decimal, received_amount_krw: Decimal) -> str:
    """그 시점의 채권 상태. **정본은 수금 전이 규칙(`build_collection_transition`)이다.**

    ```text
    받은 것이 없다      OPEN
    일부만 받았다       PARTIAL
    전액 받았다         COLLECTED
    ```

    🔴 **0 과 «없음» 을 가르지 않는다** — 여기 오는 값은 이미 복원된 금액이고,
       0 은 *"그날까지 한 푼도 안 들어왔다"* 는 사실이다.
    """
    #  ★ 완납을 **먼저** 본다. 원금이 0원인 채권은 받을 것이 없어 처음부터 끝난
    #    상태이고, 0 을 먼저 보면 그 채권이 영원히 OPEN 으로 남는다.
    if received_amount_krw >= original_amount_krw:
        return "COLLECTED"
    if received_amount_krw <= _ZERO:
        return "OPEN"
    return "PARTIAL"
