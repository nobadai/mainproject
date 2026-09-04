"""승인 → 상태전이의 **트랜잭션 경계** (C 형태 ⑦).

재무·물류의 `persist` 구현은 아직 없다. 그래서 여기서 재는 것은 *"어떤 값이 어느
칸에 들어갔나"* 가 아니라 **마스터가 소유한 것 하나** — 언제 커넥션을 열고, 어떤
순서로 부르고, 언제 한 번 커밋하고, 터지면 무엇을 되돌리는가다.

🔴 **구현이 없다고 검사를 미루면 그 자리가 영영 안 잠긴다.** 재무·물류가 들어온 뒤에
   "커밋이 두 번 나간다"를 발견하면, 그때는 이미 장부에 반쪽짜리 행이 남아 있다.
   가짜 커넥션은 그 순서를 **오늘** 잰다.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.master import decision_service as svc
from app.master import transition
from app.master.commitment import ApprovedCommitment, ArrivalLeg
from app.master.decision import DecisionIn, DecisionOut

AS_OF = date(2025, 12, 31)


@pytest.fixture(autouse=True)
def 전이_등록소를_비운다() -> Iterator[None]:
    """등록소는 프로세스 전역이다 — **앞뒤로 비운다.**

    ★ 끝나고만 비우면 앞 테스트가 남긴 등록이 이 파일로 흘러든다.
    """
    transition.reset()
    try:
        yield
    finally:
        transition.reset()


def _commitment() -> ApprovedCommitment:
    return ApprovedCommitment(
        approval_id="H1-REQ-1-1",
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario_label="보수",
        total_qty_kg=44.0,
        total_amount_krw=228800.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=44.0,
                arrival_date=date(2026, 1, 2),
                purchase_date=AS_OF,
                seq=1,
            ),
        ),
        inbound_lead_days=2.0,
    )


class 가짜커넥션:
    """세는 것만 한다 — commit · rollback · close 가 **몇 번** 불렸나."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class 가짜전이:
    """재무·물류 자리에 들어가는 대역. **부름 순서와 받은 커넥션을 기록한다.**"""

    def __init__(
        self,
        name: str,
        log: list[tuple[str, Any]],
        *,
        build_raises: Exception | None = None,
        persist_raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.log = log
        self.build_raises = build_raises
        self.persist_raises = persist_raises

    def build(
        self,
        commitment: ApprovedCommitment,
        *,
        target_state_date: date,
        purchase_ids: Mapping[int, str] | None = None,
    ) -> Any:
        # ★ 재무만 `purchase_ids` 를 받는다 — 물류 자리에 들어가는 대역은 안 받는다.
        #   기본값을 둬 두 파트가 같은 대역을 쓴다.
        self.log.append((f"{self.name}.build", target_state_date))
        if self.build_raises is not None:
            raise self.build_raises
        return f"{self.name}-row"

    def persist(self, conn: Any, rows: Any) -> None:
        self.log.append((f"{self.name}.persist", conn))
        if self.persist_raises is not None:
            raise self.persist_raises


def _connect_spy(conn: 가짜커넥션, calls: list[int]):
    def _connect() -> 가짜커넥션:
        calls.append(1)
        return conn

    return _connect


# ── a·b. 미등록은 오류가 아니라 상태다 ──────────────────────────────────


def test_둘_다_미등록이면_커넥션을_열지_않는다() -> None:
    """🔴 열고 나서 아무 일도 안 하면 **빈 트랜잭션**이 승인마다 열렸다 닫힌다."""
    calls: list[int] = []
    out = transition.apply_approval(
        _commitment(), connect=_connect_spy(가짜커넥션(), calls)
    )

    assert out.status == "NOT_APPLIED"
    assert "finance" in out.reason and "logistics" in out.reason
    assert out.missing == ["finance", "logistics"]
    assert calls == [], "미등록인데 커넥션을 열었다"


def test_한쪽만_등록되면_반쪽으로_반영하지_않는다() -> None:
    """★ 재무만 있는 날 현금만 나가면 **입고 예정 없는 장부**가 된다."""
    log: list[tuple[str, Any]] = []
    transition.register_transition("finance", 가짜전이("finance", log))
    calls: list[int] = []

    out = transition.apply_approval(
        _commitment(), connect=_connect_spy(가짜커넥션(), calls)
    )

    assert out.status == "NOT_APPLIED"
    assert out.missing == ["logistics"]
    assert calls == []
    assert log == [], "미등록인데 등록된 쪽을 불렀다"


# ── c. 한 커넥션 · 한 커밋 ──────────────────────────────────────────────


def test_둘_다_등록되면_한_커넥션으로_한_번_커밋한다() -> None:
    log: list[tuple[str, Any]] = []
    transition.register_transition("finance", 가짜전이("finance", log))
    transition.register_transition("logistics", 가짜전이("logistics", log))
    conn = 가짜커넥션()
    calls: list[int] = []

    out = transition.apply_approval(_commitment(), connect=_connect_spy(conn, calls))

    assert out.status == "APPLIED"
    assert out.parts == ["finance", "logistics"]
    assert calls == [1], "커넥션은 하나만 연다"
    assert conn.commits == 1, "커밋은 두 파트가 끝난 뒤 한 번이다"
    assert conn.rollbacks == 0
    assert conn.closed == 1

    persisted = [(name, got) for name, got in log if name.endswith(".persist")]
    assert [name for name, _ in persisted] == ["finance.persist", "logistics.persist"]
    assert [got for _, got in persisted] == [conn, conn], "두 파트가 같은 커넥션을 써야 한다"


def test_재무_build_는_상태가_설_날을_받는다() -> None:
    """★ 재는 것은 그대로다 — 재무 `build` 가 마스터가 정한 날짜를 받는다.

    ⚠️ 받는 값이 `as_of` 에서 **다음 날**로 바뀌었다 (`target_state_date`).
      그 날짜 규칙 자체는 `test_transition_protocol.py` 가 잰다.
    """
    log: list[tuple[str, Any]] = []
    transition.register_transition("finance", 가짜전이("finance", log))
    transition.register_transition("logistics", 가짜전이("logistics", log))

    transition.apply_approval(_commitment(), connect=_connect_spy(가짜커넥션(), []))

    assert ("finance.build", AS_OF + timedelta(days=1)) in log


# ── d. 터지면 되돌린다 ──────────────────────────────────────────────────


def test_물류_적재가_터지면_전부_되돌린다() -> None:
    """🔴 재무만 커밋되면 **현금은 나갔는데 입고 예정이 없는** 장부가 된다."""
    log: list[tuple[str, Any]] = []
    transition.register_transition("finance", 가짜전이("finance", log))
    transition.register_transition(
        "logistics",
        가짜전이("logistics", log, persist_raises=RuntimeError("로트 표가 없다")),
    )
    conn = 가짜커넥션()

    out = transition.apply_approval(_commitment(), connect=_connect_spy(conn, []))

    assert out.status == "FAILED"
    assert "로트 표가 없다" in out.reason, "사유를 남기지 않으면 무엇이 터졌는지 모른다"
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn.closed == 1


def test_적재_실패가_예외로_올라가지_않는다() -> None:
    """★ 결정은 **이미 적재됐다.** 전이 실패가 500 이 되면 승인이 실패로 보인다."""
    log: list[tuple[str, Any]] = []
    transition.register_transition("finance", 가짜전이("finance", log))
    transition.register_transition(
        "logistics", 가짜전이("logistics", log, persist_raises=RuntimeError("끊겼다"))
    )

    out = transition.apply_approval(_commitment(), connect=_connect_spy(가짜커넥션(), []))

    assert out.status == "FAILED"


# ── e. 계산 실패는 DB 를 만나기 전에 끝난다 ─────────────────────────────


def test_build_가_터지면_커넥션을_열지_않는다() -> None:
    log: list[tuple[str, Any]] = []
    transition.register_transition(
        "finance", 가짜전이("finance", log, build_raises=ValueError("현금 상태가 없다"))
    )
    transition.register_transition("logistics", 가짜전이("logistics", log))
    calls: list[int] = []

    out = transition.apply_approval(
        _commitment(), connect=_connect_spy(가짜커넥션(), calls)
    )

    assert out.status == "FAILED"
    assert "현금 상태가 없다" in out.reason
    assert calls == [], "계산 실패는 커넥션을 열기 전에 끝나야 한다"


# ── f. 분담이 문자로 잠긴다 ─────────────────────────────────────────────


def test_전이_모듈에_SQL_이_없다() -> None:
    """🔴 여기에 `INSERT` 가 한 줄이라도 들어오면 마스터가 남의 칸 이름을 알게 된다.

    ★ 원문을 읽어 검사한다. import 로는 안 잡힌다 — SQL 문자열은 실행되기 전까지
      아무 흔적이 없다.
    """
    source = Path(transition.__file__).read_text(encoding="utf-8")

    for 금지 in ("INSERT INTO", "UPDATE ", "DELETE "):
        assert 금지 not in source, f"마스터 전이 경계에 SQL 이 있다: {금지}"


# ── g. 승인 응답에 전이 결과가 실린다 ───────────────────────────────────


@pytest.fixture
def wired(monkeypatch):
    """DB 를 걷어내고 결정 경로만 남긴다 (`test_decision_commitment.py` 와 같은 방식)."""
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


def _response(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "end_code": "E1_APPROVED",
        "as_of": "2025-12-31",
        "scenarios": [
            {
                "label": "보수",
                "total_qty_kg": 44.0,
                "total_amount_krw": 228800.0,
                "split_plan": [{"seq": 1, "date": "2025-12-31", "qty_kg": 44.0}],
            }
        ],
        "judgment": {"meta": {"item": "배추"}},
        "constraints": {"inventory": {"inbound_lead_days": 2.0}},
    }
    base.update(over)
    return base


def test_승인_응답에_전이_결과가_실린다(wired) -> None:
    """★ 오늘은 미등록이라 `NOT_APPLIED` 다 — 그것도 **말해 주어야 하는 사실**이다."""
    out = wired(_response())

    assert out.transition is not None, "약정이 섰는데 전이가 침묵하면 반영 여부를 알 수 없다"
    assert out.transition.status == "NOT_APPLIED"
    assert out.transition.missing == ["finance", "logistics"]


def test_승인이_아니면_전이도_없다(wired) -> None:
    out = wired(_response(), decision="REJECT_ALL", scenario_label=None)

    assert out.transition is None, "거절에 전이가 붙으면 무엇을 반영한 것인지 모른다"


def test_약정을_못_만들면_전이를_시도하지_않는다(wired) -> None:
    """★ `buildable=False` 와 `NOT_APPLIED` 는 다른 사실이다 — 섞지 않는다."""
    out = wired(_response(judgment={"meta": {}}))  # 품목 없음

    assert out.commitment is not None and out.commitment.buildable is False
    assert out.transition is None, "반영할 약정이 없는데 전이를 만들면 안 된다"


def test_등록되어_있으면_승인_경로가_커밋까지_간다(wired, monkeypatch) -> None:
    """★ 재무·물류가 들어온 날 이 경로가 그대로 도는지 **미리** 잰다."""
    log: list[tuple[str, Any]] = []
    conn = 가짜커넥션()
    transition.register_transition("finance", 가짜전이("finance", log))
    transition.register_transition("logistics", 가짜전이("logistics", log))

    real_apply = transition.apply_approval
    monkeypatch.setattr(
        svc,
        "apply_approval",
        lambda commitment, **_: real_apply(commitment, connect=lambda: conn),
    )

    out = wired(_response())

    assert out.transition.status == "APPLIED"
    assert conn.commits == 1
    assert conn.rollbacks == 0
