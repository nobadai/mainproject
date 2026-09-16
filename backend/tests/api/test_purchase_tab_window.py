"""매입 탭 조회의 **날짜 창** — `window_days`.

🔴 **이 인자는 매입 탭이 아니라 대시보드를 위한 자리다** (2026-09-16). 대시보드가
`purchase_q.build()` 를 통째로 재사용하는데, 도착일을 맞추려고 **다시 훑는** 실행 조회가
달력 전체를 걸어 우리 머신에서 `829.6ms` · 7,700행이었다 (실 DB 실측).

★★ **그런데 대시보드는 그 결과를 안 읽는다** — `pu.plans` · `pu.source` 만 읽고, 그 조회가
먹이는 곳은 `_committed` 하나다. 그래서 넘길 값이 「12일」이 아니라 **`0`** 이고, 이 검사
묶음이 잠그는 것은 *"0 이면 아예 안 돈다"* 와 *"좁힌 사실을 화면이 말한다"* 둘이다.

🔴 **값을 대 보지 않는다** (규칙 8). `assert 행수 == 7` 같은 단언은 코드가 같은 상수를 들고
있어도 통과한다. 여기서는 **창을 실제로 바꿔** 관측이 따라 움직이는지를 본다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.api.purchase import query as purchase_query

AS_OF = date(2026, 1, 22)
AXIS = "SIM-WINDOW-TEST"

#: 매입일 넷 — `AS_OF` 로부터 0 · 1 · 5 · 20 일 전. 창 크기에 따라 갈라지라고 벌려 둔다.
BUY_DATES = [date(2026, 1, 2), date(2026, 1, 17), date(2026, 1, 21), AS_OF]


def _buy(purchase_date: date, amount: int) -> dict[str, Any]:
    return {
        "purchase_id": f"PUR-{purchase_date.isoformat()}",
        "purchase_date": purchase_date,
        "payment_due_date": purchase_date,
        "settlement_status": "PENDING",
        "sim_run_id": AXIS,
        "item_id": "ITEM-CABBAGE",
        "grade": "특",
        "quantity_kg": Decimal(1000),
        "unit_price_krw_per_kg": Decimal(800),
        "line_amount_krw": Decimal(amount),
    }


BUYS = [_buy(d, 800_000) for d in BUY_DATES]


# ══════════════════════════════════════════════════════════════════════════
#  ① 창이 조회를 실제로 움직이나 — `_read` 를 DB 없이 돌린다
# ══════════════════════════════════════════════════════════════════════════

class _Recorder:
    """`fetch_all` 을 대신 선다. **나간 질의의 파라미터를 그대로 모은다.**

    🔴 순서로 가르지 않고 **파라미터 칸으로** 가른다 — 도착일 조회만 `dates` 를 싣는다.
    순서로 가르면 `_read` 안에서 질의 하나만 자리를 옮겨도 검사가 조용히 딴것을 잰다.
    """

    def __init__(self) -> None:
        self.params: list[Any] = []

    def __call__(self, query: Any, params: Any = None) -> list[dict[str, Any]]:
        self.params.append(params)
        if params and "dates" in params:
            return []  # 내용은 이 검사의 관심이 아니다. **나갔는지**가 관심이다
        if params and "as_of" in params:
            seen = sum(1 for p in self.params if p and "as_of" in p)
            return [] if seen == 1 else BUYS  # 첫째가 runs · 둘째가 buys
        return []  # decisions · items

    @property
    def arrival_calls(self) -> list[Any]:
        return [p for p in self.params if p and "dates" in p]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """`_read` 가 함수 **안에서** import 하므로 모듈 속성을 갈아 끼우면 잡힌다."""
    import app.finance.db as finance_db

    monkeypatch.setenv("DB_SCHEMA", "haetdeul")
    rec = _Recorder()
    monkeypatch.setattr(finance_db, "fetch_all", rec)
    return rec


def test_창을_안_주면_매입일_전부를_걸고_전부_읽었다고_답한다(recorder: _Recorder) -> None:
    data = purchase_query._read(AS_OF)

    assert len(recorder.arrival_calls) == 1, "도착일 조회가 나가야 한다"
    assert recorder.arrival_calls[0]["dates"] == sorted(BUY_DATES)
    assert data["arrivals_complete"] is True


def test_창이_0이면_도착일_조회가_아예_안_나간다(recorder: _Recorder) -> None:
    """🔴 마스터에게 「0 이어도 됩니다」로 답했다 — 그게 실제로 돼야 한다."""
    data = purchase_query._read(AS_OF, window_days=0)

    assert recorder.arrival_calls == [], "창이 0이면 질의가 나가면 안 된다"
    assert data["arrivals"] == []
    assert data["arrivals_complete"] is False


def test_창을_좁히면_거는_날짜가_따라_줄어든다(recorder: _Recorder) -> None:
    """🔴 규칙 8 — 상수와 대 보지 않는다. **입력을 바꿔** 관측이 따라 움직이는지 본다."""
    wide = purchase_query._read(AS_OF)
    wide_dates = recorder.arrival_calls[-1]["dates"]

    narrow = purchase_query._read(AS_OF, window_days=2)
    narrow_dates = recorder.arrival_calls[-1]["dates"]

    #  ★ 좁힌 쪽이 **진부분집합**이어야 한다. 「같은 수가 나왔다」로는 창이 도는지 모른다.
    assert set(narrow_dates) < set(wide_dates)
    assert all(0 <= (AS_OF - d).days < 2 for d in narrow_dates)
    assert wide["arrivals_complete"] is True
    assert narrow["arrivals_complete"] is False


def test_창이_매입일을_다_덮으면_전부_읽었다고_답한다(recorder: _Recorder) -> None:
    """창을 줬다고 무조건 「안 읽었다」가 아니다 — **실제로 뺀 날이 있을 때만**이다."""
    span = (AS_OF - min(BUY_DATES)).days + 1

    data = purchase_query._read(AS_OF, window_days=span)

    assert recorder.arrival_calls[-1]["dates"] == sorted(BUY_DATES)
    assert data["arrivals_complete"] is True


# ══════════════════════════════════════════════════════════════════════════
#  ② 화면이 「0」과 「안 읽었다」를 가르나 — 규칙 3
# ══════════════════════════════════════════════════════════════════════════

_ARRIVALS = [{
    "as_of": AS_OF,
    "item": "배추",
    "sim_run_id": AXIS,
    "scenarios": [{
        "label": "보수",
        "total_amount_krw": 800_000,
        "split_plan": [{"buy_date": AS_OF.isoformat(), "qty_kg": 1000,
                        "expected_arrival_date": "2026-01-24"}],
    }],
}]


def _data(*, arrivals: list[dict[str, Any]], complete: bool | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "runs": [],
        "buys": [_buy(AS_OF, 800_000)],
        "decisions": [],
        "items": {"ITEM-CABBAGE": "배추"},
        "arrivals": arrivals,
    }
    if complete is not None:
        data["arrivals_complete"] = complete
    return data


@pytest.fixture
def inject(monkeypatch: pytest.MonkeyPatch):
    def _inject(data: dict[str, Any]):
        monkeypatch.setattr(purchase_query, "_read", lambda as_of, **_kwargs: data)

    return _inject


def _stat(tab: Any, label: str) -> Any:
    return next(s for s in tab.stats if s.label == label)


def test_창이_온전하면_입고예정은_숫자다(inject) -> None:
    inject(_data(arrivals=_ARRIVALS, complete=True))

    tab = purchase_query.build(AS_OF, AXIS)

    stat = _stat(tab, "확정 입고 예정")
    assert stat.raw == pytest.approx(1000.0)
    assert stat.unit == "kg"


def test_창을_좁히면_입고예정이_0이_아니라_미결로_나간다(inject) -> None:
    """🔴 규칙 3. 도착일을 안 읽어 합계가 0이 되는데, 그건 «확정된 0» 이 아니다."""
    inject(_data(arrivals=[], complete=False))

    tab = purchase_query.build(AS_OF, AXIS, window_days=0)

    stat = _stat(tab, "확정 입고 예정")
    assert stat.raw is None, "미결은 raw 가 None 이다 — 0 이 아니다"
    assert stat.value != "0"
    assert "안 읽었" in (stat.detail or "")


def test_창을_좁혀도_이번주_확정_매입액은_그대로_숫자다(inject) -> None:
    """★ 창은 **도착일 하나**에만 닿는다. 매입액은 원장에서 나오므로 미결이 아니다."""
    inject(_data(arrivals=[], complete=False))

    tab = purchase_query.build(AS_OF, AXIS, window_days=0)

    assert _stat(tab, "이번 주 확정 매입액").raw == pytest.approx(800_000)


def test_안내문이_못_맞췄다와_안_읽었다를_가른다(inject) -> None:
    """★ E3-5 와 같은 판단 — 「못 읽었다」와 「없다」는 다른 문장이다."""
    inject(_data(arrivals=[], complete=False))
    narrowed = purchase_query.build(AS_OF, AXIS, window_days=0).committed_note.text

    inject(_data(arrivals=[], complete=True))
    unmatched = purchase_query.build(AS_OF, AXIS).committed_note.text

    assert "안 읽었습니다" in narrowed
    assert "못 맞춘" not in narrowed
    assert "못 맞춘" in unmatched


def test_칸이_없는_옛_모양은_전부_읽은_것으로_읽는다(inject) -> None:
    """검사 주입은 자기가 준 `arrivals` 가 전부인 세상이라 «온전» 이 맞다."""
    inject(_data(arrivals=_ARRIVALS, complete=None))

    assert _stat(purchase_query.build(AS_OF, AXIS), "확정 입고 예정").raw is not None


# ══════════════════════════════════════════════════════════════════════════
#  ③ 스텁이 낡으면 **조용히** 예시값이 나간다 — 그 자리를 지킨다
# ══════════════════════════════════════════════════════════════════════════

def test_새_시그니처_스텁이면_실제값으로_선다(inject) -> None:
    inject(_data(arrivals=_ARRIVALS, complete=True))

    assert purchase_query.build(AS_OF, AXIS).source.filled is True


def test_build_가_창을_그대로_흘린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **받은 값이 `_read` 까지 가는지를 직접 잠근다.**

    ⚠️ 이 검사가 없으면 `build` 가 창을 안 흘려도 위 규칙 3 검사들이 **그대로 통과한다** —
    그쪽은 `arrivals_complete` 를 주입으로 넣기 때문이다. 변이로 재 보니 실제로 그랬고
    (`_read(as_of)` 로 되돌려도 아래 「덫」 하나만 빨개졌다), 곁가지 하나에 기대는 것은
    잠근 것이 아니다.
    """
    받은_창: list[Any] = []

    def _spy(as_of: date, **kwargs: Any) -> dict[str, Any]:
        받은_창.append(kwargs.get("window_days", "안 받음"))
        return _data(arrivals=[], complete=True)

    monkeypatch.setattr(purchase_query, "_read", _spy)

    for 창 in (None, 0, 7):
        purchase_query.build(AS_OF, AXIS, window_days=창)

    #  ★ 상수와 대 보는 것이 아니라 **넣은 순서 그대로 나오는지**를 본다 (규칙 8).
    assert 받은_창 == [None, 0, 7]


def test_창_인자를_못_받는_스텁은_예시값으로_떨어진다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **함정을 검사로 박아 둔다.**

    `build` 는 `_read` 를 `except Exception` 으로 **통째로** 잡는다 — 화면이 어떤 일에도
    떠야 하기 때문이고, 그건 의도된 설계다. 대가는 시그니처가 안 맞을 때 `TypeError` 도
    삼켜져 **아무 소리 없이 예시값이 나가는 것**이다.

    ⚠️ 이 검사는 그 동작을 «좋다» 고 말하는 게 아니라, 스텁을 넓히지 않은 채 인자를
    늘리면 검사 서른한 개가 조용히 예시값을 재게 된다는 것을 **보이는** 자리다.
    """
    monkeypatch.setattr(
        purchase_query, "_read",
        lambda as_of: _data(arrivals=_ARRIVALS, complete=True),  # 창 인자를 안 받는다
    )

    assert purchase_query.build(AS_OF, AXIS).source.filled is False
