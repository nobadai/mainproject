"""발화문 입구 — `/master/ask` · `/ask/execute` · `StatusFlow`.

★ 라우터만 격리해 띄우고 포트는 스텁이다 (`test_master_api.py` 와 같은 방식).
★ LLM 은 `FakeProvider` — 네트워크를 타지 않는다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import AgentReply, AgentRequest, ExecutionMetadata, decision_service, wiring
from app.master.ask_schemas import AskRequest
from app.master.ask_service import ask
from app.master.decision import DecisionOut, mark_current
from app.master.llm.runtime import IntentService, LLMSettings
from app.master.router import router
from app.master.schemas import ProcurementRunResponse

AS_OF = "2026-08-27"
#: 결정 대상 실행의 업무 키. **발화문에 없으므로 화면이 싣는다.**
TARGET = "REQ-20260827-0001"

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


@pytest.fixture
def decisions(monkeypatch):
    """결정 리포지토리를 in-memory 로 갈아 끼운다.

    ★ **공용 DB 를 건드리지 않는다.** `.env` 의 `DB_HOST` 가 팀 공용 서버라,
      라우터를 그냥 치면 실제 INSERT 가 남는다 (`test_decision.py` 와 같은 방식).
    """
    rows: list[DecisionOut] = []

    def list_decisions(request_id: str) -> list[DecisionOut]:
        return mark_current([r for r in rows if r.request_id == request_id])

    def save_decision(**kw) -> DecisionOut:
        row = DecisionOut(decision_id=uuid4(), created_at=datetime.now(UTC), **kw)
        rows.append(row)
        return row

    # 실행 이력 행의 id. **가짜가 실제를 닮아야 한다** — 이 키가 없으면 결정이
    # 실행을 가리키는 경로가 통째로 안 돈다 (2026-08-30).
    target_run_uuid = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

    def _row() -> dict:
        return {
            "run_id": target_run_uuid,
            "request_id": TARGET,
            "response_payload": {
                "end_code": "E1_APPROVED",
                "scenarios": [{"label": "보수"}, {"label": "기본"}, {"label": "공격"}],
            },
        }

    def get_run(request_id: str, *, cycle: str | None = None) -> dict:
        if request_id != TARGET:
            raise LookupError(f"실행 이력이 없다: {request_id}")
        return _row()

    def get_run_by_uuid(run_id: UUID) -> dict:
        if run_id != target_run_uuid:
            raise LookupError(f"실행 이력이 없다: {run_id}")
        return _row()

    monkeypatch.setattr(decision_service, "list_decisions", list_decisions)
    monkeypatch.setattr(decision_service, "save_decision", save_decision)
    monkeypatch.setattr(decision_service, "get_run_by_request_id", get_run)
    monkeypatch.setattr(decision_service, "get_run", get_run_by_uuid)
    return rows


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


def test_UNKNOWN_은_실행할_수_없다(client):
    """**"아직 안 만들었다" 가 아니라 "실행할 것이 없다" 다.**

    501 로 답하면 언젠가 되는 것처럼 읽힌다 — `UNKNOWN` 은 분류에 실패했다는 뜻이라
    영영 실행되지 않는다.
    """
    body = {
        "intent": {"action": "UNKNOWN", "agents": [], "confidence": "LOW"},
        "as_of": AS_OF,
        "policy_version": "v1.3",
    }
    response = client.post("/master/ask/execute", json=body)

    assert response.status_code == 422
    assert "다시 물어라" in response.json()["detail"]


# ── 말로 고른 안 ────────────────────────────────────────────────────────


def select_body(**kw) -> dict:
    base = {
        "intent": {
            "action": "SELECT_SCENARIO",
            "agents": [],
            "scenario_label": "기본",
            "confidence": "HIGH",
        },
        "as_of": AS_OF,
        "policy_version": "v1.3",
        "target_request_id": TARGET,
        "decided_by": "사장",
    }
    base.update(kw)
    return base


def test_말로_고른_안이_결정_이력에_적힌다(client, decisions):
    data = client.post("/master/ask/execute", json=select_body()).json()

    assert data["outcome"] == "DECISION_RECORDED"
    assert data["decision"]["decision"] == "APPROVE"
    assert data["decision"]["scenario_label"] == "기본"
    assert data["decision"]["decided_by"] == "사장"
    assert data["decision"]["request_id"] == TARGET
    # 이미 분류된 의도라 ①은 안 부른다
    assert data["llm_status"] == "SKIPPED_TEMPLATE"


def test_승인은_기록이지_발주가_아니라고_답에_적는다(client, decisions):
    """🔴 **안 적으면 사용자는 발주가 나간 줄 안다.**"""
    data = client.post("/master/ask/execute", json=select_body()).json()

    assert "실제 발주는 별도" in data["answer"]["text"]


def test_어느_실행인지_없으면_추측하지_않고_거절한다(client, decisions):
    """🔴 "가장 최근 실행" 으로 메우면 **엉뚱한 날의 안을 승인**할 수 있다."""
    response = client.post("/master/ask/execute", json=select_body(target_request_id=None))

    assert response.status_code == 422
    assert "target_request_id" in response.json()["detail"]


def test_승인자가_없으면_거절한다(client, decisions):
    """*"승인자가 없는 승인은 승인이 아니다"* — 말로 골랐다고 승인자가 생기지 않는다."""
    response = client.post("/master/ask/execute", json=select_body(decided_by=None))

    assert response.status_code == 422
    assert "decided_by" in response.json()["detail"]


def test_제시되지_않은_안은_화면_경로와_같은_규칙으로_막힌다(client, decisions):
    """**검사를 발화문 경로에 복제하지 않는다** — `decision_service` 가 한 곳에서 한다."""
    body = select_body()
    body["intent"]["scenario_label"] = "초공격"
    response = client.post("/master/ask/execute", json=body)

    assert response.status_code == 422
    assert "초공격" in response.json()["detail"]


def test_같은_안을_두_번_승인하면_409(client, decisions):
    assert client.post("/master/ask/execute", json=select_body()).status_code == 200
    response = client.post("/master/ask/execute", json=select_body())

    assert response.status_code == 409


def test_그_실행이_없으면_404(client, decisions):
    response = client.post("/master/ask/execute", json=select_body(target_request_id="REQ-없는것"))

    assert response.status_code == 404


# ── 라우터 ──────────────────────────────────────────────────────────────


def test_빈_발화문은_422(client):
    assert client.post("/master/ask", json=ask_body(utterance="")).status_code == 422


# ── 조건을 붙인 재요청 ──────────────────────────────────────────────────


def rerun_body(**kw) -> dict:
    base = {
        "intent": {
            "action": "RERUN_WITH_CONDITION",
            "agents": [],
            "condition": "예산 2000만원으로 낮춰서",
            "item": "배추",
            "confidence": "HIGH",
        },
        "as_of": AS_OF,
        "policy_version": "v1.3",
        "target_request_id": TARGET,
        "decided_by": "사장",
    }
    base.update(kw)
    return base


@pytest.fixture
def rerun(monkeypatch, decisions):
    """재실행을 가로챈다 — 실제 Flow 를 돌리지 않고 무엇을 넘겼는지만 본다."""
    seen: dict = {}

    def fake_run(request):
        seen["request"] = request
        return ProcurementRunResponse(
            request_id=request.request_id, as_of=request.as_of, end_code="E2_HELD", reason="..."
        )

    monkeypatch.setattr("app.master.ask_service.run_procurement", fake_run)
    monkeypatch.setattr("app.master.ask_service.link_follow_up", lambda **kw: True)
    return seen


def test_조건은_사용자의_말_그대로_매입에_넘어간다(client, rerun):
    """🔴 **숫자로 해석하지 않는다.**

    *"예산 2천만원"* 을 재무 cap 으로 꽂으면 마스터가 부서 판단을 덮어쓰는 것이 된다.
    해석은 매입이 한다 (§3.2.2).
    """
    client.post("/master/ask/execute", json=rerun_body())

    feedback = rerun["request"].prior_feedback
    assert feedback["condition_text"] == "예산 2000만원으로 낮춰서"
    assert feedback["origin_request_id"] == TARGET
    # 조건이 제약으로 둔갑하지 않았다
    assert rerun["request"].policy_values is None


def test_조건_회차는_condition_seq_로_나간다(client, rerun):
    """🔴 **`attempt` 가 아니다** (#178 · 매입 실측 2026-09-03).

    슬롯을 둘로 나누면서(계약 v0.2 §2) **안의 키 이름은 안 갈랐다.** 매입이
    `state["feedback"].get("attempt", 0)` 으로 **되먹임** 회차를 찾다가 여기 있는
    **조건** 회차를 만났다 — 틀린 값이 아니라 다른 개념이었다.

    ★ **만드는 자리에서 잰다.** `test_feedback_wiring` 은 `prior_feedback` 을 Flow 에
      직접 넣어 *"나르는가"* 만 보므로, 여기 이름을 바꿔도 그쪽은 안 깨진다 —
      변이로 확인했다. 매입이 내 `test_adjustment_scope` 에 지적한 것과 같은 구멍이다.
    """
    client.post("/master/ask/execute", json=rerun_body())

    feedback = rerun["request"].prior_feedback
    assert isinstance(feedback["condition_seq"], int)
    assert "attempt" not in feedback, "attempt 는 되먹임 슬롯이 가진다"


def test_재요청은_새_업무_키로_돌고_결정에_이어진다(client, rerun):
    data = client.post("/master/ask/execute", json=rerun_body()).json()

    assert data["outcome"] == "DECISION_RECORDED"
    assert data["decision"]["decision"] == "REQUEST_CHANGE"
    assert data["decision"]["condition_text"] == "예산 2000만원으로 낮춰서"
    # 원 실행과 다른 키로 돌고, 그 키가 결정에 이어진다
    assert data["request_id"] != TARGET
    assert data["decision"]["follow_up_request_id"] == data["request_id"]


def test_조건이_반영되지_않았다는_사실을_답에_적는다(client, rerun):
    """🔴 매입은 `prior_feedback` 을 재요청 표시로만 쓴다.

    **안 적으면 사용자는 조건이 반영된 줄 안다.** 값을 실어 주고 안 쓰는 것을 매입에
    지적해 놓고 같은 일을 조용히 할 수는 없다.
    """
    data = client.post("/master/ask/execute", json=rerun_body()).json()

    assert "아직 이 조건으로 안을 바꾸지 않습니다" in data["answer"]["text"]


def test_채팅_매입_실행은_화면_실행으로_판단한다(client, rerun):
    """🔴 **안 실으면 번인으로 떨어진다** (`service.py` `given or BURN_IN_SIM_RUN_ID`).

    2026-09-15 실측: 화면은 다른 실행을 보는데 채팅 매입이 번인 실행에 판단 22행을 쌓았다.
    매입 실행 · 조건부 재요청 두 경로 모두 화면이 보는 실행을 싣는다.
    """
    from app.api.shown_run import SHOWN_SIM_RUN_ID
    from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

    body = {
        "intent": {"action": "PROCUREMENT_RUN", "agents": [], "item": "배추", "confidence": "HIGH"},
        "as_of": AS_OF,
        "policy_version": "v1.3",
    }
    client.post("/master/ask/execute", json=body)
    first = rerun["request"]

    client.post("/master/ask/execute", json=rerun_body())
    second = rerun["request"]

    assert first is not second
    assert SHOWN_SIM_RUN_ID != BURN_IN_SIM_RUN_ID
    assert [first.sim_run_id, second.sim_run_id] == [SHOWN_SIM_RUN_ID, SHOWN_SIM_RUN_ID]


def test_조건이_비면_거절한다(client, rerun):
    body = rerun_body()
    body["intent"]["condition"] = None
    response = client.post("/master/ask/execute", json=body)

    assert response.status_code == 422
    assert "조건 없는 재요청" in response.json()["detail"]


def test_품목을_안_말하면_직전_실행에서_가져온다(client, rerun, monkeypatch):
    """*"예산 줄여서 다시 해줘"* 에는 품목이 없다. **지어내지 않고 이력을 본다.**"""
    monkeypatch.setattr(
        "app.master.ask_service.get_run_history",
        lambda rid: SimpleNamespace(request_payload={"item": "무"}),
    )
    body = rerun_body()
    body["intent"]["item"] = None
    client.post("/master/ask/execute", json=body)

    assert rerun["request"].item == "무"


def test_재요청_응답에_새로_나온_안이_실린다(client, rerun):
    """🔴 **없으면 고리가 끊긴다.**

    *"다시 해줘"* 다음 동작은 **새로 나온 안 중 하나를 고르는 것**인데, 결정만
    돌려주면 화면이 그 안을 그릴 수도 고를 수도 없다. 리포트 문장에는 있지만
    문장에서 라벨을 긁어 쓰게 하면 화면이 서버 문장 형식에 묶인다.
    """
    data = client.post("/master/ask/execute", json=rerun_body()).json()

    assert data["run"] is not None
    assert data["run"]["request_id"] == data["request_id"]
    # 결정과 새 실행이 **함께** 온다 — 한 번의 호출로 고리가 이어진다
    assert data["decision"]["follow_up_request_id"] == data["run"]["request_id"]


def test_선택_응답에는_새_실행이_없다(client, decisions):
    """안을 고른 것은 **새로 돌린 것이 아니다** — 채우면 거짓이 된다."""
    data = client.post("/master/ask/execute", json=select_body()).json()

    assert data["run"] is None


# ── DOMAIN_ACTION ───────────────────────────────────────────────────────────


def test_DOMAIN_ACTION_조회는_확인없이_실행한다(monkeypatch):
    from app.master import ask_service
    from app.master.ask_schemas import DomainActionAnswer

    monkeypatch.setattr(
        ask_service,
        "_run_domain_action",
        lambda *args, **kwargs: DomainActionAnswer(
            domain="finance",
            action="FINANCE_SUMMARY_GET",
            text="재무 현황입니다.",
            data={"current_cash_krw": "100"},
        ),
    )
    result = run(
        "지금 돈 얼마 있어?",
        intent_json(
            action="DOMAIN_ACTION",
            domain_action="FINANCE_SUMMARY_GET",
            slots={},
            confidence="HIGH",
        ),
    )
    assert result.outcome == "DOMAIN_ACTION_ANSWERED"
    assert result.confirm_required is False
    assert result.domain_result is not None
    assert result.domain_result.action == "FINANCE_SUMMARY_GET"


def test_DOMAIN_ACTION_쓰기는_확인전_실행하지_않는다(monkeypatch):
    from app.master import ask_service

    called = False

    def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("확인 전에 write가 실행되면 안 된다")

    monkeypatch.setattr(ask_service, "_run_domain_action", should_not_run)
    monkeypatch.setattr(
        ask_service,
        "_domain_preview",
        lambda *args, **kwargs: "300만원 입금을 기록합니다. 진행할까요?",
    )
    result = run(
        "300만원 입금해줘",
        intent_json(
            action="DOMAIN_ACTION",
            domain_action="FINANCE_CASH_ADJUSTMENT_CREATE",
            slots={"direction": "INFLOW", "amount": "300만원", "source_ref": "이체확인-1"},
            confidence="HIGH",
        ),
    )
    assert result.outcome == "CLASSIFIED_ONLY"
    assert result.confirm_required is True
    assert called is False


def test_DOMAIN_ACTION_필수슬롯이_없으면_되묻고_실행하지_않는다():
    result = run(
        "거래처 여신한도 바꿔줘",
        intent_json(
            action="DOMAIN_ACTION",
            domain_action="FINANCE_CREDIT_LIMIT_UPSERT",
            slots={},
            confidence="HIGH",
        ),
    )
    assert result.outcome == "NEEDS_CLARIFICATION"
    assert result.confirm_required is False
    assert "거래처" in (result.clarification or "")


def test_finance_report_domain_action_returns_structured_facts_without_markdown(monkeypatch):
    from datetime import date

    from app.master import ask_service
    from app.master.llm.schemas import Intent

    facts = {"kind": "FINANCE", "sim_run_id": "SIM-1", "start_date": "2026-09-01"}
    monkeypatch.setattr(ask_service, "render_finance_chat_report", lambda **_kwargs: facts)
    result = ask_service._domain_read(
        Intent(
            action="DOMAIN_ACTION",
            agents=[],
            item=None,
            confidence="HIGH",
            domain_action="FINANCE_REPORT_GENERATE",
        ),
        as_of=date(2026, 9, 1),
    )
    assert result.data == facts
    assert result.markdown is None
    assert result.report_kind == "FINANCE"


def test_finance_report_without_period_asks_before_generating(monkeypatch):
    from app.master import ask_service

    called = False

    def report(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ask_service, "render_finance_chat_report", report)
    result = run(
        "재무 보고서 만들어줘",
        intent_json(
            action="DOMAIN_ACTION",
            domain_action="FINANCE_REPORT_GENERATE",
            slots={},
            confidence="HIGH",
        ),
    )

    assert result.outcome == "NEEDS_CLARIFICATION"
    assert result.confirm_required is False
    assert result.clarification == "어느 기간의 재무 보고서를 생성할까요?"
    assert called is False


def test_sales_report_domain_action_returns_structured_facts_without_markdown(monkeypatch):
    from datetime import date

    from app.master import ask_service
    from app.master.llm.schemas import Intent

    facts = {"kind": "SALES", "sim_run_id": "SIM-1", "start_date": "2026-09-01"}
    monkeypatch.setattr(ask_service, "render_sales_chat_report", lambda **_kwargs: facts)
    result = ask_service._domain_read(
        Intent(
            action="DOMAIN_ACTION",
            agents=[],
            item=None,
            confidence="HIGH",
            domain_action="SALES_REPORT_GENERATE",
        ),
        as_of=date(2026, 9, 1),
    )
    assert result.data == facts
    assert result.markdown is None
    assert result.report_kind == "SALES"


def test_logistics_report_domain_action_returns_structured_facts_without_markdown(monkeypatch):
    from datetime import date

    from app.master import ask_service
    from app.master.llm.schemas import Intent

    facts = {"kind": "LOGISTICS", "sim_run_id": "SIM-1", "start_date": "2026-09-01"}
    monkeypatch.setattr(ask_service, "render_logistics_chat_report", lambda **_kwargs: facts)
    result = ask_service._domain_read(
        Intent(
            action="DOMAIN_ACTION",
            agents=[],
            item=None,
            confidence="HIGH",
            domain_action="LOGISTICS_REPORT_GENERATE",
        ),
        as_of=date(2026, 9, 1),
    )
    assert result.domain == "logistics"
    assert result.data == facts
    assert result.markdown is None
    assert result.report_kind == "LOGISTICS"


def test_logistics_report_passes_existing_period_to_renderer(monkeypatch):
    """기간은 **기존 `_period()`** 가 만든다 — 보고서가 날짜 파서를 새로 두지 않는다."""
    from datetime import date

    from app.master import ask_service
    from app.master.llm.schemas import DomainSlots, Intent

    seen: dict[str, object] = {}

    def _record(**kwargs):
        seen.update(kwargs)
        return {"kind": "LOGISTICS"}

    monkeypatch.setattr(ask_service, "render_logistics_chat_report", _record)
    as_of = date(2026, 9, 17)  # 목요일
    ask_service._domain_read(
        Intent(
            action="DOMAIN_ACTION",
            agents=[],
            item=None,
            confidence="HIGH",
            domain_action="LOGISTICS_REPORT_GENERATE",
            slots=DomainSlots(period="THIS_WEEK"),
        ),
        as_of=as_of,
    )
    assert (seen["start_date"], seen["end_date"]) == ask_service._period(
        Intent(
            action="DOMAIN_ACTION",
            agents=[],
            item=None,
            confidence="HIGH",
            domain_action="LOGISTICS_REPORT_GENERATE",
            slots=DomainSlots(period="THIS_WEEK"),
        ),
        as_of=as_of,
    )
    assert seen["start_date"] == date(2026, 9, 14)  # 그 주 월요일
    assert seen["end_date"] == as_of
    assert seen["as_of"] == as_of


def test_logistics_report_is_a_read_action_not_a_write():
    """보고서 생성은 조회다. 쓰기 목록에 들어가면 확인 절차가 붙는다."""
    from app.master import ask_service

    assert "LOGISTICS_REPORT_GENERATE" in ask_service._DOMAIN_READ_ACTIONS
    assert "LOGISTICS_REPORT_GENERATE" not in ask_service._DOMAIN_WRITE_ACTIONS


def _logistics_item(item_id, name, *, on_hand, available, reserved, unallocated=0):
    from decimal import Decimal

    from app.logistics.schemas import ConsoleInventoryItem

    return ConsoleInventoryItem(
        item_id=item_id,
        item_name=name,
        on_hand_qty_kg=Decimal(on_hand),
        available_qty_kg=None if available is None else Decimal(available),
        reserved_qty_kg=Decimal(reserved),
        allocated_qty_kg=Decimal(0),
        unallocated_reserved_qty_kg=Decimal(unallocated),
        active_reservation_count=0,
        sell_priority_lot_count=0,
        expired_lot_count=0,
        expired_qty_kg=Decimal(0),
        disposal_candidate_lot_count=0,
    )


def _logistics_receipt(
    receipt_id,
    item,
    arrived,
    *,
    ordered,
    accepted=None,
    verdict="PASS",
    stock_applied=True,
    settled_without_stock=None,
):
    from datetime import date
    from decimal import Decimal

    from app.logistics.schemas import ConsoleInboundReceipt

    return ConsoleInboundReceipt(
        inbound_id=f"INB-{receipt_id}",
        receipt_id=receipt_id,
        item_id=f"ITEM-{item}",
        item_name=item,
        arrived_at=date.fromisoformat(arrived),
        ordered_qty_kg=None if ordered is None else Decimal(ordered),
        accepted_qty_kg=None if accepted is None else Decimal(accepted),
        hold_qty_kg=Decimal(0),
        rejected_qty_kg=Decimal(0),
        receipt_status="PUTAWAY_DONE",
        fact_source="inbound_receipts",
        inspection_id=f"INSP-{receipt_id}",
        inspection_verdict=verdict,
        inspected_qty_kg=None if accepted is None else Decimal(accepted),
        lot_id=f"LOT-{receipt_id}",
        in_move_id=f"MOVE-{receipt_id}",
        stock_applied=stock_applied,
        settled_without_stock=settled_without_stock,
    )


def _logistics_reservation(reservation_id, item, *, status, unallocated, due=None, shipped=False):
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from app.logistics.schemas import ConsoleAllocation, ConsoleReservation

    allocations = ()
    if shipped:
        allocations = (
            ConsoleAllocation(
                allocation_id=f"ALC-{reservation_id}",
                lot_id=f"LOT-{reservation_id}",
                pallet_id=None,
                allocated_qty_kg=Decimal(10),
                allocation_basis="FEFO_AUTO_SELECTED",
                decided_by="tester",
                decided_at=datetime(2026, 9, 1, tzinfo=UTC),
                status="SHIPPED",
                note=None,
            ),
        )
    return ConsoleReservation(
        reservation_id=reservation_id,
        item_id=f"ITEM-{item}",
        item_name=item,
        sale_id=f"SALE-{reservation_id}",
        required_qty_kg=Decimal(10),
        reserved_qty_kg=Decimal(10),
        allocated_qty_kg=Decimal(0),
        unallocated_qty_kg=Decimal(unallocated),
        due_date=None if due is None else date.fromisoformat(due),
        status=status,
        allocations=list(allocations),
    )


def _logistics_lot(lot_id, item, received, *, fresh, sell_priority=False, disposal=False):
    from datetime import date
    from decimal import Decimal

    from app.logistics.schemas import ConsoleInventoryLot

    return ConsoleInventoryLot(
        lot_id=lot_id,
        item_id=f"ITEM-{item}",
        item_name=item,
        grade="특",
        remaining_qty_kg=Decimal(50),
        received_at=date.fromisoformat(received),
        status="ACTIVE",
        storage_zone="COLD_DRY_0_1",
        remaining_freshness_days=fresh,
        remaining_turnover_days=fresh,
        turnover_status="SELL_PRIORITY" if sell_priority else "NORMAL",
        sell_priority=sell_priority,
        disposal_candidate=disposal,
    )


@pytest.fixture
def logistics_report_stubs(monkeypatch):
    """물류 read model 을 대역으로 세운다. **DB 도 SQL 도 타지 않는다.**"""
    from datetime import date
    from decimal import Decimal

    from app.logistics import console_service, db, historical_repository
    from app.logistics.schemas import (
        ConsoleArrivalSummary,
        ConsoleCapacity,
        ConsoleInboundResponse,
        ConsoleInventoryResponse,
        ConsoleOutboundResponse,
    )

    calls: dict[str, int] = {"connection": 0, "reservations": 0}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _connect():
        calls["connection"] += 1
        return _Conn()

    def _reservations(_conn, **_kwargs):
        calls["reservations"] += 1
        return ()

    monkeypatch.setattr(db, "get_connection", _connect)
    monkeypatch.setattr(historical_repository, "reservation_state_at", _reservations)
    monkeypatch.setattr(console_service, "load_console_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(
        console_service,
        "get_inventory_console",
        lambda **_kwargs: ConsoleInventoryResponse(
            sim_run_id="SIM-1",
            as_of=date(2026, 9, 3),
            items=[
                # 🔴 배추는 판매가능량을 못 읽었다 — 전체 합도 못 읽은 것이어야 한다.
                _logistics_item("ITEM-BAECHU", "배추", on_hand=10, available=None, reserved=3),
                _logistics_item("ITEM-MU", "무", on_hand=20, available=5, reserved=0),
                # 계약 밖 품목. **이름을 코드에 적지 않고** ITEMS 로만 걸러진다.
                _logistics_item("ITEM-PIMANUL", "피마늘", on_hand=999, available=999, reserved=0),
            ],
            lots=[
                # 같은 품목·같은 입고일 둘 → 표시 순번이 붙어야 한다.
                _logistics_lot("LOT-B", "무", "2026-09-01", fresh=9),
                _logistics_lot("LOT-A", "무", "2026-09-01", fresh=9),
                _logistics_lot("LOT-URGENT", "배추", "2026-08-20", fresh=1, sell_priority=True),
                _logistics_lot("LOT-TRASH", "배추", "2026-08-10", fresh=0, disposal=True),
                _logistics_lot("LOT-PIMANUL", "피마늘", "2026-09-01", fresh=5),
            ],
            capacity=ConsoleCapacity(
                used_capacity_kg=Decimal(1234),
                guaranteed_capacity_kg=Decimal(8000),
                burst_capacity_kg=Decimal(9600),
            ),
        ),
    )
    monkeypatch.setattr(
        console_service,
        "get_inbound_console",
        lambda **_kwargs: ConsoleInboundResponse(
            sim_run_id="SIM-1",
            as_of=date(2026, 9, 3),
            in_transit_status="UNRESOLVED",
            in_transit=None,
            receipts=[
                # 기간(9/1~9/3) 안. 같은 날 같은 품목 둘 → 한 줄로 접힌다.
                _logistics_receipt("RCPT-IN-1", "무", "2026-09-02", ordered=100, accepted=100),
                _logistics_receipt("RCPT-IN-2", "무", "2026-09-02", ordered=50, accepted=50),
                _logistics_receipt("RCPT-IN-3", "배추", "2026-09-03", ordered=70, accepted=None),
                # 🔴 기간 밖 — 하루짜리 보고서에 1월 입고가 딸려 나오던 자리다.
                _logistics_receipt("RCPT-OLD", "배추", "2026-01-15", ordered=900, accepted=900),
                # 경계 밖(하루 뒤)도 빠져야 한다.
                _logistics_receipt("RCPT-FUTURE", "무", "2026-09-04", ordered=11, accepted=11),
            ],
            arrival_summary=ConsoleArrivalSummary(
                source_status="UNRESOLVED",
                due_count=0,
                blocked_count=0,
                not_due_count=0,
                unresolved_count=0,
                overdue_count=0,
            ),
        ),
    )
    monkeypatch.setattr(
        console_service,
        "get_outbound_console",
        lambda **_kwargs: ConsoleOutboundResponse(
            sim_run_id="SIM-1",
            as_of=date(2026, 9, 3),
            reservations=[
                # 아직 일이 남은 것 — 본문에 실린다. 납기일 없는 것은 뒤로 간다.
                _logistics_reservation("RSV-NODUE", "무", status="RESERVED", unallocated=10),
                _logistics_reservation(
                    "RSV-SOON",
                    "배추",
                    status="PARTIALLY_ALLOCATED",
                    unallocated=4,
                    due="2026-09-05",
                ),
                # 🔴 전량 출고가 끝난 과거 예약 — 본문에서 빠지고 건수로만 남는다.
                _logistics_reservation(
                    "RSV-DONE", "무", status="ALLOCATED", unallocated=0, shipped=True
                ),
                # 놓아준 예약도 끝난 것이다.
                _logistics_reservation("RSV-GONE", "배추", status="CANCELLED", unallocated=0),
            ],
        ),
    )
    monkeypatch.setattr(
        historical_repository,
        "onhand_total_by_day",
        lambda _conn, **_kwargs: {
            date(2026, 9, 1): Decimal(30),
            date(2026, 9, 2): Decimal(0),
            date(2026, 9, 3): Decimal(30),
        },
    )
    # 9/2 는 이 실행이 **열지 않은 날**이다 — 원장 누계 0 을 재고 0kg 으로 그리면 안 된다.
    monkeypatch.setattr(
        historical_repository,
        "snapshot_days_between",
        lambda _conn, **_kwargs: frozenset({date(2026, 9, 1), date(2026, 9, 3)}),
    )
    return calls


def test_logistics_report_facts_keep_none_and_unopened_days(logistics_report_stubs):
    from datetime import date

    from app.master.report import render_logistics_chat_report

    facts = render_logistics_chat_report(
        sim_run_id="SIM-1",
        as_of=date(2026, 9, 3),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 3),
    )

    summary = facts["summary"]
    # 🔴 아는 값만 더해 숫자를 만들지 않는다. 5 도 1004 도 아니고 None 이다.
    assert summary["total_available_qty_kg"] is None
    # 계약 밖 품목(999kg)은 표시 합계에서 빠진다.
    assert summary["total_on_hand_qty_kg"] == "30"
    assert summary["total_reserved_qty_kg"] == "3"
    assert summary["item_count"] == 2
    # 🔴 창고 사용량은 read model 값 그대로다 — 표시 품목 합(30)이 아니다.
    assert summary["used_capacity_kg"] == "1234"
    assert summary["guaranteed_capacity_kg"] == "8000"

    assert [row["item_name"] for row in facts["inventory"]["items"]] == ["배추", "무"]
    # ★ 안 연 날은 `null` 이다 — 0kg 이 아니다.
    assert facts["trend"] == [
        {"date": "2026-09-01", "on_hand_qty_kg": 30.0},
        {"date": "2026-09-02", "on_hand_qty_kg": None},
        {"date": "2026-09-03", "on_hand_qty_kg": 30.0},
    ]
    # ★ 운송 중 미확인은 `None` 이다 — 0건 확인(`[]`)과 섞지 않는다.
    assert facts["inbound"]["in_transit"] is None
    assert facts["kind"] == "LOGISTICS"


def test_logistics_report_uses_one_connection_and_one_reservation_read(logistics_report_stubs):
    """한 보고서 = 커넥션 1개 · `reservation_state_at` 1회 (화면과 같은 조립)."""
    from datetime import date

    from app.master.report import render_logistics_chat_report

    render_logistics_chat_report(
        sim_run_id="SIM-1",
        as_of=date(2026, 9, 3),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 3),
    )
    assert logistics_report_stubs == {"connection": 1, "reservations": 1}


def _logistics_facts(start="2026-09-01", end="2026-09-03"):
    from datetime import date

    from app.master.report import render_logistics_chat_report

    return render_logistics_chat_report(
        sim_run_id="SIM-1",
        as_of=date(2026, 9, 3),
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
    )


def test_logistics_report_receipts_are_period_activity_not_all_history(logistics_report_stubs):
    """🔴 기준일 Snapshot 이 아니라 **기간에 도착한 것**만 본문 대상이다."""
    facts = _logistics_facts()
    inbound = facts["inbound"]

    assert inbound["period_receipt_count"] == 3
    # 1월 입고와 하루 뒤 입고는 기간 밖이라 빠진다.
    assert [row["receipt_id"] for row in inbound["period_receipts"]] == [
        "RCPT-IN-1",
        "RCPT-IN-2",
        "RCPT-IN-3",
    ]
    # 「입고일 + 품목」 실적으로 접힌다. 최신 입고일 먼저 (지시 §27).
    rollup = inbound["period_receipt_rollup"]
    assert [(row["arrived_at"], row["item"], row["receipt_count"]) for row in rollup] == [
        ("2026-09-03", "배추", 1),
        ("2026-09-02", "무", 2),
    ]
    # 같은 날 같은 품목 두 건이 합쳐진다 — 단순 합산이다.
    assert rollup[1]["ordered_qty_kg"] == "150"
    assert rollup[1]["accepted_qty_kg"] == "150"
    assert rollup[1]["inspection_verdicts"] == ["PASS"]


def test_logistics_report_receipt_rollup_keeps_none_total(logistics_report_stubs):
    """🔴 한 칸이라도 `None` 이면 합계도 `None` 이다 — 0 으로 메우지 않는다."""
    rollup = _logistics_facts()["inbound"]["period_receipt_rollup"]
    baechu = next(row for row in rollup if row["item"] == "배추")
    assert baechu["ordered_qty_kg"] == "70"
    # 합격 수량을 못 읽은 건이 있으므로 합계도 못 읽은 것이다 (0 이 아니다).
    assert baechu["accepted_qty_kg"] is None


def test_logistics_report_rollup_counts_both_kinds_of_completion(logistics_report_stubs, monkeypatch):
    """🔴 **완료의 형태가 둘이다** (#805) — 「반영할 재고 없음」도 정상 완료다.

    수용 0 으로 끝난 건을 `stock_applied` 한 칸으로만 세면 보고서에서 「재고 반영
    0 / 1건」으로 보여 **미처리 건으로 잘못 읽힌다.**
    """
    from datetime import date

    from app.logistics import console_service
    from app.logistics.schemas import ConsoleArrivalSummary, ConsoleInboundResponse

    monkeypatch.setattr(
        console_service,
        "get_inbound_console",
        lambda **_kwargs: ConsoleInboundResponse(
            sim_run_id="SIM-1",
            as_of=date(2026, 9, 3),
            in_transit_status="UNRESOLVED",
            in_transit=None,
            receipts=[
                # 재고가 선 완료.
                _logistics_receipt("R-APPLIED", "무", "2026-09-02", ordered=100, accepted=100),
                # 🔴 수용 0 으로 재고 없이 끝난 완료 — 「아직」이 아니다.
                _logistics_receipt(
                    "R-SETTLED",
                    "무",
                    "2026-09-02",
                    ordered=50,
                    accepted=0,
                    stock_applied=False,
                    settled_without_stock=True,
                ),
                # 두 완료 어느 쪽에도 아직 닿지 않은 건.
                _logistics_receipt(
                    "R-WORKING",
                    "무",
                    "2026-09-02",
                    ordered=20,
                    accepted=20,
                    stock_applied=False,
                    settled_without_stock=False,
                ),
                # 🔴 `None` 은 「모른다」다 — `False` 가 아니다.
                _logistics_receipt(
                    "R-UNKNOWN",
                    "무",
                    "2026-09-02",
                    ordered=30,
                    accepted=30,
                    stock_applied=False,
                    settled_without_stock=None,
                ),
            ],
            arrival_summary=ConsoleArrivalSummary(
                source_status="UNRESOLVED",
                due_count=0,
                blocked_count=0,
                not_due_count=0,
                unresolved_count=0,
                overdue_count=0,
            ),
        ),
    )

    rollup = _logistics_facts()["inbound"]["period_receipt_rollup"]
    row = next(r for r in rollup if r["item"] == "무")
    assert row["receipt_count"] == 4
    assert row["stock_applied_count"] == 1
    assert row["settled_without_stock_count"] == 1
    # 🔴 「0건」과 「모름」을 가른다 — 모름을 미처리로 접지 않는다.
    assert row["settled_unknown_count"] == 1


def test_logistics_report_rollup_does_not_rejudge_settlement(logistics_report_stubs, monkeypatch):
    """🔴 보고서가 완료 여부를 **다시 판정하지 않는다.**

    `accepted_qty_kg == 0` 이어도 정본이 「아니다」라고 하면 아닌 것이다. 여기서
    수량을 보고 되재면 콘솔과 보고서가 경계에서 갈린다.
    """
    from datetime import date

    from app.logistics import console_service
    from app.logistics.schemas import ConsoleArrivalSummary, ConsoleInboundResponse

    monkeypatch.setattr(
        console_service,
        "get_inbound_console",
        lambda **_kwargs: ConsoleInboundResponse(
            sim_run_id="SIM-1",
            as_of=date(2026, 9, 3),
            in_transit_status="UNRESOLVED",
            in_transit=None,
            receipts=[
                # 수용 0 이지만 정본은 settled 가 아니라고 말한다.
                _logistics_receipt(
                    "R-ZERO",
                    "무",
                    "2026-09-02",
                    ordered=50,
                    accepted=0,
                    stock_applied=False,
                    settled_without_stock=False,
                ),
            ],
            arrival_summary=ConsoleArrivalSummary(
                source_status="UNRESOLVED",
                due_count=0,
                blocked_count=0,
                not_due_count=0,
                unresolved_count=0,
                overdue_count=0,
            ),
        ),
    )

    row = next(r for r in _logistics_facts()["inbound"]["period_receipt_rollup"] if r["item"] == "무")
    assert row["settled_without_stock_count"] == 0
    assert row["settled_unknown_count"] == 0


def test_logistics_report_available_range_uses_only_days_with_data(logistics_report_stubs):
    """🔴 요청 기간을 실제 데이터 기간으로 **복사하지 않는다.**

    시뮬레이션이 안 연 날은 `null` 이고 「0kg」이 아니다. 그런 날은 범위 계산에서 빠져야
    공용 머리말이 「요청 기간 vs 사용 가능 데이터」를 사실대로 적는다.
    """
    facts = _logistics_facts(start="2026-09-01", end="2026-09-03")
    trend = facts["trend"]
    covered = [row["date"] for row in trend if row["on_hand_qty_kg"] is not None]

    assert facts["available_start_date"] == covered[0]
    assert facts["available_end_date"] == covered[-1]
    # 값이 없는 날은 범위 밖이다 — 요청 기간 양끝을 그대로 베끼지 않는다.
    assert all(
        facts["available_start_date"] <= row["date"] <= facts["available_end_date"]
        for row in trend
        if row["on_hand_qty_kg"] is not None
    )


def test_logistics_report_available_range_is_none_when_no_day_is_open(
    logistics_report_stubs, monkeypatch
):
    """추이가 전부 `null` 이면 범위는 `None` 이다 — 0 일짜리 범위를 지어내지 않는다."""
    from app.logistics import historical_repository

    monkeypatch.setattr(historical_repository, "snapshot_days_between", lambda *a, **k: set())

    facts = _logistics_facts()
    assert all(row["on_hand_qty_kg"] is None for row in facts["trend"])
    assert facts["available_start_date"] is None
    assert facts["available_end_date"] is None
    # 🔴 물류에는 권위 있는 데이터 모드가 없다 — 문자열을 지어내지 않는다.
    assert facts["data_mode"] is None


def test_logistics_report_takes_the_period_the_user_picked_on_screen(monkeypatch):
    """화면에서 **직접 고른 날짜**가 물류 보고서 기간까지 실제로 닿는가.

    🔴 재무·판매만 받던 자리라, 물류는 사용자가 기간을 골라도 무시되고 있었다.
    """
    from datetime import date

    from app.master import ask_service
    from app.master.ask_schemas import AskRequest

    seen: dict[str, object] = {}

    def _record(**kwargs):
        seen.update(kwargs)
        return {"kind": "LOGISTICS"}

    monkeypatch.setattr(ask_service, "render_logistics_chat_report", _record)
    result = ask_service.ask(
        AskRequest(
            utterance="재고·물류 보고서 만들어줘",
            as_of=AS_OF,
            policy_version="v1.3",
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 10),
        ),
        service=svc(
            intent_json(
                action="DOMAIN_ACTION",
                domain_action="LOGISTICS_REPORT_GENERATE",
                slots={"period": "TODAY"},
                confidence="HIGH",
            )
        ),
    )

    assert result.outcome == "DOMAIN_ACTION_ANSWERED"
    assert (seen["start_date"], seen["end_date"]) == (date(2026, 6, 1), date(2026, 6, 10))


def test_screen_picked_period_still_reaches_finance_and_sales(monkeypatch):
    """물류를 더하면서 **재무·판매가 떨어지지 않았는가** — 같은 집합 하나가 셋을 태운다."""
    from app.master import ask_service

    assert ask_service._REPORT_DATE_RANGE_ACTIONS == {
        "FINANCE_REPORT_GENERATE",
        "SALES_REPORT_GENERATE",
        "LOGISTICS_REPORT_GENERATE",
    }


def test_logistics_report_empty_period_is_a_normal_answer(logistics_report_stubs):
    """기간에 입고가 없으면 **빈 배열이 정상값**이다 — 과거로 기간을 넓히지 않는다."""
    facts = _logistics_facts(start="2026-09-03", end="2026-09-03")
    assert facts["inbound"]["period_receipt_count"] == 1
    facts = _logistics_facts(start="2026-09-01", end="2026-09-01")
    assert facts["inbound"]["period_receipt_rollup"] == []
    assert facts["inbound"]["period_receipt_count"] == 0
    # 하루짜리 보고서는 화면이 추이 선을 그리지 않도록 표시한다.
    assert facts["summary"]["trend_is_single_day"] is True


def test_logistics_report_shows_only_reservations_still_working(logistics_report_stubs):
    """🔴 전량 출고가 끝난 과거 예약을 본문에 늘어놓지 않는다 (`_still_working` 기준)."""
    facts = _logistics_facts()
    outbound = facts["outbound"]

    # 납기일 있는 것 먼저, 납기일 없는 것은 뒤로 (지시 §27).
    assert [row["reservation_id"] for row in outbound["working_reservations"]] == [
        "RSV-SOON",
        "RSV-NODUE",
    ]
    assert outbound["working_reservation_count"] == 2
    # 🔴 숨긴 것이 아니라 **건수로 남긴다.**
    assert outbound["settled_reservation_count"] == 2
    assert facts["summary"]["working_reservation_count"] == 2
    # 할당 Lot 수는 `allocated_qty_kg` 와 같은 모집단(아직 안 나간 할당)이다.
    assert all(row["allocation_lot_count"] == 0 for row in outbound["working_reservations"])


def test_logistics_report_lots_are_ordered_and_numbered_for_people(logistics_report_stubs):
    """급한 Lot 이 위로 오고, 같은 품목·입고일 Lot 에는 안정된 표시 순번이 붙는다."""
    lots = _logistics_facts()["inventory"]["lots"]

    # 폐기 검토 → 우선 출고 → 신선도 잔여 적은 순 → 입고일 오래된 순.
    assert [row["lot_id"] for row in lots] == ["LOT-TRASH", "LOT-URGENT", "LOT-A", "LOT-B"]
    # 계약 밖 품목 Lot 은 빠진다.
    assert all(row["item_name"] != "피마늘" for row in lots)
    # 같은 품목·같은 입고일 둘 → `lot_id` 정렬로 #1 · #2.
    numbered = {row["lot_id"]: (row["display_index"], row["display_group_size"]) for row in lots}
    assert numbered["LOT-A"] == (1, 2)
    assert numbered["LOT-B"] == (2, 2)
    # 혼자인 Lot 은 그룹 크기가 1이라 화면이 순번을 안 붙인다.
    assert numbered["LOT-URGENT"] == (1, 1)
    # 🔴 판정값은 read model 것 그대로다 — 표시 순서를 바꿨다고 상태가 바뀌지 않는다.
    #    폐기 검토 Lot 이 맨 위로 왔지만 회전 상태는 대역이 준 값 그대로 `NORMAL` 이다.
    assert lots[0]["disposal_candidate"] is True
    assert lots[0]["turnover_status"] == "NORMAL"


def test_logistics_arrival_display_state_only_reads_the_promised_date():
    """🔴 도착 «자격» 판정을 흉내 내지 않는다 — 예정일이 지났나 하나만 본다."""
    from datetime import date

    from app.master.report import _logistics_arrival_display_state

    as_of = date(2026, 9, 3)
    assert _logistics_arrival_display_state(date(2026, 9, 5), as_of) == "SCHEDULED"
    assert _logistics_arrival_display_state(date(2026, 9, 1), as_of) == "OVERDUE"
    # 예정일 당일은 아직 안 온 것이므로 지연이다 (`arrival` 의 `eta > as_of` 경계와 같다).
    assert _logistics_arrival_display_state(as_of, as_of) == "OVERDUE"
    # 🔴 날짜를 모르면 지어내지 않는다 — 화면이 「—」로 둔다.
    assert _logistics_arrival_display_state(None, as_of) is None


def test_logistics_report_marks_pending_arrivals_for_display(logistics_report_stubs, monkeypatch):
    """도착 전 물량 각 줄에 화면용 도착 상태가 붙는다. 내부 이름 `in_transit` 은 그대로."""
    from datetime import date
    from decimal import Decimal

    from app.logistics import console_service
    from app.logistics.schemas import (
        ConsoleArrivalSummary,
        ConsoleInboundResponse,
        ConsoleInTransitItem,
    )

    def _row(item, eta):
        return ConsoleInTransitItem(
            inbound_id=f"INB-{item}-{eta or 'NA'}",
            purchase_id=None,
            item=item,
            quantity_kg=Decimal(100),
            expected_arrival_date=None if eta is None else date.fromisoformat(eta),
        )

    monkeypatch.setattr(
        console_service,
        "get_inbound_console",
        lambda **_kwargs: ConsoleInboundResponse(
            sim_run_id="SIM-1",
            as_of=date(2026, 9, 3),
            in_transit_status="CONFIRMED",
            in_transit=[_row("무", "2026-09-05"), _row("배추", "2026-09-01"), _row("무", None)],
            receipts=[],
            arrival_summary=ConsoleArrivalSummary(
                source_status="CONFIRMED",
                due_count=0,
                blocked_count=0,
                not_due_count=1,
                unresolved_count=1,
                overdue_count=1,
            ),
        ),
    )

    rows = _logistics_facts()["inbound"]["in_transit"]
    assert [row["arrival_display_state"] for row in rows] == ["SCHEDULED", "OVERDUE", None]
    # ★ 내부 계약 이름과 칸은 그대로다 — 바꾼 것은 화면 표시명뿐이다.
    assert rows[0]["inbound_id"] == "INB-무-2026-09-05"
    assert rows[0]["expected_arrival_date"] == "2026-09-05"


def test_logistics_report_summary_carries_unallocated_comparison(logistics_report_stubs):
    """미할당 예약량 대 현재고 비교는 **단순 사실**이다 — 새 등급을 만들지 않는다."""
    facts = _logistics_facts()
    summary = facts["summary"]
    # 품목 카드의 미할당 합(배추 25 + 무 0) > 현재고 합(30) 은 아니다.
    assert summary["total_unallocated_reserved_qty_kg"] == "0"
    assert summary["unallocated_exceeds_on_hand"] is False
    assert summary["lot_count"] == 4
    assert "severity" not in summary


def test_finance_report_facts_keep_null_operating_expense(monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    from app.finance import (
        console_credit,
        console_expenses,
        console_payables,
        console_receivables,
        dashboard,
    )
    from app.master.report import render_finance_chat_report

    dump = lambda **kwargs: SimpleNamespace(model_dump=lambda **_kwargs: kwargs, **kwargs)
    closing = dump(close_date=date(2026, 9, 1), operating_expense_cash_out_krw=None)
    monkeypatch.setattr(
        dashboard,
        "get_finance_dashboard",
        lambda **_kwargs: dump(states=[], recent_closings=[closing]),
    )
    monkeypatch.setattr(
        dashboard, "get_finance_cashflow", lambda **_kwargs: dump(cashflow=[closing])
    )
    empty_receivable = dump(
        total_outstanding_krw=0, days_1_7_krw=0, days_8_30_krw=0, days_30_plus_krw=0
    )
    empty_payable = dump(total_outstanding_krw=0, due_today_krw=0, due_next_7d_krw=0, overdue_krw=0)
    empty_expense = dump(accrued_krw=0, accrued_count=0, paid_krw=0, cancelled_krw=0)
    monkeypatch.setattr(
        console_receivables,
        "get_console_receivables",
        lambda **_kwargs: dump(summary=empty_receivable),
    )
    monkeypatch.setattr(
        console_payables, "get_console_payables", lambda **_kwargs: dump(summary=empty_payable)
    )
    monkeypatch.setattr(
        console_expenses,
        "get_console_expenses",
        lambda **_kwargs: dump(summary=empty_expense, rows=[]),
    )
    monkeypatch.setattr(console_credit, "get_console_credit", lambda **_kwargs: dump(partners=[]))
    facts = render_finance_chat_report(
        sim_run_id="SIM-1",
        as_of=date(2026, 9, 1),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
    )
    assert facts["kind"] == "FINANCE"
    assert facts["closings"][0]["operating_expense_cash_out_krw"] is None


def test_finance_report_cash_trend_uses_full_requested_range(monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    from app.finance import (
        console_credit,
        console_expenses,
        console_payables,
        console_receivables,
        dashboard,
    )
    from app.master.report import render_finance_chat_report

    dump = lambda **kwargs: SimpleNamespace(model_dump=lambda **_kwargs: kwargs, **kwargs)
    preview = dump(close_date=date(2026, 6, 12), base_net_cash_krw=12)
    first = dump(close_date=date(2026, 1, 3), base_net_cash_krw=1)
    last = dump(close_date=date(2026, 6, 12), base_net_cash_krw=12)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        dashboard,
        "get_finance_dashboard",
        lambda **_kwargs: dump(states=[], recent_closings=[preview]),
    )
    monkeypatch.setattr(
        dashboard,
        "get_finance_cashflow",
        lambda **kwargs: (seen.update(kwargs) or dump(cashflow=[first, last])),
    )
    monkeypatch.setattr(
        console_receivables, "get_console_receivables", lambda **_kwargs: dump(summary=dump())
    )
    monkeypatch.setattr(
        console_payables, "get_console_payables", lambda **_kwargs: dump(summary=dump())
    )
    monkeypatch.setattr(
        console_expenses, "get_console_expenses", lambda **_kwargs: dump(summary=dump(), rows=[])
    )
    monkeypatch.setattr(console_credit, "get_console_credit", lambda **_kwargs: dump(partners=[]))

    facts = render_finance_chat_report(
        sim_run_id="SIM-1",
        as_of=date(2026, 6, 12),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 12),
    )

    assert seen == {"sim_run_id": "SIM-1", "as_of": date(2026, 6, 12), "days": 163}
    assert [row["close_date"] for row in facts["closings"]] == [date(2026, 1, 3), date(2026, 6, 12)]


def test_sales_report_facts_include_actual_confirmed_sales_only(monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    from app.master.report import render_sales_chat_report
    from app.sales import console_partners, console_proposals, console_trend, dashboard

    dump = lambda **kwargs: SimpleNamespace(model_dump=lambda **_kwargs: kwargs, **kwargs)
    confirmed, presentable = (
        dump(scenario_id="C", sale_status="CONFIRMED"),
        dump(scenario_id="P", sale_status=None),
    )
    monkeypatch.setattr(dashboard, "get_sales_dashboard", lambda **_kwargs: dump())
    monkeypatch.setattr(
        console_proposals,
        "get_console_sales_proposals",
        lambda **_kwargs: dump(
            rows=[confirmed, presentable],
            state="PRESENTABLE",
            presentable_count=1,
            review_required_count=0,
            unresolved_count=0,
            rejected_count=0,
        ),
    )
    monkeypatch.setattr(console_trend, "get_console_sales_trend", lambda **_kwargs: dump(rows=[]))
    monkeypatch.setattr(console_partners, "get_console_partners", lambda **_kwargs: dump(rows=[]))
    facts = render_sales_chat_report(
        sim_run_id="SIM-1",
        as_of=date(2026, 9, 1),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
    )
    assert facts["kind"] == "SALES"
    assert [row["scenario_id"] for row in facts["confirmed_sales"]] == ["C"]
