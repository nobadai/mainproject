"""조사 Runtime 의 **공급자 전송** — 여기 한 곳에만 산다.

```text
plan_step   function calling (mode=ANY)  →  InvestigationStep     다음 한 수
finalize    구조화 JSON 출력              →  InvestigationReport   조사 정리
```

🔴 **재무 모듈을 import 하지 않는다 — 부서 경계다.** 전송 규율(`mode: ANY` ·
   `allowedFunctionNames` · Gemini 스키마 낮추기)은 `finance/llm/client.py` 에서
   **복제**한 것이고, 재무가 자기 사정으로 그 파일을 바꿔도 물류가 깨지면 안 된다.

⚠️ **해석기(`logistics/llm/runtime.py`)와도 합치지 않는다** (§29). 그쪽은 *"결정론 결과를
   설명"* 하는 역할이고 여기는 *"다음 Tool 을 고르는"* 역할이다. 같은 어휘
   (`LLMStatus` · `LLMErrorKind`)와 오류 분류(`classify_llm_error`)만 함께 쓴다.

환경 접두어는 `LOGISTICS_AGENT_*` 다 — 해석기의 `LOGISTICS_*` 와 **분리한다.** 한쪽만
끄고 켤 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

from app.logistics.agent.investigation import (
    ALLOWED_TOOL_NAMES,
    DEFAULT_BUDGET,
    LLM_CONTRACT_VIOLATION,
    AgentLLMBudgetExceeded,
    AgentLLMDisabled,
    InvestigationReport,
    InvestigationStep,
    InvestigationView,
    jsonable,
)
from app.logistics.agent.tool_dispatch import TOOL_ARGUMENT_MODELS
from app.logistics.llm.runtime import ProviderAuthError, ProviderConfigurationError

__all__ = [
    "FINISH_DECLARATION_NAME",
    "AgentLLMClient",
    "AgentLLMSettings",
    "PlannerContractError",
    "build_agent_llm",
    "get_agent_llm_settings",
    "tool_declarations",
]

_ENV_FILES = (Path(".env"), Path("../.env"))
#: 🔴 해석기는 `LOGISTICS_` 다. 접두어를 나눠야 **한쪽만 끄고 켤 수 있다.**
_ENV_PREFIX = "LOGISTICS_AGENT_"
_DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "ollama": "gemma3:4b"}
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: 모델이 *"이제 됐다"* 를 말하는 유일한 방법. 🔴 **자유 문장으로 끝내게 두지 않는다** —
#: `mode: ANY` 아래에서는 함수 호출만 나오므로 종료도 함수여야 한다.
FINISH_DECLARATION_NAME = "finish_investigation"


class PlannerContractError(ValueError):
    """모델이 계약을 깼다 — 호출 0건 · 2건 이상 · 스키마 위반.

    ★ 전송 실패(`URLError` 등)와 **다른 축**이다. 전송은 다시 걸면 되지만 계약 위반은
      다시 걸어도 같은 모델이 같은 답을 낼 수 있다 — 재계획 예산으로 센다.
    """


# ══════════════════════════════════════════════════════════════════════════
#  설정
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class AgentLLMSettings:
    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int


def _env(key: str, default: str) -> str:
    """`LOGISTICS_AGENT_<KEY>` → 전역 `<KEY>` → 기본값.

    ⚠️ **`LOGISTICS_<KEY>` 는 일부러 건너뛴다.** 거기 걸치면 해석기를 끌 때 조사도 같이
       꺼져 *"한쪽만 끄고 켠다"* 는 요구가 성립하지 않는다.
    """
    return os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key) or default


def _float_env(key: str, default: str, *, minimum: float) -> float:
    try:
        return max(minimum, float(_env(key, default)))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def _int_env(key: str, default: str, *, minimum: int) -> int:
    """🔴 파싱 실패는 기본값으로 되돌린다 — `.env` 오타 하나로 그래프가 죽으면 안 된다."""
    try:
        return max(minimum, int(_env(key, default)))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _bool_env(key: str, *, default: bool) -> bool:
    raw = _env(key, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def get_agent_llm_settings() -> AgentLLMSettings:
    """조사용 공급자 설정. 해석기 설정과 **독립**이다."""
    for env_file in _ENV_FILES:
        load_dotenv(env_file)
    scoped = os.getenv(f"{_ENV_PREFIX}LLM_PROVIDER")
    global_provider = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
    provider = (scoped or global_provider).strip().lower()
    if provider != global_provider and not os.getenv(f"{_ENV_PREFIX}LLM_MODEL"):
        # 공급자가 전역과 다른데 전역 모델을 물려받으면 없는 모델로 400 이 난다
        # (해석기에서 실제로 겪은 사례 — 같은 규율을 그대로 쓴다).
        model = _DEFAULT_MODELS.get(provider, "")
    else:
        model = _env("LLM_MODEL", _DEFAULT_MODELS.get(provider, ""))
    return AgentLLMSettings(
        enabled=_bool_env("LLM_ENABLED", default=True),
        provider=provider,
        model=model.strip(),
        base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        # 노드 하나당 30s (§8). 조사 전체 상한 120s 는 그래프가 따로 든다.
        timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", "30", minimum=0.1),
        max_retries=min(1, _int_env("LLM_MAX_RETRIES", "1", minimum=0)),
    )


# ══════════════════════════════════════════════════════════════════════════
#  선언 — 이번 호출에서 부를 수 있는 함수만 연다
# ══════════════════════════════════════════════════════════════════════════


def tool_declarations() -> list[dict[str, Any]]:
    """8개 Tool + 종료 함수. 🔴 **인자 스키마는 `guard` 가 쓰는 모델 그대로다.**

    ★ 선언과 검증이 같은 pydantic 모델에서 나오므로 **둘이 어긋날 수 없다.** 따로 손으로
      적으면 Tool 인자가 바뀔 때 한쪽만 고쳐지고, 모델은 옳게 답했는데 guard 가 막는
      상황이 생긴다.
    """
    declarations: list[dict[str, Any]] = []
    for name in ALLOWED_TOOL_NAMES:
        schema = TOOL_ARGUMENT_MODELS[name].model_json_schema()
        declarations.append(
            {
                "name": name,
                "description": _TOOL_DESCRIPTIONS[name],
                "parameters": _inline_refs(schema),
            }
        )
    declarations.append(
        {
            "name": FINISH_DECLARATION_NAME,
            "description": "더 물을 것이 없다. 조사를 끝내고 정리 단계로 넘어간다.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": [],
            },
        }
    )
    return declarations


_TOOL_DESCRIPTIONS: Mapping[str, str] = {
    "get_open_exceptions": "그날 살아 있던 운영 Exception 목록.",
    "get_lot": "Lot 하나의 잔량·상태·신선도·약정. 조사 대상 Lot 과 그 품목의 Lot 만.",
    "get_item_lots": "한 품목의 Lot 전부 (FEFO 순서).",
    "get_sales_commitments": "한 품목의 살아 있는 예약과 확정 출고.",
    "get_policy": "보관·회전 정책. item_id 를 비우면 창고 전역 정책이다.",
    "get_capacity_context": "창고 용량 문맥 — 사용량·보장량·날짜별 여유.",
    "get_inbound_schedule": "예정 입고 목록. days 는 조회 창의 길이다.",
    "estimate_action_impact": (
        "대응 후보 하나의 영향을 **Tool 이 계산한다**. "
        "판매 수량·도착일은 검토값으로만 넣는다 — 결정은 영업·매입이 한다."
    ),
}


def _inline_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """pydantic 의 `$defs`/`$ref` 를 펼친다 — 공급자 스키마는 참조를 안 받는다."""
    defs = schema.get("$defs") or {}

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            if "$ref" in node:
                key = str(node["$ref"]).rsplit("/", 1)[-1]
                return walk(defs.get(key, {}))
            return {name: walk(item) for name, item in node.items() if name != "$defs"}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def _gemini_safe_schema(node: Any) -> Any:
    """스키마의 **표현만** Gemini 가 받는 모양으로 낮춘다 — 계약은 그대로다.

    Gemini Schema 는 OpenAPI 3.0 부분집합이라 셋을 못 받고, 그대로 보내면 **HTTP 400**
    이다. 조사가 아니라 전송 형식이 문제인데 매 호출이 실패한다.

    ```text
    const                  → STRING + 한 값짜리 enum
    anyOf 안의 type:null   → 그 갈래를 빼고 nullable
    additionalProperties   → 제거
    ```
    """
    if not isinstance(node, Mapping):
        return node
    safe = {
        key: value
        for key, value in node.items()
        if key not in {"const", "anyOf", "additionalProperties"}
    }
    if "const" in node:
        safe["type"] = "string"
        safe["enum"] = [node["const"]]
    if "anyOf" in node:
        branches = [
            branch
            for branch in node["anyOf"]
            if isinstance(branch, Mapping) and branch.get("type") != "null"
        ]
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


#: `finalize` 응답 스키마. 🔴 **손으로 적는다** — pydantic 이 내는 `$ref`/`anyOf` 는
#: Gemini 부분집합 밖이다 (해석기 `_GEMINI_RESPONSE_SCHEMA` 와 같은 이유).
#: ★ 숫자 칸이 하나도 없는 것이 계약이다 (§37).
_GEMINI_REPORT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "findings": {"type": "ARRAY", "items": {"type": "STRING"}},
        "missing_or_uncertain": {"type": "ARRAY", "items": {"type": "STRING"}},
        "options": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "action": {"type": "STRING"},
                    "parameters": {"type": "STRING"},
                    "rationale": {"type": "STRING"},
                    "evidence_refs": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                },
                "required": ["action", "rationale", "evidence_refs"],
            },
        },
        "recommended_index": {"type": "INTEGER", "nullable": True},
    },
    "required": ["summary", "findings", "missing_or_uncertain", "options"],
}


PLANNER_SYSTEM_PROMPT = """당신은 재고·물류 조사 에이전트의 **계획 담당**이다.

하는 일은 하나뿐이다 — 지금까지 모은 사실을 보고 **다음에 부를 Tool 하나와 그 인자**를
고르는 것. 더 물을 것이 없으면 finish_investigation 을 부른다.

절대 하지 않는 것:
- 재고량·용량·신선도·예약량·원가·손실액을 스스로 계산하거나 추정하지 않는다.
- SQL 을 만들지 않고, DB 를 고치지 않는다.
- 판매 수량이나 매입 도착일을 결정하지 않는다.
- sim_run_id 와 as_of 는 **인자로 넣지 않는다.** 시스템이 못 박는다.
- 목록에 없는 Tool 이름을 만들지 않는다.

숫자는 전부 Tool 이 낸다. 같은 Tool 을 같은 인자로 두 번 부르지 않는다.
조사 대상과 상관없는 Lot·품목은 물을 수 없다 — scope 에 있는 것만 묻는다."""


FINALIZER_SYSTEM_PROMPT = """당신은 재고·물류 조사 에이전트의 **정리 담당**이다.

observations 에 있는 Tool 결과만 근거로 조사를 정리한다.

절대 하지 않는 것:
- **Tool 결과에 없는 숫자를 만들지 않는다.** 수량·비율·금액·남은 일수를 추정하지 않는다.
  모르면 missing_or_uncertain 에 «모른다» 고 적는다.
- 대응 후보는 주어진 카탈로그 밖으로 나가지 않는다.
- 판매 수량·매입 도착일을 결정하지 않는다. parameters 에 넣더라도 그것은 **검토값**이고
  실제 결정은 영업·매입이 한다.

evidence_refs 에는 근거로 삼은 observation 의 sequence 번호만 적는다.
options.parameters 는 JSON 객체 문자열로 적는다 (예: {"lot_id": "LOT-1"})."""


# ══════════════════════════════════════════════════════════════════════════
#  전송
# ══════════════════════════════════════════════════════════════════════════


def _post(url: str, *, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    # ★ SDK 를 안 쓰고 표준 라이브러리만 쓴다 — 팀 규율이고, HTTPError 가 그대로
    #   전파되어야 `classify_llm_error` 가 상태 코드로 분류할 수 있다.
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _gemini_key() -> str:
    key = os.getenv(f"{_ENV_PREFIX}GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise ProviderAuthError("Logistics agent Gemini API key is not set")
    return key


def _gemini_tool_call(
    settings: AgentLLMSettings,
    *,
    system_prompt: str,
    user_payload: Mapping[str, Any],
    declarations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Gemini function calling. `mode: ANY` + `allowedFunctionNames` 로 자유 문장을 닫는다.

    ⚠️ 전송 계층 강제일 뿐이다 — 돌아온 이름이 정말 허용된 것인지는 `guard` 가 다시 본다.
    """
    names = [str(item["name"]) for item in declarations]
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {"role": "user", "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}
        ],
        "tools": [
            {
                "function_declarations": [
                    {**item, "parameters": _gemini_safe_schema(item.get("parameters") or {})}
                    for item in declarations
                ]
            }
        ],
        "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": names}},
        "generationConfig": {"temperature": 0},
    }
    document = _post(
        f"{_GEMINI_BASE_URL}/models/{settings.model}:generateContent",
        body=body,
        headers={"x-goog-api-key": _gemini_key()},
        timeout=settings.timeout_seconds,
    )
    candidates = document.get("candidates") or []
    parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
    return [
        {
            "name": part["functionCall"].get("name"),
            "args": dict(part["functionCall"].get("args") or {}),
        }
        for part in parts
        if isinstance(part.get("functionCall"), Mapping)
    ]


def _ollama_tool_call(
    settings: AgentLLMSettings,
    *,
    system_prompt: str,
    user_payload: Mapping[str, Any],
    declarations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Ollama tool calling. Gemini 와 **같은 선언 목록**을 받는다 — 한 곳에서 만든다."""
    body = {
        "model": settings.model,
        "stream": False,
        "think": False,
        "tools": [{"type": "function", "function": dict(item)} for item in declarations],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "options": {"temperature": 0},
    }
    document = _post(
        f"{settings.base_url}/api/chat", body=body, headers={}, timeout=settings.timeout_seconds
    )
    raw = (document.get("message") or {}).get("tool_calls") or []
    return [
        {
            "name": (item.get("function") or {}).get("name"),
            "args": dict((item.get("function") or {}).get("arguments") or {}),
        }
        for item in raw
    ]


def _gemini_json(
    settings: AgentLLMSettings, *, system_prompt: str, user_payload: Mapping[str, Any]
) -> str:
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {"role": "user", "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _GEMINI_REPORT_SCHEMA,
        },
    }
    document = _post(
        f"{_GEMINI_BASE_URL}/models/{settings.model}:generateContent",
        body=body,
        headers={"x-goog-api-key": _gemini_key()},
        timeout=settings.timeout_seconds,
    )
    try:
        return str(document["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, TypeError) as error:
        raise TypeError("Logistics agent Gemini response did not contain text content") from error


def _ollama_json(
    settings: AgentLLMSettings, *, system_prompt: str, user_payload: Mapping[str, Any]
) -> str:
    body = {
        "model": settings.model,
        "stream": False,
        "think": False,
        "format": InvestigationReport.model_json_schema(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    document = _post(
        f"{settings.base_url}/api/chat", body=body, headers={}, timeout=settings.timeout_seconds
    )
    content = (document.get("message") or {}).get("content")
    if not isinstance(content, str):
        raise TypeError("Logistics agent Ollama response did not contain message content")
    return content


# ══════════════════════════════════════════════════════════════════════════
#  Client — 그래프가 보는 두 함수
# ══════════════════════════════════════════════════════════════════════════


class AgentLLMClient:
    """`plan` 과 `finalize` 둘뿐이다. 🔴 **Tool 을 직접 실행하지 않는다.**

    ★ 그래프는 이 클래스를 몰라도 된다 — `build_agent_llm()` 이 평범한 콜러블 둘을
      돌려주고, 테스트는 거기에 가짜 함수를 꽂는다 (매입 `selector` 와 같은 규율).
    """

    def __init__(self, settings: AgentLLMSettings, *, max_sends: int | None = None):
        self.settings = settings
        #: 🔴 **이 조사 한 번에 나갈 수 있는 전송 수.** 재시도도 교정도 여기서 센다.
        self.max_sends = DEFAULT_BUDGET.max_llm_calls if max_sends is None else max_sends
        self._sends = 0

    @property
    def sends(self) -> int:
        """지금까지 실제로 나간 전송 수 — 노드 수가 아니다."""
        return self._sends

    def _spend(self, send: Any) -> Any:
        """전송 한 번 = 예산 한 칸. 🔴 **재시도·교정도 똑같이 센다.**"""
        if self._sends >= self.max_sends:
            raise AgentLLMBudgetExceeded(
                f"Logistics agent LLM send budget exhausted ({self.max_sends})"
            )
        self._sends += 1
        return send()

    def plan(self, view: InvestigationView) -> InvestigationStep:
        """다음 한 수. 🔴 **호출이 정확히 하나가 아니면 계약 위반이다.**"""
        if not self.settings.enabled:
            raise AgentLLMDisabled("Logistics agent LLM is turned off")
        calls = self._send_tool_call(view)
        if len(calls) != 1:
            raise PlannerContractError(f"{LLM_CONTRACT_VIOLATION}:tool_calls={len(calls)}")
        call = calls[0]
        name = str(call.get("name") or "")
        if name == FINISH_DECLARATION_NAME:
            return InvestigationStep(
                action="FINISH", reason=str((call.get("args") or {}).get("reason") or "")
            )
        # 🔴 이름을 여기서 거르지 않는다 — 지어낸 이름도 `guard` 까지 데이터로 가야
        #    «무엇을 지어냈는가» 가 기록에 남는다.
        return InvestigationStep(
            action="CALL_TOOL", tool_name=name, arguments=dict(call.get("args") or {})
        )

    def finalize(self, view: InvestigationView) -> InvestigationReport:
        """조사 정리. 스키마 위반이면 **한 번** 교정을 요구하고, 그래도 안 되면 던진다."""
        if not self.settings.enabled:
            raise AgentLLMDisabled("Logistics agent LLM is turned off")
        payload = _view_payload(view)
        guidance: str | None = None
        last: Exception | None = None
        for _ in range(2):
            body = payload if guidance is None else {**payload, "correction": guidance}
            raw = self._send_json(body)
            try:
                return _parse_report(raw)
            except (ValidationError, ValueError, TypeError) as error:
                last = error
                guidance = "지정된 JSON 스키마에 정확히 맞춰 다시 작성하세요."
        raise PlannerContractError(f"{LLM_CONTRACT_VIOLATION}:report_schema") from last

    def _send_tool_call(self, view: InvestigationView) -> list[dict[str, Any]]:
        transport = _gemini_tool_call if self.settings.provider == "gemini" else _ollama_tool_call
        return _with_retry(
            lambda: self._spend(
                lambda: transport(
                    self.settings,
                    system_prompt=PLANNER_SYSTEM_PROMPT,
                    user_payload=_view_payload(view),
                    declarations=tool_declarations(),
                )
            ),
            retries=self.settings.max_retries,
        )

    def _send_json(self, payload: Mapping[str, Any]) -> str:
        transport = _gemini_json if self.settings.provider == "gemini" else _ollama_json
        return _with_retry(
            lambda: self._spend(
                lambda: transport(
                    self.settings, system_prompt=FINALIZER_SYSTEM_PROMPT, user_payload=payload
                )
            ),
            retries=self.settings.max_retries,
        )


def _with_retry[T](send: Any, *, retries: int) -> T:
    """전송만 재시도한다. 🔴 **인증·설정 실패는 다시 걸어도 같다** — 바로 던진다."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return send()
        except (ProviderAuthError, ProviderConfigurationError):
            raise
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, TypeError) as error:
            last = error
            if attempt >= retries:
                raise
        except urllib.error.HTTPError as error:
            # 4xx 는 우리 요청이 틀린 것이라 재시도해도 같다.
            if 400 <= error.code < 500:
                raise
            last = error
            if attempt >= retries:
                raise
    raise RuntimeError("Logistics agent LLM send failed") from last


def _view_payload(view: InvestigationView) -> dict[str, Any]:
    """모델에게 보내는 본문. 🔴 **`InvestigationView` 밖의 것은 안 실린다.**"""
    return {
        "as_of": view.as_of.isoformat(),
        "exception": jsonable(view.exception),
        "scope": jsonable(view.scope),
        "observations": [jsonable(item) for item in view.observations],
        "budget": jsonable(view.budget),
        "last_rejection": view.last_rejection,
        "allowed_tools": list(view.allowed_tools),
        "action_catalog": list(view.action_catalog),
    }


def _parse_report(raw: str) -> InvestigationReport:
    """공급자 JSON → `InvestigationReport`.

    ⚠️ `options[].parameters` 를 **문자열로도** 받는다 — Gemini 스키마 부분집합에 자유
       형식 객체가 없어 문자열로 선언했기 때문이다. 여기서 한 겹 푼다.
    """
    document = json.loads(raw)
    if not isinstance(document, Mapping):
        raise TypeError("Logistics agent report was not an object")
    options = []
    for option in document.get("options") or []:
        if not isinstance(option, Mapping):
            continue
        parameters = option.get("parameters")
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters) if parameters.strip() else {}
            except json.JSONDecodeError:
                parameters = {}
        # ⚠️ 객체가 아닌 것(배열 · 숫자)이 오면 **빈 인자로 낮춘다.** 그대로 넘기면 스키마
        #    위반으로 보고서 전체가 버려지는데, 정작 문장과 다른 후보는 멀쩡하다.
        #    빈 인자는 `estimate_action_impact` 가 `IMPACT_INPUT_MISSING` 으로 정확히 말한다.
        if not isinstance(parameters, Mapping):
            parameters = {}
        options.append({**option, "parameters": dict(parameters)})
    return InvestigationReport.model_validate({**document, "options": options})


def build_agent_llm(
    settings: AgentLLMSettings | None = None, *, max_sends: int | None = None
) -> tuple[Any, Any]:
    """`(plan_fn, finalize_fn)`. 그래프는 이 둘만 안다.

    ⚠️ **조사 한 번에 client 하나**다. 전송 예산이 인스턴스에 살기 때문에 재사용하면
       두 번째 조사가 첫 번째의 예산을 물려받는다.
    """
    client = AgentLLMClient(settings or get_agent_llm_settings(), max_sends=max_sends)
    return client.plan, client.finalize
