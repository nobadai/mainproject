"""가격 예측(`ml`) 상태 조회 배선 — 마스터 쪽.

ML 포트(`app/ml/adapter.py`)는 아직 dev 에 없다. 여기서는 **가짜 ML 포트**를 등록소에
꽂아 마스터가 지는 몫만 잠근다.

```text
(a) ml 은 STATUS_QUERY 를 {"question": 원문, "item": 품목} 으로 받는다
(b) finance · inventory 는 여전히 빈 payload 다 (수신 계약 불변)
(c) answer_markdown 은 사실 줄로 펴지 않고 본문 그대로 나간다 · ⑥ 을 안 부른다
(d) ml 이 없어도 매입 · 판매 필수 어댑터 검사는 통과한다
```

★ (e) 규칙 분류 경로는 dev 에 없다 (분류는 LLM ① 하나뿐이다). 그래서 검사하지 않는다.
★ 실 LLM · DB 를 타지 않는다. 적재(`persistence.record_status`)는 대역으로 막는다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.contracts.core import ContractViolation
from app.master import AgentReply, AgentRequest, ExecutionMetadata, wiring
from app.master.ask_schemas import AskRequest
from app.master.ask_service import ask
from app.master.envelope import ExecutionContext, agent_allowed_modes, agent_dept
from app.master.llm.runtime import IntentService, LLMSettings, validate_intent
from app.master.llm.schemas import NarrativeResult
from app.master.router import router

AS_OF = "2026-08-27"
질문 = "내일 배추 경락가 얼마야?"
본문 = "## 배추 경락가 전망\n\n- **내일** 1,650원/kg 안팎\n\n> 참고용 예측입니다."

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


class _FakeProvider:
    def __init__(self, response: str) -> None:
        self.response = response

    def generate(self, system: str, user: str, schema: dict) -> str:
        del system, user, schema
        return self.response


class _CountingNarrator:
    """⑥ 대역. 불렸는지만 센다."""

    def __init__(self) -> None:
        self.calls = 0

    def write(self, facts) -> NarrativeResult:
        del facts
        self.calls += 1
        return NarrativeResult(narrative="가격 예측이 답했습니다.", llm_status="SUCCESS")


def _intent_json(**kw: Any) -> str:
    base: dict[str, Any] = {"action": "STATUS_QUERY", "agents": [], "confidence": "HIGH"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def _recording_port(seen: list[AgentRequest], payload: dict[str, Any]):
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


_ML_PAYLOAD: dict[str, Any] = {
    "answer_markdown": 본문,
    "answer_status": "ANSWERED",
    "item": "배추",
    "as_of": AS_OF,
    "forecasts": [{"date": "2026-08-28", "price": 1650}],
    "model_version": "v-test",
    "forecast_source": "MODEL",
    "use_recommended": True,
    "out_of_range_note": None,
}


@pytest.fixture(autouse=True)
def clean_wiring(monkeypatch: pytest.MonkeyPatch):
    wiring.reset()
    # 조회 적재는 DB 를 친다 — 여기서는 막는다.
    monkeypatch.setattr("app.master.ask_service.persistence.record_status", lambda **kw: None)
    yield
    wiring.reset()


def _ask(utterance: str, response: str, narrator=None):
    return ask(
        AskRequest(utterance=utterance, as_of=AS_OF, policy_version="v1.3"),
        service=IntentService(SETTINGS, _FakeProvider(response)),
        narrator=narrator,
    )


# ── 봉투 ────────────────────────────────────────────────────────────────


def test_ml_은_상태_조회만_받는다():
    assert agent_allowed_modes("ml") == frozenset({"STATUS_QUERY"})
    # 조언자가 아니다 — 밴드 성립에 끼면 없는 의존이 생긴다.
    assert agent_dept("ml") is None


def test_ml_에_사이클_mode_를_보내면_봉투가_막는다():
    context = ExecutionContext(
        request_id="REQ-20260827-0001",
        as_of=date.fromisoformat(AS_OF),
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )
    with pytest.raises(ContractViolation):
        AgentRequest(context=context, agent="ml", mode="PRE_PURCHASE", call_seq=1)
    ok = AgentRequest(
        context=context,
        agent="ml",
        mode="STATUS_QUERY",
        call_seq=1,
        payload={"question": 질문, "item": "배추"},
    )
    assert ok.payload["question"] == 질문


def test_분류_검증이_ml_조회를_받는다():
    intent = validate_intent(_intent_json(agents=["ml"], item="배추"), 질문)
    assert intent.agents == ["ml"]


# ── (a)(b) payload ──────────────────────────────────────────────────────


def test_ml_은_질문과_품목을_받고_다른_부서는_빈_payload_다():
    seen: list[AgentRequest] = []
    wiring.register("ml", _recording_port(seen, _ML_PAYLOAD))
    wiring.register("finance", _recording_port(seen, {"available_cash": 7}))
    wiring.register("inventory", _recording_port(seen, {"warehouse_free_kg": 10}))

    result = _ask(질문, _intent_json(agents=["ml", "finance", "inventory"], item="배추"))

    assert result.outcome == "STATUS_ANSWERED"
    by_agent = {r.agent: r for r in seen}
    assert dict(by_agent["ml"].payload) == {"question": 질문, "item": "배추"}
    assert dict(by_agent["finance"].payload) == {}
    assert dict(by_agent["inventory"].payload) == {}


def test_품목이_없으면_item_키를_빼고_보낸다():
    seen: list[AgentRequest] = []
    wiring.register("ml", _recording_port(seen, _ML_PAYLOAD))

    _ask("내일 경락가 전망 알려줘", _intent_json(agents=["ml"]))

    assert dict(seen[0].payload) == {"question": "내일 경락가 전망 알려줘"}


# ── (c) 본문 그대로 ─────────────────────────────────────────────────────


def test_answer_markdown_은_본문_그대로_나가고_사실_줄로_펴지_않는다():
    seen: list[AgentRequest] = []
    wiring.register("ml", _recording_port(seen, _ML_PAYLOAD))
    narrator = _CountingNarrator()

    result = _ask(질문, _intent_json(agents=["ml"], item="배추"), narrator=narrator)

    assert result.answer is not None
    assert result.answer.markdown == 본문
    # 나머지 키는 사실 줄로 늘어놓지 않는다
    for key in ("forecasts", "model_version", "forecast_source", "answer_status"):
        assert key not in result.answer.text, key
    assert 본문 not in result.answer.text
    # ⑥ 이 본문을 다시 요약하지 않는다
    assert narrator.calls == 0
    assert result.answer.llm_status == "SKIPPED_TEMPLATE"
    # 구조화 값은 남는다
    assert result.status.answers["ml"]["forecasts"][0]["price"] == 1650


def test_다른_부서_조회는_종전대로_마크다운이_없다():
    seen: list[AgentRequest] = []
    wiring.register("finance", _recording_port(seen, {"available_cash": 7}))

    result = _ask("지금 자금 상황 알려줘", _intent_json(agents=["finance"]))

    assert result.answer.markdown is None
    assert "가용 현금" in result.answer.text


# ── 확인을 거친 조회 (/ask/execute) ─────────────────────────────────────


def _execute_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "intent": {
            "action": "STATUS_QUERY",
            "agents": ["ml"],
            "item": "배추",
            "confidence": "MEDIUM",
        },
        "as_of": AS_OF,
        "policy_version": "v1.3",
    }
    body.update(kw)
    return body


def test_확인한_조회는_화면이_돌려준_원문을_ml_에_싣는다():
    seen: list[AgentRequest] = []
    wiring.register("ml", _recording_port(seen, _ML_PAYLOAD))
    app = FastAPI()
    app.include_router(router)

    data = TestClient(app).post("/master/ask/execute", json=_execute_body(utterance=질문)).json()

    assert dict(seen[0].payload) == {"question": 질문, "item": "배추"}
    assert data["answer"]["markdown"] == 본문


def test_원문이_없으면_ml_을_부르지_않고_못_답했다고_밝힌다():
    seen: list[AgentRequest] = []
    wiring.register("ml", _recording_port(seen, _ML_PAYLOAD))
    app = FastAPI()
    app.include_router(router)

    data = TestClient(app).post("/master/ask/execute", json=_execute_body()).json()

    assert seen == []
    assert data["status"]["unavailable"] == ["ml"]
    assert data["status"]["missing_data"]["ml"] == ["질문 원문"]


# ── (d) 사이클은 ml 과 무관하다 ─────────────────────────────────────────


def test_ml_이_없어도_매입_판매_필수_어댑터_검사는_통과한다():
    assert "ml" not in wiring.REQUIRED_FOR_PROCUREMENT
    assert "ml" not in wiring.REQUIRED_FOR_SALES

    seen: list[AgentRequest] = []
    for name in ("finance", "inventory", "purchase", "sales"):
        wiring.register(name, _recording_port(seen, {}))

    assert not wiring.registry().has("ml")
    assert wiring.missing(wiring.REQUIRED_FOR_PROCUREMENT) == ()
    assert wiring.missing(wiring.REQUIRED_FOR_SALES) == ()
