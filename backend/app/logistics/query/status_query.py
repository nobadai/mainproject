"""질문형 STATUS_QUERY — **LLM function/tool calling** 조회 (LOG-MDS-004 · Issue #789).

기존 Overview(`payload={}`)는 `adapter._status_query` 가 그대로 처리한다. 이 모듈은
`payload={"question": ...}` 일 때만 도는 경로다.

```text
question
  → LLM 에게 Read-only Tool schema 제공(item_name 수준)
  → LLM 이 부를 Tool 을 직접 선택하고 tool_call 생성
  → runtime 이 thin wrapper 로 실행 (item_name→item_id resolve + allowlist = 결정론)
  → Tool 결과를 다시 LLM 에게
  → LLM 이 추가 tool_call 또는 최종 자연어 답변
```

🔴 **Tool 선택은 LLM 이 한다.** topic→Tool 파이썬 고정 매핑을 쓰지 않는다(#789).

🔴 **숫자·item_id 는 결정론이다.** wrapper 가 `item_name` 을 DB 로 `item_id` 로 바꾸고
   (마스터 `_item_id_of` 규약), 프로젝트 3품목(배추·무·양파)만 허용하고, 기존
   Read-only Tool 을 **시그니처 그대로** 부른다. LLM 은 `item_id` 를 만들지 않는다.

🔴 **LLM 이 primary 다.** 결정론 파서 fallback 은 없다 — provider 실패는
   `StatusQueryLLMError` 로 드러난다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_connection, get_db_schema
from app.logistics.query.llm import (
    AssistantTurn,
    Chat,
    StatusQueryLLMError,
    ToolCall,
    build_chat,
)
from app.logistics.query.tools import (
    get_capacity_context,
    get_inbound_schedule,
    get_item_lots,
    get_lot,
    get_open_exceptions,
    get_policy,
    get_sales_commitments,
)

__all__ = [
    "EXCLUDED_ITEMS",
    "MAX_TOOL_CALLS",
    "MAX_TURNS",
    "PROJECT_ITEMS",
    "PROJECT_ITEM_IDS",
    "SYSTEM_PROMPT",
    "TOOL_SCHEMAS",
    "ResolvedItems",
    "StatusQueryAnswer",
    "answer_status_question",
    "resolve_item_names",
    "run_tool",
]


# ---------------------------------------------------------------------------
# 프로젝트 품목 범위 — 🔴 업무 결정이다 (DB 정책/Zone 유무가 아니다)
# ---------------------------------------------------------------------------

#: 프로젝트 정상 조회 대상 (이름 → item_id). item_id 는 언제나 `resolve_item_names`(DB)가
#: 확정한다 — 이 상수는 allowlist 판정과 이름 표기에만 쓴다.
PROJECT_ITEMS: dict[str, str] = {
    "배추": "ITEM-BAECHU",
    "무": "ITEM-MU",
    "양파": "ITEM-YANGPA",
}
PROJECT_ITEM_IDS: frozenset[str] = frozenset(PROJECT_ITEMS.values())

#: DB 에 남아 있어도 현재 프로젝트 범위에서 제외된 품목.
EXCLUDED_ITEMS: dict[str, str] = {
    "피마늘": "ITEM-PIMANUL",
    "건고추": "ITEM-GEONGOCHU",
}

#: tool-calling loop 상한 — LLM 왕복 수와 총 tool 호출 수.
MAX_TURNS = 6
MAX_TOOL_CALLS = 12


# ---------------------------------------------------------------------------
# item resolver — 🔴 **DB 가 정본, allowlist 가 gate** (언제나 결정론)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ResolvedItems:
    allowed: dict[str, str]
    excluded: dict[str, str]
    not_found: tuple[str, ...]


def resolve_item_names(conn: Any, names: Sequence[str]) -> ResolvedItems:
    """품목명 → `item_id` 를 **DB `items` 로 확정**하고 프로젝트 허용 여부로 가른다.

    ★ 마스터 `_item_id_of` 와 같은 `item_name → item_id` 규약. 중복 이름은 한 번만 조회.
    """
    unique = tuple(dict.fromkeys(name for name in names if name))
    if not unique:
        return ResolvedItems(allowed={}, excluded={}, not_found=())

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT item_id, item_name FROM {schema}.items WHERE item_name = ANY(%(names)s)"
            ).format(schema=schema),
            {"names": list(unique)},
        )
        by_name = {row["item_name"]: row["item_id"] for row in cursor.fetchall()}

    allowed: dict[str, str] = {}
    excluded: dict[str, str] = {}
    not_found: list[str] = []
    for name in unique:
        item_id = by_name.get(name)
        if item_id is None:
            not_found.append(name)
        elif item_id in PROJECT_ITEM_IDS:
            allowed[name] = item_id
        else:
            excluded[name] = item_id
    return ResolvedItems(allowed=allowed, excluded=excluded, not_found=tuple(not_found))


def _resolve_one(conn: Any, name: str | None) -> tuple[str | None, str]:
    """한 품목명 → (item_id, 상태). 상태 ∈ ALLOWED · EXCLUDED · NOT_FOUND · MISSING."""
    if not name:
        return None, "MISSING"
    resolved = resolve_item_names(conn, [name])
    if name in resolved.allowed:
        return resolved.allowed[name], "ALLOWED"
    if name in resolved.excluded:
        return resolved.excluded[name], "EXCLUDED"
    return None, "NOT_FOUND"


def _out_of_scope(name: str | None, status: str) -> dict[str, Any]:
    """제외/미확인 품목을 «실패가 아닌 사실» 로 LLM 에 돌려준다."""
    if status == "EXCLUDED":
        return {
            "item_name": name,
            "status": "NOT_IN_PROJECT_SCOPE",
            "message": "현재 프로젝트의 재고·물류 조회 대상 품목이 아닙니다.",
        }
    if status == "MISSING":
        return {"status": "ITEM_NAME_REQUIRED", "message": "품목명이 필요합니다."}
    return {
        "item_name": name,
        "status": "ITEM_NOT_FOUND",
        "message": "해당 품목을 찾을 수 없습니다.",
    }


# ---------------------------------------------------------------------------
# JSON 직렬화 (Tool 결과를 LLM 에 돌려줄 모양)
# ---------------------------------------------------------------------------


def _num(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _jsonify(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def _lot_dict(lot: Any) -> dict[str, Any]:
    return {
        "lot_id": lot.lot_id,
        "remaining_qty_kg": _num(lot.remaining_qty_kg),
        "uncommitted_kg": _num(lot.uncommitted_kg),
        "remaining_freshness_days": lot.remaining_freshness_days,
        "turnover_status": lot.turnover_status,
        "sell_priority": lot.sell_priority,
        "disposal_candidate": lot.disposal_candidate,
        "status": lot.status,
    }


# ---------------------------------------------------------------------------
# Tool schema — LLM 에게 여는 Read-only Tool (item_name 수준 · estimate_action_impact 제외)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "get_item_lots",
        "description": "한 품목의 Lot 목록과 현재고·판매가용·신선도. 품목명은 한국어(예: 배추).",
        "parameters": {
            "type": "object",
            "properties": {"item_name": {"type": "string", "description": "품목명 예: 배추"}},
            "required": ["item_name"],
        },
    },
    {
        "name": "get_sales_commitments",
        "description": "한 품목의 살아 있는 예약과 확정 출고.",
        "parameters": {
            "type": "object",
            "properties": {"item_name": {"type": "string"}},
            "required": ["item_name"],
        },
    },
    {
        "name": "get_capacity_context",
        "description": "창고 전체 용량·여유(품목 무관 · 창고 전체 기준).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_inbound_schedule",
        "description": "입고 예정 목록. item_name 을 주면 그 품목만, 비우면 프로젝트 품목 전체.",
        "parameters": {
            "type": "object",
            "properties": {"item_name": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_open_exceptions",
        "description": "지금 열려 있는 물류 문제. item_name 을 주면 그 품목 관련만.",
        "parameters": {
            "type": "object",
            "properties": {"item_name": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_policy",
        "description": "보관·회전·용량 정책. item_name 을 주면 그 품목 정책, 비우면 전역.",
        "parameters": {
            "type": "object",
            "properties": {"item_name": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_lot",
        "description": "Lot 하나의 상태·잔량·신선도.",
        "parameters": {
            "type": "object",
            "properties": {"lot_id": {"type": "string"}},
            "required": ["lot_id"],
        },
    },
)

_ALLOWED_TOOL_NAMES = frozenset(schema["name"] for schema in TOOL_SCHEMAS)

SYSTEM_PROMPT = (
    # ── 조회 규율 (Tool 호출 단계) ──────────────────────────────────────
    "너는 창고 물류 상태를 **조회만** 하는 도우미다. 사용자의 질문에 답하려면 제공된 "
    "Read-only Tool 을 직접 선택해 호출하라. 품목은 한국어 이름(배추·무·양파)으로 넘기고, "
    "내부 item_id 를 지어내지 마라. 재고·신선도·예약·용량 같은 숫자를 직접 계산하거나 "
    "합산·환산하지 말고 반드시 Tool 결과에 있는 값을 그대로 사용하라. 여러 품목을 물으면 "
    "품목마다 Tool 을 호출하라. 어떤 품목이 프로젝트 조회 대상이 아니면 Tool 이 그렇게 "
    "알려준다 — 그 품목은 빼고 나머지는 정상 조회하라.\n\n"
    # ── 최종 답변 규율 ────────────────────────────────────────────────
    "충분한 정보를 모으면 한국어로 최종 답변을 하라. 최종 답변은 **사용자에게 그대로 보이는 "
    "글**이므로 다음을 지켜라.\n"
    "1. 질문에 대한 핵심 답을 **첫 문장에 바로** 말하라. "
    '예: "2026-08-31 기준 배추 재고는 총 3,197kg입니다."\n'
    "2. 내부 구현 정보를 답변에 **절대 노출하지 마라** — Tool 이름, 호출 인자, tool_trace, "
    "tools_used, 내부 item_id(ITEM-BAECHU 등), raw 결과, 내부 스키마/필드 이름, 상태 코드, "
    "provider 이름, 디버그 metadata. 사용자는 이런 것을 묻지 않았다.\n"
    "3. 사용자가 Lot 상세를 **명시적으로 요청하지 않았다면 Lot 을 나열하지 마라.** 특히 "
    "잔량이 0인 Lot, 소진(DEPLETED)·폐기된 Lot, 과거 이력 Lot 은 일반 재고 질문의 답변에서 "
    "생략하라.\n"
    "4. Lot 은 필요할 때만 **현재 재고가 남아 있는 것의 개수로 요약**하라. "
    '예: "현재 재고가 남아 있는 Lot은 5개입니다."\n'
    "5. 신선도·위험 정보는 질문과 관련 있을 때만 **한 문장으로 짧게** 덧붙여라. "
    '예: "이 중 514kg은 신선도가 1일 남아 판매 우선 대상입니다."\n'
    "6. 불확실한 점이 없으면 굳이 «불확실성 없음» 같은 말을 하지 마라.\n"
    "7. 조회 대상이 아닌 품목은 **사용자가 이해할 말로만** 설명하라. "
    '예: "피마늘은 현재 프로젝트 조회 대상에 포함되지 않아 조회하지 않았습니다." '
    "내부 상태 문자열(NOT_IN_PROJECT_SCOPE 등)을 그대로 쓰지 마라.\n"
    "8. 창고 여유는 창고 전체 기준이다. 내부 값(warehouse_scope=global 등)을 그대로 보이지 "
    '말고 자연어로 설명하라. 예: "창고 전체 기준 현재 여유 용량은 7,375kg입니다."\n'
    "9. 답변은 짧게 유지하라 — 사용자가 상세 설명을 요청하지 않았으면 **2~5문장**을 넘기지 "
    "마라. 표·목록·머리말·«조회 결과입니다» 같은 군더더기를 붙이지 마라."
)


# ---------------------------------------------------------------------------
# Tool wrapper — item_name → item_id resolve + allowlist(결정론) → 기존 Tool 호출
# ---------------------------------------------------------------------------


def _wrap_item_lots(
    conn: Any, *, sim_run_id: str, as_of: date, args: Mapping[str, Any]
) -> dict[str, Any]:
    name = args.get("item_name")
    item_id, status = _resolve_one(conn, name)
    if status != "ALLOWED":
        return _out_of_scope(name, status)
    answer = get_item_lots(conn, sim_run_id=sim_run_id, as_of=as_of, item_id=item_id)
    lots = answer.lots
    sellable = sum(
        (lot.uncommitted_kg for lot in lots if lot.uncommitted_kg is not None), start=Decimal(0)
    )
    return {
        "item_name": name,
        "item_id": item_id,
        "on_hand_kg": _num(sum((lot.remaining_qty_kg for lot in lots), start=Decimal(0))),
        "sellable_uncommitted_kg": _num(sellable),
        "lot_count": len(lots),
        "lots": [_lot_dict(lot) for lot in lots],
        "uncertainties": list(answer.uncertainties),
    }


def _wrap_sales_commitments(
    conn: Any, *, sim_run_id: str, as_of: date, args: Mapping[str, Any]
) -> dict[str, Any]:
    name = args.get("item_name")
    item_id, status = _resolve_one(conn, name)
    if status != "ALLOWED":
        return _out_of_scope(name, status)
    answer = get_sales_commitments(conn, sim_run_id=sim_run_id, as_of=as_of, item_id=item_id)
    return {
        "item_name": name,
        "item_id": item_id,
        "reserved_qty_kg": _num(
            sum((r.reserved_qty_kg for r in answer.live_reservations), start=Decimal(0))
        ),
        "unallocated_kg": _num(answer.unallocated_kg),
        "live_reservation_count": len(answer.live_reservations),
        "next_due_date": _jsonify(answer.next_due_date),
        "confirmed_outbound_by_date": _jsonify(dict(answer.confirmed_outbound_by_date)),
        "uncertainties": list(answer.uncertainties),
    }


def _wrap_capacity(
    conn: Any, *, sim_run_id: str, as_of: date, args: Mapping[str, Any]
) -> dict[str, Any]:
    answer = get_capacity_context(conn, sim_run_id=sim_run_id, as_of=as_of)
    return {
        "warehouse_scope": "global",
        "note": "창고 전체 기준",
        "used_kg": _num(answer.used_kg),
        "guaranteed_kg": _num(answer.guaranteed_kg),
        "available_kg": _num(answer.available_kg),
        "window_usage_ratio": _num(answer.window_usage_ratio),
        "capacity_basis": answer.capacity_basis,
        "uncertainties": list(answer.uncertainties),
    }


def _wrap_inbound(
    conn: Any, *, sim_run_id: str, as_of: date, args: Mapping[str, Any]
) -> dict[str, Any]:
    name = args.get("item_name")
    allow_ids: frozenset[str]
    if name:
        item_id, status = _resolve_one(conn, name)
        if status != "ALLOWED":
            return _out_of_scope(name, status)
        allow_ids = frozenset({item_id}) if item_id else frozenset()
    else:
        allow_ids = PROJECT_ITEM_IDS
    answer = get_inbound_schedule(conn, sim_run_id=sim_run_id, as_of=as_of)
    schedules = [
        {
            "inbound_id": fact.inbound_id,
            "item_id": fact.item_id,
            "quantity_kg": _num(fact.quantity_kg),
            "expected_arrival_date": _jsonify(fact.expected_arrival_date),
            "has_receipt": fact.has_receipt,
            "stock_applied": fact.stock_applied,
        }
        for fact in answer.schedules
        if fact.item_id in allow_ids
    ]
    return {
        "item_name": name,
        "schedules": schedules,
        "uncertainties": list(answer.uncertainties),
    }


def _wrap_exceptions(
    conn: Any, *, sim_run_id: str, as_of: date, args: Mapping[str, Any]
) -> dict[str, Any]:
    name = args.get("item_name")
    allowed_lot_ids: frozenset[str] | None = None
    if name:
        item_id, status = _resolve_one(conn, name)
        if status != "ALLOWED":
            return _out_of_scope(name, status)
        item_lots = get_item_lots(conn, sim_run_id=sim_run_id, as_of=as_of, item_id=item_id)
        allowed_lot_ids = frozenset(lot.lot_id for lot in item_lots.lots)
    answer = get_open_exceptions(conn, sim_run_id=sim_run_id, as_of=as_of)
    exceptions = []
    for fact in answer.exceptions:
        if allowed_lot_ids is not None and not (
            fact.subject_type == "LOT" and fact.subject_id in allowed_lot_ids
        ):
            continue
        exceptions.append(
            {
                "exception_id": fact.exception_id,
                "code": fact.code,
                "subject_type": fact.subject_type,
                "subject_id": fact.subject_id,
                "severity": fact.severity,
                "status": fact.status,
                "opened_as_of": _jsonify(fact.opened_as_of),
                "open_days": fact.open_days,
            }
        )
    return {
        "item_name": name,
        "exceptions": exceptions,
        "uncertainties": list(answer.uncertainties),
    }


def _wrap_policy(
    conn: Any, *, sim_run_id: str, as_of: date, args: Mapping[str, Any]
) -> dict[str, Any]:
    name = args.get("item_name")
    item_id: str | None = None
    if name:
        item_id, status = _resolve_one(conn, name)
        if status != "ALLOWED":
            return _out_of_scope(name, status)
    view = get_policy(conn, sim_run_id=sim_run_id, as_of=as_of, item_id=item_id)
    return {
        "item_name": name,
        "policy_version": view.policy_version,
        "agent_policy": _jsonify(dict(view.agent_policy)),
        "item_policy": None if view.item_policy is None else _jsonify(vars(view.item_policy)),
        "uncertainties": list(view.uncertainties),
    }


def _wrap_lot(
    conn: Any, *, sim_run_id: str, as_of: date, args: Mapping[str, Any]
) -> dict[str, Any]:
    lot_id = args.get("lot_id")
    if not lot_id:
        return {"status": "LOT_ID_REQUIRED", "message": "lot_id 가 필요합니다."}
    view = get_lot(conn, sim_run_id=sim_run_id, as_of=as_of, lot_id=lot_id)
    if view.lot is None:
        return {
            "lot_id": lot_id,
            "status": "LOT_NOT_FOUND",
            "uncertainties": list(view.uncertainties),
        }
    if view.lot.item_id not in PROJECT_ITEM_IDS:
        return {
            "lot_id": lot_id,
            "item_id": view.lot.item_id,
            "status": "NOT_IN_PROJECT_SCOPE",
            "message": "현재 프로젝트의 조회 대상 품목이 아닙니다.",
        }
    return {"lot_id": lot_id, **_lot_dict(view.lot), "item_id": view.lot.item_id}


_TOOL_EXECUTORS = {
    "get_item_lots": _wrap_item_lots,
    "get_sales_commitments": _wrap_sales_commitments,
    "get_capacity_context": _wrap_capacity,
    "get_inbound_schedule": _wrap_inbound,
    "get_open_exceptions": _wrap_exceptions,
    "get_policy": _wrap_policy,
    "get_lot": _wrap_lot,
}


def run_tool(
    name: str, arguments: Mapping[str, Any], *, conn: Any, sim_run_id: str, as_of: date
) -> dict[str, Any]:
    """LLM 이 부른 tool 하나를 실행한다. 🔴 결과·오류를 **LLM 이 읽을 dict** 로 돌려준다.

    ★ 알 수 없는 tool·실행 오류는 예외로 던지지 않고 사실로 돌려준다 — LLM 이 보고
      다른 Tool 을 부르거나 그 사실을 설명할 수 있게 한다.
    """
    executor = _TOOL_EXECUTORS.get(name)
    if executor is None:
        return {"error": "UNKNOWN_TOOL", "tool": name}
    try:
        return executor(conn, sim_run_id=sim_run_id, as_of=as_of, args=arguments or {})
    except Exception as error:  # noqa: BLE001 — 한 Tool 실패가 loop 를 무너뜨리지 않는다
        return {"error": "TOOL_FAILED", "tool": name, "detail": repr(error)}


# ---------------------------------------------------------------------------
# Orchestration — LLM tool-calling loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class StatusQueryAnswer:
    payload: Mapping[str, Any]
    missing_data: tuple[str, ...] = ()
    reasoning: str = ""


def answer_status_question(
    *,
    question: str,
    sim_run_id: str,
    as_of: date,
    chat: Chat | None = None,
    conn: Any = None,
) -> StatusQueryAnswer:
    """질문형 STATUS_QUERY 를 **LLM tool-calling loop** 로 처리한다.

    :param chat: LLM 전송 seam(기본 = 설정된 provider). 🔴 테스트는 가짜 chat 을 준다.
    :param conn: 이미 열린 커넥션(테스트/재사용). 없으면 이 함수가 하나 연다.
    """
    llm = chat if chat is not None else build_chat()
    own_connection = conn is None
    connection = conn if conn is not None else get_connection()
    try:
        return _run_loop(question, sim_run_id=sim_run_id, as_of=as_of, chat=llm, conn=connection)
    finally:
        if own_connection:
            connection.close()


def _run_loop(
    question: str, *, sim_run_id: str, as_of: date, chat: Chat, conn: Any
) -> StatusQueryAnswer:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_trace: list[dict[str, Any]] = []
    tool_calls_made = 0

    for _ in range(MAX_TURNS):
        turn: AssistantTurn = chat(messages, TOOL_SCHEMAS)
        if not turn.tool_calls:
            return _final_answer(
                question, as_of=as_of, final_text=turn.text or "", tool_trace=tool_trace
            )

        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    # 🔴 `raw`(공급자 원본 파트)를 함께 나른다 — Gemini 는 되돌려줄 때
                    #    `thoughtSignature` 를 그대로 요구한다(재구성하면 400).
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "raw": call.raw,
                    }
                    for call in turn.tool_calls
                ],
            }
        )
        for call in turn.tool_calls:
            if tool_calls_made >= MAX_TOOL_CALLS:
                result: dict[str, Any] = {"error": "TOOL_BUDGET_EXCEEDED", "tool": call.name}
            else:
                tool_calls_made += 1
                result = _run_one_tool(call, conn=conn, sim_run_id=sim_run_id, as_of=as_of)
            tool_trace.append(
                {"tool": call.name, "arguments": dict(call.arguments), "result": result}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    raise StatusQueryLLMError(
        f"tool-calling 이 {MAX_TURNS}턴 안에 최종 답을 못 냈다", kind="NO_FINAL_ANSWER"
    )


def _run_one_tool(call: ToolCall, *, conn: Any, sim_run_id: str, as_of: date) -> dict[str, Any]:
    if call.name not in _ALLOWED_TOOL_NAMES:
        return {"error": "UNKNOWN_TOOL", "tool": call.name}
    return run_tool(call.name, call.arguments, conn=conn, sim_run_id=sim_run_id, as_of=as_of)


def _final_answer(
    question: str, *, as_of: date, final_text: str, tool_trace: Sequence[Mapping[str, Any]]
) -> StatusQueryAnswer:
    tools_used = [entry["tool"] for entry in tool_trace]
    excluded = [
        entry["arguments"].get("item_name")
        for entry in tool_trace
        if isinstance(entry["result"], Mapping)
        and entry["result"].get("status") == "NOT_IN_PROJECT_SCOPE"
    ]
    failed = [
        entry["tool"]
        for entry in tool_trace
        if isinstance(entry["result"], Mapping) and entry["result"].get("error")
    ]
    # 🔴 답 전체를 한 Mapping 키 아래 둔다 — 최상위에 숫자를 두지 않아 근거(evidence)
    #    요구를 만들지 않는다. 숫자의 정본은 tool_trace 의 Tool 결과다.
    payload = {
        "status_query": {
            "question": question,
            "as_of": as_of.isoformat(),
            "answer": final_text,
            "tools_used": tools_used,
            "tool_trace": list(tool_trace),
            "excluded_items": [name for name in excluded if name],
        }
    }
    missing: list[str] = []
    if excluded:
        missing.append("excluded_items")
    if failed:
        missing.extend(f"tool_failed:{name}" for name in dict.fromkeys(failed))
    reasoning = f"질문형 물류 상태를 조회했다 (Tool {len(tools_used)}회)."
    return StatusQueryAnswer(payload=payload, missing_data=tuple(missing), reasoning=reasoning)
