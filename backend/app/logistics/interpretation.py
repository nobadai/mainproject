"""Logistics deterministic Reply projection into the optional LLM layer."""

import logging
from decimal import ROUND_HALF_UP, Decimal

from app.logistics.llm.runtime import InterpretationService, get_interpretation_service
from app.logistics.llm.schemas import ContextFact, SanitizedLLMContext
from app.logistics.rules import (
    BUSINESS_SIGNALS,
    SALES_PRIORITY_ADJUSTMENT,
    SignalMeasurements,
)
from app.logistics.schemas import LogisticsProcurementResponse, LogisticsSalesResponse

logger = logging.getLogger(__name__)

#: fact 상한 (LLM 정책 결정서 v1.3 §5). 초과 시 조용한 절단 금지 — LLM을 호출하지
#: 않고 무숫자 Template을 유지한다 (SKIPPED_TEMPLATE · llm_context_facts=[] · 로그).
#:
#: ★ 이 가드는 **의도적 휴면 상태다.** 현행 조립기의 실측 최대는 signal당 2개
#:   (신선도) · 전체 4개(capacity 1 + 신선도 2 + 시나리오 1)라 상한 3/8에 닿을 수
#:   없다. 죽은 코드가 아니라 signal·fact가 늘어나는 날을 위한 확장 자리다 —
#:   _COMPOSITE_SIGNALS 휴면과 같은 성격이다.
_MAX_FACTS_PER_SIGNAL = 3
_MAX_CONTEXT_FACTS = 8

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


def format_measured_percent(value: Decimal) -> str:
    """측정 비율의 확정 표기 — 백분율 소수 1자리 · ROUND_HALF_UP (표기 스펙 2026-08-31).

    정수 반올림은 경계(89.6% → "90%")에서 표기와 판정이 어긋나 보이고, 내림은
    사용률·잔여비율의 위험 방향이 반대라 한쪽에서 위험을 과장한다. float round()는
    banker's rounding이라 같은 값 → 같은 표기 보장이 깨진다 — 쓰지 않는다.
    """
    percent = (value * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{percent}%"


def format_policy_percent(value: Decimal) -> str:
    """임계(정책값)의 확정 표기 — 유효 정밀도 보존 + trailing zero 제거.

    0.90 → "90%" · 0.925 → "92.5%". 정수로 강제하면 실측 후 임계가 소수가 될 때
    다시 의미가 틀어진다. 백분율 소수 2자리를 상한으로 둔다 — value_numeric 은
    자릿수 제약이 없는 numeric 이라, 실측 갱신값이 길게 들어와도 표기가
    "90.12345678%" 로 늘어나지 않게 하기 위해서다.
    """
    percent = (value * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{percent.normalize():f}%"


def format_ratio_with_threshold(measured: Decimal, threshold: Decimal) -> str:
    """관계 수치 한 fact 표기 — 라벨-값 뒤바뀜을 구조로 차단한다."""
    return f"{format_measured_percent(measured)} (임계 {format_policy_percent(threshold)})"


def format_count(value: int, unit: str) -> str:
    """건수·개수 표기 — 숫자마다 단위를 붙인다 (단위 없는 숫자는 인용 불가)."""
    return f"{value}{unit}"


def _build_signal_facts(signal: str, measurements: SignalMeasurements) -> list[ContextFact]:
    """signal별 fact 조립 (v1.3 §5). 조정 축은 fact로 싣지 않는다 —
    allowed_adjustments·preferred_adjustment가 정본이다."""
    if signal == "CAPACITY_TIGHT":
        usage = measurements.get("capacity_window_usage")
        threshold = measurements.get("capacity_tight_ratio")
        if usage is None or threshold is None:
            return []
        return [
            ContextFact(
                fact_id="capacity_window_usage",
                label="판정 창 최대 창고 사용률",
                display_value=format_ratio_with_threshold(usage, threshold),
            )
        ]
    if signal in ("INVENTORY_FRESHNESS_PRESSURE", "FRESHNESS_QUALITY_RISK"):
        count = measurements.get("freshness_risk_lot_count")
        min_ratio = measurements.get("freshness_min_remaining_ratio")
        threshold = measurements.get("freshness_pressure_ratio")
        if count is None or min_ratio is None or threshold is None:
            return []
        return [
            ContextFact(
                fact_id="freshness_risk_lot_count",
                label="신선도 임박 가용 Lot 수",
                display_value=format_count(count, "개"),
            ),
            ContextFact(
                fact_id="freshness_min_remaining_ratio",
                label="최소 신선도 잔여 비율",
                display_value=format_ratio_with_threshold(min_ratio, threshold),
            ),
        ]
    if signal == "SCENARIO_ADJUSTMENT_REQUIRED":
        conditional = measurements.get("scenario_conditional_count")
        total = measurements.get("scenario_total_count")
        if conditional is None or total is None:
            return []
        conditional_text = format_count(conditional, "건")
        total_text = format_count(total, "건")
        return [
            ContextFact(
                fact_id="scenario_conditional_count",
                label="조정 필요 시나리오 수",
                display_value=f"조건부 {conditional_text} (전체 {total_text})",
            )
        ]
    return []


def _assemble_facts(
    signals: list[str],
    measurements: SignalMeasurements,
) -> tuple[list[ContextFact], bool]:
    """signal 전체의 fact 목록과 상한 초과 여부. 초과 시 조용한 절단 금지 —
    빈 목록 + True를 반환하고 호출부가 LLM을 호출하지 않는다 (Core는 정상)."""
    facts: list[ContextFact] = []
    seen_fact_ids: set[str] = set()
    for signal in signals:
        signal_facts = _build_signal_facts(signal, measurements)
        # fact_id 중복 방어 — 신선도 두 signal(매입·판매)이 같은 fact_id 를 내는
        # 구조라, 사이클 분리가 무너져 공존하게 되면 같은 fact 가 두 번 나간다.
        # 첫 것만 유지하고 사실을 로그로 남긴다 (같은 값의 중복이라 의미 손실 없음).
        deduped = [fact for fact in signal_facts if fact.fact_id not in seen_fact_ids]
        if len(deduped) < len(signal_facts):
            logger.warning("Duplicate logistics fact_id dropped: signal=%s", signal)
        signal_facts = deduped
        seen_fact_ids.update(fact.fact_id for fact in signal_facts)
        if len(signal_facts) > _MAX_FACTS_PER_SIGNAL:
            logger.warning(
                "Logistics fact overflow: signal=%s count=%d limit=%d — LLM skipped",
                signal,
                len(signal_facts),
                _MAX_FACTS_PER_SIGNAL,
            )
            return [], True
        facts.extend(signal_facts)
    if len(facts) > _MAX_CONTEXT_FACTS:
        logger.warning(
            "Logistics fact overflow: total=%d limit=%d — LLM skipped",
            len(facts),
            _MAX_CONTEXT_FACTS,
        )
        return [], True
    return facts, False


def build_logistics_context(
    response: LogisticsProcurementResponse | LogisticsSalesResponse,
    measurements: SignalMeasurements | None = None,
) -> tuple[SanitizedLLMContext, bool]:
    """결정론 응답에서 LLM Context 를 조립한다. 반환은 (context, facts_overflow).

    signals 와 missing_data 는 저장 위치가 아니라 **코드의 의미**로 분류한다 —
    soft_warnings 안의 업무 위험(BUSINESS_SIGNALS)만 signals 로 가고, 나머지
    미확정 계열은 response.missing_data(이미 번역됨)로 전달된다. facts 는 판정에
    실제 사용된 수치의 확정 표기뿐이다 — 원본 DB row·lot_id·거래처·날짜·판정에
    쓰이지 않은 수치는 싣지 않는다. 이 Context 가 외부 Provider 전송 경계다.
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
    facts, overflow = _assemble_facts(signals, measurements or {})
    context = SanitizedLLMContext(
        domain="LOGISTICS",
        signals=signals,
        facts=facts,
        allowed_adjustments=allowed_adjustments,
        # Rule/Scenario Engine 이 정한 값을 그대로 나른다 — LLM 이 고르지 않는다.
        preferred_adjustment=response.preferred_adjustment,
        missing_data=list(response.missing_data),
    )
    return context, overflow


def enrich_logistics_response[
    LogisticsResponse: (LogisticsProcurementResponse, LogisticsSalesResponse)
](
    response: LogisticsResponse,
    interpretation_service: InterpretationService | None = None,
    measurements: SignalMeasurements | None = None,
) -> LogisticsResponse:
    service = interpretation_service or get_interpretation_service()
    context, facts_overflow = build_logistics_context(response, measurements)
    result = service.interpret(
        context,
        runtime_ready=response.runtime_status == "READY",
        # FAIL 만 차단한다 — UNRESOLVED(미확인)는 호출 자체를 막지 않고, 그 사실에
        # 대한 추측만 금지된다 (LLM 정책 결정서 §2 — 17-A). 영구 UNRESOLVED 인
        # LOG-H02 하나로 Procurement LLM 이 구조적으로 죽는 것을 막는 수정이다.
        has_blocking_constraints=any(
            constraint.status == "FAIL" for constraint in response.hard_constraints
        ),
        facts_overflow=facts_overflow,
    )
    update = result.model_dump(exclude={"interpretation", "llm_context_facts"})
    update["interpretation"] = result.interpretation
    # model_copy 는 검증하지 않으므로 dict 가 아니라 모델 객체를 그대로 싣는다.
    update["llm_context_facts"] = list(result.llm_context_facts)
    return response.model_copy(update=update)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
