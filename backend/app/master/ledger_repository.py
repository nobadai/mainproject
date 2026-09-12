"""일별 마감(`daily_closings`) 조회 — **번인 구간과 걷기 구간의 현금 축.**

```text
get_burn_in(...)        번인 30일 전부 — 에이전트가 판단하기 전에 회사가 어떻게 왔는가
read_walk_closings(...) 걷기 구간의 현금 칸 — 걷기 요약의 「현금」·「현금항등식」 두 줄
```

★ **둘 다 읽기만 한다.** 이 모듈은 마감을 만들지도, 금액을 세지도 않는다.

---

**에이전트가 판단하기 전에 회사가 어떻게 왔는가.**

`sim_runs` 에 `SIM-BURNIN-202512` 가 `status=SEEDED` 로 심겨 있다.

```text
run_type     BURN_IN
기간         2025-12-02 ~ 12-31 (30일)
as_of        2025-12-31                    ← 에이전트가 처음 판단하는 날
note         "Agent 실행 전 30일 Persona 이력"
```

★ **읽기만 한다.** 이 모듈은 마감을 만들지 않는다 — 하루를 진행시키는 것은
  승인이 발주로 흘러가야 성립하고, 그건 각 파트의 상태 전이 로직이다
  (아직 없다 · 별도 이슈).

🔴 **왜 이 화면이 필요한가.** 에이전트가 12-31 에 *"살 안이 없다"* 고 답하는데,
  그 앞의 30일을 안 보면 **시스템이 고장 난 것처럼 읽힌다.** 무차입 현금이
  5,820만원에서 -1,328만원까지 떨어지고 미수금이 7,305만원 잠긴 회사에게
  *"지금 사지 마라"* 는 **정상 판단**이다. 결론만 보여주면 그 사실이 사라진다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg import sql

from app.finance.db import fetch_all, fetch_one, get_db_schema

#: 번인 구간의 시뮬레이션 키. 지금은 하나뿐이라 상수로 둔다 — 여러 개가 되면
#: 요청 파라미터로 올린다. **없는 값을 미리 만들지 않는다.**
BURN_IN_SIM_RUN_ID = "SIM-BURNIN-202512"

_CLOSING_COLUMNS = (
    "close_date",
    "day_no",
    "base_cash_balance_krw",
    "loan_cash_balance_krw",
    "receivables_balance_krw",
    "inventory_qty_kg",
    "sales_recognized_krw",
    "collection_cash_in_krw",
    "purchase_cash_out_krw",
    "closed",
)


#: 현금 축의 칸 이름. 🔴 **주인이 여기 하나다** (2026-09-12).
#:
#: ★ 걷기 요약이 이 이름들을 **손으로 다시 적지 않는다.** 적으면 표가 바뀌는 날
#:   요약만 옛 이름을 말하고, 그때 나는 것은 오류가 아니라 **조용한 0** 이다.
#:   `backtest_runner` 가 `LLM_STATUSES` 를 `envelope` 에서 들여오는 것과 같은 결이다.
CLOSE_DATE = "close_date"
PURCHASE_CASH_OUT = "purchase_cash_out_krw"
LOGISTICS_CASH_OUT = "logistics_cash_out_krw"
PAYROLL_INTEREST_CASH_OUT = "payroll_interest_cash_out_krw"
COLLECTION_CASH_IN = "collection_cash_in_krw"
NET_CASH = "base_net_cash_krw"
BASE_CASH_BALANCE = "base_cash_balance_krw"

#: 🔴 **대출을 포함한 곡선.** 현금 항등식에는 안 들어간다 — 차입·상환이 섞여
#:   축이 다르다. 그래도 **읽는다**: 읽지 않으면 *"안 섞었다"* 를 아무도 잴 수 없다.
LOAN_CASH_BALANCE = "loan_cash_balance_krw"

#: 걷기가 현금 축을 볼 때 읽는 칸 전부. **여기 있는 것만 읽는다.**
WALK_CASH_COLUMNS = (
    CLOSE_DATE,
    PURCHASE_CASH_OUT,
    LOGISTICS_CASH_OUT,
    PAYROLL_INTEREST_CASH_OUT,
    COLLECTION_CASH_IN,
    NET_CASH,
    BASE_CASH_BALANCE,
    LOAN_CASH_BALANCE,
)


def _table(name: str) -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(name))


def read_walk_closings(*, sim_run_id: str, start: date, end: date) -> tuple[dict[str, Any], ...]:
    """그 실행의 `start..end` 마감행. **날짜순으로. 읽기만 한다** (2026-09-12).

    🔴 **여기서 아무것도 안 센다.** 합도 차이도 잔액도 만들지 않는다 — 금액의
      주인은 `daily_closings` 한 곳이고, 마스터는 그 값을 **나르기만** 한다
      (`master/closing.py` 가 금액 칸을 하나도 안 든 것과 같은 규율).

    🔴 **범위를 SQL 이 건다.** 실행 하나에 번인 30일과 걷기 179일이 같이 앉을 수
      있고, 앞 구간의 행이 섞이면 **기초잔액이 그 앞 어딘가의 값**이 된다 —
      그러면 Δ잔액이 걷기의 것이 아니게 되고 항등식이 조용히 거짓말을 한다.

    ⚠️ **정렬도 여기가 정한다.** 부르는 쪽이 다시 정렬하면 순서의 주인이 둘이 된다.

    :returns: 마감행. **한 행도 없으면 빈 튜플이고 그것이 답이다** — 0 으로 메운
        행을 지어내지 않는다. *"마감이 안 돌았다"* 와 *"돌았는데 0 이다"* 는 다른
        사실이고, 지어내는 순간 그 둘이 화면에서 같아진다.
    """
    rows = fetch_all(
        sql.SQL(
            "SELECT {} FROM {} WHERE sim_run_id = %s AND close_date BETWEEN %s AND %s"
            " ORDER BY close_date"
        ).format(
            sql.SQL(", ").join(sql.Identifier(c) for c in WALK_CASH_COLUMNS),
            _table("daily_closings"),
        ),
        (sim_run_id, start, end),
    )
    return tuple(dict(row) for row in rows)


def get_burn_in(sim_run_id: str = BURN_IN_SIM_RUN_ID) -> dict[str, Any]:
    """번인 한 건과 그 일별 마감 전부.

    ★ **`closed` 를 지우지 않는다.** 마감되지 않은 날이 섞여 있으면 그 사실이
      답의 일부다 — 화면이 "아직 안 닫힌 날" 을 그대로 적을 수 있어야 한다.
    """
    run = fetch_one(
        sql.SQL(
            "SELECT sim_run_id, run_type, period_start, period_end, as_of, status,"
            " financing_mode, config_json, note FROM {} WHERE sim_run_id = %s"
        ).format(_table("sim_runs")),
        (sim_run_id,),
    )
    if run is None:
        raise LookupError(f"시뮬레이션을 찾을 수 없습니다: {sim_run_id}")

    closings = fetch_all(
        sql.SQL("SELECT {} FROM {} WHERE sim_run_id = %s ORDER BY close_date").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in _CLOSING_COLUMNS),
            _table("daily_closings"),
        ),
        (sim_run_id,),
    )
    return {"run": dict(run), "closings": [dict(row) for row in closings]}
