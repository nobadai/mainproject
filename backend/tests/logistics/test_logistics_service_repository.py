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
from app.logistics.schemas import (
    InTransitItem,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
    ScheduledQuantity,
)
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
        # 🔴 **세 status 만 남았다 (W3-2 · WP-3).** 목록의 정본이
        #    `inbound_schedules` · `sales` 로 옮겨 가면서 JSON 세 칸은 Reader 가
        #    안 읽는다 — 여기 실으면 «읽는다» 는 거짓 전제를 검사가 갖게 된다.
        "in_transit_status": "CONFIRMED_ZERO",
        "confirmed_inbound_status": "CONFIRMED_ZERO",
        "confirmed_outbound_status": "CONFIRMED_ZERO",
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


# ── runtime fixture 조회의 실행 축 ──────────────────────────────────────
#
# 🔴 **여기서는 고정 반환값을 쓰지 않는다.** `patch(..., return_value=rows)` 는 질의를
#    무엇으로 보내든 같은 행을 돌려주므로 **WHERE 절을 아예 재지 못한다.** 재려는 것이
#    정확히 *"조회가 다른 실행의 행을 집어 오는가"* 라 가짜 표를 두고 조건을 흉내 낸다.

_OTHER_RUN = "SIM-WHATIF-20260906"


def _가짜표(*rows: dict[str, object]):
    """`fetch_all` 을 대신한다 — 보낸 조건대로 걸러 준다.

    ★ `repository` 가 만드는 파라미터 순서(`usage_scope, as_of[, sim_run_id]`)를 그대로
      읽는다. 순서가 바뀌면 이 가짜가 먼저 깨져 눈에 띈다.
    """

    def fetch_all(query, params):
        usage_scope, as_of, *실행 = params
        보이는 = [
            row
            for row in rows
            if row["usage_scope"] == usage_scope and row["as_of"] == as_of
        ]
        if "sim_run_id = %s" in str(query):
            assert 실행, "조건을 걸었으면 값도 실려야 한다"
            보이는 = [row for row in 보이는 if row["sim_run_id"] == 실행[0]]
        else:
            assert not 실행, "조건이 없는데 값만 실리면 축이 반쯤 열린 것이다"
        return sorted(보이는, key=lambda row: row["fixture_id"])

    return fetch_all


def _두_실행의_같은_날() -> tuple[dict[str, object], dict[str, object]]:
    """같은 `as_of` · 같은 `usage_scope` 에 선 서로 다른 실행의 행 둘.

    ⚠️ **DB 가 이것을 허용한다.** `uq_log_runtime_fixture` 가
       `(sim_run_id, as_of, usage_scope)` 라 `sim_run_id` 만 다르면 공존한다.
    """
    a = _fixture_row()
    b = _fixture_row(
        fixture_id="LOG-RUNTIME-SIM-WHATIF-20260906",
        sim_run_id=_OTHER_RUN,
        source_ref="MVP-DECISION-20260906:WHATIF",
    )
    return a, b


@pytest.mark.parametrize("실행", ["SIM-BURNIN-202512", _OTHER_RUN])
def test_runtime_fixture_reads_only_the_requested_run(실행):
    """🔴 같은 날에 실행 둘이 서 있어도 **물어본 실행의 행만** 나온다."""
    a, b = _두_실행의_같은_날()
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", side_effect=_가짜표(a, b)),
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31), sim_run_id=실행)

    assert fixture.sim_run_id == 실행


def test_runtime_fixture_absent_for_this_run_is_absence_not_another_runs_row():
    """🔴 **다른 실행에는 있고 내 실행에는 없으면 그것은 부재다.**

    ⚠️ 남의 행을 집어 오면 *"이 실행에 그날 상태가 있다"* 가 거짓으로 성립하고, 그
       아래의 `inventory_lots` · `outbound_commitments` 조회가 **남의 실행 열쇠로**
       나간다 (`get_current_logistics_read` 가 `fixture.sim_run_id` 로 묻는다).
    """
    (a, _) = _두_실행의_같은_날()
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", side_effect=_가짜표(a)),
        pytest.raises(LookupError, match=_OTHER_RUN),
    ):
        get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31), sim_run_id=_OTHER_RUN)


def test_runtime_fixture_without_a_run_still_refuses_to_pick_one_of_two():
    """🔴 **주입을 안 받았다고 아무 행이나 고르지 않는다** — fail-open 금지.

    ★ 종전 동작 그대로다 (`found 2` ValueError). 이번 변경이 넓힌 것은 축이지 이
      규율이 아니다.
    """
    a, b = _두_실행의_같은_날()
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", side_effect=_가짜표(a, b)),
        pytest.raises(ValueError, match="found 2"),
    ):
        get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))


def test_runtime_fixture_rejects_a_row_from_a_run_it_did_not_ask_for():
    """🔴 **읽어 온 행이 물어본 실행의 것인지 다시 본다.**

    ⚠️ `as_of mismatch` · `usage_scope mismatch` 와 같은 규율이다 — WHERE 한 줄이
       조용히 빠지는 날 이 검사가 그 순간을 잡는다.
    """
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        # ★ 조건을 무시하고 남의 행을 돌려주는 표 — WHERE 가 빠진 상황 그 자체다.
        patch("app.logistics.repository.fetch_all", return_value=[_fixture_row()]),
        pytest.raises(ValueError, match="sim_run_id mismatch"),
    ):
        get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31), sim_run_id=_OTHER_RUN)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"usage_scope": "wrong"}, "usage_scope mismatch"),
        ({"as_of": date(2025, 12, 30)}, "as_of mismatch"),
        ({"sim_run_id": "SIM-OTHER"}, "sim_run_id mismatch"),
    ],
)
def test_invalid_runtime_fixture_fails_closed(updates, message):
    """🔴 **JSON 두 칸의 판정이 여기서 사라졌다 (W3-2).**

    종전에는 `in_transit_json` 이 status 와 어긋나거나 모양이 틀리면 여기서 멈췄다.
    지금 그 목록은 `inbound_schedules` 가 내고, status 는 **목록에서 유도된다**
    (`repository._schedule_source`) — fixture 가 적은 `CONFIRMED`/`CONFIRMED_ZERO`
    를 읽어 쓰지 않으므로 어긋날 두 값 자체가 없다. 남은 fail-closed 는 *"물어본
    행이 맞나"* 셋이다.
    """
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[_fixture_row(**updates)]),
        patch("app.logistics.repository._schedule_lists", return_value=([], [], [])),
        pytest.raises((ValueError, ValidationError), match=message),
    ):
        get_active_logistics_runtime_fixture(
            as_of=date(2025, 12, 31), sim_run_id="SIM-BURNIN-202512"
        )


def test_unresolved_runtime_source_preserves_none():
    row = _fixture_row(in_transit_status="UNRESOLVED", in_transit_json=None)
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[row]),
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))

    assert fixture.in_transit is None


def test_runtime_fixture_carries_inbound_id_for_b1_validation():
    """B-1: 두 축의 같은 입고 건이 **명시적 `inbound_id`** 로 연결된다 (자동 생성 금지).

    🔴 **두 목록이 한 표에서 나온다 (W3-2).** `in_transit_at` · `pending_inbound_at`
       은 같은 `inbound_schedules` 행을 각자의 종료조건으로 걸러 낸 것이라,
       `inbound_id` 가 두 축에서 어긋날 자리가 구조적으로 없다.

    ★ status 는 **목록에서 유도된다** — fixture 가 적어 둔 값을 읽어 쓰지 않는다.
    """
    운송중 = InTransitItem(
        inbound_id="INB-001",
        purchase_id="PUR-001",
        item="배추",
        quantity_kg=Decimal(500),
        expected_arrival_date=date(2026, 1, 2),
    )
    미래점유 = ScheduledQuantity(
        inbound_id="INB-001",
        item="배추",
        quantity_kg=Decimal(500),
        date=date(2026, 1, 2),
    )
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[_fixture_row()]),
        patch(
            "app.logistics.repository._schedule_lists",
            return_value=([운송중], [미래점유], []),
        ),
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))

    assert fixture.in_transit is not None
    assert fixture.in_transit[0].inbound_id == "INB-001"
    assert fixture.confirmed_inbound_schedule is not None
    assert fixture.confirmed_inbound_schedule[0].inbound_id == "INB-001"
    # ★ 저장된 CONFIRMED_ZERO 를 읽어 쓰지 않는다 — 목록이 status 를 정한다.
    assert fixture.in_transit_status == "CONFIRMED"
    assert fixture.confirmed_inbound_status == "CONFIRMED"


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


def _snapshot_with_rows(rows: list[dict[str, object]]):
    """`fetch_all` 을 가짜로 세우고 스냅샷 하나를 만든다. **DB 를 안 읽는다.**"""
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
        return get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))


def test_lot_freshness_is_none_when_storage_limit_is_missing():
    """🔴 **보관한계 NULL 은 부재지 예외가 아니다** (#366).

    같은 칸(`item_storage_policies.operational_limit_days`)을 읽는 세 자리가 같은
    답을 내야 한다 — `_item_storage_policy_from_row` 는 이미 `None` 을 보존하고,
    같은 식을 쓴다고 적어 둔 `turnover.freshness_days_of` 도 `None` 을 돌려준다.
    Lot 경로만 `TypeError` 를 냈고, 그 예외는 Repository 밖에서 **실행 실패**로
    읽혀(`adapter._load_read` → `ERROR`) 물류 에이전트를 통째로 껐다.

    ★ **두 칸이 함께 비어야 한다.** 하나만 남기면 받는 쪽이 남은 하나로 역산한다.
    """
    rows = _inventory_rows()
    rows[0]["operational_limit_days"] = None

    snapshot = _snapshot_with_rows(rows)

    baechu = next(lot for lot in snapshot.on_hand_by_lot if lot.item == "배추")
    # 🔴 `is None` 으로 잰다 — `== 0` 이면 NULL 을 0 으로 메우는 변이가 통과한다.
    assert baechu.remaining_freshness_days is None
    assert baechu.effective_freshness_limit_days is None


def test_lot_without_storage_limit_keeps_its_physical_facts():
    """신선도를 못 셌다고 **재고가 사라지지 않는다.**

    보관 정책은 *"며칠 쓸 수 있나"* 이고 Lot 물리 잔량은 *"창고에 얼마가 있나"* 다.
    앞을 모른다고 뒤를 지우면 없는 공간이 열린다.
    """
    rows = _inventory_rows()
    rows[0]["operational_limit_days"] = None

    snapshot = _snapshot_with_rows(rows)

    # Lot 이 목록에서 빠지지 않았다
    assert [lot.lot_id for lot in snapshot.on_hand_by_lot] == [
        row["lot_id"] for row in _inventory_rows()
    ]
    baechu = next(lot for lot in snapshot.on_hand_by_lot if lot.item == "배추")
    assert baechu.available_qty_kg == Decimal("286.92")
    assert baechu.status == "ACTIVE"
    assert baechu.grade == "상"
    assert baechu.storage_zone == "COLD_HUMID_0_3"
    # 🔴 점유도 그대로다 — 이 Lot 을 빼면 363.28 → 76.36 으로 줄어든다.
    assert snapshot.used_capacity_kg == Decimal("363.28")


def test_other_lots_keep_their_freshness_when_one_limit_is_missing():
    """🔴 **부재는 한 Lot 에만 머문다.** 한 품목의 정책 공백이 다른 Lot 의 셈을
    바꾸면 그건 부재가 아니라 오염이다.

    ★ 정상 int 경로의 값을 **정확히** 고정한다 — 계산식이 바뀌면 여기가 빨간불이다.
    """
    rows = _inventory_rows()
    rows[0]["operational_limit_days"] = None

    snapshot = _snapshot_with_rows(rows)

    by_item = {lot.item: lot for lot in snapshot.on_hand_by_lot}
    # 무: 한계 12 · 12-30 입고 · 기준일 12-31 → 12 - 1
    assert by_item["무"].remaining_freshness_days == 11
    assert by_item["무"].effective_freshness_limit_days == 12
    # 피마늘 · 양파: 당일 입고라 경과 0
    assert by_item["피마늘"].remaining_freshness_days == 30
    assert by_item["양파"].remaining_freshness_days == 14
    assert by_item["양파"].effective_freshness_limit_days == 14


def test_medium_grade_factor_is_not_applied_without_a_storage_limit():
    """`중` 등급이어도 **곱할 한계가 없으면 곱하지 않는다.**

    🔴 계수만 있고 한계가 없을 때 0 이나 계수 자체를 한계로 쓰면 갓 입고된 Lot 이
       즉시 임박으로 읽힌다 — `effective_freshness_limit_days` 가 막으려던 그 왜곡이다.
    """
    rows = _inventory_rows()
    rows[0]["grade"] = "중"
    rows[0]["operational_limit_days"] = None

    snapshot = _snapshot_with_rows(rows)

    baechu = next(lot for lot in snapshot.on_hand_by_lot if lot.item == "배추")
    assert baechu.grade == "중"
    assert baechu.remaining_freshness_days is None
    assert baechu.effective_freshness_limit_days is None
    assert baechu.available_qty_kg == Decimal("286.92")


def test_medium_grade_still_scales_a_present_storage_limit():
    """🔴 **회귀 방어.** `중` 등급의 정상 계산을 NULL 허용이 건드리지 않는다.

    한계 10 · 계수 0.8 → 유효 한계 8 이고, 원값 10 이 아니다.
    """
    rows = _inventory_rows()
    rows[0]["grade"] = "중"

    snapshot = _snapshot_with_rows(rows)

    baechu = next(lot for lot in snapshot.on_hand_by_lot if lot.item == "배추")
    assert baechu.effective_freshness_limit_days == 8
    assert baechu.remaining_freshness_days == 8


def test_broken_storage_limit_type_is_still_rejected():
    """🔴 **NULL 만 열었다.** 모양이 깨진 값은 여전히 실행 실패다 — 부재가 아니다."""
    rows = _inventory_rows()
    rows[0]["operational_limit_days"] = "열흘"

    with pytest.raises(TypeError, match="operational_limit_days"):
        _snapshot_with_rows(rows)


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


def test_missing_storage_limit_does_not_promote_to_runtime_error():
    """🔴 **부재가 실행 실패로 승격되지 않는다** (#366) — 원인 제거의 최종 증명.

    종전 사슬은 이랬다.

    ```text
    operational_limit_days NULL
      → _inventory_lot_from_row TypeError
      → adapter._load_read  except Exception → _SnapshotLoadError
      → runtime_status "ERROR"
      → envelope.worth_retry(ERROR) = True → 마스터가 풀리지 않을 호출을 되풀이
    ```

    ★ **어댑터를 진짜로 통과시킨다** — `_load_read` 를 갈아 끼우지 않고 `fetch_all`
      만 가짜로 세워, Repository → `_load_read` → handler 사슬 전체를 지난다.
      seam 을 위에서 막으면 정작 고친 자리를 안 지나간다.

    ★ **대표 두 mode 만 고정한다.** 넷이 같은 `_load_read` 하나를 지나므로
      (`adapter._RUNTIME_AXIS_MODES` 주석) 매입 경계와 판매 컨텍스트면 사슬이 증명된다.
    """
    from app.logistics import adapter
    from app.master.envelope import AgentRequest, ExecutionContext

    rows = _inventory_rows()
    rows[0]["operational_limit_days"] = None

    for mode in ("PRE_PURCHASE", "PRE_SALES"):
        request = AgentRequest(
            context=ExecutionContext(
                request_id="REQ-366",
                as_of=date(2025, 12, 31),
                trigger="USER_REQUEST",
                policy_version="v1.3-PROVISIONAL",
                sim_run_id="SIM-BURNIN-202512",
            ),
            agent="inventory",
            mode=mode,
            payload={},
        )
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
            reply, _ = adapter.logistics_port(request)

        # 🔴 이 한 줄이 이 이슈다 — 보관한계 하나가 없다고 실행이 실패한 것이 아니다
        assert reply.runtime_status != "ERROR", mode
        assert reply.runtime_status == "READY", mode
        # 한계를 못 센 그 Lot 도 사실로 남는다
        assert "logistics_snapshot" not in reply.missing_data
