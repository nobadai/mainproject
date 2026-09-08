"""Logistics adapter for Sales-confirmed outbound reservation requests."""

from __future__ import annotations

from typing import Any

from app.contracts.sales_logistics import SalesOutboundReservationRequest
from app.logistics.outbound import ReservationResult, reserve_stock


def reserve_confirmed_sale(
    conn: Any,
    request: SalesOutboundReservationRequest,
) -> ReservationResult:
    """Reserve stock for a confirmed sale without allocating or shipping any Lot."""

    return reserve_stock(
        conn,
        reservation_id=request.reservation_id,
        sim_run_id=request.sim_run_id,
        item_id=request.item_id,
        required_qty_kg=request.quantity_kg,
        sale_id=request.sale_id,
        as_of=request.as_of,
    )
