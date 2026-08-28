"""발화문 입구 — `/master/ask` · `/ask/execute` · `StatusFlow`.

★ 라우터만 격리해 띄우고 포트는 스텁이다 (`test_master_api.py` 와 같은 방식).
★ LLM 은 `FakeProvider` — 네트워크를 타지 않는다.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import AgentReply, AgentRequest, ExecutionMetadata, wiring
from app.master.ask_schemas import AskRequest
from app.master.ask_service import ask
from app.master.llm.runtime import IntentService, LLMSettings
from app.master.router import router

AS_OF = "2026-08-27"

SETTINGS = LLMSettings(
    enabled=True,
    provider="fake",
    model="fake-model",
    base_url="",
    timeout_seconds=1.0,
    max_retries=0,
    max_output_tokens=512,
    effort=None,
)


class FakeProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def generate(self, system: str, user: str, schema: dict) -> str:
        del system, user, schema
        self.calls += 1
        return self.response


def intent_json(**kw) -> str:
    base = {"action": "UNKNOWN", "agents": [], "confidence": "LOW"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def svc(response: str) -> IntentService:
    return IntentService(SETTINGS, FakeProvider(response))


def _reply(request: AgentRequest, **kw) -> AgentReply:
    base = {
        "request_id": request.context.request_id,
        "as_of": request.context.as_of,
        "agent": request.agent,
        "mode": request.mode,
        "run_id": f"{request.agent.upper()}-{request.call_seq}",
        "runtime_status": "READY",
        "business_status": "ok",
    }
    base.update(kw)
    return AgentReply(**base)


def _port(payload=None, **kw):
    def port(request: AgentRequest):
        reply = _reply(request, payload=payload or {}, **kw)
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
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def ask_body(**kw) -> dict:
    base = {"utterance": "지금 자금 상황 알려줘", "as_of": AS_OF, "policy_version": "v1.3"}
    base.update(kw)
    return base


def run(utterance: str, response: str):
    return ask(
        AskRequest(utterance=utterance, as_of=AS_OF, policy_version="v1.3"),
        service=svc(response),
    )


# ── 조회는 바로 돈다 ─────────────────────────────────────────────────────


def test_상태_조회는_확인_없이_돌고_답을_담는다():
    wiring.register("finance", _port({"available_cash": 31_993_913}))
    result = run(
        "지금 자금 상황 알려줘",
        intent_json(action="STATUS_QUERY", agents=["finance"], confidence="HIGH"),
    )

    assert result.outcome == "STATUS_ANSWERED"
    assert result.status.status_code == "S1_ANSWERED"
    assert result.status.answers["finance"]["available_cash"] == 31_993_913
    assert result.confirm_required is False


def test_두_부서_중_하나가_못_답하면_부분이다():
    """**빈 답과 못 받은 답은 다르다.** 조용히 빼지 않는다."""
    wiring.register("finance", _port({"available_cash": 1}))
    wiring.register(
        "inventory",
        _port(
            {},
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            missing_data=("rental_cap_kg",),
        ),
    )
    result = run(
        "자금이랑 창고 상태 알려줘",
        intent_json(action="STATUS_QUERY", agents=["finance", "inventory"], confidence="HIGH"),
    )

    assert result.status.status_code == "S2_PARTIAL"
    assert result.status.unavailable == ["inventory"]
    assert result.status.missing_data["inventory"] == ["rental_cap_kg"]


def test_어댑터가_터진_것과_값이_없는_것을_구분한다():
    """둘 다 답을 못 받지만 **재시도 가치가 다르다** — 한 칸에 담으면 구분이 사라진다."""

    def boom(request):
        raise RuntimeError("payload 조립 실패")

    wiring.register("finance", boom)
    wiring.register(
        "inventory",
        _port(
            {},
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            missing_data=("rental_cap_kg",),
        ),
    )
    result = run(
        "자금이랑 창고 알려줘",
        intent_json(action="STATUS_QUERY", agents=["finance", "inventory"], confidence="HIGH"),
    )

    assert result.status.status_code == "S3_UNAVAILABLE"
    assert "payload 조립 실패" in result.status.errors["finance"]
    assert "finance" not in result.status.missing_data
    assert result.status.missing_data["inventory"] == ["rental_cap_kg"]
    assert "inventory" not in result.status.errors


def test_어댑터가_없으면_미등록으로_밝힌다():
    """미등록은 오류가 아니라 "그 부서가 오늘 돌지 않는다"와 같다 (§5.3)."""
    result = run(
        "자금 상황 알려줘",
        intent_json(action="STATUS_QUERY", agents=["finance"], confidence="HIGH"),
    )

    assert result.status.status_code == "S3_UNAVAILABLE"
    assert result.status.missing_data["finance"] == ["ADAPTER_NOT_REGISTERED"]


# ── 매입은 확인을 받는다 ─────────────────────────────────────────────────


def test_매입_실행은_분류만_하고_되묻는다():
    """오분류 비용이 비대칭이라 예산을 태우기 전에 확인받는다."""
    wiring.register("finance", _port({}))
    result = run(
        "오늘 배추 얼마나 사야 해?",
        intent_json(action="PROCUREMENT_RUN", item="배추", confidence="HIGH"),
    )

    assert result.outcome == "CLASSIFIED_ONLY"
    assert result.confirm_required is True
    assert result.status is None
    assert "배추" in result.clarification


def test_못_알아들으면_실행하지_않고_되묻는다():
    result = run("음... 그거 있잖아", intent_json(action="UNKNOWN", confidence="LOW"))

    assert result.outcome == "NEEDS_CLARIFICATION"
    assert result.status is None
    assert result.clarification is not None


def test_확신이_낮으면_조회도_확인을_받는다():
    wiring.register("finance", _port({}))
    result = run(
        "돈 어때?",
        intent_json(action="STATUS_QUERY", agents=["finance"], confidence="LOW"),
    )

    assert result.outcome == "CLASSIFIED_ONLY"
    assert result.status is None


# ── LLM 이 죽어도 ────────────────────────────────────────────────────────


def test_LLM_이_죽어도_200_으로_되묻는다():
    class Boom:
        def generate(self, system, user, schema):
            raise RuntimeError("키가 없다")

    result = ask(
        AskRequest(utterance="오늘 뭐 사지", as_of=AS_OF, policy_version="v1.3"),
        service=IntentService(SETTINGS, Boom()),
    )

    assert result.outcome == "NEEDS_CLARIFICATION"
    assert result.llm_status == "FALLBACK"
    assert result.llm_fallback_used is True


# ── /ask/execute ────────────────────────────────────────────────────────


def test_확인한_의도는_재분류_없이_실행된다(client):
    wiring.register("finance", _port({"available_cash": 7}))
    body = {
        "intent": {
            "action": "STATUS_QUERY",
            "agents": ["finance"],
            "confidence": "HIGH",
        },
        "as_of": AS_OF,
        "policy_version": "v1.3",
    }
    data = client.post("/master/ask/execute", json=body).json()

    assert data["outcome"] == "STATUS_ANSWERED"
    assert data["status"]["answers"]["finance"]["available_cash"] == 7
    # 이미 분류된 의도라 LLM 을 부르지 않는다
    assert data["llm_status"] == "SKIPPED_TEMPLATE"


def test_아직_배선_안_된_종류는_501(client):
    body = {
        "intent": {
            "action": "SELECT_SCENARIO",
            "agents": [],
            "scenario_label": "기본",
            "confidence": "HIGH",
        },
        "as_of": AS_OF,
        "policy_version": "v1.3",
    }
    response = client.post("/master/ask/execute", json=body)

    assert response.status_code == 501
    assert "decision" in response.json()["detail"]


# ── 라우터 ──────────────────────────────────────────────────────────────


def test_빈_발화문은_422(client):
    assert client.post("/master/ask", json=ask_body(utterance="")).status_code == 422
