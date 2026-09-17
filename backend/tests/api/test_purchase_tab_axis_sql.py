"""매입 탭 도착일 조회에 **축을 SQL 로 건다** — `_read(..., sim_run_id=)` (2026-09-17).

🔴 **왜 생겼나.** 도착일 조회(`arrivals`)가 원장 줄이 있는 모든 날의 **모든 걷기** 시나리오를
끌어와 파이썬에서 한 축만 남기고 있었다. 실 DB 실측 (REH-0914 · 08-31) —

.. code-block:: text

    arrivals   13,220행 · 45.6MB · 1,150ms   (한 판 1,285ms 중)
    축을 걸면   164ms · 응답 본문 sha 같음    (REH 08-31 · FINAL 09-14 · V13 01-26)

★ **결과가 같아야 하는 변경이라 값으로는 못 잠근다** (규칙 8). 걸어도 안 걸어도 화면은
같으므로, 값을 대 보는 검사는 축 조건을 지워도 통과한다. 그래서 여기서 잠그는 것은 셋이다::

    ① 조회가 실제로 축을 싣고 나가나 — SQL 문면과 파라미터를 본다 · 축을 바꿔 따라오나
    ② `build` 가 받은 축을 `_read` 까지 흘리나 — 곁가지 말고 **직접** 본다
    ③ 대역이 낡으면 조용히 예시값이 나가는 함정 — 새 인자를 못 받는 스텁으로 보인다

🔴 **파이썬 축 필터(`_arrival_index`)는 그대로 남는다.** 주입 검사가 그것을 따로 잰다
(`test_purchase_tab_sim_run.py::test_도착일도_같은_축에서만_맞춘다`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.api.purchase import query as purchase_query

AS_OF = date(2026, 8, 31)
AXIS = "SIM-AXIS-SQL-TEST"
OTHER = "SIM-AXIS-SQL-OTHER"

#: 축마다 **다른 날**에 산다 — 날짜가 축을 따라 갈리는지 보려고 일부러 벌려 둔다.
AXIS_DATES = [date(2026, 8, 20), AS_OF]
OTHER_DATES = [date(2026, 8, 25)]


def _buy(purchase_date: date, sim_run_id: str) -> dict[str, Any]:
    return {
        "purchase_id": f"PUR-{sim_run_id}-{purchase_date.isoformat()}",
        "purchase_date": purchase_date,
        "payment_due_date": purchase_date,
        "settlement_status": "OPEN",
        "sim_run_id": sim_run_id,
        "item_id": "ITEM-CABBAGE",
        "grade": "특",
        "quantity_kg": Decimal(10),
        "unit_price_krw_per_kg": Decimal(900),
        "line_amount_krw": Decimal(9_000),
    }


BUYS = [_buy(d, AXIS) for d in AXIS_DATES] + [_buy(d, OTHER) for d in OTHER_DATES]


# ══════════════════════════════════════════════════════════════════════════
#  ① 조회가 실제로 축을 싣고 나가나 — `_read` 를 DB 없이 돌린다
# ══════════════════════════════════════════════════════════════════════════

class _Recorder:
    """`fetch_all` 을 대신 선다. **나간 질의의 문면과 파라미터를 모은다.**

    🔴 순서로 가르지 않고 **문면으로** 가른다 — `_read` 안에서 질의 하나가 자리를 옮겨도
    이 검사가 딴것을 재지 않게.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, query: Any, params: Any = None) -> list[dict[str, Any]]:
        text = query.as_string(None)
        self.calls.append((text, params))
        if "purchase_items" in text:
            return BUYS
        return []  # runs · decisions · items · arrivals — 내용은 관심이 아니다

    @property
    def arrivals(self) -> list[tuple[str, Any]]:
        return [(text, params) for text, params in self.calls if "'scenarios'" in text]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """`_read` 가 함수 **안에서** import 하므로 모듈 속성을 갈아 끼우면 잡힌다."""
    import app.finance.db as finance_db

    monkeypatch.setenv("DB_SCHEMA", "haetdeul")
    rec = _Recorder()
    monkeypatch.setattr(finance_db, "fetch_all", rec)
    return rec


def test_축을_주면_도착일_조회가_그_축과_그_축의_날짜로_나간다(recorder: _Recorder) -> None:
    """🔴 규칙 8 — 상수와 대 보지 않는다. **축을 바꿔** 조회가 따라 움직이는지 본다."""
    purchase_query._read(AS_OF, sim_run_id=AXIS)
    purchase_query._read(AS_OF, sim_run_id=OTHER)

    (text_a, params_a), (text_o, params_o) = recorder.arrivals
    for text in (text_a, text_o):
        assert "sim_run_id = %(sim)s" in text, "축 조건이 SQL 에 실려야 한다"
    assert (params_a["sim"], params_o["sim"]) == (AXIS, OTHER)
    #  ★ 날짜도 축을 따라 갈린다 — 다른 걷기만 산 날은 안 건다
    assert params_a["dates"] == sorted(AXIS_DATES)
    assert params_o["dates"] == sorted(OTHER_DATES)


def test_축을_안_주면_도착일_조회에_축을_안_걸고_날짜도_전부다(recorder: _Recorder) -> None:
    """🔴 `None` 경로는 그대로다. `= %(sim)s` 에 `None` 을 넣으면 0행이 된다 — 다 버린다."""
    purchase_query._read(AS_OF)

    ((text, params),) = recorder.arrivals
    assert "sim_run_id =" not in text
    assert "sim" not in params
    assert params["dates"] == sorted(AXIS_DATES + OTHER_DATES)


def test_다른_걷기의_날짜를_안_건_것은_안_읽었다가_아니다(recorder: _Recorder) -> None:
    """⚠️ 「전부 읽었나」는 **그 축의** 날짜 기준이다.

    다른 걷기만 산 날을 안 걸었다고 `arrivals_complete=False` 가 되면, 창을 안 좁힌 매입 탭의
    「확정 입고 예정」이 «안 읽었다» 로 떨어진다 (규칙 3 의 반대 방향 사고).
    """
    whole = purchase_query._read(AS_OF, sim_run_id=AXIS)
    #  ★ 대조군 — 그 축의 날을 실제로 뺀 창이면 여전히 «안 읽었다» 다
    narrowed = purchase_query._read(AS_OF, sim_run_id=AXIS, window_days=1)

    assert whole["arrivals_complete"] is True
    assert narrowed["arrivals_complete"] is False
    assert recorder.arrivals[-1][1]["dates"] == [AS_OF]


# ══════════════════════════════════════════════════════════════════════════
#  ② `build` 가 축을 `_read` 까지 흘리나 — 직접 잠근다
# ══════════════════════════════════════════════════════════════════════════

def _data() -> dict[str, Any]:
    return {
        "runs": [],
        "buys": [_buy(AS_OF, AXIS)],
        "decisions": [],
        "items": {"ITEM-CABBAGE": "배추"},
        "arrivals": [],
        "arrivals_complete": True,
    }


def test_build_가_축을_그대로_흘린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **받은 축이 `_read` 까지 가는지를 직접 본다.**

    ⚠️ 곁가지에 기대지 않는다. 축을 안 흘려도 화면 값은 같고(파이썬 필터가 남아 있다),
    주입 검사들은 `_read` 를 통째로 갈아 끼우므로 **아무것도 안 빨개진다.** 늦어지는 것만
    남는데 그건 검사가 못 본다 — 그래서 넘어간 인자를 받아 적는다.
    """
    받은_축: list[Any] = []

    def _spy(as_of: date, **kwargs: Any) -> dict[str, Any]:
        받은_축.append(kwargs.get("sim_run_id", "안 받음"))
        return _data()

    monkeypatch.setattr(purchase_query, "_read", _spy)

    for 축 in (None, AXIS, OTHER):
        purchase_query.build(AS_OF, 축)

    #  ★ 상수와 대 보는 것이 아니라 **넣은 순서 그대로 나오는지**를 본다 (규칙 8).
    assert 받은_축 == [None, AXIS, OTHER]


# ══════════════════════════════════════════════════════════════════════════
#  ③ 대역이 낡으면 **조용히** 예시값이 나간다 — 그 자리를 보인다
# ══════════════════════════════════════════════════════════════════════════

def test_새_인자를_받는_스텁이면_실제값으로_선다(monkeypatch: pytest.MonkeyPatch) -> None:
    """아래 함정 검사의 대조군 — 스텁만 바꿨을 때 결과가 갈리는 것을 보인다."""
    monkeypatch.setattr(purchase_query, "_read", lambda as_of, **_kwargs: _data())

    assert purchase_query.build(AS_OF, AXIS).source.filled is True


def test_축_인자를_못_받는_옛_스텁은_예시값으로_떨어진다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **함정을 검사로 박아 둔다** (`test_purchase_tab_window.py` 의 창 인자 판과 같다).

    `build` 는 `_read` 를 `except Exception` 으로 통째로 잡는다. 그래서 `window_days` 만 받는
    옛 스텁은 `TypeError` 가 삼켜져 **아무 소리 없이 예시값**을 낸다. 그 스텁으로 값을 대 보는
    검사는 예시값을 재면서 통과할 수 있다 — 스텁을 쓸 때는 `**kwargs` 를 받는다.
    """
    def _옛_스텁(as_of: date, *, window_days: int | None = None) -> dict[str, Any]:
        return _data()

    monkeypatch.setattr(purchase_query, "_read", _옛_스텁)

    assert purchase_query.build(AS_OF, AXIS).source.filled is False
