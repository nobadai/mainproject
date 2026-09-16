"""**사람 승인은 실매입 기록을 기다리고, 기록값으로 전이가 선다** (설계 260915 안 A).

```text
[전]  승인 ──(같은 커밋)──▶ 전이   ← 안의 계획값
[후]  승인(선정만 · 전이 보류) → 실매입 기록 ──▶ 전이   ← 실매입 값
      자동 승인(AUTO-BACKFILL) 은 지금 그대로 승인 즉시
```

이 판이 잠그는 것 (§4-5 · §4-6).

```text
①  AUTO-BACKFILL 승인은 지금처럼 즉시 전이 (회귀)
②  사람 승인은 전이를 안 부르고 AWAITING_PURCHASE_RECORD
③  기록 → 사본 약정의 회차 값 · 합계 · 지급일 · 등급이 기록값 · apply_approval 1회
④  seq 집합이 다르면 · 수량/금액 0 · 도착일 < 매입일 · 중복 기록 → 거부
⑤  재시도가 기록 없는 사람 승인을 건너뛰고, 기록 있는 것은 기록값으로
⑥  기록값이 선정안과 다르면 재검증 · 불통과면 저장 0 · 전이 0 · 같으면 재검증 안 부름
⑦  매입일 < 승인 실행 as_of · 지급기일 < 마지막 재무 일마감일 → 거부 (회차마다)
    · 지급기일 > 마감일 → 받는다
    · 지급기일 == 마감일 → 승인 기준일 == 매입일 == 마감일 일 때만 받는다 (재무 합의 9/16)
⑨  기록값 재검증이 재무 · 물류 SCENARIO_VALIDATION 의 실제 판정으로 갈린다
⑧  다른 sim_run_id 의 같은 request_id 기록은 별개
⑩  선검사 — 1회차 매입일 != 승인한 날 · 소수점 수량 · 소수점 금액을 사람 말로 거부
    (매입안 계약이 알기 어려운 말로 막기 전에 · 2026-09-16)
```

⚠️ **DB 를 안 탄다.** 결정 · 실행 행 · 기록 표 · 전이 · 재검증이 전부 대역이다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.master import decision_service as svc
from app.master import purchase_record as pr
from app.master.commitment import ApprovedCommitment, RecordedLeg
from app.master.decision import (
    AUTO_BACKFILL,
    DecisionIn,
    DecisionOut,
    DecisionRejected,
    PurchaseRecordIn,
)
from app.master.pending_transition import pending_approvals, retry_pending_transitions
from app.master.revalidation import Revalidation
from app.master.transition import TransitionOut

실행축 = "SIM-A"
업무키 = "REQ-DAILY-20260911-배추"
기준일 = date(2026, 9, 11)
사람 = "lhs"


def _안() -> dict[str, Any]:
    """분할 2회차 · 금액 · 도착일 · 지급 일정 · 등급이 실린 매입 안."""
    return {
        "label": "기본",
        "total_qty_kg": 300,
        "total_amount_krw": 1500000,
        "max_price": 6000,
        "split_plan": [
            {
                "seq": 1,
                "date": "2026-09-11",
                "qty_kg": 100,
                "amount_krw": 500000,
                "expected_arrival_date": "2026-09-12",
            },
            {
                "seq": 2,
                "date": "2026-09-14",
                "qty_kg": 200,
                "amount_krw": 1000000,
                "expected_arrival_date": "2026-09-15",
            },
        ],
        "payment_schedule": [
            {
                "seq": 1,
                "purchase_date": "2026-09-11",
                "payment_date": "2026-09-18",
                "qty_kg": 100,
                "amount_krw": 500000,
                "amount_max_krw": 600000,
                "basis": "QUOTE",
            },
            {
                "seq": 2,
                "purchase_date": "2026-09-14",
                "payment_date": "2026-09-21",
                "qty_kg": 200,
                "amount_krw": 1000000,
                "amount_max_krw": 1200000,
                "basis": "QUOTE",
            },
        ],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": 300, "grade_unit_price": 5000}
        ],
    }


def _응답() -> dict[str, Any]:
    return {
        "end_code": "E1_APPROVED",
        "as_of": 기준일.isoformat(),
        "scenarios": [_안()],
        "judgment": {"meta": {"item": "배추"}},
        "constraints": {
            "inventory": {"inbound_lead_days": 1},
            "finance": {"purchase_payment_days": 7},
        },
    }


def _실행행(*, sim_run_id: str | None = 실행축) -> dict[str, Any]:
    return {
        "run_id": uuid4(),
        "request_id": 업무키,
        "cycle": "PROCUREMENT",
        "sim_run_id": sim_run_id,
        "request_payload": {"policy_version": "v1.3"},
        "response_payload": _응답(),
    }


def _결정(*, decided_by: str = 사람, seq: int = 1) -> DecisionOut:
    return DecisionOut(
        decision_id=uuid4(),
        request_id=업무키,
        decision_seq=seq,
        decision="APPROVE",
        scenario_label="기본",
        decided_by=decided_by,
        end_code_at_decision="E1_APPROVED",
        created_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        is_current=True,
    )


class _커넥션:
    """세는 것만 한다. **커밋 전 적재는 `pending`, 커밋 뒤에 `store` 로.**"""

    def __init__(self, store: list[dict[str, Any]]) -> None:
        self.store = store
        self.pending: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1
        self.store.extend(self.pending)
        self.pending = []

    def rollback(self) -> None:
        self.rollbacks += 1
        self.pending = []

    def close(self) -> None:
        pass


class _전이:
    """`apply_approval` 대역. **커넥션을 열고 커밋하는** 실제 모양을 흉내 낸다."""

    def __init__(self, out: TransitionOut | None = None, *, opens: bool = True) -> None:
        self.out = out or TransitionOut(status="APPLIED", parts=["finance", "logistics"])
        self.opens = opens
        self.calls: list[tuple[ApprovedCommitment, str | None]] = []

    def __call__(self, commitment: ApprovedCommitment, *, sim_run_id: str | None, **kw: Any):
        self.calls.append((commitment, sim_run_id))
        connect = kw.get("connect")
        if self.opens and connect is not None:
            conn = connect()
            conn.commit()
        return self.out


class _재검증:
    def __init__(self, outcome: str = "PASSED", reason: str = "") -> None:
        self.outcome = outcome
        self.reason = reason
        self.scenarios: list[dict[str, Any]] = []

    def __call__(self, approval: Any, scenario: dict[str, Any]) -> Revalidation:
        self.scenarios.append(scenario)
        return Revalidation(outcome=self.outcome, request_id="REV-X", reason=self.reason)  # type: ignore[arg-type]


@pytest.fixture
def 세상(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """결정 · 실행 행 · 기록 표 · 마감일 · 재검증 대역을 한 번에 꽂는다."""
    state: dict[str, Any] = {
        "decisions": [_결정()],
        "row": _실행행(),
        "store": [],
        "closed": None,
        "reval": _재검증(),
        "conns": [],
    }

    def _rows(*, sim_run_id: str, request_id: str, decision_seq: int) -> list[dict[str, Any]]:
        return [
            r
            for r in state["store"]
            if (r["sim_run_id"], r["request_id"], r["decision_seq"])
            == (sim_run_id, request_id, decision_seq)
        ]

    def _insert(conn: _커넥션, *, sim_run_id, request_id, decision_seq, grade, recorded_by, legs):
        for leg in legs:
            conn.pending.append(
                {
                    "sim_run_id": sim_run_id,
                    "request_id": request_id,
                    "decision_seq": decision_seq,
                    "leg_seq": leg.seq,
                    "quantity_kg": leg.qty_kg,
                    "amount_krw": leg.amount_krw,
                    "purchase_date": leg.purchase_date,
                    "arrival_date": leg.arrival_date,
                    "grade": grade,
                    "recorded_by": recorded_by,
                    "recorded_at": datetime(2026, 9, 11, 13, 0, tzinfo=UTC),
                }
            )

    monkeypatch.setattr(svc, "list_decisions", lambda request_id: state["decisions"])
    monkeypatch.setattr(svc, "_run_for", lambda request_id, history_run_id: state["row"])
    monkeypatch.setattr(svc, "list_purchase_record_legs", _rows)
    monkeypatch.setattr(pr, "list_purchase_record_legs", _rows)
    monkeypatch.setattr(pr, "insert_purchase_record_legs", _insert)
    monkeypatch.setattr(pr, "last_closed_date", lambda *, sim_run_id: state["closed"])
    monkeypatch.setattr(pr, "revalidate_recorded", lambda a, s: state["reval"](a, s))

    def _connect() -> _커넥션:
        conn = _커넥션(state["store"])
        state["conns"].append(conn)
        return conn

    state["connect"] = _connect
    return state


def _본문(**over: Any) -> dict[str, Any]:
    """선정안 값 그대로의 기록."""
    body: dict[str, Any] = {
        "decision_seq": 1,
        "grade": "상",
        "recorded_by": "이현서",
        "legs": [
            {
                "seq": 1,
                "qty_kg": 100,
                "amount_krw": 500000,
                "purchase_date": "2026-09-11",
                "arrival_date": "2026-09-12",
            },
            {
                "seq": 2,
                "qty_kg": 200,
                "amount_krw": 1000000,
                "purchase_date": "2026-09-14",
                "arrival_date": "2026-09-15",
            },
        ],
    }
    body.update(over)
    return body


def _실매입(**over: Any) -> dict[str, Any]:
    """선정안과 **다른** 기록 — 1회차 수량 · 금액 · 도착일 · 등급이 바뀌었다.

    🔴 **1회차 매입일은 승인 기준일 그대로다** (2026-09-16 선검사). 사람이 여기를 다른
      날로 적으면 매입안 계약 `split_plan[0].date == meta.as_of` 가 재검증에서 떨어진다
      — 그래서 입구에서 먼저 막는다 (`_check_recordable_values`). 실매입이 선정안과
      다른 것은 수량 · 금액 · 도착일 · 등급으로 충분히 잰다.
    """
    body = _본문(grade="특", **over)
    body["legs"][0] = {
        "seq": 1,
        "qty_kg": 90,
        "amount_krw": 480000,
        "purchase_date": "2026-09-11",
        "arrival_date": "2026-09-13",
    }
    return body


def _기록한다(세상: dict[str, Any], body: dict[str, Any], 전이: _전이 | None = None):
    문 = 전이 or _전이()
    out = pr.record_purchase(업무키, PurchaseRecordIn(**body), connect=세상["connect"], apply_fn=문)
    return out, 문


# ══════════════════════════════════════════════════════════════════════
#  ① ② 승인 — 자동은 즉시 · 사람은 보류
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def 승인문(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: list[ApprovedCommitment] = []

    def _save(**kw: Any) -> DecisionOut:
        return DecisionOut(
            decision_id=uuid4(),
            created_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
            is_current=True,
            **kw,
        )

    def _apply(commitment: ApprovedCommitment, *, sim_run_id: str | None, **_: Any):
        calls.append(commitment)
        return TransitionOut(status="APPLIED", parts=["finance", "logistics"])

    monkeypatch.setattr(svc, "_run_for", lambda request_id, history_run_id: _실행행())
    monkeypatch.setattr(svc, "list_decisions", lambda request_id: [])
    monkeypatch.setattr(svc, "save_decision", _save)
    monkeypatch.setattr(svc, "_revalidation_for", lambda *a, **k: None)
    monkeypatch.setattr(svc, "apply_approval", _apply)

    def _approve(decided_by: str) -> DecisionOut:
        return svc.record_decision(
            업무키, DecisionIn(decision="APPROVE", scenario_label="기본", decided_by=decided_by)
        )

    return {"approve": _approve, "calls": calls}


def test_자동_승인은_지금처럼_승인_즉시_전이한다(승인문: dict[str, Any]) -> None:
    """① **회귀.** 걷기 자동 승인은 이 판으로 바뀌면 안 된다."""
    out = 승인문["approve"](AUTO_BACKFILL)

    assert len(승인문["calls"]) == 1, "자동 승인이 전이를 안 불렀다"
    assert out.transition is not None and out.transition.status == "APPLIED"
    assert 승인문["calls"][0].arrival_schedule[0].qty_kg == 100, "자동 승인은 계획값이다"


def test_사람_승인은_전이를_안_부르고_기록을_기다린다(승인문: dict[str, Any]) -> None:
    """② 🔴 **사람 승인이 계획값으로 원장을 쓰면 실매입 기록이 뜻이 없다.**"""
    out = 승인문["approve"](사람)

    assert 승인문["calls"] == [], "사람 승인이 전이를 불렀다 — 계획값이 원장에 앉는다"
    assert out.transition is not None
    assert out.transition.status == "AWAITING_PURCHASE_RECORD"
    assert out.transition.reason == "실매입을 기록하면 반영됩니다"
    assert out.commitment is not None and out.commitment.buildable, "선정 약정은 그대로 나간다"


# ══════════════════════════════════════════════════════════════════════
#  ③ 기록 → 기록값 약정으로 전이 1회
# ══════════════════════════════════════════════════════════════════════


def test_기록하면_기록값으로_덮은_사본으로_전이가_한_번_선다(세상: dict[str, Any]) -> None:
    out, 전이 = _기록한다(세상, _실매입())

    assert out.status == "APPLIED"
    assert len(전이.calls) == 1, "apply_approval 이 한 번이 아니다"
    commitment, sim_run_id = 전이.calls[0]
    assert sim_run_id == 실행축, "축은 승인 실행 행에서 온다"

    첫회, 둘째 = commitment.arrival_schedule
    assert (첫회.seq, 첫회.qty_kg, 첫회.amount_krw) == (1, 90.0, 480000.0)
    assert (첫회.purchase_date, 첫회.arrival_date) == (date(2026, 9, 11), date(2026, 9, 13))
    assert 첫회.payment_due_date == date(2026, 9, 18), "지급일 = 기록 매입일 + N5(7)"
    assert (둘째.qty_kg, 둘째.amount_krw) == (200.0, 1000000.0)
    assert commitment.total_qty_kg == 290.0
    assert commitment.total_amount_krw == 1480000.0
    assert commitment.grades == ("특",), "등급이 기록값이 아니다"
    assert commitment.approval_id == f"H1-{업무키}-1", "승인 id 는 선정안 그대로다"

    [conn] = 세상["conns"]
    assert conn.commits == 1, "기록과 전이가 한 커밋이 아니다"
    assert len(세상["store"]) == 2, "회차마다 한 행이 남아야 한다"


def test_전이가_커넥션_앞에서_돌아서도_기록은_남는다(세상: dict[str, Any]) -> None:
    """⚠️ §4-3 — `NOT_APPLIED` 면 기록은 남고 다음 날 재시도가 기록값으로 다시 시도한다."""
    문 = _전이(TransitionOut(status="NOT_APPLIED", reason="도착분 없음"), opens=False)
    out, _ = _기록한다(세상, _본문(), 문)

    assert out.status == "NOT_APPLIED"
    assert len(세상["store"]) == 2


# ══════════════════════════════════════════════════════════════════════
#  ④ 거부
# ══════════════════════════════════════════════════════════════════════


def test_회차_집합이_선정안과_다르면_거부한다(세상: dict[str, Any]) -> None:
    body = _본문()
    body["legs"] = body["legs"][:1]

    with pytest.raises(DecisionRejected) as caught:
        _기록한다(세상, body)
    assert caught.value.conflict is False, "본문이 틀린 것이다 (422)"
    assert 세상["store"] == [] and 세상["conns"] == []


@pytest.mark.parametrize("칸", ["qty_kg", "amount_krw"])
def test_수량이나_금액이_0_이면_거부한다(칸: str) -> None:
    body = _본문()
    body["legs"][0][칸] = 0

    with pytest.raises(ValidationError):
        PurchaseRecordIn(**body)


def test_도착일이_매입일보다_앞서면_거부한다() -> None:
    body = _본문()
    body["legs"][0]["arrival_date"] = "2026-09-10"

    with pytest.raises(ValidationError):
        PurchaseRecordIn(**body)


def test_이미_기록한_승인은_다시_못_적는다(세상: dict[str, Any]) -> None:
    _기록한다(세상, _본문())

    with pytest.raises(DecisionRejected) as caught:
        _기록한다(세상, _본문())
    assert caught.value.conflict is True, "상태 충돌이다 (409)"
    assert len(세상["store"]) == 2, "두 번째 기록이 앉았다"


def test_자동_승인에는_기록을_받지_않는다(세상: dict[str, Any]) -> None:
    세상["decisions"] = [_결정(decided_by=AUTO_BACKFILL)]

    with pytest.raises(DecisionRejected) as caught:
        _기록한다(세상, _본문())
    assert caught.value.conflict is True


def test_승인이_없으면_LookupError(세상: dict[str, Any]) -> None:
    세상["decisions"] = []

    with pytest.raises(LookupError):
        _기록한다(세상, _본문())


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 기록값 재검증 (§4-6 ①)
# ══════════════════════════════════════════════════════════════════════


def test_선정안과_같으면_재검증을_안_부른다(세상: dict[str, Any]) -> None:
    _기록한다(세상, _본문())

    assert 세상["reval"].scenarios == [], "값이 같은데 재검증을 불렀다 — 예산만 태운다"


def test_선정안과_다르면_기록값_사본으로_재검증한다(세상: dict[str, Any]) -> None:
    _기록한다(세상, _실매입())

    [사본] = 세상["reval"].scenarios
    첫회 = 사본["split_plan"][0]
    assert (첫회["qty_kg"], 첫회["amount_krw"]) == (90, 480000)
    assert (첫회["date"], 첫회["expected_arrival_date"]) == ("2026-09-11", "2026-09-13")
    assert (사본["total_qty_kg"], 사본["total_amount_krw"]) == (290, 1480000)
    지급 = 사본["payment_schedule"][0]
    assert (지급["purchase_date"], 지급["payment_date"]) == ("2026-09-11", "2026-09-18")
    assert (지급["qty_kg"], 지급["amount_krw"]) == (90, 480000)
    assert 지급["amount_max_krw"] == 90 * 6000, "상한 금액은 기록 수량 × max_price"
    배분 = 사본["sourcing_plan"]
    assert {line["grade"] for line in 배분} == {"특"}, "한 기록 = 한 등급"
    assert sum(line["qty_kg"] for line in 배분) == 290
    # 🔴 **단가도 기록값에서 다시 난다** (2026-09-16). 선정안 단가를 그대로 두면
    #   `Scenario.validate_quadruple_match` 가 깨져 두 부서가 payload 를 못 읽는다.
    #   1,480,000 ÷ 290 이 정수로 안 떨어져 나머지 줄이 하나 붙는다 — **합은 정확하다.**
    assert sum(line["qty_kg"] * line["grade_unit_price"] for line in 배분) == 1480000


def test_등급만_달라도_재검증한다(세상: dict[str, Any]) -> None:
    _기록한다(세상, _본문(grade="특"))

    assert len(세상["reval"].scenarios) == 1


def test_사본이_원_안을_안_건드린다(세상: dict[str, Any]) -> None:
    _기록한다(세상, _실매입())

    assert 세상["row"]["response_payload"]["scenarios"][0] == _안()


@pytest.mark.parametrize("결과", ["FAILED", "CONDITIONAL", "ERROR"])
def test_재검증을_통과_못_하면_저장도_전이도_없다(세상: dict[str, Any], 결과: str) -> None:
    """🔴 기록이 재무 Cap · 현금흐름을 우회하는 문이 되면 안 된다."""
    세상["reval"] = _재검증(결과, reason="FINANCE_CAP_EXCEEDED")
    문 = _전이()

    with pytest.raises(DecisionRejected) as caught:
        _기록한다(세상, _실매입(), 문)

    assert caught.value.conflict is False, "422 다"
    assert "FINANCE_CAP_EXCEEDED" in str(caught.value), "재무 사유가 안 실렸다"
    assert 세상["store"] == [], "재검증 불통과인데 기록이 앉았다"
    assert 세상["conns"] == [], "재검증 불통과인데 커넥션을 열었다"
    assert 문.calls == [], "재검증 불통과인데 전이를 불렀다"


def test_기록값_재검증은_승인_재검증과_같은_문을_지난다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `revalidate_recorded` 가 받은 사본을 그 실행의 날 · 정책판 · 축으로 넘긴다.

    🔴 매입 실행이므로 **매입 재검증**으로 간다 (2026-09-16). 판매 재검증으로 가면
      재무 `SALES_VALIDATION` 이 skipped 로 답해 늘 `FAILED` 다.
    """
    seen: dict[str, Any] = {}

    def _revalidate(**kw: Any) -> Revalidation:
        seen.update(kw)
        return Revalidation(outcome="PASSED", request_id="REV-X")

    monkeypatch.setattr(svc, "revalidate_procurement_scenario", _revalidate)
    monkeypatch.setattr(
        svc, "revalidate_scenario", lambda **kw: pytest.fail("매입 안을 판매 재검증으로 보냈다")
    )
    monkeypatch.setattr(svc, "list_decisions", lambda request_id: [_결정()])
    monkeypatch.setattr(svc, "_run_for", lambda request_id, history_run_id: _실행행())
    approval = svc.current_approval(업무키)
    assert approval is not None
    사본 = {"label": "기본", "total_qty_kg": 1}

    out = svc.revalidate_recorded(approval, 사본)

    assert out.outcome == "PASSED"
    assert seen["scenario"] is 사본
    assert (seen["as_of"], seen["sim_run_id"], seen["policy_version"]) == (
        기준일,
        실행축,
        "v1.3",
    )
    assert seen["decision_seq"] == 1
    assert seen["proposal"] == _응답()["judgment"], "제안 최상위를 원 실행 judgment 로 싣는다"


# ══════════════════════════════════════════════════════════════════════
#  ⑦ 매입일 경계 (§4-6 ②)
# ══════════════════════════════════════════════════════════════════════


def _경계한다(세상: dict[str, Any], body: dict[str, Any]) -> None:
    """**경계 함수만** 부른다 (`_check_purchase_dates`).

    ★ 선검사(`_check_recordable_values`)가 앞에 서면서 «1회차 매입일 != 승인한 날» 은
      전체 경로로 더 갈 수 없게 됐다 (2026-09-16). 지급기일 · 동일일 예외 규칙 자체는
      그대로 살아 있으므로 **경계 함수 단위로** 잰다 — 규칙이 지워진 것이 아니다.
    """
    approval = svc.current_approval(업무키)
    assert approval is not None
    one = PurchaseRecordIn(**body)
    legs = tuple(
        RecordedLeg(
            seq=leg.seq,
            qty_kg=leg.qty_kg,
            amount_krw=leg.amount_krw,
            purchase_date=leg.purchase_date,
            arrival_date=leg.arrival_date,
        )
        for leg in one.legs
    )
    recorded = svc.commitment_with_record(approval, legs, one.grade)
    pr._check_purchase_dates(approval, recorded, sim_run_id=실행축)


def test_2회차_매입일이_승인_기준일보다_앞서면_경계가_거부한다(세상: dict[str, Any]) -> None:
    """★ 선검사가 묶는 것은 1회차뿐이다 — 나머지 회차는 **기존 경계**가 그대로 잰다."""
    body = _본문()
    body["legs"][1].update(purchase_date="2026-09-10", arrival_date="2026-09-11")

    with pytest.raises(DecisionRejected, match=pr.BEFORE_APPROVAL_MESSAGE) as caught:
        _기록한다(세상, body)
    assert caught.value.conflict is False
    assert "2회차 매입일 2026-09-10" in str(caught.value)
    assert 세상["store"] == []


@pytest.mark.parametrize("마감일", [date(2026, 9, 10), 기준일, date(2026, 9, 17)])
def test_D_를_마감한_뒤에도_지급기일이_마감일_뒤면_받는다(
    세상: dict[str, Any], 마감일: date
) -> None:
    """★ **걷기가 D 를 마감한 뒤 사람이 D 매입을 기록하는 것이 정상 순서다** (2026-09-16).

    1회차 매입일 9/11 = 승인 기준일 · N5=7 → 지급기일 9/18. 마감일이 9/11(D) 이어도,
    9/17 이어도 지급기일이 그 뒤라 받는다.
    """
    세상["closed"] = 마감일

    out, _ = _기록한다(세상, _본문())

    assert out.status == "APPLIED"


@pytest.mark.parametrize("마감일", [date(2026, 9, 19), date(2026, 9, 20)])
def test_지급기일이_마지막_재무_일마감일보다_앞이면_거부한다(
    세상: dict[str, Any], 마감일: date
) -> None:
    """🔴 이미 지난 지급기일의 채무가 새로 생기면 그날 지급에 한 번도 안 잡힌다."""
    세상["closed"] = 마감일

    with pytest.raises(DecisionRejected, match=pr.CLOSED_DUE_DATE_MESSAGE) as caught:
        _기록한다(세상, _본문())
    assert caught.value.conflict is False, "본문이 틀린 것이다 (422)"
    assert "1회차 지급기일 2026-09-18" in str(caught.value)
    assert 세상["store"] == [] and 세상["conns"] == []


def test_지급기일은_회차마다_잰다(세상: dict[str, Any]) -> None:
    """★ 1회차는 마감일 뒤인데 2회차 지급기일이 마감일보다 앞이면 거부한다.

    ⚠️ **경계 함수 단위 검사다** — 1회차 매입일을 승인일 뒤로 밀어야 만들 수 있는 모양이라
      선검사가 앞에서 막는다 (2026-09-16).
    """
    세상["closed"] = date(2026, 9, 20)
    body = _본문()
    body["legs"][0].update(purchase_date="2026-09-15", arrival_date="2026-09-16")  # 9/22
    body["legs"][1].update(purchase_date="2026-09-12", arrival_date="2026-09-15")  # 9/19

    with pytest.raises(DecisionRejected, match=pr.CLOSED_DUE_DATE_MESSAGE) as caught:
        _경계한다(세상, body)
    assert "2회차 지급기일 2026-09-19" in str(caught.value)
    assert 세상["store"] == []


def test_당일_지급이면_D_마감_뒤_D_매입도_받는다(세상: dict[str, Any]) -> None:
    """★ **동일일 예외.** N5=0 · 승인 기준일 = 매입일 = 마감일 = 9/11 → 받는다 (실측 01-05 모양).

    재무 마감(`_recognize_due_payables`)이 기일이 지난 미반영 채무를 다음 마감(D+1)에서
    한 번 반영한다 (재무 합의 9/16).
    """
    세상["row"]["response_payload"]["constraints"]["finance"]["purchase_payment_days"] = 0
    세상["closed"] = 기준일

    out, _ = _기록한다(세상, _본문())

    assert out.status == "APPLIED"
    assert len(세상["store"]) == 2


def test_당일_지급인데_지급기일이_마감일_하루_앞이면_거부한다(세상: dict[str, Any]) -> None:
    """🔴 N5=0 · 매입일 9/11 · 마감일 9/12 → 지급기일이 마감일 - 1 이라 거부한다."""
    세상["row"]["response_payload"]["constraints"]["finance"]["purchase_payment_days"] = 0
    세상["closed"] = date(2026, 9, 12)

    with pytest.raises(DecisionRejected, match=pr.CLOSED_DUE_DATE_MESSAGE) as caught:
        _기록한다(세상, _본문())
    assert "1회차 지급기일 2026-09-11" in str(caught.value)
    assert 세상["store"] == [] and 세상["conns"] == []


def test_지급기일이_마감일과_같아도_과거_승인이면_거부한다(세상: dict[str, Any]) -> None:
    """🔴 N5=7 · 승인 기준일 = 매입일 = 9/11 · 마감일 9/18 = 지급기일 → 동일일 예외가 아니다.

    승인 기준일이 마감일보다 앞이다 (과거 승인).
    """
    세상["closed"] = date(2026, 9, 18)

    with pytest.raises(DecisionRejected, match=pr.SAME_DAY_DUE_DATE_MESSAGE) as caught:
        _기록한다(세상, _본문())
    assert caught.value.conflict is False, "본문이 틀린 것이다 (422)"
    assert "1회차 승인 기준일 2026-09-11 · 매입일 2026-09-11" in str(caught.value)
    assert 세상["store"] == [] and 세상["conns"] == []


def test_지급기일이_마감일과_같아도_매입일이_승인일_뒤면_거부한다(세상: dict[str, Any]) -> None:
    """🔴 N5=0 · 승인 기준일 9/11 · 매입일 = 지급기일 = 마감일 9/12 → 동일일 예외가 아니다.

    ⚠️ **경계 함수 단위 검사다** — 1회차 매입일이 승인일 뒤여야 서는 모양이라 선검사가
      앞에서 막는다 (2026-09-16).
    """
    세상["row"]["response_payload"]["constraints"]["finance"]["purchase_payment_days"] = 0
    세상["closed"] = date(2026, 9, 12)
    body = _본문()
    body["legs"][0].update(purchase_date="2026-09-12", arrival_date="2026-09-13")

    with pytest.raises(DecisionRejected, match=pr.SAME_DAY_DUE_DATE_MESSAGE) as caught:
        _경계한다(세상, body)
    assert "매입일 2026-09-12 · 지급기일 2026-09-12" in str(caught.value)
    assert 세상["store"] == [] and 세상["conns"] == []


def test_동일일_예외는_회차마다_따진다(세상: dict[str, Any]) -> None:
    """★ N5=0 · 승인 기준일 9/11 · 마감일 9/12. 1회차(9/14)는 마감일 뒤라 통과하고,
    2회차(9/12)는 지급기일이 마감일과 같은데 승인일이 아니라 거부한다.

    ⚠️ **경계 함수 단위 검사다** — 1회차 매입일을 승인일 뒤로 밀어야 만들 수 있는 모양이라
      선검사가 앞에서 막는다 (2026-09-16).
    """
    세상["row"]["response_payload"]["constraints"]["finance"]["purchase_payment_days"] = 0
    세상["closed"] = date(2026, 9, 12)
    body = _본문()
    body["legs"][0].update(purchase_date="2026-09-14", arrival_date="2026-09-15")
    body["legs"][1].update(purchase_date="2026-09-12", arrival_date="2026-09-13")

    with pytest.raises(DecisionRejected, match=pr.SAME_DAY_DUE_DATE_MESSAGE) as caught:
        _경계한다(세상, body)
    assert "2회차 승인 기준일 2026-09-11 · 매입일 2026-09-12" in str(caught.value)
    assert 세상["store"] == []


def test_마감이_있는데_지급기일을_모르면_거부한다(세상: dict[str, Any]) -> None:
    """🔴 못 잰 것을 통과로 두지 않는다."""
    del 세상["row"]["response_payload"]["constraints"]["finance"]
    세상["closed"] = date(2026, 9, 10)

    with pytest.raises(DecisionRejected, match="지급기일을 계산할 수 없어"):
        _기록한다(세상, _본문())
    assert 세상["store"] == []


# ══════════════════════════════════════════════════════════════════════
#  ⑩ 입력 선검사 — 재검증 앞에서 사람 말로 막는다 (2026-09-16)
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 **왜.** 기록값 재검증은 안 사본을 매입안 계약으로 다시 읽는다. 계약이
#    `split_plan[0].date == meta.as_of` 와 정수 수량 · 정수 등급 단가를 요구하는데,
#    사본의 as_of 는 **승인 실행의 as_of** 다. 그래서 사람이 1회차 매입일을 다른 날로
#    적거나 소수점을 적으면 계약에서 떨어지고, 화면에는 「재검증 통과 못 함」만 남는다.
#    무엇을 고쳐야 하는지 알 수 없는 문장이다.


def test_1회차_매입일이_승인한_날과_다르면_새_문구로_거부한다(세상: dict[str, Any]) -> None:
    body = _본문()
    body["legs"][0].update(purchase_date="2026-09-12", arrival_date="2026-09-13")
    문 = _전이()

    with pytest.raises(DecisionRejected) as caught:
        _기록한다(세상, body, 문)

    말 = str(caught.value)
    assert caught.value.conflict is False, "본문이 틀린 것이다 (422)"
    assert 말 == pr.PURCHASE_DATE_MESSAGE.format(as_of=기준일, seq=1)
    assert 말 == "매입일은 승인한 날(2026-09-11)과 같아야 합니다 — 1회차"
    # 🔴 **재검증 문구가 아니다.** 알기 어려운 말이 사람에게 가는 것이 이 판의 이유다.
    assert "재검증" not in 말 and "다시 검증" not in 말
    assert 세상["reval"].scenarios == [], "선검증 전에 재검증을 태웠다"
    assert 세상["store"] == [] and 세상["conns"] == [] and 문.calls == []


def test_2회차_매입일은_승인한_날과_달라도_받는다(세상: dict[str, Any]) -> None:
    """★ 계약이 묶는 것은 `split_plan[0]` 하나다 — 분할 선정안을 그대로 못 적으면 안 된다.

    선정안 2회차 매입일은 9/14 로 승인일(9/11)과 다르다.
    """
    out, _ = _기록한다(세상, _본문())

    assert out.status == "APPLIED"
    assert 세상["store"][1]["purchase_date"] == date(2026, 9, 14)


@pytest.mark.parametrize("회차", [0, 1])
def test_수량에_소수점이_있으면_거부한다(세상: dict[str, Any], 회차: int) -> None:
    body = _본문()
    body["legs"][회차]["qty_kg"] = 100.5
    문 = _전이()

    with pytest.raises(DecisionRejected) as caught:
        _기록한다(세상, body, 문)

    말 = str(caught.value)
    assert caught.value.conflict is False
    assert 말 == pr.WHOLE_QTY_MESSAGE.format(seq=회차 + 1)
    assert 말 == f"수량은 1kg 단위로 적어 주세요 — {회차 + 1}회차"
    assert "재검증" not in 말
    assert 세상["store"] == [] and 세상["conns"] == [] and 문.calls == []


@pytest.mark.parametrize("회차", [0, 1])
def test_금액에_소수점이_있으면_거부한다(세상: dict[str, Any], 회차: int) -> None:
    body = _본문()
    body["legs"][회차]["amount_krw"] = 500000.5
    문 = _전이()

    with pytest.raises(DecisionRejected) as caught:
        _기록한다(세상, body, 문)

    말 = str(caught.value)
    assert caught.value.conflict is False
    assert 말 == pr.WHOLE_AMOUNT_MESSAGE.format(seq=회차 + 1)
    assert 말 == f"금액은 원 단위 정수로 적어 주세요 — {회차 + 1}회차"
    assert "재검증" not in 말
    assert 세상["store"] == [] and 세상["conns"] == [] and 문.calls == []


def test_정수에_같은_날이면_선검사를_지나_기존_흐름대로_선다(세상: dict[str, Any]) -> None:
    """★ 선검사는 **막는 것만** 한다 — 지나가는 기록은 지금 그대로다."""
    out, 전이 = _기록한다(세상, _실매입())

    assert out.status == "APPLIED"
    assert len(전이.calls) == 1
    assert len(세상["store"]) == 2
    assert len(세상["reval"].scenarios) == 1, "선정안과 다른데 재검증을 건너뛰었다"


# ══════════════════════════════════════════════════════════════════════
#  ⑨ 기록값 재검증이 실제 판정으로 갈린다
# ══════════════════════════════════════════════════════════════════════


class _조언자:
    """재무 · 물류 어댑터 대역. **무슨 mode 로 물었는지 남기고 정한 판정을 낸다.**"""

    def __init__(self, business_status: str = "ok") -> None:
        self.business_status = business_status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: Any) -> Any:
        from app.master.envelope import AgentReply, ExecutionMetadata

        self.calls.append((request.mode, dict(request.payload)))
        runtime = "RUNTIME_NOT_READY" if self.business_status == "skipped" else "READY"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status=runtime,
            business_status=self.business_status,
            reasoning="대역",
            missing_data=("대역",) if runtime == "RUNTIME_NOT_READY" else (),
        )
        return reply, ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("tool_a",),
            tool_order=(1,),
        )


@pytest.fixture
def 조언자들(세상: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, _조언자]:
    """기록 재검증을 **대역 없이** 태운다. 부서만 대역이다."""
    from app.master import wiring

    wiring.reset()
    등록 = {"finance": _조언자(), "inventory": _조언자()}
    for 이름, 포트 in 등록.items():
        wiring.register(이름, 포트)
    monkeypatch.setattr(pr, "revalidate_recorded", svc.revalidate_recorded)
    return 등록


def test_기록값_재검증은_재무_물류_SCENARIO_VALIDATION_을_부른다(
    세상: dict[str, Any], 조언자들: dict[str, _조언자]
) -> None:
    out, _ = _기록한다(세상, _실매입())

    assert out.status == "APPLIED"
    for 이름, 부 in 조언자들.items():
        assert [mode for mode, _ in 부.calls] == ["SCENARIO_VALIDATION"], 이름
        [(_, payload)] = 부.calls
        assert payload["meta"] == {"item": "배추"}, "제안 최상위(judgment)가 안 실렸다"
        [사본] = payload["scenarios"]
        assert 사본["total_qty_kg"] == 290, "기록값 사본이 아니다"


@pytest.mark.parametrize(("누가", "판정"), [("finance", "reject"), ("inventory", "skipped")])
def test_기록값_재검증이_막히면_저장도_전이도_없다(
    세상: dict[str, Any], 조언자들: dict[str, _조언자], 누가: str, 판정: str
) -> None:
    """★ skipped 도 통과가 아니다 — 검증을 안 했으면 통과가 아니다."""
    조언자들[누가].business_status = 판정
    문 = _전이()

    with pytest.raises(DecisionRejected, match="FAILED") as caught:
        _기록한다(세상, _실매입(), 문)

    assert 누가 in str(caught.value), "막은 조언자가 사유에 없다"
    assert 세상["store"] == [] and 문.calls == []


# ══════════════════════════════════════════════════════════════════════
#  ⑧ 실행 축
# ══════════════════════════════════════════════════════════════════════


def test_다른_실행_축의_같은_업무_키_기록은_별개다(세상: dict[str, Any]) -> None:
    """🔴 PK 에 `sim_run_id` 가 있다 — 남의 실행 기록이 이 승인을 막거나 덮으면 안 된다."""
    세상["store"].append(
        {
            "sim_run_id": "SIM-B",
            "request_id": 업무키,
            "decision_seq": 1,
            "leg_seq": 1,
            "quantity_kg": 1,
            "amount_krw": 1,
            "purchase_date": date(2026, 9, 11),
            "arrival_date": date(2026, 9, 12),
            "grade": "하",
            "recorded_by": "누군가",
            "recorded_at": datetime(2026, 9, 11, tzinfo=UTC),
        }
    )

    out, 전이 = _기록한다(세상, _본문())

    assert out.status == "APPLIED", "다른 실행의 기록이 중복으로 읽혔다"
    assert 전이.calls[0][0].grades == ("상",), "다른 실행의 기록이 약정에 섞였다"
    assert {r["sim_run_id"] for r in 세상["store"]} == {"SIM-A", "SIM-B"}


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 재시도 · 재조립
# ══════════════════════════════════════════════════════════════════════


def _승인행(request_id: str, *, decided_by: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "decision_seq": 1,
        "as_of": date(2026, 9, 11),
        "sim_run_id": 실행축,
        "decided_by": decided_by,
    }


def test_재시도가_기록_없는_사람_승인을_건너뛴다() -> None:
    """⑤ 🔴 **계획값으로 자동 적용 금지.** 사람이 산 값을 적기 전에 원장에 앉지 않는다."""
    found = pending_approvals(
        decisions=[
            _승인행("REQ-사람-기록없음", decided_by=사람),
            _승인행("REQ-사람-기록있음", decided_by=사람),
            _승인행("REQ-자동", decided_by=AUTO_BACKFILL),
        ],
        purchase_ids=[],
        before=date(2026, 9, 12),
        recorded=[("REQ-사람-기록있음", 1)],
    )

    assert [one.request_id for one in found] == ["REQ-사람-기록있음", "REQ-자동"]


def test_재조립이_기록값으로_덮는다(세상: dict[str, Any]) -> None:
    """★ 재시도 · 조회가 같은 값을 본다 — 기록이 있으면 `current_approved_commitment` 가 기록값."""
    문 = _전이(TransitionOut(status="NOT_APPLIED", reason="도착분 없음"), opens=False)
    _기록한다(세상, _실매입(), 문)

    commitment = svc.current_approved_commitment(업무키)
    out = svc.current_commitment(업무키)

    assert commitment is not None and commitment.total_qty_kg == 290.0
    assert commitment.grades == ("특",)
    assert out is not None and out.total_qty_kg == 290.0


def test_재시도가_기록_있는_사람_승인을_기록값으로_세운다(세상: dict[str, Any]) -> None:
    문 = _전이(TransitionOut(status="NOT_APPLIED", reason="도착분 없음"), opens=False)
    _기록한다(세상, _실매입(), 문)
    재시도문 = _전이()

    out = retry_pending_transitions(
        date(2026, 9, 12),
        sim_run_id=실행축,
        decisions_of=lambda **kw: [_승인행(업무키, decided_by=사람)],
        purchase_ids_of=lambda **kw: [],
        recorded_of=lambda **kw: [(업무키, 1)],
        commitment_of=svc.current_approved_commitment,
        apply_fn=재시도문,
    )

    assert out.status == "RAN"
    [(commitment, _)] = 재시도문.calls
    assert commitment.arrival_schedule[0].qty_kg == 90.0, "재시도가 계획값으로 세웠다"
    assert commitment.grades == ("특",)


def test_재시도가_기록_없는_사람_승인에는_전이를_안_부른다() -> None:
    문 = _전이()

    out = retry_pending_transitions(
        date(2026, 9, 12),
        sim_run_id=실행축,
        decisions_of=lambda **kw: [_승인행(업무키, decided_by=사람)],
        purchase_ids_of=lambda **kw: [],
        recorded_of=lambda **kw: [],
        commitment_of=lambda request_id: pytest.fail("기록 없는 사람 승인을 재조립했다"),
        apply_fn=문,
    )

    assert out.status == "NOTHING_DUE"
    assert 문.calls == []


# ══════════════════════════════════════════════════════════════════════
#  조회 (화면용)
# ══════════════════════════════════════════════════════════════════════


def test_조회가_선정안_기본값과_대기_상태를_낸다(세상: dict[str, Any]) -> None:
    out = pr.get_purchase_record(업무키)

    assert out.status == "AWAITING_PURCHASE_RECORD"
    assert out.plan.grade == "상"
    assert [(leg.seq, leg.qty_kg, leg.amount_krw) for leg in out.plan.legs] == [
        (1, 100.0, 500000.0),
        (2, 200.0, 1000000.0),
    ]
    assert out.record is None


def test_조회가_기록과_반영_상태를_낸다(
    세상: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _기록한다(세상, _실매입())
    monkeypatch.setattr(pr, "ledger_purchase_ids", lambda *, sim_run_id: [])
    assert pr.get_purchase_record(업무키).status == "NOT_APPLIED"

    monkeypatch.setattr(pr, "ledger_purchase_ids", lambda *, sim_run_id: [f"PUR-{업무키}-D1-S1"])
    out = pr.get_purchase_record(업무키)

    assert out.status == "APPLIED"
    assert out.record is not None and out.record.grade == "특"
    assert out.record.legs[0].qty_kg == 90.0


def test_자동_승인_조회는_기록_대상이_아니다(세상: dict[str, Any]) -> None:
    세상["decisions"] = [_결정(decided_by=AUTO_BACKFILL)]

    assert pr.get_purchase_record(업무키).status == "NOT_REQUIRED"


# ══════════════════════════════════════════════════════════════════════
#  API — 상태 코드
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def 손님(monkeypatch: pytest.MonkeyPatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.master import router as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app), router_module


@pytest.mark.parametrize(
    ("예외", "코드"),
    [
        (LookupError("승인 없음"), 404),
        (DecisionRejected("이미 기록", conflict=True), 409),
        (DecisionRejected(pr.CLOSED_DUE_DATE_MESSAGE), 422),
    ],
)
def test_기록_API_가_거부를_상태_코드로_접는다(
    손님: Any, monkeypatch: pytest.MonkeyPatch, 예외: Exception, 코드: int
) -> None:
    client, router_module = 손님

    def _boom(request_id: str, body: PurchaseRecordIn) -> TransitionOut:
        raise 예외

    monkeypatch.setattr(router_module, "record_purchase", _boom)
    res = client.post(f"/master/runs/{업무키}/purchase-record", json=_본문())

    assert res.status_code == 코드, res.text


def test_기록_API_가_전이_결과를_201_로_낸다(손님: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, router_module = 손님
    monkeypatch.setattr(
        router_module,
        "record_purchase",
        lambda request_id, body: TransitionOut(status="APPLIED", parts=["finance", "logistics"]),
    )

    res = client.post(f"/master/runs/{업무키}/purchase-record", json=_본문())

    assert res.status_code == 201
    assert res.json()["status"] == "APPLIED"


def test_기록_API_가_본문_오류를_422_로_막는다(손님: Any) -> None:
    client, _ = 손님
    body = _본문()
    body["legs"][0]["qty_kg"] = 0

    res = client.post(f"/master/runs/{업무키}/purchase-record", json=body)

    assert res.status_code == 422
