"""하루에 물류 점검 두 칸이 **제자리에서** 돈다 (#628 Commit 2).

```text
… → 입고 → **점검 #1(AFTER_INBOUND)** → 채권 → … → 출고 → **점검 #2(AFTER_OUTBOUND)** → 마감
```

🔴 **재는 것은 배선이다.** 무엇이 문제인지는 물류가 정하고
   (`tests/logistics/test_logistics_agent_*`), 여기서는 *"불렀나 · 어느 자리에서 ·
   터져도 하루가 사나"* 만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from app.logistics.agent.schemas import DetectOut
from app.master.inspection import (
    AFTER_INBOUND,
    AFTER_OUTBOUND,
    InspectionOut,
    run_logistics_inspection,
)
from app.master.scheduler import ScheduledAction, run_scheduled_day

SEOUL = ZoneInfo("Asia/Seoul")
AS_OF = date(2026, 1, 7)
SIM = "SIM-INSPECT-TEST"


@dataclass
class _Out:
    status: str
    reason: str = ""
    outcomes: dict[str, int] | None = None


class _Spy:
    def __init__(self, out: Any = None, *, boom: Exception | None = None) -> None:
        self.out = out if out is not None else _Out("NOTHING_DUE")
        self.boom = boom
        self.calls: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, arg: Any = None, **kwargs: Any) -> Any:
        self.calls.append(arg)
        self.kwargs.append(kwargs)
        if self.boom is not None:
            raise self.boom
        return self.out


class _Retry:
    """재시도는 낸 값의 `outcomes` 를 요약이 그대로 세므로 모양을 맞춘다."""

    status = "NOTHING_DUE"
    reason = "미적용 없음"
    outcomes: ClassVar[dict[str, int]] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> _Retry:
        return self


class _Inspect:
    """물류 점검 경계. **어느 자리에서 어떤 phase 로 불렸는지**를 기록한다."""

    def __init__(self, *, boom: Exception | None = None, status: str = "NOTHING_DUE") -> None:
        self.boom = boom
        self.status = status
        self.phases: list[str] = []

    def __call__(self, as_of: date, *, sim_run_id: str, phase: str) -> InspectionOut:
        self.phases.append(phase)
        if self.boom is not None:
            raise self.boom
        return InspectionOut(
            as_of=as_of,
            phase=phase,
            status=self.status,
            reason="검사",
            result=DetectOut(as_of=as_of, phase=phase, status=self.status, opened=("EX-1",)),
        )


def _하루(**kwargs: Any) -> tuple[Any, dict[str, Any], list[str]]:
    """도는 하루 하나. **단계 순서를 함께 기록한다.**"""
    순서: list[str] = []

    def 기록(이름: str, status: str, spy: _Spy | None = None) -> Any:
        본체 = spy if spy is not None else _Spy(_Out(status))

        def _부른다(*args: Any, **kw: Any) -> Any:
            순서.append(이름)
            return 본체(*args, **kw)

        _부른다.spy = 본체  # type: ignore[attr-defined]
        return _부른다

    점검 = kwargs.pop("inspect_fn", None) or _Inspect()

    def 점검기록(as_of: date, **kw: Any) -> InspectionOut:
        순서.append(f"점검:{kw['phase']}")
        return 점검(as_of, **kw)

    defaults: dict[str, Any] = {
        "open_day_fn": _Spy(_Out("OPENED")),
        "retry_fn": _Retry(),
        "receive_fn": 기록("입고", "RECEIVED"),
        "inspect_fn": 점검기록,
        "issue_fn": 기록("채권", "ISSUED"),
        "collect_fn": 기록("수금", "COLLECTED"),
        "procure_fn": _Spy(_Out("RAN")),
        "sales_fn": _Spy(_Out("RAN")),
        "outbound_fn": 기록("출고", "NOTHING_DUE"),
        "close_fn": 기록("마감", "CLOSED"),
        "items": ("배추",),
        "sim_run_id": SIM,
    }
    defaults.update(kwargs)

    action = ScheduledAction(
        as_of=AS_OF,
        now=datetime(2026, 1, 7, 9, 30, tzinfo=SEOUL),
        action="RUN_AND_RECORD",
        reason="검사",
        deadline=datetime(2026, 1, 7, 10, 30, tzinfo=SEOUL),
    )
    assert action.should_run, "전제가 깨졌다 — 이 검사는 도는 날을 재려던 것이다"
    out = run_scheduled_day(action, **defaults)
    return out, {**defaults, "_점검": 점검}, 순서


# ===========================================================================
# A. 자리
# ===========================================================================


def test_입고_바로_뒤와_출고_바로_뒤에_한_번씩_부른다() -> None:
    """★ **입고 뒤여야 그날 매입 판단이 그 사실을 본다.** 출고 뒤여야 그날 안에 닫는다."""
    out, given, 순서 = _하루()

    assert given["_점검"].phases == [AFTER_INBOUND, AFTER_OUTBOUND]
    assert 순서 == [
        "입고",
        f"점검:{AFTER_INBOUND}",
        "채권",
        "수금",
        "출고",
        f"점검:{AFTER_OUTBOUND}",
        "마감",
    ]
    assert out.inspection_inbound_status == "NOTHING_DUE"
    assert out.inspection_outbound_status == "NOTHING_DUE"


def test_두_칸의_결과를_한_칸에_담지_않는다() -> None:
    """⚠️ 입고 뒤는 «무엇이 새로 문제인가» 이고 출고 뒤는 «무엇이 해결됐나» 다."""
    out, _, _ = _하루()

    assert out.inspection_inbound is not None and out.inspection_outbound is not None
    assert out.inspection_inbound.phase == AFTER_INBOUND
    assert out.inspection_outbound.phase == AFTER_OUTBOUND
    assert out.inspection_inbound.counts == {"opened": 1, "updated": 0, "resolved": 0}


def test_안_도는_날에는_아예_안_부른다() -> None:
    """`WAIT` · 휴장에는 하루가 통째로 안 돈다 — 칸도 `NOT_ATTEMPTED` 로 남는다."""
    점검 = _Inspect()
    action = ScheduledAction(
        as_of=AS_OF,
        now=datetime(2026, 1, 7, 9, 30, tzinfo=SEOUL),
        action="WAIT",
        reason="예측 미도착",
        deadline=datetime(2026, 1, 7, 10, 30, tzinfo=SEOUL),
    )

    out = run_scheduled_day(action, inspect_fn=점검, items=("배추",))

    assert 점검.phases == []
    assert out.inspection_inbound_status == "NOT_ATTEMPTED"
    assert out.inspection_outbound_status == "NOT_ATTEMPTED"


def test_스위치가_없다() -> None:
    """🔴 **켜고 끄는 값을 두지 않는다.** 점검은 창고를 안 바꾸고 표에 행만 남기므로
    걷기 재현성을 안 해치고, 스위치를 두면 *"어제는 문제였는데 오늘은 아니다"* 가
    데이터가 아니라 설정으로 갈린다 (`auto_maintain` 과 다른 점)."""
    import inspect as _inspect

    칸 = _inspect.signature(run_scheduled_day).parameters
    assert "inspect_fn" in 칸
    assert not any(이름.startswith("auto_inspect") for 이름 in 칸)


# ===========================================================================
# B. 터져도 하루는 계속 간다
# ===========================================================================


def test_점검이_터져도_마감까지_간다() -> None:
    """🔴 표가 아직 없는 DB 에서도 걷기가 돌아야 한다 — 두 칸이 `FAILED` 를 남기고 끝이다."""
    out, _, 순서 = _하루(inspect_fn=_Inspect(boom=RuntimeError("표가 없다")))

    assert out.inspection_inbound_status == "FAILED"
    assert out.inspection_outbound_status == "FAILED"
    assert out.closing_status == "CLOSED"
    assert out.outbound_status == "NOTHING_DUE"
    assert "마감" in 순서
    assert any("물류 점검" in one and "터졌다" in one for one in out.notes)


def test_점검_결과가_다른_단계_결과를_안_바꾼다() -> None:
    정상, _, _ = _하루()
    터짐, _, _ = _하루(inspect_fn=_Inspect(boom=RuntimeError("터졌다")))

    assert 터짐.inbound_status == 정상.inbound_status
    assert 터짐.receivable_status == 정상.receivable_status
    assert 터짐.collection_status == 정상.collection_status
    assert 터짐.procurement_status == 정상.procurement_status
    assert 터짐.closing_status == 정상.closing_status


# ===========================================================================
# C. 트랜잭션의 주인 — `run_logistics_inspection`
# ===========================================================================


class _FakeConn:
    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


def test_성공하면_한_번_커밋하고_닫는다() -> None:
    """★ **한 점검이 한 커밋이다** — 연 것과 닫은 것이 같은 재탐지의 두 면이다."""
    conn = _FakeConn()
    낸값 = DetectOut(as_of=AS_OF, phase=AFTER_OUTBOUND, status="RAN", resolved=("EX-1",))

    out = run_logistics_inspection(
        AS_OF,
        sim_run_id=SIM,
        phase=AFTER_OUTBOUND,
        connect=lambda: conn,
        detect_fn=lambda *a, **kw: 낸값,
    )

    assert (conn.committed, conn.rolled_back, conn.closed) == (1, 0, 1)
    assert out.status == "RAN"
    assert out.result is 낸값


def test_터지면_롤백하고_FAILED_로_돌아선다() -> None:
    """🔴 절반만 재탐지된 장부를 남기지 않는다."""
    conn = _FakeConn()

    def 터진다(*a: Any, **kw: Any) -> DetectOut:
        raise RuntimeError("undefined table")

    out = run_logistics_inspection(
        AS_OF, sim_run_id=SIM, phase=AFTER_INBOUND, connect=lambda: conn, detect_fn=터진다
    )

    assert (conn.committed, conn.rolled_back, conn.closed) == (0, 1, 1)
    assert out.status == "FAILED"
    assert "undefined table" in out.reason


def test_연결이_안_되면_그날을_세우지_않는다() -> None:
    def 못연다() -> Any:
        raise OSError("DB 없음")

    out = run_logistics_inspection(
        AS_OF, sim_run_id=SIM, phase=AFTER_INBOUND, connect=못연다
    )

    assert out.status == "FAILED"
    assert "연결 실패" in out.reason
    assert out.result is None


def test_못_잰_것을_사유에서_지우지_않는다() -> None:
    """*"확인했고 문제 없음"* 과 *"기준이 없어 못 쟀다"* 가 요약에서 같아 보이면,
    정책 미등재가 안전 신호로 둔갑한다."""
    conn = _FakeConn()
    낸값 = DetectOut(
        as_of=AS_OF,
        phase=AFTER_INBOUND,
        status="NOTHING_DUE",
        reason="확인했고 손댈 것이 없었다",
        uncertainties=("CAPACITY_PRESSURE:CAPACITY_TIGHT_POLICY_UNRESOLVED",),
    )

    out = run_logistics_inspection(
        AS_OF,
        sim_run_id=SIM,
        phase=AFTER_INBOUND,
        connect=lambda: conn,
        detect_fn=lambda *a, **kw: 낸값,
    )

    assert "미확인" in out.reason
    assert "CAPACITY_TIGHT_POLICY_UNRESOLVED" in out.reason


@pytest.mark.parametrize("phase", [AFTER_INBOUND, AFTER_OUTBOUND])
def test_받은_phase_를_그대로_흘려보낸다(phase: str) -> None:
    """🔴 **닫는 자리를 마스터가 정하지 않는다** — 그 규칙의 주인은 물류다."""
    본 = {}

    def 본다(conn: Any, **kw: Any) -> DetectOut:
        본.update(kw)
        return DetectOut(as_of=AS_OF, phase=kw["phase"], status="NOTHING_DUE")

    run_logistics_inspection(
        AS_OF, sim_run_id=SIM, phase=phase, connect=_FakeConn, detect_fn=본다
    )

    assert 본 == {"sim_run_id": SIM, "as_of": AS_OF, "phase": phase}
