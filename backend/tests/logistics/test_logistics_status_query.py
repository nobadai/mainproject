"""질문형 STATUS_QUERY — **LLM tool calling** orchestration (Issue #789 · LOG-MDS-004).

★ 운영 구조는 실제 provider function calling 이다. 테스트는 **가짜 LLM(chat)** 이
  tool_call 을 반환하게 해 orchestration 을 검증한다 — 네트워크를 타지 않는다.
  provider wire 파싱은 `_post` 대역으로 별도 검증한다. `@pytest.mark.db` 하나만 실 DB.
"""

from __future__ import annotations

import types
from datetime import date
from decimal import Decimal

import pytest

from app.logistics import adapter
from app.logistics.query import llm as sqllm
from app.logistics.query import status_query as sq
from app.logistics.query.llm import AssistantTurn, StatusQueryLLMError, ToolCall
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply

AS_OF = date(2026, 1, 1)


# ---------------------------------------------------------------------------
# 대역
# ---------------------------------------------------------------------------


def _lot(lot_id="LOT-1", *, remaining="100", uncommitted="80", freshness=5):
    return types.SimpleNamespace(
        lot_id=lot_id,
        remaining_qty_kg=Decimal(remaining),
        uncommitted_kg=None if uncommitted is None else Decimal(uncommitted),
        remaining_freshness_days=freshness,
        sell_priority=True,
        disposal_candidate=False,
        turnover_status="SELL_PRIORITY",
        status="ACTIVE",
    )


def _item_lots(*lots):
    return types.SimpleNamespace(lots=list(lots), uncertainties=())


class _FakeChat:
    """스크립트대로 AssistantTurn 을 돌려주는 가짜 LLM. 각 턴의 입력을 기록한다."""

    def __init__(self, *turns):
        self._turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def __call__(self, messages, tools):
        self.calls.append(([dict(m) for m in messages], list(tools)))
        return self._turns.pop(0)


def _tool_turn(*calls):
    return AssistantTurn(
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)]
    )


def _text_turn(text):
    return AssistantTurn(tool_calls=[], text=text)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.params = params

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def _allow_baechu(conn, names):
    n = names[0]
    if n == "배추":
        return sq.ResolvedItems(allowed={"배추": "ITEM-BAECHU"}, excluded={}, not_found=())
    if n == "피마늘":
        return sq.ResolvedItems(allowed={}, excluded={"피마늘": "ITEM-PIMANUL"}, not_found=())
    return sq.ResolvedItems(allowed={}, excluded={}, not_found=(n,))


# ---------------------------------------------------------------------------
# 1. tool calling loop — LLM 이 Tool 을 고르고, 실행되고, 결과가 다시 LLM 으로 간다
# ---------------------------------------------------------------------------


def test_single_tool_call_and_result_fed_back(monkeypatch):
    monkeypatch.setattr(
        sq, "get_capacity_context",
        lambda conn, *, sim_run_id, as_of: types.SimpleNamespace(
            used_kg=Decimal(1000), guaranteed_kg=Decimal(8000), available_kg=Decimal(7000),
            window_usage_ratio=Decimal("0.2"), capacity_basis="CURRENT_ACTIVE_POLICY",
            uncertainties=(),
        ),
    )
    chat = _FakeChat(
        _tool_turn(("get_capacity_context", {})),
        _text_turn("창고 전체 기준 여유는 7000kg입니다."),
    )
    answer = sq.answer_status_question(
        question="창고 여유 알려줘", sim_run_id="SIM", as_of=AS_OF, chat=chat, conn=object()
    )
    body = answer.payload["status_query"]
    # LLM 이 Tool 을 선택했고, 실행됐고, 최종 답이 나왔다.
    assert body["tools_used"] == ["get_capacity_context"]
    assert body["answer"] == "창고 전체 기준 여유는 7000kg입니다."
    # 🔴 Tool schema 가 LLM 에 전달됐다 — estimate_action_impact 는 없다.
    tool_names = {t["name"] for t in chat.calls[0][1]}
    assert "get_capacity_context" in tool_names
    assert "estimate_action_impact" not in tool_names
    # 🔴 Tool 결과가 다시 LLM 으로 들어갔다 (둘째 턴 메시지에 role=tool).
    second_turn_roles = [m["role"] for m in chat.calls[1][0]]
    assert "tool" in second_turn_roles
    # 숫자는 Tool 결과가 정본 — tool_trace 에 남는다.
    assert body["tool_trace"][0]["result"]["available_kg"] == 7000.0
    assert body["tool_trace"][0]["result"]["warehouse_scope"] == "global"


def test_multiple_tool_calls_in_one_turn(monkeypatch):
    monkeypatch.setattr(sq, "resolve_item_names", _allow_baechu)
    monkeypatch.setattr(
        sq, "get_item_lots",
        lambda conn, *, sim_run_id, as_of, item_id: _item_lots(_lot(f"LOT-{item_id}")),
    )
    monkeypatch.setattr(
        sq, "get_sales_commitments",
        lambda conn, *, sim_run_id, as_of, item_id: types.SimpleNamespace(
            live_reservations=[types.SimpleNamespace(reserved_qty_kg=Decimal(500))],
            unallocated_kg=Decimal(0), next_due_date=None, confirmed_outbound_by_date={},
            uncertainties=(),
        ),
    )
    chat = _FakeChat(
        _tool_turn(
            ("get_item_lots", {"item_name": "배추"}),
            ("get_sales_commitments", {"item_name": "배추"}),
        ),
        _text_turn("배추 재고 100kg, 예약 500kg입니다."),
    )
    answer = sq.answer_status_question(
        question="배추 재고랑 예약 알려줘", sim_run_id="SIM", as_of=AS_OF, chat=chat, conn=object()
    )
    body = answer.payload["status_query"]
    assert body["tools_used"] == ["get_item_lots", "get_sales_commitments"]
    assert body["tool_trace"][0]["result"]["item_id"] == "ITEM-BAECHU"
    assert body["tool_trace"][1]["result"]["reserved_qty_kg"] == 500.0


def test_additional_tool_call_after_seeing_result(monkeypatch):
    monkeypatch.setattr(sq, "resolve_item_names", _allow_baechu)
    monkeypatch.setattr(
        sq, "get_item_lots",
        lambda conn, *, sim_run_id, as_of, item_id: _item_lots(_lot()),
    )
    monkeypatch.setattr(
        sq, "get_sales_commitments",
        lambda conn, *, sim_run_id, as_of, item_id: types.SimpleNamespace(
            live_reservations=[], unallocated_kg=Decimal(0), next_due_date=None,
            confirmed_outbound_by_date={}, uncertainties=(),
        ),
    )
    chat = _FakeChat(
        _tool_turn(("get_item_lots", {"item_name": "배추"})),
        _tool_turn(("get_sales_commitments", {"item_name": "배추"})),  # 결과 본 뒤 추가 호출
        _text_turn("완료"),
    )
    answer = sq.answer_status_question(
        question="배추 상태 알려줘", sim_run_id="SIM", as_of=AS_OF, chat=chat, conn=object()
    )
    # 3턴이 돌았고, 둘째 턴은 첫 Tool 결과를 본 뒤에 호출됐다.
    assert len(chat.calls) == 3
    assert "tool" in [m["role"] for m in chat.calls[1][0]]
    assert answer.payload["status_query"]["tools_used"] == [
        "get_item_lots", "get_sales_commitments"
    ]


# ---------------------------------------------------------------------------
# 2. resolver — LLM 은 item_name 만, 내부 item_id 는 결정론 코드가 확정
# ---------------------------------------------------------------------------


def test_llm_sends_item_name_wrapper_resolves_item_id(monkeypatch):
    monkeypatch.setattr(sq, "resolve_item_names", _allow_baechu)
    captured: dict = {}

    def spy_item_lots(conn, *, sim_run_id, as_of, item_id):
        captured["item_id"] = item_id
        return _item_lots(_lot())

    monkeypatch.setattr(sq, "get_item_lots", spy_item_lots)
    chat = _FakeChat(
        _tool_turn(("get_item_lots", {"item_name": "배추"})),
        _text_turn("배추 재고 안내"),
    )
    sq.answer_status_question(
        question="배추 재고", sim_run_id="SIM", as_of=AS_OF, chat=chat, conn=object()
    )
    # 🔴 LLM 은 item_name="배추" 만 냈고, 결정론 resolver 가 item_id 를 확정해 기존 Tool 에 넘겼다.
    assert captured["item_id"] == "ITEM-BAECHU"


def test_llm_never_emits_item_id_only_item_name():
    # tool schema 에 item_id 속성이 없다 — LLM 이 낼 수 없다.
    item_tools = {t["name"]: t for t in sq.TOOL_SCHEMAS}
    for name in ("get_item_lots", "get_sales_commitments"):
        props = item_tools[name]["parameters"]["properties"]
        assert "item_name" in props
        assert "item_id" not in props


# ---------------------------------------------------------------------------
# 3. 제외 품목 — 실패가 아니라 «조회 대상 아님» 사실을 LLM 에 돌려준다
# ---------------------------------------------------------------------------


def test_excluded_item_returns_out_of_scope_fact(monkeypatch):
    monkeypatch.setattr(sq, "resolve_item_names", _allow_baechu)
    chat = _FakeChat(
        _tool_turn(("get_item_lots", {"item_name": "피마늘"})),
        _text_turn("피마늘은 현재 프로젝트의 조회 대상 품목이 아닙니다."),
    )
    answer = sq.answer_status_question(
        question="피마늘 재고 알려줘", sim_run_id="SIM", as_of=AS_OF, chat=chat, conn=object()
    )
    body = answer.payload["status_query"]
    result = body["tool_trace"][0]["result"]
    assert result["status"] == "NOT_IN_PROJECT_SCOPE"
    # 그 사실이 다시 LLM 으로 들어갔다.
    tool_msg = next(m for m in chat.calls[1][0] if m["role"] == "tool")
    assert "NOT_IN_PROJECT_SCOPE" in tool_msg["content"]
    assert body["excluded_items"] == ["피마늘"]
    assert "excluded_items" in answer.missing_data


def test_mixed_normal_and_excluded_does_not_fail_whole(monkeypatch):
    monkeypatch.setattr(sq, "resolve_item_names", _allow_baechu)
    monkeypatch.setattr(
        sq, "get_item_lots",
        lambda conn, *, sim_run_id, as_of, item_id: _item_lots(_lot(f"LOT-{item_id}")),
    )
    chat = _FakeChat(
        _tool_turn(
            ("get_item_lots", {"item_name": "배추"}),
            ("get_item_lots", {"item_name": "피마늘"}),
        ),
        _text_turn("배추 재고와 피마늘 제외 안내"),
    )
    answer = sq.answer_status_question(
        question="배추랑 피마늘 재고", sim_run_id="SIM", as_of=AS_OF, chat=chat, conn=object()
    )
    trace = answer.payload["status_query"]["tool_trace"]
    assert trace[0]["result"]["item_id"] == "ITEM-BAECHU"  # 정상 조회
    assert trace[1]["result"]["status"] == "NOT_IN_PROJECT_SCOPE"  # 제외 안내


# ---------------------------------------------------------------------------
# 4. run_tool 단위 — wrapper 경계
# ---------------------------------------------------------------------------


def test_run_tool_capacity_is_warehouse_wide(monkeypatch):
    monkeypatch.setattr(
        sq, "get_capacity_context",
        lambda conn, *, sim_run_id, as_of: types.SimpleNamespace(
            used_kg=Decimal(1000), guaranteed_kg=Decimal(8000), available_kg=Decimal(7000),
            window_usage_ratio=Decimal("0.2"), capacity_basis="X", uncertainties=(),
        ),
    )
    result = sq.run_tool("get_capacity_context", {}, conn=object(), sim_run_id="SIM", as_of=AS_OF)
    assert result["warehouse_scope"] == "global"
    assert result["note"] == "창고 전체 기준"


def test_run_tool_unknown_tool_is_fact_not_exception():
    result = sq.run_tool("get_bogus", {}, conn=object(), sim_run_id="SIM", as_of=AS_OF)
    assert result == {"error": "UNKNOWN_TOOL", "tool": "get_bogus"}


def test_run_tool_inbound_drops_excluded_items(monkeypatch):
    def fake_inbound(conn, *, sim_run_id, as_of):
        return types.SimpleNamespace(
            schedules=[
                types.SimpleNamespace(
                    inbound_id="INB-1", item_id="ITEM-BAECHU", quantity_kg=Decimal(500),
                    expected_arrival_date=date(2026, 1, 2), has_receipt=False, stock_applied=False,
                ),
                types.SimpleNamespace(
                    inbound_id="INB-2", item_id="ITEM-PIMANUL", quantity_kg=Decimal(300),
                    expected_arrival_date=date(2026, 1, 2), has_receipt=False, stock_applied=False,
                ),
            ],
            uncertainties=(),
        )

    monkeypatch.setattr(sq, "get_inbound_schedule", fake_inbound)
    result = sq.run_tool("get_inbound_schedule", {}, conn=object(), sim_run_id="SIM", as_of=AS_OF)
    assert [s["item_id"] for s in result["schedules"]] == ["ITEM-BAECHU"]


def test_loop_without_final_answer_raises(monkeypatch):
    always_tool = AssistantTurn(
        tool_calls=[ToolCall(id="c", name="get_capacity_context", arguments={})]
    )
    monkeypatch.setattr(
        sq, "get_capacity_context",
        lambda conn, *, sim_run_id, as_of: types.SimpleNamespace(
            used_kg=Decimal(0), guaranteed_kg=Decimal(0), available_kg=Decimal(0),
            window_usage_ratio=None, capacity_basis="X", uncertainties=(),
        ),
    )

    def never_stops(messages, tools):
        return always_tool

    with pytest.raises(StatusQueryLLMError):
        sq.answer_status_question(
            question="끝없이", sim_run_id="SIM", as_of=AS_OF, chat=never_stops, conn=object()
        )


# ---------------------------------------------------------------------------
# 5. resolver — DB 정본 (allowlist gate)
# ---------------------------------------------------------------------------


def test_resolver_partitions(monkeypatch):
    monkeypatch.setattr(sq, "get_db_schema", lambda: "haetdeul")
    rows = [
        {"item_id": "ITEM-BAECHU", "item_name": "배추"},
        {"item_id": "ITEM-PIMANUL", "item_name": "피마늘"},
    ]
    resolved = sq.resolve_item_names(_FakeConn(rows), ["배추", "피마늘", "감자", "배추"])
    assert resolved.allowed == {"배추": "ITEM-BAECHU"}
    assert resolved.excluded == {"피마늘": "ITEM-PIMANUL"}
    assert resolved.not_found == ("감자",)


@pytest.mark.db
def test_resolver_real_db_mapping():
    from app.logistics.db import get_connection

    with get_connection() as conn:
        resolved = sq.resolve_item_names(
            conn, ["배추", "무", "양파", "피마늘", "건고추", "감자없음"]
        )
    assert resolved.allowed == {"배추": "ITEM-BAECHU", "무": "ITEM-MU", "양파": "ITEM-YANGPA"}
    assert set(resolved.excluded) == {"피마늘", "건고추"}
    assert resolved.not_found == ("감자없음",)


# ---------------------------------------------------------------------------
# 6. provider function-calling wire — tool schema 전달 + tool_call 파싱
# ---------------------------------------------------------------------------


def _fake_settings(provider="ollama"):
    return types.SimpleNamespace(
        enabled=True, provider=provider, model="m", base_url="http://x", timeout_seconds=5
    )


def test_ollama_chat_sends_tools_and_parses_tool_calls(monkeypatch):
    captured: dict = {}

    def fake_post(url, *, body, headers, timeout):
        captured["url"] = url
        captured["body"] = body
        return {"message": {"tool_calls": [
            {"function": {"name": "get_item_lots", "arguments": {"item_name": "배추"}}}
        ]}}

    monkeypatch.setattr(sqllm, "_post", fake_post)
    turn = sqllm._ollama_chat(
        _fake_settings(), [{"role": "user", "content": "배추 재고"}], list(sq.TOOL_SCHEMAS)
    )
    # 🔴 tool schema 를 provider 에 전달했다.
    assert "tools" in captured["body"]
    assert {t["function"]["name"] for t in captured["body"]["tools"]} >= {"get_item_lots"}
    # 🔴 provider 의 tool_calls 를 ToolCall 로 파싱했다.
    assert turn.tool_calls[0].name == "get_item_lots"
    assert turn.tool_calls[0].arguments == {"item_name": "배추"}
    assert turn.text is None


def test_ollama_chat_parses_final_text(monkeypatch):
    monkeypatch.setattr(
        sqllm, "_post", lambda url, *, body, headers, timeout: {"message": {"content": "최종 답"}}
    )
    turn = sqllm._ollama_chat(_fake_settings(), [{"role": "user", "content": "x"}], [])
    assert turn.tool_calls == []
    assert turn.text == "최종 답"


def test_gemini_chat_parses_function_call(monkeypatch):
    monkeypatch.setenv("LOGISTICS_GEMINI_API_KEY", "k")
    captured: dict = {}

    def fake_post(url, *, body, headers, timeout):
        captured["body"] = body
        return {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "get_capacity_context", "args": {}}}
        ]}}]}

    monkeypatch.setattr(sqllm, "_post", fake_post)
    turn = sqllm._gemini_chat(
        _fake_settings("gemini"), [{"role": "user", "content": "창고 여유"}], list(sq.TOOL_SCHEMAS)
    )
    assert "tools" in captured["body"]
    assert turn.tool_calls[0].name == "get_capacity_context"


def test_build_chat_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(sqllm, "get_llm_settings", lambda: _fake_settings_disabled())
    with pytest.raises(StatusQueryLLMError) as exc:
        sqllm.build_chat()
    assert exc.value.kind == "DISABLED"


def _fake_settings_disabled():
    s = _fake_settings()
    s.enabled = False
    return s


def test_build_chat_routes_provider_and_parses(monkeypatch):
    monkeypatch.setattr(sqllm, "get_llm_settings", lambda: _fake_settings("ollama"))
    monkeypatch.setattr(
        sqllm, "_post",
        lambda url, *, body, headers, timeout: {"message": {"content": "ok"}},
    )
    chat = sqllm.build_chat()
    turn = chat([{"role": "user", "content": "x"}], list(sq.TOOL_SCHEMAS))
    assert turn.text == "ok"


# ---------------------------------------------------------------------------
# 7. 어댑터 분기 (Overview 회귀는 test_logistics_adapter 가 덮는다)
# ---------------------------------------------------------------------------


def _req(payload):
    ctx = ExecutionContext(
        request_id="REQ-1", as_of=AS_OF, trigger="USER_REQUEST",
        policy_version="POLICY-V1", sim_run_id="SIM-1",
    )
    return AgentRequest(context=ctx, agent="inventory", mode="STATUS_QUERY", payload=payload)


def test_adapter_routes_question(monkeypatch):
    captured: dict = {}

    def fake_answer(*, question, sim_run_id, as_of):
        captured.update(question=question, sim_run_id=sim_run_id, as_of=as_of)
        return sq.StatusQueryAnswer(
            payload={"status_query": {"answer": "ok", "tool_trace": []}},
            missing_data=(), reasoning="r",
        )

    monkeypatch.setattr(adapter, "answer_status_question", fake_answer)
    request = _req({"question": "배추 재고 얼마야?"})
    reply, meta = adapter.logistics_port(request)
    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert captured == {"question": "배추 재고 얼마야?", "sim_run_id": "SIM-1", "as_of": AS_OF}
    assert validate_reply(request, reply, meta) == ()


def test_adapter_empty_payload_is_overview(monkeypatch):
    def fail(**kwargs):
        raise AssertionError("빈 질문은 Overview 로 가야 한다")

    monkeypatch.setattr(adapter, "answer_status_question", fail)
    monkeypatch.setattr(adapter, "_load_read", lambda *, as_of, sim_run_id: None)
    reply, _ = adapter.logistics_port(_req({}))
    assert reply.runtime_status == "RUNTIME_NOT_READY"


def test_adapter_llm_failure_is_error(monkeypatch):
    def boom(**kwargs):
        raise StatusQueryLLMError("provider down")

    monkeypatch.setattr(adapter, "answer_status_question", boom)
    reply, _ = adapter.logistics_port(_req({"question": "배추 재고"}))
    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"


def test_gemini_tool_call_keeps_the_raw_part(monkeypatch):
    """🔴 Gemini 3.x 는 `functionCall` 파트의 `thoughtSignature` 를 **그대로** 돌려받아야
    한다 — 우리가 name·arguments 로 재구성하면 다음 턴이 400 으로 거절된다(실측).
    """
    monkeypatch.setattr(
        sqllm, "_post",
        lambda url, *, body, headers, timeout: {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "get_item_lots", "args": {"item_name": "배추"}},
             "thoughtSignature": "SIG-XYZ"}
        ]}}]},
    )
    monkeypatch.setenv("LOGISTICS_GEMINI_API_KEY", "k")
    turn = sqllm._gemini_chat(
        types.SimpleNamespace(model="m", timeout_seconds=5),
        [{"role": "user", "content": "배추 재고"}],
        list(sq.TOOL_SCHEMAS),
    )
    assert turn.tool_calls[0].raw["thoughtSignature"] == "SIG-XYZ"


def test_gemini_replays_the_raw_part_verbatim():
    """되돌려주는 model 턴이 원본 파트 그대로여야 서명이 살아 있다."""
    part = {"functionCall": {"name": "get_item_lots", "args": {"item_name": "배추"}},
            "thoughtSignature": "SIG-XYZ"}
    content = sqllm._to_gemini_content(
        {"role": "assistant", "tool_calls": [
            {"id": "c0", "name": "get_item_lots", "arguments": {"item_name": "배추"}, "raw": part}
        ]}
    )
    assert content["role"] == "model"
    assert content["parts"] == [part]


def test_gemini_falls_back_when_no_raw_part():
    """`raw` 가 없으면(예: 옛 기록) name·arguments 로 재구성한다 — 죽지 않는다."""
    content = sqllm._to_gemini_content(
        {"role": "assistant", "tool_calls": [
            {"id": "c0", "name": "get_item_lots", "arguments": {"item_name": "배추"}, "raw": None}
        ]}
    )
    assert content["parts"] == [
        {"functionCall": {"name": "get_item_lots", "args": {"item_name": "배추"}}}
    ]


def test_system_prompt_forbids_internal_metadata_in_the_answer():
    """🔴 최종 답변은 사용자에게 그대로 보인다 — 내부 metadata 노출 금지 규율이
    프롬프트에서 조용히 빠지지 않게 잠근다(#789 답변 품질).
    """
    prompt = sq.SYSTEM_PROMPT
    for 금지 in ("tool_trace", "tools_used", "item_id", "NOT_IN_PROJECT_SCOPE", "warehouse_scope"):
        assert 금지 in prompt, f"{금지} 노출 금지 규칙이 프롬프트에 없다"
    assert "노출하지 마라" in prompt
    assert "2~5문장" in prompt
