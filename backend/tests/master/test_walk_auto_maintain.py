"""**걷기가 물류 자동 유지보수를 부른다 — 개장 바로 뒤에서, 명시로 켤 때만** (2026-09-11).

```text
개장 → **물류 유지보수** → 미적용 전이 재시도 → 입고 → 채권 → 수금 → [장부 관문]
     → 매입 판단 → 매입 승인 → 판매 판단 → 판매 승인 → 출고 → 마감
```

★★ **왜 이 판이 있나 — 창고가 차서 매입이 1월 12일부터 멈췄다.**

```text
실측 2026-09-11 · 보수안 1~3월 걷기 71일
  매입   15건 · 2026-01-05 ~ 01-09 **닷새뿐**
  보류   156건 · "하드 제약(창고)으로 수량이 0까지 축소되어 제안 불가"
  그날   warehouse_free_kg 0 · rental_cap_kg 0 · 이후 cap_by_date 전부 0.0
  🔴 걷기 실행 넷의 Lot 이 **전부 ACTIVE** — 폐기가 한 번도 안 불렸다
```

  폐기도 자리 반환도 `logistics.auto_maintenance` 에 **이미 있었다.** 없던 것은
  **부르는 자리 하나**다 — 판매 판단 0건 · 매입 승인 0건 때와 같은 모양이다.

🔴 **기본이 꺼짐이다. 승인보다 더 조심할 자리다.** 승인은 append-only 표에 한 줄이
  남는 것이지만 폐기는 **물건이 없어진다** — 물류가 *"되돌릴 경로가 없다
  (`ADJUST_IN` 없음 · 실사 제외)"* 고 못박았다.

⚠️ **DB 를 안 탄다.** 물류 경계도 연결도 전부 대역이다 — 이 판이 잠그는 것은
  **부르는 자리와 그 순서와 넘기는 어휘**이지, 무엇을 버리는가가 아니다 (그것은
  `tests/logistics/` 가 이미 잠갔고 이 판은 그 규칙을 한 글자도 안 건드린다).

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가
  안 같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import ast
import inspect
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.logistics.auto_maintenance import (
    AutoMaintenanceResult,
    LotMaintenanceOutcome,
    run_logistics_auto_maintenance,
)
from app.master import backtest_runner, maintenance, scheduler
from app.master.backtest_runner import WalkResult, format_summary, walk
from app.master.clock import SEOUL
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.maintenance import (
    AUTO_MAINTENANCE,
    FRESHNESS_EXPIRED,
    MaintenanceOut,
    run_auto_maintenance,
)
from app.master.pending_transition import RetryOut
from app.master.scheduler import (
    DayRunOutcome,
    ScheduledAction,
    plan_next_action,
    run_scheduled_day,
)

AS_OF = date(2026, 1, 12)
ITEMS = ("무", "배추", "양파")
실행 = "SIM-WALK-202601"

#: 걷기가 나르는 시각. 🔴 **벽시계가 아니다** — `--now` 가 주는 그 값이다.
지금 = datetime(2026, 9, 11, 10, 35, tzinfo=SEOUL)

#: `auto_maintenance.MaintenanceOutcome` 의 넷. 🔴 **여기서 새로 짓지 않고 그대로
#: 베낀다** — 이 목록이 저쪽과 갈리면 잠금이 옛 어휘를 재게 된다.
넷 = ("DISPOSED", "SKIPPED_HELD_ALLOCATION", "PALLETS_EMPTIED", "FAILED")

_스케줄러 = Path(scheduler.__file__)
_걷기 = Path(backtest_runner.__file__)

#: 물류가 안전 조건을 건 자리들. 🔴 **마스터가 이것을 직접 들여오면 그 조건이
#: 이 자리만 안 지나게 된다** — 경계는 `run_logistics_auto_maintenance` 하나다.
_우회로 = ("confirm_disposal", "empty_pallet", "load_lot_turnover", "_lot_disposable_qty")


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
        self.calls.append(arg)
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


def _로트(outcome: str, lot_id: str = "LOT-1") -> LotMaintenanceOutcome:
    return LotMaintenanceOutcome(
        lot_id=lot_id,
        outcome=outcome,  # type: ignore[arg-type]
        disposed_qty_kg=Decimal(0),
        remaining_qty_kg=Decimal(0),
    )


def _물류결과(*outcomes: str, as_of: date = AS_OF, examined: int = 9) -> AutoMaintenanceResult:
    """`run_logistics_auto_maintenance` 가 내는 값의 모양. **접지 않고 그대로 든다.**"""
    return AutoMaintenanceResult(
        as_of=as_of,
        sim_run_id=실행,
        examined_lots=examined,
        lots=tuple(_로트(이름, f"LOT-{i}") for i, 이름 in enumerate(outcomes)),
    )


class _유지보수:
    """`maintain_fn` 대역. **부른 순서와 받은 인자를 그대로 모은다.**

    🔴 **무엇을 버릴지 판단하지 않는다.** 후보도 안전 조건도 물류의 일이고 여기서
      다시 재면 그 잠금이 두 곳에 생긴다 — 이 대역이 재는 것은 **불렸는가 ·
      언제 불렸는가 · 무엇을 받았는가** 셋이다.
    """

    def __init__(
        self,
        순서: list[str],
        *,
        out: MaintenanceOut | None = None,
        boom: Exception | None = None,
    ) -> None:
        self.순서 = 순서
        self.out = out
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    def __call__(self, as_of: date, **kwargs: Any) -> MaintenanceOut:
        self.순서.append("유지보수")
        self.calls.append({"as_of": as_of, **kwargs})
        if self.boom is not None:
            raise self.boom
        if self.out is not None:
            return self.out
        return MaintenanceOut(as_of=as_of, status="RAN", result=_물류결과("DISPOSED"))


class _달력:
    def is_market_open(self, as_of: date) -> bool:
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


def _계획(as_of: date = AS_OF, *, now: datetime | None = None) -> ScheduledAction:
    순간 = now or datetime(as_of.year, as_of.month, as_of.day, 9, 30, tzinfo=SEOUL)
    return plan_next_action(now=순간, as_of=as_of, calendar=_달력(), gate_result=_준비(as_of))


def _인자(순서: list[str], **over: Any) -> dict[str, Any]:
    인자: dict[str, Any] = {
        "open_day_fn": _단계(순서, "개장", _Out("OPENED")),
        "retry_fn": _단계(순서, "전이", RetryOut(status="NOTHING_DUE")),
        "receive_fn": _단계(순서, "입고", _Out("RECEIVED")),
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
    *, as_of: date = AS_OF, now: datetime | None = None, **over: Any
) -> tuple[DayRunOutcome, list[str], _유지보수]:
    """하루를 건다. **순서를 한 목록에 모아 돌려준다.**"""
    순서: list[str] = []
    보수 = over.pop("보수", None) or _유지보수(순서)
    인자 = _인자(순서, maintain_fn=보수, **over)
    return run_scheduled_day(_계획(as_of, now=now), **인자), 순서, 보수  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════
#  ① 🔴 안 주면 **이름조차 안 불린다** (오늘 상태)
# ══════════════════════════════════════════════════════════════════════


def test_auto_maintain_을_안_주면_유지보수_함수가_이름조차_안_불린다() -> None:
    """🔴 **켜는 것은 명시로만이다.**

    ★ *"불렸는데 0건"* 과 *"안 불렸다"* 는 다르다. 폐기는 되돌릴 경로가 없어서
      (`ADJUST_IN` 없음 · 실사 제외) 지난 것을 되돌릴 방법이 아예 없다.
    """
    out, 순서, 보수 = _하루()

    assert 보수.calls == [], "auto_maintain 을 안 줬는데 유지보수 함수가 불렸다"
    assert "유지보수" not in 순서
    assert out.maintenance_status == "NOT_ATTEMPTED"
    assert out.maintenance is None


def test_안_켠_날은_사유_줄도_안_남긴다() -> None:
    """⚠️ 매일 *"안 켰다"* 를 한 줄씩 남기면 **진짜 사유가 안 읽힌다** (`_approve` 규율)."""
    out, _, _ = _하루()

    assert not [note for note in out.notes if _NFC("유지보수") in _NFC(note)]


@pytest.mark.parametrize(
    "함수",
    [run_scheduled_day, scheduler.wake_up, walk],
)
def test_기본이_꺼짐이다(함수: Any) -> None:
    """🔴 **세 자리 다 거짓이다.** 한 자리라도 참이면 그 자리가 우회로가 된다."""
    기본 = inspect.signature(함수).parameters["auto_maintain"].default

    assert 기본 is False, f"{함수.__name__} 의 auto_maintain 기본값이 {기본!r} 다"


def test_문에도_기본이_꺼짐이다() -> None:
    """🔴 **CLI 도 안 주면 안 켠다.** `store_true` 이고 기본이 거짓이다."""
    공통 = ["--sim-run-id", 실행, "--start", "2026-01-01", "--end", "2026-03-31"]
    공통 += ["--now", "2026-09-11T10:35+09:00"]

    안준것 = backtest_runner._parser().parse_args(공통)
    준것 = backtest_runner._parser().parse_args([*공통, "--auto-maintain"])

    assert 안준것.auto_maintain is False
    assert 준것.auto_maintain is True


def test_승인_스위치로는_유지보수가_안_켜진다() -> None:
    """🔴 **두 스위치를 한 칸으로 묶지 않는다.**

    ★ 승인을 켜려던 사람이 **창고까지 비우게** 되면 안 된다 — 폐기는 되돌릴
      경로가 없어서 그 실수가 영구적이다.
    """
    _, 순서, 보수 = _하루(auto_approve=True)

    assert 보수.calls == [], "승인만 켰는데 유지보수가 불렸다"
    assert "유지보수" not in 순서


# ══════════════════════════════════════════════════════════════════════
#  ② 🔴 켜면 **개장 바로 뒤**에 선다
# ══════════════════════════════════════════════════════════════════════


#: 켠 날의 제자리. ★★ **개장 바로 뒤이고 전이·입고 앞이다.**
제자리순서 = [
    "개장",
    "유지보수",
    "전이",
    "입고",
    "채권",
    "수금",
    "매입판단",
    "매입판단",
    "매입판단",
    "판매판단",
    "판매판단",
    "판매판단",
    "출고",
    "마감",
]


def test_유지보수가_개장_바로_뒤에_선다() -> None:
    """🔴 **뒤로 밀면 red 다.**

    ★★ **그날 자리를 비워야 그날 입고와 그날 매입 판단이 들어갈 자리가 생긴다.**
      뒤로 가면 비운 자리를 그날이 못 쓰고 하루씩 밀린다.
    """
    _, 순서, 보수 = _하루(auto_maintain=True)

    assert 순서 == 제자리순서
    assert len(보수.calls) == 1


def test_개장보다_앞으로_옮기면_이_검사가_잡는다() -> None:
    """🟢 **위 검사가 실제로 순서를 재는지**를 같이 잠근다 — 자기 생존 검사다.

    ★ 개장 앞은 특히 못 쓴다. 그날 상태 행이 아직 안 섰고, 하루가 안 열린 날은
      뒤를 아예 안 하는데 그 앞에서 **물건을 버리면** 되돌릴 수 없다.
    """
    앞선것 = ["유지보수", *[이름 for 이름 in 제자리순서 if 이름 != "유지보수"]]
    _, 제자리, _ = _하루(auto_maintain=True)

    assert 제자리 != 앞선것
    assert 제자리.index("유지보수") == 제자리.index("개장") + 1


def test_입고보다_뒤로_밀면_이_검사가_잡는다() -> None:
    """🟢 **자리를 비운 결과를 그날 입고와 그날 판단이 봐야 한다.**"""
    _, 제자리, _ = _하루(auto_maintain=True)

    assert 제자리.index("유지보수") < 제자리.index("입고")
    assert 제자리.index("유지보수") < 제자리.index("매입판단")


def test_개장이_막힌_날은_유지보수를_아예_안_한다() -> None:
    """🔴 **하루가 안 열렸으면 물건을 안 버린다.**

    ★ 상태 행이 없는 날에 폐기가 돌면 그 사실이 어느 하루에도 안 걸리고, 되돌릴
      경로도 없다.
    """
    순서: list[str] = []
    보수 = _유지보수(순서)
    인자 = _인자(순서, maintain_fn=보수, open_day_fn=_단계(순서, "개장", _Out("BLOCKED")))
    out = run_scheduled_day(_계획(), auto_maintain=True, **인자)  # type: ignore[arg-type]

    assert 보수.calls == []
    assert out.maintenance_status == "NOT_ATTEMPTED"


def test_WAIT_인_날은_유지보수를_아예_안_한다() -> None:
    """🔴 **`should_run` 이 아니면 아무것도 안 부른다.**

    ★ 마감 전에 열두 번 깨어나며 열두 번 창고를 비우면 안 된다.
    """
    순서: list[str] = []
    보수 = _유지보수(순서)
    기다림 = plan_next_action(
        now=datetime(2026, 1, 12, 9, 30, tzinfo=SEOUL),
        as_of=AS_OF,
        calendar=_달력(),
        gate_result=DayForecastReadiness(
            as_of=AS_OF,
            readiness="NONE_READY",  # type: ignore[arg-type]
            items=tuple(
                ItemForecastGate(item=item, as_of=AS_OF, readiness="NOT_YET", grade="MEASURED")  # type: ignore[arg-type]
                for item in ITEMS
            ),
        ),
    )
    assert 기다림.action == "WAIT"

    out = run_scheduled_day(기다림, auto_maintain=True, **_인자(순서, maintain_fn=보수))  # type: ignore[arg-type]

    assert 보수.calls == []
    assert out.maintenance_status == "NOT_ATTEMPTED"


# ══════════════════════════════════════════════════════════════════════
#  ③ 🔴 사유 · 행위자 · 시각을 **마스터가 넘긴다**
# ══════════════════════════════════════════════════════════════════════


def test_물류가_셋_다_기본값을_안_뒀다() -> None:
    """🔴 **물류 쪽 사실부터 잰다.** 기본값이 있으면 이 판의 전제가 무너진다.

    ★ 물류 원문: *"물류가 지어내지 않는다. 기본값을 두면 묻지도 않은 사유와
      행위자가 장부에 사실로 선다."* 그 빈칸을 채우는 것이 부르는 쪽의 일이다.
    """
    칸 = inspect.signature(run_logistics_auto_maintenance).parameters

    for 이름 in ("reason_code", "recorded_by", "occurred_at"):
        assert 칸[이름].default is inspect.Parameter.empty, f"물류가 {이름} 에 기본값을 뒀다"


def test_마스터가_사유와_행위자와_시각을_물류에_넘긴다() -> None:
    """🔴 **물류 기본값에 안 기댄다 — 셋을 다 채워 넘긴다.**

    ⚠️ 여기서 도는 것은 **진짜 `run_auto_maintenance`** 다. 연결과 물류 경계만
      대역이라, 넘기는 것이 마스터의 값임을 그대로 잰다.
    """
    경계 = _물류경계()
    연결 = _연결()

    out = run_auto_maintenance(
        AS_OF,
        sim_run_id=실행,
        occurred_at=지금,
        connect=연결,
        maintain_fn=경계,
    )

    assert 경계.calls == [
        {
            "sim_run_id": 실행,
            "as_of": AS_OF,
            "reason_code": FRESHNESS_EXPIRED,
            "recorded_by": AUTO_MAINTENANCE,
            "occurred_at": 지금,
        }
    ]
    assert out.status == "RAN"


def test_행위자가_사람_이름이_아니다() -> None:
    """🔴 **자동 승인이 `AUTO-BACKFILL` 을 쓰는 것과 같은 결이다.**

    ★ `pallet_events.recorded_by` 는 NOT NULL 이다 — 자동화 주체를 코드가 지어내면
      **그 이름이 장부에 사실로 남는다.**
    """
    from app.master.backfill import AUTO_BACKFILL

    assert AUTO_MAINTENANCE.startswith("AUTO-")
    assert AUTO_MAINTENANCE != AUTO_BACKFILL, "두 자동화가 같은 이름으로 적히면 못 가른다"


def test_사유가_씨앗_보정용_사유가_아니다() -> None:
    """🔴 물류 실측: 기존 `DISPOSE` 2건의 사유는 `MVP_DEMO_FIXTURE_CORRECTION` 이고
    **씨앗 보정용이지 업무 폐기 사유가 아니다.**"""
    assert FRESHNESS_EXPIRED != "MVP_DEMO_FIXTURE_CORRECTION"
    assert FRESHNESS_EXPIRED.strip() == FRESHNESS_EXPIRED != ""


# ══════════════════════════════════════════════════════════════════════
#  ④ 🔴 `occurred_at` 이 **벽시계가 아니라 걷기의 시간축이다**
# ══════════════════════════════════════════════════════════════════════


def test_occurred_at_이_그날_걷기의_시간축이다() -> None:
    """🔴 **벽시계를 읽으면 red 다.**

    ★★ 같은 걸음을 다시 걸으면 **같은 값**이 나와야 한다 (`sim_time.py` 가 적어 둔
      그 규율 · `fefo_allocation.decided_at` 과 같다). 벽시계를 읽으면 돌린 시각에
      따라 장부가 갈리고, 그 사실이 성적표 어디에도 안 남는다.
    """
    _, _, 보수 = _하루(auto_maintain=True, now=지금.replace(year=2026, month=1, day=12))

    받은시각 = 보수.calls[0]["occurred_at"]

    assert 받은시각 == datetime(2026, 1, 12, 10, 35, tzinfo=SEOUL)
    assert 받은시각.tzinfo is not None, "시간대 없는 시각은 어느 지역의 10:35 인지가 없다"
    assert 받은시각.date() == AS_OF, "그날이 아닌 날의 시각이 장부에 앉는다"


def test_걷는_날마다_그날의_시각이_간다() -> None:
    """🔴 **받은 `--now` 를 179일에 그대로 쓰지 않는다.**

    ★ 하루를 걸을 때마다 `_moment_on` 이 시각만 떼어 그날에 붙인다 — 그 값이
      그대로 `occurred_at` 으로 가야 폐기 사건이 **그날에** 적힌다.
    """
    받은날: list[datetime] = []

    def 하루(action: ScheduledAction, **kwargs: Any) -> DayRunOutcome:
        순서: list[str] = []
        보수 = _유지보수(순서)
        out = run_scheduled_day(
            action,
            **_인자(순서, maintain_fn=보수, sim_run_id=kwargs["sim_run_id"]),  # type: ignore[arg-type]
            auto_maintain=kwargs["auto_maintain"],
        )
        받은날.append(보수.calls[0]["occurred_at"])
        return out

    walk(
        sim_run_id=실행,
        start=AS_OF,
        end=AS_OF + timedelta(days=2),
        now=지금,
        calendar=lambda: _달력(),
        readiness=_준비,
        run_day_fn=하루,
        auto_maintain=True,
        ticks=lambda: 0.0,
    )

    assert [one.date() for one in 받은날] == [
        AS_OF,
        AS_OF + timedelta(days=1),
        AS_OF + timedelta(days=2),
    ]
    assert {one.timetz() for one in 받은날} == {지금.timetz()}


def test_마스터_유지보수가_시계를_안_읽는다() -> None:
    """🔴 **벽시계를 읽는 자리는 `clock.py` 하나다.**

    ★ `tests/master/test_clock_is_the_only_wall_clock.py` 와 같은 결이다 —
      이 파일에 `datetime.now` 나 `utcnow` 가 들어오면 시간축의 주인이 둘이 된다.
    """
    원문 = Path(maintenance.__file__).read_text(encoding="utf-8")

    for 금지 in ("datetime.now", "utcnow", "date.today", "time.time"):
        assert 금지 not in 원문, f"maintenance.py 가 {금지} 로 시계를 읽는다"


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 🔴 터져도 **하루는 계속 간다**
# ══════════════════════════════════════════════════════════════════════


def test_유지보수가_터져도_하루는_계속_간다() -> None:
    """🔴 **수금 씨앗 · 전이 재시도와 같은 태도다.**

    ★ 자리를 못 비운 것과 하루를 못 산 것은 다른 사실이다. 판단도 출고도 마감도
      그대로 돈다 — 터진 것은 상태와 사유로만 남는다.
    """
    순서: list[str] = []
    보수 = _유지보수(순서, boom=RuntimeError("유지보수가 터졌다"))
    out, 순서2, _ = _하루(auto_maintain=True, 보수=보수)

    assert out.maintenance_status == "FAILED"
    assert out.maintenance is None
    assert out.day_open_status == "OPENED"
    assert out.pending_transition_status == "NOTHING_DUE"
    assert out.inbound_status == "RECEIVED"
    assert out.procurement_status == "RAN"
    assert out.sales_status == "RAN"
    assert out.closing_status == "CLOSED"
    for 이름 in ("전이", "입고", "매입판단", "판매판단", "출고", "마감"):
        assert 이름 in 순서2, f"유지보수가 터졌다고 {이름} 가 안 돌았다"
    assert any("RuntimeError" in note for note in out.notes)


def test_유지보수가_터진_날도_걷기가_다음_날을_간다() -> None:
    """🔴 **하루가 터져도 다음 날을 계속 걷는다** — 걷기 쪽에서도 같은 태도다."""
    날들: list[date] = []

    def 하루(action: ScheduledAction, **kwargs: Any) -> DayRunOutcome:
        순서: list[str] = []
        날들.append(action.as_of)
        return run_scheduled_day(
            action,
            **_인자(순서, maintain_fn=_유지보수(순서, boom=RuntimeError("터짐"))),  # type: ignore[arg-type]
            auto_maintain=kwargs["auto_maintain"],
        )

    결과 = walk(
        sim_run_id=실행,
        start=AS_OF,
        end=AS_OF + timedelta(days=2),
        now=지금,
        calendar=lambda: _달력(),
        readiness=_준비,
        run_day_fn=하루,
        auto_maintain=True,
        ticks=lambda: 0.0,
    )

    assert 날들 == [AS_OF, AS_OF + timedelta(days=1), AS_OF + timedelta(days=2)]
    assert len(결과.days) == 3
    assert 결과.completed, "유지보수가 터졌다고 걷기가 멈췄다"


def test_연결이_안_되면_FAILED_로_돌아서고_예외를_안_낸다() -> None:
    """🔴 **`run_auto_maintenance` 가 예외를 밖으로 안 낸다.**"""

    def 못붙음() -> Any:
        raise RuntimeError("연결이 안 된다")

    out = run_auto_maintenance(AS_OF, sim_run_id=실행, occurred_at=지금, connect=못붙음)

    assert out.status == "FAILED"
    assert out.result is None


def test_물류가_터지면_롤백하고_커밋을_안_한다() -> None:
    """🔴 **반쯤 비운 창고를 장부에 남기지 않는다.**"""
    연결 = _연결()
    경계 = _물류경계(boom=RuntimeError("물류가 터졌다"))

    out = run_auto_maintenance(
        AS_OF, sim_run_id=실행, occurred_at=지금, connect=연결, maintain_fn=경계
    )

    assert out.status == "FAILED"
    assert 연결.conn.committed == 0
    assert 연결.conn.rolled_back == 1
    assert 연결.conn.closed == 1


def test_트랜잭션의_주인이_마스터다() -> None:
    """🔴 **물류가 커밋도 롤백도 안 한다고 못박았다 — 그 반대편이 여기다.**

    ★ **한 사이클이 한 커밋이다.** Lot 마다 나눠 적으면 물류가 배치 시작에서 잡아
      둔 잠금 순서가 중간에 풀려 다른 실행과 역전된다.
    """
    연결 = _연결()
    경계 = _물류경계(result=_물류결과("DISPOSED", "PALLETS_EMPTIED"))

    run_auto_maintenance(
        AS_OF, sim_run_id=실행, occurred_at=지금, connect=연결, maintain_fn=경계
    )

    assert 연결.conn.committed == 1, "한 사이클에 커밋이 하나가 아니다"
    assert 연결.conn.rolled_back == 0
    assert 연결.conn.closed == 1


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 🔴 **이번 실행 축으로만 돈다**
# ══════════════════════════════════════════════════════════════════════


def test_유지보수가_이번_실행_축으로만_돈다() -> None:
    """🔴 **번인 상수로 안 떨어진다.** 남의 실행 창고를 비우면 되돌릴 수 없다."""
    _, _, 보수 = _하루(auto_maintain=True, sim_run_id="SIM-WALK-OTHER")

    assert 보수.calls[0]["sim_run_id"] == "SIM-WALK-OTHER"
    assert 보수.calls[0]["as_of"] == AS_OF


# ══════════════════════════════════════════════════════════════════════
#  ⑦ 🟢 결과가 요약에 **접히지 않고** 올라온다
# ══════════════════════════════════════════════════════════════════════


def test_어휘_넷이_요약에_접히지_않고_올라온다() -> None:
    """🔴 *"버렸다"* 와 *"사람에게 남겼다"* 와 *"못 했다"* 를 묶으면 **창고가 왜 안
    비는지**를 성적표가 못 답한다.

    ★★ 특히 `SKIPPED_HELD_ALLOCATION` 이다 — 물류가 일부러 남긴 줄이고, 창고가
      안 비는 날 **거기부터 봐야** 한다.
    """
    하루 = DayRunOutcome(
        as_of=AS_OF,
        action="RUN_NOW",
        reason="",
        maintenance_status="RAN",
        maintenance=MaintenanceOut(
            as_of=AS_OF, status="RAN", result=_물류결과(*넷, *넷[:2])
        ),
    )
    결과 = WalkResult(start=AS_OF, end=AS_OF, days=(하루,))

    센것 = dict(결과.maintenance_outcomes)
    요약 = _NFC(format_summary(결과))

    assert 센것 == {넷[0]: 2, 넷[1]: 2, 넷[2]: 1, 넷[3]: 1}
    for 이름 in 넷:
        assert 이름 in 요약, f"요약에 유지보수 어휘 '{이름}' 이 없다"
    assert _NFC("유지보수") in 요약


def test_안_켠_날은_요약이_빈_칸이다() -> None:
    """🟢 위 검사가 **아무 날에나 값을 만들어 내지 않음**을 같이 잠근다."""
    결과 = WalkResult(
        start=AS_OF, end=AS_OF, days=(DayRunOutcome(as_of=AS_OF, action="RUN_NOW", reason=""),)
    )

    assert dict(결과.maintenance_outcomes) == {}


def test_물류_결과를_접지_않고_그대로_들고_있다() -> None:
    """🔴 **`AutoMaintenanceResult` 가 주인이다.** 마스터가 다시 세지 않는다.

    ★ `examined_lots` 가 없으면 *"0건 처리"* 가 *"아무것도 안 봤다"* 인지
      *"볼 것이 없었다"* 인지 구별되지 않는다.
    """
    물류것 = _물류결과("DISPOSED", "SKIPPED_HELD_ALLOCATION", examined=17)
    보수 = _유지보수([], out=MaintenanceOut(as_of=AS_OF, status="RAN", result=물류것))
    out, _, _ = _하루(auto_maintain=True, 보수=보수)

    assert out.maintenance is not None
    assert out.maintenance.result is 물류것, "물류가 낸 값을 마스터가 다시 지었다"
    assert out.maintenance.result.examined_lots == 17


def test_NOT_ATTEMPTED_와_NOTHING_DUE_를_안_접는다() -> None:
    """🔴 *"안 켰다"* 와 *"켰는데 버릴 것이 없었다"* 는 **폐기 0건의 다른 이유**다."""
    안켠날, _, _ = _하루()
    빈날, _, _ = _하루(
        auto_maintain=True,
        보수=_유지보수(
            [], out=MaintenanceOut(as_of=AS_OF, status="NOTHING_DUE", result=_물류결과())
        ),
    )

    assert 안켠날.maintenance_status == "NOT_ATTEMPTED"
    assert 빈날.maintenance_status == "NOTHING_DUE"


def test_관문이_막아도_유지보수_사실이_안_지워진다() -> None:
    """🔴 **폐기는 되돌릴 경로가 없다** — 한 일을 결과에서 지우면 아무도 못 본다.

    ★ 지우면 *"안 했다"* 와 *"했는데 관문에서 돌아섰다"* 가 같아진다.
    """
    순서: list[str] = []
    보수 = _유지보수(순서)
    인자 = _인자(순서, maintain_fn=보수, receive_fn=_단계(순서, "입고", _Out("BLOCKED")))
    out = run_scheduled_day(_계획(), auto_maintain=True, **인자)  # type: ignore[arg-type]

    assert out.procurement_status == "NOT_ATTEMPTED", "관문이 안 막았다 — 전제가 무너졌다"
    assert out.maintenance_status == "RAN"
    assert out.maintenance is not None


# ══════════════════════════════════════════════════════════════════════
#  ⑧ 🔴 **물류의 안전 조건을 우회하지 않는다**
# ══════════════════════════════════════════════════════════════════════


def test_마스터가_물류_경계를_우회하지_않는다() -> None:
    """★★ **다른 길을 내면 그 순간 「폐기」가 두 종류가 된다.**

    🔴 `confirm_disposal` 이나 `empty_pallet` 을 직접 들여오면 *"살아있는 할당이
      있으면 통째로 건너뛴다"* 는 물류의 안전 조건이 **이 자리만 안 지나게** 된다.
    """
    for 파일 in (_스케줄러, _걷기, Path(maintenance.__file__)):
        가져온것 = {
            별칭.name
            for node in ast.walk(ast.parse(파일.read_text(encoding="utf-8")))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for 별칭 in node.names
        }
        for 우회 in _우회로:
            assert 우회 not in 가져온것, f"{파일.name} 이 물류 경계를 건너뛰고 {우회} 를 들여왔다"

    들여온것 = {
        별칭.name
        for node in ast.walk(ast.parse(Path(maintenance.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for 별칭 in node.names
    }
    assert "run_logistics_auto_maintenance" in 들여온것, "마스터가 물류 경계를 안 들여왔다"


def test_기본_유지보수_자리가_run_auto_maintenance_자체다() -> None:
    """★ 갈아 끼울 자리에 **기본값이 진짜 함수**다 — `None` 을 안 받는다."""
    for 함수 in (run_scheduled_day, scheduler.wake_up):
        기본 = inspect.signature(함수).parameters["maintain_fn"].default
        assert 기본 is run_auto_maintenance, f"{함수.__name__} 의 기본 유지보수 자리가 다르다"


@pytest.mark.parametrize("어휘", [이름 for 이름 in 넷 if 이름 != "FAILED"])
def test_걷기_코드가_유지보수_어휘를_제_손으로_안_짓는다(어휘: str) -> None:
    """🔴 **넷의 주인은 `logistics/auto_maintenance.py` 하나다.** 세기만 한다.

    ⚠️ **`FAILED` 는 뺀다.** 그 한 낱말은 스케줄러가 단계마다 이미 쓰는 말이라
      원문 잠금이 유지보수와 무관한 자리를 잡는다.
    """
    for 파일 in (_스케줄러, _걷기, Path(maintenance.__file__)):
        코드 = 파일.read_text(encoding="utf-8")
        assert f'"{어휘}"' not in 코드, f"{파일.name} 에 유지보수 어휘 '{어휘}' 가 박혀 있다"


# ── 연결 · 물류 경계 대역 ───────────────────────────────────────────────


class _커넥션:
    """커밋 · 롤백 · 닫기를 **세기만** 한다. 🔴 DB 를 안 탄다."""

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


class _연결:
    def __init__(self) -> None:
        self.conn = _커넥션()

    def __call__(self) -> _커넥션:
        return self.conn


class _물류경계:
    """`run_logistics_auto_maintenance` 대역. **받은 인자를 그대로 모은다.**"""

    def __init__(
        self,
        *,
        result: AutoMaintenanceResult | None = None,
        boom: Exception | None = None,
    ) -> None:
        self.result = result if result is not None else _물류결과("DISPOSED")
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: Any, **kwargs: Any) -> AutoMaintenanceResult:
        self.calls.append(kwargs)
        if self.boom is not None:
            raise self.boom
        return self.result
