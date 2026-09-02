from __future__ import annotations

import json
import os
import urllib.error
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import messages
from app.finance.application.harness import (
    FINALIZE_TOOL_NAME,
    build_planner_tool_adapter,
    finalize_tool,
)
from app.finance.application.orchestration import FinanceAgentController
from app.finance.db import FinanceDataNotReady
from app.finance.llm.client import (
    _DEFAULT_MODELS,
    _DEFAULT_OLLAMA_TOOL_CALLING_MODEL,
    _finance_model,
    _finance_provider_name,
    _gemini_availability_failure_reason,
    _gemini_response_text,
    _is_gemini_availability_failure,
    _load_finance_environment,
    _ollama_tool_calling_model,
    finance_planner_model,
)
from app.finance.llm.finalizer import GeminiFinanceFinalizer, OllamaFinanceFinalizer
from app.finance.llm.planner import (
    FinanceChatModel,
    FinancePlannerContractViolation,
    LangChainFinancePlanner,
    ToolAction,
    _AvailabilityFallbackFinanceFinalizer,
    _AvailabilityFallbackFinancePlanner,
    _configured_finance_llms,
    _ProviderFallbackState,
    finance_chat_model,
)
from app.finance.schemas import FinancePolicy
from app.master.envelope import AgentRequest, ExecutionContext


class _Response:
    def __init__(self, document: dict):
        self.body = json.dumps(document).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class _FinancePort:
    def load_finance_position(self, as_of):
        assert as_of == date(2025, 1, 1)
        return {
            "finance_state_id": "FIN-GEMINI-FALLBACK",
            "current_cash_krw": Decimal(1000),
            "current_debt_krw": Decimal(0),
        }

    def load_policy(self, as_of, policy_version):
        assert as_of == date(2025, 1, 1)
        assert policy_version == "v1.3-PROVISIONAL"
        return FinancePolicy(
            purchase_payment_days=1,
            payroll_date=10,
            monthly_labor_cost_krw=Decimal(100),
            minimum_cash_balance_krw=Decimal(100),
            cashflow_projection_days=30,
            cash_priority_reference="minimum_cash_balance_krw",
            cash_priority_high_ratio=Decimal(1),
            cash_priority_medium_ratio=Decimal(2),
            policy_version="v1.3-PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            source_refs={
                "payroll_date": "POL-PAYROLL-DATE",
                "monthly_labor_cost_krw": "FACT-PAYROLL-AMOUNT",
                "purchase_payment_days": "policy:purchase-days",
                "minimum_cash_balance_krw": "policy:min-cash",
                "cash_priority_reference": "policy:pressure",
                "cash_priority_high_ratio": "policy:pressure-high",
                "cash_priority_medium_ratio": "policy:pressure-medium",
            },
        )

    def load_payroll(self, as_of, horizon):
        del as_of, horizon
        return Decimal(100)

    def load_obligations(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_receivables(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_debt_schedule(self, as_of, horizon):
        del as_of, horizon
        raise FinanceDataNotReady("debt_policy")


class _UnavailablePlanner:
    model = "gemini-3.5-flash-lite"

    def __init__(self, error):
        self.error = error
        self.attempts = 0

    def decide(self, **_kwargs):
        self.attempts += 1
        raise self.error


class _FallbackPlanner:
    model = "gemma3:4b"

    def __init__(self):
        self.attempts = 0

    def decide(self, *, missing_capabilities, **_kwargs):
        self.attempts += 1
        if not missing_capabilities:
            return ToolAction(finalize=True)
        preferred = (
            "assess_finance_position",
            "analyze_payment_pressure",
            "calculate_purchase_finance_cap",
        )
        capability_tools = {
            "finance_position": "assess_finance_position",
            "payment_pressure": "analyze_payment_pressure",
            "finance_cap": "calculate_purchase_finance_cap",
            "cashflow_projection": "calculate_purchase_finance_cap",
        }
        selected = next(
            name
            for name in preferred
            if name in {capability_tools[item] for item in missing_capabilities}
        )
        return ToolAction(selected)


@pytest.fixture(autouse=True)
def _prevent_real_finance_env_loading(monkeypatch):
    monkeypatch.setattr("app.finance.llm.client._load_finance_environment", lambda: None)


def _request() -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-gemini",
            as_of=date(2025, 1, 1),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode="PRE_PURCHASE",
    )


def _gemini_document(content: dict, *, thought_first: bool = False) -> dict:
    """구조화 출력 응답 — Finalizer 가 아직 이 형태를 쓴다."""
    parts = []
    if thought_first:
        parts.append({"thought": True, "text": "internal reasoning"})
    parts.append({"text": json.dumps(content)})
    return {"candidates": [{"content": {"parts": parts}}]}


def _gemini_tool_call_document(name: str | None, args: dict | None = None) -> dict:
    """tool calling 응답 — Planner 가 실제로 받는 형태다.

    `name` 이 ``None`` 이면 Tool 을 하나도 부르지 않은 자유 문장 답을 뜻한다.
    """
    parts = [] if name is None else [{"functionCall": {"name": name, "args": args or {}}}]
    return {"candidates": [{"content": {"parts": parts}}]}


def _ollama_tool_call_document(name: str, args: dict | None = None) -> dict:
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}}


def _adapters(*names: str) -> tuple:
    """Harness 가 노출하는 것과 **같은** Tool 객체. 실행은 하지 않는다."""
    return tuple(
        finalize_tool() if name == FINALIZE_TOOL_NAME
        else build_planner_tool_adapter(name, lambda *_a, **_k: {})
        for name in names
    )


_DEFAULT_TOOLS = ("assess_finance_position",)


def _planner_decide(
    planner,
    *,
    missing=("finance_position",),
    exposed: tuple[str, ...] = _DEFAULT_TOOLS,
):
    return planner.decide(
        request=_request(),
        allowed_tools=frozenset(exposed),
        observations=(),
        missing_capabilities=missing,
        langchain_tools=_adapters(*exposed),
    )


def _gemini_planner(model: str | None = None) -> LangChainFinancePlanner:
    return LangChainFinancePlanner(finance_chat_model("gemini", model=model))


def _ollama_planner(model: str | None = None) -> LangChainFinancePlanner:
    return LangChainFinancePlanner(finance_chat_model("ollama", model=model))


def _mock_successful_pre_purchase(monkeypatch, provider, finalizer_type):
    """`provider` 쪽 Planner 만 대본대로 답하게 한다.

    ★ Planner 클래스는 이제 Provider 와 무관하게 `LangChainFinancePlanner` 하나다.
      그래서 클래스를 통째로 갈면 **Primary 가 실패하는 상황 자체를 못 만든다** —
      어느 Provider 를 감쌌는지로 갈라야 가용성 대체가 그대로 시험된다.

    ★ Tool 순서는 합법이어야 한다. `analyze_payment_pressure` 와
      `calculate_purchase_finance_cap` 은 `cashflow_projection` 을 선행으로 요구한다.
    """
    actions = iter(
        [
            ToolAction("assess_finance_position"),
            ToolAction("project_cashflow"),
            ToolAction("analyze_payment_pressure"),
            ToolAction("calculate_purchase_finance_cap"),
            ToolAction(finalize=True),
        ]
    )
    original_decide = LangChainFinancePlanner.decide

    def decide(planner, **kwargs):
        if getattr(planner.chat_model, "provider", None) != provider:
            return original_decide(planner, **kwargs)
        planner.attempts += 1
        return next(actions)

    def finalize(finalizer, **_kwargs):
        finalizer.attempts += 1
        return messages.explanation_for("PRE_PURCHASE", "ok")

    monkeypatch.setattr(LangChainFinancePlanner, "decide", decide)
    monkeypatch.setattr(finalizer_type, "finalize", finalize)


def _provider_observation(metadata):
    return next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_llm_provider"
    )


def test_finance_settings_load_env_independent_of_working_directory(tmp_path, monkeypatch):
    env_file = tmp_path / "config" / ".env"
    env_file.parent.mkdir()
    env_file.write_text(
        "FINANCE_LLM_PROVIDER=gemini\n"
        "FINANCE_LLM_MODEL=gemini-test-model\n"
        "FINANCE_GEMINI_API_KEY=test-key\n",
        encoding="utf-8",
    )
    unrelated_directory = tmp_path / "unrelated"
    unrelated_directory.mkdir()
    monkeypatch.setattr("app.finance.llm.client._ENV_FILES", (env_file,))
    monkeypatch.setattr(
        "app.finance.llm.client._load_finance_environment", _load_finance_environment
    )
    monkeypatch.chdir(unrelated_directory)

    with patch.dict(os.environ, {}, clear=True):
        provider = _finance_provider_name()

        assert provider == "gemini"
        assert _finance_model(provider) == "gemini-test-model"
        assert bool(os.getenv("FINANCE_GEMINI_API_KEY")) is True


def test_finance_env_file_does_not_override_process_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FINANCE_LLM_PROVIDER=gemini\nFINANCE_LLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.finance.llm.client._ENV_FILES", (env_file,))
    monkeypatch.setattr(
        "app.finance.llm.client._load_finance_environment", _load_finance_environment
    )

    process_environment = {
        "FINANCE_LLM_PROVIDER": "ollama",
        "FINANCE_LLM_MODEL": "process-model",
    }
    with patch.dict(os.environ, process_environment, clear=True):
        provider = _finance_provider_name()

        assert provider == "ollama"
        assert _finance_model(provider) == "process-model"


def test_finance_provider_defaults_to_gemini_even_when_global_is_ollama(monkeypatch):
    monkeypatch.delenv("FINANCE_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    assert _finance_provider_name() == "gemini"


def test_finance_provider_defaults_to_gemini(monkeypatch):
    monkeypatch.delenv("FINANCE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    assert _finance_provider_name() == "gemini"


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Finance Gemini API key is not set"),
        urllib.error.HTTPError("https://gemini.invalid", 429, "quota", {}, None),
        urllib.error.HTTPError("https://gemini.invalid", 500, "server", {}, None),
        TimeoutError("timeout"),
        urllib.error.URLError("network"),
    ],
)
def test_gemini_availability_failures_are_eligible_for_ollama(error):
    assert _is_gemini_availability_failure(error) is True


@pytest.mark.parametrize("cause", [TimeoutError("timeout"), urllib.error.URLError("network")])
def test_wrapped_gemini_transport_failures_are_eligible_for_ollama(cause):
    error = RuntimeError("Finance Gemini request failed")
    error.__cause__ = cause

    assert _is_gemini_availability_failure(error) is True


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("Finance Gemini API key is not set"), "API_KEY_MISSING"),
        (urllib.error.HTTPError("https://gemini.invalid", 429, "quota", {}, None), "HTTP_429"),
        (urllib.error.HTTPError("https://gemini.invalid", 503, "server", {}, None), "HTTP_5XX"),
        (TimeoutError("timeout"), "TIMEOUT"),
        (urllib.error.URLError("network"), "NETWORK_ERROR"),
    ],
)
def test_gemini_availability_failure_reason_is_observable(error, reason):
    assert _gemini_availability_failure_reason(error) == reason


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("https://gemini.invalid", 400, "schema", {}, None),
        urllib.error.HTTPError("https://gemini.invalid", 401, "auth", {}, None),
        urllib.error.HTTPError("https://gemini.invalid", 403, "permission", {}, None),
        urllib.error.HTTPError("https://gemini.invalid", 404, "model", {}, None),
        ValueError("invalid structured output"),
        json.JSONDecodeError("invalid JSON", "not-json", 0),
    ],
)
def test_gemini_contract_failures_are_not_eligible_for_ollama(error):
    assert _is_gemini_availability_failure(error) is False


@patch("app.finance.execution.save_finance_execution")
def test_configured_gemini_unavailable_uses_observable_ollama_provider_fallback(
    save_run, monkeypatch
):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-primary-model")
    monkeypatch.delenv("FINANCE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _mock_successful_pre_purchase(monkeypatch, "ollama", OllamaFinanceFinalizer)
    with patch("app.finance.llm.client.urllib.request.urlopen") as urlopen:
        controller = FinanceAgentController(_FinancePort())
        reply, metadata = controller.run(_request())

    assert reply.runtime_status == "READY"
    assert metadata.llm_status == "SUCCESS"
    assert metadata.llm_fallback_used is False
    assert metadata.llm_model == "gemma3:4b"
    assert metadata.llm_attempts == 7
    assert controller.planner.state.active is True
    assert _provider_observation(metadata) == {
        "effective_provider": "ollama",
        "observation_type": "finance_llm_provider",
        "primary_provider": "gemini",
        "provider_fallback_reason": "API_KEY_MISSING",
        "provider_fallback_used": True,
    }
    urlopen.assert_not_called()
    save_run.assert_called_once()


@patch("app.finance.execution.save_finance_execution")
def test_normal_gemini_provider_observation_is_distinct(save_run, monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-primary-model")
    _mock_successful_pre_purchase(monkeypatch, "gemini", GeminiFinanceFinalizer)

    reply, metadata = FinanceAgentController(_FinancePort()).run(_request())

    assert reply.runtime_status == "READY"
    assert metadata.llm_status == "SUCCESS"
    assert metadata.llm_fallback_used is False
    assert _provider_observation(metadata) == {
        "effective_provider": "gemini",
        "observation_type": "finance_llm_provider",
        "primary_provider": "gemini",
        "provider_fallback_reason": None,
        "provider_fallback_used": False,
    }
    save_run.assert_called_once()


@patch("app.finance.execution.save_finance_execution")
def test_explicit_ollama_provider_observation_is_distinct(save_run, monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemma3:4b")
    _mock_successful_pre_purchase(monkeypatch, "ollama", OllamaFinanceFinalizer)

    reply, metadata = FinanceAgentController(_FinancePort()).run(_request())

    assert reply.runtime_status == "READY"
    assert metadata.llm_status == "SUCCESS"
    assert metadata.llm_fallback_used is False
    assert _provider_observation(metadata) == {
        "effective_provider": "ollama",
        "observation_type": "finance_llm_provider",
        "primary_provider": "ollama",
        "provider_fallback_reason": None,
        "provider_fallback_used": False,
    }
    save_run.assert_called_once()


def test_schema_failure_does_not_activate_provider_fallback():
    state = _ProviderFallbackState("gemini", "gemini")
    fallback = _FallbackPlanner()
    planner = _AvailabilityFallbackFinancePlanner(
        _UnavailablePlanner(ValueError("invalid structured output")),
        fallback,
        state,
    )

    with pytest.raises(ValueError, match="invalid structured output"):
        _planner_decide(planner)

    assert state.active is False
    assert fallback.attempts == 0


def test_gemini_planner_never_uses_ollama_url(monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    seen = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        return _Response(_gemini_tool_call_document("assess_finance_position"))

    monkeypatch.setattr("app.finance.llm.client.urllib.request.urlopen", urlopen)
    action = _planner_decide(_gemini_planner())

    assert action.tool_name == "assess_finance_position"
    assert seen[0][0].full_url.startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/"
    )
    assert "127.0.0.1:11434" not in seen[0][0].full_url
    # Tool 제약은 Ollama 와 같다 — 이번 호출의 실행 가능 Tool 이 그대로 선언된다.
    body = json.loads(seen[0][0].data)
    declarations = body["tools"][0]["function_declarations"]
    assert [item["name"] for item in declarations] == ["assess_finance_position"]


def test_gemini_skips_thought_part_and_uses_later_text():
    document = _gemini_document({"ok": True}, thought_first=True)
    assert json.loads(_gemini_response_text(document)) == {"ok": True}


def test_gemini_does_not_skip_thought_signature_only():
    document = {
        "candidates": [
            {"content": {"parts": [{"thoughtSignature": "signature", "text": '{"ok": true}'}]}}
        ]
    }
    assert json.loads(_gemini_response_text(document)) == {"ok": True}


def test_gemini_thought_only_response_fails():
    document = {
        "candidates": [{"content": {"parts": [{"thought": True, "text": "internal reasoning"}]}}]
    }
    with pytest.raises(TypeError, match="did not contain text content"):
        _gemini_response_text(document)


def test_gemini_http_429_is_preserved(monkeypatch):
    """★ 예외를 감싸지 않는다 — 감싸면 가용성 분류가 사유를 못 읽는다."""
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    error = urllib.error.HTTPError("https://gemini.invalid", 429, "quota", {}, None)
    with (
        patch("app.finance.llm.client.urllib.request.urlopen", side_effect=error),
        pytest.raises(urllib.error.HTTPError) as caught,
    ):
        _planner_decide(_gemini_planner())
    assert caught.value.code == 429


def test_gemini_model_does_not_inherit_global_ollama_model(monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.delenv("FINANCE_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    assert _gemini_planner().model == "gemini-3.5-flash-lite"


def test_explicit_finance_model_overrides_provider_default(monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-explicit-model")
    assert _gemini_planner().model == "gemini-explicit-model"


def test_missing_gemini_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("FINANCE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with (
        patch("app.finance.llm.client.urllib.request.urlopen") as urlopen,
        pytest.raises(RuntimeError, match="API key is not set"),
    ):
        _planner_decide(_gemini_planner())
    urlopen.assert_not_called()


def test_gemini_planner_returns_valid_tool_action(monkeypatch):
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.finance.llm.client.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            _gemini_tool_call_document("assess_finance_position")
        ),
    )
    action = _planner_decide(_gemini_planner())
    assert action.tool_name == "assess_finance_position"
    assert action.arguments == {}
    assert action.finalize is False


@pytest.mark.parametrize(
    ("called", "missing", "exposed"),
    [
        # 남은 capability 가 있는데 종료를 부른다.
        (FINALIZE_TOOL_NAME, ("finance_position",), ("assess_finance_position",)),
        # 다 채웠는데 업무 Tool 을 부른다.
        ("assess_finance_position", (), ("assess_finance_position",)),
    ],
)
def test_gemini_rejects_invalid_finalize_tool_combinations(
    monkeypatch, called, missing, exposed
):
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.finance.llm.client.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(_gemini_tool_call_document(called)),
    )
    with pytest.raises(ValueError, match="Finance Planner must"):
        _planner_decide(_gemini_planner(), missing=missing, exposed=exposed)


@patch("app.finance.execution.save_finance_execution")
def test_gemini_planner_failure_is_error_with_fallback_metadata(save_run, monkeypatch):
    monkeypatch.delenv("FINANCE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    planner = _gemini_planner()

    reply, metadata = FinanceAgentController(object(), planner=planner).run(_request())

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert metadata.llm_status == "FALLBACK"
    assert metadata.llm_fallback_used is True
    assert metadata.llm_model == "gemini-3.5-flash-lite"
    assert metadata.llm_attempts == 1
    save_run.assert_called_once()


def test_ollama_planner_stays_on_the_local_endpoint(monkeypatch):
    """대체 Provider 도 **같은 Tool 목록**을 같은 방식으로 받는다."""
    monkeypatch.delenv("FINANCE_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    seen = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        return _Response(_ollama_tool_call_document("assess_finance_position"))

    monkeypatch.setattr("app.finance.llm.client.urllib.request.urlopen", urlopen)
    planner = _ollama_planner()
    action = _planner_decide(planner)

    assert action.tool_name == "assess_finance_position"
    assert planner.model == "gemma3:4b"
    assert seen[0][0].full_url == "http://127.0.0.1:11434/api/chat"
    body = json.loads(seen[0][0].data.decode())
    assert [item["function"]["name"] for item in body["tools"]] == [
        "assess_finance_position"
    ]


def test_controller_selects_gemini_planner_and_finalizer(monkeypatch):
    """Planner 는 LangChain tool calling 으로 돌고, **Provider 정책은 그대로다.**

    Gemini 가 Primary 이고 Gemma 가 가용성 대체다 — 프레임워크가 바뀌었다고 Primary
    가 바뀌지 않는다. Finalizer 는 계속 구조화 출력 Provider 를 그대로 쓴다.
    """
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-primary-model")
    controller = FinanceAgentController(object())
    assert isinstance(controller.planner, _AvailabilityFallbackFinancePlanner)
    assert isinstance(controller.planner.primary, LangChainFinancePlanner)
    assert isinstance(controller.planner.primary.chat_model, FinanceChatModel)
    assert controller.planner.primary.chat_model.provider == "gemini"
    assert controller.planner.fallback.chat_model.provider == "ollama"
    assert controller.planner.primary.model == "gemini-primary-model"
    # ★ 대체 **Planner** 는 tool 을 부를 수 있는 모델이어야 한다 (아래 회귀 테스트).
    #   설명(Finalizer)은 tool calling 이 아니라 기존 기본값 그대로다.
    assert controller.planner.fallback.model == _ollama_tool_calling_model()
    assert isinstance(controller.finalizer, _AvailabilityFallbackFinanceFinalizer)
    assert isinstance(controller.finalizer.primary, GeminiFinanceFinalizer)
    assert controller.finalizer.primary.model == "gemini-primary-model"
    assert controller.finalizer.fallback.model == "gemma3:4b"


# ---------------------------------------------------------------------------
# Gemini 전송 형식 — 계약은 Ollama 와 같고 표현만 Gemini 가 받는 모양이다
#
# 🔴 Gemini 가 못 받는 형태를 보내면 HTTP 400 이 난다. 재무 Planner 가 매 호출 실패하고
#    마스터가 `E4_NOT_STARTED` 로 멈춘다 — 재무가 아니라 전송 형식이 문제였던 적이 있다.
# ---------------------------------------------------------------------------


_GEMINI_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def _assert_gemini_schema_is_wire_safe(node) -> None:
    """Gemini 가 받아 주는 형태인가 (OpenAPI 3.0 부분집합).

    네 가지가 400 의 원인이다.
      · `enum` 은 STRING 에만 붙는다
      · 타입은 6종뿐이라 `{"type": "null"}` 은 없다 (`nullable` 로 표현한다)
      · `const` 는 Gemini Schema 에 없다 (한 값짜리 `enum` 으로 표현한다)
    """
    if not isinstance(node, dict):
        return
    assert "const" not in node, "const 는 Gemini Schema 에 없다"
    assert "additionalProperties" not in node, (
        "Gemini Tool Schema 는 additionalProperties 를 받지 않는다"
    )
    node_type = node.get("type")
    if node_type is not None:
        assert node_type in _GEMINI_TYPES, f"Gemini 가 모르는 타입: {node_type!r}"
    if "enum" in node:
        assert node_type == "string", f"enum 은 string 에만 붙는다 (여기는 {node_type!r})"
    for child in node.get("properties", {}).values():
        _assert_gemini_schema_is_wire_safe(child)
    for branch in node.get("anyOf", ()):
        _assert_gemini_schema_is_wire_safe(branch)
    if "items" in node:
        _assert_gemini_schema_is_wire_safe(node["items"])


def _sent_gemini_payload(monkeypatch, exposed, *, missing=("finance_position",)) -> dict:
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    seen = []

    def urlopen(request, timeout):
        del timeout
        seen.append(json.loads(request.data.decode()))
        return _Response(_gemini_tool_call_document(exposed[0]))

    monkeypatch.setattr("app.finance.llm.client.urllib.request.urlopen", urlopen)
    _planner_decide(_gemini_planner(), missing=missing, exposed=exposed)
    return seen[0]


@pytest.mark.parametrize(
    "exposed",
    [("assess_finance_position", "project_cashflow"), (FINALIZE_TOOL_NAME,)],
)
def test_gemini_tool_declarations_have_no_unsupported_form(monkeypatch, exposed):
    """조사 국면과 종료 국면 **양쪽** 전송 형식이 안전해야 한다."""
    missing = () if exposed == (FINALIZE_TOOL_NAME,) else ("finance_position",)
    payload = _sent_gemini_payload(monkeypatch, exposed, missing=missing)

    declarations = payload["tools"][0]["function_declarations"]
    assert [item["name"] for item in declarations] == list(exposed)
    for declaration in declarations:
        _assert_gemini_schema_is_wire_safe(declaration.get("parameters"))


def test_gemini_restricts_callable_functions_to_the_exposed_set(monkeypatch):
    """★ 전송 계층도 이번 단계의 Tool 만 부르게 막는다.

    이것은 **1차 방어**다. 이를 무시하는 모델이 있으므로 Planner 사후 검증과 Harness
    승인이 뒤에 그대로 남아 있다.
    """
    exposed = ("assess_finance_position", "project_cashflow")
    payload = _sent_gemini_payload(monkeypatch, exposed)

    config = payload["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == "ANY"
    assert config["allowedFunctionNames"] == list(exposed)
    assert "calculate_purchase_finance_cap" not in json.dumps(payload)


def test_gemini_amount_argument_stays_a_declared_field(monkeypatch):
    """Planner에는 금액이 노출되지 않고 실행 경계에서 source-owned로 주입된다."""
    payload = _sent_gemini_payload(
        monkeypatch,
        ("validate_amount_adjustment",),
        missing=("amount_adjustment_validation",),
    )
    declaration = payload["tools"][0]["function_declarations"][0]
    properties = declaration["parameters"]["properties"]
    assert set(properties) == {"axis"}
    _assert_gemini_schema_is_wire_safe(declaration["parameters"])
    # 🔴 표현만 낮춘 것이지 계약은 그대로다 — 금액 축 하나만 부를 수 있다.
    assert properties["axis"]["enum"] == ["amount"]


# ---------------------------------------------------------------------------
# 전송 강제를 믿지 않는다 — 사후 검증이 그대로 잡는가
# ---------------------------------------------------------------------------


def _decide_with(monkeypatch, called, *, missing, exposed=("assess_finance_position",)):
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.finance.llm.client.urllib.request.urlopen",
        lambda *_a, **_k: _Response(_gemini_tool_call_document(called)),
    )
    return _planner_decide(_gemini_planner(), missing=missing, exposed=exposed)


def test_finalizing_while_capabilities_missing_is_rejected(monkeypatch):
    """전송 계층이 더는 막지 않으므로 **여기서** 잡아야 한다."""
    with pytest.raises(FinancePlannerContractViolation):
        _decide_with(monkeypatch, FINALIZE_TOOL_NAME, missing=("finance_position",))


def test_selecting_a_tool_after_capabilities_complete_is_rejected(monkeypatch):
    with pytest.raises(FinancePlannerContractViolation):
        _decide_with(monkeypatch, "assess_finance_position", missing=())


def test_disallowed_tool_is_still_rejected(monkeypatch):
    """노출하지 않은 Tool 이름을 골라도 Planner 계약에서 걸린다."""
    with pytest.raises(FinancePlannerContractViolation):
        _decide_with(monkeypatch, "calculate_purchase_finance_cap", missing=("finance_cap",))


def test_free_text_answer_is_rejected(monkeypatch):
    """Tool 을 하나도 부르지 않은 답은 계약 위반이다 — 자유 문장으로 끝낼 수 없다."""
    with pytest.raises(FinancePlannerContractViolation):
        _decide_with(monkeypatch, None, missing=("finance_position",))


def test_valid_finalization_is_accepted(monkeypatch):
    action = _decide_with(
        monkeypatch, FINALIZE_TOOL_NAME, missing=(), exposed=(FINALIZE_TOOL_NAME,)
    )
    assert action.finalize is True
    assert action.tool_name is None


# ---------------------------------------------------------------------------
# Provider 대체 Planner 의 모델 — **대체가 필요한 날에 같이 죽지 않아야 한다**
# ---------------------------------------------------------------------------

def test_ollama_fallback_planner_uses_a_tool_calling_model(monkeypatch):
    """🔴 대체 Planner 가 tool 을 못 부르는 모델로 만들어지고 있었다.

    Gemini 가 429 로 접힌 날 Ollama 대체가 `does not support tools` 로 같이 죽어,
    **대체가 필요한 순간에만 대체가 없었다.** 설정으로는 고칠 수 없는 자리였다 —
    대체 Planner 는 모델 이름을 코드에서 직접 든다.
    """
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-3.5-flash-lite")
    planner, _finalizer, state = _configured_finance_llms()
    assert state is not None and state.primary_provider == "gemini"
    fallback_model = planner.fallback.model
    assert fallback_model == _ollama_tool_calling_model()
    assert fallback_model == _DEFAULT_OLLAMA_TOOL_CALLING_MODEL
    # ★ 재무 설정(Gemini 모델 이름)이 대체 Planner로 새지 않는다.
    assert fallback_model != "gemini-3.5-flash-lite"


def test_ollama_planner_default_does_not_inherit_the_global_interpretation_model(
    monkeypatch,
):
    """전역 `LLM_MODEL` 은 tool 을 부르지 않는 레거시 해석 계층의 값이다."""
    monkeypatch.delenv("FINANCE_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    model = finance_planner_model("ollama")
    assert model == _ollama_tool_calling_model()
    assert model == _DEFAULT_OLLAMA_TOOL_CALLING_MODEL


def test_explicit_finance_model_still_wins_for_the_ollama_planner(monkeypatch):
    """운영자가 고른 모델을 우리가 덮지 않는다."""
    monkeypatch.setenv("FINANCE_LLM_MODEL", "operator-choice:8b")
    assert finance_planner_model("ollama") == "operator-choice:8b"
    assert finance_planner_model("gemini") == "operator-choice:8b"


def test_finalizer_keeps_the_non_tool_calling_default(monkeypatch):
    """★ 설명은 tool calling 이 아니다 — 기본값을 같이 바꾸지 않았다."""
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    _planner, finalizer, _state = _configured_finance_llms()
    assert finalizer.fallback.model == _DEFAULT_MODELS["ollama"]


def test_declared_ollama_planner_default_is_separate_from_finalizer_default():
    """모델명 휴리스틱이 아니라 설정 배선만 고정한다."""
    assert _DEFAULT_OLLAMA_TOOL_CALLING_MODEL != _DEFAULT_MODELS["ollama"]


def test_ollama_planner_model_is_operator_overridable(monkeypatch):
    """설치된 모델은 배포마다 다르다 — 코드를 고치지 않고 바꿀 수 있어야 한다."""
    monkeypatch.setenv("FINANCE_OLLAMA_PLANNER_MODEL", "local-tools:latest")
    assert _ollama_tool_calling_model() == "local-tools:latest"
