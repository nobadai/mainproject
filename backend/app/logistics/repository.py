"""Inventory/Logistics Policy 및 T0 Runtime Snapshot Repository."""

from datetime import date
from decimal import Decimal

from psycopg import sql

from app.logistics.db import fetch_all, get_db_schema
from app.logistics.schemas import (
    InventoryLogisticsSnapshot,
    InventoryLotSnapshot,
    ItemStoragePolicyFact,
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
#: 선택 정책 2종 (LLM 정책 결정서 §4) — 업무 위험 signal 의 임계값.
#: _REQUIRED_POLICY_KEYS 로 승격 금지: DB 행이 없는 순간 스냅샷 전체가 실패해
#: 물류가 통째로 RUNTIME_NOT_READY 가 된다. 없으면 None → 해당 판정만 SKIPPED.
_OPTIONAL_NUMERIC_POLICY_KEYS = {
    "capacity_tight_ratio",
    "freshness_pressure_ratio",
}


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
        if key not in _REQUIRED_POLICY_KEYS and key not in _OPTIONAL_NUMERIC_POLICY_KEYS:
            continue
        if key in values:
            raise ValueError(f"Duplicate Logistics policy key: {key}")
        if row.get("policy_version") != LOGISTICS_POLICY_VERSION:
            raise ValueError(f"Logistics policy_version mismatch: {key}")
        if row.get("usage_scope") != LOGISTICS_POLICY_USAGE_SCOPE:
            raise ValueError(f"Logistics policy usage_scope mismatch: {key}")

        kind = row.get("value_kind")
        expected_kind = "TEXT" if key in _TEXT_POLICY_KEYS else "NUMERIC"
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
    # 선택 정책은 없어도 실패가 아니다 — None 으로 두면 해당 signal 판정만 꺼진다.
    for optional_key in _OPTIONAL_NUMERIC_POLICY_KEYS:
        values.setdefault(optional_key, None)

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


def get_item_storage_policies() -> list[ItemStoragePolicyFact]:
    """품목 단위 보관 정책을 조회한다.

    Lot 목록에서 역산하지 않는다 — 새로 매입하려는 품목은 현재 재고가 0kg일 수 있고
    그때도 보관한계는 알아야 한다. 정책 테이블 자체를 기준으로 읽는다.
    """
    schema = sql.Identifier(get_db_schema())
    rows = fetch_all(
        sql.SQL(
            """
            SELECT
                i.item_name,
                p.operational_limit_days,
                p.medium_grade_factor
            FROM {}.item_storage_policies p
            JOIN {}.items i ON i.item_id = p.item_id
            ORDER BY i.item_name
            """
        ).format(schema, schema),
        [],
    )
    return [_item_storage_policy_from_row(row) for row in rows]


def _item_storage_policy_from_row(row: dict[str, object]) -> ItemStoragePolicyFact:
    item = row.get("item_name")
    limit_days = row.get("operational_limit_days")
    medium_factor = row.get("medium_grade_factor")
    if not isinstance(item, str) or not item:
        raise TypeError("Item storage policy item_name must be a non-empty string")
    # 값이 없으면 없는 대로 둔다 — 0이나 0.6 같은 기본값을 코드에서 지어내지 않는다.
    if limit_days is not None and (isinstance(limit_days, bool) or not isinstance(limit_days, int)):
        raise TypeError(f"Item storage policy operational_limit_days must be an int: {item}")
    if medium_factor is not None and (
        isinstance(medium_factor, bool) or not isinstance(medium_factor, Decimal)
    ):
        raise TypeError(f"Item storage policy medium_grade_factor must be a Decimal: {item}")
    return ItemStoragePolicyFact(
        item=item,
        operational_limit_days=limit_days,
        medium_grade_factor=medium_factor,
    )


def get_current_inventory_logistics_snapshot(*, as_of: date) -> InventoryLogisticsSnapshot:
    """Fixture, direct physical lots, Policy를 한 번 읽어 고정 T0 Snapshot을 만든다."""
    fixture = get_active_logistics_runtime_fixture(as_of=as_of)
    policy = get_active_logistics_policy()
    schema = sql.Identifier(get_db_schema())

    # 물리 점유 대상: 잔량이 남아 실제 창고 안에 존재하는 모든 Lot.
    # status로 거르지 않는다 — 검수·격리·사용불가·신선도 만료 재고도 반출/폐기 전이면
    # 공간을 점유한다. 소진/반출 완료 Lot은 remaining_qty_kg = 0으로 자연히 빠진다
    # (현행 DB의 DEPLETED가 그 예). 가용 여부 판정은 tools.build_inventory_by_item 몫이다.
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
        # Lot 조회와 별도로 읽는다 — 재고가 0kg인 품목의 보관 정책도 필요하다.
        item_storage_policies=get_item_storage_policies(),
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
        capacity_tight_ratio=policy.capacity_tight_ratio,
        freshness_pressure_ratio=policy.freshness_pressure_ratio,
        evidence_refs=[
            f"DB:logistics_runtime_fixture/{fixture.fixture_id}",
            fixture.source_ref,
            f"DB:inventory_lots/sim_run_id={fixture.sim_run_id}",
            "DB:item_storage_policies",
            *policy.source_refs.values(),
        ],
    )


#: Purchase 등급 어휘. 원천이 이미 이 어휘면 변환이 아니므로 그대로 통과시킨다.
_PURCHASE_GRADE_VOCABULARY = frozenset({"특", "상", "중", "하"})
#: 근거가 확정된 raw → 정규화 매핑만 등록한다. 현재 확정된 매핑은 없다 —
#: 특히 `상품 → 상` 같은 임의 치환은 금지다 (등급 표준화 근거 확정 시 여기에 반영).
_RAW_GRADE_NORMALIZATION: dict[str, str] = {}


def _normalize_grade(raw_grade: object) -> str | None:
    """DB raw grade를 Purchase용 정규화 등급으로 옮긴다. 근거 없으면 None."""
    if not isinstance(raw_grade, str):
        return None
    if raw_grade in _PURCHASE_GRADE_VOCABULARY:
        return raw_grade
    return _RAW_GRADE_NORMALIZATION.get(raw_grade)


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
    # 등급 의존 판단은 raw가 아니라 정규화 결과 기준이다 — raw `상품` 계열은
    # 정규화되지 않으므로(None) medium_grade_factor를 조용히 건너뛰지 않고,
    # 해석 불가 사실이 lots[].grade = None으로 드러난다.
    normalized_grade = _normalize_grade(row.get("grade"))
    freshness_limit = operational_limit
    if normalized_grade == "중":
        freshness_limit = int(Decimal(operational_limit) * medium_factor)
    return InventoryLotSnapshot(
        lot_id=row.get("lot_id"),
        item=row.get("item_name"),
        grade=normalized_grade,
        available_qty_kg=quantity,
        remaining_freshness_days=freshness_limit - (as_of - received_at).days,
        # remaining 계산에 쓴 그 한계를 그대로 싣는다 — 신선도 잔여 비율의 분모는
        # operational_limit 원값이 아니라 이 값이어야 한다 (중 등급 왜곡 방지).
        effective_freshness_limit_days=freshness_limit,
        status=row.get("status"),
        storage_zone=row.get("storage_zone"),
    )
