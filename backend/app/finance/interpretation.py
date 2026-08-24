"""Finance deterministic Reply projection into the optional LLM layer."""

from app.finance.llm.runtime import InterpretationService, get_interpretation_service
from app.finance.llm.schemas import SanitizedLLMContext
from app.finance.schemas import FinanceProcurementResponse, FinanceSalesResponse

_FINANCE_FACTS = {
    "AS_OF_MISMATCH": "요청 기준시점과 재무 기준시점이 일치하지 않습니다.",
    "CASH_BUFFER_LOW": "운영 현금 버퍼가 낮은 상태입니다.",
    "CASH_PRIORITY_POLICY_UNRESOLVED": "현금 회수 우선도 정책이 확정되지 않았습니다.",
    "COST_MISMATCH": "보고 금액과 재계산 금액이 일치하지 않습니다.",
    "FINANCIAL_LIMIT_EXCEEDED": "재무 한도를 초과하는 지급 의무가 확인되었습니다.",
    "NO_FINANCIAL_CAPACITY": "현재 추가 재무 여력이 없습니다.",
    "PAYABLES_DUE_SOON": "지급 예정 채무가 임박해 있습니다.",
    "RECEIVABLES_CONCENTRATION": "매출채권이 일부 거래처에 집중되어 있습니다.",
    "REQUIRED_FINANCE_STATE_MISSING": "재무 판단에 필요한 상태 정보가 부족합니다.",
}


def build_finance_context(
    response: FinanceProcurementResponse | FinanceSalesResponse,
) -> SanitizedLLMContext:
    signals = _unique([*response.hard_constraints, *response.soft_warnings])
    return SanitizedLLMContext(
        domain="FINANCE",
        signals=signals,
        facts=[
            _FINANCE_FACTS.get(signal, "정의되지 않은 재무 신호가 확인되었습니다.")
            for signal in signals
        ],
        allowed_adjustments=[],
    )


def enrich_finance_response[FinanceResponse: (FinanceProcurementResponse, FinanceSalesResponse)](
    response: FinanceResponse,
    interpretation_service: InterpretationService | None = None,
) -> FinanceResponse:
    service = interpretation_service or get_interpretation_service()
    context = build_finance_context(response)
    result = service.interpret(
        context,
        runtime_ready=response.runtime_status == "READY",
        has_blocking_constraints=bool(response.hard_constraints),
    )
    update = result.model_dump(exclude={"interpretation"})
    update["interpretation"] = result.interpretation
    return response.model_copy(update=update)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
