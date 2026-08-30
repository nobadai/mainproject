"""Logistics deterministic Reply projection into the optional LLM layer."""

import logging

from app.logistics.llm.runtime import InterpretationService, get_interpretation_service
from app.logistics.llm.schemas import SanitizedLLMContext
from app.logistics.rules import BUSINESS_SIGNALS, SALES_PRIORITY_ADJUSTMENT
from app.logistics.schemas import LogisticsProcurementResponse, LogisticsSalesResponse

logger = logging.getLogger(__name__)

#: 업무 위험 signal 의 사람용 의미. signals 에 실리는 코드만 여기 있으면 된다 —
#: 데이터/정책 미확정은 missing_data 번역명으로 가므로 facts 를 만들지 않는다.
_LOGISTICS_FACTS = {
    "CAPACITY_TIGHT": "확정 입출고를 반영한 미래 창고 여유가 운영 임계 수준 이하입니다.",
    "FRESHNESS_QUALITY_RISK": "재고의 우선 출고와 품질 위험 검토가 필요합니다.",
    "INVENTORY_FRESHNESS_PRESSURE": "기존 재고의 신선도 잔여가 보관한계 대비 충분하지 않습니다.",
    "SCENARIO_ADJUSTMENT_REQUIRED": "매입안이 물류 경계에 걸려 조정 검토가 필요합니다.",
}

#: 내부 미확정 코드 → 사람이 읽을 무숫자 번역명. 코드 1개 → 이름 1개이며,
#: 여러 미확정을 하나로 뭉개지 않는다 (LLM 정책 결정서 §5).
_MISSING_DATA_NAMES = {
    "LOG-H01": "warehouse_capacity_policy",
    "LOG-H02": "zone_capacity",
    "LOG-H03": "daily_inbound_capacity",
    "LOG-H04": "inbound_transport_capacity",
    "LOG-H05": "inbound_lead_time",
    "N17": "shared_outbound_capacity",
    "N17-LOT": "lot_outbound_constraints",
    "IN_TRANSIT_SCHEDULE_UNRESOLVED": "in_transit_schedule",
    "CONFIRMED_OUTBOUND_ITEM_UNRESOLVED": "confirmed_outbound_item",
    "AS_OF_MISMATCH": "snapshot_as_of",
    "REQUIRED_LOGISTICS_SNAPSHOT_MISSING": "logistics_snapshot",
    "SNAPSHOT_ID_UNRESOLVED": "snapshot_id",
    "GRADE_VOCABULARY_UNRESOLVED": "grade_vocabulary",
    "PROVISIONAL_CAPACITY_EXCLUDED_FROM_HARD_LIMIT": "provisional_capacity_basis",
    "IN_TRANSIT_UNRESOLVED": "in_transit_records",
    "CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED": "confirmed_inbound_schedule",
    "CONFIRMED_OUTBOUND_SCHEDULE_UNRESOLVED": "confirmed_outbound_schedule",
    "H1_FUTURE_OCCUPANCY_UNRESOLVED": "future_occupancy",
    "CAPACITY_TIGHT_POLICY_UNRESOLVED": "capacity_tight_policy",
    "FRESHNESS_PRESSURE_POLICY_UNRESOLVED": "freshness_pressure_policy",
    "LOT_FRESHNESS_UNRESOLVED": "lot_freshness",
}
#: 번역표에 없는 새 코드가 왔을 때의 대체 이름. raw 코드(숫자 포함 가능)를 LLM
#: Context 로 보내면 무숫자 경계가 깨진다 — 결정론 응답에는 원본이 그대로 남고,
#: 여기서는 이 generic 이름으로 대체하며 원본은 로그에 남긴다 (조용한 폐기 금지).
_UNMAPPED_MISSING_DATA = "unrecognized_missing_information"

#: Procurement 의 허용 조정 축 — 구조적 어휘. Sales 우선출고 문장과 섞지 않는다.
_PROCUREMENT_ALLOWED_ADJUSTMENTS = ["quantity", "timing"]


def translate_missing_data(codes: list[str]) -> list[str]:
    """내부 코드 목록을 사람용 무숫자 번역명으로 옮긴다 (중복 제거, 순서 유지)."""
    names: list[str] = []
    for code in dict.fromkeys(codes):
        name = _MISSING_DATA_NAMES.get(code)
        if name is None:
            logger.warning("Unmapped logistics missing-data code: %s", code)
            name = _UNMAPPED_MISSING_DATA
        if name not in names:
            names.append(name)
    return names


def build_logistics_context(
    response: LogisticsProcurementResponse | LogisticsSalesResponse,
) -> SanitizedLLMContext:
    """결정론 응답에서 LLM Context 를 조립한다.

    signals 와 missing_data 는 저장 위치가 아니라 **코드의 의미**로 분류한다 —
    soft_warnings 안의 업무 위험(BUSINESS_SIGNALS)만 signals 로 가고, 나머지
    미확정 계열은 response.missing_data(이미 번역됨)로 전달된다. 원본 숫자·lot_id
    는 싣지 않는다 — 이 Context 가 외부 Provider 전송 경계다.
    """
    signals = _unique(
        [warning for warning in response.soft_warnings if warning in BUSINESS_SIGNALS]
    )
    if isinstance(response, LogisticsSalesResponse):
        allowed_adjustments = (
            [SALES_PRIORITY_ADJUSTMENT] if response.preferred_adjustment is not None else []
        )
    else:
        allowed_adjustments = list(_PROCUREMENT_ALLOWED_ADJUSTMENTS)
    return SanitizedLLMContext(
        domain="LOGISTICS",
        signals=signals,
        facts=[
            _LOGISTICS_FACTS.get(signal, "정의되지 않은 재고물류 신호가 확인되었습니다.")
            for signal in signals
        ],
        allowed_adjustments=allowed_adjustments,
        # Rule/Scenario Engine 이 정한 값을 그대로 나른다 — LLM 이 고르지 않는다.
        preferred_adjustment=response.preferred_adjustment,
        missing_data=list(response.missing_data),
    )


def enrich_logistics_response[
    LogisticsResponse: (LogisticsProcurementResponse, LogisticsSalesResponse)
](
    response: LogisticsResponse,
    interpretation_service: InterpretationService | None = None,
) -> LogisticsResponse:
    service = interpretation_service or get_interpretation_service()
    context = build_logistics_context(response)
    result = service.interpret(
        context,
        runtime_ready=response.runtime_status == "READY",
        # FAIL 만 차단한다 — UNRESOLVED(미확인)는 호출 자체를 막지 않고, 그 사실에
        # 대한 추측만 금지된다 (LLM 정책 결정서 §2 — 17-A). 영구 UNRESOLVED 인
        # LOG-H02 하나로 Procurement LLM 이 구조적으로 죽는 것을 막는 수정이다.
        has_blocking_constraints=any(
            constraint.status == "FAIL" for constraint in response.hard_constraints
        ),
    )
    update = result.model_dump(exclude={"interpretation"})
    update["interpretation"] = result.interpretation
    return response.model_copy(update=update)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
