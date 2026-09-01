from __future__ import annotations

import json
import os
import urllib.error
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.agent import FinanceAgentController, ToolAction
from app.finance.llm.client import (
    _gemini_availability_failure_reason,
    _gemini_response_text,
    _is_gemini_availability_failure,
)
from app.finance.llm.config import (
    _finance_model,
    _finance_provider_name,
    _load_finance_environment,
)
from app.finance.llm.finalizer import GeminiFinanceFinalizer, OllamaFinanceFinalizer
from app.finance.llm.planner import GeminiFinancePlanner, OllamaFinancePlanner
from app.finance.llm.provider import (
    _AvailabilityFallbackFinanceFinalizer,
    _AvailabilityFallbackFinancePlanner,
    _ProviderFallbackState,
)
from app.finance.repository import FinanceDataNotReady
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
    monkeypatch.setattr("app.finance.llm.config._load_finance_environment", lambda: None)


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
    parts = []
    if thought_first:
        parts.append({"thought": True, "text": "internal reasoning"})
    parts.append({"text": json.dumps(content)})
    return {"candidates": [{"content": {"parts": parts}}]}


def _planner_decide(planner: GeminiFinancePlanner, *, missing=("finance_position",)):
    return planner.decide(
        request=_request(),
        allowed_tools=frozenset({"assess_finance_position"}),
        observations=(),
        missing_capabilities=missing,
    )


def _mock_successful_pre_purchase(monkeypatch, planner_type, finalizer_type):
    actions = iter(
        [
            ToolAction("assess_finance_position"),
            ToolAction("analyze_payment_pressure"),
            ToolAction("calculate_purchase_finance_cap"),
            ToolAction(finalize=True),
        ]
    )

    def decide(planner, **_kwargs):
        planner.attempts += 1
        return next(actions)

    def finalize(finalizer, **_kwargs):
        finalizer.attempts += 1
        return "Verified Finance Evidence supports the reported purchasing boundary."

    monkeypatch.setattr(planner_type, "decide", decide)
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
    monkeypatch.setattr("app.finance.llm.config._ENV_FILES", (env_file,))
    monkeypatch.setattr(
        "app.finance.llm.config._load_finance_environment", _load_finance_environment
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
    monkeypatch.setattr("app.finance.llm.config._ENV_FILES", (env_file,))
    monkeypatch.setattr(
        "app.finance.llm.config._load_finance_environment", _load_finance_environment
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


@patch("app.finance.agent.save_finance_execution")
def test_configured_gemini_unavailable_uses_observable_ollama_provider_fallback(
    save_run, monkeypatch
):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-primary-model")
    monkeypatch.delenv("FINANCE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _mock_successful_pre_purchase(
        monkeypatch, OllamaFinancePlanner, OllamaFinanceFinalizer
    )
    with patch("app.finance.llm.client.urllib.request.urlopen") as urlopen:
        controller = FinanceAgentController(_FinancePort())
        reply, metadata = controller.run(_request())

    assert reply.runtime_status == "READY"
    assert metadata.llm_status == "SUCCESS"
    assert metadata.llm_fallback_used is False
    assert metadata.llm_model == "gemma3:4b"
    assert metadata.llm_attempts == 6
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


@patch("app.finance.agent.save_finance_execution")
def test_normal_gemini_provider_observation_is_distinct(save_run, monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-primary-model")
    _mock_successful_pre_purchase(
        monkeypatch, GeminiFinancePlanner, GeminiFinanceFinalizer
    )

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


@patch("app.finance.agent.save_finance_execution")
def test_explicit_ollama_provider_observation_is_distinct(save_run, monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemma3:4b")
    _mock_successful_pre_purchase(
        monkeypatch, OllamaFinancePlanner, OllamaFinanceFinalizer
    )

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
        return _Response(
            _gemini_document(
                {
                    "tool_name": "assess_finance_position",
                    "arguments": {},
                    "reason": "Read Finance position.",
                    "finalize": False,
                }
            )
        )

    monkeypatch.setattr("app.finance.llm.client.urllib.request.urlopen", urlopen)
    action = _planner_decide(GeminiFinancePlanner())

    assert action.tool_name == "assess_finance_position"
    assert seen[0][0].full_url.startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/"
    )
    assert "127.0.0.1:11434" not in seen[0][0].full_url
    schema = json.loads(seen[0][0].data)["generationConfig"]["responseSchema"]
    # Gemini 도 Ollama 와 같은 제약을 받는다 — 이번 호출의 allowed_tools 가 enum 이고,
    # missing capability 가 있으므로 finalize 는 False 만 낼 수 있다 (§14).
    assert schema["properties"]["finalize"]["enum"] == [False]
    assert schema["properties"]["tool_name"]["enum"] == ["assess_finance_position"]


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
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    error = urllib.error.HTTPError("https://gemini.invalid", 429, "quota", {}, None)
    with (
        patch("app.finance.llm.client.urllib.request.urlopen", side_effect=error),
        pytest.raises(urllib.error.HTTPError) as caught,
    ):
        _planner_decide(GeminiFinancePlanner())
    assert caught.value.code == 429


def test_gemini_model_does_not_inherit_global_ollama_model(monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.delenv("FINANCE_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    assert GeminiFinancePlanner().model == "gemini-3.5-flash-lite"


def test_explicit_finance_model_overrides_provider_default(monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-explicit-model")
    assert GeminiFinancePlanner().model == "gemini-explicit-model"


def test_missing_gemini_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("FINANCE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with (
        patch("app.finance.llm.client.urllib.request.urlopen") as urlopen,
        pytest.raises(RuntimeError, match="API key is not set"),
    ):
        _planner_decide(GeminiFinancePlanner())
    urlopen.assert_not_called()


def test_gemini_planner_returns_valid_tool_action(monkeypatch):
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    document = _gemini_document(
        {
            "tool_name": "assess_finance_position",
            "arguments": {},
            "reason": "Use an allowed Finance Tool.",
            "finalize": False,
        },
        thought_first=True,
    )
    monkeypatch.setattr(
        "app.finance.llm.client.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(document),
    )
    action = _planner_decide(GeminiFinancePlanner())
    assert action == ToolAction(
        "assess_finance_position", {}, "Use an allowed Finance Tool.", False
    )


@pytest.mark.parametrize(
    ("content", "missing"),
    [
        (
            {"tool_name": None, "arguments": {}, "reason": "Done.", "finalize": True},
            ("finance_position",),
        ),
        (
            {
                "tool_name": "assess_finance_position",
                "arguments": {},
                "reason": "Call Tool.",
                "finalize": False,
            },
            (),
        ),
    ],
)
def test_gemini_rejects_invalid_finalize_tool_combinations(monkeypatch, content, missing):
    monkeypatch.setenv("FINANCE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.finance.llm.client.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(_gemini_document(content)),
    )
    with pytest.raises(ValueError, match="Finance Planner must"):
        _planner_decide(GeminiFinancePlanner(), missing=missing)


@patch("app.finance.agent.save_finance_execution")
def test_gemini_planner_failure_is_error_with_fallback_metadata(save_run, monkeypatch):
    monkeypatch.delenv("FINANCE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    planner = GeminiFinancePlanner()

    reply, metadata = FinanceAgentController(object(), planner=planner).run(_request())

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert metadata.llm_status == "FALLBACK"
    assert metadata.llm_fallback_used is True
    assert metadata.llm_model == "gemini-3.5-flash-lite"
    assert metadata.llm_attempts == 1
    save_run.assert_called_once()


def test_ollama_planner_remains_backward_compatible(monkeypatch):
    monkeypatch.delenv("FINANCE_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    seen = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        return _Response(
            {
                "message": {
                    "content": json.dumps(
                        {
                            "tool_name": "assess_finance_position",
                            "arguments": {},
                            "reason": "Read Finance position.",
                            "finalize": False,
                        }
                    )
                }
            }
        )

    monkeypatch.setattr("app.finance.llm.client.urllib.request.urlopen", urlopen)
    planner = OllamaFinancePlanner()
    action = planner.decide(
        request=_request(),
        allowed_tools=frozenset({"assess_finance_position"}),
        observations=(),
        missing_capabilities=("finance_position",),
    )

    assert action.tool_name == "assess_finance_position"
    assert planner.model == "gemma3:4b"
    assert seen[0][0].full_url == "http://127.0.0.1:11434/api/chat"


def test_controller_selects_gemini_planner_and_finalizer(monkeypatch):
    monkeypatch.setenv("FINANCE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("FINANCE_LLM_MODEL", "gemini-primary-model")
    controller = FinanceAgentController(object())
    assert isinstance(controller.planner, _AvailabilityFallbackFinancePlanner)
    assert isinstance(controller.planner.primary, GeminiFinancePlanner)
    assert controller.planner.primary.model == "gemini-primary-model"
    assert controller.planner.fallback.model == "gemma3:4b"
    assert isinstance(controller.finalizer, _AvailabilityFallbackFinanceFinalizer)
    assert isinstance(controller.finalizer.primary, GeminiFinanceFinalizer)
    assert controller.finalizer.primary.model == "gemini-primary-model"
    assert controller.finalizer.fallback.model == "gemma3:4b"
