from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from psycopg import OperationalError
from pydantic import ValidationError

from app.logistics.repository import (
    get_active_logistics_policy,
    get_active_logistics_runtime_fixture,
    get_current_inventory_logistics_snapshot,
)
from app.logistics.rules import evaluate_procurement_rules
from app.logistics.schemas import LogisticsSalesRequest, PurchaseAgentOutput
from app.logistics.service import run_logistics_procurement, run_logistics_sales


def _policy_rows() -> list[dict[str, object]]:
    values = {
        "guaranteed_capacity_kg": ("NUMERIC", Decimal(8000)),
        "burst_capacity_kg": ("NUMERIC", Decimal(9600)),
        "inbound_lead_days": ("NUMERIC", Decimal(2)),
        "daily_inbound_capacity_kg": ("NUMERIC", Decimal(5000)),
        "inbound_transport_capacity_kg": ("NUMERIC", Decimal(5000)),
        "shared_daily_outbound_capacity_kg": ("NUMERIC", Decimal(5000)),
        "cap_by_date_policy": ("TEXT", "CONFIRMED_ONLY"),
    }
    return [
        {
            "policy_key": key,
            "value_kind": kind,
            "value_numeric": value if kind == "NUMERIC" else None,
            "value_text": value if kind == "TEXT" else None,
            "value_json": None,
            "source_ref": f"MVP-POLICY:{key}",
            "policy_version": "v1.3-PROVISIONAL",
            "usage_scope": "AGENT_MVP_DEMO",
        }
        for key, (kind, value) in values.items()
    ]


def _load_policy(rows: list[dict[str, object]]):
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=rows) as fetch,
    ):
        policy = get_active_logistics_policy()
    assert fetch.call_args.args[1] == [
        "logistics",
        "v1.3-PROVISIONAL",
        "AGENT_MVP_DEMO",
    ]
    return policy


def _fixture_row(**updates) -> dict[str, object]:
    row = {
        "fixture_id": "LOG-RUNTIME-SIM-BURNIN-202512-DAY30",
        "sim_run_id": "SIM-BURNIN-202512",
        "as_of": date(2025, 12, 31),
        "in_transit_status": "CONFIRMED_ZERO",
        "in_transit_json": [],
        "confirmed_inbound_status": "CONFIRMED_ZERO",
        "confirmed_inbound_json": [],
        "confirmed_outbound_status": "CONFIRMED_ZERO",
        "confirmed_outbound_json": [],
        "usage_scope": "AGENT_MVP_DEMO",
        "evidence_grade": "SIM_FIXED",
        "source_ref": "MVP-DECISION-20260825:LOG-RUNTIME-DAY30",
        "approved_by": "HUMAN",
    }
    row.update(updates)
    return row


def _inventory_rows() -> list[dict[str, object]]:
    return [
        {
            "lot_id": "LOT-KIMCHI-015-BAECHU",
            "item_name": "배추",
            "grade": "상",
            "received_at": date(2025, 12, 31),
            "remaining_qty_kg": Decimal("286.92"),
            "status": "ACTIVE",
            "storage_zone": "COLD_HUMID_0_3",
            "operational_limit_days": 10,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "lot_id": "LOT-KIMCHI-015-MU",
            "item_name": "무",
            "grade": "상",
            "received_at": date(2025, 12, 30),
            "remaining_qty_kg": Decimal("61.76"),
            "status": "ACTIVE",
            "storage_zone": "COLD_HUMID_0_4",
            "operational_limit_days": 12,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "lot_id": "LOT-KIMCHI-015-PIMANUL",
            "item_name": "피마늘",
            "grade": "상",
            "received_at": date(2025, 12, 31),
            "remaining_qty_kg": Decimal("8.88"),
            "status": "ACTIVE",
            "storage_zone": "FROZEN_DRY_-3",
            "operational_limit_days": 30,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "lot_id": "LOT-KIMCHI-015-YANGPA",
            "item_name": "양파",
            "grade": "상",
            "received_at": date(2025, 12, 31),
            "remaining_qty_kg": Decimal("5.72"),
            "status": "ACTIVE",
            "storage_zone": "COLD_DRY_0_1",
            "operational_limit_days": 14,
            "medium_grade_factor": Decimal("0.8"),
        },
    ]


#: 🔴 Snapshot 조립은 예약·할당을 **두 번** 조회한다 (할당 축 · 미할당 예약 축).
#: 가짜 `fetch_all` 순서에서 이 둘을 빠뜨리면 StopIteration 이 난다.
_COMMITMENT_ROWS: list[list[dict[str, object]]] = [[], []]


def _storage_policy_rows() -> list[dict[str, object]]:
    """items JOIN item_storage_policies 결과.

    양파는 현재 Lot이 없어도(재고 0kg) 정책은 존재한다 — 새로 살 물건의 보관한계는
    현재 재고 존재 여부에 종속되면 안 된다.
    """
    return [
        {
            "item_name": "무",
            "operational_limit_days": 12,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "item_name": "배추",
            "operational_limit_days": 10,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "item_name": "양파",
            "operational_limit_days": 14,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "item_name": "피마늘",
            "operational_limit_days": 30,
            "medium_grade_factor": Decimal("0.8"),
        },
    ]


def test_logistics_policy_loads_typed_values_and_metadata():
    policy = _load_policy(_policy_rows())

    assert policy.guaranteed_capacity_kg == Decimal(8000)
    assert policy.burst_capacity_kg == Decimal(9600)
    assert policy.inbound_lead_days == 2
    assert policy.daily_inbound_capacity_kg == Decimal(5000)
    assert policy.inbound_transport_capacity_kg == Decimal(5000)
    assert policy.shared_daily_outbound_capacity_kg == Decimal(5000)
    assert policy.cap_by_date_policy == "CONFIRMED_ONLY"
    assert policy.policy_version == "v1.3-PROVISIONAL"
    assert policy.usage_scope == "AGENT_MVP_DEMO"
    assert policy.source_refs["guaranteed_capacity_kg"] == ("MVP-POLICY:guaranteed_capacity_kg")


def test_zero_numeric_policy_is_not_treated_as_missing():
    rows = _policy_rows()
    next(row for row in rows if row["policy_key"] == "inbound_lead_days")["value_numeric"] = (
        Decimal(0)
    )
    assert _load_policy(rows).inbound_lead_days == 0


@pytest.mark.parametrize("field", ["policy_version", "usage_scope"])
def test_logistics_policy_metadata_mismatch_fails_closed(field):
    rows = _policy_rows()
    rows[0][field] = "wrong"
    with pytest.raises(ValueError, match="mismatch"):
        _load_policy(rows)


def test_missing_or_inactive_required_logistics_policy_fails_closed():
    with pytest.raises(LookupError, match="guaranteed_capacity_kg"):
        _load_policy(_policy_rows()[1:])


@pytest.mark.parametrize(
    ("key", "mutation", "error"),
    [
        ("guaranteed_capacity_kg", {"value_kind": "TEXT"}, ValueError),
        ("guaranteed_capacity_kg", {"value_numeric": None}, ValueError),
        ("guaranteed_capacity_kg", {"value_numeric": "8000"}, TypeError),
        ("cap_by_date_policy", {"value_text": 1}, TypeError),
        ("cap_by_date_policy", {"value_json": {}}, ValueError),
    ],
)
def test_invalid_logistics_policy_value_fails_closed(key, mutation, error):
    rows = _policy_rows()
    next(row for row in rows if row["policy_key"] == key).update(mutation)
    with pytest.raises(error):
        _load_policy(rows)


def test_unsupported_cap_by_date_policy_fails_closed():
    rows = _policy_rows()
    next(row for row in rows if row["policy_key"] == "cap_by_date_policy")["value_text"] = (
        "FORECAST_ALLOWED"
    )
    with pytest.raises(ValidationError):
        _load_policy(rows)


def test_inactive_zone_policy_is_not_required_or_reconstructed():
    rows = _policy_rows()
    rows.append(
        {
            "policy_key": "guaranteed_capacity_by_zone_kg",
            "value_kind": "JSON",
            "value_numeric": None,
            "value_text": None,
            "value_json": {"GENERAL": 8000},
            "source_ref": "LEGACY:GENERAL",
            "policy_version": "v1.3-PROVISIONAL",
            "usage_scope": "AGENT_MVP_DEMO",
        }
    )
    policy = _load_policy(rows)

    assert "guaranteed_capacity_by_zone_kg" not in policy.model_fields_set


def test_independent_sla_capacity_never_falls_back_to_legacy_6_4_ton():
    rows = _policy_rows()[1:]
    with pytest.raises(LookupError, match="guaranteed_capacity_kg"):
        _load_policy(rows)


def test_runtime_fixture_loads_confirmed_zero_schedules():
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[_fixture_row()]) as fetch,
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))

    assert fixture.fixture_id == "LOG-RUNTIME-SIM-BURNIN-202512-DAY30"
    assert fixture.sim_run_id == "SIM-BURNIN-202512"
    assert fixture.in_transit == []
    assert fixture.confirmed_inbound_schedule == []
    assert fixture.confirmed_outbound_schedule == []
    assert fetch.call_args.args[1] == ["AGENT_MVP_DEMO", date(2025, 12, 31)]


@pytest.mark.parametrize(
    ("rows", "expected_error", "match"),
    [
        # 부재 — 다시 불러도 같다. 소비자는 RUNTIME_NOT_READY 로 옮긴다
        ([], LookupError, "No active"),
        # 중복 — 어느 것이 그날의 사실인지 모른다. 부재가 아니라 무결성 위반이므로
        # 소비자가 ERROR(재시도 가치)로 옮길 수 있게 다른 예외로 낸다 (#121 4단계)
        ([_fixture_row(), _fixture_row(fixture_id="duplicate")], ValueError, "found 2"),
    ],
)
def test_runtime_fixture_separates_absence_from_duplication(rows, expected_error, match):
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=rows),
        pytest.raises(expected_error, match=match),
    ):
        get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"usage_scope": "wrong"}, "usage_scope mismatch"),
        ({"as_of": date(2025, 12, 30)}, "as_of mismatch"),
        (
            {
                "in_transit_json": [
                    {
                        "item": "배추",
                        "quantity_kg": 1,
                        "expected_arrival_date": "2026-01-02",
                    }
                ]
            },
            "CONFIRMED_ZERO",
        ),
        ({"confirmed_inbound_json": {}}, "list"),
        ({"in_transit_status": "INVALID"}, "Input should be"),
    ],
)
def test_invalid_runtime_fixture_fails_closed(updates, message):
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[_fixture_row(**updates)]),
        pytest.raises((ValueError, ValidationError), match=message),
    ):
        get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))


def test_unresolved_runtime_source_preserves_none():
    row = _fixture_row(in_transit_status="UNRESOLVED", in_transit_json=None)
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[row]),
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))

    assert fixture.in_transit is None


def test_runtime_fixture_carries_inbound_id_for_b1_validation():
    """B-1: Fixture의 동일 입고 건은 명시적 inbound_id로 연결된다 (자동 생성 금지)."""
    row = _fixture_row(
        in_transit_status="CONFIRMED",
        in_transit_json=[
            {
                "inbound_id": "INB-001",
                "item": "배추",
                "quantity_kg": 500,
                "expected_arrival_date": "2026-01-02",
            }
        ],
        confirmed_inbound_status="CONFIRMED",
        confirmed_inbound_json=[
            {
                "inbound_id": "INB-001",
                "item": "배추",
                "quantity_kg": 500,
                "date": "2026-01-02",
            }
        ],
    )
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[row]),
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))

    assert fixture.in_transit is not None
    assert fixture.in_transit[0].inbound_id == "INB-001"
    assert fixture.confirmed_inbound_schedule is not None
    assert fixture.confirmed_inbound_schedule[0].inbound_id == "INB-001"


def test_runtime_snapshot_combines_fixture_direct_lots_and_policy():
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[
                [_fixture_row()],
                _policy_rows(),
                _inventory_rows(),
                _storage_policy_rows(),
                *_COMMITMENT_ROWS,
            ],
        ) as fetch,
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    assert snapshot.snapshot_id is None
    assert [lot.lot_id for lot in snapshot.on_hand_by_lot] == [
        "LOT-KIMCHI-015-BAECHU",
        "LOT-KIMCHI-015-MU",
        "LOT-KIMCHI-015-PIMANUL",
        "LOT-KIMCHI-015-YANGPA",
    ]
    assert all(lot.item != "건고추" for lot in snapshot.on_hand_by_lot)
    assert snapshot.used_capacity_kg == Decimal("363.28")
    assert snapshot.guaranteed_capacity_kg == Decimal(8000)
    assert snapshot.guaranteed_capacity_kg - snapshot.used_capacity_kg == Decimal("7636.72")
    assert snapshot.guaranteed_capacity_kg - snapshot.used_capacity_kg != Decimal("6036.72")
    assert snapshot.burst_capacity_kg == Decimal(9600)
    assert snapshot.in_transit == []
    assert snapshot.confirmed_inbound_schedule == []
    assert snapshot.confirmed_outbound_schedule == []
    assert snapshot.guaranteed_capacity_by_zone_kg is None
    inventory_call = fetch.call_args_list[2]
    assert inventory_call.args[1] == ["SIM-BURNIN-202512", date(2025, 12, 31)]
    query_text = str(inventory_call.args[0])
    assert "inventory_lots" in query_text
    assert "received_at <= %s" in query_text
    # 물리 점유는 status와 무관하다 — 잔량이 남아 창고 안에 있으면 전부 읽는다.
    # 소진/반출 완료 Lot은 remaining_qty_kg = 0으로 자연히 빠진다.
    assert "status = 'ACTIVE'" not in query_text
    assert "remaining_qty_kg > 0" in query_text
    assert "v_current_inventory" not in query_text
    assert "v_current_logistics_capacity" not in query_text


def test_lot_grade_in_purchase_vocabulary_passes_through():
    """DB raw가 이미 특/상/중/하 어휘면 변환이 아니므로 그대로 싣는다."""
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[
                [_fixture_row()],
                _policy_rows(),
                _inventory_rows(),
                _storage_policy_rows(),
                *_COMMITMENT_ROWS,
            ],
        ),
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    assert all(lot.grade == "상" for lot in snapshot.on_hand_by_lot)


def test_lot_grade_without_normalization_evidence_is_none():
    """TC-03: raw `상품`은 근거 없는 `상` 변환 금지 — grade=None.

    등급 의존 판단(medium_grade_factor)도 정규화 결과 기준이라 적용되지 않고,
    freshness는 operational_limit 기준으로 남는다. 해석 불가 사실은
    GRADE_VOCABULARY_UNRESOLVED soft warning으로만 드러나며 Runtime은 유지된다.
    """
    rows = _inventory_rows()
    for row in rows:
        row["grade"] = "상품"
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[
                [_fixture_row()],
                _policy_rows(),
                rows,
                _storage_policy_rows(),
                *_COMMITMENT_ROWS,
            ],
        ),
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    assert all(lot.grade is None for lot in snapshot.on_hand_by_lot)
    assert all(lot.grade != "상" for lot in snapshot.on_hand_by_lot)
    baechu = next(lot for lot in snapshot.on_hand_by_lot if lot.item == "배추")
    # operational_limit 10 · factor 미적용 — as_of 당일 입고라 잔여 10일 그대로.
    assert baechu.remaining_freshness_days == 10

    result = evaluate_procurement_rules(as_of=date(2025, 12, 31), snapshot=snapshot)
    assert "GRADE_VOCABULARY_UNRESOLVED" in result["soft_warnings"]
    assert result["runtime_status"] == "READY"


def test_medium_grade_lot_applies_medium_grade_factor():
    """raw `중`은 Purchase 어휘 그대로라 정규화되고 medium_grade_factor가 적용된다."""
    rows = _inventory_rows()
    rows[0]["grade"] = "중"
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[
                [_fixture_row()],
                _policy_rows(),
                rows,
                _storage_policy_rows(),
                *_COMMITMENT_ROWS,
            ],
        ),
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    baechu = next(lot for lot in snapshot.on_hand_by_lot if lot.item == "배추")
    assert baechu.grade == "중"
    # operational_limit 10 × medium_grade_factor 0.8 = 8 — as_of 당일 입고라 잔여 8일.
    assert baechu.remaining_freshness_days == 8


def test_non_active_lot_occupies_capacity_when_physically_present():
    """검수/격리 등 비-ACTIVE 재고도 잔량이 남아 있으면 물리 점유에 포함한다."""
    rows = _inventory_rows()
    rows[0]["status"] = "QUARANTINED"
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[
                [_fixture_row()],
                _policy_rows(),
                rows,
                _storage_policy_rows(),
                *_COMMITMENT_ROWS,
            ],
        ),
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    assert snapshot.used_capacity_kg == Decimal("363.28")
    quarantined = next(lot for lot in snapshot.on_hand_by_lot if lot.status == "QUARANTINED")
    assert quarantined.lot_id == "LOT-KIMCHI-015-BAECHU"


def test_item_storage_policy_is_separate_from_lot_freshness():
    """Lot의 잔여 신선도와 품목의 보관한계는 다른 값이다.

    배추 보관한계가 15일이고 Lot이 7일 경과했으면 그 Lot은 8일 남았다.
    새로 매입하는 배추의 기준은 8이 아니라 15다.
    """
    rows = _inventory_rows()[:1]
    rows[0]["received_at"] = date(2025, 12, 24)
    rows[0]["operational_limit_days"] = 15
    storage_rows = [
        {
            "item_name": "배추",
            "operational_limit_days": 15,
            "medium_grade_factor": Decimal("0.6"),
        }
    ]
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[[_fixture_row()], _policy_rows(), rows, storage_rows, *_COMMITMENT_ROWS],
        ),
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    lot = snapshot.on_hand_by_lot[0]
    assert lot.remaining_freshness_days == 8
    assert snapshot.item_storage_policies is not None
    baechu = next(row for row in snapshot.item_storage_policies if row.item == "배추")
    assert baechu.operational_limit_days == 15
    assert baechu.operational_limit_days != lot.remaining_freshness_days
    # DB 값을 그대로 나른다 — 코드에서 0.6이나 0.8을 새로 만들지 않는다.
    assert baechu.medium_grade_factor == Decimal("0.6")


def test_item_storage_policy_covers_items_without_lots():
    """재고가 0kg인 품목도 정책은 나온다 — Lot 목록에서 역산하지 않는다."""
    rows = [row for row in _inventory_rows() if row["item_name"] == "배추"]
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[
                [_fixture_row()],
                _policy_rows(),
                rows,
                _storage_policy_rows(),
                *_COMMITMENT_ROWS,
            ],
        ) as fetch,
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    assert [lot.item for lot in snapshot.on_hand_by_lot] == ["배추"]
    assert snapshot.item_storage_policies is not None
    assert [row.item for row in snapshot.item_storage_policies] == ["무", "배추", "양파", "피마늘"]

    storage_call = fetch.call_args_list[3]
    query_text = str(storage_call.args[0])
    assert "item_storage_policies" in query_text
    assert "items" in query_text
    # 재고 조회에 얹지 않는다 — Lot이 없으면 정책도 못 받는 구조가 되면 안 된다.
    assert "inventory_lots" not in query_text


def test_item_storage_policy_preserves_missing_values():
    """DB에 값이 없으면 없는 대로 둔다 — 0이나 0.6을 지어내지 않는다."""
    storage_rows = [
        {"item_name": "무", "operational_limit_days": None, "medium_grade_factor": None}
    ]
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[
                [_fixture_row()],
                _policy_rows(),
                _inventory_rows(),
                storage_rows,
                *_COMMITMENT_ROWS,
            ],
        ),
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    assert snapshot.item_storage_policies is not None
    mu = snapshot.item_storage_policies[0]
    assert mu.item == "무"
    assert mu.operational_limit_days is None
    assert mu.medium_grade_factor is None


def test_logistics_a_ready_response_and_persistence(
    complete_logistics_snapshot, logistics_purchase_payload
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=complete_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_procurement(request)

    assert response.runtime_status == "READY"
    assert response.verdict == "REVIEW_REQUIRED"
    assert response.snapshot_id == "T0-20260821-001"
    assert response.band.cap_by_date == {date(2026, 8, 23): Decimal(7000)}
    assert response.inventory_by_item is not None
    assert [(row.item, row.available_qty_kg) for row in response.inventory_by_item] == [
        ("배추", Decimal(1000))
    ]
    assert [result.verdict for result in response.scenario_results] == ["ok"]
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    saved = save_run.call_args.kwargs
    assert saved["cycle"] == "PROCUREMENT"
    assert saved["runtime_status"] == "READY"
    assert saved["verdict"] == "REVIEW_REQUIRED"
    assert saved["response_payload"]["verdict"] == "REVIEW_REQUIRED"
    assert saved["snapshot_id"] == "T0-20260821-001"
    assert saved["request_payload"]["scenarios"][0]["total_qty_kg"] == 4500
    assert saved["response_payload"]["llm_status"] == "SKIPPED_TEMPLATE"
    assert [row["item"] for row in saved["response_payload"]["inventory_by_item"]] == ["배추"]
    assert [row["verdict"] for row in saved["response_payload"]["scenario_results"]] == ["ok"]


def test_logistics_a_unresolved_response_is_saved(
    unresolved_logistics_snapshot, logistics_purchase_payload
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=unresolved_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_procurement(request)

    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.verdict is None
    assert response.band.cap_by_date == {}
    # 계산 불가(None)는 0건 확인([])이 아니다 — 직렬화에서 키 자체가 빠진다.
    assert response.inventory_by_item is None
    assert [result.verdict for result in response.scenario_results] == ["skipped"]
    assert save_run.call_args.kwargs["runtime_status"] == "RUNTIME_NOT_READY"
    assert save_run.call_args.kwargs["verdict"] is None
    assert save_run.call_args.kwargs["response_payload"]["verdict"] is None
    assert "inventory_by_item" not in save_run.call_args.kwargs["response_payload"]


def test_logistics_b_keeps_h1_out_of_on_hand_and_saves_run(
    complete_logistics_snapshot, logistics_sales_payload
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=complete_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_sales(request)

    assert response.runtime_status == "READY"
    assert response.verdict == "PASS"
    assert response.approval_id == "H1-20260821-001"
    assert response.daily_outbound_capacity_kg == Decimal(1000)
    assert [item.lot_id for item in response.lot_constraints] == ["LOT-001"]
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    assert save_run.call_args.kwargs["cycle"] == "SALES"
    assert save_run.call_args.kwargs["verdict"] == "PASS"
    assert save_run.call_args.kwargs["response_payload"]["verdict"] == "PASS"


def test_logistics_b_unresolved_n17_is_saved(
    unresolved_logistics_snapshot, logistics_sales_payload
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=unresolved_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_sales(request)

    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.verdict is None
    assert response.daily_outbound_capacity_kg is None
    assert save_run.call_args.kwargs["runtime_status"] == "RUNTIME_NOT_READY"
    assert save_run.call_args.kwargs["verdict"] is None


def test_logistics_b_ready_blocking_constraint_persists_fail(
    complete_logistics_snapshot, logistics_sales_payload
):
    snapshot = complete_logistics_snapshot.model_copy(
        update={"guaranteed_capacity_kg": Decimal(5000)}
    )
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_sales(request)

    assert response.runtime_status == "READY"
    assert response.verdict == "FAIL"
    assert save_run.call_args.kwargs["verdict"] == "FAIL"
    assert save_run.call_args.kwargs["response_payload"]["verdict"] == "FAIL"


def test_logistics_persistence_failure_is_not_runtime_warning(
    complete_logistics_snapshot, logistics_purchase_payload
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=complete_logistics_snapshot,
        ),
        patch(
            "app.logistics.service.save_logistics_agent_run",
            side_effect=OperationalError("persistence unavailable"),
        ),
        pytest.raises(OperationalError, match="persistence unavailable"),
    ):
        run_logistics_procurement(request)
