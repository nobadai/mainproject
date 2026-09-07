"""Logistics deterministic Reply projection into the optional LLM layer.

두 호출자가 **같은 조립기**를 지난다 (#385).

```text
독립 Service    LogisticsProcurementResponse | LogisticsSalesResponse
                → build_logistics_context (얇은 wrapper)
Master 어댑터   signals · measurements · preferred · missing 원재료
                → build_sanitized_context
                                     ↘ SanitizedLLMContext — 외부 Provider 전송 경계
```

★ Master-facing 해석은 **명시적 opt-in** 이다 — `master_interpretation_service` 참조.
"""

import logging
import os
from collections.abc import Sequence
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal

from app.logistics.llm.runtime import (
    InterpretationService,
    UnavailableProvider,
    get_interpretation_service,
    get_llm_settings,
)
from app.logistics.llm.schemas import ContextFact, InterpretationResult, SanitizedLLMContext
from app.logistics.rules import (
    BUSINESS_SIGNALS,
    SALES_PRIORITY_ADJUSTMENT,
    SignalMeasurements,
)
from app.logistics.schemas import (
    LogisticsCycle,
    LogisticsProcurementResponse,
    LogisticsSalesResponse,
)

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

#: 이미 번역된 이름의 전체 집합 — `translate_missing_data` 가 **멱등**이 되는 근거다.
#: 독립 Service 응답의 `missing_data` 는 이미 번역돼 있고 Master 어댑터는 raw 코드를
#: 준다. 한 조립기가 둘을 다 받으려면 번역명은 그대로 통과해야 한다 (#385).
#: 코드(대문자 · `LOG-H01`)와 이름(소문자 snake)은 어휘가 겹치지 않는다.
_TRANSLATED_MISSING_DATA_NAMES = frozenset({*_MISSING_DATA_NAMES.values(), _UNMAPPED_MISSING_DATA})

#: Procurement 의 허용 조정 축 — 구조적 어휘. Sales 우선출고 문장과 섞지 않는다.
_PROCUREMENT_ALLOWED_ADJUSTMENTS = ["quantity", "timing"]

#: Master-facing 해석의 **명시적 opt-in** 환경변수 (#385). 전역 폴백이 없다 —
#: `LLM_ENABLED`·`LOGISTICS_LLM_ENABLED` 는 독립 `/logistics/*` 경로의 손잡이고, 그 둘은
#: 값이 없으면 **켜짐**으로 읽힌다(`get_llm_settings`). 그 기본값만으로 마스터 동기
#: 경로에 외부 호출이 얹히면 안 되므로 이 변수는 값이 없으면 **꺼짐**이다.
MASTER_LLM_ENV = "LOGISTICS_MASTER_LLM_ENABLED"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def translate_missing_data(codes: Sequence[str]) -> list[str]:
    """내부 코드 목록을 사람용 무숫자 번역명으로 옮긴다 (중복 제거, 순서 유지).

    ★ **멱등이다** — 이미 번역된 이름은 그대로 통과한다. 그래서 Service 응답(번역됨)과
      어댑터 원재료(raw 코드)가 같은 조립기를 지날 수 있다 (#385).
    """
    names: list[str] = []
    for code in dict.fromkeys(codes):
        if code in _TRANSLATED_MISSING_DATA_NAMES:
            name = code
        else:
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
    다시 의미가 틀어진다. 자릿수 상한은 여기서 두지 않는다 — 정밀도는 정책값
    (`agent_policy_config.value_numeric`)이 소유하고 formatter 는 그대로 보존한다.
    표기가 길어지면 그것은 정책값 등록 시 자릿수를 정할 사안이지 formatter 가
    몰래 반올림할 사안이 아니다 (PR #104 리뷰 반영).
    """
    percent = value * Decimal(100)
    return f"{percent.normalize():f}%"


def format_ratio_with_threshold(measured: Decimal, threshold: Decimal) -> str:
    """관계 수치 한 fact 표기 — 라벨-값 오용 위험을 줄인다.

    검증기는 숫자 토큰만 대조하므로 두 토큰을 서로 바꿔 쓰는 것까지 막지는
    못한다 — 의미 관계의 semantic validation 은 v1.3 범위 밖이다 (결정서 §5).
    """
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
    """signal 전체의 fact 목록과 fail-closed 플래그 (상한 초과 또는 조립 실패).

    True 면 호출부가 LLM 을 호출하지 않는다 (Core 는 정상). 상한 초과는 조용한
    절단 금지이고, 조립 실패(signal 은 섰는데 판정 수치가 전달되지 않음)는 배선
    버그다 — 확인된 fact 없이 해석시키지 않고 무숫자 Template 로 남긴다."""
    facts: list[ContextFact] = []
    seen_fact_ids: set[str] = set()
    for signal in signals:
        signal_facts = _build_signal_facts(signal, measurements)
        if not signal_facts:
            # 업무 signal 은 전부 fact 를 동반해야 한다 — measurements 미전달은
            # Rule→Service 배선이 깨진 상태이므로 fail-closed 로 LLM 을 건너뛴다.
            logger.warning(
                "Logistics fact assembly failed: signal=%s has no measurements — LLM skipped",
                signal,
            )
            return [], True
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


def build_sanitized_context(
    *,
    cycle: LogisticsCycle,
    signals: Sequence[str],
    measurements: SignalMeasurements | None,
    preferred_adjustment: str | None,
    missing_data: Sequence[str],
) -> tuple[SanitizedLLMContext, bool]:
    """결정론 **원재료**에서 LLM Context 를 조립한다. 반환은 (context, facts_incomplete).

    독립 Service 와 Master 어댑터가 **같은 조립기**를 지난다 (#385). 응답 타입이 아니라
    원재료를 받는 이유는 어댑터가 `AgentReply` 를 내기 때문이다 — 그것을 Service 응답으로
    되살리면 없는 필드를 지어내게 된다.

    ```text
    signals          BUSINESS_SIGNALS 만 남긴다 — 미확정 코드는 signal 이 아니다
    measurements     판정에 실제 쓰인 수치 → 확정 표기 facts (없으면 fail-closed)
    preferred        Rule/Scenario Engine 이 정한 값 그대로 — LLM 이 고르지 않는다
    missing_data     raw 코드든 번역명이든 `translate_missing_data` 를 지난다 —
                     `LOG-H02` 같은 숫자 든 코드가 무숫자 경계를 우회하지 못한다
    ```

    facts 는 판정에 실제 사용된 수치의 확정 표기뿐이다 — 원본 DB row·lot_id·거래처·
    날짜·판정에 쓰이지 않은 수치는 싣지 않는다. 이 Context 가 외부 Provider 전송 경계다.
    """
    if cycle not in ("PROCUREMENT", "SALES"):
        raise ValueError(f"unknown logistics cycle: {cycle!r}")
    business_signals = _unique([signal for signal in signals if signal in BUSINESS_SIGNALS])
    if cycle == "SALES":
        allowed_adjustments = (
            [SALES_PRIORITY_ADJUSTMENT] if preferred_adjustment is not None else []
        )
    else:
        allowed_adjustments = list(_PROCUREMENT_ALLOWED_ADJUSTMENTS)
    facts, incomplete = _assemble_facts(business_signals, measurements or {})
    context = SanitizedLLMContext(
        domain="LOGISTICS",
        signals=business_signals,
        facts=facts,
        allowed_adjustments=allowed_adjustments,
        # Rule/Scenario Engine 이 정한 값을 그대로 나른다 — LLM 이 고르지 않는다.
        preferred_adjustment=preferred_adjustment,
        missing_data=translate_missing_data(missing_data),
    )
    return context, incomplete


def build_logistics_context(
    response: LogisticsProcurementResponse | LogisticsSalesResponse,
    measurements: SignalMeasurements | None = None,
) -> tuple[SanitizedLLMContext, bool]:
    """결정론 응답에서 LLM Context 를 조립한다 — `build_sanitized_context` 의 얇은 wrapper.

    signals 와 missing_data 는 저장 위치가 아니라 **코드의 의미**로 분류한다 —
    soft_warnings 안의 업무 위험(BUSINESS_SIGNALS)만 signals 로 가고, 나머지
    미확정 계열은 response.missing_data(이미 번역됨 — 번역이 멱등이라 그대로 통과)로
    전달된다. 독립 Service 경로의 동작은 추출 전과 같다 (wrapper 등가 테스트가 고정).
    """
    return build_sanitized_context(
        cycle="SALES" if isinstance(response, LogisticsSalesResponse) else "PROCUREMENT",
        signals=response.soft_warnings,
        measurements=measurements,
        preferred_adjustment=response.preferred_adjustment,
        missing_data=response.missing_data,
    )


def enrich_logistics_response[
    LogisticsResponse: (LogisticsProcurementResponse, LogisticsSalesResponse)
](
    response: LogisticsResponse,
    interpretation_service: InterpretationService | None = None,
    measurements: SignalMeasurements | None = None,
) -> LogisticsResponse:
    service = interpretation_service or get_interpretation_service()
    context, facts_incomplete = build_logistics_context(response, measurements)
    result = service.interpret(
        context,
        runtime_ready=response.runtime_status == "READY",
        # FAIL 만 차단한다 — UNRESOLVED(미확인)는 호출 자체를 막지 않고, 그 사실에
        # 대한 추측만 금지된다 (LLM 정책 결정서 §2 — 17-A). 영구 UNRESOLVED 인
        # LOG-H02 하나로 Procurement LLM 이 구조적으로 죽는 것을 막는 수정이다.
        has_blocking_constraints=any(
            constraint.status == "FAIL" for constraint in response.hard_constraints
        ),
        facts_incomplete=facts_incomplete,
    )
    update = result.model_dump(exclude={"interpretation", "llm_context_facts"})
    update["interpretation"] = result.interpretation
    # model_copy 는 검증하지 않으므로 dict 가 아니라 모델 객체를 그대로 싣는다.
    update["llm_context_facts"] = list(result.llm_context_facts)
    return response.model_copy(update=update)


# ---------------------------------------------------------------------------
# Master-facing 해석 — 명시적 opt-in (#385)
# ---------------------------------------------------------------------------


def master_llm_enabled() -> bool:
    """Master-facing 해석의 opt-in 여부 — `LOGISTICS_MASTER_LLM_ENABLED` **하나만** 본다.

    전역 `LLM_ENABLED` 로 폴백하지 않는다. 그 값은 독립 경로가 기본 `True` 로 읽는
    손잡이라, 폴백하면 *"설정 부재"* 가 곧 *"마스터 경로도 켜짐"* 이 된다 — 이 함수가
    막는 것이 그것이다.
    """
    value = os.getenv(MASTER_LLM_ENV)
    if value is None:
        return False
    return value.strip().lower() in _TRUE_VALUES


def master_interpretation_service() -> InterpretationService:
    """Master-facing 경로가 쓰는 해석 서비스 (#385). **설정 부재 = DISABLED.**

    ```text
    opt-in 없음 · 또는 물류 LLM 자체가 꺼짐   enabled=False + UnavailableProvider
                                            → 외부 클라이언트를 만들지도 않는다
    opt-in 있음 · 물류 LLM 켜짐               독립 경로와 같은 Provider (Ollama · Gemini)
    ```

    ★ 두 조건의 AND 다. `LOGISTICS_MASTER_LLM_ENABLED` 는 마스터 경로를 **추가로** 여는
      손잡이지, 꺼 둔 물류 LLM 을 마스터 경로에서만 되살리는 손잡이가 아니다.
    ★ 여기서는 네트워크가 열리지 않는다 — Provider 는 `generate()` 시점에만 요청을 만든다.
    """
    settings = get_llm_settings()  # ★ 먼저 부른다 — `.env` 를 읽어 opt-in 값을 채운다
    if not (master_llm_enabled() and settings.enabled):
        return InterpretationService(replace(settings, enabled=False), UnavailableProvider())
    return get_interpretation_service()


def uncalled_interpretation(service: InterpretationService) -> InterpretationResult:
    """LLM 을 부를 자리에 **이르지 못한** 회신의 상태 — DISABLED 또는 SKIPPED_TEMPLATE.

    입력이 없어 판정을 못 낸 회신(RUNTIME_NOT_READY · ERROR)에도 *"켜져 있었는데 안
    불렀다"* 와 *"애초에 꺼져 있다"* 는 다른 사실이다. 어휘 판정을 여기서 다시 적지 않고
    서비스 자신의 게이트(`runtime_ready=False`)에 맡긴다 — Provider 는 부르지 않는다.
    """
    context, _ = build_sanitized_context(
        cycle="PROCUREMENT",
        signals=[],
        measurements=None,
        preferred_adjustment=None,
        missing_data=[],
    )
    return service.interpret(context, runtime_ready=False, has_blocking_constraints=False)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
