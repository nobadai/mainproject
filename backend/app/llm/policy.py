"""Policy deciding whether qualitative synthesis benefits from an LLM call."""

from app.llm.schemas import SanitizedLLMContext

_QUALITATIVE_SIGNALS = {"FRESHNESS_QUALITY_RISK"}
_COMPOSITE_SIGNALS = {
    "CASH_BUFFER_LOW",
    "COST_MISMATCH",
    "FRESHNESS_QUALITY_RISK",
    "PAYABLES_DUE_SOON",
    "RECEIVABLES_CONCENTRATION",
}


def needs_llm(
    context: SanitizedLLMContext,
    *,
    runtime_ready: bool,
    has_blocking_constraints: bool,
) -> bool:
    """Use LLM only for ready, non-blocking qualitative or composite signals."""
    if not runtime_ready or has_blocking_constraints:
        return False
    signals = set(context.signals)
    if signals & _QUALITATIVE_SIGNALS:
        return True
    return len(signals & _COMPOSITE_SIGNALS) >= 2
