"""확정 판매 결과를 Logistics 예약 요청 계약으로 옮기는 Sales 소유 projection."""

from __future__ import annotations

from datetime import date

from app.contracts.sales_logistics import (
    SalesOutboundReservationRequest,
    reservation_id_for_sale_item,
)
from app.sales.persistence import SaleWriteResult


def outbound_reservation_for_sale(
    result: SaleWriteResult,
    *,
    sim_run_id: str,
    as_of: date,
) -> SalesOutboundReservationRequest:
    """FEFO Lot 선택이나 출고 실행 없이 예약 요청만 만든다."""

    if not isinstance(sim_run_id, str) or not sim_run_id.strip():
        raise ValueError("sim_run_id must not be blank")
    return SalesOutboundReservationRequest(
        reservation_id=reservation_id_for_sale_item(result.sale_item_id),
        sim_run_id=sim_run_id,
        sale_id=result.sale_id,
        sale_item_id=result.sale_item_id,
        item_id=result.item_id,
        quantity_kg=result.quantity_kg,
        as_of=as_of,
    )
