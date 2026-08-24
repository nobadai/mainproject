"""현재 Inventory/Logistics Snapshot 조회 Repository."""

from decimal import Decimal

from psycopg import sql

from app.logistics.db import fetch_all, fetch_one, get_db_schema
from app.logistics.schemas import InventoryLogisticsSnapshot, InventoryLotSnapshot


def get_current_inventory_logistics_snapshot() -> InventoryLogisticsSnapshot:
    """현재 View에서 확인 가능한 재고 사실과 미결 물류 정책을 반환한다."""
    schema = sql.Identifier(get_db_schema())
    dashboard = fetch_one(
        sql.SQL("SELECT as_of, used_capacity_kg FROM {}.v_dashboard_state").format(schema)
    )
    if dashboard is None:
        raise LookupError("Current Inventory/Logistics Snapshot was not found")

    inventory_rows = fetch_all(
        sql.SQL(
            """
            SELECT
                lot_id,
                item_name,
                remaining_qty_kg,
                freshness_days_left,
                status,
                storage_zone
            FROM {}.v_current_inventory
            ORDER BY lot_id
            """
        ).format(schema)
    )
    capacity = fetch_one(
        sql.SQL(
            """
            SELECT
                guaranteed_capacity_plt,
                effective_kg_per_pallet,
                equivalent_capacity_ton,
                used_capacity_kg
            FROM {}.v_current_logistics_capacity
            """
        ).format(schema)
    )
    contracts = fetch_all(
        sql.SQL(
            """
            SELECT
                logistics_contract_id,
                guaranteed_capacity_plt,
                effective_kg_per_pallet,
                equivalent_capacity_ton,
                contract_status,
                provisional
            FROM {}.logistics_contracts
            ORDER BY logistics_contract_id
            """
        ).format(schema)
    )

    guaranteed_capacity_kg = _validated_guaranteed_capacity(capacity, contracts)
    evidence_refs = ["DB:v_dashboard_state", "DB:v_current_inventory"]
    if capacity is not None:
        evidence_refs.append("DB:v_current_logistics_capacity")
    evidence_refs.extend(
        f"DB:logistics_contracts/{row['logistics_contract_id']}:provisional={str(row['provisional']).lower()}"
        for row in contracts
    )
    lots = [
        InventoryLotSnapshot(
            lot_id=row["lot_id"],
            item=row["item_name"],
            available_qty_kg=row["remaining_qty_kg"],
            remaining_freshness_days=row["freshness_days_left"],
            status=row["status"],
            storage_zone=row["storage_zone"],
        )
        for row in inventory_rows
    ]
    used_capacity = (
        capacity["used_capacity_kg"] if capacity is not None else dashboard["used_capacity_kg"]
    )
    return InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=dashboard["as_of"],
        on_hand_by_lot=lots,
        in_transit=None,
        confirmed_inbound_schedule=None,
        confirmed_outbound_schedule=None,
        used_capacity_kg=used_capacity,
        guaranteed_capacity_kg=guaranteed_capacity_kg,
        burst_capacity_kg=None,
        guaranteed_capacity_by_zone_kg=None,
        inbound_lead_days=None,
        daily_inbound_capacity_kg=None,
        inbound_transport_capacity_kg=None,
        shared_daily_outbound_capacity_kg=None,
        evidence_refs=evidence_refs,
    )


def _validated_guaranteed_capacity(
    capacity: dict[str, object] | None,
    contracts: list[dict[str, object]],
) -> Decimal | None:
    """현재 Capacity View와 유일하게 일치하는 확정 계약의 kg Capacity만 반환한다."""
    if capacity is None:
        return None
    matches = [
        contract
        for contract in contracts
        if contract["guaranteed_capacity_plt"] == capacity["guaranteed_capacity_plt"]
        and contract["effective_kg_per_pallet"] == capacity["effective_kg_per_pallet"]
        and contract["equivalent_capacity_ton"] == capacity["equivalent_capacity_ton"]
    ]
    if len(matches) != 1 or matches[0]["provisional"] is not False:
        return None
    value = capacity["equivalent_capacity_ton"]
    if not isinstance(value, Decimal):
        raise TypeError("equivalent_capacity_ton must be a Decimal")
    return value * Decimal(1000)
