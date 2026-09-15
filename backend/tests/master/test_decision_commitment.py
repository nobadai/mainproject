"""승인 응답에 **확정 입고 약정이 같이 나간다** (H1 · ⓐ).

🔴 **실측 2026-09-01 — 처음에 제가 반대로 만들었다.**

`_as_of_of` 가 기준일을 못 읽으면 `DecisionRejected` 를 올리게 했는데, 그 시점은
**결정이 이미 적재된 뒤**였다. 저장은 되고 응답은 409 가 나갔다 — *"약정을 못 만들어도
결정은 남아야 한다"* 고 docstring 에 적어 놓고 그 반대를 했다.

이 파일이 그 자리를 잠근다. **약정 조립은 결정을 죽이지 않는다.**
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.master import decision_service as svc
from app.master.decision import DecisionIn, DecisionOut

AS_OF = date(2025, 12, 31)


def _scenario(label: str = "보수", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "label": label,
        "total_qty_kg": 44.0,
        "total_amount_krw": 228800.0,
        "split_plan": [{"seq": 1, "date": "2025-12-31", "qty_kg": 44.0}],
    }
    base.update(over)
    return base


def _response(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "end_code": "E1_APPROVED",
        "as_of": "2025-12-31",
        "scenarios": [_scenario()],
        "judgment": {"meta": {"item": "배추"}},
        "constraints": {"inventory": {"inbound_lead_days": 2.0}},
    }
    base.update(over)
    return base


@pytest.fixture
def wired(monkeypatch):
    """DB 를 걷어내고 결정 경로만 남긴다."""
    saved: dict[str, Any] = {}

    def _run_for(request_id, history_run_id):
        return {"run_id": uuid4(), "request_id": request_id, "response_payload": saved["response"]}

    def _save(**kw):
        return DecisionOut(
            decision_id=uuid4(),
            created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            is_current=True,
            **kw,
        )

    monkeypatch.setattr(svc, "_run_for", _run_for)
    monkeypatch.setattr(svc, "list_decisions", lambda request_id: [])
    monkeypatch.setattr(svc, "save_decision", _save)

    def _record(response: dict[str, Any], **payload: Any) -> DecisionOut:
        saved["response"] = response
        body = {"decision": "APPROVE", "scenario_label": "보수", "decided_by": "lhs"}
        body.update(payload)
        return svc.record_decision("REQ-1", DecisionIn(**body))

    return _record


def test_승인하면_약정이_같이_나온다(wired):
    out = wired(_response())

    assert out.commitment is not None
    commitment = out.commitment
    assert commitment.buildable
    assert commitment.item == "배추"
    assert commitment.approval_id == "H1-REQ-1-1"
    assert [leg.arrival_date for leg in commitment.arrival_schedule] == [date(2026, 1, 2)]
    assert commitment.first_arrival == date(2026, 1, 2)


def test_승인이_아니면_약정이_없다(wired):
    out = wired(_response(), decision="REJECT_ALL", scenario_label=None)

    assert out.commitment is None, "거절에 약정이 붙으면 무엇을 약속한 것인지 모른다"


def test_기준일이_없어도_결정은_남는다(wired):
    """🔴 이 자리에서 예외를 올려 결정이 적재된 뒤 409 가 나갔다."""
    response = _response()
    del response["as_of"]

    out = wired(response)

    assert out.decision == "APPROVE"
    assert out.commitment is not None
    assert out.commitment.buildable is False
    assert "기준일" in (out.commitment.reason or "")


def test_품목이_없어도_결정은_남고_이유가_실린다(wired):
    out = wired(_response(judgment={"meta": {}}))

    assert out.decision == "APPROVE"
    assert out.commitment.buildable is False
    assert "품목" in (out.commitment.reason or "")


def test_N4_가_없으면_일정만_비고_약정은_선다(wired):
    """**빈 일정과 못 만든 약정을 가른다.** 둘 다 비면 물류가 구분할 수 없다."""
    out = wired(_response(constraints={"inventory": {}}))

    commitment = out.commitment
    assert commitment.buildable is True, "약정 자체는 서야 한다"
    assert commitment.arrival_schedule == []
    assert any("N4" in note for note in commitment.notes), commitment.notes


def test_승인한_안을_라벨로_찾는다(wired):
    """순서로 고르면 라벨이 바뀌는 날 다른 안이 승인된 것으로 남는다."""
    response = _response(
        scenarios=[
            _scenario("보수"),
            _scenario(
                "공격",
                total_qty_kg=100.0,
                total_amount_krw=520000.0,
                split_plan=[{"seq": 1, "date": "2025-12-31", "qty_kg": 100.0}],
            ),
        ]
    )
    out = wired(response, scenario_label="공격")

    assert out.commitment.scenario_label == "공격"
    assert out.commitment.total_qty_kg == 100.0


def test_라벨이_겹치면_첫_것을_고르지_않고_막는다(wired):
    """🔴 첫 것을 조용히 고르면 어느 안을 약정했는지가 운에 걸린다 (자기 리뷰)."""
    response = _response(scenarios=[_scenario("보수"), _scenario("보수", total_qty_kg=999.0)])

    out = wired(response)

    assert out.decision == "APPROVE", "결정 자체는 남아야 한다"
    assert out.commitment.buildable is False
    assert "유일" in (out.commitment.reason or "") or "2개" in (out.commitment.reason or "")


# ---------------------------------------------------------------------------
# GET /runs/{id}/commitment — 전달 방식 ⓐ (물류 회신 2026-09-01)
# ---------------------------------------------------------------------------


def _current(monkeypatch, decisions, response):
    monkeypatch.setattr(svc, "list_decisions", lambda request_id: decisions)
    monkeypatch.setattr(
        svc,
        "_run_for",
        lambda request_id, history_run_id: {
            "run_id": uuid4(),
            "request_id": request_id,
            "response_payload": response,
        },
    )
    return svc.current_commitment("REQ-1")


def _decision(**over):
    base = {
        "decision_id": uuid4(),
        "request_id": "REQ-1",
        "decision_seq": 1,
        "decision": "APPROVE",
        "scenario_label": "보수",
        "decided_by": "lhs",
        "end_code_at_decision": "E1_APPROVED",
        "created_at": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        "is_current": True,
    }
    base.update(over)
    return DecisionOut(**base)


def test_현재_승인의_약정을_재조립한다(monkeypatch):
    out = _current(monkeypatch, [_decision()], _response())

    assert out is not None and out.buildable
    assert out.approval_id == "H1-REQ-1-1"
    assert out.item == "배추"


def test_승인이_없으면_None_이다(monkeypatch):
    assert _current(monkeypatch, [], _response()) is None
    assert (
        _current(
            monkeypatch,
            [_decision(decision="REJECT_ALL", scenario_label=None)],
            _response(),
        )
        is None
    )


def test_번복되면_현재_결정의_약정만_나온다(monkeypatch):
    """★ 앞 승인(보수)은 접혔다 — is_current 인 공격만 재조립된다.

    🔴 **이 상태는 지금 API 로 만들 수 없다** (`#290`). `_reject_repeat_approval` 이
       승인 위에 다른 승인을 얹는 것을 막으므로, 승인 둘이 한 요청에 붙은 이력은
       `record_decision` 을 지나서는 안 생긴다. 이 검사는 `DecisionOut` 을 손으로
       만들어 그 상태를 흉내 낸다.

    ⚠️ **그래서 초록인 것이 "이 경로가 돈다" 는 뜻이 아니다.** 검사는 살아 있고
       재조립 규칙도 살아 있지만, 그 규칙을 밟는 실행이 없다.

    ★ **그래도 지운다면 잃는 것이 있다.** 취소 경로(`purchases.CANCELLED` ·
      payable 역분개 · `confirmed_inbound` 정리)가 생기면 `#290` 의 차단이
      *"취소되지 않은 승인이 있으면 막는다"* 로 완화되고, 그때 이 상태가 다시
      API 로 생긴다. 그날 *"번복되면 약정이 어떻게 되나"* 를 처음부터 다시 묻지
      않으려고 남긴다.
    """
    decisions = [
        _decision(decision_seq=1, scenario_label="보수", is_current=False),
        _decision(decision_seq=2, scenario_label="공격", is_current=True),
    ]
    response = _response(
        scenarios=[
            _scenario("보수"),
            _scenario(
                "공격",
                total_qty_kg=100.0,
                split_plan=[{"seq": 1, "date": "2025-12-31", "qty_kg": 100.0}],
            ),
        ]
    )

    out = _current(monkeypatch, decisions, response)

    assert out.scenario_label == "공격"
    assert out.approval_id == "H1-REQ-1-2"


def test_못_만든_약정은_None_이_아니라_사유다(monkeypatch):
    """404(승인 없음)와 buildable=false(승인인데 못 만듦)를 섞지 않는다."""
    response = _response(judgment={"meta": {}})  # 품목 없음

    out = _current(monkeypatch, [_decision()], response)

    assert out is not None
    assert out.buildable is False
    assert "품목" in (out.reason or "")
