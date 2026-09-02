"""마스터 실행 계획 적재 — 저장 payload 조립과 조회 API.

★ 실제 INSERT 는 돌지 않는다.
  `run_repository.history_enabled()` 가 pytest 안에서 False 라 `try_save_run` 이
  no-op 이다 (팀 공용 DB 오염 방지). 여기서는 **무엇을 저장하려 했는가**를 고정한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import persistence
from app.master.router import router
from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse, StepOut

AS_OF = date(2026, 8, 27)


def response(**kw) -> ProcurementRunResponse:
    base = {
        "request_id": "REQ-20260827-0001",
        "as_of": AS_OF,
        "end_code": "E1_APPROVED",
        "reason": "사용자 선택 대기",
        "plan": [
            StepOut(
                seq=1,
                agent="finance",
                mode="PRE_PURCHASE",
                call_seq=1,
                run_id="FIN-1",
                runtime_status="READY",
                business_status="ok",
                used_tools=["assess_finance_position"],
            )
        ],
    }
    base.update(kw)
    return ProcurementRunResponse(**base)


# ---------------------------------------------------------------------------
# 종료 코드 → 런타임 상태
# ---------------------------------------------------------------------------


def test_E4_만_미가동이다():
    assert persistence.runtime_status_of("E4_NOT_STARTED") == "RUNTIME_NOT_READY"


@pytest.mark.parametrize(
    "end_code", ["E1_APPROVED", "E2_HELD", "E3_REJECTED", "E5_NO_FEASIBLE_PLAN"]
)
def test_나머지는_돌긴_돈_날이다(end_code):
    """보류·반려·계획없음은 회사 상태이지 실행 환경 문제가 아니다."""
    assert persistence.runtime_status_of(end_code) == "READY"


# ---------------------------------------------------------------------------
# plan 직렬화 — 시각을 담지 않는다
# ---------------------------------------------------------------------------


def test_plan_이_JSON_으로_직렬화된다():
    rows = persistence.plan_rows(response())
    assert rows[0]["agent"] == "finance"
    assert rows[0]["used_tools"] == ["assess_finance_position"]


def test_plan_에_시각_필드가_없다():
    """계획은 같은 입력에 같은 값이어야 한다 — 언제 돌았는지는 created_at 이 답한다."""
    row = persistence.plan_rows(response())[0]
    assert not any(k in row for k in ("created_at", "started_at", "timestamp", "elapsed_ms"))


def test_계획이_비어도_적재_형태는_유지된다():
    assert persistence.plan_rows(response(plan=[])) == []


# ---------------------------------------------------------------------------
# 적재 호출 — pytest 안에서는 no-op 이지만 터지지 않아야 한다
# ---------------------------------------------------------------------------


def test_적재는_예외를_올리지_않는다():
    request = ProcurementRunRequest(as_of=AS_OF, policy_version="v1")
    persistence.record(request, response(), elapsed_ms=12)  # 예외 없이 끝나면 통과


# ---------------------------------------------------------------------------
# 조회 API
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_없는_요청은_404(client, monkeypatch):
    def missing(request_id, *, cycle=None):
        raise LookupError(f"실행이력을 찾을 수 없습니다: {request_id}")

    monkeypatch.setattr("app.master.service.get_run_by_request_id", missing)
    r = client.get("/master/runs/REQ-NONE")
    assert r.status_code == 404
    assert "REQ-NONE" in r.json()["detail"]


def test_이력을_찾으면_계획과_지문을_돌려준다(client, monkeypatch):
    row = {
        "request_id": "REQ-20260827-0001",
        "as_of": AS_OF,
        "agent": "master",
        "cycle": "PROCUREMENT",
        "runtime_status": "READY",
        "elapsed_ms": 4210,
        "created_at": datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        "plan": [
            {"seq": 1, "agent": "finance", "mode": "PRE_PURCHASE", "call_seq": 1},
            {"seq": 2, "agent": "purchase", "mode": "GENERATE_SCENARIOS", "call_seq": 1},
        ],
        "request_payload": {"as_of": "2026-08-27"},
        "response_payload": {"end_code": "E1_APPROVED"},
    }
    monkeypatch.setattr("app.master.service.get_run_by_request_id", lambda _, **_kw: row)
    monkeypatch.setattr("app.master.service.get_decisions", lambda _: [])

    data = client.get("/master/runs/REQ-20260827-0001").json()
    assert data["agent"] == "master"
    assert len(data["plan"]) == 2
    assert [tuple(x) for x in data["plan_signature"]] == [
        ("finance", "PRE_PURCHASE", 1),
        ("purchase", "GENERATE_SCENARIOS", 1),
    ]
    assert data["response_payload"]["end_code"] == "E1_APPROVED"


def test_계획이_NULL_이어도_깨지지_않는다(client, monkeypatch):
    """오케·Critic 행에는 plan 이 없다 — 같은 표를 쓰므로 방어한다."""
    row = {
        "request_id": "REQ-X",
        "as_of": AS_OF,
        "agent": "orchestrator",
        "cycle": "PROCUREMENT",
        "runtime_status": "READY",
        "elapsed_ms": None,
        "created_at": datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        "plan": None,
        "request_payload": None,
        "response_payload": None,
    }
    monkeypatch.setattr("app.master.service.get_run_by_request_id", lambda _, **_kw: row)
    monkeypatch.setattr("app.master.service.get_decisions", lambda _: [])

    data = client.get("/master/runs/REQ-X").json()
    assert data["plan"] == []
    assert data["plan_signature"] == []


def test_이미_결정이_붙은_키로_다시_돌면_경고한다(monkeypatch):
    """🔴 **리허설에서 재현한 것이다 (2026-08-29).**

    실행 이력은 append-only 라 같은 키로 두 번 돌면 행이 둘이 되고, 조회는 **최신
    1건**을 돌려준다. 그러면 첫 실행에 걸린 승인이 **두 번째 실행을 가리키는 것처럼**
    보인다 — 두 실행의 같은 라벨이 다른 수량이면 승인한 것과 다른 것이 승인된 것으로
    읽힌다.

    ★ **막지 않고 드러낸다.** 승인 게이트를 마스터가 들고 있으면 안 된다(8/26 회의).
    """
    from app.master.decision import DecisionOut
    from app.master.service import _decision_collision

    row = DecisionOut(
        decision_id=uuid4(),
        request_id="REQ-1",
        decision_seq=1,
        decision="APPROVE",
        scenario_label="기본",
        decided_by="사장",
        end_code_at_decision="E1_APPROVED",
        created_at=datetime.now(UTC),
        is_current=True,
    )
    monkeypatch.setattr("app.master.service.get_decisions", lambda _: [row])
    warnings = _decision_collision("REQ-1")

    assert len(warnings) == 1
    assert "DECISION-COLLISION" in warnings[0]
    assert "기본" in warnings[0]
    assert "새 업무 키" in warnings[0]


def test_결정이_없으면_조용하다(monkeypatch):
    """**할 말이 없으면 안 한다** — 매번 경고를 내면 진짜 충돌이 묻힌다."""
    from app.master.service import _decision_collision

    monkeypatch.setattr("app.master.service.get_decisions", lambda _: [])
    assert _decision_collision("REQ-1") == []
