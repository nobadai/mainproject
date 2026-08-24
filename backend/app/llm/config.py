"""Environment-backed Local LLM settings."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int


def get_llm_settings() -> LLMSettings:
    """Read the optional interpretation runtime configuration."""
    load_dotenv(_ENV_FILE)
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=os.getenv("LLM_PROVIDER", "ollama").strip().lower(),
        model=os.getenv("LLM_MODEL", "gemma3:4b").strip(),
        base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        timeout_seconds=max(0.1, float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))),
        max_retries=min(1, max(0, int(os.getenv("LLM_MAX_RETRIES", "1")))),
    )


def _read_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
