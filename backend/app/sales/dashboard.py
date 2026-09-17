"""Sales 화면용 DB 조회와 dashboard 응답 조립."""

from datetime import date
from decimal import Decimal

from psycopg import sql

from app.sales.db import fetch_all, fetch_one, get_db_schema
from app.sales.receivable_history import history_columns, history_join, projected_status
from app.sales.schemas import (
    SalesCollectionStatusSummary,
    SalesDashboardMeta,
    SalesDashboardResponse,
    SalesDashboardSummary,
    SalesHistoryItem,
    SalesItemSummary,
    SalesReceivableItem,
    TodayConfirmedSaleItem,
)


def load_sales_dashboard_meta(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    query = sql.SQL(
        """
        SELECT sim_run_id, %s::date AS as_of, run_type AS data_type
        FROM {}.sim_runs
        WHERE sim_run_id = %s
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_one(query, [as_of, sim_run_id])


def load_sales_summary(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    """이 실행의 판매 합계. **미수는 기준일 시점으로 복원한다.**

    🔴 `receivables` 의 수금 칸을 그대로 더하면 과거 기준일 KPI 에 미래 수금이 실린다
       (`app.sales.receivable_history`).
    """
    schema = get_db_schema()
    query = (
        sql.SQL(
            """
        SELECT
            COUNT(*)::int AS sales_count,
            COUNT(DISTINCT s.customer_partner_id)::int AS customer_count,
            COALESCE(SUM(s.total_quantity_kg), 0) AS total_sales_quantity_kg,
            COALESCE(SUM(s.total_amount_krw), 0) AS total_sales_amount_krw,
            COALESCE(SUM(s.contribution_profit_krw), 0) AS contribution_profit_krw,
            COALESCE(SUM(COALESCE(collected.target_received_total_krw, 0)), 0)
                AS received_amount_krw,
            COALESCE(
                SUM(r.original_amount_krw - COALESCE(collected.target_received_total_krw, 0)),
                0
            ) AS outstanding_receivables_krw
        FROM {}.sales AS s
        LEFT JOIN {}.receivables AS r
          ON r.sale_id = s.sale_id
         AND r.sim_run_id = s.sim_run_id
         AND r.issued_date <= %s
        """
        )
        .format(sql.Identifier(schema), sql.Identifier(schema))
        + history_join(schema)
        + sql.SQL(
            """
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        """
        )
    )
    #  ⚠️ 발행일 · 수금 복원 기준일 · 실행 축 · 판매일 순이다.
    return fetch_one(query, [as_of, as_of, sim_run_id, as_of])


def load_collection_summary(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT
            s.collection_status,
            COUNT(*)::int AS count,
            COALESCE(SUM(s.total_amount_krw), 0) AS sales_amount_krw
        FROM {}.sales AS s
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        GROUP BY s.collection_status
        ORDER BY s.collection_status
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of])


def load_item_summaries(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT
            si.item_id,
            i.item_name,
            COUNT(*)::int AS line_count,
            COALESCE(SUM(si.quantity_kg), 0) AS total_quantity_kg,
            COALESCE(SUM(si.line_amount_krw), 0) AS sales_amount_krw,
            COALESCE(SUM(si.contribution_profit_krw), 0) AS contribution_profit_krw,
            COALESCE(SUM(si.line_amount_krw) / NULLIF(SUM(si.quantity_kg), 0), 0)
                AS avg_unit_price_krw_per_kg
        FROM {}.sale_items AS si
        JOIN {}.sales AS s
          ON s.sale_id = si.sale_id
        JOIN {}.items AS i
          ON i.item_id = si.item_id
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        GROUP BY si.item_id, i.item_name
        ORDER BY sales_amount_krw DESC, si.item_id
        """
    ).format(sql.Identifier(schema), sql.Identifier(schema), sql.Identifier(schema))
    return fetch_all(query, [sim_run_id, as_of])


def load_recent_sales(*, sim_run_id: str, as_of: date, limit: int) -> list[dict[str, object]]:
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT
            s.sale_id,
            s.order_date,
            s.sale_date,
            s.customer_partner_id,
            p.partner_name,
            s.total_quantity_kg,
            s.total_amount_krw,
            s.contribution_profit_krw,
            s.collection_due_date,
            s.collection_status,
            s.order_status
        FROM {}.sales AS s
        LEFT JOIN {}.partners AS p
          ON p.partner_id = s.customer_partner_id
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        ORDER BY s.sale_date DESC, s.sale_id DESC
        LIMIT %s
        """
    ).format(sql.Identifier(schema), sql.Identifier(schema))
    return fetch_all(query, [sim_run_id, as_of, limit])


def load_today_confirmed_sales(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    """기준일에 확정된 판매 원장 품목 행을 읽는다.

    판매 확정일(``order_date``)과 납품일(``sale_date``)은 다른 사실이다. 이 목록은
    오늘 판매하기로 확정한 주문을 보여 주므로 미래 납품 건도 포함한다. 납품/실적 기준
    dashboard 집계는 ``load_recent_sales`` 등 기존 read model이 계속 소유한다.
    """
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT
            s.sale_id,
            s.order_date,
            s.sale_date,
            s.customer_partner_id,
            p.partner_name,
            si.item_id,
            i.item_name,
            si.quantity_kg,
            si.unit_price_krw_per_kg,
            si.line_amount_krw,
            s.order_status
        FROM {}.sales AS s
        JOIN {}.sale_items AS si
          ON si.sale_id = s.sale_id
        LEFT JOIN {}.items AS i
          ON i.item_id = si.item_id
        LEFT JOIN {}.partners AS p
          ON p.partner_id = s.customer_partner_id
        WHERE s.sim_run_id = %s
          AND s.order_date = %s
          AND s.order_status IN ('CONFIRMED', 'DELIVERED')
        ORDER BY s.sale_id DESC, si.item_id
        """
    ).format(
        sql.Identifier(schema),
        sql.Identifier(schema),
        sql.Identifier(schema),
        sql.Identifier(schema),
    )
    return fetch_all(query, [sim_run_id, as_of])


def load_sales_receivables(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    schema = get_db_schema()
    query = (
        sql.SQL(
            """
        SELECT
            r.receivable_id,
            r.sale_id,
            s.sale_date,
            s.customer_partner_id,
            p.partner_name,
            r.issued_date,
            r.due_date,
            r.original_amount_krw,
        """
        )
        + history_columns()
        + sql.SQL(
            """
        FROM {}.receivables AS r
        JOIN {}.sales AS s
          ON s.sale_id = r.sale_id
         AND s.sim_run_id = r.sim_run_id
        LEFT JOIN {}.partners AS p
          ON p.partner_id = s.customer_partner_id
        """
        ).format(sql.Identifier(schema), sql.Identifier(schema), sql.Identifier(schema))
        + history_join(schema)
        + sql.SQL(
            """
        WHERE r.sim_run_id = %s
          AND r.issued_date <= %s
          AND s.sale_date <= %s
        ORDER BY r.due_date ASC, r.receivable_id ASC
        """
        )
    )
    #  ⚠️ `%s` 는 네 개다 — LATERAL 의 기준일이 WHERE 보다 **먼저** 온다.
    return fetch_all(query, [as_of, sim_run_id, as_of, as_of])


_ZERO = Decimal(0)
_COLLECTION_LABELS = {
    "COLLECTED": "수금 완료",
    "PARTIAL": "일부 수금",
    "OPEN": "수금 예정",
}


def get_sales_dashboard(
    *, sim_run_id: str, as_of: date, recent_limit: int = 10
) -> SalesDashboardResponse:
    meta = load_sales_dashboard_meta(sim_run_id=sim_run_id, as_of=as_of)
    summary = load_sales_summary(sim_run_id=sim_run_id, as_of=as_of) or {}
    total_sales = _decimal(summary.get("total_sales_amount_krw"))
    profit = _decimal(summary.get("contribution_profit_krw"))

    return SalesDashboardResponse(
        meta=SalesDashboardMeta(
            sim_run_id=sim_run_id,
            as_of=as_of,
            data_type=None if meta is None else str(meta["data_type"]),
        ),
        summary=SalesDashboardSummary(
            sales_count=int(summary.get("sales_count") or 0),
            customer_count=int(summary.get("customer_count") or 0),
            total_sales_quantity_kg=_decimal(summary.get("total_sales_quantity_kg")),
            total_sales_amount_krw=total_sales,
            contribution_profit_krw=profit,
            contribution_margin_pct=_pct(profit, total_sales),
            received_amount_krw=_decimal(summary.get("received_amount_krw")),
            outstanding_receivables_krw=_decimal(summary.get("outstanding_receivables_krw")),
        ),
        collection_summary=_collection_summary(
            load_collection_summary(sim_run_id=sim_run_id, as_of=as_of)
        ),
        items=_items(load_item_summaries(sim_run_id=sim_run_id, as_of=as_of)),
        recent_sales=_recent_sales(
            load_recent_sales(
                sim_run_id=sim_run_id, as_of=as_of, limit=recent_limit
            )
        ),
        today_confirmed_sales=_today_confirmed_sales(
            load_today_confirmed_sales(sim_run_id=sim_run_id, as_of=as_of)
        ),
        receivables=_receivables(
            load_sales_receivables(sim_run_id=sim_run_id, as_of=as_of),
            as_of=as_of,
        ),
    )


def _collection_summary(rows: list[dict[str, object]]) -> dict[str, SalesCollectionStatusSummary]:
    result = {
        status: SalesCollectionStatusSummary(count=0, sales_amount_krw=_ZERO)
        for status in ("COLLECTED", "PARTIAL", "OPEN")
    }
    for row in rows:
        status = str(row["collection_status"])
        result[status] = SalesCollectionStatusSummary(
            count=int(row.get("count") or 0),
            sales_amount_krw=_decimal(row.get("sales_amount_krw")),
        )
    return result


def _items(rows: list[dict[str, object]]) -> list[SalesItemSummary]:
    items = []
    for row in rows:
        amount = _decimal(row["sales_amount_krw"])
        profit = _decimal(row["contribution_profit_krw"])
        items.append(
            SalesItemSummary(
                item_id=str(row["item_id"]),
                item_name=str(row["item_name"]),
                line_count=int(row["line_count"]),
                total_quantity_kg=_decimal(row["total_quantity_kg"]),
                sales_amount_krw=amount,
                contribution_profit_krw=profit,
                contribution_margin_pct=_pct(profit, amount),
                avg_unit_price_krw_per_kg=_decimal(row["avg_unit_price_krw_per_kg"]),
            )
        )
    return items


def _recent_sales(rows: list[dict[str, object]]) -> list[SalesHistoryItem]:
    result = []
    for row in rows:
        amount = _decimal(row["total_amount_krw"])
        profit = _decimal(row["contribution_profit_krw"])
        status = str(row["collection_status"])
        result.append(
            SalesHistoryItem(
                sale_id=str(row["sale_id"]),
                order_date=row["order_date"],
                sale_date=row["sale_date"],
                customer_partner_id=str(row["customer_partner_id"]),
                partner_name=None if row.get("partner_name") is None else str(row["partner_name"]),
                total_quantity_kg=_decimal(row["total_quantity_kg"]),
                total_amount_krw=amount,
                contribution_profit_krw=profit,
                contribution_margin_pct=_pct(profit, amount),
                collection_due_date=row["collection_due_date"],
                collection_status=status,
                collection_status_label=_COLLECTION_LABELS.get(status, status),
                order_status=str(row["order_status"]),
            )
        )
    return result


def _today_confirmed_sales(rows: list[dict[str, object]]) -> list[TodayConfirmedSaleItem]:
    return [
        TodayConfirmedSaleItem(
            sale_id=str(row["sale_id"]),
            order_date=row["order_date"],
            sale_date=row["sale_date"],
            customer_partner_id=str(row["customer_partner_id"]),
            partner_name=None if row.get("partner_name") is None else str(row["partner_name"]),
            item_id=str(row["item_id"]),
            item_name=None if row.get("item_name") is None else str(row["item_name"]),
            quantity_kg=_decimal(row["quantity_kg"]),
            unit_price_krw_per_kg=_decimal(row["unit_price_krw_per_kg"]),
            line_amount_krw=_decimal(row["line_amount_krw"]),
            order_status=str(row["order_status"]),
        )
        for row in rows
    ]


def _receivables(rows: list[dict[str, object]], *, as_of: date) -> list[SalesReceivableItem]:
    result = []
    for row in rows:
        #  🔴 저장된 status 는 덮여 쓰인다. 복원한 금액에서 다시 세운다.
        status = projected_status(
            original_amount_krw=_decimal(row["original_amount_krw"]),
            received_amount_krw=_decimal(row["received_amount_krw"]),
        )
        due_date = row["due_date"]
        outstanding = _decimal(row["outstanding_amount_krw"])
        display_status = (
            "연체"
            if due_date < as_of and outstanding > 0
            else _COLLECTION_LABELS.get(status, status)
        )
        result.append(
            SalesReceivableItem(
                receivable_id=str(row["receivable_id"]),
                sale_id=str(row["sale_id"]),
                sale_date=row["sale_date"],
                customer_partner_id=str(row["customer_partner_id"]),
                partner_name=None if row.get("partner_name") is None else str(row["partner_name"]),
                issued_date=row["issued_date"],
                due_date=due_date,
                original_amount_krw=_decimal(row["original_amount_krw"]),
                received_amount_krw=_decimal(row["received_amount_krw"]),
                outstanding_amount_krw=outstanding,
                status=status,
                display_status=display_status,
                d_day=None if status == "COLLECTED" else (due_date - as_of).days,
            )
        )
    return result


def _decimal(value: object) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    if whole == 0:
        return _ZERO
    return (part / whole * Decimal(100)).quantize(Decimal("0.01"))
