"""모델이 고른 한 수를 **Tool 까지 보낼지** 결정론이 판정한다 — 허용 · 인자 · 범위 · 중복 · 예산.

```text
plan_step (LLM)  →  guard (여기)  →  execute_tool (여기)  →  observations
                      ↑ 막히면 Tool 까지 가지도 않는다
```

🔴 **모델 인자를 그대로 Tool 에 넘기지 않는다.** 여기서 한 번 더 보는 이유는 구조화
   출력이 계약을 지킨다는 보장이 없어서다 (재무 `_gemini_tool_call` 주석과 같은 근거).

★ **Dynamic Plugin Registry 가 아니다.** 이름 → 함수 매핑 하나뿐이고, 목록을 넓히려면
  `agent.tools` 에 Tool 을 먼저 만들어야 한다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.logistics.agent import tools
from app.logistics.agent.investigation import (
    ALLOWED_TOOL_NAMES,
    DUPLICATE_TOOL_CALL,
    INVALID_ARGUMENTS,
    PINNED_ARGUMENT_OVERRIDE,
    PINNED_TOOL_ARGUMENTS,
    SUBJECT_OUT_OF_SCOPE,
    TOOL_BUDGET_EXCEEDED,
    UNKNOWN_TOOL,
    InvestigationStep,
    jsonable,
)
from app.logistics.agent.tools import SUPPORTED_ACTIONS

__all__ = [
    "TOOL_ARGUMENT_MODELS",
    "TOOL_EXECUTORS",
    "GuardVerdict",
    "call_key",
    "guard_step",
    "run_tool",
]


# ══════════════════════════════════════════════════════════════════════════
#  Tool 별 인자 스키마 — 모르는 키 · 빠진 키 · 틀린 타입은 여기서 멈춘다
# ══════════════════════════════════════════════════════════════════════════


class _Arguments(BaseModel):
    #: 🔴 `extra="forbid"` 가 *"모르는 키"* 를 막는 자리다. 조용히 버리면 모델은 자기가
    #:    보낸 인자가 먹혔다고 믿고 같은 실수를 반복한다.
    model_config = ConfigDict(extra="forbid")


class NoArguments(_Arguments):
    """`sim_run_id` · `as_of` 말고는 받을 것이 없는 Tool."""


class LotArguments(_Arguments):
    lot_id: str = Field(min_length=1)


class ItemArguments(_Arguments):
    item_id: str = Field(min_length=1)


class PolicyArguments(_Arguments):
    #: 품목을 안 주면 창고 전역 정책이다 — Tool 계약 그대로 `None` 을 허용한다.
    item_id: str | None = None


class InboundScheduleArguments(_Arguments):
    #: Tool 이 다시 `0..18` 로 조인다. 여기서는 음수만 막는다.
    days: int | None = Field(default=None, ge=0)


class ActionImpactArguments(_Arguments):
    """🔴 **카탈로그 밖 행동은 여기서 멈춘다.**

    Tool 은 모르는 행동에 `UNSUPPORTED` 를 정확히 답하지만, 그 답을 받으려고 예산을
    한 칸 태울 이유가 없다 — 카탈로그는 닫혀 있고(§14) 그 사실은 모델에게 이미 줬다.
    """

    action: Literal[
        "SALES_PRIORITY_REQUEST",
        "PURCHASE_ADJUST_REQUEST",
        "ACCEPT_RISK",
        "DISPOSAL_REQUEST",
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def _coerce(cls, value: dict[str, Any]) -> dict[str, Any]:
        """JSON 이 낮춘 표현을 **Tool 이 읽는 타입으로 되돌린다.** 값은 안 바꾼다.

        ```text
        "2026-08-25"  → date(2026, 8, 25)    Tool 은 date 만 cap_by_date 키와 맞춘다
        120.5         → Decimal("120.5")     🔴 tools._quantity 는 float 을 거부한다
        ```

        ⚠️ 업무 판단이 아니라 **전송 형식 복원**이다. 이걸 안 하면 모델이 옳은 인자를
           보내도 이유 없는 `UNRESOLVED` 가 돌아온다 — 그 실패는 모델 탓으로 보이지만
           실제로는 우리 쪽 전송 문제다.
        """
        restored = dict(value)
        for key in ("qty_kg", "qty_delta_kg"):
            if key in restored:
                restored[key] = _decimal(restored[key])
        if "arrival_date" in restored:
            restored["arrival_date"] = _iso_date(restored["arrival_date"])
        return restored


def _decimal(value: Any) -> Any:
    """`float` 은 **문자열을 거쳐** `Decimal` 로 — 이진 오차를 업무 수량에 들이지 않는다."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float | str):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return value
    return value


def _iso_date(value: Any) -> Any:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


#: 이름 → 인자 스키마. 🔴 `TOOL_EXECUTORS` 와 **같은 키 집합**이어야 한다.
TOOL_ARGUMENT_MODELS: Mapping[str, type[_Arguments]] = {
    "get_open_exceptions": NoArguments,
    "get_lot": LotArguments,
    "get_item_lots": ItemArguments,
    "get_sales_commitments": ItemArguments,
    "get_policy": PolicyArguments,
    "get_capacity_context": NoArguments,
    "get_inbound_schedule": InboundScheduleArguments,
    "estimate_action_impact": ActionImpactArguments,
}

#: 이름 → 실제 함수. 평범한 파이썬 호출이다 — LangChain `@tool` 로 다시 감싸지 않는다.
TOOL_EXECUTORS: Mapping[str, Any] = {
    "get_open_exceptions": tools.get_open_exceptions,
    "get_lot": tools.get_lot,
    "get_item_lots": tools.get_item_lots,
    "get_sales_commitments": tools.get_sales_commitments,
    "get_policy": tools.get_policy,
    "get_capacity_context": tools.get_capacity_context,
    "get_inbound_schedule": tools.get_inbound_schedule,
    "estimate_action_impact": tools.estimate_action_impact,
}


# ══════════════════════════════════════════════════════════════════════════
#  범위 — 조사 대상과 상관없는 것을 무제한으로 못 본다 (§17)
# ══════════════════════════════════════════════════════════════════════════

#: 🔴 이 두 이름이 **범위 판정의 전부**다. 나머지 Tool 은 대상 인자가 없어 창고 문맥
#:    (용량 · 입고 · 정책 · 목록)이므로 언제나 허용이다.
_LOT_SCOPED = ("get_lot",)
_ITEM_SCOPED = ("get_item_lots", "get_sales_commitments", "get_policy")


@dataclass(frozen=True, kw_only=True)
class GuardVerdict:
    """`guard` 한 번의 판정. **승인이면 인자는 이미 검증·복원된 것**이다."""

    approved: bool
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None
    #: 중복 판정에 쓰는 정규화 키. 승인일 때만 있다.
    key: str | None = None
    #: 거부 사유 (`UNKNOWN_TOOL:foo` 처럼 값이 붙는다). 승인이면 `None`.
    rejection: str | None = None


def call_key(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """`(Tool, 인자)` 의 정규형. 🔴 **키 순서가 달라도 같은 호출이다.**

    ★ 인자를 `jsonable` 로 낮춰 비교한다 — `Decimal("1")` 과 `Decimal("1.0")` 은 다른
      문자열이 되지만, 그건 *실제로 다른 질문*이라 다른 호출로 세는 편이 맞다.
    """
    return f"{tool_name}:{json.dumps(jsonable(arguments), sort_keys=True, ensure_ascii=False)}"


def guard_step(
    step: InvestigationStep,
    *,
    allowed_lot_ids: frozenset[str],
    allowed_item_ids: frozenset[str],
    executed_keys: frozenset[str],
    tool_call_count: int,
    max_tool_calls: int,
) -> GuardVerdict:
    """모델의 한 수를 **일곱 관문**에 통과시킨다. 하나라도 걸리면 Tool 까지 안 간다.

    ```text
    ① 허용 목록      지어낸 Tool 이름 · 목록 밖 이름          UNKNOWN_TOOL
    ② 못 박힌 인자    sim_run_id · as_of 를 건드렸다           PINNED_ARGUMENT_OVERRIDE
    ③ 인자 스키마     모르는 키 · 빠진 키 · 틀린 타입           INVALID_ARGUMENTS
    ④ 조사 범위      상관없는 Lot/품목                        SUBJECT_OUT_OF_SCOPE
    ⑤ 중복          같은 Tool 을 같은 인자로 또                DUPLICATE_TOOL_CALL
    ⑥ Tool 예산      8회를 다 썼다                            TOOL_BUDGET_EXCEEDED
    ```

    ⚠️ ②를 *"조용히 덮기"* 로 처리하지 않는다. 덮으면 **모델이 다른 실행/다른 날을
       물었다는 사실 자체가 사라지고**, 로그에는 정상 호출로 남는다.
    """
    name = step.tool_name
    if not name or name not in ALLOWED_TOOL_NAMES:
        return GuardVerdict(approved=False, rejection=f"{UNKNOWN_TOOL}:{name or ''}")

    pinned = [key for key in PINNED_TOOL_ARGUMENTS if key in step.arguments]
    if pinned:
        return GuardVerdict(
            approved=False, rejection=f"{PINNED_ARGUMENT_OVERRIDE}:{','.join(pinned)}"
        )

    model = TOOL_ARGUMENT_MODELS[name]
    try:
        parsed = model.model_validate(step.arguments)
    except ValidationError as error:
        return GuardVerdict(approved=False, rejection=f"{INVALID_ARGUMENTS}:{_brief(error)}")

    arguments = parsed.model_dump()
    out_of_scope = _out_of_scope(
        name,
        arguments,
        allowed_lot_ids=allowed_lot_ids,
        allowed_item_ids=allowed_item_ids,
    )
    if out_of_scope:
        return GuardVerdict(approved=False, rejection=f"{SUBJECT_OUT_OF_SCOPE}:{out_of_scope}")

    key = call_key(name, arguments)
    if key in executed_keys:
        return GuardVerdict(approved=False, rejection=f"{DUPLICATE_TOOL_CALL}:{name}")

    if tool_call_count >= max_tool_calls:
        return GuardVerdict(approved=False, rejection=f"{TOOL_BUDGET_EXCEEDED}:{max_tool_calls}")

    return GuardVerdict(approved=True, tool_name=name, arguments=arguments, key=key)


def _out_of_scope(
    name: str,
    arguments: Mapping[str, Any],
    *,
    allowed_lot_ids: frozenset[str],
    allowed_item_ids: frozenset[str],
) -> str | None:
    """범위 밖 대상 하나를 이름으로 돌려준다. 범위 안이면 `None`.

    ★ **정당한 확장은 막지 않는다.** 같은 품목의 다른 Lot(`get_item_lots`)은 조사상
      당연한 질문이고, 범위는 *이미 본 증거*가 넓힌다 (`investigation.collect_ids`).
    """
    if name in _LOT_SCOPED:
        lot_id = arguments.get("lot_id")
        if isinstance(lot_id, str) and lot_id not in allowed_lot_ids:
            return f"lot_id={lot_id}"
        return None
    if name in _ITEM_SCOPED:
        item_id = arguments.get("item_id")
        if isinstance(item_id, str) and item_id not in allowed_item_ids:
            return f"item_id={item_id}"
        return None
    if name == "estimate_action_impact":
        lot_id = (arguments.get("parameters") or {}).get("lot_id")
        if isinstance(lot_id, str) and lot_id not in allowed_lot_ids:
            return f"lot_id={lot_id}"
        return None
    return None


def _brief(error: ValidationError) -> str:
    """오류를 **한 줄**로 줄인다 — 모델에게 되돌려 줄 문장이라 길면 예산만 먹는다."""
    parts = [
        f"{'.'.join(str(piece) for piece in item['loc']) or '(root)'}:{item['type']}"
        for item in error.errors()[:3]
    ]
    return " ".join(parts) or "invalid"


def run_tool(
    conn: Any,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    sim_run_id: str,
    as_of: date,
) -> Any:
    """Tool 하나를 부른다. 🔴 **`sim_run_id` · `as_of` 는 여기서 못 박는다.**

    모델이 준 `arguments` 에는 이 둘이 들어올 수 없다 (`guard` ②관문). 그래서 덮어쓰기
    충돌이 날 자리가 없고, 두 축의 정본은 언제나 요청이다.
    """
    executor = TOOL_EXECUTORS[tool_name]
    return executor(conn, sim_run_id=sim_run_id, as_of=as_of, **dict(arguments))


def catalog_actions() -> tuple[str, ...]:
    """대응 후보 카탈로그. `agent.tools` 의 정본을 그대로 쓴다 — 두 벌로 두지 않는다."""
    return SUPPORTED_ACTIONS
