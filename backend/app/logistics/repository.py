"""Inventory/Logistics Policy 및 T0 Runtime Snapshot Repository."""

from datetime import date
from decimal import Decimal

from psycopg import sql

from app.logistics.db import fetch_all, get_db_schema
from app.logistics.schemas import (
    InventoryLogisticsSnapshot,
    InventoryLotSnapshot,
    LogisticsPolicy,
    LogisticsRuntimeFixture,
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


def get_active_logistics_runtime_fixture(*, as_of: date) -> LogisticsRuntimeFixture:
    """요청 기준일과 정확히 일치하는 active MVP runtime fixture 한 건을 조회한다."""
    schema = sql.Identifier(get_db_schema())
    rows = fetch_all(
        sql.SQL(
            """
            SELECT
                fixture_id,
                sim_run_id,
                as_of,
                in_transit_status,
                in_transit_json,
                confirmed_inbound_status,
                confirmed_inbound_json,
                confirmed_outbound_status,
                confirmed_outbound_json,
                usage_scope,
                evidence_grade,
                source_ref,
                approved_by
            FROM {}.logistics_runtime_fixture
            WHERE usage_scope = %s
              AND as_of = %s
              AND is_active = TRUE
            ORDER BY fixture_id
            """
        ).format(schema),
        [LOGISTICS_POLICY_USAGE_SCOPE, as_of],
    )
    if len(rows) != 1:
        raise LookupError(
            f"Expected exactly one active Logistics runtime fixture, found {len(rows)}"
        )
    return _build_logistics_runtime_fixture(rows[0], expected_as_of=as_of)


def _build_logistics_runtime_fixture(
    row: dict[str, object], *, expected_as_of: date
) -> LogisticsRuntimeFixture:
    if row.get("as_of") != expected_as_of:
        raise ValueError("Logistics runtime fixture as_of mismatch")
    if row.get("usage_scope") != LOGISTICS_POLICY_USAGE_SCOPE:
        raise ValueError("Logistics runtime fixture usage_scope mismatch")
    return LogisticsRuntimeFixture(
        fixture_id=row.get("fixture_id"),
        sim_run_id=row.get("sim_run_id"),
        as_of=row.get("as_of"),
        in_transit_status=row.get("in_transit_status"),
        in_transit=row.get("in_transit_json"),
        confirmed_inbound_status=row.get("confirmed_inbound_status"),
        confirmed_inbound_schedule=row.get("confirmed_inbound_json"),
        confirmed_outbound_status=row.get("confirmed_outbound_status"),
        confirmed_outbound_schedule=row.get("confirmed_outbound_json"),
        usage_scope=row.get("usage_scope"),
        evidence_grade=row.get("evidence_grade"),
        source_ref=row.get("source_ref"),
        approved_by=row.get("approved_by"),
    )


def get_current_inventory_logistics_snapshot(*, as_of: date) -> InventoryLogisticsSnapshot:
    """Fixture, direct physical lots, Policy를 한 번 읽어 고정 T0 Snapshot을 만든다."""
    fixture = get_active_logistics_runtime_fixture(as_of=as_of)
    policy = get_active_logistics_policy()
    schema = sql.Identifier(get_db_schema())

    inventory_rows = fetch_all(
        sql.SQL(
            """
            SELECT
                l.lot_id,
                i.item_name,
                l.grade,
                l.received_at,
                l.remaining_qty_kg,
                l.status,
                l.storage_zone,
                p.operational_limit_days,
                p.medium_grade_factor
            FROM {}.inventory_lots l
            JOIN {}.items i ON i.item_id = l.item_id
            JOIN {}.item_storage_policies p ON p.item_id = l.item_id
            WHERE l.sim_run_id = %s
              AND l.received_at <= %s
              AND l.status = 'ACTIVE'
              AND l.remaining_qty_kg > 0
            ORDER BY l.lot_id
            """
        ).format(schema, schema, schema),
        [fixture.sim_run_id, fixture.as_of],
    )

    lots = [_inventory_lot_from_row(row, as_of=fixture.as_of) for row in inventory_rows]
    used_capacity = sum((lot.available_qty_kg for lot in lots), start=Decimal(0))
    return InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=fixture.as_of,
        on_hand_by_lot=lots,
        in_transit=fixture.in_transit,
        confirmed_inbound_schedule=fixture.confirmed_inbound_schedule,
        confirmed_outbound_schedule=fixture.confirmed_outbound_schedule,
        used_capacity_kg=used_capacity,
        guaranteed_capacity_kg=policy.guaranteed_capacity_kg,
        burst_capacity_kg=policy.burst_capacity_kg,
        guaranteed_capacity_by_zone_kg=None,
        inbound_lead_days=policy.inbound_lead_days,
        daily_inbound_capacity_kg=policy.daily_inbound_capacity_kg,
        inbound_transport_capacity_kg=policy.inbound_transport_capacity_kg,
        shared_daily_outbound_capacity_kg=policy.shared_daily_outbound_capacity_kg,
        evidence_refs=[
            f"DB:logistics_runtime_fixture/{fixture.fixture_id}",
            fixture.source_ref,
            f"DB:inventory_lots/sim_run_id={fixture.sim_run_id}",
            *policy.source_refs.values(),
        ],
    )


def _inventory_lot_from_row(row: dict[str, object], *, as_of: date) -> InventoryLotSnapshot:
    received_at = row.get("received_at")
    quantity = row.get("remaining_qty_kg")
    operational_limit = row.get("operational_limit_days")
    medium_factor = row.get("medium_grade_factor")
    if not isinstance(received_at, date):
        raise TypeError("Inventory lot received_at must be a date")
    if isinstance(quantity, bool) or not isinstance(quantity, Decimal):
        raise TypeError("Inventory lot remaining_qty_kg must be a Decimal")
    if not isinstance(operational_limit, int):
        raise TypeError("Inventory lot operational_limit_days must be an int")
    if isinstance(medium_factor, bool) or not isinstance(medium_factor, Decimal):
        raise TypeError("Inventory lot medium_grade_factor must be a Decimal")
    freshness_limit = operational_limit
    if row.get("grade") == "중":
        freshness_limit = int(Decimal(operational_limit) * medium_factor)
    return InventoryLotSnapshot(
        lot_id=row.get("lot_id"),
        item=row.get("item_name"),
        available_qty_kg=quantity,
        remaining_freshness_days=freshness_limit - (as_of - received_at).days,
        status=row.get("status"),
        storage_zone=row.get("storage_zone"),
    )
