from __future__ import annotations

import json
import os
import urllib.error
from datetime import date
from unittest.mock import patch

import pytest

from app.finance.agent import (
    FinanceAgentController,
    GeminiFinanceFinalizer,
    GeminiFinancePlanner,
    OllamaFinancePlanner,
    ToolAction,
    _finance_model,
    _finance_provider_name,
    _gemini_response_text,
    _load_finance_environment,
)
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


@pytest.fixture(autouse=True)
def _prevent_real_finance_env_loading(monkeypatch):
    monkeypatch.setattr("app.finance.agent._load_finance_environment", lambda: None)


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
    monkeypatch.setattr("app.finance.agent._ENV_FILES", (env_file,))
    monkeypatch.setattr(
        "app.finance.agent._load_finance_environment", _load_finance_environment
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
    monkeypatch.setattr("app.finance.agent._ENV_FILES", (env_file,))
    monkeypatch.setattr(
        "app.finance.agent._load_finance_environment", _load_finance_environment
    )

    process_environment = {
        "FINANCE_LLM_PROVIDER": "ollama",
        "FINANCE_LLM_MODEL": "process-model",
    }
    with patch.dict(os.environ, process_environment, clear=True):
        provider = _finance_provider_name()

        assert provider == "ollama"
        assert _finance_model(provider) == "process-model"


def test_finance_provider_inherits_global_provider(monkeypatch):
    monkeypatch.delenv("FINANCE_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    assert _finance_provider_name() == "gemini"


def test_finance_provider_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("FINANCE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    assert _finance_provider_name() == "ollama"


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

    monkeypatch.setattr("app.finance.agent.urllib.request.urlopen", urlopen)
    action = _planner_decide(GeminiFinancePlanner())

    assert action.tool_name == "assess_finance_position"
    assert seen[0][0].full_url.startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/"
    )
    assert "127.0.0.1:11434" not in seen[0][0].full_url
    schema = json.loads(seen[0][0].data)["generationConfig"]["responseSchema"]
    assert "enum" not in schema["properties"]["finalize"]


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
        patch("app.finance.agent.urllib.request.urlopen", side_effect=error),
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
        patch("app.finance.agent.urllib.request.urlopen") as urlopen,
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
        "app.finance.agent.urllib.request.urlopen", lambda *_args, **_kwargs: _Response(document)
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
        "app.finance.agent.urllib.request.urlopen",
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

    monkeypatch.setattr("app.finance.agent.urllib.request.urlopen", urlopen)
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
    controller = FinanceAgentController(object())
    assert isinstance(controller.planner, GeminiFinancePlanner)
    assert isinstance(controller.finalizer, GeminiFinanceFinalizer)
