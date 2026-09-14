"""「마스터에게 묻기」 조회는 **화면이 보는 실행**을 읽고, 이력은 정본 축으로 안 적는다.

2026-09-14 · 재무 보고 「자금을 물으면 기준일을 바꿔도 금액이 늘 같다」.
원인은 `ask_service._run_status` 의 봉투 축이 번인 상수였던 것이다 (`#656` 이 남긴 자리).

🔴 **실 DB 에 닿지 않는다.** 부서 포트는 대역이고 적재 함수는 가로챈다.

```text
① 조회 봉투의 실행 축 = SHOWN_SIM_RUN_ID (번인이 아니다)
② 기준일 두 개로 부르면 재무 조회에 서로 다른 as_of 가 간다
③ 조회 이력 행은 정본 축으로 안 적힌다 (None)
④ 응답 note 에 보고 있는 실행 · 기준일이 실린다
⑤ 판매 조회가 분류 사전 · 검증 · 조회 흐름에서 열려 있다
```
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.api.shown_run import SHOWN_SIM_RUN_ID
from app.master import AgentReply, AgentRequest, ExecutionMetadata, ask_service, wiring
from app.master.ask_schemas import AskExecuteRequest
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.llm.runtime import SYSTEM_PROMPT, _clarification, validate_intent
from app.master.llm.schemas import Intent

AS_OF_A = date(2026, 1, 13)
AS_OF_B = date(2026, 1, 20)


def _spy_port(seen: list[AgentRequest], payload: dict):
    def port(request: AgentRequest):
        seen.append(request)
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status="ok",
            payload=payload,
        )
        meta = ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("status_tool",),
            tool_order=(1,),
        )
        return reply, meta

    return port


@pytest.fixture(autouse=True)
def clean_wiring():
    wiring.reset()
    yield
    wiring.reset()


@pytest.fixture
def recorded(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(ask_service.persistence, "record_status", lambda **kw: calls.append(kw))
    return calls


def _status(agent: str, as_of: date):
    return ask_service.execute(
        AskExecuteRequest(
            intent=Intent(action="STATUS_QUERY", agents=[agent], confidence="HIGH"),
            as_of=as_of,
            policy_version="v1.3",
        )
    )


def test_조회_봉투는_화면이_보는_실행을_읽고_기준일을_그대로_넘긴다(recorded):
    seen: list[AgentRequest] = []
    wiring.register("finance", _spy_port(seen, {"available_cash": 1}))

    _status("finance", AS_OF_A)
    _status("finance", AS_OF_B)

    assert [r.context.sim_run_id for r in seen] == [SHOWN_SIM_RUN_ID, SHOWN_SIM_RUN_ID]
    assert SHOWN_SIM_RUN_ID != BURN_IN_SIM_RUN_ID
    assert [r.context.as_of for r in seen] == [AS_OF_A, AS_OF_B]


def test_조회_이력_행은_정본_축으로_안_적힌다(recorded):
    wiring.register("finance", _spy_port([], {"available_cash": 1}))

    _status("finance", AS_OF_A)

    assert len(recorded) == 1
    assert recorded[0]["sim_run_id"] is None
    assert recorded[0]["as_of"] == AS_OF_A


def test_응답에_보고_있는_실행과_기준일이_실린다(recorded):
    wiring.register("finance", _spy_port([], {"available_cash": 1}))

    response = _status("finance", AS_OF_B)

    assert response.outcome == "STATUS_ANSWERED"
    assert SHOWN_SIM_RUN_ID in response.note
    assert AS_OF_B.isoformat() in response.note


def test_판매는_분류_사전에_있고_검증을_통과한다():
    assert "  sales  " in SYSTEM_PROMPT
    raw = json.dumps({"action": "STATUS_QUERY", "agents": ["sales"], "confidence": "HIGH"})
    intent = validate_intent(raw, "판매 진행 상황 알려줘")
    assert intent.agents == ["sales"]
    low = Intent(action="STATUS_QUERY", agents=["sales"], confidence="LOW")
    assert "판매" in _clarification(low)


def test_판매_조회가_같은_흐름으로_답한다(recorded):
    seen: list[AgentRequest] = []
    wiring.register("sales", _spy_port(seen, {"recent_runs": []}))

    response = _status("sales", AS_OF_A)

    assert response.status.status_code == "S1_ANSWERED"
    assert [(r.agent, r.mode, r.context.sim_run_id) for r in seen] == [
        ("sales", "STATUS_QUERY", SHOWN_SIM_RUN_ID)
    ]
