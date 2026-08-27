"""마스터 API — `/master/request` · `/master/trigger`.

★ 라우터만 격리해 띄운다.
  `app.main` 전체는 finance·logistics 가 `psycopg` 를 요구해서, 마스터 검증에
  DB 드라이버가 끼어들 이유가 없다.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import AgentReply, AgentRequest, ExecutionMetadata, wiring
from app.master.router import router

AS_OF = "2026-08-26"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    wiring.reset()
    yield TestClient(app)
    wiring.reset()


def body(**kw) -> dict:
    base = {"as_of": AS_OF, "policy_version": "v1.3-PROVISIONAL"}
    base.update(kw)
    return base


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
            used_tools=("tool_a",),
            tool_order=(1,),
        )
        return reply, meta

    return port


def wire_all(scenarios=None):
    scn = scenarios if scenarios is not None else [{"scenario_id": "SCN-1"}]

    def purchase(request: AgentRequest):
        return _port({"scenarios": list(scn)})(request)

    wiring.register("finance", _port({"cap": 1}))
    wiring.register("inventory", _port({"cap": 2}))
    wiring.register("purchase", purchase)


# ---------------------------------------------------------------------------
# 어댑터 미등록 — 오류가 아니라 E4 다
# ---------------------------------------------------------------------------


def test_어댑터가_없으면_500_이_아니라_E4(client):
    r = client.post("/master/request", json=body())
    assert r.status_code == 200
    data = r.json()
    assert data["end_code"] == "E4_NOT_STARTED"
    assert sorted(data["missing_adapters"]) == ["finance", "inventory", "purchase"]


def test_일부만_등록돼도_누가_없는지_알려준다(client):
    wiring.register("finance", _port())
    data = client.post("/master/request", json=body()).json()
    assert data["missing_adapters"] == ["inventory", "purchase"]
    assert "inventory" in data["reason"]


# ---------------------------------------------------------------------------
# 정상 경로
# ---------------------------------------------------------------------------


def test_전원_등록되면_E1(client):
    wire_all([{"scenario_id": "SCN-1"}, {"scenario_id": "SCN-2"}])
    data = client.post("/master/request", json=body()).json()
    assert data["end_code"] == "E1_APPROVED"
    assert data["presentable"] is True
    assert len(data["scenarios"]) == 2


def test_실행_계획이_응답에_실린다(client):
    wire_all()
    data = client.post("/master/request", json=body()).json()
    assert [tuple(x) for x in data["plan_signature"]] == [
        ("finance", "PRE_PURCHASE", 1),
        ("inventory", "PRE_PURCHASE", 1),
        ("purchase", "GENERATE_SCENARIOS", 1),
        ("finance", "SCENARIO_VALIDATION", 1),
        ("inventory", "SCENARIO_VALIDATION", 1),
    ]
    assert len(data["plan"]) == 5
    assert data["plan"][0]["used_tools"] == ["tool_a"]


def test_계획에_시각_필드가_없다(client):
    wire_all()
    step = client.post("/master/request", json=body()).json()["plan"][0]
    assert not any(k in step for k in ("started_at", "ended_at", "timestamp", "elapsed_ms"))


def test_같은_입력에_같은_계획(client):
    wire_all()
    a = client.post("/master/request", json=body()).json()
    b = client.post("/master/request", json=body()).json()
    assert a["plan_signature"] == b["plan_signature"]
    assert a["request_id"] == b["request_id"]


def test_검증_Tool_이_기본으로_붙는다(client):
    """★ 전에는 기본값이 None 이라 API 경로에서 검증이 통째로 건너뛰어졌다.

    `verification_skipped: true` 로 드러나긴 했지만 아무도 안 봤다. 끄려면 명시적으로
    꺼야 하는 쪽이 안전하다.
    """
    wire_all()
    data = client.post("/master/request", json=body()).json()
    assert data["verification_skipped"] is False


def test_못_본_검사가_응답에_드러난다(client):
    """§3.7.6 — 커버리지를 감추지 않는다.

    `findings: []` 를 "56검사 통과"로 읽지 않게 **못 본 것**을 함께 낸다.
    """
    wire_all()
    skipped = client.post("/master/request", json=body()).json()["skipped_checks"]
    assert any("56검사" in s for s in skipped)


def test_조언자_봉투_위반은_재호출을_유발하지_않는다(client):
    """★ 배선하고 나서 드러난 것.

    스텁 조언자가 Evidence 를 안 붙여 `E-EVIDENCE-MISSING` 이 난다. 이걸 findings 로
    올리면 **매입을 다시 부른다** — 매입이 몇 번을 다시 만들어도 재무의 위반은 그대로다.
    호출 예산만 태우고 E3 로 끝난다.
    """
    wire_all([{"scenario_id": "SCN-1"}, {"scenario_id": "SCN-2"}])
    data = client.post("/master/request", json=body()).json()
    assert data["end_code"] == "E1_APPROVED"
    assert data["purchase_attempts"] == 1
    assert any("E-EVIDENCE-MISSING" in c for c in data["concerns"])
    assert data["findings"] == []


def test_단일안이_표시된다(client):
    wire_all([{"scenario_id": "SCN-1"}])
    assert client.post("/master/request", json=body()).json()["single_option"] is True


# ---------------------------------------------------------------------------
# 종료 코드가 200 으로 내려온다
# ---------------------------------------------------------------------------


def test_부서_미가동은_200_에_E4(client):
    """실패도 오류가 아니라 그날의 결과다 (§5.3)."""
    wiring.register("finance", _port())
    wiring.register(
        "inventory",
        _port(runtime_status="RUNTIME_NOT_READY", business_status="skipped", missing_data=("N2",)),
    )
    wiring.register("purchase", _port({"scenarios": []}))

    r = client.post("/master/request", json=body())
    assert r.status_code == 200
    data = r.json()
    assert data["end_code"] == "E4_NOT_STARTED"
    assert data["blocked_by"] == ["inventory"]


def test_시나리오_0개_납품의무_있으면_E5(client):
    wire_all([])
    data = client.post("/master/request", json=body(has_unmet_obligation=True)).json()
    assert data["end_code"] == "E5_NO_FEASIBLE_PLAN"


def test_시나리오_0개_납품의무_없으면_E2(client):
    wire_all([])
    assert client.post("/master/request", json=body()).json()["end_code"] == "E2_HELD"


def test_예산이_작으면_E3(client):
    wire_all()
    data = client.post("/master/request", json=body(budget=2)).json()
    assert data["end_code"] == "E3_REJECTED"
    assert "예산" in data["reason"]


# ---------------------------------------------------------------------------
# 요청 계약
# ---------------------------------------------------------------------------


def test_request_id_를_주면_그대로_쓴다(client):
    wire_all()
    data = client.post("/master/request", json=body(request_id="REQ-CUSTOM")).json()
    assert data["request_id"] == "REQ-CUSTOM"


def test_request_id_기본값은_날짜_순번(client):
    wire_all()
    assert client.post("/master/request", json=body()).json()["request_id"] == "REQ-20260826-0001"


def test_모르는_필드는_422(client):
    assert client.post("/master/request", json=body(unknown=1)).status_code == 422


def test_policy_version_이_비면_422(client):
    assert client.post("/master/request", json=body(policy_version="")).status_code == 422


def test_예산은_1_이상(client):
    assert client.post("/master/request", json=body(budget=0)).status_code == 422


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def test_trigger_는_ML_COMPLETE_로_바꿔_실행한다(client):
    seen = {}

    def watching(request: AgentRequest):
        seen["trigger"] = request.context.trigger
        return _port({"cap": 1})(request)

    wiring.register("finance", watching)
    wiring.register("inventory", _port({"cap": 2}))
    wiring.register("purchase", _port({"scenarios": [{"scenario_id": "SCN-1"}]}))

    r = client.post("/master/trigger", json=body(trigger="USER_REQUEST"))
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert seen["trigger"] == "ML_COMPLETE"


def test_trigger_는_아직_동기_실행이다(client):
    """Queue·비동기는 별도 이슈 — 붙으면 note 가 queued 가 된다."""
    wire_all()
    assert client.post("/master/trigger", json=body()).json()["note"] == "executed"


def test_trigger_도_어댑터_없으면_받아는_준다(client):
    r = client.post("/master/trigger", json=body())
    assert r.status_code == 200
    assert r.json()["accepted"] is True


def test_as_of_는_필수(client):
    r = client.post("/master/request", json={"policy_version": "v1"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# §3.2.5 예외 — API 로 실어 주기
# ---------------------------------------------------------------------------


def test_예측을_요청에_실으면_매입에_전달된다(client):
    seen: dict = {}

    def purchase(request: AgentRequest):
        if request.mode == "GENERATE_SCENARIOS":
            seen.update(request.payload)
        return _port({"scenarios": [{"scenario_id": "SCN-1"}]})(request)

    wiring.register("finance", _port({"cap": 1}))
    wiring.register("inventory", _port({"cap": 2}))
    wiring.register("purchase", purchase)

    r = client.post(
        "/master/request",
        json=body(
            forecast={"generated_at": "2026-08-26T06:00:00+09:00", "horizon_days": 18},
            confirmed_orders={"total_kg": 5000},
            policy_values={"contract_price_krw": 1900},
        ),
    )
    assert r.status_code == 200
    assert seen["forecast"]["horizon_days"] == 18
    assert seen["policy_values"]["contract_price_krw"] == 1900
