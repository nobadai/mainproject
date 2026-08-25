"""현재 Inventory/Logistics Snapshot 조회 Repository."""

from decimal import Decimal

from psycopg import sql

from app.logistics.db import fetch_all, fetch_one, get_db_schema
from app.logistics.schemas import (
    InventoryLogisticsSnapshot,
    InventoryLotSnapshot,
    LogisticsPolicy,
)

LOGISTICS_POLICY_VERSION = "v1.3-PROVISIONAL"
LOGISTICS_POLICY_USAGE_SCOPE = "AGENT_MVP_DEMO"
_NUMERIC_POLICY_KEYS = {
    "guaranteed_capacity_kg",
    "burst_capacity_kg",
    "inbound_lead_days",
    "daily_inbound_capacity_kg",
    "inbound_transport_capacity_kg",
    "shared_daily_outbound_capacity_kg",
}
_TEXT_POLICY_KEYS = {"cap_by_date_policy"}
_REQUIRED_POLICY_KEYS = _NUMERIC_POLICY_KEYS | _TEXT_POLICY_KEYS


def get_active_logistics_policy() -> LogisticsPolicy:
    """현재 Logistics MVP 범위의 active policy를 typed contract로 조회한다."""
    query = sql.SQL(
        """
        SELECT
            policy_key,
            value_kind,
            value_numeric,
            value_text,
            value_json,
            source_ref,
            policy_version,
            usage_scope
        FROM {}.agent_policy_config
        WHERE domain = %s
          AND policy_version = %s
          AND usage_scope = %s
          AND is_active = TRUE
        """
    ).format(sql.Identifier(get_db_schema()))
    rows = fetch_all(
        query,
        ["logistics", LOGISTICS_POLICY_VERSION, LOGISTICS_POLICY_USAGE_SCOPE],
    )
    return _build_logistics_policy(rows)


def _build_logistics_policy(rows: list[dict[str, object]]) -> LogisticsPolicy:
    values: dict[str, object] = {}
    source_refs: dict[str, str] = {}
    for row in rows:
        key = row.get("policy_key")
        if key not in _REQUIRED_POLICY_KEYS:
            continue
        if key in values:
            raise ValueError(f"Duplicate Logistics policy key: {key}")
        if row.get("policy_version") != LOGISTICS_POLICY_VERSION:
            raise ValueError(f"Logistics policy_version mismatch: {key}")
        if row.get("usage_scope") != LOGISTICS_POLICY_USAGE_SCOPE:
            raise ValueError(f"Logistics policy usage_scope mismatch: {key}")

        kind = row.get("value_kind")
        expected_kind = "NUMERIC" if key in _NUMERIC_POLICY_KEYS else "TEXT"
        if kind != expected_kind:
            raise ValueError(f"Invalid value_kind for Logistics policy {key}: {kind}")
        selected_column = "value_numeric" if kind == "NUMERIC" else "value_text"
        unused_columns = {"value_numeric", "value_text", "value_json"} - {selected_column}
        value = row.get(selected_column)
        if value is None or any(row.get(column) is not None for column in unused_columns):
            raise ValueError(f"Inconsistent value columns for Logistics policy: {key}")
        if kind == "NUMERIC" and (isinstance(value, bool) or not isinstance(value, Decimal)):
            raise TypeError(f"Invalid Python NUMERIC value for Logistics policy: {key}")
        if kind == "TEXT" and not isinstance(value, str):
            raise TypeError(f"Invalid Python TEXT value for Logistics policy: {key}")

        source_ref = row.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise ValueError(f"Missing source_ref for Logistics policy: {key}")
        values[key] = value
        source_refs[key] = source_ref

    missing = _REQUIRED_POLICY_KEYS - values.keys()
    if missing:
        raise LookupError(
            f"Required Logistics policies were not found: {', '.join(sorted(missing))}"
        )

    inbound_lead_days = values["inbound_lead_days"]
    assert isinstance(inbound_lead_days, Decimal)
    if inbound_lead_days != inbound_lead_days.to_integral_value():
        raise ValueError("Logistics policy must be an integer: inbound_lead_days")
    values["inbound_lead_days"] = int(inbound_lead_days)
    return LogisticsPolicy(
        **values,
        policy_version=LOGISTICS_POLICY_VERSION,
        usage_scope=LOGISTICS_POLICY_USAGE_SCOPE,
        source_refs=source_refs,
    )


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
