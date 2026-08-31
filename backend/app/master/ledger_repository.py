"""번인(burn-in) 일별 마감 조회 — **에이전트가 판단하기 전에 회사가 어떻게 왔는가.**

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


def _table(name: str) -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(name))


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
