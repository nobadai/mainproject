"""Sales 승인 결과를 실제 Sales 원장으로 멱등 저장하는 계약."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.sales.db import get_db_schema
from app.sales.schemas import (
    SalesApprovalLine,
    SalesConfirmationInput,
    SalesExecutionIdentity,
    SalesScenario,
)

_ZERO_TOLERANCE = Decimal("0.1")


class SalesPersistenceConflict(RuntimeError):
    """같은 승인 축으로 다른 사실이 들어왔다."""


@dataclass(frozen=True)
class SaleItemWrite:
    sale_item_id: str
    sale_id: str
    item_id: str
    grade: str | None
    quantity_kg: Decimal
    unit_price_krw_per_kg: Decimal
    line_amount_krw: Decimal
    contribution_profit_krw: Decimal
    contribution_margin_rate: Decimal | None


@dataclass(frozen=True)
class SaleWritePlan:
    sale_id: str
    sim_run_id: str
    customer_partner_id: str
    order_date: date
    sale_date: date
    collection_due_date: date
    total_quantity_kg: Decimal
    total_amount_krw: Decimal
    contribution_profit_krw: Decimal
    collection_status: str
    source_order_id: str | None
    note: str | None
    order_status: str
    item_name: str
    sale_item: SaleItemWrite


@dataclass(frozen=True)
class SaleWriteResult:
    sale_id: str
    sale_item_id: str
    item_id: str
    quantity_kg: Decimal
    sales_written: int
    sale_items_written: int


def confirm_sale(conn: Any, request: SalesConfirmationInput) -> SaleWriteResult:
    """Sales 승인 결과를 계산하고 caller-owned connection으로 원장에 적는다."""

    return persist_sale(conn, build_sale_confirmation_plan(request))


def build_sale_confirmation_plan(request: SalesConfirmationInput) -> SaleWritePlan:
    """승인된 Sales 시나리오를 실제 원장 write plan 으로 고정한다."""

    identity = request.execution_identity
    if not identity.run_id:
        raise SalesPersistenceConflict("sales approval execution identity is missing run_id")
    scenario = request.selected_scenario
    if scenario.scenario_id != request.selected_scenario_id:
        raise SalesPersistenceConflict("selected scenario id does not match the scenario payload")
    if scenario.partner_id is None:
        raise SalesPersistenceConflict("selected scenario is missing partner_id")
    if scenario.quantity_kg is None or scenario.unit_price_krw is None:
        raise SalesPersistenceConflict("selected scenario is missing quantity or unit price")
    if scenario.sales_amount_krw is None:
        raise SalesPersistenceConflict("selected scenario is missing sales amount")
    if scenario.payment_terms_type != "SINGLE":
        raise SalesPersistenceConflict(
            "installment sale confirmations need an authoritative schedule"
        )
    if scenario.payment_days is None:
        raise SalesPersistenceConflict("single-term sale confirmations need payment_days")
    if request.line.item_name != scenario.item:
        raise SalesPersistenceConflict("sale line item does not match the selected scenario item")
    if request.line.quantity_kg != scenario.quantity_kg:
        raise SalesPersistenceConflict("sale line quantity does not match the selected scenario")
    if request.line.unit_price_krw_per_kg != scenario.unit_price_krw:
        raise SalesPersistenceConflict("sale line unit price does not match the selected scenario")

    total_profit = _line_profit(request.line, scenario)
    sale_id = sale_id_for(identity, scenario)
    sale_item_id = sale_item_id_for(sale_id, 1)
    due_date = request.sale_date + timedelta(days=scenario.payment_days)
    line = SaleItemWrite(
        sale_item_id=sale_item_id,
        sale_id=sale_id,
        item_id="",
        grade=request.line.grade,
        quantity_kg=scenario.quantity_kg,
        unit_price_krw_per_kg=scenario.unit_price_krw,
        line_amount_krw=scenario.sales_amount_krw,
        contribution_profit_krw=total_profit,
        contribution_margin_rate=_decimal_or_none(
            request.line.contribution_margin_rate
            if request.line.contribution_margin_rate is not None
            else scenario.contribution_margin_rate
        ),
    )
    return SaleWritePlan(
        sale_id=sale_id,
        sim_run_id=request.sim_run_id,
        customer_partner_id=scenario.partner_id,
        order_date=request.order_date,
        sale_date=request.sale_date,
        collection_due_date=due_date,
        total_quantity_kg=scenario.quantity_kg,
        total_amount_krw=scenario.sales_amount_krw,
        contribution_profit_krw=total_profit,
        collection_status="OPEN",
        source_order_id=request.source_order_id,
        note=request.note,
        order_status="CONFIRMED",
        item_name=scenario.item,
        sale_item=line,
    )


def persist_sale(conn: Any, plan: SaleWritePlan) -> SaleWriteResult:
    """부르는 쪽 connection 으로 Sales header/item 을 멱등 저장한다."""

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        item_id = _lookup_item_id(cursor, schema, plan.item_name)
        sale_item = plan.sale_item
        sale_item = SaleItemWrite(
            sale_item_id=sale_item.sale_item_id,
            sale_id=sale_item.sale_id,
            item_id=item_id,
            grade=sale_item.grade,
            quantity_kg=sale_item.quantity_kg,
            unit_price_krw_per_kg=sale_item.unit_price_krw_per_kg,
            line_amount_krw=sale_item.line_amount_krw,
            contribution_profit_krw=sale_item.contribution_profit_krw,
            contribution_margin_rate=sale_item.contribution_margin_rate,
        )
        header_written = _insert_sale(cursor, schema, plan)
        item_written = _insert_sale_item(cursor, schema, sale_item)
        if header_written == 0:
            _assert_same_sale(cursor, schema, plan)
        if item_written == 0:
            _assert_same_sale_item(cursor, schema, sale_item)
    return SaleWriteResult(
        sale_id=plan.sale_id,
        sale_item_id=sale_item.sale_item_id,
        item_id=sale_item.item_id,
        quantity_kg=sale_item.quantity_kg,
        sales_written=header_written,
        sale_items_written=item_written,
    )


def mark_sale_delivered(conn: Any, *, sale_id: str) -> bool:
    """Caller-owned completion hook after Logistics has shipped all sale_items."""

    if not isinstance(sale_id, str) or not sale_id.strip():
        raise SalesPersistenceConflict("sale_id must not be blank")
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.sales
                SET order_status = 'DELIVERED'
                WHERE sale_id = %s
                  AND order_status IN ('CONFIRMED', 'READY')
                """
            ).format(schema),
            [sale_id],
        )
        if cursor.rowcount == 1:
            return True
        cursor.execute(
            sql.SQL(
                """
                SELECT order_status
                FROM {}.sales
                WHERE sale_id = %s
                LIMIT 2
                """
            ).format(schema),
            [sale_id],
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise SalesPersistenceConflict(f"sale was not found: {sale_id}")
    status = _row_value(rows[0], "order_status", 0)
    if status == "DELIVERED":
        return False
    raise SalesPersistenceConflict(f"sale cannot be marked DELIVERED from {status!r}")


def sale_id_for(identity: SalesExecutionIdentity, scenario: SalesScenario) -> str:
    if not identity.run_id:
        raise SalesPersistenceConflict("sales approval execution identity is missing run_id")
    return f"SALE-{identity.run_id}-{scenario.scenario_id}"


def sale_item_id_for(sale_id: str, seq: int) -> str:
    return f"SI-{sale_id}-{seq}"


def _lookup_item_id(cursor: Any, schema: sql.Identifier, item_name: str) -> str:
    cursor.execute(
        sql.SQL(
            """
            SELECT item_id
            FROM {}.items
            WHERE item_name = %s
            LIMIT 2
            """
        ).format(schema),
        [item_name],
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SalesPersistenceConflict(f"item was not uniquely resolved: {item_name}")
    return str(_row_value(rows[0], "item_id", 0))


def _insert_sale(cursor: Any, schema: sql.Identifier, plan: SaleWritePlan) -> int:
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {}.sales (
                sale_id, sim_run_id, customer_partner_id, order_date, sale_date,
                collection_due_date, total_quantity_kg, total_amount_krw,
                contribution_profit_krw, collection_status, source_order_id, note,
                order_status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sale_id) DO NOTHING
            """
        ).format(schema),
        [
            plan.sale_id,
            plan.sim_run_id,
            plan.customer_partner_id,
            plan.order_date,
            plan.sale_date,
            plan.collection_due_date,
            plan.total_quantity_kg,
            plan.total_amount_krw,
            plan.contribution_profit_krw,
            plan.collection_status,
            plan.source_order_id,
            plan.note,
            plan.order_status,
        ],
    )
    return int(cursor.rowcount)


def _insert_sale_item(cursor: Any, schema: sql.Identifier, sale_item: SaleItemWrite) -> int:
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {}.sale_items (
                sale_item_id, sale_id, item_id, grade, quantity_kg,
                unit_price_krw_per_kg, line_amount_krw, contribution_profit_krw,
                contribution_margin_rate
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sale_item_id) DO NOTHING
            """
        ).format(schema),
        [
            sale_item.sale_item_id,
            sale_item.sale_id,
            sale_item.item_id,
            sale_item.grade,
            sale_item.quantity_kg,
            sale_item.unit_price_krw_per_kg,
            sale_item.line_amount_krw,
            sale_item.contribution_profit_krw,
            sale_item.contribution_margin_rate,
        ],
    )
    return int(cursor.rowcount)


def _assert_same_sale(cursor: Any, schema: sql.Identifier, plan: SaleWritePlan) -> None:
    cursor.execute(
        sql.SQL(
            """
            SELECT *
            FROM {}.sales
            WHERE sale_id = %s
            LIMIT 2
            """
        ).format(schema),
        [plan.sale_id],
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SalesPersistenceConflict(f"sale was not found after insert conflict: {plan.sale_id}")
    row = rows[0]
    expected = {
        "sale_id": plan.sale_id,
        "sim_run_id": plan.sim_run_id,
        "customer_partner_id": plan.customer_partner_id,
        "order_date": plan.order_date,
        "sale_date": plan.sale_date,
        "collection_due_date": plan.collection_due_date,
        "total_quantity_kg": plan.total_quantity_kg,
        "total_amount_krw": plan.total_amount_krw,
        "contribution_profit_krw": plan.contribution_profit_krw,
        "collection_status": plan.collection_status,
        "source_order_id": plan.source_order_id,
        "note": plan.note,
    }
    for key, value in expected.items():
        if _row_value(row, key) != value:
            raise SalesPersistenceConflict(f"conflicting sale row for {plan.sale_id}: {key}")
    status = _row_value(row, "order_status")
    if status not in {"CONFIRMED", "READY", "DELIVERED"}:
        raise SalesPersistenceConflict(
            f"conflicting sale row for {plan.sale_id}: order_status"
        )


def _assert_same_sale_item(cursor: Any, schema: sql.Identifier, sale_item: SaleItemWrite) -> None:
    cursor.execute(
        sql.SQL(
            """
            SELECT *
            FROM {}.sale_items
            WHERE sale_item_id = %s
            LIMIT 2
            """
        ).format(schema),
        [sale_item.sale_item_id],
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SalesPersistenceConflict(
            f"sale item was not found after insert conflict: {sale_item.sale_item_id}"
        )
    row = rows[0]
    expected = {
        "sale_item_id": sale_item.sale_item_id,
        "sale_id": sale_item.sale_id,
        "item_id": sale_item.item_id,
        "grade": sale_item.grade,
        "quantity_kg": sale_item.quantity_kg,
        "unit_price_krw_per_kg": sale_item.unit_price_krw_per_kg,
        "line_amount_krw": sale_item.line_amount_krw,
        "contribution_profit_krw": sale_item.contribution_profit_krw,
        "contribution_margin_rate": sale_item.contribution_margin_rate,
    }
    for key, value in expected.items():
        if _row_value(row, key) != value:
            raise SalesPersistenceConflict(
                f"conflicting sale item row for {sale_item.sale_item_id}: {key}"
            )


def _line_profit(line: SalesApprovalLine, scenario: SalesScenario) -> Decimal:
    value = line.contribution_profit_krw
    if value is None:
        value = scenario.contribution_margin_krw
    if value is None:
        raise SalesPersistenceConflict("selected scenario is missing contribution profit")
    return value


def _decimal_or_none(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value


def _row_value(row: Any, name: str, index: int = 0) -> Any:
    if isinstance(row, dict):
        return row[name]
    return row[index]
