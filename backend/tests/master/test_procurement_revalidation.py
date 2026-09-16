"""**매입 승인의 재검증은 매입의 물음으로 묻는다** (2026-09-16 실측).

```text
실측  SIM-TEST-PURREC-0916 · 01-05 배추 사람 승인
      revalidation_outcome = FAILED
      REV-SIM-TEST-PURREC-0916-20260105-0001  cycle = SALES
      reason 「재검증에서 막혔다: FINANCIAL_VALIDATION(READY/skipped)」
```

원인은 매입 승인도 판매 재검증(`revalidate_scenario`)을 탔던 것이다. 거기서
`FINANCIAL_VALIDATION` 은 재무 `SALES_VALIDATION` 으로 가고, 매입 안에는 판매 사실이
없어 재무가 `INPUT_INCOMPLETE` → `READY/skipped` 로 답했다. skipped 는 허용목록 밖이라
늘 `FAILED` 였다.

이 판이 잠그는 것.

```text
①  매입 실행 승인 → 재무 · 물류 SCENARIO_VALIDATION 을 부른다 · 판매 mode 는 안 부른다
②  묻는 모양은 매입 Flow ④ 와 같다 — 제안 최상위(judgment) + 고른 안 하나
③  이력 행은 cycle=PROCUREMENT
④  판정이 결과를 가른다 — ok 둘이면 PASSED · reject · skipped 면 FAILED
⑤  🔴 재검증 결과가 승인 전이를 막지 않는다 (기존 동작) — 자동은 전이 · 사람은 보류
⑥  판매 실행 승인은 여전히 판매 재검증으로 간다
```

⚠️ **DB 를 안 탄다.** 결정 저장 · 실행 행 · 전이 · 부서가 전부 대역이다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.master import decision_service as svc
from app.master import persistence, wiring
from app.master.commitment import ApprovedCommitment
from app.master.decision import AUTO_BACKFILL, DecisionIn, DecisionOut
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.transition import TransitionOut

실행축 = "SIM-TEST-PURREC"
업무키 = "REQ-DAILY-SIM-TEST-PURREC-20260105-배추"
기준일 = date(2026, 1, 5)


def _안() -> dict[str, Any]:
    return {
        "label": "보수",
        "total_qty_kg": 300,
        "total_amount_krw": 270000,
        "split_plan": [
            {
                "seq": 1,
                "date": "2026-01-05",
                "qty_kg": 300,
                "amount_krw": 270000,
                "expected_arrival_date": "2026-01-06",
            }
        ],
        "sourcing_plan": [{"market": "가락", "grade": "특", "qty_kg": 300}],
    }


def _판정부() -> dict[str, Any]:
    return {"meta": {"as_of": 기준일.isoformat(), "item": "배추"}, "situation": "정상"}


def _실행행(cycle: str = "PROCUREMENT") -> dict[str, Any]:
    return {
        "run_id": uuid4(),
        "request_id": 업무키,
        "cycle": cycle,
        "item": "배추",
        "sim_run_id": 실행축,
        "request_payload": {"policy_version": "v1.3"},
        "response_payload": {
            "end_code": "E1_APPROVED",
            "as_of": 기준일.isoformat(),
            "scenarios": [_안()],
            "judgment": _판정부(),
            "constraints": {
                "inventory": {"inbound_lead_days": 1},
                "finance": {"purchase_payment_days": 0},
            },
        },
    }


class _부서:
    """등록된 어댑터 대역. **무슨 mode · payload 로 물었는지 남긴다.**"""

    def __init__(self, business_status: str = "ok") -> None:
        self.business_status = business_status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.calls.append((request.mode, dict(request.payload)))
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status=self.business_status,
            reasoning="대역",
        )
        return reply, ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("tool_a",),
            tool_order=(1,),
        )


@pytest.fixture
def 부서들() -> dict[str, _부서]:
    wiring.reset()  # 루트 conftest 가 스냅샷을 떠 두므로 이 파일 밖으로 안 샌다
    등록 = {"finance": _부서(), "inventory": _부서()}
    for 이름, 포트 in 등록.items():
        wiring.register(이름, 포트)
    return 등록


@pytest.fixture
def 세상(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"row": _실행행(), "saved": [], "applied": [], "runs": []}

    def _save(**kw: Any) -> DecisionOut:
        state["saved"].append(kw)
        return DecisionOut(
            decision_id=uuid4(),
            created_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
            is_current=True,
            **kw,
        )

    def _apply(commitment: ApprovedCommitment, *, sim_run_id: str | None, **_: Any):
        state["applied"].append(commitment)
        return TransitionOut(status="APPLIED", parts=["finance", "logistics"])

    monkeypatch.setattr(svc, "_run_for", lambda request_id, history_run_id: state["row"])
    monkeypatch.setattr(svc, "list_decisions", lambda request_id: [])
    monkeypatch.setattr(svc, "save_decision", _save)
    monkeypatch.setattr(svc, "apply_approval", _apply)
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: state["runs"].append(kw))
    return state


def _승인(decided_by: str = "이현서") -> DecisionOut:
    return svc.record_decision(
        업무키, DecisionIn(decision="APPROVE", scenario_label="보수", decided_by=decided_by)
    )


def test_매입_승인은_재무_물류_SCENARIO_VALIDATION_을_부른다(
    세상: dict[str, Any], 부서들: dict[str, _부서]
) -> None:
    """① 🔴 판매 mode(`SALES_VALIDATION` · `PRE_SALES`)로 물으면 재무가 skipped 로 답한다."""
    _승인()

    assert [m for m, _ in 부서들["finance"].calls] == ["SCENARIO_VALIDATION"]
    assert [m for m, _ in 부서들["inventory"].calls] == ["SCENARIO_VALIDATION"]


def test_묻는_모양은_제안_최상위에_고른_안_하나다(
    세상: dict[str, Any], 부서들: dict[str, _부서]
) -> None:
    """② 재무 · 물류가 `meta.as_of` · `meta.item` 을 제안 최상위에서 읽는다 (`flow._validate`)."""
    _승인()

    for 부 in 부서들.values():
        [(_, payload)] = 부.calls
        assert payload["meta"] == _판정부()["meta"]
        assert payload["situation"] == "정상"
        assert payload["scenarios"] == [_안()]


def test_매입_재검증_이력은_매입_사이클이다(세상: dict[str, Any], 부서들: dict[str, _부서]) -> None:
    """③ 실측에서는 `cycle=SALES` 로 남았다."""
    saved = _승인()

    [run] = 세상["runs"]
    assert run["cycle"] == "PROCUREMENT"
    assert run["request_id"] == saved.revalidation_request_id
    assert run["request_payload"]["capabilities"] == ["finance", "inventory"]


@pytest.mark.parametrize(
    ("재무", "물류", "기대"),
    [
        ("ok", "ok", "PASSED"),
        ("conditional", "ok", "CONDITIONAL"),
        ("reject", "ok", "FAILED"),
        ("ok", "skipped", "FAILED"),
    ],
)
def test_조언자_판정이_재검증_결과를_가른다(
    세상: dict[str, Any], 부서들: dict[str, _부서], 재무: str, 물류: str, 기대: str
) -> None:
    """④ ★ skipped 는 통과가 아니다 — 원 실행이 `E1_APPROVED` 로 올라온 것이 곧 두 조언자가
    규칙으로 판정을 냈다는 뜻이라, 재검증에서 skipped 가 오면 *"못 봤다"* 다."""
    부서들["finance"].business_status = 재무
    부서들["inventory"].business_status = 물류

    saved = _승인()

    assert saved.revalidation_outcome == 기대
    assert 세상["saved"][-1]["revalidation_outcome"] == 기대


def test_사람_승인은_재검증이_막혀도_보류_그대로다(
    세상: dict[str, Any], 부서들: dict[str, _부서]
) -> None:
    """⑤ 🔴 기존 동작. 사람 승인은 어차피 전이를 보류한다."""
    부서들["finance"].business_status = "reject"

    saved = _승인()

    assert saved.revalidation_outcome == "FAILED"
    assert saved.transition is not None
    assert saved.transition.status == "AWAITING_PURCHASE_RECORD"
    assert 세상["applied"] == []


def test_자동_승인은_재검증이_막혀도_전이한다(
    세상: dict[str, Any], 부서들: dict[str, _부서]
) -> None:
    """⑤ 🔴 기존 동작. 재검증 결과로 자동 승인 전이를 막지 않는다."""
    부서들["finance"].business_status = "reject"

    saved = _승인(AUTO_BACKFILL)

    assert saved.revalidation_outcome == "FAILED"
    assert saved.transition is not None and saved.transition.status == "APPLIED"
    assert len(세상["applied"]) == 1


def test_제안_최상위를_못_읽으면_ERROR_이고_부서를_안_부른다(
    세상: dict[str, Any], 부서들: dict[str, _부서]
) -> None:
    del 세상["row"]["response_payload"]["judgment"]

    saved = _승인()

    assert saved.revalidation_outcome == "ERROR"
    assert saved.revalidation_request_id is None
    assert [부.calls for 부 in 부서들.values()] == [[], []]


def test_판매_실행이_아니면_판매_재검증으로_간다(
    monkeypatch: pytest.MonkeyPatch, 세상: dict[str, Any]
) -> None:
    """⑥ 사이클은 실행 행이 정한다. 매입이 아닌 행은 지금까지의 경로 그대로다."""
    seen: list[str] = []
    monkeypatch.setattr(svc, "revalidate_scenario", lambda **kw: seen.append("sales") or _통과())
    monkeypatch.setattr(
        svc, "revalidate_procurement_scenario", lambda **kw: seen.append("procurement") or _통과()
    )
    row = _실행행(cycle="")

    svc._revalidate_scenario_of(row, row["response_payload"], "보수", _안(), 1, sim_run_id=실행축)
    svc._revalidate_scenario_of(
        _실행행(), _실행행()["response_payload"], "보수", _안(), 1, sim_run_id=실행축
    )

    assert seen == ["sales", "procurement"]


def _통과():
    from app.master.revalidation import Revalidation

    return Revalidation(outcome="PASSED", request_id="REV-X")
