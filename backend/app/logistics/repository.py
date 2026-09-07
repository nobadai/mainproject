"""Inventory/Logistics Policy 및 Runtime Fact Repository.

★ 여기의 "Snapshot" 은 **폐지된 T0 스냅샷이 아니다.** 정의서 v2.5 §3.2 가 폐지한
  것은 *마스터가 전 부서 데이터를 얼려 배포하던 덩어리*이고(v1.2 §1.2-9 · §3.1.1 ·
  §3.2.3 — v2.5 부록 B 가 각각 대체·폐지로 적은 조항들이다),
  이 모듈이 만드는 것은 **물류가 자기 도메인만 진입 시점에 1회 읽어 호출이 끝날
  때까지 고정하는 값**이다 — 정의서 §1.2-13("한 호출 안에서 같은 값을 두 번 조회하지
  않는다")의 구현 수단이다.

  두 개념이 같은 단어를 쓰는 탓에 *"폐지된 것을 왜 아직 쓰나"* 로 읽히기 쉬워 여기에
  구분을 남긴다. 타입 이름(`InventoryLogisticsSnapshot`)은 물류 문서 세트 v1.4 의
  IO Contract 가 그 이름으로 계약을 적고 있어 문서와 함께 움직여야 한다.
"""

from datetime import date
from decimal import Decimal
from typing import NamedTuple

from psycopg import sql

from app.logistics.db import fetch_all, get_db_schema
from app.logistics.outbound import (
    _ASSIGNED_ALLOCATION,
    _HOLDING_ALLOCATION,
    _HOLDING_RESERVATION,
)
from app.logistics.schemas import (
    POLICY_VERSION,
    InventoryLogisticsSnapshot,
    InventoryLotSnapshot,
    ItemStoragePolicyFact,
    LogisticsPolicy,
    LogisticsRuntimeFixture,
    OutboundCommitment,
)

#: 계약(Literal)과 같은 값을 쓴다 — schemas 가 단일 소유다 (#121 ⑤).
LOGISTICS_POLICY_VERSION = POLICY_VERSION
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


def get_active_logistics_runtime_fixture(
    *, as_of: date, sim_run_id: str | None = None
) -> LogisticsRuntimeFixture:
    """요청 기준일과 정확히 일치하는 active MVP runtime fixture 한 건을 조회한다.

    🔴 **조회 축은 `(sim_run_id, as_of, usage_scope)` 다** — DB 의 유일성 축
       (`uq_log_runtime_fixture`)과 **같은 축이다.** 다르면 유일해야 할 조회가 유일하지
       않고, 실제로 그랬다: `sim_run_id` 가 다른 활성 행 둘이 같은 날에 공존할 수 있어
       **다른 실행의 상태를 이번 실행의 상태로 읽을 수 있었다.**

    ⚠️ **`sim_run_id` 는 아직 선택 인자다 — 이제 한 경로 때문이다** (`#345` 로 갱신).

       ```text
       adapter._load_read              봉투(ExecutionContext)로 받아 **나른다**   ✅ #345
       service._get_snapshot_or_none   HTTP 요청에 실행 식별자가 없다             ⬜ 후속
       ```

       독립 Service 경로가 값을 못 나르는 동안 필수로 만들면 물류가 값을 **지어내야**
       하므로(그것이 곧 fail-open 이다) 축은 열어 둔 채 남겨 둔다. 어댑터 경로는
       `_load_read(*, as_of, sim_run_id)` 로 이미 닫혔다 — 거기서는 선택이 아니다.

       🔴 **안 받았다고 아무 행이나 고르지 않는다.** 그 경우 실행이 둘 보이면 종전처럼
          `ValueError` 로 멈춘다 — *"둘 중 하나를 고르지 않는다"* 가 이 함수의 규율이고
          그건 안 바뀐다. 값을 받으면 그 실행으로 좁혀 애초에 둘이 안 보인다.

    :param sim_run_id: 어느 실행의 장부인가. **마스터가 소유한 값**이다. `None` 이면
        실행으로 좁히지 않는다 (그리고 둘 이상 보이면 실패한다).
    """
    schema = sql.Identifier(get_db_schema())
    # ★ 파라미터 순서를 안 바꾼다 — 실행 조건은 **뒤에** 붙인다. 앞을 흔들면 이
    #   질의를 파라미터로 재는 검사들이 축과 무관하게 깨진다.
    params: list[object] = [LOGISTICS_POLICY_USAGE_SCOPE, as_of]
    실행조건 = sql.SQL("")
    if sim_run_id is not None:
        실행조건 = sql.SQL("AND sim_run_id = %s")
        params.append(sim_run_id)
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
              {}
            ORDER BY fixture_id
            """
        ).format(schema, 실행조건),
        params,
    )
    # 🔴 0건과 2건 이상은 **다른 종류의 실패다** (#121 4단계 · 2026-09-01 교차검증 지적).
    #
    #   0건       그날의 fixture 가 아직 없다 — 부재. 다시 불러도 같다
    #   2건 이상  활성 fixture 가 둘이라 어느 것이 그날의 사실인지 모른다 — 무결성 위반
    #
    # ★ 둘을 같은 LookupError 로 내면 소비자가 가릴 수 없다. 어댑터는 부재를
    #   RUNTIME_NOT_READY 로, 실행 오류를 ERROR 로 나누는데(M-1 §5.1) 중복이 부재로
    #   섞이면 **깨진 데이터가 "데이터를 주세요" 로 나간다.**
    #
    # ★ `sim_run_id` 를 받으면 DB 가 막아 준다 — 그때 2건은 `uq_log_runtime_fixture`
    #   위반이라 실제로 일어날 수 없고, 그래도 검사를 남기는 것은 이 함수가 그 제약을
    #   전제하지 않고도 옳아야 하기 때문이다 (WHERE 한 줄이 지워지는 날 여기가 잡는다).
    #
    # ★ 여기서 하나를 고르지 않는다 — 뒤 행이 앞 행을 덮는 것도 고르는 것이다
    #   (`find_in_transit_schedule_gap` 의 inbound_id 중복 처리와 같은 규율).
    실행 = "" if sim_run_id is None else f", sim_run_id={sim_run_id}"
    if not rows:
        raise LookupError(f"No active Logistics runtime fixture for as_of={as_of}{실행}")
    if len(rows) > 1:
        raise ValueError(
            f"Expected exactly one active Logistics runtime fixture, found {len(rows)}{실행}"
        )
    return _build_logistics_runtime_fixture(
        rows[0], expected_as_of=as_of, expected_sim_run_id=sim_run_id
    )


def _build_logistics_runtime_fixture(
    row: dict[str, object], *, expected_as_of: date, expected_sim_run_id: str | None = None
) -> LogisticsRuntimeFixture:
    if row.get("as_of") != expected_as_of:
        raise ValueError("Logistics runtime fixture as_of mismatch")
    if row.get("usage_scope") != LOGISTICS_POLICY_USAGE_SCOPE:
        raise ValueError("Logistics runtime fixture usage_scope mismatch")
    # 🔴 **읽어 온 행이 물어본 실행의 행인지 다시 본다.** 위 두 줄과 같은 규율이다 —
    #    WHERE 가 조용히 빠지면 이 검사가 그 순간을 잡는다. `fixture.sim_run_id` 는
    #    바로 아래에서 `inventory_lots` · `outbound_commitments` 조회 열쇠가 되므로,
    #    여기서 안 잡으면 **한 스냅샷 안에 두 실행의 사실이 섞인다.**
    if expected_sim_run_id is not None and row.get("sim_run_id") != expected_sim_run_id:
        raise ValueError("Logistics runtime fixture sim_run_id mismatch")
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


class LogisticsRead(NamedTuple):
    """한 호출이 읽은 물류 Fact 한 벌 — Snapshot 과 **그것을 만든 Policy**.

    ★ 정의서 §1.2-13(한 호출 안에서 같은 값을 두 번 조회하지 않는다)의 구현 수단이다
      (#121 ⑤). 종전에는 Snapshot 조립이 Policy 를 읽어 **값만** 담고 버렸고,
      어댑터가 `source_refs`·`policy_version` 때문에 같은 테이블을 다시 읽었다.
      두 읽기가 서로 다른 active 행을 볼 수 있어 *"payload 값은 옛 정책, 표기된
      policy_version 은 새 정책"* 이 조용히 성립하는 구조였다.

    ★ 두 읽기가 여전히 다른 connection 인 것(조회 원자성)은 별개 위험이며 여기서
      해결하지 않는다 — 이 타입이 닫는 것은 **같은 값의 중복 조회**다.
    """

    snapshot: InventoryLogisticsSnapshot
    policy: LogisticsPolicy


def get_current_inventory_logistics_snapshot(
    *, as_of: date, sim_run_id: str | None = None
) -> InventoryLogisticsSnapshot:
    """Snapshot 만 필요한 소비자용 (독립 Service 경로)."""
    return get_current_logistics_read(as_of=as_of, sim_run_id=sim_run_id).snapshot


def get_current_logistics_read(*, as_of: date, sim_run_id: str | None = None) -> LogisticsRead:
    """Fixture, direct physical lots, Policy를 한 번 읽어 호출 중 고정될 값을 만든다.

    "한 번"이 계약이다 (정의서 §1.2-13) — 같은 호출이 같은 값을 다시 읽으면 그 사이
    원장이 바뀌어 **같은 `as_of` 인데 값이 다른** 상태가 성립한다.

    ★ **실행 축은 fixture 한 곳에서만 정해진다.** 아래 `inventory_lots` ·
      `get_outbound_commitments` 는 이미 `fixture.sim_run_id` 로 묻고 있었다 — 즉 실행을
      가르는 자리는 처음부터 **fixture 조회 하나**였고, 그래서 이번 변경이 그 한 곳만
      넓히면 스냅샷 전체가 같은 실행 위에 선다.
    """
    fixture = get_active_logistics_runtime_fixture(as_of=as_of, sim_run_id=sim_run_id)
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
    snapshot = InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=fixture.as_of,
        on_hand_by_lot=lots,
        # Lot 조회와 별도로 읽는다 — 재고가 0kg인 품목의 보관 정책도 필요하다.
        item_storage_policies=get_item_storage_policies(),
        in_transit=fixture.in_transit,
        confirmed_inbound_schedule=fixture.confirmed_inbound_schedule,
        confirmed_outbound_schedule=fixture.confirmed_outbound_schedule,
        # 🔴 예약·할당 축을 **여기서 한 번** 읽는다. 안 읽으면 매입에 나가는
        #    `inventory_by_item` 이 이미 팔린 재고를 다시 팔 수 있다고 답한다.
        outbound_commitments=get_outbound_commitments(sim_run_id=fixture.sim_run_id),
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
    return LogisticsRead(snapshot=snapshot, policy=policy)


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


def get_outbound_commitments(*, sim_run_id: str) -> list[OutboundCommitment]:
    """출고가 **이미 잡아 둔 몫**을 읽는다. `outbound.py` 와 같은 규율로 센다.

    ```text
    lot_id 있음   살아있는 할당      ALLOCATED · PICKED
    lot_id 없음   미할당 예약 잔여   reserved − (ALLOCATED·PICKED·SHIPPED)  · 음수는 0
    ```

    🔴 **`SHIPPED` 를 할당 쪽에서는 빼고 예약 쪽에서는 뺀다.** 헷갈리는 자리라 이유를
       적는다.

    ```text
    할당 축   SHIPPED 는 제외   원장 OUT 이 remaining_qty_kg 에서 이미 덜어냈다
    예약 축   SHIPPED 도 포함   그 예약이 더 이상 새로 잡아 둘 필요가 없는 몫이다
    ```

       ⚠️ 이 두 줄이 `outbound._HOLDING_ALLOCATION` · `_ASSIGNED_ALLOCATION` 과 **글자
          그대로 같아야 한다.** 다르면 같은 재고를 두 곳이 다르게 세고, 매입에 나가는
          `inventory_by_item` 과 예약이 실제로 잡을 수 있는 양이 어긋난다.

    ⚠️ **놓아준 예약(`RELEASED`·`CANCELLED`)은 세지 않는다** — 돌려준 몫이다.

    ★ 빈 목록은 *"0건 확인"* 이다. 못 읽은 것과 구분하려고 예외를 삼키지 않는다.
    """
    schema = sql.Identifier(get_db_schema())
    # ── 살아있는 할당: Lot 축 ──────────────────────────────────────────
    allocation_rows = fetch_all(
        sql.SQL(
            """
            SELECT a.lot_id, i.item_name, SUM(a.allocated_qty_kg) AS quantity_kg
            FROM {}.inventory_allocations a
            JOIN {}.inventory_reservations r ON r.reservation_id = a.reservation_id
            JOIN {}.inventory_lots l ON l.lot_id = a.lot_id
            JOIN {}.items i ON i.item_id = l.item_id
            WHERE r.sim_run_id = %s AND a.status = ANY(%s)
            GROUP BY a.lot_id, i.item_name
            ORDER BY a.lot_id
            """
        ).format(schema, schema, schema, schema),
        [sim_run_id, sorted(_HOLDING_ALLOCATION)],
    )
    # ── 미할당 예약: 품목 축 ──────────────────────────────────────────
    reservation_rows = fetch_all(
        sql.SQL(
            """
            SELECT i.item_name,
                   SUM(GREATEST(r.required_qty_kg - COALESCE(a.assigned_qty_kg, 0), 0))
                       AS quantity_kg
            FROM {}.inventory_reservations r
            JOIN {}.items i ON i.item_id = r.item_id
            LEFT JOIN (
                SELECT reservation_id, SUM(allocated_qty_kg) AS assigned_qty_kg
                FROM {}.inventory_allocations
                WHERE status = ANY(%s)
                GROUP BY reservation_id
            ) a ON a.reservation_id = r.reservation_id
            WHERE r.sim_run_id = %s AND r.status = ANY(%s)
            GROUP BY i.item_name
            ORDER BY i.item_name
            """
        ).format(schema, schema, schema),
        [
            sorted(_ASSIGNED_ALLOCATION),
            sim_run_id,
            sorted(_HOLDING_RESERVATION),
        ],
    )
    commitments = [
        OutboundCommitment(
            item=_text(row.get("item_name"), 칸="item_name"),
            lot_id=_text(row.get("lot_id"), 칸="lot_id"),
            quantity_kg=_decimal(row.get("quantity_kg"), 칸="allocated_qty_kg"),
        )
        for row in allocation_rows
        if _decimal(row.get("quantity_kg"), 칸="allocated_qty_kg") > 0
    ]
    commitments += [
        OutboundCommitment(
            item=_text(row.get("item_name"), 칸="item_name"),
            lot_id=None,
            quantity_kg=_decimal(row.get("quantity_kg"), 칸="unallocated_qty_kg"),
        )
        for row in reservation_rows
        if _decimal(row.get("quantity_kg"), 칸="unallocated_qty_kg") > 0
    ]
    return commitments


def _text(value: object, *, 칸: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Outbound commitment {칸} must be a non-empty str")
    return value


def _decimal(value: object, *, 칸: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError(f"Outbound commitment {칸} must be a Decimal")
    return value
