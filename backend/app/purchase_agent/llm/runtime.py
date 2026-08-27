"""프로바이더 · 검증 · 재시도 · fallback (팀 규약 준수 — finance/critic 런타임과 같은 배치).

**노드는 이 모듈을 직접 부르지 않는다** — ``mix.make_mix_selector()``만 안다.

프로바이더는 3종이고 ``LLM_PROVIDER``로 고른다. **팀의 환경변수 규약이 이미 프로바이더
중립**이라 이름을 새로 만들지 않았다:

* ``anthropic`` — Messages API + 구조화 출력(``output_config.format``)
* ``openai``    — Chat Completions + ``response_format`` json_schema(strict)
* ``ollama``    — 팀 기존 4벌과 같은 로컬 경로 (``format``에 JSON Schema)

**검증 체인은 프로바이더 밖에 있다.** 프로바이더는 "문자열을 받아온다"까지만 하고,
후보 대조·숫자 금지·재시도는 ``MixSelectionService``가 소유한다 — 프로바이더를 갈아끼워도
판정이 갈라지지 않는다. JSON 강제 방식만 프로바이더마다 다르다(구조화 출력 API가 서로 다르다).

**API 키는 ``.env``에서만 읽는다.** 코드에 기본값을 두지 않고, ``LLMSettings``에도 싣지
않는다 — 설정 객체는 로그·예외에 실릴 수 있고 키가 거기 묻어나가면 안 된다. 각 프로바이더가
호출 직전에 ``os.getenv``로 직접 읽는다. 키가 없으면 예외를 던지고 **fallback으로 간다** —
팀원이 브랜치만 받아도 산출물이 그대로 나오는 게 요건이다.
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import ValidationError

from app.purchase_agent.config import load_constraints
from app.purchase_agent.llm.schemas import (
    GradeMixInterpretation,
    InterpretationResult,
    LLMStatus,
    SanitizedLLMContext,
)

#: 팀 4벌은 ``backend/.env``를 읽는데 이 저장소의 실제 파일은 루트에 있다.
#: 어느 쪽에 두든 동작해야 하므로 **둘 다** 읽는다 (없는 파일은 무시된다).
_ENV_FILES = (
    Path(__file__).resolve().parents[3] / ".env",  # backend/.env — 팀 규약 위치
    Path(__file__).resolve().parents[4] / ".env",  # 저장소 루트 — 실제 위치
)
#: 에이전트 전용 접두사 — ``PURCHASE_LLM_MODEL``로 다른 에이전트와 분리한다 (critic 선례).
_ENV_PREFIX = "PURCHASE_"
#: 출력에 숫자가 있으면 거부한다. 팀 4벌이 전부 쓰는 규칙이고, 정의서 §1.2-3("LLM은
#: 가격·수량 숫자를 생성하지 않는다")을 프롬프트가 아니라 **검증기**로 강제하는 장치다.
#: ``\d``는 ASCII와 전각(１２３)을 잡지만 ``½``·``²``·``Ⅻ`` 같은 유니코드 수치 문자는
#: 놓친다 — ``str.isnumeric()``이 그쪽을 덮는다 (Codex 교차검증).
#: ⚠️ 한글 수사("백삼십원")는 **정규식으로 못 막는다.** 그건 판단 영역이라 프롬프트가
#: 맡고, 여기서 잡는 건 기계적으로 판별 가능한 것뿐이다 — 이 한계를 알고 쓴다.
_NUMERIC_PATTERN = re.compile(r"\d")


def _contains_number(text: str) -> bool:
    return bool(_NUMERIC_PATTERN.search(text)) or any(ch.isnumeric() for ch in text)


def _contains_control_chars(text: str) -> bool:
    """제어문자·zero-width·bidi 문자. rationale에 그대로 실리므로 표시 안전성 문제다."""
    return any(
        unicodedata.category(ch) in {"Cc", "Cf"} and ch not in "\n\t" for ch in text
    )

SYSTEM_PROMPT = """당신은 매입 에이전트의 등급 조합 판단 레이어다.
계산은 이미 끝났다. 규칙이 만든 후보 중 **하나를 고르고 이유를 쓰는 것**이 전부다.

규칙:
- 반드시 주어진 candidates의 candidate_id 중 하나를 고른다. 새 id를 만들지 않는다.
- 숫자, 비율, 수량, 금액, 날짜를 출력하지 않는다. 라벨과 말로만 설명한다.
- 계산하거나 추정하지 않는다. 수량은 이미 확정돼 있다.
- reason은 한두 문장으로 왜 그 후보인지만 쓴다.
- 지정된 JSON Schema에 맞는 JSON만 출력한다.

판단 기준:
- 중품은 싸지만 잔여신선도 안에 소진해야 한다. 못 쓰면 폐기 손실이다.
- SPREAD_WIDE면 단가 이득이 크고, SPREAD_NORMAL이면 신선도 리스크가 이득을 넘기 쉽다.
- SHELF_TIGHT면 중품 비중을 낮추는 쪽이, SHELF_AMPLE이면 높이는 쪽이 유리하다."""


@dataclass(frozen=True)
class LLMSettings:
    """설정. **API 키를 담지 않는다** — 로그·예외에 새는 경로를 만들지 않기 위해서다."""

    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int
    max_output_tokens: int
    effort: str | None
    #: 사유 길이 상한. constraints.yaml이 소유한다 (규칙 7) — 환경변수가 아니라
    #: 도메인 설정이라 ``.env``가 아니라 YAML에서 온다.
    reason_max_chars: int


class LLMProvider(Protocol):
    """문자열을 받아오는 것까지가 프로바이더의 일이다. 검증은 서비스가 한다."""

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str: ...


def _user_payload(
    context: SanitizedLLMContext, retry_guidance: list[str] | None
) -> str:
    payload: dict[str, Any] = {"context": context.model_dump(mode="json")}
    if retry_guidance:
        payload["correction"] = retry_guidance
    return json.dumps(payload, ensure_ascii=False)


def _require_model(settings: "LLMSettings") -> None:
    """모델명이 비었으면 호출하지 않는다 — 빈 문자열을 API에 보내면 사유가 흐려진다."""
    if not settings.model:
        raise RuntimeError(
            f"LLM_MODEL is not set for provider {settings.provider!r}"
        )


def _response_schema() -> dict[str, Any]:
    """구조화 출력에 넘길 JSON Schema.

    ``extra="forbid"``라 Pydantic이 ``additionalProperties: false``를 넣어준다 —
    Anthropic 구조화 출력과 OpenAI strict 모드가 **둘 다 요구하는** 항목이다.
    """
    return GradeMixInterpretation.model_json_schema()


class AnthropicProvider:
    """Messages API + 구조화 출력(``output_config.format``)."""

    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        import anthropic  # 지연 임포트 — 키·서버 없는 환경에서 import 비용을 안 낸다

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _require_model(self.settings)
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=self.settings.timeout_seconds,
            max_retries=0,  # 재시도는 서비스가 소유한다 — 두 층이 각자 세면 상한이 곱해진다
        )
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": _response_schema()}
        }
        if self.settings.effort:
            # **설정했을 때만 싣는다.** 지원하지 않는 모델에 실어 보내면 호출이 통째로
            # 실패하고, 서비스는 그걸 여느 실패와 똑같이 삼켜 규칙 기본안으로 떨어뜨린다
            # — LLM을 켜 뒀는데 매번 fallback인 상태가 조용히 유지된다.
            output_config["effort"] = self.settings.effort
        message = client.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_output_tokens,
            system=SYSTEM_PROMPT,
            output_config=output_config,
            messages=[{"role": "user", "content": _user_payload(context, retry_guidance)}],
        )
        # ⚠️ content[0]이 아니다. 사고(thinking) 블록이 앞에 오는 모델이 있어
        #    첫 블록을 그냥 읽으면 빈 문자열을 파싱하게 된다.
        for block in message.content:
            if getattr(block, "type", None) == "text":
                return block.text
        raise TypeError("Anthropic response contained no text block")


class OpenAIProvider:
    """Chat Completions + ``response_format`` json_schema(strict)."""

    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        import openai

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _require_model(self.settings)
        client = openai.OpenAI(
            api_key=api_key,
            timeout=self.settings.timeout_seconds,
            max_retries=0,
        )
        completion = client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_payload(context, retry_guidance)},
            ],
            # 토큰 상한을 여기도 건다 — 안 걸면 설정값이 무시된 장문 생성이 가능하다
            # (Codex 교차검증). OpenAI의 이름은 ``max_completion_tokens``다.
            max_completion_tokens=self.settings.max_output_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "grade_mix_interpretation",
                    "strict": True,
                    "schema": _response_schema(),
                },
            },
        )
        content = completion.choices[0].message.content
        if not isinstance(content, str):
            raise TypeError("OpenAI response did not contain message content")
        return content


class OllamaProvider:
    """팀 기존 4벌과 같은 로컬 경로. 표준 라이브러리만 쓴다 (SDK 없음)."""

    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        import urllib.error
        import urllib.request

        payload = {
            "model": self.settings.model,
            "stream": False,
            "think": False,
            "format": _response_schema(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_payload(context, retry_guidance)},
            ],
            # ``num_predict``가 Ollama의 출력 토큰 상한이다 — 세 프로바이더가 같은
            # 설정값을 각자의 이름으로 받는다.
            "options": {
                "temperature": 0,
                "num_ctx": 4096,
                "num_predict": self.settings.max_output_tokens,
            },
        }
        request = urllib.request.Request(
            f"{self.settings.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError("Purchase Local LLM request failed") from error
        content = (document.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise TypeError("Purchase Local LLM response did not contain message content")
        return content


class UnavailableProvider:
    """미지원 ``LLM_PROVIDER`` 값. 조용히 무시하지 않고 **터뜨려 fallback으로 보낸다**."""

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        del context, retry_guidance
        raise RuntimeError("Configured purchase LLM provider is not supported")


_PROVIDERS: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}


class ValidationIssue(StrEnum):
    INVALID_SCHEMA = "INVALID_SCHEMA"
    NUMERIC_OUTPUT_FORBIDDEN = "NUMERIC_OUTPUT_FORBIDDEN"
    UNKNOWN_CANDIDATE = "UNKNOWN_CANDIDATE"
    REASON_TOO_LONG = "REASON_TOO_LONG"
    EMPTY_FIELD = "EMPTY_FIELD"
    CONTROL_CHARACTERS = "CONTROL_CHARACTERS"


class MixValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        super().__init__(", ".join(issues))
        self.issues = issues


def validate_interpretation(
    raw_output: str, context: SanitizedLLMContext, *, reason_max_chars: int
) -> GradeMixInterpretation:
    """**프로바이더 밖의 공통 검증.** 어느 API를 쓰든 같은 관문을 지난다.

    구조화 출력이 스키마를 강제해도 이 검사가 필요하다 — 스키마는 "문자열 필드가 있다"까지
    보장할 뿐, 그 문자열이 **실재하는 후보 id인지**도 **숫자가 없는지**도 모른다.
    """
    try:
        interpretation = GradeMixInterpretation.model_validate_json(raw_output)
    except ValidationError as error:
        raise MixValidationError([ValidationIssue.INVALID_SCHEMA]) from error
    issues = _validation_issues(interpretation, context, reason_max_chars)
    if issues:
        raise MixValidationError(issues)
    return interpretation


def _validation_issues(
    interpretation: GradeMixInterpretation,
    context: SanitizedLLMContext,
    reason_max_chars: int,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    # 빈/공백 문자열. 스키마로 못 막는다 — 두 API의 JSON Schema가 문자열 길이 제약을
    # 지원하지 않아 ``min_length``를 뺐다 (schemas.GradeMixInterpretation 참조).
    # ``reason``이 비면 근거 없는 판단이 출력에 실리고, id가 비면 후보 대조가 무의미해진다.
    if not interpretation.chosen_candidate_id.strip() or not interpretation.reason.strip():
        issues.append(ValidationIssue.EMPTY_FIELD)
    # 후보 밖 id는 곧 "LLM이 비율을 지어냈다"와 같다 — 노드가 그 id로 비율을 못 찾는다.
    known = {candidate.candidate_id for candidate in context.candidates}
    if interpretation.chosen_candidate_id not in known:
        issues.append(ValidationIssue.UNKNOWN_CANDIDATE)
    # chosen_candidate_id는 검사 대상이 아니다 — 후보 id에 숫자가 들어갈 수 있다.
    if _contains_number(interpretation.reason):
        issues.append(ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN)
    if _contains_control_chars(interpretation.reason) or _contains_control_chars(
        interpretation.chosen_candidate_id
    ):
        issues.append(ValidationIssue.CONTROL_CHARACTERS)
    if len(interpretation.reason) > reason_max_chars:
        issues.append(ValidationIssue.REASON_TOO_LONG)
    return issues


def retry_guidance(issues: list[ValidationIssue]) -> list[str]:
    guidance = []
    if ValidationIssue.INVALID_SCHEMA in issues:
        guidance.append("지정된 두 필드만 포함한 유효한 JSON을 작성하세요.")
    if ValidationIssue.UNKNOWN_CANDIDATE in issues:
        guidance.append("candidates에 있는 candidate_id 중 하나를 그대로 사용하세요.")
    if ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in issues:
        guidance.append("reason에 숫자를 쓰지 마세요. 라벨과 말로만 설명하세요.")
    if ValidationIssue.REASON_TOO_LONG in issues:
        guidance.append("reason을 한두 문장으로 줄이세요.")
    if ValidationIssue.EMPTY_FIELD in issues:
        guidance.append("두 필드 모두 비어 있지 않게 작성하세요.")
    if ValidationIssue.CONTROL_CHARACTERS in issues:
        guidance.append("보이지 않는 제어문자를 넣지 말고 일반 텍스트로만 작성하세요.")
    return guidance


def needs_llm(context: SanitizedLLMContext) -> bool:
    """**후보가 둘 이상일 때만 부른다** — 고를 게 하나면 물어볼 이유가 없다.

    ⚠️ **이 검사만으로는 평시 호출이 0회가 되지 않는다.** ``cap_ratio``는 스프레드와
    무관하게 근접 납품량으로 계산되므로 평시에도 후보가 3개 나온다(실측). 비용 완화의
    본체는 노드 쪽 게이팅이다 — ``_select_mix``가 "규칙이 중품을 채택한 날"(rule_ratio > 0)
    에만 여기까지 온다. 이 함수는 그 뒤의 **마지막 안전장치**다.
    """
    return len(context.candidates) >= 2


class MixSelectionService:
    """검증·재시도·fallback을 소유한다. **프로바이더가 바뀌어도 이 층은 그대로다.**"""

    def __init__(self, settings: LLMSettings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def select(
        self, context: SanitizedLLMContext, *, default_candidate_id: str
    ) -> InterpretationResult:
        """후보 하나를 고른다. **실패하면 규칙 기본안을 그대로 돌려준다.**

        ``default_candidate_id``는 규칙이 고르던 값이라, LLM이 전면 실패해도 산출물이
        E3-1 시절과 동일해진다 — 회귀가 아니라 무변화다.
        """
        template = GradeMixInterpretation(
            chosen_candidate_id=default_candidate_id,
            reason="규칙 기본안",
        )
        if not self.settings.enabled:
            return self._result(template, status="DISABLED", attempts=0, fallback=False)
        if not needs_llm(context):
            return self._result(
                template, status="SKIPPED_TEMPLATE", attempts=0, fallback=False
            )

        guidance: list[str] | None = None
        attempts = 0
        for _ in range(self.settings.max_retries + 1):
            attempts += 1
            try:
                raw_output = self.provider.generate(context, retry_guidance=guidance)
                interpretation = validate_interpretation(
                    raw_output, context, reason_max_chars=self.settings.reason_max_chars
                )
                return self._result(
                    interpretation, status="SUCCESS", attempts=attempts, fallback=False
                )
            except MixValidationError as error:
                guidance = retry_guidance(error.issues)
            except Exception:  # noqa: BLE001 - 선택 실패가 그래프를 멈추면 안 된다
                # 키 없음·서버 없음·타임아웃·SDK 예외를 전부 여기서 받는다. **팀원이
                # 브랜치만 받아도 877건이 그대로 도는 것**이 이 한 줄에 걸려 있다.
                guidance = ["지정된 규칙과 JSON 형식에 맞춰 다시 작성하세요."]
        return self._result(template, status="FALLBACK", attempts=attempts, fallback=True)

    def _result(
        self,
        interpretation: GradeMixInterpretation,
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


#: 프로바이더별 기본 모델. ``LLM_MODEL``로 덮어쓴다.
#: **openai에는 기본값을 두지 않는다** — 확인하지 않은 모델 id를 코드에 박으면 404를
#: fallback으로 삼키게 되고, 그건 "LLM이 실패했다"와 "모델명을 지어냈다"를 뒤섞는다.
#: 값이 없으면 프로바이더가 즉시 터지고 사유가 risks에 남는다.
#: 프로바이더별 기본 모델. **openai만 일부러 비어 있다** — 확인하지 않은 모델 id를 박으면
#: 404가 fallback에 삼켜져 "LLM이 실패했다"와 "모델명이 틀렸다"가 구분되지 않는다.
#:
#: anthropic 기본이 **Haiku급인 이유** (모델 목록 2026-08-26 재확인): ⑤가 LLM에 맡기는 일은
#: 규칙이 만든 후보 3개 중 하나를 고르고 사유 한 문장을 쓰는 것이 전부다 — 숫자는 규칙이
#: 만든다(규칙 6). 판단 밀도가 낮은데 백테스트는 회당 품목 수만큼 호출하므로, 여기서 상위
#: 모델을 쓰면 비용·지연만 곱해진다. 상위 모델은 ③ 트레이드오프 서술처럼 서술 밀도가
#: 높은 자리에 남겨둔다.
#:
#: **날짜 붙은 스냅샷을 쓴다.** ``claude-haiku-4-5``는 스냅샷을 가리키는 별칭이라 가리키는
#: 대상이 바뀔 수 있다. 같은 as_of로 두 번 돌렸는데 결과가 다르면 백테스트 성적이 무효가
#: 되는 건 규칙 1의 look-ahead와 같은 종류의 문제다 — 재현되지 않는 실행은 근거가 못 된다.
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "ollama": "gemma3:4b",  # 팀 4벌의 기본값과 같다
}


def _env(key: str, default: str) -> str:
    """``PURCHASE_<KEY>`` → ``<KEY>`` → default. 빈 문자열은 미설정으로 본다 (critic 선례)."""
    return os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key) or default


def _read_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(key: str, default: str, *, minimum: int) -> int:
    """정수 설정. **파싱 실패는 기본값으로 되돌린다.**

    ``int("삼십")``이 ``ValueError``를 던지면 ``get_llm_settings()``가 터지고, 그 호출은
    ``build_graph()`` 안이라 **게이팅 이전**이다 — ``LLM_ENABLED=false``여도 그래프가
    멈춘다. ``.env`` 오타 하나로 877건이 죽는 건 "키·서버 없어도 돈다"는 요건 위반이다
    (Codex 교차검증 P2). 잘못된 값은 무시하고 기본값으로 간다.
    """
    raw = _env(key, default)
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _float_env(key: str, default: str, *, minimum: float) -> float:
    """실수 설정. 파싱 실패 시 기본값 — 이유는 ``_int_env``와 같다."""
    raw = _env(key, default)
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def get_llm_settings() -> LLMSettings:
    """``.env``에서 읽는다. **키는 여기 담지 않는다** — 프로바이더가 호출 직전에 읽는다.

    ⚠️ ``timeout_seconds``는 **총 벽시계 deadline이 아니다.** SDK에는 HTTP 단계별
    타임아웃으로, Ollama에는 소켓 타임아웃으로 전달된다. 서비스 재시도까지 더하면 최악의
    경우 설정값의 두 배를 넘을 수 있다 (Codex 교차검증). 총 deadline이 필요해지면
    별도 장치가 있어야 하고, 이 값 하나로는 보장되지 않는다.
    """
    for env_file in _ENV_FILES:
        load_dotenv(env_file)
    provider = _env("LLM_PROVIDER", "anthropic").strip().lower()
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=provider,
        model=_env("LLM_MODEL", _DEFAULT_MODELS.get(provider, "")).strip(),
        base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", "30", minimum=0.1),
        max_retries=min(1, _int_env("LLM_MAX_RETRIES", "1", minimum=0)),
        max_output_tokens=_int_env("LLM_MAX_OUTPUT_TOKENS", "8192", minimum=256),
        # **기본은 미설정이다.** effort는 모델마다 지원 여부가 다르고 기본 모델인
        # Haiku 4.5는 지원 목록에 없다. 기본 모델이 이미 최저 티어인 이상 비용 레버는
        # effort가 아니라 모델 선택이다 — 상위 모델로 올릴 때만 켠다.
        effort=(_env("LLM_EFFORT", "").strip() or None),
        # 도메인 임계는 .env가 아니라 constraints.yaml에서 온다 (규칙 7).
        reason_max_chars=load_constraints()["grade"]["mix_reason_max_chars"],
    )



def get_mix_selection_service() -> MixSelectionService:
    settings = get_llm_settings()
    factory = _PROVIDERS.get(settings.provider)
    provider: LLMProvider = factory(settings) if factory else UnavailableProvider()
    return MixSelectionService(settings, provider)
