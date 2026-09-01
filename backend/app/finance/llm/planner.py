"""Finance Planner — **Tool 선택만 한다.**

숫자·정책값을 만들지 않는다. 고를 수 있는 Tool 집합과 남은 capability 는 Controller 가
정해서 넘기고, Planner 는 그중 하나를 고르거나 finalize 를 낼 뿐이다.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from app.finance.llm.client import _gemini_generate
from app.finance.llm.config import _finance_model
from app.finance.llm.contracts import (
    _PLANNER_SYSTEM_PROMPT,
    FinancePlannerFailure,
    ToolAction,
    _gemini_planner_response_schema,
    _planner_prompt,
    _planner_response_schema,
    _validate_planner_action,
)
from app.finance.state import _CAPABILITY_TOOLS
from app.master.envelope import AgentRequest


class OllamaFinancePlanner:
    """허용된 Tool 호출 또는 finalize로 출력이 제한된 LLM Planner."""

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or _finance_model("ollama")
        self.base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        self.attempts = 0

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        self.attempts += 1
        schema = _planner_response_schema(
            allowed_tools, planning_required=bool(missing_capabilities)
        )
        schema["additionalProperties"] = False
        prompt = _planner_prompt(
            request=request,
            allowed_tools=allowed_tools,
            observations=observations,
            missing_capabilities=missing_capabilities,
        )
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": schema,
            "messages": [
                {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt, default=str)},
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
        content = json.loads(raw["message"]["content"])
        action = ToolAction(**content)
        _validate_planner_action(action, allowed_tools, missing_capabilities)
        return action


class GeminiFinancePlanner:
    """Finance Tool 선택만 수행하는 Gemini structured-output Planner."""

    def __init__(self) -> None:
        self.model = _finance_model("gemini")
        self.attempts = 0

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        self.attempts += 1
        content = json.loads(
            _gemini_generate(
                model=self.model,
                system_prompt=_PLANNER_SYSTEM_PROMPT,
                user_payload=_planner_prompt(
                    request=request,
                    allowed_tools=allowed_tools,
                    observations=observations,
                    missing_capabilities=missing_capabilities,
                ),
                # ★ 계약은 Ollama 와 **같다** — 이번 호출의 allowed_tools 가 enum 으로
                #   들어간다. 다만 Gemini 가 못 받는 표현(boolean enum · null 타입)은
                #   낮춰서 보내고, 그 강제는 `_validate_planner_action` 이 대신 한다.
                response_schema=_gemini_planner_response_schema(
                    allowed_tools, planning_required=bool(missing_capabilities)
                ),
            )
        )
        action = ToolAction(**content)
        _validate_planner_action(action, allowed_tools, missing_capabilities)
        return action


class DeterministicFinancePlanner:
    """LLM 이 꺼졌을 때 쓰는 Planner. **선택만** 결정론으로 대신한다.

    ★ 새 재무 정책을 만들지 않는다. 고를 수 있는 Tool 집합(`allowed_tools`)과 남은
      capability 는 Controller 가 이미 정해서 넘긴다 — 여기서 하는 일은 그중 하나를
      **정해진 순서로** 집는 것뿐이다. 숫자·판정은 여전히 Tool 과 Rule 이 만든다.
    """

    model = "deterministic-finance-planner"

    def __init__(self) -> None:
        self.attempts = 0

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        del request, observations
        self.attempts += 1
        if not missing_capabilities:
            return ToolAction(finalize=True, reason="capabilities complete")
        for capability in missing_capabilities:
            for tool in sorted(_CAPABILITY_TOOLS[capability]):
                if tool in allowed_tools:
                    return ToolAction(tool_name=tool, reason=f"satisfies {capability}")
        raise FinancePlannerFailure(
            "no allowed Finance tool can satisfy the missing capabilities"
        )
