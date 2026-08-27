"""사용자 결정 기록 — `/master/runs/{id}/decision` · `/decisions`.

★ **공용 DB 를 건드리지 않는다.** 리포지토리 함수를 in-memory 로 갈아 끼운다.
  `.env` 의 `DB_HOST` 가 팀 공용 서버라, 라우터를 그냥 치면 실제 INSERT 가 남는다.

★ 라우터만 격리해 띄운다 (`test_master_api.py` 와 같은 방식).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import decision_service
from app.master.decision import Decision, DecisionOut, mark_current
from app.master.router import router

REQ = "REQ-20260827-0001"
LABELS = ("보수", "기본", "공격")


class FakeStore:
    """`master_decisions` 를 대신하는 append-only 리스트."""

    def __init__(self) -> None:
        self.rows: list[DecisionOut] = []

    def list_decisions(self, request_id: str) -> list[DecisionOut]:
        return mark_current([r for r in self.rows if r.request_id == request_id])

    def save_decision(
        self,
        *,
        request_id: str,
        decision_seq: int,
        decision: Decision,
        decided_by: str,
        end_code_at_decision: str,
        scenario_label: str | None = None,
        condition_text: str | None = None,
        follow_up_request_id: str | None = None,
        note: str | None = None,
    ) -> DecisionOut:
        row = DecisionOut(
            decision_id=uuid4(),
            request_id=request_id,
            decision_seq=decision_seq,
            decision=decision,
            scenario_label=scenario_label,
            condition_text=condition_text,
            decided_by=decided_by,
            follow_up_request_id=follow_up_request_id,
            end_code_at_decision=end_code_at_decision,
            note=note,
            created_at=datetime.now(UTC),
            is_current=True,
        )
        self.rows.append(row)
        return row


def _run_row(end_code: str = "E1_APPROVED", labels: tuple[str, ...] = LABELS) -> dict[str, Any]:
    return {
        "request_id": REQ,
        "response_payload": {
            "end_code": end_code,
            "scenarios": [{"label": label} for label in labels],
        },
    }


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(decision_service, "list_decisions", fake.list_decisions)
    monkeypatch.setattr(decision_service, "save_decision", fake.save_decision)
    return fake


@pytest.fixture
def client(store):
    """`store` 를 인자로 받는 이유 — 리포지토리 patch 가 **먼저** 붙어야 한다.

    쓰지 않는 인자처럼 보이지만 fixture 순서를 강제하는 장치다. 빼면 라우터가
    실제 DB 를 친다.
    """
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _set_run(monkeypatch, row: dict[str, Any] | None) -> None:
    def fake(request_id: str) -> dict[str, Any]:
        if row is None:
            raise LookupError(f"실행 이력이 없다: {request_id}")
        return row

    monkeypatch.setattr(decision_service, "get_run_by_request_id", fake)


def _post(client, **kw):
    body = {"decided_by": "사장"}
    body.update(kw)
    return client.post(f"/master/runs/{REQ}/decision", json=body)


# ── 승인 ────────────────────────────────────────────────────────────────


def test_승인은_제시된_안이면_기록된다(client, monkeypatch):
    _set_run(monkeypatch, _run_row())
    response = _post(client, decision="APPROVE", scenario_label="기본")

    assert response.status_code == 201
    body = response.json()
    assert body["decision"] == "APPROVE"
    assert body["scenario_label"] == "기본"
    assert body["decision_seq"] == 1
    assert body["is_current"] is True
    assert body["end_code_at_decision"] == "E1_APPROVED"


def test_제시되지_않은_안은_거부된다(client, monkeypatch):
    """**이 검사가 핵심이다.** 없는 안을 승인하면 대조할 대상 없이 승인만 남는다."""
    _set_run(monkeypatch, _run_row())
    response = _post(client, decision="APPROVE", scenario_label="초공격")

    assert response.status_code == 422
    assert "내놓은 안이 아니다" in response.json()["detail"]


def test_E4_에는_아무_결정도_받지_않는다(client, monkeypatch):
    """부서가 못 돈 날을 승인하면 아무도 판단하지 않은 계획이 승인된 것으로 남는다."""
    _set_run(monkeypatch, _run_row(end_code="E4_NOT_STARTED", labels=()))
    response = _post(client, decision="REJECT_ALL")

    assert response.status_code == 409


def test_통과안이_없는_날은_승인할_수_없다(client, monkeypatch):
    _set_run(monkeypatch, _run_row(end_code="E3_REJECTED", labels=()))
    response = _post(client, decision="APPROVE", scenario_label="기본")

    assert response.status_code == 409
    assert "승인할 안이 없다" in response.json()["detail"]


# ── 번복 ────────────────────────────────────────────────────────────────


def test_같은_안_재승인은_막고_다른_안으로는_번복된다(client, monkeypatch, store):
    _set_run(monkeypatch, _run_row())

    assert _post(client, decision="APPROVE", scenario_label="기본").status_code == 201
    assert _post(client, decision="APPROVE", scenario_label="기본").status_code == 409

    second = _post(client, decision="APPROVE", scenario_label="보수")
    assert second.status_code == 201
    assert second.json()["decision_seq"] == 2

    # 이전 결정은 지워지지 않고 접힌다
    rows = store.list_decisions(REQ)
    assert [(r.decision_seq, r.scenario_label, r.is_current) for r in rows] == [
        (1, "기본", False),
        (2, "보수", True),
    ]


# ── 거절 · 조건부 재요청 ─────────────────────────────────────────────────


def test_보류된_날도_거절을_기록할_수_있다(client, monkeypatch):
    _set_run(monkeypatch, _run_row(end_code="E2_HELD", labels=()))
    assert _post(client, decision="REJECT_ALL").status_code == 201


def test_반려된_날에_조건부_재요청은_받는다(client, monkeypatch):
    """조건 바꿔 다시 묻는 것은 정상 업무다."""
    _set_run(monkeypatch, _run_row(end_code="E3_REJECTED", labels=()))
    response = _post(client, decision="REQUEST_CHANGE", condition_text="예산을 2천만원으로")

    assert response.status_code == 201
    assert response.json()["condition_text"] == "예산을 2천만원으로"


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "APPROVE"},  # 라벨 없음
        {"decision": "REJECT_ALL", "scenario_label": "기본"},  # 거절인데 라벨
        {"decision": "REQUEST_CHANGE"},  # 조건 없음
        {"decision": "APPROVE", "scenario_label": "기본", "decided_by": ""},  # 승인자 없음
    ],
)
def test_모양이_안_맞는_요청은_입구에서_막힌다(client, monkeypatch, payload):
    _set_run(monkeypatch, _run_row())
    body = {"decided_by": "사장"}
    body.update(payload)
    assert client.post(f"/master/runs/{REQ}/decision", json=body).status_code == 422


# ── 조회 ────────────────────────────────────────────────────────────────


def test_없는_요청에_결정하면_404(client, monkeypatch):
    _set_run(monkeypatch, None)
    assert _post(client, decision="REJECT_ALL").status_code == 404


def test_결정_이력은_오래된_것부터_최신만_current(client, monkeypatch):
    _set_run(monkeypatch, _run_row())
    _post(client, decision="APPROVE", scenario_label="기본")
    _post(client, decision="APPROVE", scenario_label="공격")

    rows = client.get(f"/master/runs/{REQ}/decisions").json()
    assert [r["scenario_label"] for r in rows] == ["기본", "공격"]
    assert [r["is_current"] for r in rows] == [False, True]


def test_결정이_없으면_빈_목록(client, monkeypatch):
    _set_run(monkeypatch, _run_row())
    assert client.get(f"/master/runs/{REQ}/decisions").json() == []
