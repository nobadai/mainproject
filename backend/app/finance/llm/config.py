"""Finance LLM 설정 — **재무 키로만 정해진다.**

전역 `LLM_PROVIDER` 를 상속하지 않는다. 전역은 레거시 Ollama 해석 계층이 쓰는
값이고, 그것을 상속하면 전역을 ollama 로 둔 배포에서 재무 Agent 가 **조용히 Gemini 를
떠난다** — 값은 멀쩡히 나오고 아무도 눈치채지 못한다.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_DEFAULT_MODELS = {
    "ollama": "gemma3:4b",
    "gemini": "gemini-3.5-flash-lite",
}
_ENV_FILES = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
)


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
