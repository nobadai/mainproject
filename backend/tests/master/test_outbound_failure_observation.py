"""**출고가 터진 순간을 값으로 남긴다** — 고아 예약 미설명 6건을 새 걷기에서 잡는 칸 (2026-09-15).

물류 문서 「24. 고아 Reservation 원인 확정」 §5-㉣ · §6 이다.

```text
REH-0914 · 납품일별
06-03  2건 중 2건 실패      07-08  3건 중 3건 실패      B 04-21  1건 실패
← 납품일 기준 재고가 충분한데 터졌다 · 원인 코드 자리 미발견
```

전에는 `SaleItemOutcome` 이 예외 전문(`reason`) · 요구량 · 출고량만 날랐고, 걷기 요약은
`sale_item_id` 목록조차 안 냈다. **터진 순간의 후보가 몇 개 · 몇 kg 이었나**를 모르면
다음 걷기에서도 손으로 되짚어야 한다.

🔴 **여기서 잠그는 것 넷.**

```text
① 할당이 터지면 error_type · reserved_qty_kg · 후보 두 칸 · release_outcome=RELEASED 가 선다
② 후보 읽기가 터져도 FAILED · 사유는 그대로다 — 후보 칸만 None
③ 놓아주기가 터지면 release_outcome=RELEASE_FAILED 다 — 사유 문장에서 뽑지 않는다
④ 걷기 요약에 FAILED 품목마다 한 줄이 칸들을 담는다
```

★ **DB 를 안 탄다.** `test_outbound_flow.py` 와 같은 대역 결이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.master import outbound_flow
from app.master.outbound_flow import DueSaleItem, OutboundOut, SaleItemOutcome

AS_OF = date(2026, 2, 10)
축 = "SIM-CHAIN-REH-0914"

#: 🔴 실측의 그 양. 배추 02-10 확보 3,586kg · 납품일 후보합 2,870kg.
확보 = Decimal(3586)


class _Conn:
    def __init__(self) -> None:
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


@dataclass
class _Reserved:
    reserved_qty_kg: Decimal = 확보


@dataclass
class _Candidate:
    """`FefoCandidate` 의 최소 모양. 이 파일이 보는 칸만 있다."""

    lot_id: str
    available_qty_kg: Decimal


def _row() -> DueSaleItem:
    return DueSaleItem(
        sale_id="SALE-A",
        sale_item_id="SI-SALE-A-1",
        item_id="ITEM-배추",
        sim_run_id=축,
        quantity_kg=확보,
        sale_date=AS_OF,
    )


def _터지는(name: str, conn: _Conn, exc: Exception):
    def _call(*_a: Any, **_k: Any) -> Any:
        conn.events.append(name)
        raise exc

    return _call


def _기록(name: str, conn: _Conn, result: Any = None, calls: list | None = None):
    def _call(*a: Any, **k: Any) -> Any:
        conn.events.append(name)
        if calls is not None:
            calls.append(k)
        return result

    return _call


class OutboundIntegrityError(Exception):
    """물류 예외 이름만 흉내 낸다 — `type(exc).__name__` 이 실리는지 본다."""


def _ship(
    conn: _Conn,
    *,
    candidates_fn: Any,
    release_fn: Any | None = None,
) -> SaleItemOutcome:
    return outbound_flow._ship_one(
        conn,
        _row(),
        as_of=AS_OF,
        decided_at=None,  # type: ignore[arg-type]
        reserve_fn=_기록("reserve", conn, _Reserved()),
        allocate_fn=_터지는(
            "allocate", conn, OutboundIntegrityError("확보해 둔 몫을 Lot 에서 다 못 찾았다")
        ),
        ship_fn=_기록("ship", conn),
        release_fn=release_fn if release_fn is not None else _기록("release", conn),
        candidates_fn=candidates_fn,
    )


# ── ① 할당이 터지면 칸들이 선다 ─────────────────────────────────────────


def test_할당이_터지면_관측_칸이_채워진다() -> None:
    """🔴 **터진 순간의 후보** — 그날 FEFO 후보가 몇 개 · 몇 kg 이었나가 값으로 남는다."""
    conn = _Conn()
    읽은것: list[dict[str, Any]] = []
    후보 = (_Candidate("LOT-1", Decimal(2000)), _Candidate("LOT-2", Decimal(870)))

    결과 = _ship(conn, candidates_fn=_기록("candidates", conn, 후보, 읽은것))

    assert 결과.status == "FAILED"
    assert 결과.error_type == "OutboundIntegrityError"
    assert 결과.reserved_qty_kg == 확보
    assert 결과.candidate_lot_count == 2
    assert 결과.candidate_available_kg == Decimal(2870)
    assert 결과.release_outcome == "RELEASED"
    assert 결과.item_id == "ITEM-배추"
    assert 읽은것 == [{"sim_run_id": 축, "item_id": "ITEM-배추", "as_of": AS_OF}]
    # ★ 후보는 **놓아주기 전**에 읽는다 · 읽은 뒤 롤백으로 트랜잭션을 비운다.
    assert conn.events == [
        "reserve", "commit", "allocate", "rollback",
        "candidates", "rollback", "release", "commit",
    ]


def test_성공한_출고에도_확보량이_실린다() -> None:
    """★ 확보량은 FAILED 만의 칸이 아니다 — RAN 에서도 얼마를 잡고 나갔는지 보인다."""
    conn = _Conn()

    @dataclass
    class _Shipped:
        shipped_qty_kg: Decimal = 확보

    결과 = outbound_flow._ship_one(
        conn,
        _row(),
        as_of=AS_OF,
        decided_at=None,  # type: ignore[arg-type]
        reserve_fn=_기록("reserve", conn, _Reserved()),
        allocate_fn=_기록("allocate", conn),
        ship_fn=_기록("ship", conn, _Shipped()),
        release_fn=_기록("release", conn),
        candidates_fn=_터지는("candidates", conn, AssertionError("성공 경로는 후보를 안 읽는다")),
    )

    assert 결과.status == "RAN"
    assert 결과.reserved_qty_kg == 확보
    assert 결과.error_type is None
    assert 결과.candidate_lot_count is None
    assert 결과.release_outcome is None


# ── ② 후보 읽기가 터져도 결과는 그대로 ────────────────────────────────


def test_후보_읽기가_터져도_FAILED_와_사유는_그대로다() -> None:
    """🔴 관측이 판정을 바꾸면 안 된다 — 칸만 None 이고 사유에 후보 이야기가 섞이지 않는다."""
    기준 = _ship(_Conn(), candidates_fn=_기록("candidates", _Conn(), ()))
    conn = _Conn()

    결과 = _ship(conn, candidates_fn=_터지는("candidates", conn, RuntimeError("읽기 실패")))

    assert 결과.status == "FAILED"
    assert 결과.reason == 기준.reason, "후보 읽기 실패가 사유를 바꿨다"
    assert 결과.candidate_lot_count is None
    assert 결과.candidate_available_kg is None
    assert 결과.error_type == "OutboundIntegrityError"
    assert 결과.release_outcome == "RELEASED", "후보 읽기 실패가 놓아주기를 막았다"
    assert conn.events[-4:] == ["candidates", "rollback", "release", "commit"]


# ── ③ 놓아주기가 터지면 RELEASE_FAILED ─────────────────────────────────


def test_놓아주기가_터지면_RELEASE_FAILED_다() -> None:
    """🔴 값으로 돌려받는다 — 사유 문장을 파싱하면 문장을 고치는 날 칸이 조용히 틀린다."""
    conn = _Conn()

    결과 = _ship(
        conn,
        candidates_fn=_기록("candidates", conn, ()),
        release_fn=_터지는("release", conn, RuntimeError("놓아주기 실패")),
    )

    assert 결과.status == "FAILED"
    assert 결과.release_outcome == "RELEASE_FAILED"
    assert 결과.candidate_lot_count == 0
    assert 결과.candidate_available_kg == Decimal(0)


def test_release_stranded_가_값을_돌려준다() -> None:
    conn = _Conn()
    문장, 값 = outbound_flow._release_stranded(
        conn, reservation_id="RSV-X", as_of=AS_OF, release_fn=_기록("release", conn)
    )
    assert 값 == "RELEASED"
    assert 문장

    문장, 값 = outbound_flow._release_stranded(
        conn,
        reservation_id="RSV-X",
        as_of=AS_OF,
        release_fn=_터지는("release", conn, RuntimeError("x")),
    )
    assert 값 == "RELEASE_FAILED"
    assert "x" in 문장


def test_기존_생성_지점은_새_칸_없이도_선다() -> None:
    """★ 기본값이 None 이라 옛 생성 지점이 안 깨진다 — 모르는 것을 0 으로 채우지 않는다."""
    one = SaleItemOutcome(
        sale_id="S", sale_item_id="SI", reservation_id="RSV-SI", status="SHORT"
    )
    for 칸 in (
        "reserved_qty_kg",
        "candidate_lot_count",
        "candidate_available_kg",
        "error_type",
        "release_outcome",
    ):
        assert getattr(one, 칸) is None


# ── ④ 걷기 요약에 FAILED 한 줄 ──────────────────────────────────────────


def test_걷기_요약에_출고실패_한_줄이_칸들을_담는다() -> None:
    """🔴 **값이 있는데 성적표가 안 읽으면 없는 것과 같다** (`예약어휘` 와 같은 규율)."""
    from app.master.backtest_runner import WalkResult, format_summary
    from app.master.scheduler import DayRunOutcome

    실패 = SaleItemOutcome(
        sale_id="SALE-A",
        sale_item_id="SI-SALE-A-1",
        reservation_id="RSV-SI-SALE-A-1",
        status="FAILED",
        reason="OutboundIntegrityError: 확보해 둔 몫을 Lot 에서 다 못 찾았다",
        required_qty_kg=확보,
        item_id="ITEM-배추",
        reserved_qty_kg=확보,
        candidate_lot_count=2,
        candidate_available_kg=Decimal(2870),
        error_type="OutboundIntegrityError",
        release_outcome="RELEASED",
    )
    성공 = SaleItemOutcome(
        sale_id="SALE-B",
        sale_item_id="SI-SALE-B-1",
        reservation_id="RSV-SI-SALE-B-1",
        status="RAN",
        shipped_qty_kg=Decimal(10),
        required_qty_kg=Decimal(10),
    )
    출고 = OutboundOut(as_of=AS_OF, status="RAN", items=(실패, 성공))
    결과 = WalkResult(
        start=AS_OF,
        end=AS_OF,
        days=(
            DayRunOutcome(
                as_of=AS_OF, action="RUN_NOW", reason="", outbound_status="RAN", outbound=출고
            ),
        ),
    )

    요약 = format_summary(결과)

    줄들 = [줄 for 줄 in 요약.splitlines() if 줄.lstrip().startswith("출고실패")]
    assert len(줄들) == 1, f"FAILED 한 건에 한 줄이어야 한다: {줄들}"
    줄 = 줄들[0]
    for 조각 in (
        AS_OF.isoformat(),
        "SALE-A",
        "SI-SALE-A-1",
        "RSV-SI-SALE-A-1",
        "ITEM-배추",
        "required 3586",
        "reserved 3586",
        "후보 2",
        "후보합 2870",
        "OutboundIntegrityError",
        "확보해 둔 몫을 Lot 에서 다 못 찾았다",
        "release RELEASED",
    ):
        assert 조각 in 줄, f"요약 줄에 {조각!r} 이 없다: {줄}"
    assert "SI-SALE-B-1" not in 요약, "성공한 품목까지 실패 줄에 섰다"


def test_출고_결과는_하루_결과에_그대로_실린다() -> None:
    """★ 요약이 읽을 값이 **배관을 타고 오는가**.

    `run_scheduled_day` 가 출고의 낸 값을 떨어뜨리면 ④ 가 공짜로 통과한다.
    """
    from app.master import scheduler

    출고 = OutboundOut(as_of=AS_OF, status="NOTHING_DUE")
    status, 낸값, note = scheduler._outbound(
        as_of=AS_OF, sim_run_id=축, outbound_fn=lambda as_of, *, sim_run_id: 출고
    )
    assert status == "NOTHING_DUE"
    assert 낸값 is 출고
    assert note.startswith("출고")

    def _터짐(as_of: date, *, sim_run_id: str) -> Any:
        raise RuntimeError("연결 없음")

    status, 낸값, note = scheduler._outbound(as_of=AS_OF, sim_run_id=축, outbound_fn=_터짐)
    assert status == "FAILED"
    assert 낸값 is None


def test_하루를_돌리면_출고가_낸_값이_하루_결과에_실린다() -> None:
    """🔴 `_outbound` 가 값을 돌려줘도 `run_scheduled_day` 가 안 실으면 요약 줄은 늘 빈다.

    ★ `test_outbound_carries_run_axis.py` 의 «실제로 하루를 돌려 본다» 와 같은 대역 결이다.
    """
    from datetime import datetime

    from app.master.clock import SEOUL
    from app.master.pending_transition import RetryOut
    from app.master.scheduler import ScheduledAction, run_scheduled_day

    @dataclass
    class _단계결과:
        status: str
        reason: str = ""

    def _단계(status: str):
        return lambda _as_of, **_k: _단계결과(status)

    출고 = OutboundOut(as_of=AS_OF, status="NOTHING_DUE")
    action = ScheduledAction(
        as_of=AS_OF,
        now=datetime(2026, 2, 10, 9, 30, tzinfo=SEOUL),
        action="RUN_NOW",
        reason="검사",
        deadline=datetime(2026, 2, 10, 10, 30, tzinfo=SEOUL),
        ready_items=(),
        retry_after=None,
    )

    out = run_scheduled_day(
        action,
        sim_run_id=축,
        open_day_fn=_단계("OPENED"),
        retry_fn=lambda _as_of, **_k: RetryOut(status="NOTHING_DUE", reason="없다"),
        receive_fn=_단계("RECEIVED"),
        issue_fn=_단계("ISSUED"),
        collect_fn=_단계("COLLECTED"),
        outbound_fn=lambda as_of, *, sim_run_id: 출고,
        close_fn=_단계("CLOSED"),
        items=(),
    )

    assert out.outbound_status == "NOTHING_DUE", f"출고 단계가 안 돌았다: {out.notes}"
    assert out.outbound is 출고, "출고가 낸 값이 하루 결과에 안 실렸다"
