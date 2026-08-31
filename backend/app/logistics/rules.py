"""재고·물류 Agent의 Runtime 및 Hard Constraint 규칙."""

from datetime import date
from decimal import Decimal
from typing import TypedDict

from app.logistics.schemas import (
    ConstraintResult,
    FinalVerdict,
    InventoryLogisticsSnapshot,
    RuntimeStatus,
    ScenarioValidationResult,
)
from app.logistics.tools import (
    calculate_window_capacity_usage,
    collect_freshness_pressure_inputs,
    find_in_transit_schedule_gap,
    has_unattributed_confirmed_outbound,
    is_inbound_schedule_complete,
)

# ---------------------------------------------------------------------------
# 업무 위험 Signal (LLM 정책 결정서 §3)
# ---------------------------------------------------------------------------
#
# ★ 이름에 숫자를 넣지 않는다 — LLM 출력 검증기의 숫자 금지 검사와 충돌하지 않기
#   위한 명명 규칙이다 (`LOG-H02` 같은 내부 코드가 겪은 함정).
# ★ 데이터/정책 미확정 코드는 여기 넣지 않는다 — signals 와 missing_data 는
#   저장 위치가 아니라 **코드의 의미**로 분류한다.

CAPACITY_TIGHT = "CAPACITY_TIGHT"
INVENTORY_FRESHNESS_PRESSURE = "INVENTORY_FRESHNESS_PRESSURE"
FRESHNESS_QUALITY_RISK = "FRESHNESS_QUALITY_RISK"
SCENARIO_ADJUSTMENT_REQUIRED = "SCENARIO_ADJUSTMENT_REQUIRED"

#: LLM 해석 대상 업무 위험의 전체 집합. 여기 없는 코드는 전부 미확정 계열이다.
BUSINESS_SIGNALS = frozenset(
    {
        CAPACITY_TIGHT,
        INVENTORY_FRESHNESS_PRESSURE,
        FRESHNESS_QUALITY_RISK,
        SCENARIO_ADJUSTMENT_REQUIRED,
    }
)

#: 정책값 부재로 판정을 건너뛴 사실의 무숫자 경고 — 조용한 off 금지 (결정서 §4).
#: "검사해서 문제 없음"과 "기준이 없어 검사 안 함"을 구분한다.
CAPACITY_TIGHT_POLICY_UNRESOLVED = "CAPACITY_TIGHT_POLICY_UNRESOLVED"
FRESHNESS_PRESSURE_POLICY_UNRESOLVED = "FRESHNESS_PRESSURE_POLICY_UNRESOLVED"
#: 잔여일·유효 한계 미확인으로 신선도 비율 계산에서 제외된 Lot 이 있다는 사실.
LOT_FRESHNESS_UNRESOLVED = "LOT_FRESHNESS_UNRESOLVED"

#: Sales 에서 Rule 이 정하는 우선 조정 어휘. Procurement 의 quantity/timing 과
#: 사이클 어휘를 섞지 않는다 (결정서 §5).
SALES_PRIORITY_ADJUSTMENT = "우선 출고 대상으로 검토합니다."


class BusinessSignalResult(TypedDict):
    signals: list[str]
    #: 판정 스킵·제외 사실 — 데이터/정책 미확정 계열. soft_warnings 로 나간다.
    warnings: list[str]


def evaluate_procurement_business_signals(
    *,
    as_of: date,
    snapshot: InventoryLogisticsSnapshot | None,
    scenario_results: list[ScenarioValidationResult],
) -> BusinessSignalResult:
    """PROCUREMENT 업무 위험 3종을 판정한다. 비교식은 여기(Rule)가 소유한다.

    계산(사용률·신선도 비율)은 Tool 이 하고, 임계 비교와 signal 생성만 여기서 한다.
    임계가 None(선택 정책 미등록)이면 판정을 지어내지 않고 SKIPPED + 경고다.
    """
    signals: list[str] = []
    warnings: list[str] = []
    if snapshot is not None:
        usage = calculate_window_capacity_usage(snapshot, as_of)
        if snapshot.capacity_tight_ratio is None:
            warnings.append(CAPACITY_TIGHT_POLICY_UNRESOLVED)
        elif usage is not None and usage >= snapshot.capacity_tight_ratio:
            signals.append(CAPACITY_TIGHT)

        ratios, unresolved_lots = collect_freshness_pressure_inputs(snapshot)
        if snapshot.freshness_pressure_ratio is None:
            warnings.append(FRESHNESS_PRESSURE_POLICY_UNRESOLVED)
        elif any(ratio <= snapshot.freshness_pressure_ratio for ratio in ratios):
            signals.append(INVENTORY_FRESHNESS_PRESSURE)
        if unresolved_lots:
            warnings.append(LOT_FRESHNESS_UNRESOLVED)

    # 조정 필요 = 이번 실행에서 실제로 conditional 이 나온 상태. ok-only 는 조정이
    # 없고 reject-only 는 Rule 이 불가를 확정한 것이라 signal 을 만들지 않는다.
    if any(result.verdict == "conditional" for result in scenario_results):
        signals.append(SCENARIO_ADJUSTMENT_REQUIRED)
    return {"signals": signals, "warnings": warnings}


def evaluate_sales_business_signals(
    *,
    snapshot: InventoryLogisticsSnapshot | None,
) -> BusinessSignalResult:
    """SALES 신선도 위험을 비율 Rule 로 판정한다.

    기존에는 Lot `status = NEEDS_PRIORITY_SHIPMENT` 에 의존했는데, 그 상태를 만드는
    코드가 물류에 없고 DB 생성 주체도 확인되지 않아 실데이터에서 트리거가 죽는다 —
    매입 전과 같은 비율 계산을 쓰되 사이클 업무 의미에 따라 이름만 달리 붙인다.
    """
    signals: list[str] = []
    warnings: list[str] = []
    if snapshot is not None:
        ratios, unresolved_lots = collect_freshness_pressure_inputs(snapshot)
        if snapshot.freshness_pressure_ratio is None:
            warnings.append(FRESHNESS_PRESSURE_POLICY_UNRESOLVED)
        elif any(ratio <= snapshot.freshness_pressure_ratio for ratio in ratios):
            signals.append(FRESHNESS_QUALITY_RISK)
        if unresolved_lots:
            warnings.append(LOT_FRESHNESS_UNRESOLVED)
    return {"signals": signals, "warnings": warnings}


class LogisticsRuleResult(TypedDict):
    runtime_status: RuntimeStatus
    hard_constraints: list[ConstraintResult]
    soft_warnings: list[str]
    calculation_ready: bool


def derive_logistics_verdict(result: LogisticsRuleResult) -> FinalVerdict | None:
    """Runtime readiness와 개별 Hard Check 상태를 분리해 최종 판정을 집계한다."""
    if result["runtime_status"] != "READY":
        return None
    statuses = {constraint.status for constraint in result["hard_constraints"]}
    if "FAIL" in statuses:
        return "FAIL"
    if "UNRESOLVED" in statuses:
        return "REVIEW_REQUIRED"
    return "PASS"


def evaluate_procurement_rules(
    *,
    as_of: date,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsRuleResult:
    """Logistics A의 cap_by_date 계산 가능 여부를 fail-closed로 판단한다."""
    boundary = _snapshot_boundary(as_of=as_of, snapshot=snapshot)
    if boundary is not None:
        return boundary
    assert snapshot is not None

    constraints = [
        _known_constraint("LOG-H01", snapshot.guaranteed_capacity_kg, "N2_UNRESOLVED"),
        _known_constraint(
            "LOG-H02",
            snapshot.guaranteed_capacity_by_zone_kg,
            "ZONE_CAPACITY_UNRESOLVED",
        ),
        _known_constraint(
            "LOG-H03",
            snapshot.daily_inbound_capacity_kg,
            "DAILY_INBOUND_CAPACITY_UNRESOLVED",
        ),
        _known_constraint(
            "LOG-H04",
            snapshot.inbound_transport_capacity_kg,
            "INBOUND_TRANSPORT_CAPACITY_UNRESOLVED",
        ),
        _known_constraint("LOG-H05", snapshot.inbound_lead_days, "N4_UNRESOLVED"),
    ]
    inbound_gap = find_in_transit_schedule_gap(snapshot)
    if inbound_gap is not None:
        constraints.append(
            ConstraintResult(
                code="IN_TRANSIT_SCHEDULE_UNRESOLVED",
                status="UNRESOLVED",
                skip_reason=inbound_gap,
            )
        )
    # 확정 출고 행에 item이 없으면 품목별 예약 차감을 못 한다 — inventory_by_item만
    # 생략하는 Partial Output이며 PRE 전체는 READY를 유지한다. Master Adapter는 이
    # ConstraintResult로 누락을 식별해 M-1 missing_data로 번역한다.
    if has_unattributed_confirmed_outbound(snapshot):
        constraints.append(
            ConstraintResult(
                code="CONFIRMED_OUTBOUND_ITEM_UNRESOLVED",
                status="UNRESOLVED",
                skip_reason="CONFIRMED_OUTBOUND_ITEM_UNRESOLVED",
            )
        )
    soft_warnings = _snapshot_warnings(snapshot)
    # 1차 Hard Capacity는 guaranteed 하나다 — daily inbound/transport는 값이 없어도
    # Runtime을 막지 않는다 (Policy 결정값 §3).
    core_values = (
        snapshot.guaranteed_capacity_kg,
        snapshot.inbound_lead_days,
        snapshot.confirmed_inbound_schedule,
        snapshot.confirmed_outbound_schedule,
    )
    calculation_ready = all(value is not None for value in core_values) and inbound_gap is None
    return {
        "runtime_status": "READY" if calculation_ready else "RUNTIME_NOT_READY",
        "hard_constraints": constraints,
        "soft_warnings": soft_warnings,
        "calculation_ready": calculation_ready,
    }


def evaluate_sales_rules(
    *,
    as_of: date,
    snapshot: InventoryLogisticsSnapshot | None,
    future_occupancy_by_date: dict[date, Decimal] | None,
) -> LogisticsRuleResult:
    """Logistics B의 outbound 및 H1 미래 점유 계산 가능 여부를 판단한다."""
    boundary = _snapshot_boundary(as_of=as_of, snapshot=snapshot)
    if boundary is not None:
        return boundary
    assert snapshot is not None

    warehouse_constraint = _known_constraint(
        "LOG-H01", snapshot.guaranteed_capacity_kg, "N2_UNRESOLVED"
    )
    if snapshot.guaranteed_capacity_kg is not None and future_occupancy_by_date is not None:
        warehouse_constraint = ConstraintResult(
            code="LOG-H01",
            status="PASS"
            if all(
                value <= snapshot.guaranteed_capacity_kg
                for value in future_occupancy_by_date.values()
            )
            else "FAIL",
        )
    outbound_constraint = _known_constraint(
        "N17",
        snapshot.shared_daily_outbound_capacity_kg,
        "N17_UNRESOLVED",
    )
    lots_complete = all(lot.remaining_freshness_days is not None for lot in snapshot.on_hand_by_lot)
    lot_constraint = ConstraintResult(
        code="N17-LOT",
        status="PASS" if lots_complete else "UNRESOLVED",
        skip_reason=None if lots_complete else "N17_LOT_FRESHNESS_UNRESOLVED",
    )
    inbound_completeness_constraint = None
    sales_inbound_gap = find_in_transit_schedule_gap(snapshot)
    if sales_inbound_gap is not None:
        inbound_completeness_constraint = ConstraintResult(
            code="IN_TRANSIT_SCHEDULE_UNRESOLVED",
            status="UNRESOLVED",
            skip_reason=sales_inbound_gap,
        )
    soft_warnings = _snapshot_warnings(snapshot)
    if future_occupancy_by_date is None:
        soft_warnings.append("H1_FUTURE_OCCUPANCY_UNRESOLVED")
    calculation_ready = (
        snapshot.shared_daily_outbound_capacity_kg is not None
        and snapshot.guaranteed_capacity_kg is not None
        and future_occupancy_by_date is not None
        and lots_complete
        and is_inbound_schedule_complete(snapshot)
    )
    constraints = [warehouse_constraint, outbound_constraint, lot_constraint]
    if inbound_completeness_constraint is not None:
        constraints.append(inbound_completeness_constraint)
    return {
        "runtime_status": "READY" if calculation_ready else "RUNTIME_NOT_READY",
        "hard_constraints": constraints,
        "soft_warnings": soft_warnings,
        "calculation_ready": calculation_ready,
    }


def _snapshot_boundary(
    *,
    as_of: date,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsRuleResult | None:
    if snapshot is None:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "hard_constraints": [
                ConstraintResult(
                    code="REQUIRED_LOGISTICS_SNAPSHOT_MISSING",
                    status="FAIL",
                )
            ],
            "soft_warnings": [],
            "calculation_ready": False,
        }
    if as_of != snapshot.as_of:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "hard_constraints": [ConstraintResult(code="AS_OF_MISMATCH", status="FAIL")],
            "soft_warnings": _snapshot_warnings(snapshot),
            "calculation_ready": False,
        }
    return None


def _known_constraint(
    code: str,
    value: object | None,
    skip_reason: str,
) -> ConstraintResult:
    return ConstraintResult(
        code=code,
        status="PASS" if value is not None else "UNRESOLVED",
        skip_reason=None if value is not None else skip_reason,
    )


def _snapshot_warnings(snapshot: InventoryLogisticsSnapshot) -> list[str]:
    warnings: list[str] = []
    if snapshot.snapshot_id is None:
        warnings.append("SNAPSHOT_ID_UNRESOLVED")
    # 정규화 근거가 없어 등급 어휘를 해석하지 못한 Lot이 있다는 사실만 드러낸다 —
    # 임의 매핑은 하지 않고, 이 경고만으로 Runtime을 중단시키지도 않는다.
    if any(lot.grade is None for lot in snapshot.on_hand_by_lot):
        warnings.append("GRADE_VOCABULARY_UNRESOLVED")
    if any("provisional=true" in ref for ref in snapshot.evidence_refs):
        warnings.append("PROVISIONAL_CAPACITY_EXCLUDED_FROM_HARD_LIMIT")
    if snapshot.in_transit is None:
        warnings.append("IN_TRANSIT_UNRESOLVED")
    if snapshot.confirmed_inbound_schedule is None:
        warnings.append("CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED")
    if snapshot.confirmed_outbound_schedule is None:
        warnings.append("CONFIRMED_OUTBOUND_SCHEDULE_UNRESOLVED")
    return warnings
