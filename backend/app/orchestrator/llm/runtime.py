"""Orchestrator-owned Ollama provider, policy, validator, retry and fallback runtime.

SYSTEM_PROMPT 와 환각 방지 검사는 `selector_llm.py`(설계 원본)에서 옮겨왔다.
런타임 골격(설정·재시도·상태 결정 순서)은 Finance / Logistics 와 동일하다.
"""

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv
from pydantic import ValidationError

from app.orchestrator.llm.schemas import (
    InterpretationResult,
    LLMStatus,
    SanitizedLLMContext,
    SelectionInterpretation,
)

_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"
# 에이전트 전용 설정 접두사 — `ORCHESTRATOR_LLM_MODEL` 이 있으면 공통 `LLM_MODEL` 을 덮는다.
_ENV_PREFIX = "ORCHESTRATOR_"
_NUMERIC_PATTERN = re.compile(r"\d")
_SENTENCE_SPLIT = re.compile(r"[.!?。]+")
_MAX_SUMMARY_CHARACTERS = 240
_MAX_RATIONALE_CHARACTERS = 300

SYSTEM_PROMPT = """당신은 농산물 유통회사 햇들농산 오케스트레이터의 선정 레이어다.
입력 Context는 deterministic Core의 밴드 결합과 클리핑을 통과했다.
계산기나 결정 엔진이 아니며 후보를 정렬하고 질적 설명만 작성한다.

규칙:
- 수량, 금액, 단가, 비율, 날짜를 새로 계산하거나 출력하지 않는다.
- candidates에 없는 scenario_id를 만들지 않는다.
- 모든 후보를 정확히 한 번 포함한다.
- rationale_per_id는 ranked_scenario_ids의 모든 id에 대해 작성한다.
- rationale은 binding_constraints와 facts에 실제로 근거해야 한다.
- 부서 간 신호가 충돌하면 해결하지 말고 conflict_note에 서술만 한다.
- 충돌이 없으면 conflict_note는 null이다.
- summary는 최대 두 문장으로 작성하고 반복하지 않는다.
- 모든 문장은 한국어로 작성한다.
- FULL, MINOR_CLIP, MAJOR_CLIP 같은 내부 라벨을 문장에 그대로 쓰지 않는다.
  각각 '전량 반영', '일부 축소', '대폭 축소'처럼 사람이 읽을 말로 바꾼다.
- 서로 다른 사실을 인과로 엮지 않는다. 근거가 없으면 원인을 지어내지 않는다.
- 지정된 JSON Schema에 맞는 JSON만 출력한다.

정렬 기준은 "사람이 먼저 검토할 가치가 있는 순서"다.
가장 공격적인 안이 1순위인 것은 아니다. 최종 결정은 사람이 한다."""


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int


class LLMProvider(Protocol):
    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str: ...


class OllamaProvider:
    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        user_payload: dict[str, object] = {"context": context.model_dump(mode="json")}
        if retry_guidance:
            user_payload["correction"] = retry_guidance
        payload = {
            "model": self.settings.model,
            "stream": False,
            "think": False,
            "format": SelectionInterpretation.model_json_schema(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "options": {"temperature": 0, "num_ctx": 4096},
        }
        request = urllib.request.Request(
            f"{self.settings.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError("Orchestrator Local LLM request failed") from error
        content = (document.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise TypeError("Orchestrator Local LLM response did not contain message content")
        return content


class UnavailableProvider:
    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        del context, retry_guidance
        raise RuntimeError("Configured Orchestrator LLM provider is not supported")


class ValidationIssue(StrEnum):
    INVALID_SCHEMA = "INVALID_SCHEMA"
    UNKNOWN_SCENARIO_ID = "UNKNOWN_SCENARIO_ID"
    NUMERIC_OUTPUT_FORBIDDEN = "NUMERIC_OUTPUT_FORBIDDEN"
    RATIONALE_MISSING = "RATIONALE_MISSING"
    UNSUPPORTED_RATIONALE = "UNSUPPORTED_RATIONALE"
    RATIONALE_TOO_LONG = "RATIONALE_TOO_LONG"
    SUMMARY_TOO_LONG = "SUMMARY_TOO_LONG"
    TOO_MANY_SENTENCES = "TOO_MANY_SENTENCES"
    REPETITIVE_OUTPUT = "REPETITIVE_OUTPUT"


class SelectionValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        super().__init__(", ".join(issues))
        self.issues = issues


class SelectionService:
    def __init__(self, settings: LLMSettings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def select(
        self,
        context: SanitizedLLMContext,
        *,
        runtime_ready: bool,
        deterministic_ranking: list[str],
    ) -> InterpretationResult:
        """상태 결정 순서는 Finance / Logistics 런타임과 동일하다.

        DISABLED → SKIPPED_TEMPLATE → SUCCESS → FALLBACK.
        어느 경로로 끝나든 Core 가 확정한 밴드·클리핑은 그대로 살아 있다.
        """
        template = build_template_selection(context, deterministic_ranking)
        if not self.settings.enabled:
            return self._result(template, status="DISABLED", attempts=0, fallback=False)
        if not needs_llm(context, runtime_ready=runtime_ready):
            return self._result(template, status="SKIPPED_TEMPLATE", attempts=0, fallback=False)

        guidance = None
        attempts = 0
        for _ in range(self.settings.max_retries + 1):
            attempts += 1
            try:
                raw_output = self.provider.generate(context, retry_guidance=guidance)
                interpretation = validate_selection(raw_output, context)
                return self._result(
                    interpretation,
                    status="SUCCESS",
                    attempts=attempts,
                    fallback=False,
                )
            except SelectionValidationError as error:
                guidance = retry_guidance(error.issues)
            except Exception:  # noqa: BLE001 - optional LLM cannot fail Orchestrator Core.
                guidance = ["지정된 규칙과 JSON 형식에 맞춰 다시 작성하세요."]
        return self._result(template, status="FALLBACK", attempts=attempts, fallback=True)

    def _result(
        self,
        interpretation: SelectionInterpretation,
        *,
        status: LLMStatus,
        attempts: int,
        fallback: bool,
    ) -> InterpretationResult:
        return InterpretationResult(
            interpretation=interpretation,
            llm_status=status,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model,
            llm_attempts=attempts,
            llm_fallback_used=fallback,
        )


def get_llm_settings() -> LLMSettings:
    """에이전트 전용 설정 → 공통 설정 → 기본값 순으로 읽는다.

    `ORCHESTRATOR_LLM_MODEL` 을 두면 오케만 다른 모델을 쓴다. 없으면 공통 `LLM_MODEL`.
    Critic judge 를 생성 측과 다른 모델로 돌리기 위한 장치다 (설계서 §6.4).
    """
    load_dotenv(_ENV_FILE)
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=_env("LLM_PROVIDER", "ollama").strip().lower(),
        model=_env("LLM_MODEL", "gemma3:4b").strip(),
        base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        timeout_seconds=max(0.1, float(_env("LLM_TIMEOUT_SECONDS", "30"))),
        max_retries=min(1, max(0, int(_env("LLM_MAX_RETRIES", "1")))),
    )


def get_selection_service() -> SelectionService:
    settings = get_llm_settings()
    provider: LLMProvider = (
        OllamaProvider(settings) if settings.provider == "ollama" else UnavailableProvider()
    )
    return SelectionService(settings, provider)


def needs_llm(context: SanitizedLLMContext, *, runtime_ready: bool) -> bool:
    """진짜 선택지가 있을 때만 LLM 을 부른다.

    후보가 1개 이하면 정렬할 것이 없다 — `node_t3_select` 의 `len(feasible) > 1` 과 같은 판단이다.
    """
    if not runtime_ready:
        return False
    return len(context.candidates) > 1


def validate_selection(
    raw_output: str,
    context: SanitizedLLMContext,
) -> SelectionInterpretation:
    """환각 id 는 즉시 실패, 중복·누락은 자동 보정한다.

    ★ 존재하지 않는 scenario_id 는 되살릴 방법이 없으므로 재시도로 보낸다.
      순서 중복·누락은 코드가 고칠 수 있으므로 고쳐서 통과시킨다 (원본 selector_llm 과 동일).
    """
    try:
        interpretation = SelectionInterpretation.model_validate_json(raw_output)
    except ValidationError as error:
        raise SelectionValidationError([ValidationIssue.INVALID_SCHEMA]) from error

    valid_ids = [candidate.scenario_id for candidate in context.candidates]
    unknown = [i for i in interpretation.ranked_scenario_ids if i not in valid_ids]
    if unknown:
        raise SelectionValidationError([ValidationIssue.UNKNOWN_SCENARIO_ID])

    # 중복 제거 → 누락분을 결정론 순서대로 뒤에 붙인다.
    ordered = list(dict.fromkeys(interpretation.ranked_scenario_ids))
    ordered += [i for i in valid_ids if i not in ordered]
    interpretation = interpretation.model_copy(update={"ranked_scenario_ids": ordered})

    issues = _validation_issues(interpretation, context)
    if issues:
        raise SelectionValidationError(issues)
    return interpretation


def _validation_issues(
    interpretation: SelectionInterpretation,
    context: SanitizedLLMContext,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    valid_ids = {candidate.scenario_id for candidate in context.candidates}

    # 숫자 금지 검사 — scenario_id 자체는 숫자를 포함할 수 있으므로 먼저 지우고 본다.
    prose = " ".join(
        [interpretation.summary, *interpretation.rationale_per_id.values()]
        + ([interpretation.conflict_note] if interpretation.conflict_note else [])
    )
    for scenario_id in valid_ids:
        prose = prose.replace(scenario_id, " ")
    if _NUMERIC_PATTERN.search(prose):
        issues.append(ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN)

    rationale_ids = set(interpretation.rationale_per_id)
    if set(interpretation.ranked_scenario_ids) - rationale_ids:
        issues.append(ValidationIssue.RATIONALE_MISSING)
    if rationale_ids - valid_ids:
        issues.append(ValidationIssue.UNSUPPORTED_RATIONALE)
    if any(
        len(text) > _MAX_RATIONALE_CHARACTERS for text in interpretation.rationale_per_id.values()
    ):
        issues.append(ValidationIssue.RATIONALE_TOO_LONG)

    if len(interpretation.summary) > _MAX_SUMMARY_CHARACTERS:
        issues.append(ValidationIssue.SUMMARY_TOO_LONG)
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT.split(interpretation.summary)
        if sentence.strip()
    ]
    if len(sentences) > 2:
        issues.append(ValidationIssue.TOO_MANY_SENTENCES)
    normalized = [" ".join(sentence.split()) for sentence in sentences]
    if len(normalized) != len(set(normalized)):
        issues.append(ValidationIssue.REPETITIVE_OUTPUT)
    return issues


def retry_guidance(issues: list[ValidationIssue]) -> list[str]:
    guidance = []
    if ValidationIssue.INVALID_SCHEMA in issues:
        guidance.append("지정된 네 필드만 포함한 유효한 JSON을 작성하세요.")
    if ValidationIssue.UNKNOWN_SCENARIO_ID in issues:
        guidance.append("candidates에 있는 scenario_id만 사용하세요.")
    if ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in issues:
        guidance.append("설명 문장에 숫자와 날짜를 사용하지 마세요.")
    if ValidationIssue.RATIONALE_MISSING in issues:
        guidance.append("정렬한 모든 id에 대해 rationale_per_id를 작성하세요.")
    if ValidationIssue.UNSUPPORTED_RATIONALE in issues:
        guidance.append("candidates에 없는 id의 rationale을 만들지 마세요.")
    if ValidationIssue.RATIONALE_TOO_LONG in issues:
        guidance.append("각 rationale을 짧게 작성하세요.")
    if ValidationIssue.SUMMARY_TOO_LONG in issues or ValidationIssue.TOO_MANY_SENTENCES in issues:
        guidance.append("summary를 짧은 두 문장 이내로 작성하세요.")
    if ValidationIssue.REPETITIVE_OUTPUT in issues:
        guidance.append("같은 내용을 반복하지 마세요.")
    return guidance


def build_template_selection(
    context: SanitizedLLMContext,
    deterministic_ranking: list[str],
) -> SelectionInterpretation:
    """LLM 을 못 쓸 때의 기본값 — 결정론 정렬을 그대로 쓴다.

    ★ Finance 는 템플릿 '문장'으로 되돌아가지만, 오케는 되돌아갈 곳이 **결정론 순위**다.
      숫자는 어차피 Core 가 확정했으므로 LLM 이 없어도 결과는 완전하다.
    """
    ranking = deterministic_ranking or [c.scenario_id for c in context.candidates]
    summary = (
        " ".join(context.facts[:2])
        if context.facts
        else "결정론적 정렬 순서를 유지합니다. 별도 선정 근거가 생성되지 않았습니다."
    )
    return SelectionInterpretation(
        summary=summary,
        ranked_scenario_ids=list(ranking),
        rationale_per_id={},
        conflict_note=None,
    )


def _env(key: str, default: str) -> str:
    """`ORCHESTRATOR_<KEY>` → `<KEY>` → default. 빈 문자열은 미설정으로 본다."""
    return os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key) or default


def _read_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
