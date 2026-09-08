"""**예측 게이트는 `load_forecast` 의 등급과 1:1 로 붙어 있다.**

🔴 게이트가 *"왔다"* 고 했는데 `load_forecast` 가 `MISSING` 을 내면 최악이다 —
스케줄러는 돌리고 매입은 `RUNTIME_NOT_READY` 를 낸다. 그래서 게이트는 **새 쿼리를
쓰지 않고 같은 함수를 부른다.** 이 파일은 그 대응이 실제로 1:1 인지 잰다.

★ **DB 를 안 탄다.** `load` 자리에 대역을 넣어 등급을 바꿔 가며 본다
  (`test_forecast_must_be_todays_batch.py` 와 같은 방식).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import forecast_gate as gate_mod
from app.master.forecast_gate import (
    check_forecast_gate,
    day_forecast_readiness,
)
from app.master.inputs import SourcedInput

AS_OF = date(2026, 9, 8)


def _measured(item: str) -> SourcedInput:
    """그날 배치가 있는 모양. `load_forecast` 가 실제로 내는 값 그대로다."""
    return SourcedInput(
        key="forecast",
        payload={"item": item, "current_price": 645},
        grade="MEASURED",
        source=f"v_ml_price_forecast(as_of={AS_OF}, AUC)",
    )


def _missing(item: str) -> SourcedInput:
    return SourcedInput(
        key="forecast",
        payload=None,
        grade="MISSING",
        source="-",
        note=f"{AS_OF} 당일 예측 배치가 없다",
    )


def _by_item(table: dict[str, SourcedInput]):
    """품목마다 다른 답을 주는 대역."""

    def load(item: str, as_of: date) -> SourcedInput:
        return table[item]

    return load


# ── 등급 → 값 (1:1) ─────────────────────────────────────────────────────


def test_MEASURED_면_READY_다():
    """정상 경로부터 본다 — 없으면 아래 검사는 '전부 막기' 로도 통과한다."""
    got = check_forecast_gate("배추", AS_OF, load=lambda i, d: _measured(i))

    assert got.readiness == "READY"
    assert got.ready is True
    assert got.grade == "MEASURED"


def test_MISSING_이면_NOT_YET_이다():
    got = check_forecast_gate("배추", AS_OF, load=lambda i, d: _missing(i))

    assert got.readiness == "NOT_YET"
    assert got.grade == "MISSING"
    assert "당일 예측 배치가 없다" in got.reason, "왜 안 왔는지가 사라졌다"


def test_예외면_UNREADABLE_이다():
    def 터진다(item: str, as_of: date) -> SourcedInput:
        raise RuntimeError("connection refused")

    got = check_forecast_gate("배추", AS_OF, load=터진다)

    assert got.readiness == "UNREADABLE"
    assert got.grade is None, "못 물었는데 등급이 붙었다"
    assert "RuntimeError" in got.reason


def test_UNREADABLE_은_NOT_YET_으로_안_접힌다():
    """🔴 **접으면 DB 가 죽은 날 스케줄러가 영원히 재시도하고 아무 데도 안 남는다.**

    ```text
    NOT_YET     확인했고 아직 안 왔다      → 재시도할 자리
    UNREADABLE  못 물었다                  → 재시도로 안 풀린다
    ```
    """

    def 터진다(item: str, as_of: date) -> SourcedInput:
        raise ConnectionError("ML DB down")

    got = check_forecast_gate("배추", AS_OF, load=터진다)

    assert got.readiness != "NOT_YET", "못 물어본 것을 '아직 안 왔다' 로 접었다"
    assert got.readiness == "UNREADABLE"


def test_MOCK_은_READY_가_아니다():
    """⚠️ `MEASURED` 만 `READY` 다.

    지금 `load_forecast` 는 `MOCK` 을 안 내지만, 새 다리가 생기는 날 그것이
    *"예측이 왔다"* 로 읽히면 mock 으로 매입안이 선다 — `inputs.py` 가 mock 다리를
    걷은 이유 그대로다.
    """
    mocked = SourcedInput(key="forecast", payload={"x": 1}, grade="MOCK", source="mocks/")
    got = check_forecast_gate("배추", AS_OF, load=lambda i, d: mocked)

    assert got.readiness != "READY"
    assert got.grade == "MOCK", "접힌 뒤에도 실제 등급은 남아야 한다"


# ── 게이트가 자기 쿼리를 쓰지 않는다 ─────────────────────────────────────


def test_게이트는_load_forecast_를_그대로_부른다(monkeypatch):
    """🔴 **같은 함수를 불러야 둘이 어긋날 수 없다.**

    게이트가 자기 조회를 짜면 *"게이트는 통과했는데 적재는 MISSING"* 이 생긴다.
    여기서는 `inputs.load_forecast` 를 가로채고, 기본 경로가 **그것을** 부르는지 본다.
    """
    불린: list[tuple[str, date]] = []

    def 감시(item: str, as_of: date) -> SourcedInput:
        불린.append((item, as_of))
        return _measured(item)

    monkeypatch.setattr(gate_mod, "load_forecast", 감시)
    # ★ 기본 인자는 import 시점에 굳으므로 기본값도 같이 갈아 끼운다.
    monkeypatch.setattr(
        check_forecast_gate, "__kwdefaults__", {**check_forecast_gate.__kwdefaults__, "load": 감시}
    )

    got = check_forecast_gate("배추", AS_OF)

    assert 불린 == [("배추", AS_OF)], "load_forecast 를 그 인자 그대로 안 불렀다"
    assert got.readiness == "READY"


def test_게이트가_DB_를_직접_안_짚는다():
    """★ 소스에 조회가 없다 — 새 쿼리를 짜는 순간 여기서 운다."""
    from pathlib import Path

    source = Path(gate_mod.__file__).read_text(encoding="utf-8")
    코드 = "\n".join(
        줄 for 줄 in source.splitlines() if not 줄.lstrip().startswith(("#", "*", "```"))
    )

    for 금지 in ("fetch_one", "fetch_all", "SELECT", "v_ml_price_forecast"):
        assert 금지 not in 코드, f"게이트가 자기 조회를 짰다: {금지}"


# ── 날 단위로 접기 ───────────────────────────────────────────────────────


def test_전부_오면_ALL_READY():
    got = day_forecast_readiness(("배추", "무", "양파"), AS_OF, load=lambda i, d: _measured(i))

    assert got.readiness == "ALL_READY"
    assert got.ready_items == ("배추", "무", "양파")
    assert got.not_yet_items == ()


def test_하나도_안_오면_NONE_READY():
    got = day_forecast_readiness(("배추", "무", "양파"), AS_OF, load=lambda i, d: _missing(i))

    assert got.readiness == "NONE_READY"
    assert got.ready_items == ()
    assert got.not_yet_items == ("배추", "무", "양파")


def test_일부만_오면_SOME_READY_이고_어느_품목인지_담는다():
    """🔴 **접으면 *"배추만 왔는데 전부 왔다"* 가 된다.**

    `SOME_READY` 라는 값만으로는 다음 걸음을 못 정한다 — 어느 품목을 더 기다리는지가
    없으면 스케줄러는 셋을 다 다시 물어야 한다.
    """
    got = day_forecast_readiness(
        ("배추", "무", "양파"),
        AS_OF,
        load=_by_item({"배추": _measured("배추"), "무": _missing("무"), "양파": _missing("양파")}),
    )

    assert got.readiness == "SOME_READY"
    assert got.ready_items == ("배추",), "온 품목이 어느 것인지 안 남았다"
    assert got.not_yet_items == ("무", "양파"), "안 온 품목이 어느 것인지 안 남았다"
    assert len(got.items) == 3, "품목별 내역이 통째로 사라졌다"


def test_SOME_READY_가_ALL_READY_로_안_접힌다():
    """하나만 온 날을 *"전부 왔다"* 로 읽으면 예측 없는 품목으로 매입안이 선다."""
    got = day_forecast_readiness(
        ("배추", "무"),
        AS_OF,
        load=_by_item({"배추": _measured("배추"), "무": _missing("무")}),
    )

    assert got.readiness != "ALL_READY"
    assert got.readiness != "NONE_READY"
    assert got.readiness == "SOME_READY"


def test_하나라도_못_물으면_UNREADABLE_이_가장_세다():
    """🔴 나머지가 다 와도 `ALL_READY` 가 아니다 — 전부를 물어보지 못했다."""

    def load(item: str, as_of: date) -> SourcedInput:
        if item == "양파":
            raise ConnectionError("ML DB down")
        return _measured(item)

    got = day_forecast_readiness(("배추", "무", "양파"), AS_OF, load=load)

    assert got.readiness == "UNREADABLE"
    assert got.unreadable_items == ("양파",)
    assert got.ready_items == ("배추", "무")
    assert got.unreadable_items != got.not_yet_items


def test_품목이_비면_터진다():
    """⚠️ 빈 목록을 `NONE_READY` 로 접으면 오지 않을 예측을 영원히 기다린다."""
    with pytest.raises(ValueError):
        day_forecast_readiness((), AS_OF, load=lambda i, d: _measured(i))


def test_as_of_를_안_주면_터진다():
    """🔴 **게이트는 시계를 안 읽는다.** 오늘이 며칠인지는 `clock` 하나만 안다."""
    with pytest.raises(ValueError):
        day_forecast_readiness(("배추",), load=lambda i, d: _measured(i))


def test_기본_품목은_계약의_세_품목이다():
    """★ 품목 목록의 주인은 `app.contracts.core.ITEMS` 다. 여기서 다시 적지 않는다."""
    from app.contracts.core import ITEMS

    got = day_forecast_readiness(as_of=AS_OF, load=lambda i, d: _measured(i))

    assert tuple(gate.item for gate in got.items) == ITEMS


# ── 값이 세 개 그대로다 ──────────────────────────────────────────────────


def test_세_값이_서로_다른_값이다():
    """세 답이 실제로 갈라지는지 한 자리에서 본다."""
    답: dict[str, Any] = {}
    답["READY"] = check_forecast_gate("배추", AS_OF, load=lambda i, d: _measured(i)).readiness
    답["NOT_YET"] = check_forecast_gate("배추", AS_OF, load=lambda i, d: _missing(i)).readiness

    def 터진다(item: str, as_of: date) -> SourcedInput:
        raise RuntimeError("x")

    답["UNREADABLE"] = check_forecast_gate("배추", AS_OF, load=터진다).readiness

    assert 답 == {"READY": "READY", "NOT_YET": "NOT_YET", "UNREADABLE": "UNREADABLE"}
    assert len(set(답.values())) == 3, "셋 중 둘이 같은 값으로 접혔다"
