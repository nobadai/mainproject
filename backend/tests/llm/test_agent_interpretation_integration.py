import json
from decimal import Decimal

from app.finance.interpretation import build_finance_context, enrich_finance_response
from app.finance.llm.runtime import (
    InterpretationService as FinanceInterpretationService,
)
from app.finance.llm.runtime import LLMSettings as FinanceLLMSettings
from app.finance.schemas import FinanceBand, FinanceProcurementResponse
from app.logistics.interpretation import build_logistics_context, enrich_logistics_response
from app.logistics.llm.runtime import (
    InterpretationService as LogisticsInterpretationService,
)
from app.logistics.llm.runtime import LLMSettings as LogisticsLLMSettings
from app.logistics.schemas import LogisticsSalesResponse

_LLM_FIELDS = {
    "interpretation",
    "llm_status",
    "llm_provider",
    "llm_model",
    "llm_attempts",
    "llm_fallback_used",
    "llm_error_kind",
}


class FailingProvider:
    def generate(self, context, *, retry_guidance=None):
        del context, retry_guidance
        raise RuntimeError("ollama unavailable")


def _failing_finance_service():
    return FinanceInterpretationService(
        FinanceLLMSettings(
            enabled=True,
            provider="ollama",
            model="gemma3:4b",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        FailingProvider(),
    )


def _failing_logistics_service():
    return LogisticsInterpretationService(
        LogisticsLLMSettings(
            enabled=True,
            provider="ollama",
            model="gemma3:4b",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        FailingProvider(),
    )


def test_finance_context_contains_only_sanitized_meanings():
    response = FinanceProcurementResponse(
        as_of="2025-12-31",
        snapshot_id=None,
        runtime_status="READY",
        verdict="PASS",
        band=FinanceBand(max_feasible_amount_krw=Decimal("16091273.77")),
        base_projected_cash_min=None,
        base_cash_priority=None,
        hard_constraints=[],
        soft_warnings=["COST_MISMATCH", "PAYABLES_DUE_SOON"],
        suggested_adjustment={"max_amount_krw": Decimal("16091273.77")},
        evidences=[],
    )

    context = build_finance_context(response)
    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)

    assert context.signals == ["COST_MISMATCH", "PAYABLES_DUE_SOON"]
    assert "16091273" not in serialized
    assert "2025-12-31" not in serialized


def test_logistics_context_does_not_expose_freshness_number():
    # 신선도 위험은 Lot status 가 아니라 비율 Rule 이 판정해 soft_warnings 로 온다
    # (LLM 정책 결정서 §3 — status 생성 주체 부재로 status 의존 폐기).
    response = LogisticsSalesResponse(
        snapshot_id=None,
        approval_id="H1",
        runtime_status="READY",
        verdict="PASS",
        daily_outbound_capacity_kg=Decimal(1000),
        lot_constraints=[
            {
                "lot_id": "LOT",
                "item": "배추",
                "available_qty_kg": Decimal(800),
                "remaining_freshness_days": 2,
                "status": "ACTIVE",
            }
        ],
        hard_constraints=[],
        soft_warnings=["FRESHNESS_QUALITY_RISK", "SNAPSHOT_ID_UNRESOLVED"],
        missing_data=["snapshot_id"],
        preferred_adjustment="우선 출고 대상으로 검토합니다.",
    )

    context = build_logistics_context(response)
    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)

    # 데이터 미확정 코드는 signals 에 섞이지 않는다 — 의미 기준 분류 (41-A).
    assert context.signals == ["FRESHNESS_QUALITY_RISK"]
    assert context.allowed_adjustments == ["우선 출고 대상으로 검토합니다."]
    assert context.preferred_adjustment == "우선 출고 대상으로 검토합니다."
    assert context.missing_data == ["snapshot_id"]
    assert "1000" not in serialized
    assert "800" not in serialized
    assert '"2"' not in serialized


def test_llm_failure_preserves_all_logistics_deterministic_fields():
    response = LogisticsSalesResponse(
        snapshot_id=None,
        approval_id="H1",
        runtime_status="READY",
        verdict="PASS",
        daily_outbound_capacity_kg=Decimal(1000),
        lot_constraints=[
            {
                "lot_id": "LOT",
                "item": "배추",
                "available_qty_kg": Decimal(800),
                "remaining_freshness_days": 2,
                "status": "ACTIVE",
            }
        ],
        hard_constraints=[],
        # Rule 이 판정한 업무 위험이 있어야 LLM 이 호출되고, 그 호출이 실패해야
        # FALLBACK 경로가 재현된다.
        soft_warnings=["FRESHNESS_QUALITY_RISK"],
        preferred_adjustment="우선 출고 대상으로 검토합니다.",
    )
    deterministic = response.model_dump(exclude=_LLM_FIELDS)

    enriched = enrich_logistics_response(response, _failing_logistics_service())

    assert enriched.model_dump(exclude=_LLM_FIELDS) == deterministic
    assert enriched.llm_status == "FALLBACK"
    assert enriched.llm_fallback_used is True
    assert enriched.llm_attempts == 2


def test_finance_hard_constraint_uses_template_without_provider_call():
    response = FinanceProcurementResponse(
        as_of="2025-12-31",
        snapshot_id=None,
        runtime_status="RUNTIME_NOT_READY",
        verdict=None,
        band=FinanceBand(max_feasible_amount_krw=None),
        base_projected_cash_min=None,
        base_cash_priority=None,
        hard_constraints=["AS_OF_MISMATCH"],
        soft_warnings=[],
        suggested_adjustment=None,
        evidences=[],
    )
    deterministic = response.model_dump(exclude=_LLM_FIELDS)

    enriched = enrich_finance_response(response, _failing_finance_service())

    assert enriched.model_dump(exclude=_LLM_FIELDS) == deterministic
    assert enriched.llm_status == "SKIPPED_TEMPLATE"
    assert enriched.llm_attempts == 0
