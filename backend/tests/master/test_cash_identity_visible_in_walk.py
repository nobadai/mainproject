"""걷기 요약이 **현금이 맞는지**를 말하는가 — 2026-09-12.

`daily_closings` 에 현금 칸은 처음부터 있었다. **걷기가 한 번도 안 봤다.**

```text
실행   Δ잔액         Σ순현금        차이            판정
V4    19,706,568    19,706,568             0     성립
V5    18,891,049    18,891,049             0     성립
V6    18,214,996    18,214,996             0     성립
V7    18,557,854    -8,564,374    27,122,228     🔴 깨짐
```

★★ V7 에서 매입 현금유출 27,122,228 원이 **흐름에는 잡히는데 잔액에서 안 빠진다.**
  그 수정은 재무 몫이다 (recognition 과 settlement 사이에 빠진 계층). 이 파일이
  잠그는 것은 **걷기가 그 불일치를 스스로 말하는가** 하나다.

🔴 **이 파일이 잡으려는 다섯.**

```text
① 항등식이 판정을 낸다            맞아도 찍는다 — 0 이라 안 보이면 아무도 안 본다
② 0 인 칸도 찍는다                그 0 이 V4~V6 세 판을 통과시킨 값이다
③ 대출 곡선은 안 섞는다            차입·상환이 들어가 축이 다르다 — 지금 0이라 안 갈릴 뿐이다
④ 깨져도 걷기를 안 멈춘다          사고 줄을 안 건드린다 — 멈추면 정본 판을 못 돌린다
⑤ 마감행이 0행이면 「없음」이다    「안 돌았다」와 「돌았는데 0이다」는 다른 사실이다
```

★ **DB 를 안 탄다.** 마감행은 손으로 세우고, 걷기가 그 행을 읽는 자리(`closings_of`)
  에 대역을 꽂는다.
"""

from __future__ import annotations

import ast
import inspect
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.master import backtest_runner
from app.master.backtest_runner import WalkResult, format_summary, walk
from app.master.clock import SEOUL
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.ledger_repository import (
    BASE_CASH_BALANCE,
    COLLECTION_CASH_IN,
    LOAN_CASH_BALANCE,
    LOGISTICS_CASH_OUT,
    NET_CASH,
    PAYROLL_INTEREST_CASH_OUT,
    PURCHASE_CASH_OUT,
    WALK_CASH_COLUMNS,
    read_walk_closings,
)
from app.master.scheduler import DayRunOutcome

첫날 = date(2026, 2, 2)

#: 이 검사가 쓰는 실행 축. 🔴 **운영값을 안 쓴다.**
실행축 = "SIM-TEST-CASH"


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


def _마감행(
    day: date,
    *,
    매입유출: int = 0,
    물류유출: int = 0,
    인건이자: int = 0,
    수금: int = 0,
    순현금: int,
    잔액: int,
    대출잔액: int | None = None,
) -> dict[str, Any]:
    """마감 한 줄. **표에 있는 그 칸 그대로다** — 여기서 이름을 새로 안 짓는다.

    ⚠️ `순현금` 과 `잔액` 을 따로 받는다. 🔴 **둘이 어긋나는 것이 이 판의 사실이고**,
      검사가 잔액을 순현금에서 만들어 내면 그 사실을 영영 못 만든다.
    """
    return {
        "close_date": day,
        PURCHASE_CASH_OUT: Decimal(매입유출),
        LOGISTICS_CASH_OUT: Decimal(물류유출),
        PAYROLL_INTEREST_CASH_OUT: Decimal(인건이자),
        COLLECTION_CASH_IN: Decimal(수금),
        NET_CASH: Decimal(순현금),
        BASE_CASH_BALANCE: Decimal(잔액),
        LOAN_CASH_BALANCE: Decimal(잔액 if 대출잔액 is None else 대출잔액),
    }


def _걷기(*행들: dict[str, Any], **kw: Any) -> WalkResult:
    return WalkResult(start=첫날, end=첫날 + timedelta(days=len(행들)), closings=행들, **kw)


def _성립하는_두날() -> tuple[dict[str, Any], dict[str, Any]]:
    """기초 100 에서 순현금대로 잔액이 움직인 이틀. **항등식이 성립한다.**"""
    return (
        _마감행(첫날, 수금=30, 순현금=30, 잔액=130),
        _마감행(첫날 + timedelta(days=1), 매입유출=50, 순현금=-50, 잔액=80),
    )


def _깨지는_두날() -> tuple[dict[str, Any], dict[str, Any]]:
    """유출이 **흐름에는 잡히는데 잔액에서 안 빠진** 이틀 — V7 의 모양이다."""
    return (
        _마감행(첫날, 수금=30, 순현금=30, 잔액=130),
        _마감행(첫날 + timedelta(days=1), 매입유출=50, 순현금=-50, 잔액=130),
    )


# ---------------------------------------------------------------------------
# ① 항등식이 판정을 낸다 — **맞아도 찍는다**
# ---------------------------------------------------------------------------


def test_성립하면_성립이라고_찍는다() -> None:
    """🔴 **0 이라 안 보이면 아무도 안 본다** — `LLM어휘` 와 같은 규율이다."""
    결과 = _걷기(*_성립하는_두날())
    요약 = _NFC(format_summary(결과))
    항등식 = 결과.cash_identity

    assert 항등식 is not None
    assert 항등식.holds, f"성립해야 하는데 차이가 {항등식.gap_krw} 다"
    assert 항등식.gap_krw == 0
    assert 항등식.mismatched_days == 0
    assert _NFC("현금항등식") in 요약, "성립인 판에서 항등식 줄이 사라졌다"
    assert _NFC("🟢 성립") in 요약, f"성립 판정이 요약에 없다: {요약}"


def test_깨지면_깨졌다고_찍는다() -> None:
    """★★ 이 줄이 없어서 27,122,228 원이 179일 동안 아무 데도 안 나왔다."""
    결과 = _걷기(*_깨지는_두날())
    요약 = _NFC(format_summary(결과))
    항등식 = 결과.cash_identity

    assert 항등식 is not None
    assert not 항등식.holds
    assert 항등식.gap_krw == 50, f"차이를 잘못 냈다: {항등식}"
    assert 항등식.mismatched_days == 1
    assert _NFC("🔴 깨짐") in 요약, f"깨짐 판정이 요약에 없다: {요약}"
    assert _NFC("차이 50") in 요약
    assert _NFC("어긋난 날 1일") in 요약


def test_기초잔액은_첫날_잔액에서_그날_순현금을_뺀_값이다() -> None:
    """★ 걷기 앞에 무엇이 있었는지를 표는 말하지 않는다 — 첫 행이 말한다."""
    항등식 = _걷기(*_성립하는_두날()).cash_identity

    assert 항등식 is not None
    assert 항등식.opening_balance_krw == 100
    assert 항등식.balance_delta_krw == -20
    assert 항등식.net_cash_krw == -20


def test_어긋난_날은_첫날을_안_센다() -> None:
    """⚠️ 첫날은 **앞 잔액이 없다.** 세면 기초를 아는 판마다 하루가 늘 어긋난다."""
    항등식 = _걷기(_마감행(첫날, 수금=30, 순현금=30, 잔액=130)).cash_identity

    assert 항등식 is not None
    assert 항등식.mismatched_days == 0
    assert 항등식.holds


def test_1원_미만은_같은_것으로_본다() -> None:
    """★ `numeric(18,6)` 이라 소수점이 남는다 — 반올림 찌꺼기를 사고로 세지 않는다."""
    둘째날 = 첫날 + timedelta(days=1)
    행들 = (
        _마감행(첫날, 수금=30, 순현금=30, 잔액=130),
        {**_마감행(둘째날, 매입유출=50, 순현금=-50, 잔액=80), NET_CASH: Decimal("-50.4")},
    )
    항등식 = _걷기(*행들).cash_identity

    assert 항등식 is not None
    assert 항등식.mismatched_days == 0
    assert 항등식.holds


def test_실측_V7_이_그대로_요약에_올라온다() -> None:
    """🔴 **그날 원장에 적혀 있던 그 숫자다.** 18,557,854 · -8,564,374 · 27,122,228.

    ★ 둘째 날 잔액은 **수금만큼만** 늘었다 — 매입유출 27,122,228 원이 순현금에는
      잡히고 잔액에서는 안 빠진 그 모양이다.
    """
    행들 = (
        _마감행(첫날, 수금=9_996_565, 순현금=9_996_565, 잔액=9_996_565),
        _마감행(
            첫날 + timedelta(days=1),
            매입유출=27_122_228,
            수금=8_561_289,
            순현금=-18_560_939,
            잔액=18_557_854,
        ),
    )
    결과 = _걷기(*행들)
    항등식 = 결과.cash_identity
    요약 = _NFC(format_summary(결과))

    assert 항등식 is not None
    assert 항등식.balance_delta_krw == 18_557_854
    assert 항등식.net_cash_krw == -8_564_374
    assert 항등식.gap_krw == 27_122_228
    assert "18,557,854" in 요약
    assert "-8,564,374" in 요약
    assert "27,122,228" in 요약
    assert _NFC("🔴 깨짐") in 요약


# ---------------------------------------------------------------------------
# ② 🔴 **0 인 칸도 찍는다** — 그 0 이 V4~V6 세 판을 통과시킨 값이다
# ---------------------------------------------------------------------------


def test_유출이_0_이어도_칸이_빠지지_않는다() -> None:
    """🔴 **빼고 찍으면 「물류가 한 푼도 안 나갔다」를 아무도 못 본다.**

    ★ V4~V6 은 물류유출 · 인건이자가 전부 0 인 채로 「성립」 판정을 받았다. 그
      0 이 안 보이면 그 세 판의 성립이 무엇을 통과시킨 것인지를 못 읽는다.
    """
    결과 = _걷기(_마감행(첫날, 수금=30, 순현금=30, 잔액=130))
    요약 = _NFC(format_summary(결과))

    for 이름 in ("매입유출", "물류유출", "인건이자"):
        assert _NFC(f"{이름}: 0") in 요약, f"0 인 칸 '{이름}' 이 요약에서 빠졌다: {요약}"


def test_현금_줄이_여섯_칸을_다_찍는다() -> None:
    """★ 다섯은 그 구간 합이고 **기말잔액만 마지막 날의 값이다.**"""
    결과 = _걷기(*_성립하는_두날())
    현금 = 결과.cash
    요약 = _NFC(format_summary(결과))

    assert 현금 is not None
    assert 현금[PURCHASE_CASH_OUT] == 50
    assert 현금[COLLECTION_CASH_IN] == 30
    assert 현금[NET_CASH] == -20
    assert 현금[BASE_CASH_BALANCE] == 80, "기말잔액은 합이 아니라 마지막 날의 잔액이다"
    for 이름 in ("매입유출", "물류유출", "인건이자", "수금", "순현금", "기말잔액"):
        assert _NFC(이름) in 요약, f"현금 줄에 '{이름}' 칸이 없다: {요약}"


# ---------------------------------------------------------------------------
# ③ 대출 곡선은 이 항등식에 안 섞는다
# ---------------------------------------------------------------------------


def test_대출_잔액이_달라도_판정이_안_바뀐다() -> None:
    """🔴 **차입·상환이 들어가 축이 다르다.**

    ⚠️ 지금 네 판 다 대출이 0 이라 안 갈린다. **안 갈린다고 한 축으로 접으면**
      차입이 한 번 서는 날 항등식이 조용히 거짓말을 한다.
    """
    둘째날 = 첫날 + timedelta(days=1)
    행들 = (
        _마감행(첫날, 수금=30, 순현금=30, 잔액=130, 대출잔액=9_999_999),
        _마감행(둘째날, 매입유출=50, 순현금=-50, 잔액=80, 대출잔액=1),
    )
    항등식 = _걷기(*행들).cash_identity

    assert 항등식 is not None
    assert 항등식.holds, f"대출 곡선이 항등식에 섞였다: {항등식}"
    assert 항등식.mismatched_days == 0
    assert 항등식.balance_delta_krw == -20


def test_요약의_두_줄이_대출_잔액을_안_찍는다() -> None:
    """★ 읽어는 두되 **이 두 줄에는 안 싣는다** — 싣는 순간 축이 둘인 줄이 된다."""
    결과 = _걷기(
        _마감행(첫날, 수금=30, 순현금=30, 잔액=130, 대출잔액=7_654_321),
    )
    현금줄 = [one for one in _NFC(format_summary(결과)).splitlines() if _NFC("현금") in one]

    assert 현금줄, "현금 줄 자체가 없다"
    assert not any("7,654,321" in one for one in 현금줄), f"대출 잔액이 찍혔다: {현금줄}"


# ---------------------------------------------------------------------------
# ④ 🔴 **깨져도 걷기를 안 멈춘다** — 이 판에서 제일 중요한 자리
# ---------------------------------------------------------------------------


def _gate(as_of: date) -> DayForecastReadiness:
    return DayForecastReadiness(
        as_of=as_of,
        readiness="ALL_READY",
        items=(ItemForecastGate(item="무", as_of=as_of, readiness="READY", grade="MEASURED"),),
    )


class _달력:
    def is_market_open(self, day: date) -> bool:
        return True


def _돈하루(as_of: date, **kw: Any) -> DayRunOutcome:
    return DayRunOutcome(
        as_of=as_of,
        action="RUN_NOW",
        reason="예측이 왔다",
        day_open_status="OPENED",
        inbound_status="NOTHING_DUE",
        collection_status="NOTHING_DUE",
        procurement_status="RAN",
        outbound_status="NOTHING_DUE",
        **kw,
    )


def _한판(
    *행들: dict[str, Any],
    closings_of: Any = None,
    days: int = 3,
) -> WalkResult:
    """대역만으로 사흘을 걷는다. **마감행은 읽는 자리에 꽂는다.**"""
    끝 = 첫날 + timedelta(days=days - 1)
    return walk(
        sim_run_id=실행축,
        start=첫날,
        end=끝,
        now=datetime(2026, 2, 2, 10, 35, tzinfo=SEOUL),
        calendar=_달력,
        readiness=_gate,
        run_day_fn=lambda action, **kw: _돈하루(action.as_of),
        terms_of=lambda _sim: None,
        closings_of=(lambda **kw: 행들) if closings_of is None else closings_of,
        ticks=lambda: 0.0,
    )


def test_항등식이_깨져도_사고가_안_는다() -> None:
    """🔴 **넣으면 `max_consecutive_failures` 에 걸려 정본 판을 못 돌린다.**

    ★★ 재무 지급 전이가 서기 전에는 **매일 깨진다.** 세고 찍고 판정은 내되,
      걷기를 멈추는 축은 안 건드린다.
    """
    결과 = _한판(*_깨지는_두날())

    assert 결과.cash_identity is not None
    assert not 결과.cash_identity.holds, "이 판은 깨진 상태를 재려는 것이다"
    assert 결과.incidents == (), f"항등식이 사고 줄을 건드렸다: {결과.incidents}"
    assert 결과.completed, "항등식이 깨졌다고 걷기가 멈췄다"
    assert 결과.stopped_reason is None
    assert len(결과.days) == 3, "깨진 판에서 걷기가 짧아졌다"


def test_걷기가_받은_자리에서_마감행을_읽는다() -> None:
    """★ 값이 있어도 **읽는 자리가 없으면** 요약은 영영 「없음」이다."""
    부른것: list[dict[str, Any]] = []

    def _읽는다(**kw: Any) -> tuple[dict[str, Any], ...]:
        부른것.append(kw)
        return _성립하는_두날()

    결과 = _한판(closings_of=_읽는다)

    assert len(부른것) == 1, f"마감행을 {len(부른것)}번 읽었다 — 걷기 한 판에 한 번이다"
    assert 부른것[0] == {"sim_run_id": 실행축, "start": 첫날, "end": 첫날 + timedelta(days=2)}
    assert 결과.cash_identity is not None
    assert 결과.cash_identity.holds


def test_마감행을_못_읽으면_못_읽었다고_찍는다() -> None:
    """🔴 **「못 읽었다」와 「0행이다」는 다른 사실이다.** 0 으로도 「없음」으로도 안 접는다.

    ⚠️ 그렇다고 걷기를 터뜨리지도 않는다 — 179일을 다 걷고 마지막 조회에서
      죽으면 **성적을 통째로 잃는다** (`_use_utf8_output` 이 막은 그 모양).
    """

    def _터진다(**kw: Any) -> tuple[dict[str, Any], ...]:
        raise RuntimeError("connection refused")

    결과 = _한판(closings_of=_터진다)
    요약 = _NFC(format_summary(결과))

    assert 결과.closings == ()
    assert 결과.closings_reason is not None
    assert "connection refused" in 결과.closings_reason
    assert 결과.incidents == (), "조회 실패가 사고 줄을 건드렸다"
    assert 결과.completed
    assert _NFC("못 읽음") in 요약, f"못 읽은 사실이 요약에 없다: {요약}"
    assert _NFC("없음") not in 요약, "못 읽은 것을 「없음」으로 접었다"


# ---------------------------------------------------------------------------
# ⑤ 마감행이 0행이면 「없음」이다 — **0 으로 안 메운다**
# ---------------------------------------------------------------------------


def test_마감행이_없으면_두_줄_다_없음이다() -> None:
    """🔴 **「마감이 안 돌았다」와 「돌았는데 0 이다」는 다른 사실이다.**"""
    결과 = _걷기()
    요약 = _NFC(format_summary(결과))

    assert 결과.cash is None, "마감행이 0행인데 현금 축이 값을 냈다"
    assert 결과.cash_identity is None
    현금줄 = [one for one in 요약.splitlines() if one.startswith(_NFC("현금"))]
    assert len(현금줄) == 2, f"현금 두 줄이 아니다: {현금줄}"
    assert all(_NFC("없음") in one for one in 현금줄), f"0 으로 메웠다: {현금줄}"
    assert _NFC("🟢 성립") not in 요약, "행이 없는데 성립이라고 찍었다"


def test_요약에_현금_두_줄이_늘_선다() -> None:
    """★★ 값이 있는데 성적표가 안 읽으면 **없는 것과 같다.**"""
    요약 = _NFC(format_summary(_걷기(*_성립하는_두날())))
    줄들 = [one for one in 요약.splitlines() if one.startswith(_NFC("현금"))]

    assert len(줄들) == 2, f"현금 줄이 둘이 아니다: {줄들}"
    assert 줄들[0].startswith(_NFC("현금        ")), f"현금 줄 이름이 다르다: {줄들[0]}"
    assert 줄들[1].startswith(_NFC("현금항등식  ")), f"항등식 줄 이름이 다르다: {줄들[1]}"


# ---------------------------------------------------------------------------
# ⑥ 칸 이름을 마스터 요약이 안 짓는다 — **0건을 세면 그것도 막는다**
# ---------------------------------------------------------------------------


_요약파일 = Path(backtest_runner.__file__)


def _식의_문자열들(source: str) -> list[str]:
    """그 파일이 **식에** 적어 둔 문자열. docstring 은 뺀다 (뜻을 적는 자리다)."""
    나무 = ast.parse(source)
    docstrings = {
        node.body[0].value
        for node in ast.walk(나무)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(나무)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    ]


def test_요약_모듈이_마감_칸_이름을_손으로_안_적는다() -> None:
    """🔴 **칸 이름의 주인은 `ledger_repository` 하나다.**

    손으로 적으면 표가 바뀌는 날 요약만 옛 이름을 말하고, 그때 나는 것은 오류가
    아니라 **조용한 0** 이다.

    ★ **자기 생존 검사를 같이 둔다** — 스캐너가 문자열을 실제로 찾는지부터 본다.
    """
    문자열 = _식의_문자열들(_요약파일.read_text(encoding="utf-8"))

    assert 문자열, "스캐너가 식의 문자열을 한 개도 못 찾았다 — 아래 단언은 공짜 초록이다"
    assert any(_NFC("현금항등식") in one for one in 문자열), (
        "스캐너가 요약의 현금항등식 줄을 못 찾았다 — 스캐너가 망가졌거나 줄이 사라졌다"
    )

    지어낸것 = sorted({one for one in 문자열 if one.strip() in WALK_CASH_COLUMNS})
    assert not 지어낸것, (
        f"요약 모듈이 마감 칸 이름을 직접 적는다: {지어낸것} — 주인은 ledger_repository 하나다"
    )


def test_요약_모듈이_칸_이름을_주인에게서_들여온다() -> None:
    """★ 안 들여오면 위 검사는 *"안 적었다"* 만 말하고 줄이 사라져도 초록이 난다."""
    들여온것 = {
        alias.asname or alias.name
        for node in ast.walk(ast.parse(_요약파일.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "app.master.ledger_repository"
        for alias in node.names
    }

    assert {"BASE_CASH_BALANCE", "NET_CASH", "read_walk_closings"} <= 들여온것, (
        f"요약이 칸 이름의 주인을 안 본다: {sorted(들여온것)}"
    )


def test_요약_모듈이_대출_칸을_아예_안_들여온다() -> None:
    """🔴 **안 갈린다고 한 축으로 접지 않는다** — 들여올 이유가 없으면 안 들여온다."""
    들여온것 = {
        alias.asname or alias.name
        for node in ast.walk(ast.parse(_요약파일.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "app.master.ledger_repository"
        for alias in node.names
    }

    assert "LOAN_CASH_BALANCE" not in 들여온것, (
        "요약이 대출 잔액 칸을 들여왔다 — 이 항등식은 무차입 축 하나다"
    )


def test_걷기의_기본_읽는_자리가_마감행_조회다() -> None:
    """🔴 **대역이 아니라 진짜 조회가 기본이다.**

    ★ 검사는 전부 대역을 꽂고 돈다 — 그래서 **운영이 무엇을 부르는지**는 여기서만
      잠긴다. 기본값이 빈 함수로 바뀌면 요약은 영영 「없음」이고 아무도 안 터진다.
    """
    기본값 = inspect.signature(walk).parameters["closings_of"].default

    assert 기본값 is read_walk_closings, f"걷기가 다른 자리에서 읽는다: {기본값}"


def test_읽는_칸_목록이_여섯을_다_든다() -> None:
    """★ 이 파일이 세우는 행과 걷기가 읽는 칸이 같은지를 한 줄로 붙잡는다."""
    assert set(WALK_CASH_COLUMNS) >= {
        PURCHASE_CASH_OUT,
        LOGISTICS_CASH_OUT,
        PAYROLL_INTEREST_CASH_OUT,
        COLLECTION_CASH_IN,
        NET_CASH,
        BASE_CASH_BALANCE,
    }
    assert LOAN_CASH_BALANCE in WALK_CASH_COLUMNS, (
        "대출 칸을 읽지도 않으면 「안 섞는다」를 잴 수가 없다"
    )
