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
                "status": "NEEDS_PRIORITY_SHIPMENT",
            }
        ],
        hard_constraints=[],
        soft_warnings=[],
    )

    context = build_logistics_context(response)
    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)

    assert context.signals == ["FRESHNESS_QUALITY_RISK"]
    assert context.allowed_adjustments == ["우선 출고 대상으로 검토합니다."]
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
                "status": "NEEDS_PRIORITY_SHIPMENT",
            }
        ],
        hard_constraints=[],
        soft_warnings=[],
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
        verdict="REVIEW_REQUIRED",
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
