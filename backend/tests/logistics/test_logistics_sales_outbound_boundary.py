from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.contracts.sales_logistics import SalesOutboundReservationRequest
from app.logistics.outbound import ReservationResult
from app.logistics.sales_outbound import reserve_confirmed_sale


def test_sales_reservation_request_calls_existing_reserve_stock(monkeypatch):
    captured = {}

    def fake_reserve_stock(conn, **kwargs):
        captured["conn"] = conn
        captured.update(kwargs)
        return ReservationResult(
            applied=True,
            reservation_id=kwargs["reservation_id"],
            status="RESERVED",
            required_qty_kg=kwargs["required_qty_kg"],
            # ★ `reserve_stock` 은 **전량 확보**라 둘이 늘 같다. 가짜도 그 계약을 흉내낸다.
            reserved_qty_kg=kwargs["required_qty_kg"],
        )

    monkeypatch.setattr("app.logistics.sales_outbound.reserve_stock", fake_reserve_stock)
    conn = object()
    request = SalesOutboundReservationRequest(
        reservation_id="RSV-SI-SALE-1-1",
        sim_run_id="SIM-1",
        sale_id="SALE-1",
        sale_item_id="SI-SALE-1-1",
        item_id="ITEM-BAECHU",
        quantity_kg=Decimal(8500),
        as_of=date(2026, 9, 10),
    )

    result = reserve_confirmed_sale(conn, request)

    assert result.status == "RESERVED"
    assert captured == {
        "conn": conn,
        "reservation_id": "RSV-SI-SALE-1-1",
        "sim_run_id": "SIM-1",
        "item_id": "ITEM-BAECHU",
        "required_qty_kg": Decimal(8500),
        "sale_id": "SALE-1",
        "as_of": date(2026, 9, 10),
    }
