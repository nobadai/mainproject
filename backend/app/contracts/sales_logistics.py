"""Sales confirmation facts handed to Logistics outbound boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class SalesOutboundReservationRequest:
    """Confirmed sale quantity that Logistics may reserve without choosing a Lot."""

    reservation_id: str
    sim_run_id: str
    sale_id: str
    sale_item_id: str
    item_id: str
    quantity_kg: Decimal
    as_of: date


def reservation_id_for_sale_item(sale_item_id: str) -> str:
    """Deterministic reservation identity owned by the sale item fact."""

    if not isinstance(sale_item_id, str) or not sale_item_id.strip():
        raise ValueError("sale_item_id must not be blank")
    return f"RSV-{sale_item_id}"
