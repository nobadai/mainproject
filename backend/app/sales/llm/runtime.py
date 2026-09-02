"""Sales 후보 해석을 Gemini 구조화 출력으로 연결한다."""

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.sales.llm.schemas import CandidateInterpretationInput, LlmInterpretationOutput
from app.sales.schemas import SalesCandidate, SalesRecommendation

_NUMBER = re.compile(r"\d")
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_SYSTEM_PROMPT = """Sales 후보 중 하나만 추천하세요. 후보 ID 외 숫자·금액·수량·날짜를 쓰지 말고,
새 후보나 조건을 만들지 마세요. 모든 문장은 자연스러운 한국어로 작성하세요."""
_ENV_FILES = (
    Path(__file__).resolve().parents[3] / ".env",
    Path(__file__).resolve().parents[4] / ".env",
)


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    provider: str
    model: str
    timeout_seconds: float


def load_settings() -> LLMSettings:
    """Sales 전용 설정을 우선하고 전역 Ollama 설정이 모델로 섞이지 않게 한다."""
    _load_environment()
    enabled = _read_bool("SALES_LLM_ENABLED")
    if enabled is None:
        enabled = _read_bool("LLM_ENABLED")
    provider = (os.getenv("SALES_LLM_PROVIDER") or "gemini").strip().lower()
    explicit_model = os.getenv("SALES_LLM_MODEL")
    model = explicit_model or _DEFAULT_MODEL
    return LLMSettings(
        enabled=False if enabled is None else enabled,
        provider=provider,
        model=model,
        timeout_seconds=max(0.1, float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))),
    )


def interpret_candidates(candidates: list[SalesCandidate]) -> SalesRecommendation:
    """Gemini 실패는 Scenario를 바꾸지 않고 결정론 fallback으로만 전환한다."""
    settings = load_settings()
    if not candidates:
        return _fallback(candidates, "SKIPPED_TEMPLATE", settings, 0)
    if not settings.enabled:
        return _fallback(candidates, "DISABLED", settings, 0)
    try:
        output = _call_gemini(_safe_context(candidates), settings)
        return _validated(candidates, output, settings)
    except Exception:  # noqa: BLE001 - 외부 호출 실패는 Sales 제안 실패가 아니다.
        return _fallback(candidates, "FALLBACK", settings, 1)


def _safe_context(candidates: list[SalesCandidate]) -> list[CandidateInterpretationInput]:
    """LLM에는 수량·가격·날짜를 전달하지 않고 의미 라벨만 전달한다."""
    return [
        CandidateInterpretationInput(
            candidate_id=c.candidate_id,
            strategy_label=c.strategy_label,
            adjustment_axis=c.adjustment_axis,
            conditional=c.conditional,
            risk_labels=c.risks,
            uncertainty_labels=c.uncertainties,
        )
        for c in candidates
    ]


def _call_gemini(context: list[CandidateInterpretationInput], settings: LLMSettings):
    """Gemini의 JSON Schema 응답을 받아 Sales 전용 계약으로 검증한다."""
    if settings.provider != "gemini":
        raise RuntimeError("unsupported Sales LLM provider")
    api_key = os.getenv("SALES_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Sales Gemini API key is not set")
    context_json = json.dumps([c.model_dump() for c in context], ensure_ascii=False)
    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": context_json}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_safe_schema(LlmInterpretationOutput.model_json_schema()),
        },
    }
    request = urllib.request.Request(
        f"{_GEMINI_BASE_URL}/models/{settings.model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
        document = json.loads(response.read().decode("utf-8"))
    return LlmInterpretationOutput.model_validate_json(_gemini_response_text(document))


def _gemini_response_text(document: dict[str, Any]) -> str:
    candidates = document.get("candidates") or []
    parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    raise ValueError("empty Gemini response")


def _gemini_safe_schema(node: Any) -> Any:
    """Pydantic Schema를 Gemini가 받는 표현으로만 낮추고 계약 의미는 유지한다."""
    if not isinstance(node, dict):
        return node
    excluded = {"const", "anyOf", "additionalProperties"}
    safe = {key: value for key, value in node.items() if key not in excluded}
    if "const" in node:
        safe.update({"type": "string", "enum": [node["const"]]})
    if "anyOf" in node:
        branches = [b for b in node["anyOf"] if isinstance(b, dict) and b.get("type") != "null"]
        if len(branches) != len(node["anyOf"]):
            safe["nullable"] = True
        if len(branches) == 1:
            safe.update(_gemini_safe_schema(branches[0]))
        elif branches:
            safe["anyOf"] = [_gemini_safe_schema(branch) for branch in branches]
    if "properties" in node:
        safe["properties"] = {
            name: _gemini_safe_schema(child) for name, child in node["properties"].items()
        }
    if "items" in node:
        safe["items"] = _gemini_safe_schema(node["items"])
    return safe


def _validated(candidates, output, settings) -> SalesRecommendation:
    """후보 ID·빈 문장·숫자 포함 여부를 검사해 LLM의 권한을 제한한다."""
    selectable = {c.candidate_id for c in candidates if "FINANCE_FAIL" not in c.risks}
    if output.recommended_candidate_id not in selectable:
        raise ValueError("unknown candidate")
    texts = (
        output.summary,
        output.recommendation_reason,
        output.risk_explanation,
        output.user_message,
    )
    if any(not text.strip() or _NUMBER.search(text) for text in texts):
        raise ValueError("unsafe LLM output")
    return SalesRecommendation(
        status="SUCCESS", recommended_candidate_id=output.recommended_candidate_id,
        summary=output.summary, recommendation_reason=output.recommendation_reason,
        risk_explanation=output.risk_explanation, user_message=output.user_message,
        llm_provider="gemini", llm_model=settings.model, llm_attempts=1, llm_fallback_used=False,
    )


def _fallback(candidates, status, settings, attempts) -> SalesRecommendation:
    selectable = [c for c in candidates if "FINANCE_FAIL" not in c.risks]
    candidate = next((c for c in selectable if not c.conditional), None)
    if candidate is None and selectable:
        candidate = selectable[0]
    return SalesRecommendation(
        status=status, recommended_candidate_id=candidate.candidate_id if candidate else None,
        summary="규칙 기반 판매안을 준비했습니다.",
        recommendation_reason="외부 해석 없이 근거가 있는 판매 조건을 우선 표시합니다.",
        risk_explanation="외부 검증 결과와 조건부 조달 여부를 함께 확인해 주세요.",
        user_message="현재 확인된 조건을 기준으로 판매안을 검토해 주세요.",
        llm_provider=settings.provider if settings.enabled else None, llm_model=settings.model,
        llm_attempts=attempts, llm_fallback_used=status == "FALLBACK",
    )


def _load_environment() -> None:
    for env_file in _ENV_FILES:
        load_dotenv(env_file, override=False)


def _read_bool(name: str) -> bool | None:
    value = os.getenv(name)
    return None if value is None else value.strip().lower() in {"1", "true", "yes", "on"}
