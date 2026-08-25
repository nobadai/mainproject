"""Logistics deterministic Reply projection into the optional LLM layer."""

from app.logistics.llm.runtime import InterpretationService, get_interpretation_service
from app.logistics.llm.schemas import SanitizedLLMContext
from app.logistics.schemas import LogisticsProcurementResponse, LogisticsSalesResponse

_LOGISTICS_FACTS = {
    "AS_OF_MISMATCH": "요청 기준시점과 재고물류 기준시점이 일치하지 않습니다.",
    "CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED": "확정 입고 일정 정보가 부족합니다.",
    "CONFIRMED_OUTBOUND_SCHEDULE_UNRESOLVED": "확정 출고 일정 정보가 부족합니다.",
    "FRESHNESS_QUALITY_RISK": "재고의 우선 출고와 품질 위험 검토가 필요합니다.",
    "H1_FUTURE_OCCUPANCY_UNRESOLVED": "승인 매입의 미래 점유를 확정할 수 없습니다.",
    "IN_TRANSIT_SCHEDULE_UNRESOLVED": "운송 중 재고와 입고 일정의 관계가 확정되지 않았습니다.",
    "IN_TRANSIT_UNRESOLVED": "운송 중 재고 정보가 부족합니다.",
    "LOG-H01": "창고 수용 가능 여부를 확정할 수 없습니다.",
    "LOG-H02": "구역별 수용 가능 여부를 확정할 수 없습니다.",
    "LOG-H03": "일일 입고 처리 가능 여부를 확정할 수 없습니다.",
    "LOG-H04": "입고 운송 처리 가능 여부를 확정할 수 없습니다.",
    "LOG-H05": "입고 리드타임 가능 여부를 확정할 수 없습니다.",
    "N17": "공유 출고 처리 가능량이 확정되지 않았습니다.",
    "N17-LOT": "재고 단위 출고 제약 정보가 부족합니다.",
    "PROVISIONAL_CAPACITY_EXCLUDED_FROM_HARD_LIMIT": (
        "잠정 수용량은 확정 한도에서 제외되었습니다."
    ),
    "REQUIRED_LOGISTICS_SNAPSHOT_MISSING": "재고물류 판단에 필요한 상태 정보가 부족합니다.",
    "SNAPSHOT_ID_UNRESOLVED": "재고물류 스냅샷 식별자가 확정되지 않았습니다.",
}
_FRESHNESS_STATUSES = {"NEEDS_PRIORITY_SHIPMENT"}
_PRIORITY_ADJUSTMENT = "우선 출고 대상으로 검토합니다."


def build_logistics_context(
    response: LogisticsProcurementResponse | LogisticsSalesResponse,
) -> SanitizedLLMContext:
    constraint_signals = [
        constraint.code for constraint in response.hard_constraints if constraint.passed is not True
    ]
    signals = _unique([*constraint_signals, *response.soft_warnings])
    allowed_adjustments: list[str] = []
    if isinstance(response, LogisticsSalesResponse) and any(
        lot.status in _FRESHNESS_STATUSES for lot in response.lot_constraints
    ):
        signals = _unique([*signals, "FRESHNESS_QUALITY_RISK"])
        allowed_adjustments.append(_PRIORITY_ADJUSTMENT)
    return SanitizedLLMContext(
        domain="LOGISTICS",
        signals=signals,
        facts=[
            _LOGISTICS_FACTS.get(signal, "정의되지 않은 재고물류 신호가 확인되었습니다.")
            for signal in signals
        ],
        allowed_adjustments=allowed_adjustments,
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
        has_blocking_constraints=any(
            constraint.passed is not True for constraint in response.hard_constraints
        ),
    )
    update = result.model_dump(exclude={"interpretation"})
    update["interpretation"] = result.interpretation
    return response.model_copy(update=update)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
