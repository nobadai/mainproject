"""Finance Finalizer — **검증된 Evidence 에서 설명 키를 고른다.**

문장을 쓰지 않는다. `_FINAL_EXPLANATIONS` 의 키 중 하나를 고를 뿐이라, 새 숫자나
새 주장이 설명을 통해 들어올 자리가 없다.

★ 사용자에게 나가는 문장 자체는 `app.finance.messages` 소유다. 여기서는 **어느 문장을
  고를지**만 정한다 — 문장을 여기 두면 Provider 코드마다 조금씩 다른 말투가 생긴다.
"""

from __future__ import annotations

import json
import os
import urllib.request

from app.finance.llm.client import _finance_model, _gemini_generate
from app.finance.messages import FINANCE_EXPLANATIONS, explanation_keys
from app.finance.schemas import FinanceMode
from app.orchestrator.contracts_core import Evidence

#: 사용자에게 그대로 보이는 확정 설명. **정본은 `app.finance.messages`** 다.
#:
#: ★ **키는 기계 계약이고 값만 표시 문장이다.** Finalizer 는 이 키 중 하나를 고를 뿐이라,
#:   설명을 어떻게 고쳐 써도 LLM 이 숫자를 새로 만들 자리는 여전히 없다.
_FINAL_EXPLANATIONS = FINANCE_EXPLANATIONS

#: Finalizer 에게 주는 규율. **사용자가 읽을 문장을 고르는 일**이라는 것을 명시한다.
#:
#: ★ 내부 구조를 말하지 말라고 적어 두는 이유: 모델은 프롬프트에 들어간 관측을 그대로
#:   흉내 내려는 경향이 있다. 고정 문장을 고르는 구조가 1차 방어이고, 이 규율은 그 위의
#:   2차 방어다 — 둘 중 하나만 두지 않는다.
_FINALIZER_SYSTEM_PROMPT = (
    "You choose the Korean explanation that a business user will read for a Finance "
    "review that is already complete. Answer only by selecting one allowed "
    "explanation key.\n"
    "Rules:\n"
    "- The reply the user sees is Korean and written for a finance/business reader.\n"
    "- Explain what the result means for their purchase decision and why.\n"
    "- Use only the verified evidence you are given.\n"
    "- Never calculate, derive, restate or invent any number or policy value.\n"
    "- Never change the verdict; it is already decided by deterministic rules.\n"
    "- Never mention internal architecture, agent framework, LangChain, Harness, "
    "Planner, Registry, Capability, Dependency, Tool names, run state or any other "
    "debugging detail.\n"
    "- Do not translate English implementation terms literally; the user does not "
    "know them."
)


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
        allowed = explanation_keys(mode, business_status)
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
                {"role": "system", "content": _FINALIZER_SYSTEM_PROMPT},
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
        allowed = explanation_keys(mode, business_status)
        selected = json.loads(
            _gemini_generate(
                model=self.model,
                system_prompt=_FINALIZER_SYSTEM_PROMPT,
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
        return _FINAL_EXPLANATIONS[explanation_keys(mode, business_status)[0]]
