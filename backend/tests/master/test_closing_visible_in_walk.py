"""**걷기 요약이 재무 일마감 결과를 말하고, 마감 실패를 사고로 센다** (2026-09-16).

★★ 실측: `SIM-CHAIN-CHECK-0916`(01-01~09-10) 요약은 「사고 0건 · 현금항등식 🟢」 이었는데
`daily_closings` 는 03-06 뒤로 0행이었다. 03-09 마감은
`FinanceDataNotReady: receivables_balance_mismatch` 로 터졌고, 요약에 마감 줄이 없고
마감 실패가 사고로 안 세어져서 아무도 못 봤다.

🔴 **DB 를 안 탄다.** 하루 실행 · 달력 · 게이트 · 마감행 조회를 전부 대역으로 준다.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.master.backtest_runner import WalkResult, format_summary, walk
from app.master.clock import SEOUL
from app.master.closing import ClosingOut
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.ledger_repository import (
    BASE_CASH_BALANCE,
    COLLECTION_CASH_IN,
    LOGISTICS_CASH_OUT,
    NET_CASH,
    PAYROLL_INTEREST_CASH_OUT,
    PURCHASE_CASH_OUT,
)
from app.master.scheduler import DayRunOutcome, ItemRunOutcome

실행축 = "SIM-TEST-CLOSING-LINE"
첫날 = date(2026, 3, 2)
AFTER_DEADLINE = datetime(2026, 3, 2, 10, 35, tzinfo=SEOUL)
ITEMS = ("무", "배추")

#: 03-09 에 실제로 나온 사유 모양 그대로.
실패사유 = "마감 실패: FinanceDataNotReady: receivables_balance_mismatch"


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class _Open:
    def is_market_open(self, day: date) -> bool:
        return True


def _gate(as_of: date) -> DayForecastReadiness:
    return DayForecastReadiness(
        as_of=as_of,
        readiness="ALL_READY",
        items=tuple(
            ItemForecastGate(item=i, as_of=as_of, readiness="READY", grade="MEASURED")
            for i in ITEMS
        ),
    )


def _하루(as_of: date, 마감: str) -> DayRunOutcome:
    """판단 · 출고까지 멀쩡히 돌고 마감만 `마감` 으로 끝난 하루.

    ★ `_stage` 가 `ClosingOut.reason` 을 note 로 싣는 모양 그대로 note 를 둔다.
    """
    사유 = 실패사유 if 마감 == "FAILED" else ""
    return DayRunOutcome(
        as_of=as_of,
        action="RUN_NOW",
        reason="예측이 왔다",
        day_open_status="OPENED",
        inbound_status="NOTHING_DUE",
        collection_status="NOTHING_DUE",
        procurement_status="RAN",
        outbound_status="NOTHING_DUE",
        closing_status=마감,
        closing=ClosingOut(as_of=as_of, status=마감, reason=사유),
        items=tuple(
            ItemRunOutcome(item=i, request_id=f"REQ-{i}", status="RAN", end_code="E1_OK")
            for i in ITEMS
        ),
        notes=("출고: NOTHING_DUE", f"마감: {마감} {사유}".strip()),
    )


def _마감행(day: date) -> dict[str, Any]:
    return {
        "close_date": day,
        PURCHASE_CASH_OUT: Decimal(0),
        LOGISTICS_CASH_OUT: Decimal(0),
        PAYROLL_INTEREST_CASH_OUT: Decimal(0),
        COLLECTION_CASH_IN: Decimal(0),
        NET_CASH: Decimal(0),
        BASE_CASH_BALANCE: Decimal(1000),
    }


def _걷기(마감들: list[str], *, 상한: int = 5) -> WalkResult:
    """`마감들[i]` 가 i 번째 날의 마감 상태다. 마감행은 `CLOSED` 인 날만 선다."""
    끝날 = 첫날 + timedelta(days=len(마감들) - 1)
    상태 = {첫날 + timedelta(days=i): one for i, one in enumerate(마감들)}
    행들 = tuple(_마감행(day) for day, one in 상태.items() if one == "CLOSED")
    return walk(
        sim_run_id=실행축,
        start=첫날,
        end=끝날,
        now=AFTER_DEADLINE,
        calendar=_Open,
        readiness=_gate,
        run_day_fn=lambda action, **_: _하루(action.as_of, 상태[action.as_of]),
        max_consecutive_failures=상한,
        ticks=lambda: 0.0,
        closings_of=lambda **_: 행들,
    )


def _줄(요약: str, 머리: str) -> str:
    줄들 = [one for one in 요약.splitlines() if one.startswith(_NFC(머리))]
    assert len(줄들) == 1, f"{머리!r} 줄이 하나가 아니다: {줄들}"
    return 줄들[0]


# ── (a) 마감이 선 날들 ─────────────────────────────────────────────────────


def test_마감이_선_날들은_CLOSED_수와_마지막_마감일을_찍는다() -> None:
    결과 = _걷기(["CLOSED", "CLOSED", "CLOSED"])
    줄 = _줄(_NFC(format_summary(결과)), "마감      ")

    assert 결과.closing_statuses["CLOSED"] == 3
    assert "'CLOSED': 3" in 줄, f"CLOSED 수가 줄에 없다: {줄}"
    assert "마지막 마감일 2026-03-04" in 줄, f"마지막 마감일이 다르다: {줄}"
    assert "첫 실패일 안 났다" in 줄
    assert 결과.incidents == ()


def test_마감_줄은_0_인_칸도_찍는다() -> None:
    """🔴 `FAILED` 0 이 안 보이면 *"없었다"* 와 *"안 셌다"* 가 같아진다."""
    줄 = _줄(_NFC(format_summary(_걷기(["CLOSED"]))), "마감      ")

    for 이름 in ("CLOSED", "NOTHING_DUE", "BLOCKED", "NOT_OPENED", "FAILED", "NOT_ATTEMPTED"):
        assert f"'{이름}'" in 줄, f"{이름} 칸이 안 찍혔다: {줄}"
    assert "'FAILED': 0" in 줄


# ── (b) 중간에 마감이 터진다 ──────────────────────────────────────────────


def test_중간에_마감이_터지면_FAILED_수와_첫_실패일과_사유를_찍는다() -> None:
    결과 = _걷기(["CLOSED", "CLOSED", "FAILED", "FAILED"])
    줄 = _줄(_NFC(format_summary(결과)), "마감      ")

    assert "'FAILED': 2" in 줄, f"FAILED 수가 다르다: {줄}"
    assert "'CLOSED': 2" in 줄
    assert "마지막 마감일 2026-03-03" in 줄, f"마지막 마감일이 다르다: {줄}"
    assert f"첫 실패일 2026-03-04 ({실패사유})" in 줄, f"첫 실패일·사유가 다르다: {줄}"


def test_마감_실패는_사고로_센다() -> None:
    """🔴 **「사고 0건」 인데 마감이 멈춰 있던 판을 다시 못 만든다.**"""
    결과 = _걷기(["CLOSED", "FAILED", "CLOSED", "FAILED"])
    요약 = _NFC(format_summary(결과))

    assert [one.as_of for one in 결과.incidents] == [date(2026, 3, 3), date(2026, 3, 5)]
    assert all("마감" in one.reason for one in 결과.incidents)
    assert all(실패사유 in one.reason for one in 결과.incidents), "사유가 사고 줄에 안 실렸다"
    assert _NFC("사고      2건") in 요약


def test_마감이_연달아_터지면_연속_사고_상한에_걸려_멈춘다() -> None:
    """★ **조용히 계속 가는 것보다 멈추는 쪽이 낫다.** 안 닫힌 장부 위에서 판단이 돈다."""
    결과 = _걷기(["CLOSED", "FAILED", "FAILED", "FAILED", "CLOSED"], 상한=3)

    assert not 결과.completed
    assert 결과.stopped_at == date(2026, 3, 5)
    assert len(결과.days) == 4


def test_마감_BLOCKED_는_사고로_안_센다() -> None:
    """⚠️ 앞 단계가 막힌 날의 결과이고, 그 사실은 앞 줄이 이미 잡는다."""
    결과 = _걷기(["CLOSED", "BLOCKED", "NOTHING_DUE"])

    assert 결과.incidents == ()


# ── (c) 현금 줄이 부분 합임을 드러낸다 ────────────────────────────────────


def test_현금_줄에_마감이_선_날과_돈_날을_찍는다() -> None:
    결과 = _걷기(["CLOSED", "CLOSED", "FAILED", "FAILED"], 상한=5)
    줄 = _줄(_NFC(format_summary(결과)), "현금        ")

    assert _NFC("마감이 선 날 2일 / 돈 날 4일") in 줄, f"현금 줄에 일수가 없다: {줄}"


def test_마감행이_0행이어도_현금_줄에_일수를_찍는다() -> None:
    결과 = _걷기(["FAILED", "NOTHING_DUE"], 상한=5)
    줄 = _줄(_NFC(format_summary(결과)), "현금        ")

    assert _NFC("없음") in 줄
    assert _NFC("마감이 선 날 0일 / 돈 날 2일") in 줄, f"현금 줄에 일수가 없다: {줄}"


# ── 배관 — 마감이 낸 값이 하루 결과까지 온다 ─────────────────────────────


def test_하루가_마감이_낸_값을_싣는다() -> None:
    """★ 값이 하루 결과에 안 실리면 요약의 사유는 영영 `모름` 이다."""
    from app.master import scheduler

    낸값 = ClosingOut(as_of=첫날, status="FAILED", reason=실패사유)
    받은: list[dict[str, Any]] = []

    def _마감(as_of: date, **kw: Any) -> ClosingOut:
        받은.append(kw)
        return 낸값

    상태, out, note = scheduler._closing(as_of=첫날, sim_run_id=실행축, close_fn=_마감)
    assert (상태, out) == ("FAILED", 낸값)
    assert 실패사유 in note
    assert 받은 == [{"sim_run_id": 실행축}], "관문이 통과한 날 ledger_gap 을 넘겼다"

    상태, out, _ = scheduler._closing(
        as_of=첫날, sim_run_id=실행축, close_fn=_마감, ledger_gap="장부가 안 섰다"
    )
    assert out == 낸값
    assert 받은[-1] == {"sim_run_id": 실행축, "ledger_gap": "장부가 안 섰다"}


def test_마감이_예외로_터지면_값은_없고_사유는_모름이다() -> None:
    from app.master import scheduler

    def _터진다(as_of: date, **kw: Any) -> ClosingOut:
        raise RuntimeError("재무가 죽었다")

    상태, out, note = scheduler._closing(as_of=첫날, sim_run_id=실행축, close_fn=_터진다)
    assert (상태, out) == ("FAILED", None)
    assert "재무가 죽었다" in note

    터진날 = DayRunOutcome(
        as_of=첫날, action="RUN_NOW", reason="예측이 왔다", closing_status="FAILED"
    )
    결과 = WalkResult(start=첫날, end=첫날, days=(터진날,))
    assert 결과.first_closing_failure == (첫날, "모름"), "사유를 지어냈다"
