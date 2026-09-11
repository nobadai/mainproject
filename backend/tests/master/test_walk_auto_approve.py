"""**걷기가 승인을 부른다 — 각 판단 바로 뒤에서, 명시로 켤 때만** (2026-09-11).

```text
개장 → 입고 → 채권 → 수금 → [장부 관문]
     → 매입 판단 → **매입 승인** → 판매 판단 → **판매 승인** → 출고 → 마감
```

★★ **왜 이 판이 있나.** 206일을 끝까지 걸었는데 승인이 **한 건도 안 섰다.** 승인
  로직은 `backfill.py` 에 이미 있었고 **부르는 자리 하나**가 없었다 — 판매 판단이
  0건이던 때와 같은 모양이다. 걷기 안에서 승인이 안 서면 **다음 날이 어제 산 것을
  못 보고**, 179일을 걸어도 재고가 영영 안 쌓인다.

🔴 **기본이 꺼짐이다.** 설정에 규칙이 있다고 켜지지 않는다 — *"있으니까 한다"* 는
  암묵 스위치이고, 그러면 설정을 실험하려고 넣은 사람이 **승인까지 하게 된다.**

⚠️ **DB 를 안 탄다.** 승인 문도 조회도 전부 대역이다 — 이 판이 잠그는 것은
  **부르는 자리와 그 순서**이지, 승인이 무엇을 적는가가 아니다 (그것은
  `test_backfill.py` 가 이미 잠갔다).

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가
  안 같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import ast
import inspect
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.master import backtest_runner, scheduler
from app.master.backfill import (
    BACKFILL_BOUNDARY_AS_OF,
    BackfillOut,
    BackfillRuleMissing,
    BackfillRules,
    backfill_decisions,
)
from app.master.backtest_runner import WalkResult, format_summary, walk
from app.master.clock import SEOUL
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.scheduler import (
    DayRunOutcome,
    ScheduledAction,
    daily_request_id,
    daily_sales_request_id,
    plan_next_action,
    run_scheduled_day,
)

AS_OF = date(2026, 9, 8)
ITEMS = ("무", "배추", "양파")
실행 = "SIM-WALK-202601"

#: `backfill.BackfillOutcome` 의 여덟. 🔴 **여기서 새로 짓지 않고 그대로 베낀다** —
#: 이 목록이 저쪽과 갈리면 잠금이 옛 어휘를 재게 된다.
여덟 = (
    "RECORDED",
    "ALREADY_DECIDED",
    "NOT_APPROVABLE",
    "NO_RULE_FOR_CYCLE",
    "LABEL_NOT_OFFERED",
    "AMBIGUOUS_TYPE",
    "BLOCKED_BY_BOUNDARY",
    "FAILED",
)

_스케줄러 = Path(scheduler.__file__)
_걷기 = Path(backtest_runner.__file__)


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
    """`run_procurement` · `run_sales` 대역. **품목마다 한 줄씩 순서에 적는다.**"""

    def __init__(self, 순서: list[str], 이름: str, end_code: str) -> None:
        self.순서 = 순서
        self.이름 = 이름
        self.end_code = end_code
        self.requests: list[Any] = []

    def __call__(self, request, verifier=None, **kwargs: Any):
        self.순서.append(self.이름)
        self.requests.append(request)
        return _Out(status="RAN", end_code=self.end_code)


class _승인문:
    """`approve_fn` 대역. **부른 순서와 받은 인자를 그대로 모은다.**

    🔴 **`backfill_decisions` 를 흉내 내되 판단을 안 한다.** 어느 안을 고르는가는
      `backfill.py` 의 일이고 여기서 다시 재면 그 잠금이 두 곳에 생긴다 — 이 대역이
      재는 것은 **불렸는가 · 언제 불렸는가 · 무엇을 받았는가** 셋이다.
    """

    def __init__(
        self,
        순서: list[str],
        *,
        status: str = "RAN",
        outcomes: dict[str, int] | None = None,
        boom: Exception | None = None,
    ) -> None:
        self.순서 = 순서
        self.status = status
        self.outcomes = outcomes or {}
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> BackfillOut:
        self.순서.append("승인")
        self.calls.append(kwargs)
        if self.boom is not None:
            raise self.boom
        return BackfillOut(
            sim_run_id=kwargs["sim_run_id"],
            start=kwargs["start"],
            end=kwargs["end"],
            status=self.status,  # type: ignore[arg-type]
            runs=tuple(
                _행결과(kwargs["start"], outcome)
                for outcome, 수 in self.outcomes.items()
                for _ in range(수)
            ),
        )


def _행결과(as_of: date, outcome: str):
    from app.master.backfill import BackfilledRun

    return BackfilledRun(as_of=as_of, run_id="run-1", request_id="req-1", outcome=outcome)  # type: ignore[arg-type]


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


def _계획(as_of: date = AS_OF) -> ScheduledAction:
    return plan_next_action(
        now=datetime(as_of.year, as_of.month, as_of.day, 9, 30, tzinfo=SEOUL),
        as_of=as_of,
        calendar=_달력(),
        gate_result=_준비(as_of),
    )


def _하루(*, as_of: date = AS_OF, **over: Any) -> tuple[DayRunOutcome, list[str], _승인문]:
    """하루를 건다. **순서를 한 목록에 모아 돌려준다.**"""
    순서: list[str] = []
    문 = over.pop("문", None) or _승인문(순서)
    인자: dict[str, Any] = {
        "open_day_fn": _단계(순서, "개장", _Out("OPENED")),
        "receive_fn": _단계(순서, "입고", _Out("RECEIVED")),
        "issue_fn": _단계(순서, "채권", _Out("ISSUED")),
        "collect_fn": _단계(순서, "수금", _Out("COLLECTED")),
        "procure_fn": _판단(순서, "매입판단", "E1_DONE"),
        "sales_fn": _판단(순서, "판매판단", "SL1_PRESENTED"),
        "outbound_fn": _단계(순서, "출고", _Out("NOTHING_DUE")),
        "close_fn": _단계(순서, "마감", _Out("CLOSED")),
        "sim_run_id": 실행,
        "items": ITEMS,
        "approve_fn": 문,
    }
    인자.update(over)
    return run_scheduled_day(_계획(as_of), **인자), 순서, 문  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════
#  ① 🔴 안 주면 **이름조차 안 불린다**
# ══════════════════════════════════════════════════════════════════════


def test_auto_approve_를_안_주면_승인_함수가_이름조차_안_불린다() -> None:
    """🔴 **켜는 것은 명시로만이다.**

    ★ *"불렸는데 0건"* 과 *"안 불렸다"* 는 다르다. 앞엣것은 승인 문을 지난 것이고,
      `master_decisions` 는 append-only 라 지난 것을 되돌릴 방법이 없다.
    """
    out, 순서, 문 = _하루()

    assert 문.calls == [], "auto_approve 를 안 줬는데 승인 함수가 불렸다"
    assert "승인" not in 순서
    assert out.procurement_approval_status == "NOT_ATTEMPTED"
    assert out.sales_approval_status == "NOT_ATTEMPTED"
    assert out.procurement_approval is None
    assert out.sales_approval is None


def test_안_켠_날은_사유_줄도_안_남긴다() -> None:
    """⚠️ 매일 *"안 켰다"* 를 한 줄씩 남기면 **진짜 사유가 안 읽힌다.**"""
    out, _, _ = _하루()

    assert not [note for note in out.notes if "승인" in _NFC(note)]


@pytest.mark.parametrize(
    ("함수", "인자"),
    [
        (run_scheduled_day, "auto_approve"),
        (scheduler.wake_up, "auto_approve"),
        (walk, "auto_approve"),
    ],
)
def test_기본이_꺼짐이다(함수: Any, 인자: str) -> None:
    """🔴 **세 자리 다 거짓이다.** 한 자리라도 참이면 그 자리가 우회로가 된다."""
    기본 = inspect.signature(함수).parameters[인자].default

    assert 기본 is False, f"{함수.__name__} 의 {인자} 기본값이 {기본!r} 다"


def test_문에도_기본이_꺼짐이다() -> None:
    """🔴 **CLI 도 안 주면 안 켠다.** `store_true` 이고 기본이 거짓이다."""
    공통 = ["--sim-run-id", 실행, "--start", "2026-02-07", "--end", "2026-09-09"]
    공통 += ["--now", "2026-09-11T10:35+09:00"]

    안준것 = backtest_runner._parser().parse_args(공통)
    준것 = backtest_runner._parser().parse_args([*공통, "--auto-approve"])

    assert 안준것.auto_approve is False
    assert 준것.auto_approve is True


# ══════════════════════════════════════════════════════════════════════
#  ② 🔴 켜면 **각 판단 바로 뒤**에 선다
# ══════════════════════════════════════════════════════════════════════


def test_승인이_각_판단_바로_뒤에_선다() -> None:
    """🔴 **끝에 몰면 red 다.**

    ★★ 판매 판단이 그날의 매입 결과를 봐야 하고, 다음 날이 어제 산 것 위에 서야
      한다. 둘을 끝으로 옮기면 이 순서가 무너진다.
    """
    _, 순서, 문 = _하루(auto_approve=True)

    assert 순서 == [
        "개장",
        "입고",
        "채권",
        "수금",
        "매입판단",
        "매입판단",
        "매입판단",
        "승인",
        "판매판단",
        "판매판단",
        "판매판단",
        "승인",
        "출고",
        "마감",
    ]
    assert len(문.calls) == 2


def test_승인_둘을_끝으로_몰면_이_검사가_잡는다() -> None:
    """🟢 **위 검사가 실제로 순서를 재는지**를 같이 잠근다.

    ★ 끝에 몰린 순서를 손으로 지어 보이고, 그것이 위 검사의 기대와 **다름**을
      확인한다 — 자기 생존 검사다.
    """
    몰린것 = [
        "개장",
        "입고",
        "채권",
        "수금",
        "매입판단",
        "매입판단",
        "매입판단",
        "판매판단",
        "판매판단",
        "판매판단",
        "승인",
        "승인",
        "출고",
        "마감",
    ]
    _, 제자리, _ = _하루(auto_approve=True)

    assert 제자리 != 몰린것


def test_승인은_제_사이클이_낸_행만_본다() -> None:
    """🔴 **매입 승인이 판매 행을, 판매 승인이 매입 행을 안 훑는다.**

    ⚠️ 안 좁히면 판매 승인이 그날 매입 행을 다시 봐 `ALREADY_DECIDED` 를 품목 수만큼
      더 쌓고, 그 어휘가 뜻하던 *"사람이 이미 정했다"* 가 성적표에서 안 읽힌다.
    """
    _, _, 문 = _하루(auto_approve=True)
    매입행 = {"request_id": daily_request_id(AS_OF, "배추", sim_run_id=실행), "as_of": AS_OF}
    판매행 = {
        "request_id": daily_sales_request_id(AS_OF, "배추", sim_run_id=실행),
        "as_of": AS_OF,
    }

    매입문 = 문.calls[0]["runs_on"]
    판매문 = 문.calls[1]["runs_on"]

    # ★ `runs_on` 은 `list_runs` 를 부른다 — 그 자리에 대역을 세워 재료를 준다.
    assert _걸러진(매입문, [매입행, 판매행]) == [매입행]
    assert _걸러진(판매문, [매입행, 판매행]) == [판매행]


def _걸러진(runs_on: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`runs_on` 이 `list_runs` 자리에서 무엇을 걸러 내는지 잰다. **DB 를 안 탄다.**"""
    import app.master.scheduler as _s

    원래 = _s.list_runs
    _s.list_runs = lambda **kwargs: rows  # type: ignore[assignment]
    try:
        return list(runs_on(sim_run_id=실행, as_of=AS_OF, limit=500))
    finally:
        _s.list_runs = 원래  # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════════════
#  ③ 🔴 **설정에 규칙이 있다고 켜지지 않는다**
# ══════════════════════════════════════════════════════════════════════


def test_설정에_규칙이_있어도_자동으로_안_켜진다() -> None:
    """★★ *"있으니까 한다"* 는 **암묵 스위치**다.

    그러면 설정을 실험하려고 규칙을 넣은 사람이 **승인까지 하게 된다.**
    """
    규칙읽기 = _규칙읽기(BackfillRules())
    하루 = _하루기록()

    walk(
        sim_run_id=실행,
        start=AS_OF,
        end=AS_OF,
        now=datetime(2026, 9, 11, 10, 35, tzinfo=SEOUL),
        calendar=lambda: _달력(),
        readiness=_준비,
        run_day_fn=하루,
        rules_of=규칙읽기,
        ticks=lambda: 0.0,
    )

    assert 하루.받은것[0]["auto_approve"] is False, "규칙이 있다고 승인이 켜졌다"
    assert 규칙읽기.calls == [], "안 켠 걷기가 규칙을 물었다 — 물을 이유가 없다"


def test_규칙이_설정에_없는데_켜면_걷기_전에_막는다() -> None:
    """⚠️ **조용히 아무것도 안 하면** 사람이 *"승인이 돌았는데 0건이구나"* 로 읽는다.

    🔴 **첫날을 걷기도 전에 막는다.** 179일을 다 걷고 나서 알면 늦다.
    """
    하루 = _하루기록()

    def 규칙없음(sim_run_id: str) -> BackfillRules:
        raise BackfillRuleMissing("backfill 칸이 없다")

    with pytest.raises(ValueError, match="auto-approve"):
        walk(
            sim_run_id=실행,
            start=AS_OF,
            end=AS_OF,
            now=datetime(2026, 9, 11, 10, 35, tzinfo=SEOUL),
            calendar=lambda: _달력(),
            readiness=_준비,
            run_day_fn=하루,
            auto_approve=True,
            rules_of=규칙없음,
            ticks=lambda: 0.0,
        )

    assert 하루.받은것 == [], "막았다면서 하루를 걸었다"


def test_규칙이_있으면_켠_걷기가_그대로_걷는다() -> None:
    """🟢 **위 검사가 아무것이나 막는 것이 아님**을 같이 잠근다."""
    하루 = _하루기록()
    규칙읽기 = _규칙읽기(BackfillRules())

    walk(
        sim_run_id=실행,
        start=AS_OF,
        end=AS_OF,
        now=datetime(2026, 9, 11, 10, 35, tzinfo=SEOUL),
        calendar=lambda: _달력(),
        readiness=_준비,
        run_day_fn=하루,
        auto_approve=True,
        rules_of=규칙읽기,
        ticks=lambda: 0.0,
    )

    assert 규칙읽기.calls == [실행]
    assert 하루.받은것[0]["auto_approve"] is True


class _규칙읽기:
    def __init__(self, rules: BackfillRules) -> None:
        self.rules = rules
        self.calls: list[str] = []

    def __call__(self, sim_run_id: str) -> BackfillRules:
        self.calls.append(sim_run_id)
        return self.rules


class _하루기록:
    """`run_day_fn` 대역. **무엇을 받았는지**를 모은다."""

    def __init__(self) -> None:
        self.받은것: list[dict[str, Any]] = []

    def __call__(self, action: ScheduledAction, **kwargs: Any) -> DayRunOutcome:
        self.받은것.append(kwargs)
        return DayRunOutcome(
            as_of=action.as_of,
            action=action.action,
            reason=action.reason,
            procurement_status="RAN",
        )


# ══════════════════════════════════════════════════════════════════════
#  ④ 🔴 경계 · ⑤ 🔴 축
# ══════════════════════════════════════════════════════════════════════


def test_경계_뒤_날짜는_BLOCKED_BY_BOUNDARY_로_센다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **경계는 걷기가 다시 재지 않는다** — `backfill.py` 의 상수가 행마다 막는다.

    ★ 여기서 도는 승인은 **진짜 `backfill_decisions`** 다. 승인 문과 조회만 대역이라,
      막는 것이 걷기의 새 판단이 아니라 저쪽 가드임을 그대로 잰다.
    """
    넘은날 = BACKFILL_BOUNDARY_AS_OF.replace(day=BACKFILL_BOUNDARY_AS_OF.day + 1)
    행 = [
        {
            "as_of": 넘은날,
            "run_id": "run-1",
            "request_id": daily_request_id(넘은날, item, sim_run_id=실행),
            "cycle": "PROCUREMENT",
            "end_code": "E1_DONE",
            "response_payload": {},
        }
        for item in ITEMS
    ]
    monkeypatch.setattr(scheduler, "list_runs", lambda **kwargs: 행)
    문지기 = _문지기()
    # ⚠️ 라벨은 **설정에서** 온다 — 검사가 값을 들되 코드는 안 든다.
    설정 = {"backfill": {"procurement": {"rule": "ALWAYS_BASE", "scenario_label": "라벨"}}}

    def 승인(**kwargs: Any) -> BackfillOut:
        return backfill_decisions(
            sim_run_id=kwargs["sim_run_id"],
            start=kwargs["start"],
            end=kwargs["end"],
            runs_on=kwargs["runs_on"],
            load_config=lambda _: 설정,
            decisions_of=lambda _: [],
            decide=문지기,
        )

    out, _, _ = _하루(as_of=넘은날, auto_approve=True, 문=승인)

    assert out.procurement_approval is not None
    assert dict(out.procurement_approval.outcomes) == {"BLOCKED_BY_BOUNDARY": 3}
    assert 문지기.calls == [], "경계 밖인데 승인 문이 불렸다"
    assert out.closing_status == "CLOSED", "경계에 막혀도 하루는 끝까지 간다"


class _문지기:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, request_id: str, payload: Any) -> Any:
        self.calls.append((request_id, payload))
        raise AssertionError("불리면 안 되는 자리다")


def test_승인이_이번_실행_축으로_앉는다() -> None:
    """🔴 **번인 상수로 안 떨어진다.** 판단은 걷기 축에, 승인은 번인에 앉으면
    그 승인이 **남의 실행 장부**를 채운다."""
    _, _, 문 = _하루(auto_approve=True, sim_run_id="SIM-WALK-OTHER")

    assert [call["sim_run_id"] for call in 문.calls] == ["SIM-WALK-OTHER", "SIM-WALK-OTHER"]
    assert [call["start"] for call in 문.calls] == [AS_OF, AS_OF]
    assert [call["end"] for call in 문.calls] == [AS_OF, AS_OF]


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 🔴 승인이 터져도 **하루는 계속 간다**
# ══════════════════════════════════════════════════════════════════════


def test_승인이_터져도_하루는_계속_간다() -> None:
    """🔴 **수금 씨앗과 같은 태도다** — 그 결과가 개장을 실패시키지 않는다.

    ★ 판단도 출고도 마감도 그대로 돈다. 터진 것은 상태와 사유로만 남는다.
    """
    순서: list[str] = []
    문 = _승인문(순서, boom=RuntimeError("승인이 터졌다"))
    out, 순서2, _ = _하루(auto_approve=True, 문=문)

    assert out.procurement_approval_status == "FAILED"
    assert out.sales_approval_status == "FAILED"
    assert out.procurement_status == "RAN"
    assert out.sales_status == "RAN"
    assert out.outbound_status == "NOTHING_DUE"
    assert out.closing_status == "CLOSED"
    assert "판매판단" in 순서2 and "출고" in 순서2 and "마감" in 순서2
    assert any("RuntimeError" in note for note in out.notes)


def test_규칙이_없는_날은_NO_RULE_로_남고_NOT_ATTEMPTED_와_안_섞인다() -> None:
    """🔴 *"안 켰다"* 와 *"켰는데 규칙이 없었다"* 는 **승인 0건의 다른 이유**다."""
    순서: list[str] = []
    문 = _승인문(순서, status="NO_RULE")
    out, _, _ = _하루(auto_approve=True, 문=문)

    assert out.procurement_approval_status == "NO_RULE"
    assert out.sales_approval_status == "NO_RULE"


# ══════════════════════════════════════════════════════════════════════
#  ⑦ 🟢 어휘 여덟이 **접히지 않고** 요약에 올라온다
# ══════════════════════════════════════════════════════════════════════


def test_어휘_여덟이_요약에_접히지_않고_올라온다() -> None:
    """🔴 *"없다"* 와 *"안 했다"* 와 *"못 했다"* 를 묶으면 **왜 승인이 0건인지**를
    성적표가 못 답한다."""
    하루 = DayRunOutcome(
        as_of=AS_OF,
        action="RUN_NOW",
        reason="",
        procurement_approval_status="RAN",
        sales_approval_status="RAN",
        procurement_approval=_백필결과({이름: 1 for 이름 in 여덟[:4]}),
        sales_approval=_백필결과({이름: 2 for 이름 in 여덟[4:]}),
    )
    결과 = WalkResult(start=AS_OF, end=AS_OF, days=(하루,))

    센것 = dict(결과.approval_outcomes)
    요약 = _NFC(format_summary(결과))

    assert 센것 == {이름: 1 for 이름 in 여덟[:4]} | {이름: 2 for 이름 in 여덟[4:]}
    for 이름 in 여덟:
        assert 이름 in 요약, f"요약에 승인 어휘 '{이름}' 이 없다"
    assert _NFC("승인어휘") in 요약
    assert _NFC("승인      ") in 요약


def test_승인_단계와_어휘를_한_줄로_묶지_않는다() -> None:
    """🔴 축이 다르다 — 저쪽은 하루의 단계이고 이쪽은 실행 이력 한 행이다."""
    안켠날 = DayRunOutcome(as_of=AS_OF, action="RUN_NOW", reason="")
    켠날 = DayRunOutcome(
        as_of=AS_OF,
        action="RUN_NOW",
        reason="",
        procurement_approval_status="RAN",
        sales_approval_status="NO_RULE",
        procurement_approval=_백필결과({"RECORDED": 3}),
    )
    결과 = WalkResult(start=AS_OF, end=AS_OF, days=(안켠날, 켠날))

    assert dict(결과.approval_statuses) == {"NOT_ATTEMPTED": 2, "RAN": 1, "NO_RULE": 1}
    assert dict(결과.approval_outcomes) == {"RECORDED": 3}


def _백필결과(outcomes: dict[str, int]) -> BackfillOut:
    from app.master.backfill import BackfilledRun

    return BackfillOut(
        sim_run_id=실행,
        start=AS_OF,
        end=AS_OF,
        status="RAN",
        runs=tuple(
            BackfilledRun(as_of=AS_OF, run_id="r", request_id="q", outcome=이름)  # type: ignore[arg-type]
            for 이름, 수 in outcomes.items()
            for _ in range(수)
        ),
    )


# ══════════════════════════════════════════════════════════════════════
#  ⑧ 🔴 **새 승인 경로를 만들지 않는다**
# ══════════════════════════════════════════════════════════════════════


def test_스케줄러가_승인_문을_우회하지_않는다() -> None:
    """★★ **다른 길을 내면 그 순간 「승인」이 두 종류가 된다.**

    ★ `backfill.py` 가 자기 파일에 건 잠금과 **같은 모양**이다 — 걷기 쪽에도 같은
      잠금이 서야 우회로가 이 자리에서 안 열린다.
    """
    원문 = _스케줄러.read_text(encoding="utf-8")
    가져온것 = {
        별칭.name
        for node in ast.walk(ast.parse(원문))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for 별칭 in node.names
    }

    assert "backfill_decisions" in 가져온것, "걷기가 승인 자리를 안 들여왔다"
    for 우회 in ("record_decision", "save_decision", "apply_approval"):
        assert 우회 not in 가져온것, f"스케줄러가 승인 문을 건너뛰고 {우회} 를 직접 들여왔다"


def test_기본_승인_자리가_backfill_decisions_자체다() -> None:
    """★ 갈아 끼울 자리에 **기본값이 진짜 함수**다 — `None` 을 안 받는다."""
    기본 = inspect.signature(run_scheduled_day).parameters["approve_fn"].default

    assert 기본 is backfill_decisions, f"기본 승인 자리가 다르다: {기본}"


@pytest.mark.parametrize("어휘", [이름 for 이름 in 여덟 if 이름 != "FAILED"])
def test_걷기_코드가_승인_어휘를_제_손으로_안_짓는다(어휘: str) -> None:
    """🔴 **여덟의 주인은 `backfill.py` 하나다.** 세기만 하고 새 이름을 안 붙인다.

    ⚠️ 이름을 여기 베끼면 저쪽이 어휘를 늘리는 날 성적표만 옛 목록으로 센다.

    ⚠️ **`FAILED` 는 뺀다.** 그 한 낱말은 스케줄러가 단계마다 이미 쓰는 말이라
      (`_stage` · `ItemRunOutcome`) 원문 잠금이 승인과 무관한 자리를 잡는다 —
      승인 쪽 `FAILED` 는 `_approve` 가 `BackfillOut.status` 를 그대로 싣는 것이지
      제 손으로 지은 것이 아니고, 그 사실은 위 검사들이 이미 잰다.
    """
    for 파일 in (_스케줄러, _걷기):
        코드 = 파일.read_text(encoding="utf-8")
        assert f'"{어휘}"' not in 코드, f"{파일.name} 에 승인 어휘 '{어휘}' 가 박혀 있다"
