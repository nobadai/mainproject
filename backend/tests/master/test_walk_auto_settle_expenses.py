"""**걷기가 운영비를 지급한다 — 마감 바로 앞에서, 명시로 켤 때만** (2026-09-17).

```text
… → 출고 → 물류 점검 #2 → **운영비 지급** → 마감
```

★★ **왜 이 판이 있나 — `operating_expense_cash_out_krw` 가 늘 0원이었다.**

```text
발생  create_expense()     재무 원장에 있다
지급  settle_expense()     재무 원장에 있다
걷기  부르는 자리          **없었다**
```

  그래서 새 실행축의 운영비 유출은 날마다 0원이었다. 없던 것은 **부르는 자리 하나**이고
  (유지보수·전이 재시도 때와 같은 모양), 이 판이 잠그는 것이 그 자리다.

🔴 **기본이 꺼짐이다. 폐기보다 더 조심할 자리다.** 폐기는 물건이 없어지고 지급은
  **현금이 줄어드는데**, 되돌리는 경로가 재무에 없다 — `PAID → CANCELLED` 는 없다고
  `expenses.py` 가 못박았다.

🔴 **지급이 터진 날은 그날 마감이 `BLOCKED` 다.** 다른 단계와 태도가 다른 유일한 칸이다.
  그냥 넘기면 마감이 정상 `CLOSED` 로 서고 **«현금은 줄었는데 비용은 0원»** 인 기록이
  손익 곡선의 확정값으로 앉는다.

⚠️ **§4 말고는 DB 를 안 탄다.** 재무 경계도 연결도 대역이다 — 이 판이 잠그는 것은
  **부르는 자리와 그 순서와 트랜잭션 경계**이지, 무엇이 얼마 나가는가가 아니다
  (그것은 `tests/finance/` 가 이미 잠갔고 이 판은 그 규칙을 한 글자도 안 건드린다).

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가
  안 같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import inspect
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg import sql

from app.finance.closing_adapter import FinanceClosingAdapter
from app.finance.db import get_connection, get_db_schema
from app.finance.expenses import ExpenseSettlement, settle_due_expenses, settle_expense
from app.master import backtest_runner
from app.master import closing as master_closing
from app.master.backtest_runner import WalkResult, format_summary, walk
from app.master.clock import SEOUL
from app.master.closing import close_day
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.maintenance import MaintenanceOut
from app.master.pending_transition import RetryOut
from app.master.scheduler import (
    EXPENSE_SETTLEMENT_STATUSES,
    DayRunOutcome,
    ScheduledAction,
    plan_next_action,
    run_scheduled_day,
)

AS_OF = date(2026, 1, 12)
ITEMS = ("무", "배추", "양파")
실행 = "SIM-WALK-202601"

#: 걷기가 나르는 시각. 🔴 **벽시계가 아니다** — `--now` 가 주는 그 값이다.
지금 = datetime(2026, 9, 17, 10, 35, tzinfo=SEOUL)


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# ── 대역 ────────────────────────────────────────────────────────────────


@dataclass
class _Out:
    """서비스 함수가 내는 값의 최소 모양."""

    status: str
    reason: str = ""
    end_code: str | None = None


class _단계:
    """서비스 함수 대역. **불린 순서를 공용 목록에 적는다.**"""

    def __init__(self, 순서: list[str], 이름: str, out: object = None) -> None:
        self.순서 = 순서
        self.이름 = 이름
        self.out = out
        self.calls: list[Any] = []

    def __call__(self, arg=None, *args: Any, **kwargs: Any):
        self.순서.append(self.이름)
        self.calls.append({"as_of": arg, **kwargs})
        return self.out


class _판단:
    """`run_procurement` · `run_sales` 대역."""

    def __init__(self, 순서: list[str], 이름: str, end_code: str) -> None:
        self.순서 = 순서
        self.이름 = 이름
        self.end_code = end_code

    def __call__(self, request, verifier=None, **kwargs: Any):
        self.순서.append(self.이름)
        return _Out(status="RAN", end_code=self.end_code)


class _커넥션:
    """커밋 · 롤백 · 닫기를 **세기만** 한다. 🔴 DB 를 안 탄다."""

    def __init__(self, 순서: list[str]) -> None:
        self.순서 = 순서
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0

    def commit(self) -> None:
        self.committed += 1
        self.순서.append("지급커밋")

    def rollback(self) -> None:
        self.rolled_back += 1
        self.순서.append("지급롤백")

    def close(self) -> None:
        self.closed += 1


class _연결:
    """커넥션 팩토리 대역. `boom` 이면 **붙는 자리에서** 터진다."""

    def __init__(self, 순서: list[str], *, boom: Exception | None = None) -> None:
        self.순서 = 순서
        self.conn = _커넥션(순서)
        self.boom = boom
        self.calls = 0

    def __call__(self) -> _커넥션:
        self.calls += 1
        if self.boom is not None:
            raise self.boom
        return self.conn


def _지급결과(*금액: int) -> tuple[ExpenseSettlement, ...]:
    """`settle_due_expenses` 가 내는 값의 모양. **접지 않고 그대로 든다.**"""
    return tuple(
        ExpenseSettlement(
            expense_id=f"EXP-{자리}",
            paid_date=AS_OF,
            amount_krw=Decimal(하나),
            current_cash_krw=Decimal(0),
        )
        for 자리, 하나 in enumerate(금액)
    )


class _지급:
    """`settle_expenses_fn` 대역. **불렸는가 · 언제 · 무엇을 받았는가만 잰다.**

    🔴 **무엇을 지급할지 판단하지 않는다.** 대상도 금액도 지급일도 재무의 일이고,
      여기서 다시 재면 그 잠금이 두 곳에 생긴다.
    """

    def __init__(
        self,
        순서: list[str],
        *,
        out: tuple[ExpenseSettlement, ...] = (),
        boom: Exception | None = None,
    ) -> None:
        self.순서 = 순서
        self.out = out
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: Any, **kwargs: Any) -> tuple[ExpenseSettlement, ...]:
        self.순서.append("운영비지급")
        self.calls.append({"conn": conn, **kwargs})
        if self.boom is not None:
            raise self.boom
        return self.out


class _달력:
    def is_market_open(self, as_of: date) -> bool:
        return True


class _배치가_도는_날:
    def has_ml_batch(self, day: date) -> bool:
        return True


def _준비(as_of: date = AS_OF) -> DayForecastReadiness:
    return DayForecastReadiness(
        as_of=as_of,
        readiness="ALL_READY",  # type: ignore[arg-type]
        items=tuple(
            ItemForecastGate(item=item, as_of=as_of, readiness="READY", grade="MEASURED")  # type: ignore[arg-type]
            for item in ITEMS
        ),
    )


def _계획(as_of: date = AS_OF) -> ScheduledAction:
    return plan_next_action(
        now=datetime(as_of.year, as_of.month, as_of.day, 10, 35, tzinfo=SEOUL),
        as_of=as_of,
        calendar=_달력(),
        ml_batch=_배치가_도는_날(),
        gate_result=_준비(as_of),
    )


def _인자(순서: list[str], **over: Any) -> dict[str, Any]:
    인자: dict[str, Any] = {
        "open_day_fn": _단계(순서, "개장", _Out("OPENED")),
        "maintain_fn": _단계(순서, "유지보수", _Out("NOTHING_DUE")),
        "retry_fn": _단계(순서, "전이", RetryOut(status="NOTHING_DUE")),
        "receive_fn": _단계(순서, "입고", _Out("RECEIVED")),
        "inspect_fn": _단계(순서, "점검", _Out("NOTHING_DUE")),
        "issue_fn": _단계(순서, "채권", _Out("ISSUED")),
        "collect_fn": _단계(순서, "수금", _Out("COLLECTED")),
        "procure_fn": _판단(순서, "매입판단", "E1_DONE"),
        "sales_fn": _판단(순서, "판매판단", "SL1_PRESENTED"),
        "outbound_fn": _단계(순서, "출고", _Out("NOTHING_DUE")),
        "close_fn": _단계(순서, "마감", _Out("CLOSED")),
        "sim_run_id": 실행,
        "items": ITEMS,
    }
    인자.update(over)
    return 인자


def _하루(
    *, as_of: date = AS_OF, **over: Any
) -> tuple[DayRunOutcome, list[str], _지급, _연결]:
    """하루를 건다. **순서를 한 목록에 모아 돌려준다.**"""
    순서: list[str] = []
    지급 = over.pop("지급", None) or _지급(순서)
    연결 = over.pop("연결", None) or _연결(순서)
    인자 = _인자(순서, settle_expenses_fn=지급, connect=연결, **over)
    return run_scheduled_day(_계획(as_of), **인자), 순서, 지급, 연결  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════
#  ① 🔴 A — 안 주면 **이름조차 안 불린다** (오늘 상태)
# ══════════════════════════════════════════════════════════════════════


def test_auto_settle_expenses_를_안_주면_지급_함수가_이름조차_안_불린다() -> None:
    """🔴 **켜는 것은 명시로만이다.**

    ★ *"불렸는데 0건"* 과 *"안 불렸다"* 는 다르다. 지급은 현금을 줄이고, 되돌리는
      경로가 재무에 없다 (`PAID → CANCELLED` 없음).

    ⚠️ **지급할 것이 있어도 그렇다.** 이 대역은 `ACCRUED` 한 건을 내주기로 돼 있는데,
      스위치가 거짓이면 그 사실을 **물어보지도 않는다.**
    """
    순서: list[str] = []
    지급 = _지급(순서, out=_지급결과(3_855_000))
    out, 순서2, _, 연결 = _하루(지급=지급)

    assert 지급.calls == [], "auto_settle_expenses 를 안 줬는데 지급 함수가 불렸다"
    assert "운영비지급" not in 순서2
    assert out.expense_settlement_status == "NOT_ATTEMPTED"
    assert out.expense_settlements == ()
    assert 연결.calls == 0, "안 켰는데 커넥션을 열었다"


def test_안_켠_날은_사유_줄도_안_남긴다() -> None:
    """⚠️ 매일 *"안 켰다"* 를 한 줄씩 남기면 **진짜 사유가 안 읽힌다** (`_approve` 규율)."""
    out, _, _, _ = _하루()

    assert not [note for note in out.notes if _NFC("운영비") in _NFC(note)]


def test_안_켠_하루가_플래그가_없던_때와_글자_그대로_같다() -> None:
    """🔴 **플래그를 안 주면 기존과 한 글자도 달라지면 안 된다.**

    ★ 인자를 아예 안 준 하루와 `False` 로 준 하루가 **같은 값**이어야 한다 — 새 칸이
      기본값에 앉아 있다는 사실을 이 한 줄이 잠근다.
    """
    안준것, _, _, _ = _하루()
    끈것, _, _, _ = _하루(auto_settle_expenses=False)

    assert 안준것 == 끈것


def test_유지보수_스위치로는_지급이_안_켜진다() -> None:
    """🔴 **스위치를 한 칸으로 묶지 않는다.**

    ★ 창고를 비우려던 사람이 **현금까지 내보내게** 되면 안 된다.
    """
    순서: list[str] = []
    지급 = _지급(순서)
    인자 = _인자(
        순서,
        settle_expenses_fn=지급,
        connect=_연결(순서),
        maintain_fn=_단계(순서, "유지보수", MaintenanceOut(as_of=AS_OF, status="NOTHING_DUE")),
    )
    run_scheduled_day(_계획(), auto_approve=True, auto_maintain=True, **인자)  # type: ignore[arg-type]

    assert 지급.calls == [], "승인·유지보수만 켰는데 지급이 불렸다"
    assert "운영비지급" not in 순서


@pytest.mark.parametrize("함수", [run_scheduled_day, walk])
def test_기본이_꺼짐이다(함수: Any) -> None:
    """🔴 **두 자리 다 거짓이다.** 한 자리라도 참이면 그 자리가 우회로가 된다."""
    기본 = inspect.signature(함수).parameters["auto_settle_expenses"].default

    assert 기본 is False, f"{함수.__name__} 의 auto_settle_expenses 기본값이 {기본!r} 다"


def test_문에도_기본이_꺼짐이다() -> None:
    """🔴 **CLI 도 안 주면 안 켠다.** `store_true` 이고 기본이 거짓이다."""
    from app.master import backtest_runner

    공통 = ["--sim-run-id", 실행, "--start", "2026-01-01", "--end", "2026-03-31"]
    공통 += ["--now", "2026-09-17T10:35+09:00"]

    안준것 = backtest_runner._parser().parse_args(공통)
    준것 = backtest_runner._parser().parse_args([*공통, "--auto-settle-expenses"])

    assert 안준것.auto_settle_expenses is False
    assert 준것.auto_settle_expenses is True


def test_기본_지급_자리가_settle_due_expenses_자체다() -> None:
    """★ 갈아 끼울 자리에 **기본값이 진짜 함수**다 — `None` 을 안 받는다."""
    기본 = inspect.signature(run_scheduled_day).parameters["settle_expenses_fn"].default

    assert 기본 is settle_due_expenses


# ══════════════════════════════════════════════════════════════════════
#  ② 🔴 켜면 **마감 바로 앞**에 서고, 커밋이 마감보다 앞이다
# ══════════════════════════════════════════════════════════════════════


def test_지급이_마감_바로_앞에_선다() -> None:
    """🔴 **마감이 그날 `PAID` 인 비용을 읽어 운영비 칸을 적는다.**

    ★ 뒤로 밀면 오늘 나간 돈이 **내일 장부에** 적히고, 그날의 `Δ잔액` 과 `Σ순현금` 이
      그만큼 어긋난 채로 남는다.
    """
    out, 순서, 지급, _ = _하루(auto_settle_expenses=True, 지급=None)

    assert len(지급.calls) == 1
    assert 순서.index("운영비지급") == 순서.index("마감") - 2, "지급과 마감 사이에 다른 단계가 있다"
    assert 순서.index("출고") < 순서.index("운영비지급")
    assert out.expense_settlement_status == "NOTHING_DUE"


def test_커밋이_마감보다_먼저다() -> None:
    """🔴 **마감이 다른 커넥션으로 그날 `PAID` 를 읽는다.**

    ★★ 커밋을 마감 뒤로 미루면 마감이 **방금 나간 돈을 못 본다** — 운영비 칸이 0원으로
      확정되고, 그 숫자는 손익 곡선의 확정값이라 되돌릴 자리가 없다.
    """
    지급결과 = _지급결과(3_855_000)
    순서: list[str] = []
    지급 = _지급(순서, out=지급결과)
    _, 순서2, _, 연결 = _하루(auto_settle_expenses=True, 지급=지급)

    assert 순서2.index("지급커밋") < 순서2.index("마감")
    assert 연결.conn.committed == 1, "한 사이클에 커밋이 하나가 아니다"
    assert 연결.conn.rolled_back == 0
    assert 연결.conn.closed == 1


def test_커넥션을_부르는_쪽이_연다() -> None:
    """🔴 **`settle_due_expenses` 는 commit 도 rollback 도 안 한다고 적어 뒀다.**

    ★ 그 반대편이 스케줄러다 — 여러 건을 지급하다 중간에 터지면 앞선 지급까지 같이
      되돌아가야 하므로 **한 사이클이 한 커밋**이다.
    """
    _, _, 지급, 연결 = _하루(auto_settle_expenses=True)

    assert 연결.calls == 1, "하루에 커넥션을 한 번만 연다"
    assert 지급.calls[0]["conn"] is 연결.conn, "부르는 쪽이 연 커넥션을 안 넘겼다"
    assert 지급.calls[0]["sim_run_id"] == 실행
    assert 지급.calls[0]["as_of"] == AS_OF


def test_지급이_이번_실행_축으로만_돈다() -> None:
    """🔴 **번인 상수로 안 떨어진다.** 남의 실행 현금을 내보내면 되돌릴 수 없다."""
    _, _, 지급, _ = _하루(auto_settle_expenses=True, sim_run_id="SIM-WALK-OTHER")

    assert 지급.calls[0]["sim_run_id"] == "SIM-WALK-OTHER"


def test_금액도_지급일도_스케줄러가_안_정한다() -> None:
    """🔴 **넘기는 것은 실행 축과 기준일 둘뿐이다.**

    ★ 무엇이 얼마나 언제 나가는지의 주인은 `expenses` 표다 — 스케줄러가 금액이나
      지급일을 인자로 실으면 그 순간 업무 규칙이 코드에 박힌다.
    """
    _, _, 지급, _ = _하루(auto_settle_expenses=True)

    assert set(지급.calls[0]) == {"conn", "sim_run_id", "as_of"}


def test_NOT_ATTEMPTED_와_NOTHING_DUE_를_안_접는다() -> None:
    """🔴 *"안 켰다"* 와 *"켰는데 지급일이 된 것이 없었다"* 는 **0원의 다른 이유**다."""
    안켠날, _, _, _ = _하루()
    빈날, _, _, _ = _하루(auto_settle_expenses=True)

    assert 안켠날.expense_settlement_status == "NOT_ATTEMPTED"
    assert 빈날.expense_settlement_status == "NOTHING_DUE"


def test_지급한_건들을_접지_않고_그대로_들고_있다() -> None:
    """🔴 **`ExpenseSettlement` 가 주인이다.** 마스터가 다시 세지 않는다."""
    지급결과 = _지급결과(3_000_000, 855_000)
    순서: list[str] = []
    out, _, _, _ = _하루(auto_settle_expenses=True, 지급=_지급(순서, out=지급결과))

    assert out.expense_settlement_status == "RAN"
    assert out.expense_settlements == 지급결과


def test_관문이_막은_날은_지급을_아예_안_한다() -> None:
    """🔴 **그날은 어차피 마감이 안 선다** (`BLOCKED`).

    ★★ 현금만 줄이면 **«현금은 줄었는데 마감은 BLOCKED»** 가 남고, 그 판은 되돌릴
      자리가 없다.
    """
    순서: list[str] = []
    지급 = _지급(순서)
    연결 = _연결(순서)
    인자 = _인자(
        순서,
        settle_expenses_fn=지급,
        connect=연결,
        receive_fn=_단계(순서, "입고", _Out("BLOCKED")),
    )
    out = run_scheduled_day(_계획(), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

    assert out.procurement_status == "NOT_ATTEMPTED", "관문이 안 막았다 — 전제가 무너졌다"
    assert 지급.calls == []
    assert 연결.calls == 0
    assert out.expense_settlement_status == "NOT_ATTEMPTED"


def test_개장이_막힌_날은_지급을_아예_안_한다() -> None:
    """🔴 **하루가 안 열렸으면 현금을 안 내보낸다.**"""
    순서: list[str] = []
    지급 = _지급(순서)
    인자 = _인자(
        순서,
        settle_expenses_fn=지급,
        connect=_연결(순서),
        open_day_fn=_단계(순서, "개장", _Out("BLOCKED")),
    )
    out = run_scheduled_day(_계획(), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

    assert 지급.calls == []
    assert out.expense_settlement_status == "NOT_ATTEMPTED"


# ══════════════════════════════════════════════════════════════════════
#  ③ 🔴 F' — 지급이 터지면 **그날 마감이 BLOCKED 다**
# ══════════════════════════════════════════════════════════════════════
#
# ★★ 여기가 §8 의 핵심이다. 다른 단계는 터져도 하루가 계속 가는데 이 칸만 마감을 막는다.


@pytest.fixture
def 재무마감이_등록된다():
    """실 `close_day` 를 쓰려고 마감 등록소를 채운다. **끝나면 비운다.**

    ⚠️ 여기 재무는 **실 어댑터**이지만 커넥션이 대역이라 DB 를 안 탄다 — 이 판이
      잠그는 것은 *"막힌 날에 어댑터를 부르지도 않는다"* 이다.
    """
    master_closing.register_closing("finance", FinanceClosingAdapter())
    yield
    master_closing.reset()


def _마감문(연결: _연결):
    """스케줄러가 부르는 모양 그대로 실 `close_day` 를 태운다."""

    def 마감(as_of: date, **kwargs: Any):
        연결.순서.append("마감")
        return close_day(as_of, connect=연결, **kwargs)

    return 마감


def test_지급이_터지면_그날_마감이_BLOCKED_다(재무마감이_등록된다) -> None:
    """🔴🔴 **여기가 이 판의 핵심이다.**

    ★★ 그냥 `FAILED` 만 적고 넘기면 마감이 정상 `CLOSED` 로 서고 **«현금은 줄었는데
      비용은 0원»** 인 기록이 손익 곡선의 확정값으로 앉는다.

    🔴 **새 차단 수단을 안 만든다.** 장부 관문이 쓰는 그 길(`ledger_gap`)을 그대로 탄다 —
      `close_day` 의 판정 순서 ②다.
    """
    순서: list[str] = []
    지급 = _지급(순서, boom=RuntimeError("원장이 안 열린다"))
    연결 = _연결(순서)
    인자 = _인자(순서, settle_expenses_fn=지급, connect=연결, close_fn=_마감문(연결))
    out = run_scheduled_day(_계획(), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

    assert out.expense_settlement_status == "FAILED"
    assert out.closing_status == "BLOCKED", "지급이 터졌는데 그날이 그대로 닫혔다"
    assert out.closing_status != "CLOSED"
    assert out.closing is not None
    assert _NFC("운영비 지급이 터졌다") in _NFC(out.closing.reason)
    assert "RuntimeError" in out.closing.reason


def test_지급이_섰으면_그날은_그대로_닫힌다(재무마감이_등록된다) -> None:
    """🟢 **위 검사가 아무 날에나 BLOCKED 를 만들지 않음**을 같이 잠근다 — 자기 생존 검사다."""
    순서: list[str] = []
    지급 = _지급(순서, out=_지급결과(3_855_000))
    연결 = _연결(순서)
    인자 = _인자(순서, settle_expenses_fn=지급, connect=연결, close_fn=_마감문(연결))
    out = run_scheduled_day(_계획(), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

    assert out.expense_settlement_status == "RAN"
    assert out.closing_status != "BLOCKED"


def test_터지면_롤백하고_커밋을_안_한다() -> None:
    """🔴 **절반만 나간 지급을 장부에 남기지 않는다.**"""
    순서: list[str] = []
    지급 = _지급(순서, boom=RuntimeError("두 번째에서 터졌다"))
    _, _, _, 연결 = _하루(auto_settle_expenses=True, 지급=지급)

    assert 연결.conn.committed == 0
    assert 연결.conn.rolled_back == 1
    assert 연결.conn.closed == 1


def test_커넥션을_못_열어도_마감을_막는다() -> None:
    """🔴 **못 붙은 날을 조용히 «지급할 것이 없었다» 로 적지 않는다.**

    ★ 안 막으면 그날 나갔어야 할 돈이 **0원으로 확정된다.**
    """
    순서: list[str] = []
    연결 = _연결(순서, boom=RuntimeError("연결이 안 된다"))
    인자 = _인자(순서, settle_expenses_fn=_지급(순서), connect=연결)
    out = run_scheduled_day(_계획(), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

    assert out.expense_settlement_status == "FAILED"
    assert 연결.conn.committed == 0


def test_지급이_터져도_그날의_앞선_결과는_안_바뀐다() -> None:
    """★ **판단도 출고도 이미 끝났고 그것은 사실이다.** 막는 것은 마감 한 칸이다."""
    순서: list[str] = []
    지급 = _지급(순서, boom=RuntimeError("터짐"))
    out, _, _, _ = _하루(auto_settle_expenses=True, 지급=지급)

    assert out.day_open_status == "OPENED"
    assert out.inbound_status == "RECEIVED"
    assert out.procurement_status == "RAN"
    assert out.sales_status == "RAN"
    assert out.outbound_status == "NOTHING_DUE"
    assert any("RuntimeError" in note for note in out.notes)


def test_사유를_두_벌로_안_짓는다() -> None:
    """🔴 **note 에 적은 문장과 마감에 넘긴 문장이 같아야 한다.**

    ★ 두 벌이 되면 한쪽만 고치는 날 화면과 이력이 갈린다 — 장부 관문이 `gap_reason`
      하나를 두 곳에 그대로 넘기는 것과 같은 규율이다.
    """
    순서: list[str] = []
    지급 = _지급(순서, boom=RuntimeError("터짐"))
    마감 = _단계(순서, "마감", _Out("BLOCKED"))
    연결 = _연결(순서)
    인자 = _인자(순서, settle_expenses_fn=지급, connect=연결, close_fn=마감)
    out = run_scheduled_day(_계획(), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

    넘긴사유 = 마감.calls[0]["ledger_gap"]

    assert [note for note in out.notes if _NFC(넘긴사유) in _NFC(note)]


def test_지급이_선_날에는_마감에_ledger_gap_을_안_넘긴다() -> None:
    """🟢 **위 검사가 늘 `ledger_gap` 을 넘기지 않음**을 같이 잠근다.

    ★ 관문이 통과한 날 `None` 으로라도 넘기면 마감 대역마다 인자 모양이 바뀐다
      (`_closing` 이 적어 둔 그 규율).
    """
    순서: list[str] = []
    마감 = _단계(순서, "마감", _Out("CLOSED"))
    연결 = _연결(순서)
    인자 = _인자(순서, settle_expenses_fn=_지급(순서), connect=연결, close_fn=마감)
    run_scheduled_day(_계획(), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

    assert "ledger_gap" not in 마감.calls[0]


# ══════════════════════════════════════════════════════════════════════
#  ④ 🔴 G — `daily_closings` 까지 이어진다 (`-m db`)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.db
def test_그날_지급한_운영비가_마감행의_운영비_칸에_잡힌다() -> None:
    """🔴🔴 **지급 → 마감이 실제로 이어지는가.** 여기서만 그것이 보인다.

    ```text
    RENT 3,855,000원 ACCRUED · due_date = as_of
      → 지급 (현금 −3,855,000)
      → 마감 operating_expense_cash_out_krw = 3,855,000
                base_net_cash_krw          = −3,855,000   ← **한 번만** 빠진다
    ```

    ★ **커밋하지 않는다.** 넣고 돌리고 되돌린다 — 공유 DB 에 시연용 실행을 남기면
      그것이 나중에 사실로 읽힌다 (`test_collection_events` 와 같은 규율).

    ⚠️ **지급 대역이 실 `settle_expense` 를 부른다.** `settle_due_expenses` 는 아직
      껍데기라(재무 몫) 그 속으로는 한 건도 안 나간다 — 이 판이 재는 것은 **마스터가
      건 배선**(커넥션 · 커밋 · 마감보다 앞)이고, 속이 채워져도 그 배선은 그대로다.

    🔴 **`base_net_cash_krw` 를 여기서 다시 계산하지 않는다.** 주인은
      `_ClosingFacts.base_net_cash_krw` 이고, 이 판은 *"두 번 빠지지 않았나"* 만 본다.
    """
    schema = sql.Identifier(get_db_schema())
    축 = f"SIM-TEST-EXPENSE-{uuid.uuid4().hex[:8]}"
    기준일 = date(2026, 1, 12)
    금액 = Decimal(3855000)
    시작현금 = Decimal(100000000)
    conn = get_connection()

    class _안닫는커넥션:
        """스케줄러가 트랜잭션을 닫지 못하게 감싼다 — 안 그러면 되돌릴 수 없다."""

        def cursor(self) -> Any:
            return conn.cursor()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:  # pragma: no cover - 이 판에서는 안 탄다
            raise AssertionError("지급이 롤백됐다 — 이 판의 전제가 깨졌다")

        def close(self) -> None:
            return None

    그커넥션 = _안닫는커넥션()

    def _연결하기() -> Any:
        return 그커넥션

    비용 = f"EXP-{uuid.uuid4().hex[:8]}"

    def 지급하기(c: Any, *, sim_run_id: str, as_of: date):
        """실 `settle_expense` 를 그날 지급일이 된 한 건에 태운다."""
        return (
            settle_expense(
                c,
                expense_id=비용,
                sim_run_id=sim_run_id,
                financing_mode="LOAN_BASELINE",
                paid_date=as_of,
            ),
        )

    master_closing.register_closing("finance", FinanceClosingAdapter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT company_persona_id FROM {}.sim_runs LIMIT 1").format(schema)
            )
            페르소나 = cur.fetchall()[0]["company_persona_id"]
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.sim_runs (sim_run_id, company_persona_id, run_type,"
                    " period_start, period_end, as_of, status, financing_mode, config_json)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                ).format(schema),
                [
                    축,
                    # ★ **페르소나를 지어내지 않는다.** `sim_runs` 가 실제 행을 참조하고,
                    #   이 판은 실행을 하나 더 만드는 것이지 회사를 만드는 것이 아니다.
                    페르소나,
                    "WALK",
                    date(2026, 1, 1),
                    date(2026, 3, 31),
                    기준일,
                    "RUNNING",
                    "LOAN_BASELINE",
                    "{}",
                ],
            )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.finance_states (finance_state_id, sim_run_id, state_date,"
                    " state_type, financing_mode, current_cash_krw, minimum_operating_cash_krw)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                ).format(schema),
                [f"FS-{축}", 축, 기준일, "DAY", "LOAN_BASELINE", 시작현금, Decimal(0)],
            )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.expenses (expense_id, sim_run_id, expense_date,"
                    " expense_category, amount_krw, is_fixed, status, due_date)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                ).format(schema),
                [비용, 축, 기준일, "RENT", 금액, True, "ACCRUED", 기준일],
            )

        순서: list[str] = []
        인자 = _인자(
            순서,
            settle_expenses_fn=지급하기,
            connect=_연결하기,
            close_fn=lambda as_of, **kw: close_day(as_of, connect=_연결하기, **kw),
            sim_run_id=축,
        )
        out = run_scheduled_day(_계획(기준일), auto_settle_expenses=True, **인자)  # type: ignore[arg-type]

        assert out.expense_settlement_status == "RAN", out.notes
        assert out.closing_status == "CLOSED", out.notes

        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT operating_expense_cash_out_krw, base_net_cash_krw,"
                    " base_cash_balance_krw FROM {}.daily_closings"
                    " WHERE sim_run_id = %s AND close_date = %s"
                ).format(schema),
                [축, 기준일],
            )
            행들 = cur.fetchall()
            cur.execute(
                sql.SQL("SELECT status, paid_date FROM {}.expenses WHERE expense_id = %s").format(
                    schema
                ),
                [비용],
            )
            비용행 = cur.fetchall()[0]

        assert len(행들) == 1, "그날 마감행이 하나가 아니다"
        행 = 행들[0]
        assert 비용행["status"] == "PAID"
        assert 비용행["paid_date"] == 기준일
        assert Decimal(str(행["operating_expense_cash_out_krw"])) == 금액
        # 🔴 **정확히 한 번만 빠진다.** 두 번 빠지면 여기가 −7,710,000 이 된다.
        assert Decimal(str(행["base_net_cash_krw"])) == -금액
        assert Decimal(str(행["base_cash_balance_krw"])) == 시작현금 - 금액
    finally:
        master_closing.reset()
        conn.rollback()
        conn.close()


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 🟢 걷기 요약에 **지급 사실이 보인다**
# ══════════════════════════════════════════════════════════════════════


def test_지급이_있던_날마다_요약에_한_줄이_선다() -> None:
    """🔴 **이 줄이 없으면 걷기를 다 걷고도 «지급이 돌긴 했나» 를 못 답한다.**"""
    하루 = DayRunOutcome(
        as_of=AS_OF,
        action="RUN_NOW",
        reason="",
        expense_settlement_status="RAN",
        expense_settlements=_지급결과(3_000_000, 855_000),
    )
    결과 = WalkResult(start=AS_OF, end=AS_OF, days=(하루,))

    요약 = _NFC(format_summary(결과))

    assert 결과.expense_settlement_lines == ("2026-01-12  운영비 지급 2건 / 3,855,000원",)
    assert _NFC("운영비 지급 2건 / 3,855,000원") in 요약


def test_지급이_0건인_날은_줄이_안_는다() -> None:
    """🟢 **위 검사가 아무 날에나 줄을 만들지 않음**을 같이 잠근다.

    ★ 179일 중 지급이 있는 날은 몇 날뿐이고, 없는 날까지 찍으면 그 몇 줄이 묻힌다.
    """
    안켠날 = DayRunOutcome(as_of=AS_OF, action="RUN_NOW", reason="")
    빈날 = DayRunOutcome(
        as_of=AS_OF, action="RUN_NOW", reason="", expense_settlement_status="NOTHING_DUE"
    )
    결과 = WalkResult(start=AS_OF, end=AS_OF, days=(안켠날, 빈날))

    assert 결과.expense_settlement_lines == ()


def test_요약이_안_켠_것과_0건을_가른다() -> None:
    """🔴 **날짜 줄 하나로는 둘이 안 갈린다.**

    ```text
    안 켰다          운영비 줄이 없다   ← --auto-settle-expenses 를 안 줬다
    켰는데 0건       운영비 줄이 없다   ← 지급일이 된 것이 없었다
    ```

    ★★ 성적표를 보는 사람이 뒤엣것으로 읽는다. 실제로는 앞엣것일 수 있고, **그 둘은
      다음 걸음이 다르다** — 마감 줄이 없어서 03-06 뒤를 아무도 못 본 그 모양이다.
    """

    def _결과(*상태: str) -> WalkResult:
        날들 = tuple(
            DayRunOutcome(
                as_of=AS_OF + timedelta(days=자리),
                action="RUN_NOW",
                reason="",
                expense_settlement_status=하나,
            )
            for 자리, 하나 in enumerate(상태)
        )
        return WalkResult(start=AS_OF, end=날들[-1].as_of, days=날들)

    안켠것 = _결과("NOT_ATTEMPTED", "NOT_ATTEMPTED")
    켠것 = _결과("NOTHING_DUE", "NOTHING_DUE")

    assert 안켠것.expense_settlement_lines == 켠것.expense_settlement_lines, (
        "이 검사의 전제가 깨졌다 — 날짜 줄이 둘을 이미 가르면 이 줄은 필요 없다"
    )
    assert 안켠것.expense_settlement_statuses != 켠것.expense_settlement_statuses
    assert 안켠것.expense_settlement_statuses["NOT_ATTEMPTED"] == 2
    assert 켠것.expense_settlement_statuses["NOTHING_DUE"] == 2


def test_지급_분포_줄이_FAILED_0_도_찍는다() -> None:
    """🔴 **키가 안 보이면 «없었다» 와 «안 셌다» 가 같아진다** (`closing_statuses` 규율).

    ★ 이 줄에서 묻는 것은 *"지급이 터진 날이 없었다"* 이고, 그 답이 보이려면 `FAILED 0`
      이 찍혀야 한다.
    """
    결과 = WalkResult(
        start=AS_OF,
        end=AS_OF,
        days=(DayRunOutcome(as_of=AS_OF, action="RUN_NOW", reason=""),),
    )

    분포 = 결과.expense_settlement_statuses
    요약 = _NFC(format_summary(결과))

    assert set(분포) == {"NOT_ATTEMPTED", "RAN", "NOTHING_DUE", "FAILED"}
    assert 분포["FAILED"] == 0
    assert _NFC("운영비    {'FAILED': 0,") in 요약


def test_분포_어휘를_걷기가_제_손으로_안_짓는다() -> None:
    """🔴 **셋의 주인은 `scheduler.EXPENSE_SETTLEMENT_STATUSES` 하나다.**

    ⚠️ `NOT_ATTEMPTED` 는 거기 없다 — 단계를 안 탄 날 `DayRunOutcome` 이 두는
      기본값이고, 요약은 그 기본값을 그대로 읽어 붙인다.
    """
    assert EXPENSE_SETTLEMENT_STATUSES == {"RAN", "NOTHING_DUE", "FAILED"}
    assert DayRunOutcome.expense_settlement_status == "NOT_ATTEMPTED"


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 🔴 지급이 터진 날은 **사고로 세지고 · 걷기가 멈춘다**
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 **앞의 두 줄(개장 · 장부 관문)이 이 날을 못 잡는다.** 지급은 판단 뒤라 그 날
#    `procurement_status` 가 이미 `RAN` 이다 — 사고로 세는 자리가 지급 어휘뿐이다.


def _터진하루(as_of: date) -> DayRunOutcome:
    """지급이 터져 마감이 `BLOCKED` 로 닫힌 날. **판단은 이미 돌았다.**"""
    return DayRunOutcome(
        as_of=as_of,
        action="RUN_NOW",
        reason="",
        day_open_status="OPENED",
        inbound_status="RECEIVED",
        receivable_status="ISSUED",
        collection_status="COLLECTED",
        procurement_status="RAN",
        sales_status="RAN",
        outbound_status="NOTHING_DUE",
        expense_settlement_status="FAILED",
        closing_status="BLOCKED",
        notes=("운영비 지급이 터졌다: RuntimeError: 원장이 안 열린다",),
    )


def test_지급이_터진_날이_사고로_세진다() -> None:
    """🔴🔴 **조용히 계속 가면 179일을 BLOCKED 로 걷고 끝에서 안다.**

    ★ 마감 `FAILED` 를 사고로 센 것과 같은 이유다 — 그날 장부가 안 닫혔으면 다음 날
      판단은 **안 닫힌 장부 위에서** 돈다.
    """
    사유 = backtest_runner._incident_reason(_터진하루(AS_OF), scope="FULL")

    assert 사유 is not None, "지급이 터진 날이 사고로 안 세졌다"
    assert _NFC("운영비를 못 지급했다") in _NFC(사유)
    assert "RuntimeError" in 사유


def test_지급이_선_날은_사고가_아니다() -> None:
    """🟢 **위 검사가 아무 날에나 사고를 만들지 않음**을 같이 잠근다 — 자기 생존 검사다.

    ★ `NOT_ATTEMPTED` 도 `NOTHING_DUE` 도 정상이다. 안 켠 걷기가 날마다 사고면
      사고 목록이 아무것도 안 가리킨다.
    """
    for 상태 in ("NOT_ATTEMPTED", "NOTHING_DUE", "RAN"):
        하루 = DayRunOutcome(
            as_of=AS_OF,
            action="RUN_NOW",
            reason="",
            day_open_status="OPENED",
            procurement_status="RAN",
            expense_settlement_status=상태,
            closing_status="CLOSED",
        )
        assert backtest_runner._incident_reason(하루, scope="FULL") is None, 상태


def test_지급이_날마다_터지면_걷기가_연속_사고_상한에_걸려_멈춘다() -> None:
    """🔴 **첫날 지급이 터진 판이 179일을 걷고 끝에서 발견되면 안 된다.**

    ★★ 이 줄이 잠그는 것은 «사고로 센다» 가 실제로 **걷기를 멈추는 데까지** 이어지는가다 —
      센 것이 상한에 안 걸리면 세나 마나다.
    """

    def 하루(action: ScheduledAction, **kwargs: Any) -> DayRunOutcome:
        return _터진하루(action.as_of)

    결과 = walk(
        sim_run_id=실행,
        start=AS_OF,
        end=AS_OF + timedelta(days=30),
        now=지금,
        calendar=lambda: _달력(),
        ml_batch=lambda: _배치가_도는_날(),
        readiness=_준비,
        run_day_fn=하루,
        max_consecutive_failures=3,
        auto_settle_expenses=True,
        closings_of=lambda **kwargs: [],
        ticks=lambda: 0.0,
    )

    assert not 결과.completed, "지급이 날마다 터지는데 끝까지 걸었다"
    assert 결과.stopped_at == AS_OF + timedelta(days=2)
    assert len(결과.incidents) == 3
    assert _NFC("운영비를 못 지급했다") in _NFC(결과.incidents[0].reason)


def test_걷기가_받은_스위치를_그대로_하루에_넘긴다() -> None:
    """🔴 **여기서 다시 정하지 않는다** — 그러면 현금이 나가고 안 나가고를 정하는
    자리가 둘이 된다."""
    받은것: list[bool] = []

    def 하루(action: ScheduledAction, **kwargs: Any) -> DayRunOutcome:
        받은것.append(kwargs["auto_settle_expenses"])
        return DayRunOutcome(as_of=action.as_of, action=action.action, reason=action.reason)

    walk(
        sim_run_id=실행,
        start=AS_OF,
        end=AS_OF,
        now=지금,
        calendar=lambda: _달력(),
        ml_batch=lambda: _배치가_도는_날(),
        readiness=_준비,
        run_day_fn=하루,
        auto_settle_expenses=True,
        closings_of=lambda **kwargs: [],
        ticks=lambda: 0.0,
    )

    assert 받은것 == [True]
