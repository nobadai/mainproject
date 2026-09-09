from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.sales.logistics_request as outbound_module
from app.sales.logistics_request import outbound_reservation_for_sale
from app.sales.persistence import SaleWriteResult


def test_confirmed_sale_result_builds_logistics_reservation_request():
    result = SaleWriteResult(
        sale_id="SALE-1",
        sale_item_id="SI-SALE-1-1",
        item_id="ITEM-BAECHU",
        quantity_kg=Decimal(8500),
        sales_written=1,
        sale_items_written=1,
    )

    request = outbound_reservation_for_sale(
        result,
        sim_run_id="SIM-1",
        as_of=date(2026, 9, 10),
    )

    assert request.reservation_id == "RSV-SI-SALE-1-1"
    assert request.sim_run_id == "SIM-1"
    assert request.sale_id == "SALE-1"
    assert request.sale_item_id == "SI-SALE-1-1"
    assert request.item_id == "ITEM-BAECHU"
    assert request.quantity_kg == Decimal(8500)
    assert request.as_of == date(2026, 9, 10)


def test_sales_outbound_boundary_rejects_missing_execution_axis():
    result = SaleWriteResult(
        sale_id="SALE-1",
        sale_item_id="SI-SALE-1-1",
        item_id="ITEM-BAECHU",
        quantity_kg=Decimal(8500),
        sales_written=1,
        sale_items_written=1,
    )

    with pytest.raises(ValueError, match="sim_run_id"):
        outbound_reservation_for_sale(result, sim_run_id="", as_of=date(2026, 9, 10))


def test_sales_outbound_boundary_does_not_call_logistics():
    source = Path(outbound_module.__file__).read_text(encoding="utf-8")

    assert "app.logistics" not in source
    for forbidden in ("reserve_confirmed_sale(", "reserve_confirmed_sale_available("):
        assert forbidden not in source

