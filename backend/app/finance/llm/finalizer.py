"""Finance Finalizer — **검증된 Evidence 에서 설명 키를 고른다.**

문장을 쓰지 않는다. `_FINAL_EXPLANATIONS` 세 키 중 하나를 고를 뿐이라, 새 숫자나
새 주장이 설명을 통해 들어올 자리가 없다.
"""

from __future__ import annotations

import json
import os
import urllib.request

from app.finance.llm.client import _gemini_generate
from app.finance.llm.config import _finance_model
from app.finance.llm.contracts import FinanceMode
from app.orchestrator.contracts_core import Evidence

#: 사용자에게 그대로 보이는 확정 설명.
#:
#: ★ **키는 기계 계약이고 값만 표시 문장이다.** Finalizer 는 이 키 중 하나를 고를 뿐이라,
#:   설명을 한국어로 바꿔도 LLM 이 숫자를 새로 만들 자리는 여전히 없다 — 고정 문장을
#:   고르는 구조 자체가 숫자 비소유를 지킨다.
#:
#: ★ 숫자를 넣지 않는다. `_validate_ready_reasoning` 이 reasoning 안의 숫자를 막는다.
_FINAL_EXPLANATIONS = {
    "PRE_BOUNDARY": "검증된 재무 근거가 보고된 매입 가능 경계를 뒷받침합니다.",
    "SCENARIO_REJECT": (
        "검증된 재무 근거가 원안 시나리오 중 최소 하나를 반려했습니다. "
        "함께 제시된 금액 대안은 별도로 검증했습니다."
    ),
    "SCENARIO_ACCEPT": "검증된 재무 근거가 보고된 시나리오 판정을 뒷받침합니다.",
}


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
        allowed = (
            ["PRE_BOUNDARY"]
            if mode == "PRE_PURCHASE"
            else ["SCENARIO_REJECT"]
            if business_status == "reject"
            else ["SCENARIO_ACCEPT"]
        )
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
                {
                    "role": "system",
                    "content": (
                        "Finalize the Finance reply from verified Evidence only. Select the "
                        "allowed explanation key. Do not calculate or add numbers or claims."
                    ),
                },
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
        allowed = (
            ["PRE_BOUNDARY"]
            if mode == "PRE_PURCHASE"
            else ["SCENARIO_REJECT"]
            if business_status == "reject"
            else ["SCENARIO_ACCEPT"]
        )
        selected = json.loads(
            _gemini_generate(
                model=self.model,
                system_prompt=(
                    "Finalize the Finance reply from verified Evidence only. Select the "
                    "allowed explanation key. Do not calculate or add numbers or claims."
                ),
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
        if mode == "PRE_PURCHASE":
            return _FINAL_EXPLANATIONS["PRE_BOUNDARY"]
        return _FINAL_EXPLANATIONS[
            "SCENARIO_REJECT" if business_status == "reject" else "SCENARIO_ACCEPT"
        ]
