"""Provider 구성과 가용성 대체.

★ **Provider 대체는 LLM 실패가 아니다.** Gemini 가 못 받아 Gemma 가 답했어도 LLM 은
  답한 것이다 (`llm_status=SUCCESS`). 대체 사실은 observation 으로 따로 남긴다 — 두
  개념을 섞으면 *"모델이 틀렸다"* 와 *"모델을 못 불렀다"* 를 구분할 수 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.finance.llm.client import _gemini_availability_failure_reason
from app.finance.llm.config import _DEFAULT_MODELS, _finance_provider_name, finance_llm_enabled
from app.finance.llm.contracts import FinanceFinalizer, FinanceMode, FinancePlanner, ToolAction
from app.finance.llm.finalizer import (
    DeterministicFinanceFinalizer,
    GeminiFinanceFinalizer,
    OllamaFinanceFinalizer,
)
from app.finance.llm.planner import (
    DeterministicFinancePlanner,
    GeminiFinancePlanner,
    OllamaFinancePlanner,
)
from app.master.envelope import AgentRequest
from app.orchestrator.contracts_core import Evidence


@dataclass
class _ProviderFallbackState:
    primary_provider: str
    effective_provider: str
    active: bool = False
    reason: str | None = None

    def activate(self, reason: str) -> None:
        self.active = True
        self.effective_provider = "ollama"
        self.reason = reason


class _AvailabilityFallbackFinancePlanner:
    def __init__(
        self,
        primary: FinancePlanner,
        fallback: FinancePlanner,
        state: _ProviderFallbackState,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.state = state

    @property
    def model(self) -> str:
        return self.fallback.model if self.state.active else self.primary.model

    @property
    def attempts(self) -> int:
        return self.primary.attempts + self.fallback.attempts

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        kwargs = {
            "request": request,
            "allowed_tools": allowed_tools,
            "observations": observations,
            "missing_capabilities": missing_capabilities,
        }
        if self.state.active:
            return self.fallback.decide(**kwargs)
        try:
            return self.primary.decide(**kwargs)
        except Exception as error:
            reason = _gemini_availability_failure_reason(error)
            if reason is None:
                raise
            self.state.activate(reason)
            return self.fallback.decide(**kwargs)


class _AvailabilityFallbackFinanceFinalizer:
    def __init__(
        self,
        primary: FinanceFinalizer,
        fallback: FinanceFinalizer,
        state: _ProviderFallbackState,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.state = state

    @property
    def model(self) -> str:
        return self.fallback.model if self.state.active else self.primary.model

    @property
    def attempts(self) -> int:
        return self.primary.attempts + self.fallback.attempts

    def finalize(
        self,
        *,
        mode: FinanceMode,
        business_status: str,
        evidences: tuple[Evidence, ...],
    ) -> str:
        kwargs = {
            "mode": mode,
            "business_status": business_status,
            "evidences": evidences,
        }
        if self.state.active:
            return self.fallback.finalize(**kwargs)
        try:
            return self.primary.finalize(**kwargs)
        except Exception as error:
            reason = _gemini_availability_failure_reason(error)
            if reason is None:
                raise
            self.state.activate(reason)
            return self.fallback.finalize(**kwargs)


def _configured_finance_llms(
) -> tuple[FinancePlanner, FinanceFinalizer, _ProviderFallbackState | None]:
    """설정이 정하는 Planner/Finalizer 한 쌍.

    LLM 이 꺼져 있으면 Provider 를 아예 만들지 않는다 — 끈 상태에서 API 키나 로컬
    서버를 확인하러 나가면 "껐는데 왜 나가나" 가 된다.
    """
    if not finance_llm_enabled():
        return DeterministicFinancePlanner(), DeterministicFinanceFinalizer(), None
    provider = _finance_provider_name()
    state = _ProviderFallbackState(
        primary_provider=provider,
        effective_provider=provider,
    )
    if provider == "ollama":
        return OllamaFinancePlanner(), OllamaFinanceFinalizer(), state
    return (
        _AvailabilityFallbackFinancePlanner(
            GeminiFinancePlanner(),
            OllamaFinancePlanner(model=_DEFAULT_MODELS["ollama"]),
            state,
        ),
        _AvailabilityFallbackFinanceFinalizer(
            GeminiFinanceFinalizer(),
            OllamaFinanceFinalizer(model=_DEFAULT_MODELS["ollama"]),
            state,
        ),
        state,
    )
