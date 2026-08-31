"""Tool을 사용하는 Finance Agent 진입 경로.

이 모듈은 의도적으로 ``FinanceSnapshot``을 import하지 않는다. 레거시
매입/영업 서비스는 호환성 용도로만 남기고, 이 경로는 M-1 실행 컨텍스트를
경계로 사용한다.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from dotenv import load_dotenv

from app.finance.repository import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.rules import classify_base_stress
from app.finance.run_repository import save_finance_execution
from app.finance.schemas import CashEvent, FinancePolicy
from app.finance.tools import (
    build_payroll_schedule,
    calculate_finance_cap,
    derive_cash_priority,
    derive_critical_payment_dates,
    project_cashflow,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment

FinanceMode = Literal["PRE_PURCHASE", "SCENARIO_VALIDATION"]
DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_MAX_REPLANS = 2

_DEFAULT_MODELS = {
    "ollama": "gemma3:4b",
    "gemini": "gemini-3.5-flash-lite",
}
_ENV_FILES = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
)
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
def _planner_response_schema(
    allowed_tools: frozenset[str], *, planning_required: bool
) -> dict[str, Any]:
    """이번 호출에서 Planner 가 낼 수 있는 형태를 그대로 스키마로 만든다.

    ★ **Provider 마다 Tool 허용 수준이 달라지면 안 된다.** Ollama 는 ``format`` 으로
      enum 을 강제하는데 Gemini 쪽만 자유 문자열이면, 같은 재무 판단이 Provider 에
      따라 다른 Tool 을 부를 수 있게 열린다. 두 Planner 가 같은 스키마를 쓴다.

    ★ 스키마는 **1차 방어**일 뿐이다. `_validate_planner_action` 사후 검증을 대체하지
      않는다 — 구조화 출력을 무시하는 모델이 있고, 그때 걸러야 할 곳은 우리 쪽이다.
    """
    return {
        "type": "object",
        "properties": {
            "tool_name": (
                {"type": "string", "enum": sorted(allowed_tools)}
                if planning_required
                else {"type": "null"}
            ),
            "arguments": {
                "type": "object",
                "properties": {
                    "axis": {"type": "string"},
                    "candidate_amount_krw": {"type": "number"},
                },
            },
            "reason": {"type": "string"},
            "finalize": {"type": "boolean", "enum": [not planning_required]},
        },
        "required": ["tool_name", "arguments", "reason", "finalize"],
    }


_PLANNER_SYSTEM_PROMPT = (
    "You plan Finance capability calls. Select only an allowed tool. "
    "Never calculate or invent financial numbers or policy values. "
    "Use observations only. When missing_capabilities is non-empty, you MUST set "
    "finalize=false and select exactly one allowed tool that can satisfy a missing "
    "capability. You may set finalize=true only when missing_capabilities is empty; "
    "then tool_name must be null. For validate_amount_adjustment, copy the observed "
    "deterministic finance_cap_amount_krw exactly and set axis to amount."
)

_TOOL_ARGUMENT_CONTRACTS = {
    "assess_finance_position": {},
    "project_cashflow": {},
    "calculate_purchase_finance_cap": {},
    "analyze_payment_pressure": {},
    "evaluate_purchase_scenario": {},
    "validate_amount_adjustment": {
        "axis": "amount",
        "candidate_amount_krw": (
            "copy the exact finance_cap_amount_krw from a prior observation; "
            "never create a number"
        ),
    },
}


def _planner_prompt(
    *,
    request: AgentRequest,
    allowed_tools: frozenset[str],
    observations: tuple[dict[str, Any], ...],
    missing_capabilities: tuple[str, ...],
) -> dict[str, Any]:
    """두 Planner 가 같은 입력을 본다.

    직전 재계획 사유는 Controller 가 ``observations`` 에 남긴 GUARD 항목에서 뽑는다 —
    Planner 계약에 인자를 더하지 않고도 **왜 반려됐는지**를 모델에게 되돌려준다.
    """
    rejected = [
        {key: value for key, value in observation.items() if key != "branch_id"}
        for observation in observations
        if observation.get("type") == "GUARD"
    ]
    prompt: dict[str, Any] = {
        "mode": request.mode,
        "business_payload": dict(request.payload),
        "allowed_tools": sorted(allowed_tools),
        "observations": observations,
        "missing_capabilities": missing_capabilities,
        "tool_argument_contracts": _TOOL_ARGUMENT_CONTRACTS,
    }
    if rejected:
        prompt["previous_attempts_rejected"] = rejected
    return prompt


def _load_finance_environment() -> None:
    for env_file in _ENV_FILES:
        load_dotenv(env_file, override=False)


def _read_bool(key: str) -> bool | None:
    """설정된 경우에만 bool 을 돌려준다. 미설정과 false 를 섞지 않기 위해서다."""
    value = os.getenv(key)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def finance_llm_enabled() -> bool:
    """Finance Agent LLM 활성화 여부.

    ``FINANCE_LLM_ENABLED`` → ``LLM_ENABLED`` → 기본 활성. 재무만 끄고 싶은 경우와
    전역으로 끈 경우를 구분한다 (재무 전용 키가 전역 키를 이긴다).
    """
    _load_finance_environment()
    finance = _read_bool("FINANCE_LLM_ENABLED")
    if finance is not None:
        return finance
    shared = _read_bool("LLM_ENABLED")
    if shared is not None:
        return shared
    return True


def _finance_provider_name() -> str:
    """★ 전역 ``LLM_PROVIDER`` 를 상속하지 않는다.

    전역은 레거시 Ollama 해석 계층이 쓰는 값이다. 그것을 상속하면 전역을 ollama 로
    둔 배포에서 재무 Agent 가 조용히 Gemini 를 떠난다 — 재무 Provider 정책은 재무
    키로만 정해진다.
    """
    _load_finance_environment()
    provider = (
        os.getenv("FINANCE_LLM_PROVIDER")
        or "gemini"
    ).strip().lower()
    if provider not in _DEFAULT_MODELS:
        raise RuntimeError("Configured Finance LLM provider is not supported")
    return provider


def _finance_model(provider: str) -> str:
    _load_finance_environment()
    explicit = os.getenv("FINANCE_LLM_MODEL")
    if explicit:
        return explicit
    global_provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    global_model = os.getenv("LLM_MODEL")
    if provider == global_provider and global_model:
        return global_model
    return _DEFAULT_MODELS[provider]


def _gemini_response_text(document: dict[str, Any]) -> str:
    candidates = document.get("candidates") or []
    parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
    for part in parts:
        if part.get("thought"):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    raise TypeError("Finance Gemini response did not contain text content")


def _gemini_generate(
    *, model: str, system_prompt: str, user_payload: dict[str, Any], response_schema: dict[str, Any]
) -> str:
    _load_finance_environment()
    api_key = os.getenv("FINANCE_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Finance Gemini API key is not set")
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(user_payload, default=str)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }
    request = urllib.request.Request(
        f"{_GEMINI_BASE_URL}/models/{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        ) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError("Finance Gemini request failed") from error
    return _gemini_response_text(document)

PRE_PURCHASE_TOOLS = frozenset(
    {
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    }
)
SCENARIO_VALIDATION_TOOLS = frozenset({"evaluate_purchase_scenario", "validate_amount_adjustment"})

#: Critic 이 재무 cap 검사를 부르는 이름(`critic_bridge._FINANCE_CAP_CHECK`).
#: `DeptMeta.inputs_used` 는 이 check_id 로 색인된다.
FINANCE_CAP_CHECK_ID = "finance_cap_amount_krw"

#: Finance Cap 을 만들 때 **실제로 읽는** 재무 입력.
#:
#: ★ 이것은 선언이 아니라 **관측이어야 한다.** 그래서 실행 중 실제로 부른 Tool 을 보고
#:   골라 담는다 (`_finance_dept_meta`). 목록을 손으로 적어 두고 실행과 어긋나면,
#:   Critic 의 등급 누출 검사는 **우리가 적은 거짓말을 검사하게 된다.**
#:
#: ★ 매입 소유 입력(`qty_kg` · `grade_unit_price` · `sourcing_plan` …)은 여기 없다.
#:   PRE_PURCHASE 는 payload 가 비어 있고 Tool 이 그 값을 읽지 않기 때문이다 — 읽게
#:   되는 날이 오면 **숨기지 말고 여기에 나타나야 한다.** 그것이 이 검사의 존재 이유다.
_CAP_TOOL_INPUTS: dict[str, tuple[str, ...]] = {
    "assess_finance_position": (
        "finance_state.current_cash_krw",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
    ),
    "project_cashflow": (
        "finance_state.current_cash_krw",
        "finance_policy.cashflow_projection_days",
        "finance_cash_events.obligations",
        "finance_cash_events.receivables",
        "finance_policy.monthly_labor_cost_krw",
        "finance_policy.payroll_date",
    ),
    "calculate_purchase_finance_cap": (
        "base_projection.projected_cash_by_date",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
    ),
    "analyze_payment_pressure": (
        "base_projection.projected_cash_min",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.cash_priority_high_ratio",
        "finance_policy.cash_priority_medium_ratio",
    ),
}

_PAYROLL_SOURCE_KEYS: tuple[str, ...] = ("monthly_labor_cost_krw", "payroll_date")
"""이 둘만 **출처가 없으면 계산 자체가 안 된다** (재무 #63 · M-23).

출처 없는 급여 이벤트를 만들지 않기로 재무가 정했으므로 급여 유출이 통째로 빠지고,
그 상태의 `finance_cap` 은 틀린 게 아니라 **낙관적으로 틀린다** — 그 상한으로 매입이
실행된다. 나머지 정책값은 값 자체를 쓸 수 있어 실행을 세우지 않는다
(`_optional_source_ref`)."""


@dataclass(frozen=True)
class ToolAction:
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    finalize: bool = False


@dataclass(frozen=True)
class ScenarioPayment:
    seq: int
    purchase_date: date
    payment_date: date
    qty_kg: Decimal | None
    amount_krw: Decimal
    amount_max_krw: Decimal
    basis: str


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
    ) -> str: ...


class FinancePlannerFailure(RuntimeError):
    """되돌릴 수 없는 Planner 실패를 Controller 상태로 전달한다.

    Provider 장애·네트워크 오류·구조화 출력 파싱 불가처럼 **다시 물어도 같은 것**이
    여기로 온다. 모델이 계약을 어긴 것은 `FinancePlannerContractViolation` 이다.
    """


class FinancePlannerContractViolation(ValueError):
    """모델이 계약을 어긴 **회복 가능한** 잘못.

    ★ 이것을 `FinancePlannerFailure` 와 섞으면 재계획이 죽는다. 예전에는 검증 실패가
      `decide()` 안에서 예외로 올라와 Controller 가 통째로 ERROR 로 접었고, 그래서
      `_guard_replan` 은 있으나 마나였다 — `metadata.replans` 는 늘 0 이었다.
      허용되지 않은 Tool 선택 같은 잘못은 **왜 반려됐는지 알려주고 다시 묻는다.**
    """


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


def _gemini_availability_failure_reason(error: Exception) -> str | None:
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 429:
            return "HTTP_429"
        if 500 <= error.code < 600:
            return "HTTP_5XX"
        return None
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if isinstance(error, urllib.error.URLError):
        return "NETWORK_ERROR"
    if (
        isinstance(error, RuntimeError)
        and str(error) == "Finance Gemini API key is not set"
    ):
        return "API_KEY_MISSING"
    if isinstance(error.__cause__, TimeoutError):
        return "TIMEOUT"
    if isinstance(error.__cause__, urllib.error.URLError):
        return "NETWORK_ERROR"
    return None


def _is_gemini_availability_failure(error: Exception) -> bool:
    return _gemini_availability_failure_reason(error) is not None


class OllamaFinancePlanner:
    """허용된 Tool 호출 또는 finalize로 출력이 제한된 LLM Planner."""

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or _finance_model("ollama")
        self.base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        self.attempts = 0

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        self.attempts += 1
        schema = _planner_response_schema(
            allowed_tools, planning_required=bool(missing_capabilities)
        )
        schema["additionalProperties"] = False
        prompt = _planner_prompt(
            request=request,
            allowed_tools=allowed_tools,
            observations=observations,
            missing_capabilities=missing_capabilities,
        )
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": schema,
            "messages": [
                {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt, default=str)},
            ],
            "options": {"temperature": 0},
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw = json.loads(response.read().decode())
        content = json.loads(raw["message"]["content"])
        action = ToolAction(**content)
        _validate_planner_action(action, allowed_tools, missing_capabilities)
        return action


class GeminiFinancePlanner:
    """Finance Tool 선택만 수행하는 Gemini structured-output Planner."""

    def __init__(self) -> None:
        self.model = _finance_model("gemini")
        self.attempts = 0

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        self.attempts += 1
        content = json.loads(
            _gemini_generate(
                model=self.model,
                system_prompt=_PLANNER_SYSTEM_PROMPT,
                user_payload=_planner_prompt(
                    request=request,
                    allowed_tools=allowed_tools,
                    observations=observations,
                    missing_capabilities=missing_capabilities,
                ),
                # ★ Ollama 와 **같은** 스키마다 — 이번 호출의 allowed_tools 가 enum 으로
                #   들어간다. Provider 를 바꿔도 고를 수 있는 Tool 집합이 같아야 한다.
                response_schema=_planner_response_schema(
                    allowed_tools, planning_required=bool(missing_capabilities)
                ),
            )
        )
        action = ToolAction(**content)
        _validate_planner_action(action, allowed_tools, missing_capabilities)
        return action


class DeterministicFinancePlanner:
    """LLM 이 꺼졌을 때 쓰는 Planner. **선택만** 결정론으로 대신한다.

    ★ 새 재무 정책을 만들지 않는다. 고를 수 있는 Tool 집합(`allowed_tools`)과 남은
      capability 는 Controller 가 이미 정해서 넘긴다 — 여기서 하는 일은 그중 하나를
      **정해진 순서로** 집는 것뿐이다. 숫자·판정은 여전히 Tool 과 Rule 이 만든다.
    """

    model = "deterministic-finance-planner"

    def __init__(self) -> None:
        self.attempts = 0

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        del request, observations
        self.attempts += 1
        if not missing_capabilities:
            return ToolAction(finalize=True, reason="capabilities complete")
        for capability in missing_capabilities:
            for tool in sorted(_CAPABILITY_TOOLS[capability]):
                if tool in allowed_tools:
                    return ToolAction(tool_name=tool, reason=f"satisfies {capability}")
        raise FinancePlannerFailure(
            "no allowed Finance tool can satisfy the missing capabilities"
        )


_FINAL_EXPLANATIONS = {
    "PRE_BOUNDARY": "Verified Finance Evidence supports the reported purchasing boundary.",
    "SCENARIO_REJECT": (
        "Verified Finance Evidence rejects at least one original scenario. "
        "Any published amount alternative was independently validated."
    ),
    "SCENARIO_ACCEPT": "Verified Finance Evidence supports the reported scenario verdicts.",
}


class OllamaFinanceFinalizer:
    """조사 Planner와 분리된 Evidence 전용 LLM finalization."""

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or _finance_model("ollama")
        self.base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        self.attempts = 0

    def finalize(
        self,
        *,
        mode: FinanceMode,
        business_status: str,
        evidences: tuple[Evidence, ...],
    ) -> str:
        self.attempts += 1
        allowed = (
            ["PRE_BOUNDARY"]
            if mode == "PRE_PURCHASE"
            else ["SCENARIO_REJECT"]
            if business_status == "reject"
            else ["SCENARIO_ACCEPT"]
        )
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": {
                "type": "object",
                "properties": {"explanation_key": {"type": "string", "enum": allowed}},
                "required": ["explanation_key"],
                "additionalProperties": False,
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Finalize the Finance reply from verified Evidence only. Select the "
                        "allowed explanation key. Do not calculate or add numbers or claims."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "mode": mode,
                            "business_status": business_status,
                            "verified_claims": [item.claim for item in evidences],
                            "allowed_explanation_keys": allowed,
                        }
                    ),
                },
            ],
            "options": {"temperature": 0},
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw = json.loads(response.read().decode())
        selected = json.loads(raw["message"]["content"])["explanation_key"]
        if selected not in allowed:
            raise ValueError("Finance finalization selected an unsupported explanation")
        return _FINAL_EXPLANATIONS[selected]


class GeminiFinanceFinalizer:
    """검증된 Evidence에서 설명 키만 고르는 Gemini Finalizer."""

    def __init__(self) -> None:
        self.model = _finance_model("gemini")
        self.attempts = 0

    def finalize(
        self,
        *,
        mode: FinanceMode,
        business_status: str,
        evidences: tuple[Evidence, ...],
    ) -> str:
        self.attempts += 1
        allowed = (
            ["PRE_BOUNDARY"]
            if mode == "PRE_PURCHASE"
            else ["SCENARIO_REJECT"]
            if business_status == "reject"
            else ["SCENARIO_ACCEPT"]
        )
        selected = json.loads(
            _gemini_generate(
                model=self.model,
                system_prompt=(
                    "Finalize the Finance reply from verified Evidence only. Select the "
                    "allowed explanation key. Do not calculate or add numbers or claims."
                ),
                user_payload={
                    "mode": mode,
                    "business_status": business_status,
                    "verified_claims": [item.claim for item in evidences],
                    "allowed_explanation_keys": allowed,
                },
                response_schema={
                    "type": "object",
                    "properties": {
                        "explanation_key": {"type": "string", "enum": allowed}
                    },
                    "required": ["explanation_key"],
                },
            )
        )["explanation_key"]
        if selected not in allowed:
            raise ValueError("Finance finalization selected an unsupported explanation")
        return _FINAL_EXPLANATIONS[selected]


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


class DeterministicFinanceFinalizer:
    """동일한 검증 완료 설명 계약을 구현하는 테스트/오프라인 finalizer."""

    model = "deterministic-finance-finalizer"

    def __init__(self) -> None:
        self.attempts = 0

    def finalize(
        self,
        *,
        mode: FinanceMode,
        business_status: str,
        evidences: tuple[Evidence, ...],
    ) -> str:
        self.attempts += 1
        del evidences
        if mode == "PRE_PURCHASE":
            return _FINAL_EXPLANATIONS["PRE_BOUNDARY"]
        return _FINAL_EXPLANATIONS[
            "SCENARIO_REJECT" if business_status == "reject" else "SCENARIO_ACCEPT"
        ]


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


def _source_ref(policy: FinancePolicy, key: str) -> str:
    """**계산 자체가 성립하지 않는** 정책 출처. 없으면 멈춘다.

    급여 두 키 전용이다 (`_PAYROLL_SOURCE_KEYS`). 출처 없는 급여 이벤트를 만들지
    않기로 재무가 정했으므로(재무 #63 · M-23) 급여 유출이 통째로 빠지고, 그 상태의
    `finance_cap` 은 틀린 게 아니라 **낙관적으로 틀린다** — 그 상한으로 매입이 실행된다.

    ★ `KeyError` 로 두지 않는 이유: Controller 의 일반 예외 경로로 빠져 `ERROR` 가
      된다. **출처가 없는 것은 프로그램 오류가 아니라 그날의 사실**이므로
      `RUNTIME_NOT_READY` + `missing_data` 다 — 둘은 재시도 가치가 다르다 (M-1 §5.1).
    """
    ref = policy.source_refs.get(key)
    if not ref:
        raise FinanceDataNotReady(f"{key}@policy_source_ref")
    return ref


def _optional_source_ref(
    policy: FinancePolicy, key: str, state: FinanceAgentState
) -> str | None:
    """급여 외 정책값의 출처. **없어도 실행은 계속한다.**

    ★ 급여만 특별하다 (`_PAYROLL_SOURCE_KEYS`). 나머지는 값 자체를 쓸 수 있으므로
      계산은 그대로 돌고, 실행을 통째로 세우지 않는다 — 기존 재무 정책이다.

    ★ 다만 **지어내지 않는다.** 없는 출처를 `finance-policy:{version}:{key}` 같은
      문자열이나 스냅샷 id 로 채우면, 값은 멀쩡히 나오고 에러도 안 나지만 그 ref 는
      따라갔을 때 **아무 데도 닿지 않는다.** 근거가 있는 척하는 판정만 남는다.

    ★ 그래서 `None` 을 돌려주고, 부르는 쪽이 **그 claim 의 payload 필드와 Evidence 를
      함께 뺀다.** 숫자만 남기고 근거를 빼면 봉투 검증이 `E-EVIDENCE-MISSING` 을
      낸다 — 낼 수 없는 근거를 요구받는 것이 아니라, 낼 수 없는 값을 안 내는 것이다.
      빠진 사실은 `missing_data` 의 `<key>@policy_source_ref` 로 밝힌다.
    """
    ref = policy.source_refs.get(key)
    if not ref:
        state.note_missing_source(key)
        return None
    return ref


class FinanceToolRegistry:
    def __init__(self, data_port: FinanceAsOfDataPort):
        self.data_port = data_port

    def names_for(self, mode: FinanceMode) -> frozenset[str]:
        return PRE_PURCHASE_TOOLS if mode == "PRE_PURCHASE" else SCENARIO_VALIDATION_TOOLS

    def execute(
        self, name: str, arguments: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        if name not in self.names_for(state.request.mode):
            raise ValueError(f"Tool {name} is not allowed for {state.request.mode}")
        return getattr(self, name)(arguments, state)

    def _context(
        self, state: FinanceAgentState
    ) -> tuple[dict[str, Any], FinancePolicy, list[CashEvent]]:
        if state.context_cache is not None:
            return state.context_cache
        ctx = state.request.context
        position = self.data_port.load_finance_position(ctx.as_of)
        policy = self.data_port.load_policy(ctx.as_of, ctx.policy_version)
        horizon = ctx.as_of + timedelta(days=policy.cashflow_projection_days)
        payroll_amount = self.data_port.load_payroll(ctx.as_of, horizon)
        if payroll_amount is None:
            raise FinanceDataNotReady("payroll_schedule")
        policy = policy.model_copy(update={"monthly_labor_cost_krw": payroll_amount})
        # 급여 출처는 fail-closed 다. `build_payroll_schedule` 도 막지만 그쪽은
        # `ValueError` 라 일반 `ERROR` 로 분류된다 — **입력이 없어서 못 내는 답**은
        # `RUNTIME_NOT_READY` 여야 재시도 가치가 제대로 남는다 (M-1 §5.1).
        for key in _PAYROLL_SOURCE_KEYS:
            _source_ref(policy, key)
        events = [
            *self.data_port.load_obligations(ctx.as_of, horizon),
            *self.data_port.load_receivables(ctx.as_of, horizon),
            *build_payroll_schedule(as_of=ctx.as_of, horizon_end=horizon, policy=policy),
        ]
        current_debt = Decimal(position["current_debt_krw"])
        if current_debt > 0:
            events.extend(self.data_port.load_debt_schedule(ctx.as_of, horizon))
        state.context_cache = (position, policy, events)
        return state.context_cache

    def assess_finance_position(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        position, policy, _ = self._context(state)
        if state.request.mode == "PRE_PURCHASE" and policy.purchase_payment_days is None:
            raise FinanceDataNotReady("purchase_payment_days")
        # ★ 값과 근거는 **한 쌍으로** 실린다. 정책 출처가 없으면 그 claim 은 payload
        #   에서도 빠진다 — 숫자만 남기면 봉투가 `E-EVIDENCE-MISSING` 을 내고, 근거를
        #   지어내면 따라갈 수 없는 ref 가 남는다. 빠진 사실은 missing_data 로 밝힌다.
        result: dict[str, Any] = {
            "available_cash": str(position["current_cash_krw"]),
            "payroll_payment_day": policy.payroll_date,
            # Finance Policy 버전은 Master 실행 컨텍스트와 독립적이다. 재현을 위해
            # Finance 데이터 경계에서 실제로 읽은 버전을 반환한다.
            "policy_version_used": policy.policy_version,
        }
        evidence: list[Evidence] = [
            _evidence(
                "available_cash",
                position["current_cash_krw"],
                "krw",
                str(position["finance_state_id"]),
                source="finance",
            ),
            Evidence(
                claim="payroll_payment_day",
                source="finance",
                ref_ids=(_source_ref(policy, "payroll_date"),),
                value=policy.payroll_date,
                unit="day_of_month",
                evidence_grade="SIM_FIXED",
                evidence_detail="Finance Policy DB day-of-month value.",
            ),
        ]

        minimum_cash_ref = _optional_source_ref(policy, "minimum_cash_balance_krw", state)
        if minimum_cash_ref is not None:
            result["minimum_cash_balance_krw"] = str(policy.minimum_cash_balance_krw)
            evidence.append(
                _evidence(
                    "minimum_cash_balance_krw",
                    policy.minimum_cash_balance_krw,
                    "krw",
                    minimum_cash_ref,
                    source="persona",
                )
            )

        payment_days_ref = _optional_source_ref(policy, "purchase_payment_days", state)
        if payment_days_ref is not None:
            result["purchase_payment_days"] = policy.purchase_payment_days
            evidence.append(
                _evidence(
                    "purchase_payment_days",
                    policy.purchase_payment_days,
                    "day",
                    payment_days_ref,
                    source="persona",
                )
            )
            # `policy_version_used` 는 봉투 어휘라 근거가 필수는 아니다. 다만 달 수
            # 있을 때는 단다 — 어느 정책 행을 읽었는지가 재현의 핵심이다.
            evidence.append(
                Evidence(
                    claim="policy_version_used",
                    source="persona",
                    ref_ids=(payment_days_ref,),
                    value=policy.policy_version,
                    unit="version",
                    evidence_grade="SIM_FIXED",
                    evidence_detail="Version of the Finance policy used for this execution.",
                )
            )

        if policy.margin_defense_floor_rate is None:
            # 값이 없다는 것 자체가 답이다 — 근거를 요구받지 않는다(숫자가 아니다).
            result["margin_defense_floor_rate"] = None
        else:
            margin_ref = _optional_source_ref(policy, "margin_defense_floor_rate", state)
            if margin_ref is not None:
                result["margin_defense_floor_rate"] = str(policy.margin_defense_floor_rate)
                evidence.append(
                    _evidence(
                        "margin_defense_floor_rate",
                        policy.margin_defense_floor_rate,
                        "ratio",
                        margin_ref,
                        source="persona",
                    )
                )

        result["evidence"] = evidence
        return result

    def project_cashflow(self, args: dict[str, Any], state: FinanceAgentState) -> dict[str, Any]:
        del args
        position, policy, events = self._context(state)
        projection = project_cashflow(
            as_of=state.request.context.as_of,
            current_cash_krw=Decimal(position["current_cash_krw"]),
            horizon_end=state.request.context.as_of
            + timedelta(days=policy.cashflow_projection_days),
            cash_events=events,
        )
        state.projection = projection
        return {
            "base_projected_cash_min": str(projection.projected_cash_min),
            "evidence": [
                _evidence(
                    "base_projected_cash_min",
                    projection.projected_cash_min,
                    "krw",
                    _tool_ref("project_cashflow", state),
                )
            ],
        }

    def calculate_purchase_finance_cap(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        _, policy, _ = self._context(state)
        if policy.purchase_payment_days is None:
            raise FinanceDataNotReady("purchase_payment_days")
        if state.projection is None:
            self.project_cashflow({}, state)
        cap = calculate_finance_cap(base_projection=state.projection, policy=policy)
        return {
            "finance_cap_amount_krw": str(cap),
            "base_projected_cash_min": str(state.projection.projected_cash_min),
            "evidence": [
                _evidence(
                    "finance_cap_amount_krw",
                    cap,
                    "krw",
                    _tool_ref("calculate_purchase_finance_cap", state),
                ),
                _evidence(
                    "base_projected_cash_min",
                    state.projection.projected_cash_min,
                    "krw",
                    _tool_ref("project_cashflow", state),
                ),
            ],
        }

    def analyze_payment_pressure(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        _, policy, events = self._context(state)
        if state.projection is None:
            self.project_cashflow({}, state)
        pressure = derive_cash_priority(
            projected_cash_min=state.projection.projected_cash_min, policy=policy
        )
        dates = [
            item.isoformat()
            for item in derive_critical_payment_dates(
                current_cash_krw=Decimal(self._context(state)[0]["current_cash_krw"]),
                cash_events=events,
                minimum_cash_balance_krw=policy.minimum_cash_balance_krw,
            )
        ]
        ratio = state.projection.projected_cash_min / policy.minimum_cash_balance_krw
        # 압박 판정은 Tool 이 이미 만들었다. 여기서 정하는 것은 **근거를 달 수 있는가**
        # 뿐이고, 못 다는 claim 은 값도 싣지 않는다 (`_optional_source_ref` 참조).
        priority_refs = [
            _optional_source_ref(policy, key, state)
            for key in (
                "cash_priority_reference",
                "cash_priority_high_ratio",
                "cash_priority_medium_ratio",
            )
        ]
        minimum_cash_ref = _optional_source_ref(policy, "minimum_cash_balance_krw", state)

        result: dict[str, Any] = {
            "base_projected_cash_min": str(state.projection.projected_cash_min),
        }
        evidence: list[Evidence] = [
            _evidence(
                "base_projected_cash_min",
                state.projection.projected_cash_min,
                "krw",
                _tool_ref("project_cashflow", state),
            ),
        ]

        if all(ref is not None for ref in priority_refs):
            result["payment_pressure"] = pressure
            evidence.append(
                Evidence(
                    claim="payment_pressure",
                    source="tool_calc",
                    ref_ids=(
                        _tool_ref("analyze_payment_pressure", state),
                        *(ref for ref in priority_refs if ref is not None),
                    ),
                    value=float(ratio),
                    unit="ratio",
                    evidence_grade="OFFICIAL",
                    evidence_detail=(
                        "base_projected_cash_min / minimum_cash_balance_krw; "
                        "compared with cash_priority_high_ratio and "
                        "cash_priority_medium_ratio."
                    ),
                )
            )
        if minimum_cash_ref is not None:
            result["critical_payment_dates"] = dates
            evidence.append(
                Evidence(
                    claim="critical_payment_dates",
                    source="tool_calc",
                    ref_ids=(
                        _tool_ref("analyze_payment_pressure", state),
                        minimum_cash_ref,
                    ),
                    value=float(policy.minimum_cash_balance_krw),
                    unit="KRW",
                    evidence_grade="SIM_FIXED",
                    evidence_detail=(
                        "Payment dates whose post-payment cash is below the "
                        "Finance minimum-cash threshold, plus the maximum daily outflow date."
                    ),
                )
            )

        result["evidence"] = evidence
        return result

    def evaluate_purchase_scenario(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        payload = state.request.payload
        amount = Decimal(str(payload["total_amount_krw"]))
        position, policy, events = self._context(state)
        horizon = state.request.context.as_of + timedelta(days=policy.cashflow_projection_days)
        schedule = _scenario_schedule(
            scenario=payload,
            as_of=state.request.context.as_of,
            horizon=horizon,
            default_payment_days=policy.purchase_payment_days,
        )
        base_projection = project_cashflow(
            as_of=state.request.context.as_of,
            current_cash_krw=Decimal(position["current_cash_krw"]),
            horizon_end=horizon,
            cash_events=events,
        )
        base_scenario_projection = project_cashflow(
            as_of=state.request.context.as_of,
            current_cash_krw=Decimal(position["current_cash_krw"]),
            horizon_end=horizon,
            cash_events=[
                *events,
                *_schedule_events(payload["scenario_id"], schedule, stress=False),
            ],
        )
        stress_scenario_projection = project_cashflow(
            as_of=state.request.context.as_of,
            current_cash_krw=Decimal(position["current_cash_krw"]),
            horizon_end=horizon,
            cash_events=[
                *events,
                *_schedule_events(payload["scenario_id"], schedule, stress=True),
            ],
        )
        cap = _calculate_schedule_cap(
            base_projection=base_projection,
            schedule=schedule,
            total_amount=amount,
            minimum_cash=policy.minimum_cash_balance_krw,
        )
        state.projection = base_projection
        state.scenario_projection = base_scenario_projection
        state.scenario_schedule = schedule
        state.base_state_violated = (
            base_projection.projected_cash_min < policy.minimum_cash_balance_krw
        )
        base_safe = base_scenario_projection.projected_cash_min >= policy.minimum_cash_balance_krw
        stress_safe = (
            stress_scenario_projection.projected_cash_min >= policy.minimum_cash_balance_krw
        )
        scenario_verdict = classify_base_stress(base_safe=base_safe, stress_safe=stress_safe)
        if state.base_state_violated:
            cap = Decimal(0)
            verdict = "reject"
            rule_id = "FIN-BASE-MIN-CASH"
            reason = "Base Finance minimum-cash rule failed."
        elif scenario_verdict == "ok":
            verdict, rule_id, reason = "ok", "FIN-BASE-STRESS", "BASE and STRESS passed."
        elif scenario_verdict == "conditional":
            verdict, rule_id, reason = (
                "conditional",
                "FIN-BASE-STRESS",
                "BASE passed and STRESS failed.",
            )
        else:
            verdict, rule_id, reason = "reject", "FIN-BASE-STRESS", "BASE failed."
        state.scenario_cap = cap
        scenario_ref = str(payload["scenario_id"])
        return {
            "scenario_id": payload["scenario_id"],
            "verdict": verdict,
            "adjustability": "NOT_NEEDED" if verdict == "ok" else "NOT_ADJUSTABLE",
            "finance_cap_amount_krw": str(cap),
            "scenario_projected_cash_min": str(base_scenario_projection.projected_cash_min),
            "stress_projected_cash_min": str(stress_scenario_projection.projected_cash_min),
            "critical_cash_date": base_scenario_projection.projected_cash_min_date.isoformat(),
            "rule_id": rule_id,
            "payment_schedule": [
                ({
                    "seq": item.seq,
                    "purchase_date": item.purchase_date.isoformat(),
                    "payment_date": item.payment_date.isoformat(),
                    "qty_kg": str(item.qty_kg) if item.qty_kg is not None else None,
                    "amount_krw": str(item.amount_krw),
                    "amount_max_krw": str(item.amount_max_krw),
                    "basis": item.basis,
                } if item.qty_kg is not None else {
                    "payment_date": item.payment_date.isoformat(),
                    "amount_krw": str(item.amount_krw),
                })
                for item in schedule
            ],
            "reason": reason,
            "rules": [{"rule_id": rule_id, "status": "PASS" if verdict == "ok" else "FAIL"}],
            "evidence": [
                _evidence("scenario_id", 1, "identity", scenario_ref),
                _evidence(
                    "finance_cap_amount_krw",
                    cap,
                    "krw",
                    _tool_ref("evaluate_purchase_scenario", state),
                ),
                _evidence(
                    "scenario_projected_cash_min",
                    base_scenario_projection.projected_cash_min,
                    "krw",
                    _branch_ref("cashflow", state),
                ),
                _evidence(
                    "stress_projected_cash_min",
                    stress_scenario_projection.projected_cash_min,
                    "krw",
                    _branch_ref("stress-cashflow", state),
                ),
                _evidence("verdict", verdict == "ok", "boolean", _branch_ref(rule_id, state)),
                _evidence(
                    "payment_schedule",
                    len(schedule),
                    "payment_count",
                    _branch_ref("payment-schedule", state),
                ),
                _evidence(
                    "adjustability",
                    0 if verdict == "ok" else 2,
                    "enum_code",
                    _branch_ref(rule_id, state),
                ),
            ],
        }

    def validate_amount_adjustment(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        axis = args.get("axis", "amount")
        if axis != "amount":
            raise ValueError("Finance may adjust only the amount axis")
        candidate = Decimal(str(args["candidate_amount_krw"]))
        if candidate < 0:
            raise ValueError("candidate amount must not be negative")
        self._context(state)
        cap = state.scenario_cap
        if cap is None:
            raise FinanceDataNotReady("scenario_finance_cap")
        source_values = {
            Decimal(str(state.request.payload[key]))
            for key in ("candidate_amount_krw", "proposed_amount_krw")
            if state.request.payload.get(key) is not None
        }
        source_values.add(cap)
        if candidate not in source_values:
            raise ValueError("candidate amount has no DB, policy, payload, or Tool evidence source")
        valid = candidate <= cap
        return {
            "candidate_amount_krw": str(candidate),
            "validation_status": "PASS" if valid else "FAIL",
            "evidence": [
                _evidence(
                    "candidate_amount_krw",
                    candidate,
                    "krw",
                    _tool_ref("validate_amount_adjustment", state),
                ),
                _evidence(
                    "validation_status",
                    valid,
                    "boolean",
                    _branch_ref("FIN-CAP", state),
                ),
            ],
        }


@dataclass
class FinanceAgentState:
    request: AgentRequest
    branch_id: str = "PRE_PURCHASE"
    observations: list[dict[str, Any]] = field(default_factory=list)
    tool_order: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    replans: int = 0
    context_cache: tuple[dict[str, Any], FinancePolicy, list[CashEvent]] | None = None
    projection: Any = None
    scenario_projection: Any = None
    scenario_cap: Decimal | None = None
    scenario_schedule: tuple[ScenarioPayment, ...] = ()
    base_state_violated: bool = False
    missing_sources: list[str] = field(default_factory=list)

    def note_missing_source(self, key: str) -> None:
        """근거를 달지 못해 뺀 정책값을 기록한다. 어댑터 경계와 같은 이름을 쓴다."""
        name = f"{key}@policy_source_ref"
        if name not in self.missing_sources:
            self.missing_sources.append(name)


_CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    "finance_position": frozenset({"assess_finance_position"}),
    "cashflow_projection": frozenset(
        {"project_cashflow", "calculate_purchase_finance_cap", "analyze_payment_pressure"}
    ),
    "finance_cap": frozenset({"calculate_purchase_finance_cap"}),
    "payment_pressure": frozenset({"analyze_payment_pressure"}),
    "scenario_evaluation": frozenset({"evaluate_purchase_scenario"}),
    "amount_adjustment_validation": frozenset({"validate_amount_adjustment"}),
}

_PRE_REQUIRED_CAPABILITIES = frozenset(
    {"finance_position", "cashflow_projection", "finance_cap", "payment_pressure"}
)
_SCENARIO_REQUIRED_CAPABILITIES = frozenset({"scenario_evaluation"})


class FinanceAgentController:
    def __init__(
        self,
        data_port: FinanceAsOfDataPort,
        planner: FinancePlanner | None = None,
        finalizer: FinanceFinalizer | None = None,
        *,
        max_tool_calls: int | None = None,
        max_replans: int | None = None,
    ):
        self.registry = FinanceToolRegistry(data_port)
        if planner is None:
            configured_planner, configured_finalizer, provider_state = (
                _configured_finance_llms()
            )
            self.planner = configured_planner
            self.finalizer = finalizer or configured_finalizer
            self._provider_state = provider_state
            # 설정으로 껐을 때만 DISABLED 다. 주입된 Planner 는 설정과 무관하다.
            self.llm_enabled = provider_state is not None
        else:
            self.planner = planner
            self.finalizer = finalizer or DeterministicFinanceFinalizer()
            self._provider_state = None
            self.llm_enabled = not isinstance(planner, DeterministicFinancePlanner)
        self.max_tool_calls = max_tool_calls or int(
            os.getenv("FINANCE_MAX_TOOL_CALLS", str(DEFAULT_MAX_TOOL_CALLS))
        )
        self.max_replans = (
            max_replans
            if max_replans is not None
            else int(os.getenv("FINANCE_MAX_REPLANS", str(DEFAULT_MAX_REPLANS)))
        )

    def run(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        if request.agent != "finance" or request.mode not in (
            "PRE_PURCHASE",
            "SCENARIO_VALIDATION",
        ):
            raise ValueError("Finance v2.2 supports only its two core modes")
        started = time.monotonic()
        run_id = str(uuid4())
        states: list[FinanceAgentState] = []
        runtime_status: Literal["READY", "RUNTIME_NOT_READY", "ERROR"] = "READY"
        missing_data: tuple[str, ...] = ()
        error_reason = ""
        shared_context = None
        seen: set[str] = set()
        total_calls = 0
        total_replans = 0
        planner_failed = False
        try:
            _validate_finance_payload(request)
            branch_requests = self._branch_requests(request)
            for branch_request in branch_requests:
                branch_id = str(branch_request.payload.get("scenario_id", "PRE_PURCHASE"))
                state = FinanceAgentState(
                    branch_request,
                    branch_id=branch_id,
                    context_cache=shared_context,
                )
                # ★ 루프 **전에** 담는다. 실패해도 그때까지의 observation 과 재계획
                #   횟수가 이력에 남아야 한다 — 실패한 실행일수록 흔적이 필요하다.
                states.append(state)
                total_calls, total_replans = self._execute_loop(
                    state,
                    seen=seen,
                    total_calls=total_calls,
                    total_replans=total_replans,
                )
                shared_context = state.context_cache
        except FinancePlannerFailure as exc:
            planner_failed = True
            runtime_status, error_reason = "ERROR", str(exc)
        except FinanceDataNotReady as exc:
            runtime_status, missing_data, error_reason = "RUNTIME_NOT_READY", (exc.key,), str(exc)
        except Exception as exc:  # noqa: BLE001 - Agent boundary converts failures to ERROR.
            runtime_status, error_reason = "ERROR", str(exc)

        payload, evidences, business_status, adjustments = self._finalize(
            request, states, runtime_status
        )
        # 🔴 LLMStatus 는 **이번 실행에서 실제로 무슨 일이 있었는가**다 (envelope §LLMStatus).
        #    예전에는 `SUCCESS if attempts else DISABLED` 였다. 그러면 LLM 을 켜 두고도
        #    Controller 가 첫 Tool 전에 접힌 실행이 전부 `DISABLED` 로 남는다 — 이력에는
        #    *"LLM 을 안 켰다"* 고 적히고, 실제로는 **켜 뒀는데 부를 일이 없었다** 이다.
        #    둘은 다음 조치가 다르다.
        llm_status = self._llm_status(planner_failed=planner_failed)
        llm_fallback_used = planner_failed
        if runtime_status == "READY":
            finalization_evidence = [*evidences]
            for verdict in payload.get("verdicts", []):
                finalization_evidence.extend(
                    _evidence_from_dict(item) for item in verdict.get("evidences", [])
                )
            try:
                reasoning = self.finalizer.finalize(
                    mode=request.mode,
                    business_status=business_status,
                    evidences=tuple(finalization_evidence),
                )
                _validate_ready_reasoning(reasoning)
                llm_status = self._llm_status(planner_failed=planner_failed)
            except Exception:  # noqa: BLE001 - complete Evidence permits safe fallback.
                reasoning = self._fallback_reasoning(request.mode, business_status)
                llm_status = "DISABLED" if not self.llm_enabled else "FALLBACK"
                llm_fallback_used = self.llm_enabled
        else:
            reasoning = error_reason[:240]
        elapsed = int((time.monotonic() - started) * 1000)
        observations = [item for state in states for item in state.observations]
        dept_meta = _finance_dept_meta(request.mode, payload, states)
        if dept_meta is not None and runtime_status == "READY":
            observations.append(dept_meta)
        if self._provider_state is not None:
            observations.append(
                {
                    "observation_type": "finance_llm_provider",
                    "primary_provider": self._provider_state.primary_provider,
                    "effective_provider": self._provider_state.effective_provider,
                    "provider_fallback_used": self._provider_state.active,
                    "provider_fallback_reason": self._provider_state.reason,
                }
            )
        used_tools = [item for state in states for item in state.tool_order]
        rules = [f"{state.branch_id}:{rule}" for state in states for rule in state.rules]
        metadata = ExecutionMetadata(
            run_id=run_id,
            request_id=request.context.request_id,
            agent="finance",
            used_tools=tuple(used_tools),
            tool_order=tuple(range(1, len(used_tools) + 1)),
            observations=tuple(
                json.dumps(o, default=str, sort_keys=True) for o in observations
            ),
            rules_applied=tuple(rules),
            # 🔴 `total_replans` 가 아니라 상태에서 센다. 루프가 예외로 끝나면 지역
            #    변수는 갱신되지 않아 **실패한 실행의 재계획이 0 으로 남았다** — 가장
            #    알아야 할 실행에서 숫자가 사라진다.
            replans=sum(state.replans for state in states),
            llm_status=llm_status,
            llm_model=(
                self.finalizer.model if self.finalizer.attempts else self.planner.model
            ),
            llm_attempts=self.planner.attempts + self.finalizer.attempts,
            llm_fallback_used=llm_fallback_used,
            elapsed_ms=elapsed,
        )
        # 근거가 없어 뺀 정책값을 밝힌다. 실행은 계속했지만 **못 낸 것을 낸 척하지
        # 않는다** (§3.7.6). 이미 담긴 missing_data 뒤에 붙이고 중복은 지운다.
        missing_data = tuple(
            dict.fromkeys(
                [*missing_data, *(item for state in states for item in state.missing_sources)]
            )
        )
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="finance",
            mode=request.mode,
            run_id=run_id,
            runtime_status=runtime_status,
            business_status=business_status,
            payload=payload,
            evidences=tuple(evidences),
            suggested_adjustments=tuple(adjustments),
            reasoning=reasoning,
            missing_data=missing_data,
            needs_followup=(runtime_status == "RUNTIME_NOT_READY" or bool(adjustments)),
            additional_validation_required=False,
        )
        nested_findings = validate_finance_scenario_output(reply)
        if nested_findings:
            reply = replace(
                reply,
                runtime_status="ERROR",
                business_status="skipped",
                payload={},
                evidences=(),
                suggested_adjustments=(),
                reasoning="Finance scenario output validation failed.",
                needs_followup=True,
            )
        try:
            save_finance_execution(request=request, reply=reply, metadata=metadata)
        except Exception:  # noqa: BLE001 - persistence failure is an Agent ERROR value.
            reply = replace(
                reply,
                runtime_status="ERROR",
                business_status="skipped",
                payload={},
                evidences=(),
                suggested_adjustments=(),
                reasoning="Finance run history persistence failed.",
                missing_data=(),
                needs_followup=True,
            )
        return reply, metadata

    def _llm_status(self, *, planner_failed: bool) -> str:
        """공용 `LLMStatus` 의미를 재무 실행에 그대로 적용한다.

            DISABLED          설정으로 껐다
            SKIPPED_TEMPLATE  켜져 있는데 **이번 실행에서는 부를 일이 없었다**
            SUCCESS           실제로 불렀고 쓸 수 있는 답을 받았다
            FALLBACK          불렀는데 실패해서 결정론이 대신 답했다

        ★ Gemini→Gemma **Provider 대체는 `FALLBACK` 이 아니다.** LLM 은 답을 냈다 —
          다른 Provider 가 냈을 뿐이다. 그 사실은 observations 로 따로 남긴다 (§17).
        """
        if not self.llm_enabled:
            return "DISABLED"
        if planner_failed:
            return "FALLBACK"
        if self.planner.attempts + self.finalizer.attempts == 0:
            return "SKIPPED_TEMPLATE"
        return "SUCCESS"

    def _branch_requests(self, request: AgentRequest) -> list[AgentRequest]:
        if request.mode != "SCENARIO_VALIDATION":
            return [request]
        scenarios = request.payload.get("scenarios")
        if scenarios is None:
            payload = dict(request.payload)
            payload["scenario_id"] = _scenario_identity(payload)
            return [replace(request, payload=payload)]
        if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
            raise ValueError("SCENARIO_VALIDATION requires one to three scenarios")
        branches: list[AgentRequest] = []
        for scenario in scenarios:
            payload = dict(scenario)
            payload["scenario_id"] = _scenario_identity(payload)
            branches.append(replace(request, payload=payload))
        return branches

    def _execute_loop(
        self,
        state: FinanceAgentState,
        *,
        seen: set[str],
        total_calls: int,
        total_replans: int,
    ) -> tuple[int, int]:
        required = set(
            _PRE_REQUIRED_CAPABILITIES
            if state.request.mode == "PRE_PURCHASE"
            else _SCENARIO_REQUIRED_CAPABILITIES
        )
        while total_calls < self.max_tool_calls:
            satisfied = _satisfied_capabilities(state)
            if _scenario_verdict(state) == "reject" and not state.base_state_violated:
                required.add("amount_adjustment_validation")
            missing = tuple(sorted(required - satisfied))
            planner_tools = frozenset().union(*(_CAPABILITY_TOOLS[name] for name in missing))
            if not planner_tools:
                planner_tools = self.registry.names_for(state.request.mode)
            try:
                action = self.planner.decide(
                    request=state.request,
                    allowed_tools=planner_tools,
                    observations=tuple(state.observations),
                    missing_capabilities=missing,
                )
            except FinancePlannerContractViolation as exc:
                # 모델이 계약을 어겼다 — **되물어 볼 가치가 있다.** 왜 반려됐는지를
                # GUARD 로 남기면 다음 호출의 프롬프트에 그대로 들어간다.
                total_replans = self._guard_replan(
                    state,
                    total_replans,
                    {"rejected_action": _short_reason(str(exc)), "unresolved": list(missing)},
                )
                continue
            except FinancePlannerFailure:
                raise
            except Exception as exc:
                # Provider 장애·네트워크·구조화 출력 파싱 불가 — 다시 물어도 같다.
                raise FinancePlannerFailure(str(exc)) from exc
            if action.finalize:
                if not missing:
                    return total_calls, total_replans
                total_replans = self._guard_replan(
                    state, total_replans, {"unresolved": list(missing)}
                )
                continue
            if action.tool_name is None:
                raise RuntimeError("planner returned neither a tool nor finalize")
            if action.tool_name not in planner_tools:
                total_replans = self._guard_replan(
                    state,
                    total_replans,
                    {"rejected_tool": action.tool_name, "unresolved": list(missing)},
                )
                continue
            signature = json.dumps(
                [state.branch_id, action.tool_name, action.arguments],
                sort_keys=True,
                default=str,
            )
            if signature in seen:
                raise RuntimeError("duplicate unresolved Finance tool call blocked")
            seen.add(signature)
            arguments = self._source_owned_arguments(action, state)
            observation = self.registry.execute(action.tool_name, arguments, state)
            total_calls += 1
            state.tool_order.append(action.tool_name)
            state.observations.append(
                {
                    "branch_id": state.branch_id,
                    "tool": action.tool_name,
                    "reason": _short_reason(action.reason),
                    "result": observation,
                }
            )
            state.rules.extend(item["rule_id"] for item in observation.get("rules", []))
        raise RuntimeError("Finance tool call limit exceeded")

    def _guard_replan(
        self, state: FinanceAgentState, total_replans: int, detail: dict[str, Any]
    ) -> int:
        if total_replans >= self.max_replans:
            # 되묻기에는 상한이 있다. 넘으면 최종 실패다 — 계약 위반을 무한히 숨기지
            # 않는다. `FinancePlannerFailure` 로 올려 이력에 FALLBACK 으로 남긴다.
            raise FinancePlannerFailure(
                "required Finance capability planning did not complete"
            )
        state.replans += 1
        state.observations.append(
            {"branch_id": state.branch_id, "type": "GUARD", **detail}
        )
        return total_replans + 1

    def _source_owned_arguments(
        self, action: ToolAction, state: FinanceAgentState
    ) -> dict[str, Any]:
        if action.tool_name != "validate_amount_adjustment":
            return action.arguments
        if action.arguments.get("axis", "amount") != "amount":
            raise ValueError("Finance may adjust only the amount axis")
        source_amount = next(
            (
                state.request.payload[key]
                for key in ("candidate_amount_krw", "proposed_amount_krw")
                if state.request.payload.get(key) is not None
            ),
            state.scenario_cap,
        )
        if source_amount is None:
            raise FinanceDataNotReady("amount_adjustment_source")
        return {"axis": "amount", "candidate_amount_krw": source_amount}

    def _finalize(
        self, request: AgentRequest, states: list[FinanceAgentState], runtime_status: str
    ) -> tuple[dict[str, Any], list[Evidence], str, list[SuggestedAdjustment]]:
        if runtime_status != "READY":
            return {}, [], "skipped", []
        if request.mode == "SCENARIO_VALIDATION":
            results = [self._scenario_result(state) for state in states]
            verdicts = [result["verdict"] for result in results]
            status = (
                "reject"
                if "reject" in verdicts
                else "conditional"
                if "conditional" in verdicts
                else "ok"
            )
            indexed_evidence = _indexed_verdict_evidence(results)
            if "scenarios" in request.payload:
                adjustments = [
                    _adjustment_from_dict(adjustment)
                    for result in results
                    for adjustment in result["suggested_adjustments"]
                ]
                return {"verdicts": results}, indexed_evidence, status, adjustments
            result = results[0]
            branch_evidence = [_evidence_from_dict(item) for item in result.pop("evidences")]
            branch_adjustments = result.pop("suggested_adjustments")
            adjustments = [_adjustment_from_dict(item) for item in branch_adjustments]
            return (
                {"verdicts": [dict(result)], **result},
                [*indexed_evidence, *branch_evidence],
                status,
                adjustments,
            )

        state = states[0]
        payload: dict[str, Any] = {}
        evidences: list[Evidence] = []
        for observation in state.observations:
            result = observation.get("result", {})
            for key, value in result.items():
                if key not in {"evidence", "rules"}:
                    payload[key] = _json_value(value)
            evidences.extend(result.get("evidence", []))
        evidence_by_claim = {item.claim: item for item in evidences}
        return payload, list(evidence_by_claim.values()), "ok", []

    def _scenario_result(self, state: FinanceAgentState) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        evidence: list[Evidence] = []
        for observation in state.observations:
            result = observation.get("result", {})
            for key, value in result.items():
                if key not in {"evidence", "rules"}:
                    payload[key] = _json_value(value)
            evidence.extend(result.get("evidence", []))
        validation = next(
            (
                item["result"]
                for item in reversed(state.observations)
                if item.get("tool") == "validate_amount_adjustment"
                and item["result"]["validation_status"] == "PASS"
            ),
            None,
        )
        adjustments: list[dict[str, Any]] = []
        if payload["verdict"] == "ok":
            payload["adjustability"] = "NOT_NEEDED"
        elif validation and Decimal(str(validation["candidate_amount_krw"])) > 0:
            payload["adjustability"] = "ADJUSTABLE"
            adjustments.append(
                {
                    "dept": "finance",
                    "axis": "amount",
                    "target_value": float(validation["candidate_amount_krw"]),
                    "unit": "krw",
                    "reason": "Verified Finance amount alternative.",
                    "ref_ids": [_tool_ref("validate_amount_adjustment", state)],
                }
            )
        else:
            payload["adjustability"] = "NOT_ADJUSTABLE"
        evidence = [item for item in evidence if item.claim != "adjustability"]
        adjustability_code = {
            "NOT_NEEDED": 0,
            "ADJUSTABLE": 1,
            "NOT_ADJUSTABLE": 2,
        }[payload["adjustability"]]
        evidence.append(
            _evidence(
                "adjustability",
                adjustability_code,
                "enum_code",
                _branch_ref("adjustability", state),
            )
        )
        payload["evidences"] = [_evidence_dict(item) for item in evidence]
        payload["suggested_adjustments"] = adjustments
        return payload

    @staticmethod
    def _fallback_reasoning(mode: str, business: str) -> str:
        if mode == "PRE_PURCHASE":
            return _FINAL_EXPLANATIONS["PRE_BOUNDARY"]
        if business == "reject":
            return _FINAL_EXPLANATIONS["SCENARIO_REJECT"]
        return _FINAL_EXPLANATIONS["SCENARIO_ACCEPT"]


def _finance_dept_meta(
    mode: str, payload: dict[str, Any], states: list[FinanceAgentState]
) -> dict[str, Any] | None:
    """이번 실행의 사용 입력·산출 필드를 **재무 자신이** 기계가 읽을 형태로 낸다.

    Critic 의 `E-GRADE-LEAK`(재무 cap 에 등급·수량이 섞였나)와 `E-AUTHORITY`(부서가
    S3 전속 판정을 냈나)는 이 둘이 없으면 아예 돌지 않는다 — 통과가 아니라 **생략**이다.

    ★ **마스터가 추측하면 안 되는 것이라 재무가 낸다.** 마스터는 Tool 이름이나
      payload 키를 보고 *"재무가 무엇을 읽었는지"* 를 알 수 없다. 모르는 것을 빈
      dict 로 보내면 Critic 은 *"금지 입력이 없다"* 로 읽고 **통과시킨다** — 모르는
      것이 통과가 되는 구조라, 마스터는 아예 안 보내고 생략으로 남겨 왔다.

    ★ **관측이지 선언이 아니다.** `inputs_used` 는 실행에서 실제로 성공한 Tool
      (`state.tool_order`)만 보고 만든다. `produced_fields` 는 실제로 실린 payload
      키다. 둘 다 실행과 어긋날 수 없다.

    PRE_PURCHASE 만 낸다 — Critic 의 두 검사가 조언자 경계 회신을 대상으로 한다.
    """
    if mode != "PRE_PURCHASE" or not states:
        return None
    executed = [tool for state in states for tool in state.tool_order]
    inputs: list[str] = []
    for tool in executed:
        for name in _CAP_TOOL_INPUTS.get(tool, ()):
            if name not in inputs:
                inputs.append(name)
    return {
        "observation_type": "finance_dept_meta",
        "inputs_used": {FINANCE_CAP_CHECK_ID: inputs},
        # 값이 `None` 인 키는 뺀다 — 어댑터가 경계에서 실제로 빼는 것과 같은 기준이다
        # (`_controller_run` 의 `margin_defense_floor_rate`). 산출하지 않은 필드를
        # 산출했다고 적으면 권한 검사가 엉뚱한 것을 본다.
        "produced_fields": sorted(key for key, value in payload.items() if value is not None),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def _evidence(
    claim: str,
    value: Any,
    unit: str,
    ref_id: str,
    *,
    source: Literal["finance", "tool_calc", "persona"] = "tool_calc",
) -> Evidence:
    numeric = float(value)
    return Evidence(
        claim=claim,
        source=source,
        ref_ids=(ref_id,),
        value=numeric,
        unit=unit,
        evidence_grade="OFFICIAL",
    )


def _tool_ref(tool_name: str, state: FinanceAgentState) -> str:
    return _branch_ref(tool_name, state)


def _branch_ref(kind: str, state: FinanceAgentState) -> str:
    return (
        f"FIN-AGENT:{state.request.context.request_id}:{state.request.call_seq}:"
        f"{state.branch_id}:{kind}"
    )


def _short_reason(reason: str) -> str:
    return " ".join(reason.split())[:160]


def _validate_ready_reasoning(reasoning: str) -> None:
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", reasoning.strip()) if part]
    if not reasoning.strip() or len(sentences) > 3:
        raise ValueError("Finance reasoning must contain one to three sentences")
    if re.search(r"\d", reasoning):
        raise ValueError("Finance reasoning must not introduce numeric claims")


def _satisfied_capabilities(state: FinanceAgentState) -> set[str]:
    keys = {
        key
        for observation in state.observations
        for key in observation.get("result", {})
    }
    # ★ **실행한 Tool 로도 센다.** 결과 키만 보면, 정책 출처가 없어 claim 을 뺀 실행에서
    #   capability 가 영영 안 채워진 것처럼 보여 Planner 를 계속 다시 부른다 — 답을
    #   못 내는 것도 아닌데 Tool 호출 상한까지 돌다 죽는다. Tool 이 성공적으로 돌았으면
    #   그 capability 는 조사된 것이다 (`tool_order` 는 성공한 실행만 담는다).
    out: set[str] = {
        capability
        for capability, tools in _CAPABILITY_TOOLS.items()
        if tools & set(state.tool_order)
        and (capability != "finance_cap" or state.request.mode == "PRE_PURCHASE")
    }
    if {"available_cash", "payroll_payment_day"} <= keys:
        out.add("finance_position")
    if "base_projected_cash_min" in keys:
        out.add("cashflow_projection")
    if "finance_cap_amount_krw" in keys and state.request.mode == "PRE_PURCHASE":
        out.add("finance_cap")
    if {"payment_pressure", "critical_payment_dates"} <= keys:
        out.add("payment_pressure")
    if "verdict" in keys:
        out.add("scenario_evaluation")
    if "validation_status" in keys:
        out.add("amount_adjustment_validation")
    return out


def _scenario_verdict(state: FinanceAgentState) -> str | None:
    return next(
        (
            observation["result"]["verdict"]
            for observation in reversed(state.observations)
            if "verdict" in observation.get("result", {})
        ),
        None,
    )


def _scenario_schedule(
    *,
    scenario: Any,
    as_of: date,
    horizon: date,
    default_payment_days: int | None,
) -> tuple[ScenarioPayment, ...]:
    amount = Decimal(str(scenario["total_amount_krw"]))
    if amount <= 0:
        raise ValueError("total_amount_krw must be positive")
    raw_schedule = scenario.get("payment_schedule")
    if raw_schedule is None:
        if default_payment_days is None:
            raise FinanceDataNotReady("purchase_payment_days")
        payment_date = as_of + timedelta(days=default_payment_days)
        if not as_of < payment_date <= horizon:
            raise FinanceDataNotReady("default_purchase_payment_date")
        return (
            ScenarioPayment(
                seq=1,
                purchase_date=as_of,
                payment_date=payment_date,
                qty_kg=None,
                amount_krw=amount,
                amount_max_krw=amount,
                basis="non_split_policy_reconstruction",
            ),
        )
    if not isinstance(raw_schedule, list) or not raw_schedule:
        raise ValueError("payment_schedule must be a non-empty list")
    split_plan = scenario.get("split_plan")
    if not isinstance(split_plan, list) or len(split_plan) != len(raw_schedule):
        raise ValueError("payment_schedule must correspond one-to-one with split_plan")
    total_qty = Decimal(str(scenario["total_qty_kg"]))
    max_price = Decimal(str(scenario["max_price"]))
    authoritative_h1 = bool(
        scenario.get("h1_authoritative") or scenario.get("authoritative_h1_payment_data")
    )
    schedule: list[ScenarioPayment] = []
    for index, (row, split) in enumerate(zip(raw_schedule, split_plan, strict=True), start=1):
        required = {
            "seq", "purchase_date", "payment_date", "qty_kg", "amount_krw",
            "amount_max_krw", "basis",
        }
        if not required <= row.keys():
            raise ValueError("payment_schedule row is missing required Finance fields")
        purchase_date = date.fromisoformat(str(row["purchase_date"]))
        payment_date = date.fromisoformat(str(row["payment_date"]))
        payment_amount = Decimal(str(row["amount_krw"]))
        max_amount = Decimal(str(row["amount_max_krw"]))
        qty = Decimal(str(row["qty_kg"]))
        basis = str(row["basis"]).strip()
        if not isinstance(payment_date, date) or not as_of < payment_date <= horizon:
            raise ValueError("payment_date must be inside the Finance projection horizon")
        if int(row["seq"]) != index or int(split["seq"]) != index:
            raise ValueError("payment_schedule and split_plan seq must align")
        if purchase_date != date.fromisoformat(str(split["date"])):
            raise ValueError("payment_schedule purchase_date must equal split_plan date")
        split_qty = Decimal(str(split.get("qty_kg", split.get("quantity_kg"))))
        if qty != split_qty:
            raise ValueError("payment_schedule qty_kg must equal split_plan qty_kg")
        if payment_amount <= 0 or max_amount <= 0 or qty <= 0:
            raise ValueError("payment_schedule amounts and qty must be positive")
        if not basis:
            raise ValueError("payment_schedule basis must be non-empty")
        if not authoritative_h1:
            if default_payment_days is None:
                raise FinanceDataNotReady("purchase_payment_days")
            if payment_date != purchase_date + timedelta(days=default_payment_days):
                raise ValueError("payment_date must equal purchase_date plus policy days before H1")
            if max_amount != qty * max_price:
                raise ValueError("amount_max_krw must equal qty_kg times max_price")
        schedule.append(
            ScenarioPayment(
                index, purchase_date, payment_date, qty, payment_amount, max_amount, basis
            )
        )
    if sum((item.amount_krw for item in schedule), Decimal(0)) != amount:
        raise ValueError("payment_schedule amount sum must equal total_amount_krw")
    if sum((item.qty_kg or Decimal(0) for item in schedule), Decimal(0)) != total_qty:
        raise ValueError("payment_schedule qty sum must equal total_qty_kg")
    return tuple(schedule)


def _validate_finance_payload(request: AgentRequest) -> None:
    if request.mode != "SCENARIO_VALIDATION":
        return
    raw_scenarios = request.payload.get("scenarios")
    scenarios = raw_scenarios if raw_scenarios is not None else [request.payload]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        raise ValueError("SCENARIO_VALIDATION requires one to three scenarios")
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise TypeError("each Finance scenario must be an object")
        scenario_id = _scenario_identity(scenario)
        if scenario_id in scenario_ids:
            raise ValueError("scenario_id must be unique within the request")
        scenario_ids.add(scenario_id)
        try:
            amount = Decimal(str(scenario["total_amount_krw"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("total_amount_krw must be a valid number") from exc
        if (
            isinstance(scenario.get("total_amount_krw"), bool)
            or not amount.is_finite()
            or amount <= 0
        ):
            raise ValueError("total_amount_krw must be a positive finite number")
        schedule = scenario.get("payment_schedule")
        if schedule is None:
            continue
        if not isinstance(schedule, list) or not schedule:
            raise ValueError("payment_schedule must be a non-empty list")
        total = Decimal(0)
        for payment in schedule:
            if not isinstance(payment, dict):
                raise TypeError("each payment_schedule entry must be an object")
            try:
                date.fromisoformat(str(payment["payment_date"]))
                payment_amount = Decimal(str(payment["amount_krw"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("payment_schedule date and amount must be valid") from exc
            if (
                isinstance(payment.get("amount_krw"), bool)
                or not payment_amount.is_finite()
                or payment_amount <= 0
            ):
                raise ValueError("payment_schedule amount must be positive and finite")
            total += payment_amount
        if total != amount:
            raise ValueError("payment_schedule amount sum must equal total_amount_krw")


def _scenario_identity(scenario: dict[str, Any]) -> str:
    """scenario_id가 없으면 Purchase가 보장하는 non-empty label을 identity로 사용한다."""
    if "scenario_id" in scenario:
        scenario_id = scenario["scenario_id"]
        if isinstance(scenario_id, str) and scenario_id.strip():
            return scenario_id.strip()
        raise ValueError("scenario_id must be a non-empty string when present")
    label = scenario.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    raise ValueError("label must be a non-empty string when scenario_id is absent")


def validate_finance_scenario_output(reply: AgentReply) -> tuple[str, ...]:
    """공통 Envelope를 넘어 Finance가 소유한 중첩 시나리오 계보를 검증한다."""
    if reply.runtime_status != "READY" or reply.mode != "SCENARIO_VALIDATION":
        return ()
    scenarios = reply.payload.get("verdicts")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        return ("payload.verdicts must contain one to three results",)
    # 유지하는 단일 시나리오 호환 형식은 branch Evidence를 공통 Envelope 수준에 둔다.
    # 문서화된 복수 시나리오 계약은 이를 중첩한다.
    if reply.payload.get("scenario_id") is not None and len(scenarios) == 1:
        return ()
    findings: list[str] = []
    seen: set[str] = set()
    nested_adjustment_refs: set[str] = set()
    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id") if isinstance(scenario, dict) else None
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in seen:
            findings.append("scenario result ids must be non-empty and unique")
            continue
        seen.add(scenario_id)
        if scenario.get("adjustability") not in {"NOT_NEEDED", "ADJUSTABLE", "NOT_ADJUSTABLE"}:
            findings.append(f"{scenario_id}: invalid adjustability")
        evidence = scenario.get("evidences")
        claims = {item.get("claim") for item in evidence} if isinstance(evidence, list) else set()
        required = {
            "finance_cap_amount_krw",
            "scenario_projected_cash_min",
            "payment_schedule",
            "verdict",
            "adjustability",
        }
        if not required <= claims:
            findings.append(f"{scenario_id}: nested Evidence is incomplete")
        for item in evidence if isinstance(evidence, list) else ():
            for ref in item.get("ref_ids", ()):
                if str(ref).startswith("FIN-AGENT:") and scenario_id not in str(ref):
                    findings.append(f"{scenario_id}: cross-branch Evidence ref")
        adjustments = scenario.get("suggested_adjustments", [])
        if scenario.get("adjustability") == "ADJUSTABLE" and not adjustments:
            findings.append(f"{scenario_id}: verified adjustment is missing")
        if scenario.get("adjustability") != "ADJUSTABLE" and adjustments:
            findings.append(f"{scenario_id}: unexpected adjustment")
        for adjustment in adjustments:
            refs = adjustment.get("ref_ids", ())
            if (
                adjustment.get("axis") != "amount"
                or not refs
                or not all(scenario_id in str(ref) for ref in refs)
            ):
                findings.append(f"{scenario_id}: adjustment lineage is invalid")
            nested_adjustment_refs.update(str(ref) for ref in refs)
        if adjustments and scenario.get("verdict") == "ok":
            findings.append(f"{scenario_id}: adjustment must not rewrite reject to ok")
    top_refs = {
        str(ref)
        for adjustment in reply.suggested_adjustments
        for ref in adjustment.ref_ids
    }
    if top_refs != nested_adjustment_refs:
        findings.append("top-level and nested Finance adjustments differ")
    return tuple(dict.fromkeys(findings))


def _schedule_events(
    scenario_id: object, schedule: tuple[ScenarioPayment, ...], *, stress: bool
) -> tuple[CashEvent, ...]:
    return tuple(
        CashEvent(
            event_date=payment.payment_date,
            event_type="EXTRA_PURCHASE",
            amount_krw=payment.amount_max_krw if stress else payment.amount_krw,
            direction="OUTFLOW",
            ref_id=(
                f"SCENARIO:{scenario_id}:{'STRESS' if stress else 'BASE'}:"
                f"{index}:{payment.payment_date.isoformat()}"
            ),
            source_ref=str(scenario_id),
        )
        for index, payment in enumerate(schedule, start=1)
    )


def _calculate_schedule_cap(
    *,
    base_projection: Any,
    schedule: tuple[ScenarioPayment, ...],
    total_amount: Decimal,
    minimum_cash: Decimal,
) -> Decimal:
    balances = {
        point.projection_date: point.cash_balance_krw
        for point in base_projection.projected_cash_by_date
    }
    dates = sorted({*balances, *(item.payment_date for item in schedule)})
    current_balance = balances[base_projection.as_of]
    paid = Decimal(0)
    bounds: list[Decimal] = []
    schedule_by_date: dict[date, Decimal] = {}
    for payment in schedule:
        schedule_by_date[payment.payment_date] = (
            schedule_by_date.get(payment.payment_date, Decimal(0)) + payment.amount_krw
        )
    for current_date in dates:
        if current_date in balances:
            current_balance = balances[current_date]
        paid += schedule_by_date.get(current_date, Decimal(0))
        if paid > 0:
            fraction = paid / total_amount
            bounds.append((current_balance - minimum_cash) / fraction)
    if not bounds:
        raise FinanceDataNotReady("scenario_payment_schedule")
    return max(Decimal(0), min(bounds).quantize(Decimal(1), rounding=ROUND_FLOOR))


def _evidence_dict(evidence: Evidence) -> dict[str, Any]:
    return {
        "claim": evidence.claim,
        "source": evidence.source,
        "ref_ids": list(evidence.ref_ids),
        "value": evidence.value,
        "unit": evidence.unit,
        "evidence_grade": evidence.evidence_grade,
        "evidence_detail": evidence.evidence_detail,
    }


def _evidence_from_dict(value: dict[str, Any]) -> Evidence:
    return Evidence(
        claim=value["claim"],
        source=value["source"],
        ref_ids=tuple(value["ref_ids"]),
        value=value["value"],
        unit=value["unit"],
        evidence_grade=value["evidence_grade"],
        evidence_detail=value["evidence_detail"],
    )


def _indexed_verdict_evidence(results: list[dict[str, Any]]) -> list[Evidence]:
    """실제 숫자 branch claim을 Envelope v0.4 인덱스 경로에 다시 바인딩한다."""
    indexed: list[Evidence] = []
    for index, result in enumerate(results):
        raw_evidence = result.get("evidences", [])
        by_claim = {
            item.get("claim"): item
            for item in raw_evidence
            if isinstance(item, dict) and isinstance(item.get("claim"), str)
        }
        for claim, value in result.items():
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            source = by_claim.get(claim)
            if source is None:
                continue
            indexed.append(
                Evidence(
                    claim=f"verdicts[{index}].{claim}",
                    source=source["source"],
                    ref_ids=tuple(source["ref_ids"]),
                    value=float(value),
                    unit=source["unit"],
                    evidence_grade=source["evidence_grade"],
                    evidence_detail=source.get("evidence_detail"),
                )
            )
    return indexed


def _adjustment_from_dict(value: dict[str, Any]) -> SuggestedAdjustment:
    return SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=value["target_value"],
        unit=value["unit"],
        reason=value["reason"],
        ref_ids=tuple(value["ref_ids"]),
    )
