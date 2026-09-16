"""판매 추이 read model — **날짜별로 접은 판매 사실.**

★ 판매 현황 화면이 «언제 얼마나 팔렸나» 를 묻는데, 기존 read model 은 그 축이 없다.
  `console_partners` 는 거래처별로, `dashboard.load_item_summaries` 는 품목별로 접고,
  `load_recent_sales` 는 최근 몇 건만 준다 — **날짜로 접은 계열이 어디에도 없다.**

🔴 **화면이 접지 않는다.** 최근 판매 목록을 받아 프론트에서 날짜별로 더하면, 그 목록은
   `limit` 이 걸린 일부라 **합계가 조용히 틀린다.** 접는 일은 여기서 끝낸다.

🔴 **`sim_run_id` 에 기본값이 없다.** 빠뜨리면 전 실행이 한 그래프에 섞인다.
"""

from datetime import date
from decimal import Decimal

from psycopg import sql
from pydantic import BaseModel, ConfigDict

from app.sales.db import fetch_all, get_db_schema

#: 한 번에 돌려주는 최대 일수. 판매 실행이 분기 단위라 한 분기를 덮는다.
MAX_TREND_DAYS = 400


class SalesTrendPoint(BaseModel):
    """하루치 판매. **없는 날은 행이 없다** — 0 으로 채우지 않는다."""

    model_config = ConfigDict(extra="forbid")

    sale_date: date
    sales_count: int
    quantity_kg: Decimal
    sales_amount_krw: Decimal
    contribution_profit_krw: Decimal


class SalesTrendResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sim_run_id: str
    as_of: date
    rows: list[SalesTrendPoint]


def get_console_sales_trend(
    *,
    sim_run_id: str,
    as_of: date,
    days: int = MAX_TREND_DAYS,
    from_date: date | None = None,
    to_date: date | None = None,
) -> SalesTrendResponse:
    """이 실행의 날짜별 판매를 요청한 기간 안에서 읽는다."""
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValueError("from_date must not be after to_date")
    upper = as_of if to_date is None else min(to_date, as_of)
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT *
        FROM (
            SELECT
                s.sale_date,
                COUNT(*)::int AS sales_count,
                COALESCE(SUM(s.total_quantity_kg), 0) AS quantity_kg,
                COALESCE(SUM(s.total_amount_krw), 0) AS sales_amount_krw,
                COALESCE(SUM(s.contribution_profit_krw), 0) AS contribution_profit_krw
            FROM {}.sales AS s
            WHERE s.sim_run_id = %s
              AND s.sale_date <= %s
              AND (%s::date IS NULL OR s.sale_date >= %s)
              AND s.sale_date <= %s
            GROUP BY s.sale_date
            ORDER BY s.sale_date DESC
            LIMIT %s
        ) AS recent
        ORDER BY sale_date ASC
        """
    ).format(sql.Identifier(schema))
    rows = fetch_all(
        query,
        [sim_run_id, upper, from_date, from_date, upper, min(days, MAX_TREND_DAYS)],
    )
    return SalesTrendResponse(
        sim_run_id=sim_run_id,
        as_of=as_of,
        rows=[SalesTrendPoint(**row) for row in rows],
    )
