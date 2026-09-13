"""**예측 배치가 원래 없는 날 — 장부는 돌고 판단만 안 돈다** (2026-09-13).

```text
SIM-CHAIN-V9 (1~3월) · is_open=t 이고 is_survey=f 인 날 14일
  판단   매입 E4 42 · 판매 SL2 3 · SL6 39 · 확정 판매 0 · 선 매입 0   ← 아무것도 안 만들었다
  장부   입고 19 · 출고 19건 8,405kg · 폐기 3 · 마감 14일 · 수금 4,938,729원
```

★★ **스케줄러가 그 날을 「ML 이 늦는 날」로 읽었다.** 개장 달력만 보면 토요일은
  *"장이 선다"* 이고 게이트는 `NONE_READY` 라, 마감까지 기다린 뒤 `E4` 로 적었다.
  ML 이 `is_survey` 가 **「그날 예측 배치가 도는가」** 축이라고 확인했다 (90/90).

🔴 **이 파일이 막는 여섯 가지.**

```text
㉠ NO_ML_BATCH 인 날 출고를 건너뛴다              ← 제일 중요하다 · 곡선이 무너진다
㉡ NO_ML_BATCH 인 날 판단까지 돈다                ← E4 가 품목 수만큼 다시 쌓인다
㉢ NO_ML_BATCH 인 날 장부까지 안 돈다             ← V9① 의 모양 (수금 1,965만 → 1,166만)
㉣ 판단을 안 돈 것을 사고로 센다 / 장부 사고를 안 센다
㉤ is_survey 대신 is_open 을 읽는다               ← 이 분기가 한 번도 안 선다
㉥ 요약 기준시각 경고가 「배치 없는 날」로 돌아간다   (test_walked_now_provenance.py)
```

🔴 **DB 를 안 탄다.** 달력 둘 · 게이트 · 부서 함수가 전부 대역이다. 배치 축의 조회
   모양은 `MlBatchDays(read=...)` 로 가짜 행을 꽂아 잰다.

⚠️ **「0건을 세고 초록」을 막는다.** 불린 횟수는 `>= 1` 이 아니라 **정확한 목록**으로
  재고, 요약 줄은 **그 값이 실제로 찍혔는지**를 잰다.
"""

from __future__ import annotations

import ast
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, get_args

import pytest

from app.master import ml_batch_calendar, persistence, scheduler
from app.master.backfill import BackfillOut
from app.master.backtest_runner import format_summary, walk
from app.master.clock import SEOUL
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.maintenance import MaintenanceOut
from app.master.ml_batch_calendar import MlBatchDays
from app.master.pending_transition import RetryOut
from app.master.scheduler import (
    DayRunOutcome,
    ScheduledAction,
    SchedulerAction,
    plan_next_action,
    run_scheduled_day,
    scope_of,
)

_MASTER = Path(__file__).resolve().parents[2] / "app" / "master"

ITEMS = ("무", "배추", "양파")
실행축 = "SIM-TEST-NO-ML-BATCH"

#: 토요일 · 장은 서고 배치는 없다 (`SIM-CHAIN-V9` 의 그 14일 중 하루 모양).
토요일 = date(2026, 2, 7)
금요일 = date(2026, 2, 6)
월요일 = date(2026, 2, 9)


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SEOUL)


# ── 대역 ────────────────────────────────────────────────────────────────


class _시장:
    """개장 축 대역. **늘 선다** — 이 파일이 가르는 것은 배치 축이다."""

    def __init__(self, is_open: bool = True) -> None:
        self._is_open = is_open
        self.calls: list[date] = []

    def is_market_open(self, day: date) -> bool:
        self.calls.append(day)
        return self._is_open


class _배치:
    """배치 축 대역. `없는날` 에만 거짓 · `모르는날` 에는 못 읽었다고 던진다."""

    def __init__(
        self, *, 없는날: frozenset[date] = frozenset(), 모르는날: frozenset[date] = frozenset()
    ) -> None:
        self.없는날 = 없는날
        self.모르는날 = 모르는날
        self.calls: list[date] = []

    def has_ml_batch(self, day: date) -> bool:
        self.calls.append(day)
        if day in self.모르는날:
            raise CalendarNotCovered(f"{day} 의 배치 여부를 모른다")
        return day not in self.없는날


def _게이트(day: date, readiness: str) -> DayForecastReadiness:
    준비 = {"ALL_READY": "READY", "NONE_READY": "NOT_YET", "UNREADABLE": "UNREADABLE"}[readiness]
    등급 = {"READY": "MEASURED", "NOT_YET": "MISSING", "UNREADABLE": None}[준비]
    return DayForecastReadiness(
        as_of=day,
        readiness=readiness,  # type: ignore[arg-type]
        items=tuple(
            ItemForecastGate(item=i, as_of=day, readiness=준비, grade=등급)  # type: ignore[arg-type]
            for i in ITEMS
        ),
    )


def _계획(
    day: date = 토요일,
    *,
    now: datetime | None = None,
    batch: _배치 | None = None,
    market: _시장 | None = None,
    gate: str = "NONE_READY",
) -> ScheduledAction:
    return plan_next_action(
        now=_at(day, 9, 30) if now is None else now,
        as_of=day,
        calendar=_시장() if market is None else market,
        ml_batch=_배치(없는날=frozenset({day})) if batch is None else batch,
        gate_result=_게이트(day, gate),
    )


@dataclass
class _Out:
    status: str
    reason: str = ""


@dataclass
class _판단답:
    end_code: str


class _하루기록:
    """하루가 **무엇을 어떤 인자로** 불렀는지 한 목록에 모은다.

    ★ 이름과 함께 `(as_of, sim_run_id)` 를 적는다. 배치 없는 날과 있는 날의 장부
      호출이 **인자까지 같은지**를 한 줄로 대조하려는 것이다.
    """

    def __init__(self, *, inbound: str = "RECEIVED", open_status: str = "OPENED") -> None:
        self.log: list[tuple[str, date, str]] = []
        self.inbound = inbound
        self.open_status = open_status

    def _단계(self, name: str, out: Any):
        def call(as_of: date, *, sim_run_id: str, **_: Any) -> Any:
            self.log.append((name, as_of, sim_run_id))
            return out

        return call

    def 인자(self, **over: Any) -> dict[str, Any]:
        def 매입(request, verifier=None):
            self.log.append((f"매입판단:{request.item}", request.as_of, request.sim_run_id))
            return _판단답(end_code="E4_NOT_STARTED")

        def 판매(request, verifier=None):
            self.log.append((f"판매판단:{request.item}", request.as_of, request.sim_run_id))
            return _판단답(end_code="SL6_VALIDATION_UNRESOLVED")

        def 승인(*, sim_run_id: str, start: date, end: date, runs_on: Any) -> BackfillOut:
            self.log.append(("승인", start, sim_run_id))
            return BackfillOut(sim_run_id=sim_run_id, start=start, end=end, status="RAN")

        인자: dict[str, Any] = {
            "open_day_fn": self._단계("개장", _Out(self.open_status)),
            "maintain_fn": lambda as_of, *, sim_run_id, occurred_at: (
                self.log.append(("유지보수", as_of, sim_run_id))
                or MaintenanceOut(as_of=as_of, status="NOTHING_DUE")
            ),
            "retry_fn": self._단계("재시도", RetryOut(status="NOTHING_DUE")),
            "receive_fn": self._단계("입고", _Out(self.inbound)),
            "issue_fn": self._단계("채권", _Out("ISSUED")),
            "collect_fn": self._단계("수금", _Out("COLLECTED")),
            "procure_fn": 매입,
            "sales_fn": 판매,
            "approve_fn": 승인,
            "outbound_fn": self._단계("출고", _Out("RAN")),
            "close_fn": self._단계("마감", _Out("CLOSED")),
            "sim_run_id": 실행축,
            "items": ITEMS,
            "auto_approve": True,
            "auto_maintain": True,
        }
        인자.update(over)
        return 인자

    def 이름들(self) -> list[str]:
        return [name for name, _, _ in self.log]


#: 배치 없는 날에도 **돌아야 하는** 단계. 🔴 출고가 여기 있다.
장부단계 = ("개장", "유지보수", "재시도", "입고", "채권", "수금", "출고", "마감")


@pytest.fixture(autouse=True)
def 관문_행_적재를_막는다(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """장부 관문이 막는 날 `record_ledger_gap` 이 실 DB 로 가지 않게 한다."""
    적힌것: list[dict[str, Any]] = []
    monkeypatch.setattr(persistence, "record_ledger_gap", lambda **kw: 적힌것.append(kw))
    return 적힌것


# ── ① 판정 — 달력 뒤 · 게이트 앞 ────────────────────────────────────────


@pytest.mark.parametrize(("hour", "minute"), [(9, 30), (10, 29), (10, 30), (16, 0)])
def test_배치가_없는_날은_마감과_무관하게_NO_ML_BATCH_다(hour: int, minute: int) -> None:
    """🔴 **마감 전에도 `WAIT` 이 아니다.** 기다릴 예측이 원래 없다.

    ★ V9① 이 09:00 으로 걸었을 때 이 날이 `WAIT` 이 되어 장부까지 통째로 안 돌았다.
    """
    action = _계획(now=_at(토요일, hour, minute))

    assert action.action == "NO_ML_BATCH", action
    assert action.retry_after is None, "NO_ML_BATCH 에 재시도 간격이 붙으면 그것이 곧 WAIT 다"
    assert action.scope == "LEDGER_ONLY"
    assert "달력은 열렸는데 ML 배치가 없었다" in action.reason


def test_배치_축은_is_survey_를_읽은_답이다_개장_여부가_아니다() -> None:
    """🔴 **㉤ 장은 서고 배치는 없는 날이 이 판의 전부다.** 개장 칸을 다시 읽으면 이
    분기는 한 번도 안 선다 — 토요일이 다시 `RUN_AND_RECORD` 가 된다.
    """
    배치 = _배치(없는날=frozenset({토요일}))
    시장 = _시장(is_open=True)

    action = _계획(now=_at(토요일, 16, 0), batch=배치, market=시장)

    assert action.action == "NO_ML_BATCH", action
    assert 배치.calls == [토요일], f"배치 축에 그날을 안 물었다: {배치.calls}"


def test_배치가_도는_날은_종전_그대로다() -> None:
    """★ 배치 축을 지난 날에는 게이트가 종전대로 가른다 — 늦으면 기다리고 마감 뒤엔 기록한다."""
    있음 = _배치()

    assert _계획(now=_at(토요일, 9, 30), batch=있음).action == "WAIT"
    assert _계획(now=_at(토요일, 10, 35), batch=_배치()).action == "RUN_AND_RECORD"
    assert _계획(now=_at(토요일, 9, 30), batch=_배치(), gate="ALL_READY").action == "RUN_NOW"


def test_장이_안_서는_날은_배치_축을_안_묻는다() -> None:
    """🔴 **달력 뒤다.** 휴장일은 `NOT_A_MARKET_DAY` 이고 배치 여부는 물을 이유가 없다."""
    배치 = _배치(없는날=frozenset({토요일}))

    action = _계획(market=_시장(is_open=False), batch=배치)

    assert action.action == "NOT_A_MARKET_DAY"
    assert 배치.calls == []


def test_게이트를_못_읽어도_배치가_없는_날은_NO_ML_BATCH_다() -> None:
    """🔴 **게이트 앞이다.** 배치가 없는 날은 게이트를 볼 이유가 없다."""
    action = _계획(gate="UNREADABLE")

    assert action.action == "NO_ML_BATCH", action


def test_배치_축을_못_읽으면_BLOCKED_다_NO_ML_BATCH_로_접지_않는다() -> None:
    """🔴 **fail-closed.** 못 읽은 것을 *"배치가 없다"* 로 만들면 DB 가 죽은 날 판단이
    통째로 빠지고, 그 사실이 정상처럼 보인다.
    """
    action = _계획(batch=_배치(모르는날=frozenset({토요일})))

    assert action.action == "BLOCKED"
    assert action.scope == "NONE"
    assert "배치 달력" in action.reason


def test_배치가_없다는데_예측이_와_있으면_사유에_어긋남을_적는다() -> None:
    """⚠️ **조용히 넘기지 않는다.** 90일 중 0일이지만 달력은 사람이 넣는 값이다."""
    어긋남 = _계획(gate="ALL_READY")
    정상 = _계획(gate="NONE_READY")

    assert 어긋남.action == "NO_ML_BATCH"
    assert "달력과 배치가 어긋났다" in 어긋남.reason, 어긋남.reason
    assert "배추" in 어긋남.reason, "어느 품목이 와 있었는지가 사라졌다"
    assert "어긋났다" not in 정상.reason, "어긋나지 않은 날에도 경고가 붙으면 경고가 아니다"


def test_요일로_판정하지_않는다() -> None:
    """🔴 **토요일은 대부분 개장이고, 공휴일이 아닌데 장이 안 서는 평일도 있다.**

    ★ 배치 축을 읽는 두 모듈이 `weekday` · `isoweekday` 를 부르면 달력이 아니라 요일이
      판정한다.
    """
    for 파일 in ("scheduler.py", "ml_batch_calendar.py"):
        나무 = ast.parse((_MASTER / 파일).read_text(encoding="utf-8"))
        부른것 = {
            node.func.attr
            for node in ast.walk(나무)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not 부른것 & {"weekday", "isoweekday"}, f"{파일} 이 요일로 판정한다"


# ── ② 범위 — bool 에 안 끼운다 ──────────────────────────────────────────


def test_어휘마다_하루가_어디까지_도는지가_한_표에_있다() -> None:
    """🔴 **㉡ · ㉢ 여섯 어휘 전부를 잰다.** 표에 없는 어휘가 생기면 여기서 빨개진다."""
    기대 = {
        "RUN_NOW": "FULL",
        "RUN_AND_RECORD": "FULL",
        "NO_ML_BATCH": "LEDGER_ONLY",
        "WAIT": "NONE",
        "NOT_A_MARKET_DAY": "NONE",
        "BLOCKED": "NONE",
    }

    assert set(get_args(SchedulerAction)) == set(기대), "어휘와 표가 갈렸다"
    assert {이름: scope_of(이름) for 이름 in get_args(SchedulerAction)} == 기대


def test_참거짓_하나로_세_갈래를_말하는_자리가_남아_있지_않다() -> None:
    """🔴 **`should_run` 이 남으면 누군가 그것을 읽어 `NO_ML_BATCH` 를 둘 중 한쪽으로 접는다.**"""
    assert not hasattr(ScheduledAction, "should_run")


# ── ③ 하루 — 장부는 돌고 판단만 안 돈다 ─────────────────────────────────


def test_배치가_없는_날_장부가_전부_실제로_불리고_판단은_하나도_안_불린다() -> None:
    """🔴 **㉠ · ㉡ · ㉢ 을 한 목록으로 잰다.** 순서까지 정확히 같아야 한다.

    ★★ **출고가 목록에 있다.** 그날 나가는 것은 앞선 날 확정된 주문이다 — V9 의 그
      14일에 19건 8,405kg 이 나갔다.
    """
    기록 = _하루기록()

    out = run_scheduled_day(_계획(), **기록.인자())

    assert 기록.이름들() == list(장부단계), 기록.이름들()
    assert all(day == 토요일 and 축 == 실행축 for _, day, 축 in 기록.log), 기록.log
    assert out.action == "NO_ML_BATCH"
    assert out.outbound_status == "RAN", "배치 없는 날에 출고가 빠졌다 — 곡선이 무너진다"
    assert out.closing_status == "CLOSED"
    assert (out.inbound_status, out.receivable_status, out.collection_status) == (
        "RECEIVED",
        "ISSUED",
        "COLLECTED",
    )
    assert out.maintenance_status == "NOTHING_DUE"
    assert out.pending_transition_status == "NOTHING_DUE"
    assert out.items == () and out.sales_items == ()


def test_배치가_없는_날의_장부_호출이_배치가_있는_날과_인자까지_같다() -> None:
    """🔴 **업무 숫자가 안 움직여야 한다.** V10 과 V9 의 입고 · 수금 · 출고 · 마감이
    같으려면, 배치가 없는 날의 장부 호출이 **판단만 뺀 전체 날**과 같아야 한다.
    """
    배치없음 = _하루기록()
    배치있음 = _하루기록()

    run_scheduled_day(_계획(), **배치없음.인자())
    run_scheduled_day(_계획(batch=_배치(), gate="ALL_READY"), **배치있음.인자())

    전체에서_장부만 = [one for one in 배치있음.log if one[0] in 장부단계]
    assert 배치없음.log == 전체에서_장부만, (배치없음.log, 배치있음.log)
    assert len(배치있음.log) > len(전체에서_장부만), "비교 대상이 판단을 안 돌렸다 — 전제가 깨졌다"


def test_배치가_없는_날의_판단_네_칸에_새_값이_실린다() -> None:
    """🔴 **㉣ `NOT_ATTEMPTED` 가 아니다.** 그 값이면 걷기가 14일을 전부 사고로 센다."""
    out = run_scheduled_day(_계획(), **_하루기록().인자())

    assert (
        out.procurement_status,
        out.procurement_approval_status,
        out.sales_status,
        out.sales_approval_status,
    ) == ("NO_ML_BATCH",) * 4
    assert out.procurement_approval is None and out.sales_approval is None


def test_승인을_안_켠_날에도_승인_칸이_NO_ML_BATCH_다() -> None:
    """⚠️ 안 켠 날에 `NOT_ATTEMPTED` 로 두면 **승인 줄에서만** 그 날이 사라진다."""
    out = run_scheduled_day(_계획(), **_하루기록().인자(auto_approve=False))

    assert out.procurement_approval_status == "NO_ML_BATCH"
    assert out.sales_approval_status == "NO_ML_BATCH"


def test_배치가_없는_날에도_장부_관문은_선다() -> None:
    """🔴 **장부가 막히면 종전 그대로 돌아선다.** 출고도 안 하고 마감은 `BLOCKED` 로 적는다."""
    기록 = _하루기록(inbound="BLOCKED")

    out = run_scheduled_day(_계획(), **기록.인자(close_fn=lambda as_of, **kw: _Out("BLOCKED")))

    assert "출고" not in 기록.이름들()
    assert out.procurement_status == "NOT_ATTEMPTED", "장부 사고가 NO_ML_BATCH 로 가려졌다"
    assert out.outbound_status == "NOT_ATTEMPTED"
    assert out.closing_status == "BLOCKED"


# ── ④ 걷기 — 사고 판정과 요약 ──────────────────────────────────────────


def _걷기(*, now: datetime, 기록: _하루기록, **over: Any):
    인자: dict[str, Any] = {
        "sim_run_id": 실행축,
        "start": 금요일,
        "end": 월요일,
        "now": now,
        "calendar": lambda: _시장(),
        "ml_batch": lambda: _배치(없는날=frozenset({토요일, date(2026, 2, 8)})),
        "readiness": lambda day: _게이트(
            day, "NONE_READY" if day in (토요일, date(2026, 2, 8)) else "ALL_READY"
        ),
        # 🔴 **진짜 하루 실행을 부른다.** 걷기 대역을 쓰면 「사고로 세나」 를 대역이 정한다.
        "run_day_fn": partial(run_scheduled_day, **기록.인자()),
        "ticks": lambda: 0.0,
        "terms_of": lambda _sim: None,
        "closings_of": lambda **_: (),
    }
    인자.update(over)
    return walk(**인자)


@pytest.mark.parametrize("시각", [(16, 0), (9, 0)])
def test_걷기에서_배치가_없는_날은_사고가_아니고_장부는_돈다(시각: tuple[int, int]) -> None:
    """🔴 **㉣ 판단을 안 돈 날을 사고로 세지 않는다 · ㉢ 마감 전 시각에도 장부가 돈다.**

    ★ 09:00 이 V9① 의 그 시각이다. 그때는 이 이틀이 `WAIT` 이 되어 통째로 빠졌다.
    """
    기록 = _하루기록()

    result = _걷기(now=_at(금요일, *시각), 기록=기록)

    assert result.completed and result.incidents == (), result.incidents
    assert dict(result.actions)["NO_ML_BATCH"] == 2, result.actions
    출고한날 = [day for name, day, _ in 기록.log if name == "출고"]
    마감한날 = [day for name, day, _ in 기록.log if name == "마감"]
    assert 출고한날 == [금요일, 토요일, date(2026, 2, 8), 월요일], 출고한날
    assert 마감한날 == 출고한날
    assert "E4_NOT_STARTED" in result.end_codes, "전제가 깨졌다 — 판단이 돈 날의 코드가 없다"
    assert result.end_codes["E4_NOT_STARTED"] == len(ITEMS) * 2, "배치 없는 날의 판단이 돌았다"


def test_걷기에서_배치가_없는_날의_장부_사고는_여전히_사고다() -> None:
    """🔴 **㉣ 사고 판정에서 통째로 빼지 않는다.** 개장이 막힌 토요일은 사고다."""
    기록 = _하루기록(open_status="FAILED")

    result = _걷기(now=_at(금요일, 16, 0), 기록=기록, start=토요일, end=토요일)

    assert result.actions == {"NO_ML_BATCH": 1}
    assert len(result.incidents) == 1, "배치 없는 날의 장부 사고가 안 세졌다"
    assert "판단 단계를 안 탔다" in result.incidents[0].reason


def test_요약_판단_매입_판매_승인_줄에_NO_ML_BATCH_가_찍힌다() -> None:
    """🔴 **「안 돌렸다」 가 요약에서 사라지면 14일이 어디 갔는지 아무도 모른다.**

    ★ 줄마다 **그 값이 실제로 찍혔는지**를 잰다 — 이름만 있고 0 이면 빨개진다.
    """
    result = _걷기(now=_at(금요일, 16, 0), 기록=_하루기록())
    줄들 = {
        _NFC(줄.split()[0]): _NFC(줄) for 줄 in format_summary(result).splitlines() if 줄.strip()
    }

    assert "'NO_ML_BATCH': 2" in 줄들["판단"], 줄들["판단"]
    assert "'NO_ML_BATCH': 2" in 줄들["매입"], 줄들.get("매입")
    assert "'NO_ML_BATCH': 2" in 줄들["판매"], 줄들["판매"]
    # ★ 매입 승인 · 판매 승인 두 칸이 하루에 하나씩이다.
    assert "'NO_ML_BATCH': 4" in 줄들["승인"], 줄들["승인"]


# ── ⑤ 배치 축 모듈 — `is_survey` 하나만 읽는다 ────────────────────────────


def _행(day: date, *, is_open: bool, is_survey: bool | None) -> dict[str, Any]:
    return {"dt": day, "is_open": is_open, "is_survey": is_survey}


def test_배치_축은_is_survey_칸의_값을_답한다() -> None:
    """🔴 **㉤ 두 칸이 갈리는 날로 잰다.** 같은 날로 재면 어느 칸을 읽었는지 안 보인다."""
    달력 = MlBatchDays(
        read=lambda: [
            _행(토요일, is_open=True, is_survey=False),
            _행(date(2026, 1, 2), is_open=False, is_survey=True),
        ]
    )

    assert 달력.has_ml_batch(토요일) is False
    assert 달력.has_ml_batch(date(2026, 1, 2)) is True


def test_빈_칸은_거짓으로_접지_않는다() -> None:
    """🔴 NULL 을 *"배치가 없다"* 로 읽으면 그 날 판단이 조용히 빠진다."""
    달력 = MlBatchDays(read=lambda: [_행(토요일, is_open=True, is_survey=None)])

    with pytest.raises(CalendarNotCovered, match="is_survey"):
        달력.has_ml_batch(토요일)


def test_표에_없는_날_못_읽은_표_빈_표는_답하지_않는다() -> None:
    with pytest.raises(CalendarNotCovered):
        MlBatchDays(read=lambda: [_행(토요일, is_open=True, is_survey=True)]).has_ml_batch(월요일)

    def 터진다() -> list[dict[str, Any]]:
        raise ConnectionError("DB 가 죽었다")

    with pytest.raises(CalendarNotCovered):
        MlBatchDays(read=터진다).has_ml_batch(토요일)
    with pytest.raises(CalendarNotCovered):
        MlBatchDays(read=list).has_ml_batch(토요일)


def test_표를_한_번만_읽는다() -> None:
    읽음: list[int] = []

    def 읽기() -> list[dict[str, Any]]:
        읽음.append(1)
        return [_행(토요일, is_open=True, is_survey=False)]

    달력 = MlBatchDays(read=읽기)
    달력.has_ml_batch(토요일)
    달력.has_ml_batch(토요일)

    assert 읽음 == [1]


def test_조회가_is_survey_만_가져온다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **`SELECT *` 를 안 쓴다.** 안 가져오면 나중에 누가 그 칸으로 판정하지 못한다."""
    잡은질의: list[Any] = []
    monkeypatch.setattr(ml_batch_calendar, "get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr(
        ml_batch_calendar, "fetch_all", lambda query: (잡은질의.append(query), [])[1]
    )

    with pytest.raises(CalendarNotCovered):
        MlBatchDays().has_ml_batch(토요일)

    질의 = repr(잡은질의[0])
    assert "is_survey" in 질의 and "ml_calendar_days" in 질의, 질의
    for 금지 in ("is_open", "holiday_nm", "status", "has_batch", "*"):
        assert 금지 not in 질의, f"판정에 안 쓰는 칸을 가져온다: {금지} — {질의}"


def test_개장_축_모듈에_배치_칸이_안_들어갔다() -> None:
    """🔴 **`market_calendar.py` 는 「그날 시장에서 살 수 있는가」 만 답한다.**"""
    원문 = (_MASTER / "market_calendar.py").read_text(encoding="utf-8")
    나무 = ast.parse(원문)
    문자열 = [
        node.value
        for node in ast.walk(나무)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("SELECT")
    ]

    assert 문자열 == ["SELECT dt, is_open FROM {}.{} ORDER BY dt"], 문자열
    assert "has_ml_batch" not in 원문


def test_검사_안에서는_배치_축이_가짜다() -> None:
    """🔴 **격리가 섰는지 직접 잰다.** 기본값으로 걷는 검사가 실 DB 를 안 친다."""
    assert not isinstance(ml_batch_calendar.get_ml_batch_calendar(), MlBatchDays)
    assert ml_batch_calendar.get_ml_batch_calendar().has_ml_batch(토요일) is True


def test_기본값이_실제_달력_함수_자체다() -> None:
    """★ `None` 을 안 받는다 (`clock.py` · `verifier.py` 와 같은 규율)."""
    import inspect

    from app.master import backtest_runner

    for 함수 in (scheduler.wake_up, backtest_runner.walk):
        기본 = inspect.signature(함수).parameters["ml_batch"].default
        assert 기본 is ml_batch_calendar.get_ml_batch_calendar, 함수


def test_DayRunOutcome_기본값은_여전히_NOT_ATTEMPTED_다() -> None:
    """⚠️ **새 값은 배치 없는 날에만 실린다.** 기본을 바꾸면 휴장 · WAIT 가 섞인다."""
    out = DayRunOutcome(as_of=토요일, action="WAIT", reason="")

    assert out.procurement_status == "NOT_ATTEMPTED"
    assert out.sales_status == "NOT_ATTEMPTED"
