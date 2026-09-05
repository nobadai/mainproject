"""업무 위험 signal 3종·preferred·missing_data 채널 — LLM 정책 결정서 §3~§5 검증."""

from datetime import date
from decimal import Decimal

from app.logistics.llm.runtime import InterpretationService, LLMSettings, UnavailableProvider
from app.logistics.rules import (
    CAPACITY_TIGHT,
    CAPACITY_TIGHT_POLICY_UNRESOLVED,
    FRESHNESS_PRESSURE_POLICY_UNRESOLVED,
    FRESHNESS_QUALITY_RISK,
    INVENTORY_FRESHNESS_PRESSURE,
    LOT_FRESHNESS_UNRESOLVED,
    SALES_PRIORITY_ADJUSTMENT,
    SCENARIO_ADJUSTMENT_REQUIRED,
    evaluate_procurement_business_signals,
    evaluate_sales_business_signals,
)
from app.logistics.scenario_engine import derive_preferred_adjustment
from app.logistics.schemas import (
    InventoryLotSnapshot,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
    ScenarioAdjustment,
    ScenarioValidationResult,
)
from app.logistics.service import (
    run_logistics_procurement_with_snapshot,
    run_logistics_sales_with_snapshot,
)
from app.logistics.tools import (
    calculate_window_capacity_usage,
    collect_freshness_pressure_inputs,
)

AS_OF = date(2026, 8, 21)


def _disabled_llm_service() -> InterpretationService:
    """LLM 을 타지 않는 서비스 — 결정론 채널 검증에 Provider 가 끼지 않게 한다."""
    return InterpretationService(
        LLMSettings(
            enabled=False,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=0,
        ),
        UnavailableProvider(),
    )


def _lot(**overrides) -> InventoryLotSnapshot:
    lot = {
        "lot_id": "LOT-001",
        "item": "배추",
        "grade": None,
        "available_qty_kg": Decimal(500),
        "remaining_freshness_days": 8,
        "effective_freshness_limit_days": 10,
        "status": "ACTIVE",
        "storage_zone": "COLD_HUMID",
    }
    lot.update(overrides)
    # model_copy(update=) 는 dict 를 모델로 강제 변환하지 않으므로 모델로 만든다.
    return InventoryLotSnapshot(**lot)


def _scenario(verdict: str, adjustments: list[ScenarioAdjustment]) -> ScenarioValidationResult:
    return ScenarioValidationResult(
        label="기본", verdict=verdict, reason_codes=[], adjustments=adjustments
    )


def _quantity_adjustment() -> ScenarioAdjustment:
    return ScenarioAdjustment(axis="quantity", split_date=AS_OF, suggested_qty_kg=Decimal(100))


def _timing_adjustment() -> ScenarioAdjustment:
    return ScenarioAdjustment(
        axis="timing", split_date=AS_OF, suggested_arrival_date=date(2026, 8, 25)
    )


# ---------------------------------------------------------------------------
# CAPACITY_TIGHT — 고정 18일 창 · guaranteed 분모 · 경계 포함
# ---------------------------------------------------------------------------


def test_window_usage_uses_fixed_window_not_proposal_dates(complete_logistics_snapshot):
    """점유 1,000 / guaranteed 8,000 — 제안 없이도 창 사용률이 나온다."""
    usage = calculate_window_capacity_usage(complete_logistics_snapshot, AS_OF)

    assert usage == Decimal("0.125")


def test_capacity_tight_fires_at_boundary_inclusive(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "used_capacity_kg": Decimal(7200),  # 7200/8000 = 정확히 0.90
            "capacity_tight_ratio": Decimal("0.90"),
        }
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert CAPACITY_TIGHT in result["signals"]


def test_capacity_below_threshold_stays_silent(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "used_capacity_kg": Decimal(7100),
            "capacity_tight_ratio": Decimal("0.90"),
        }
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert CAPACITY_TIGHT not in result["signals"]


def test_missing_capacity_policy_skips_with_warning_not_silence(complete_logistics_snapshot):
    """정책값 부재 = 판정 SKIPPED + 경고. 100% 점유여도 판정을 지어내지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={"used_capacity_kg": Decimal(8000), "capacity_tight_ratio": None}
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert CAPACITY_TIGHT not in result["signals"]
    assert CAPACITY_TIGHT_POLICY_UNRESOLVED in result["warnings"]


# ---------------------------------------------------------------------------
# INVENTORY_FRESHNESS_PRESSURE — 유효 한계 분모 · grade=None 포함 · None 제외
# ---------------------------------------------------------------------------


def test_freshness_pressure_fires_at_boundary_inclusive(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot(remaining_freshness_days=3, effective_freshness_limit_days=10)],
            "freshness_pressure_ratio": Decimal("0.30"),  # 3/10 = 정확히 0.30
        }
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert INVENTORY_FRESHNESS_PRESSURE in result["signals"]


def test_freshness_pressure_includes_grade_none_lots(complete_logistics_snapshot):
    """grade=None 은 제외 사유가 아니다 — 현재 전 Lot 이 None 이라 빼면 신호가 죽는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot(grade=None, remaining_freshness_days=2, effective_freshness_limit_days=10)
            ],
            "freshness_pressure_ratio": Decimal("0.30"),
        }
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert INVENTORY_FRESHNESS_PRESSURE in result["signals"]


def test_effective_limit_is_the_denominator_not_operational_raw():
    """중 등급: remaining 6 / 유효 한계 6 = 1.0 — 원값 10 으로 나누면 0.6 이 된다."""
    from app.logistics.schemas import InventoryLogisticsSnapshot

    snapshot = InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=AS_OF,
        on_hand_by_lot=[
            _lot(grade="중", remaining_freshness_days=6, effective_freshness_limit_days=6)
        ],
        in_transit=[],
        confirmed_inbound_schedule=[],
        confirmed_outbound_schedule=[],
        outbound_commitments=[],
        used_capacity_kg=Decimal(500),
        guaranteed_capacity_by_zone_kg=None,
        evidence_refs=[],
    )

    ratios, unresolved = collect_freshness_pressure_inputs(snapshot)

    assert ratios == [Decimal(1)]
    assert unresolved == 0


def test_unresolved_freshness_lot_is_excluded_and_surfaced(complete_logistics_snapshot):
    """remaining=None → 0 취급도 위험 강제도 없이 제외하되, 제외 사실은 경고로 남는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot(remaining_freshness_days=None, effective_freshness_limit_days=None)
            ],
            "freshness_pressure_ratio": Decimal("0.30"),
        }
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert INVENTORY_FRESHNESS_PRESSURE not in result["signals"]
    assert LOT_FRESHNESS_UNRESOLVED in result["warnings"]


def test_non_active_lots_do_not_join_freshness_pressure(complete_logistics_snapshot):
    """격리·검수 재고의 신선도는 매입 압박 신호에 섞지 않는다 — 대상은 가용 재고다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot(
                    status="QUARANTINED",
                    remaining_freshness_days=1,
                    effective_freshness_limit_days=10,
                )
            ],
            "freshness_pressure_ratio": Decimal("0.30"),
        }
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert INVENTORY_FRESHNESS_PRESSURE not in result["signals"]


def test_missing_freshness_policy_skips_with_warning(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot(remaining_freshness_days=1, effective_freshness_limit_days=10)],
            "freshness_pressure_ratio": None,
        }
    )

    result = evaluate_procurement_business_signals(
        as_of=AS_OF, snapshot=snapshot, scenario_results=[]
    )

    assert INVENTORY_FRESHNESS_PRESSURE not in result["signals"]
    assert FRESHNESS_PRESSURE_POLICY_UNRESOLVED in result["warnings"]


# ---------------------------------------------------------------------------
# SCENARIO_ADJUSTMENT_REQUIRED — conditional ≥ 1 에서만
# ---------------------------------------------------------------------------


def test_conditional_scenario_raises_adjustment_signal(complete_logistics_snapshot):
    result = evaluate_procurement_business_signals(
        as_of=AS_OF,
        snapshot=complete_logistics_snapshot,
        scenario_results=[_scenario("conditional", [_quantity_adjustment()])],
    )

    assert SCENARIO_ADJUSTMENT_REQUIRED in result["signals"]


def test_ok_only_and_reject_only_do_not_raise_adjustment_signal(complete_logistics_snapshot):
    for verdict in ("ok", "reject"):
        result = evaluate_procurement_business_signals(
            as_of=AS_OF,
            snapshot=complete_logistics_snapshot,
            scenario_results=[_scenario(verdict, [])],
        )

        assert SCENARIO_ADJUSTMENT_REQUIRED not in result["signals"]


# ---------------------------------------------------------------------------
# preferred_adjustment 집계 — 고유 축 1종만 채택, 혼재·0건은 null
# ---------------------------------------------------------------------------


def test_preferred_is_the_single_axis():
    results = [
        _scenario("conditional", [_quantity_adjustment()]),
        _scenario("conditional", [_quantity_adjustment()]),
    ]

    assert derive_preferred_adjustment(results) == "quantity"


def test_mixed_axes_yield_no_preferred():
    results = [
        _scenario("conditional", [_quantity_adjustment()]),
        _scenario("conditional", [_timing_adjustment()]),
    ]

    assert derive_preferred_adjustment(results) is None


def test_no_adjustments_yield_no_preferred():
    assert derive_preferred_adjustment([_scenario("ok", [])]) is None


# ---------------------------------------------------------------------------
# Sales — 비율 Rule 전환 (status 의존 폐기)
# ---------------------------------------------------------------------------


def test_sales_freshness_risk_comes_from_ratio_not_status(complete_logistics_snapshot):
    """NEEDS_PRIORITY_SHIPMENT status 없이도 비율만으로 판매 위험이 선다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot(status="ACTIVE", remaining_freshness_days=2, effective_freshness_limit_days=10)
            ],
            "freshness_pressure_ratio": Decimal("0.30"),
        }
    )

    result = evaluate_sales_business_signals(snapshot=snapshot)

    assert result["signals"] == [FRESHNESS_QUALITY_RISK]


# ---------------------------------------------------------------------------
# Service 조립 — soft_warnings/missing_data 채널 분리와 preferred 배선
# ---------------------------------------------------------------------------


def test_procurement_response_carries_signal_missing_and_preferred(
    logistics_purchase_payload, complete_logistics_snapshot
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot(remaining_freshness_days=2, effective_freshness_limit_days=10)],
            "used_capacity_kg": Decimal(500),
            "freshness_pressure_ratio": Decimal("0.30"),
            # capacity_tight_ratio 미등록 → 경고가 missing_data 번역으로 나가야 한다
            "capacity_tight_ratio": None,
        }
    )

    response = run_logistics_procurement_with_snapshot(request, snapshot, _disabled_llm_service())

    assert INVENTORY_FRESHNESS_PRESSURE in response.soft_warnings
    assert CAPACITY_TIGHT_POLICY_UNRESOLVED in response.soft_warnings
    # 번역 채널 — 원본 코드가 아니라 무숫자 이름이 실린다
    assert "capacity_tight_policy" in response.missing_data
    assert CAPACITY_TIGHT_POLICY_UNRESOLVED not in response.missing_data
    # 업무 위험은 미확정이 아니다 — missing_data 에 섞이지 않는다
    assert all("FRESHNESS" not in name for name in response.missing_data)


def test_sales_response_sets_priority_preferred_when_risk_fires(
    logistics_sales_payload, complete_logistics_snapshot
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot(remaining_freshness_days=2, effective_freshness_limit_days=10)],
            "freshness_pressure_ratio": Decimal("0.30"),
        }
    )

    response = run_logistics_sales_with_snapshot(request, snapshot, _disabled_llm_service())

    assert FRESHNESS_QUALITY_RISK in response.soft_warnings
    # Rule 이 우선출고를 preferred 로 지정한다 — 이게 없으면 검증기의 preferred 강제와
    # 결합해 판매 추천이 영구 봉쇄된다.
    assert response.preferred_adjustment == SALES_PRIORITY_ADJUSTMENT


def test_sales_wiring_carries_rule_measurements_to_llm_context_facts(
    logistics_sales_payload, complete_logistics_snapshot
):
    """Rule 판정 수치 → Service 전달 → llm_context_facts 까지 실제 배선 검증.

    수치를 테스트가 만들어 넣지 않고 Snapshot 에서 Rule 이 계산한 값이 응답까지
    도달하는지를 본다 — Service 전달 실수(fact 누락)는 단위 테스트로 못 잡는다.
    """
    import json

    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot(remaining_freshness_days=2, effective_freshness_limit_days=10)],
            "freshness_pressure_ratio": Decimal("0.30"),
        }
    )

    class _ValidProvider:
        def generate(self, context, *, retry_guidance=None):
            del context, retry_guidance
            return json.dumps(
                {
                    "summary": "재고의 우선 출고와 품질 위험 검토가 필요합니다.",
                    "risks": ["FRESHNESS_QUALITY_RISK"],
                    "suggested_adjustment": SALES_PRIORITY_ADJUSTMENT,
                },
                ensure_ascii=False,
            )

    service = InterpretationService(
        LLMSettings(
            enabled=True,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=0,
        ),
        _ValidProvider(),
    )

    response = run_logistics_sales_with_snapshot(request, snapshot, service)

    assert response.llm_status == "SUCCESS"
    # Rule 이 Snapshot 에서 계산한 값(위험 Lot 1개 · 잔여비율 2/10)이 formatter 를
    # 거쳐 그대로 도달한다 — 테스트가 수치를 주입하지 않았다.
    assert [(f.fact_id, f.display_value) for f in response.llm_context_facts] == [
        ("freshness_risk_lot_count", "1개"),
        ("freshness_min_remaining_ratio", "20.0% (임계 30%)"),
    ]
    # response_payload 실행이력 자동 기록의 전제 — 직렬화에 facts 가 실린다.
    dumped = response.model_dump(mode="json")
    assert dumped["llm_context_facts"][0]["display_value"] == "1개"


def test_sales_response_without_risk_has_no_preferred(
    logistics_sales_payload, complete_logistics_snapshot
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot(remaining_freshness_days=9, effective_freshness_limit_days=10)],
            "freshness_pressure_ratio": Decimal("0.30"),
        }
    )

    response = run_logistics_sales_with_snapshot(request, snapshot, _disabled_llm_service())

    assert FRESHNESS_QUALITY_RISK not in response.soft_warnings
    assert response.preferred_adjustment is None
