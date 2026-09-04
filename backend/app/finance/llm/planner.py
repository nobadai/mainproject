"""Finance Planner — **고를 뿐 실행하지 않는다.**

이 파일이 소유하는 것
    Planner/Finalizer 계약(`ToolAction` · 프로토콜 · 예외) · Planner 프롬프트 ·
    출력 사후 검증 · LangChain ChatModel 어댑터 · tool-calling Planner ·
    결정론(오프라인) Planner · Provider 구성과 가용성 대체

여기 **없는 것**
    Tool 실행 · capability 판단 · 예산 · 재무 계산 · 설명 문장
    → 실행 승인은 `application.harness`, 계산은 `capabilities`, 문장은 `messages` 다.

★ **LangChain 이 Tool 을 실행하지 않는다.** 모델의 tool call 은 *실행 요청*으로만
  쓰이고, 실제 실행은 Harness 승인을 지난 뒤 같은 어댑터를 통해 일어난다. 에이전트
  실행기(AgentExecutor 류)를 쓰면 그 승인 자리가 사라진다.

★ 종료도 Tool 이다. 남은 capability 가 있으면 종료 Tool 이 애초에 바인딩되지 않아서,
  모델이 "재무 검토 완료" 라고 답할 자리가 형식적으로 존재하지 않는다.

★ **Provider 대체는 LLM 실패가 아니다.** Gemini 가 못 받아 Gemma 가 답했어도 LLM 은
  답한 것이다. 대체 사실은 observation 으로 따로 남긴다 — 두 개념을 섞으면
  *"모델이 틀렸다"* 와 *"모델을 못 불렀다"* 를 구분할 수 없다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.contracts.core import Evidence
from app.finance.llm.client import (
    _DEFAULT_MODELS,
    _finance_model,
    _finance_provider_name,
    _gemini_availability_failure_reason,
    _gemini_tool_call,
    _ollama_availability_failure_reason,
    _ollama_tool_call,
    _ollama_tool_calling_model,
    finance_llm_enabled,
    finance_planner_model,
)
from app.finance.llm.finalizer import (
    DeterministicFinanceFinalizer,
    GeminiFinanceFinalizer,
    OllamaFinanceFinalizer,
)
from app.finance.schemas import FinanceMode
from app.master.envelope import AgentRequest

# ---------------------------------------------------------------------------
# Planner/Finalizer 계약과 출력 검증
# ---------------------------------------------------------------------------

#: capability 를 다 채웠을 때 Planner 가 부르는 종료 Tool 의 이름.
#:
#: ★ 종료도 **Tool 호출**이다. 자유 문장으로 "재무 검토 완료" 라고 답할 자리를 주지
#:   않기 위해서다 — 필수 capability 가 남아 있으면 Harness 가 이 Tool 을 아예
#:   바인딩하지 않는다. 이름을 여기서 소유하는 이유는 **Planner 계약**이기 때문이다.
FINALIZE_TOOL_NAME = "finalize_finance_review"


_PLANNER_SYSTEM_PROMPT = (
    "You plan Finance capability calls. Call exactly one of the tools you were given "
    "this step; they are the only ones that can legally run right now. "
    "Never calculate or invent financial numbers or policy values - use the "
    "observations only. Never copy business payload fields into tool arguments. "
    "For a tool with no declared parameters, send an empty arguments object. "
    "Call the finalize tool only when missing_capabilities is empty; while it is "
    "non-empty you must call a capability tool instead."
)


def _planner_prompt(
    *,
    request: AgentRequest,
    allowed_tools: frozenset[str],
    observations: tuple[dict[str, Any], ...],
    missing_capabilities: tuple[str, ...],
) -> dict[str, Any]:
    """Provider 와 무관하게 **같은 입력**을 만든다.

    직전 반려 사유는 루프가 ``observations`` 에 남긴 GUARD 항목에서 뽑는다 — Planner
    계약에 인자를 더하지 않고도 **왜 반려됐는지**를 모델에게 되돌려준다.
    """
    rejected = [
        {key: value for key, value in observation.items() if key != "branch_id"}
        for observation in observations
        if observation.get("type") == "GUARD"
    ]
    prompt: dict[str, Any] = {
        "mode": request.mode,
        "executable_tools": sorted(allowed_tools),
        "observations": observations,
        "missing_capabilities": missing_capabilities,
    }
    if rejected:
        prompt["previous_attempts_rejected"] = rejected
    return prompt


@dataclass(frozen=True)
class ToolAction:
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    finalize: bool = False


class FinancePlanner(Protocol):
    model: str
    attempts: int

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
        **kwargs: Any,
    ) -> ToolAction: ...


class FinanceFinalizer(Protocol):
    model: str
    attempts: int

    def finalize(
        self,
        *,
        mode: FinanceMode,
        business_status: str,
        evidences: tuple[Evidence, ...],
        has_verified_adjustment: bool = False,
    ) -> str: ...


class FinancePlannerFailure(RuntimeError):
    """되돌릴 수 없는 Planner 실패를 Controller 상태로 전달한다.

    Provider 장애·네트워크 오류·구조화 출력 파싱 불가처럼 **다시 물어도 같은 것**이
    여기로 온다. 모델이 계약을 어긴 것은 `FinancePlannerContractViolation` 이다.
    """


class FinancePlannerUnavailable(FinancePlannerFailure):
    """구성된 LLM Planner들이 모두 실행 불가해 결정론 선택으로 내릴 수 있는 실패."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.reason = reason


class FinancePlannerContractViolation(ValueError):
    """모델이 계약을 어긴 **회복 가능한** 잘못.

    ★ 이것을 `FinancePlannerFailure` 와 섞으면 재계획이 죽는다. 예전에는 검증 실패가
      `decide()` 안에서 예외로 올라와 Controller 가 통째로 ERROR 로 접었고, 그래서
      `_guard_replan` 은 있으나 마나였다 — `metadata.replans` 는 늘 0 이었다.
      허용되지 않은 Tool 선택 같은 잘못은 **왜 반려됐는지 알려주고 다시 묻는다.**
    """


def _validate_planner_action(
    action: ToolAction,
    allowed_tools: frozenset[str],
    missing_capabilities: tuple[str, ...],
) -> None:
    """Planner 출력 사후 검증 — 스키마를 무시한 모델을 여기서 잡는다.

    전부 `FinancePlannerContractViolation` 으로 올린다. Controller 가 이것만 bounded
    replan 으로 되묻고, 나머지 예외는 즉시 실패로 접는다.
    """
    if not isinstance(action.finalize, bool):
        raise FinancePlannerContractViolation("Finance Planner finalize must be boolean")
    if not isinstance(action.arguments, dict):
        raise FinancePlannerContractViolation("Finance Planner arguments must be an object")
    if missing_capabilities:
        if action.finalize or action.tool_name not in allowed_tools:
            raise FinancePlannerContractViolation(
                "Finance Planner must select one allowed tool while capabilities are missing"
            )
        return
    if not action.finalize or action.tool_name is not None:
        raise FinancePlannerContractViolation(
            "Finance Planner must finalize without a tool when capabilities are complete"
        )


# ---------------------------------------------------------------------------
# LangChain tool-calling 계층
# ---------------------------------------------------------------------------

class FinanceChatModel(BaseChatModel):
    """재무 Provider 한 곳을 감싼 LangChain ChatModel.

    ★ Provider 별로 클래스를 두 벌 만들지 않는다. 갈라 두면 *"Gemini 만 tool 을
      제한한다"* 같은 비대칭이 조용히 생긴다 — 차이는 전송 함수 하나뿐이다.
    """

    provider: str = "gemini"
    finance_model: str = ""

    @property
    def _llm_type(self) -> str:
        return f"finance-{self.provider}"

    @property
    def model(self) -> str:
        """이력에 남는 모델 이름."""
        return self.finance_model

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        """이번 호출에서 **부를 수 있는 Tool 만** 바인딩한다.

        Harness 가 정한 실행 가능 Tool 이 그대로 들어온다. 여기서 목록을 넓히지 않는다.
        """
        declarations = [convert_to_openai_tool(tool)["function"] for tool in tools]
        return self.bind(
            finance_tool_declarations=declarations, tool_choice=tool_choice, **kwargs
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        declarations = list(kwargs.get("finance_tool_declarations") or [])
        if not declarations:
            # Tool 없이 부르면 모델이 자유 문장으로 답할 수 있게 된다. 재무 Planner 는
            # 그럴 자리가 없으므로 여기서 막는다 — 조용히 통과시키지 않는다.
            raise ValueError("Finance chat model requires at least one bound tool")
        system_prompt, user_payload = _split_prompt(messages)
        transport = _gemini_tool_call if self.provider == "gemini" else _ollama_tool_call
        calls = transport(
            model=self.finance_model,
            system_prompt=system_prompt,
            user_payload=user_payload,
            tool_declarations=declarations,
        )
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": str(call.get("name") or ""),
                    "args": dict(call.get("args") or {}),
                    "id": f"finance-tool-call-{index}",
                    "type": "tool_call",
                }
                for index, call in enumerate(calls)
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _split_prompt(messages: list[BaseMessage]) -> tuple[str, dict[str, Any]]:
    """LangChain 메시지를 기존 재무 Provider 호출 형태로 되돌린다.

    ★ Planner 입력은 예전과 **같은 JSON** 이다. 프롬프트를 새로 쓰지 않는다 — 재무
      규칙이 프롬프트로 새어 들어갈 자리를 만들지 않기 위해서다.
    """
    system_prompt = "".join(
        str(message.content) for message in messages if isinstance(message, SystemMessage)
    )
    payload: dict[str, Any] = {}
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        try:
            parsed = json.loads(str(message.content))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            payload = parsed
    return system_prompt, payload


def finance_chat_model(provider: str, *, model: str | None = None) -> FinanceChatModel:
    return FinanceChatModel(
        provider=provider, finance_model=model or _finance_model(provider)
    )


class LangChainFinancePlanner:
    """Finance Planner 를 LangChain tool calling 으로 실행한다."""

    def __init__(self, chat_model: BaseChatModel, *, model: str | None = None) -> None:
        self.chat_model = chat_model
        self._model = model or getattr(chat_model, "model", "") or chat_model._llm_type
        self.attempts = 0

    @property
    def model(self) -> str:
        return self._model

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
        langchain_tools: tuple[BaseTool, ...] = (),
        **_kwargs: Any,
    ) -> ToolAction:
        self.attempts += 1
        if not langchain_tools:
            # Harness 가 Tool 을 하나도 노출하지 않았다면 고를 것이 없다. 자유 문장
            # 답을 받아 해석하는 대신 **계약 위반으로 되묻는다.**
            raise FinancePlannerContractViolation(
                "Finance Harness exposed no executable tool for this step"
            )
        bound = self.chat_model.bind_tools(list(langchain_tools), tool_choice="any")
        prompt = _planner_prompt(
            request=request,
            allowed_tools=allowed_tools,
            observations=observations,
            missing_capabilities=missing_capabilities,
        )
        message = bound.invoke(
            [
                SystemMessage(content=_PLANNER_SYSTEM_PROMPT),
                HumanMessage(content=json.dumps(prompt, default=str)),
            ]
        )
        action = _action_from_message(message)
        _validate_planner_action(action, allowed_tools, missing_capabilities)
        return action


def _action_from_message(message: AIMessage) -> ToolAction:
    """모델 응답에서 **정확히 하나의** Tool 호출을 읽는다.

    0 개는 자유 문장 답이고 2 개 이상은 이번 단계의 계약이 아니다. 둘 다 회복 가능한
    잘못이라 `FinancePlannerContractViolation` 으로 올려 bounded replan 에 태운다 —
    Provider 장애와 섞으면 되묻기가 죽는다.
    """
    calls = list(getattr(message, "tool_calls", ()) or ())
    if len(calls) != 1:
        raise FinancePlannerContractViolation(
            f"Finance Planner must request exactly one tool call, got {len(calls)}"
        )
    call = calls[0]
    name = str(call.get("name") or "")
    arguments = dict(call.get("args") or {})
    reason = str(arguments.pop("reason", "") or f"LangChain tool call: {name}")
    if name == FINALIZE_TOOL_NAME:
        return ToolAction(finalize=True, reason=reason)
    return ToolAction(tool_name=name, arguments=arguments, reason=reason)


# ---------------------------------------------------------------------------
# Provider 구성 · 결정론 Planner · 가용성 대체
# ---------------------------------------------------------------------------

class DeterministicFinancePlanner:
    """LLM 이 꺼졌을 때 쓰는 Planner. **선택만** 결정론으로 대신한다.

    ★ 새 재무 정책을 만들지 않는다. 고를 수 있는 Tool 집합과 남은 capability 는
      Harness 가 이미 정해서 넘긴다 — 여기서 하는 일은 그중 하나를 정해진 순서로
      집는 것뿐이다. 숫자·판정은 여전히 Tool 과 Rule 이 만든다.
    """

    model = "deterministic-finance-planner"

    def __init__(self) -> None:
        self.attempts = 0

    def decide(
        self,
        *,
        allowed_tools: frozenset[str],
        missing_capabilities: tuple[str, ...],
        **_kwargs: Any,
    ) -> ToolAction:
        # capability 소유표는 Harness 가 든다. 모듈 최상단에서 부르면 순환이 되므로
        # (Harness → planner → Harness) 실행 시점에 읽는다 — 고르는 순서를 바꾸지
        # 않기 위해서다. 순서가 바뀌면 결정론 실행의 Tool 순서가 달라진다.
        from app.finance.application.harness import CAPABILITY_OWNER

        self.attempts += 1
        if not missing_capabilities:
            return ToolAction(finalize=True, reason="capabilities complete")
        for capability in missing_capabilities:
            tool = CAPABILITY_OWNER[capability]
            if tool in allowed_tools:
                return ToolAction(tool_name=tool, reason=f"satisfies {capability}")
        raise FinancePlannerFailure(
            "no allowed Finance tool can satisfy the missing capabilities"
        )


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

    def decide(self, **kwargs: Any) -> ToolAction:
        # ★ 인자를 **그대로 흘린다.** 여기서 목록을 다시 적으면 Harness 가 넘긴
        #   실행 가능 Tool 같은 새 인자가 대체 경로에서만 조용히 사라진다.
        if self.state.active:
            return self._fallback_decide(**kwargs)
        try:
            return self.primary.decide(**kwargs)
        except Exception as error:
            reason = _gemini_availability_failure_reason(error)
            if reason is None:
                raise
            self.state.activate(reason)
            return self._fallback_decide(**kwargs)

    def _fallback_decide(self, **kwargs: Any) -> ToolAction:
        try:
            return self.fallback.decide(**kwargs)
        except Exception as error:
            reason = _ollama_availability_failure_reason(error)
            if reason is None:
                raise
            raise FinancePlannerUnavailable(
                f"Finance LLM providers unavailable: ollama {reason}",
                provider="ollama",
                reason=reason,
            ) from error


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
        has_verified_adjustment: bool = False,
    ) -> str:
        # ★ Provider 가 갈려도 **같은 사실**을 넘긴다. 한쪽만 조정 여부를 못 받으면
        #   같은 결과가 Provider 에 따라 다른 문장을 고르게 된다.
        kwargs = {
            "mode": mode,
            "business_status": business_status,
            "evidences": evidences,
            "has_verified_adjustment": has_verified_adjustment,
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


def _langchain_planner(provider: str, *, model: str | None = None) -> LangChainFinancePlanner:
    """재무 Planner 는 **LangChain tool calling** 으로 돈다.

    ★ Finalizer 는 바꾸지 않는다. 설명은 검증된 Evidence 에서 고정 문장 키를 고르는
      일이라 Tool 계층이 아니고, 그 제약을 tool calling 으로 옮길 이유가 없다.
    """
    return LangChainFinancePlanner(
        finance_chat_model(provider, model=model or finance_planner_model(provider))
    )


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
        return _langchain_planner("ollama"), OllamaFinanceFinalizer(), state
    return (
        _AvailabilityFallbackFinancePlanner(
            _langchain_planner("gemini"),
            # ★ 대체 Planner 의 모델은 **재무 설정을 물려받지 않는다.** 설정값은
            #   Gemini 모델 이름이고, Ollama 로 옮겨 갈 때 그대로 쓰면 없는 모델을
            #   부른다. 여기서 고르는 것은 tool 을 부를 수 있는 기본값이다.
            _langchain_planner("ollama", model=_ollama_tool_calling_model()),
            state,
        ),
        _AvailabilityFallbackFinanceFinalizer(
            GeminiFinanceFinalizer(),
            OllamaFinanceFinalizer(model=_DEFAULT_MODELS["ollama"]),
            state,
        ),
        state,
    )
