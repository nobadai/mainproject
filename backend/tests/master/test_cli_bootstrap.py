"""**CLI 경로에서도 등록소가 차는가** (2026-09-09).

🔴 **안 찼다.** 범위 러너를 실제로 걸었더니 5일이 5일 다 사고였다.

```text
python -m app.master.backtest_runner --start 2026-01-05 --end 2026-01-09 \
    --now 2026-09-09T10:00+09:00

판단   {'RUN_NOW': 5}
사고   5건
  2026-01-05  판단 단계를 안 탔다 (개장: NOT_OPENED · 입고: NOT_ATTEMPTED · 수금: NOT_ATTEMPTED)
              하루 넘김 미등록: finance, logistics
멈춤   5일 연속이라 상한에서 멈춤
```

★ **러너가 틀린 것이 아니었다.** 미등록을 이름까지 정확히 말했다. 등록 줄이
  `app/main.py` 모듈 수준에만 있었고 CLI 는 그 모듈을 안 거쳤다.

⚠️ **기존 등록 검사들로는 이 자리를 못 잰다.** `test_transition_registration.py` ·
  `test_inbound_registration.py` · `test_collection_registration.py` 는 전부
  `import app.main` 을 전제로 깔고 잰다 — 그러면 **앱 진입점만** 재는 것이고,
  등록소가 CLI 에서 비는 것은 그 검사들이 전부 초록인 채로 일어난다.

★ **여기서는 `app.main` 을 안 깔고 잰다.** 비운 등록소에 `wire_registries()` 만
  불러 무엇이 차는지 본다 — 그것이 CLI 가 보는 세상 그대로다.

🔴 **자기 생존 검사를 같이 둔다.** 등록소를 세는 함수가 망가져 0 을 세면 위 검사는
  *"등록됐다"* 가 아니라 *"아무것도 안 봤다"* 를 초록으로 보고한다. 안 부른 상태에서
  같은 함수가 **빨간불을 내는지**부터 잰다.
"""

from __future__ import annotations

import importlib
import io
import sys
from collections.abc import Iterator
from datetime import date

import pytest

from app.master import (
    backtest_runner,
    cancellation,
    collection,
    day_open,
    inbound,
    transition,
    wiring,
)
from app.master.backtest_runner import WalkResult
from app.master.bootstrap import wire_registries

# ── 등록소 여섯을 한 함수로 읽는다 ──────────────────────────────────────
#
# ★ **읽는 자리를 하나로 둔다.** 검사마다 따로 읽으면 한 곳만 고쳐지고, 그때
#   "무엇을 세고 있었나" 가 검사마다 달라진다.


def _등록_현황() -> dict[str, tuple[str, ...]]:
    """지금 등록된 이름들. **판단하지 않는다 — 세기만 한다.**"""
    return {
        "agents": wiring.registry().registered,
        "transition": tuple(sorted(transition.registered())),
        "day_open": tuple(sorted(day_open.registered())),
        "cancellation": tuple(sorted(cancellation.registered_cancellations())),
        "inbound": tuple(sorted(inbound.registered())),
        "collection": tuple(sorted(collection.registered())),
    }


def _빈_등록소(현황: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """아직 아무도 안 든 등록소 이름."""
    return tuple(name for name, entries in 현황.items() if not entries)


def _모두_비운다() -> None:
    """여섯을 다 비운다. **CLI 프로세스가 시작할 때의 상태다.**"""
    wiring.reset()
    transition.reset()
    day_open.reset()
    cancellation.reset()
    inbound.reset()
    collection.reset()


@pytest.fixture(autouse=True)
def _등록소를_되돌린다() -> Iterator[None]:
    """이 파일이 등록소를 비우고 채운다 — **밖으로 새면 안 된다.**

    🔴 **루트 `tests/conftest.py` 는 셋만 되돌린다** (`wiring` · `transition` ·
       `day_open`). 나머지 셋(`cancellation` · `inbound` · `collection`)은 아무도
       안 되돌리고, 실제로 그 자리에서 샌 적이 있다
       (`tests/logistics/test_logistics_day_open.py` 의 `실제_배선` 주석).

    ★ **`reset()` 만으로는 못 되돌린다** (`wiring.py` 의 2026-09-03 실측). 뜬 것을
      다시 등록해야 하고, 그래서 여기서는 여섯을 다 떠 놓고 나간다.
    """
    저장 = {
        "agents": wiring.snapshot(),
        "transition": dict(transition.registered()),
        "day_open": dict(day_open.registered()),
        "cancellation": dict(cancellation.registered_cancellations()),
        "inbound": dict(inbound.registered()),
        "collection": dict(collection.registered()),
    }
    try:
        yield
    finally:
        wiring.restore(저장["agents"])
        transition.reset()
        for part, impl in 저장["transition"].items():
            transition.register_transition(part, impl)
        day_open.reset()
        for part, impl in 저장["day_open"].items():
            day_open.register_day_opening(part, impl)
        cancellation.reset()
        for part, impl in 저장["cancellation"].items():
            cancellation.register_cancellation(part, impl)
        inbound.reset()
        for part, impl in 저장["inbound"].items():
            inbound.register_inbound(part, impl)
        collection.reset()
        for part, impl in 저장["collection"].items():
            collection.register_collection(part, impl)


# ── ① 자기 생존 — 안 부르면 빨간불이어야 한다 ───────────────────────────


def test_안_부르면_여섯이_다_비어_있다():
    """🔴 **아래 검사가 공짜 초록이 아님을 여기서 못 박는다.**

    `_등록_현황()` 이 망가져 늘 채워진 것처럼 답하면 ②는 조립을 지우고도 통과한다.
    비운 채로 세었을 때 **여섯이 다 빈 것으로 보이는지**를 먼저 잰다.

    ⚠️ 이것이 `#442` 이전 CLI 프로세스의 실제 상태다 — 지어낸 상황이 아니다.
    """
    _모두_비운다()

    assert _빈_등록소(_등록_현황()) == (
        "agents",
        "transition",
        "day_open",
        "cancellation",
        "inbound",
        "collection",
    )


# ── ② CLI 경로가 등록소를 채운다 ────────────────────────────────────────


def test_조립_뿌리를_부르면_여섯이_다_찬다():
    """🔴 **`app.main` 을 안 깔고 잰다.** 그것이 CLI 가 보는 세상이다."""
    _모두_비운다()

    wire_registries()

    assert _빈_등록소(_등록_현황()) == ()


def test_하루_넘김에_재무와_물류가_다_있다():
    """🔴 **실제로 걷기를 세운 것이 이 둘이다.**

    5일 전부의 사유가 *"하루 넘김 미등록: finance, logistics"* 였다. 여섯이 비었나만
    보면 한쪽만 등록돼도 초록이 되므로, **터졌던 그 자리를 이름으로 못 박는다.**
    """
    _모두_비운다()

    wire_registries()

    assert tuple(sorted(day_open.registered())) == ("finance", "logistics")


def test_두_번_불러도_안전하다():
    """★ **멱등이다.** 검사가 `app.main` 을 이미 깔아 둔 채로 CLI 경로를 또 탈 수 있다.

    ⚠️ *"이미 했다"* 깃발을 두면 안 되는 이유이기도 하다 — 깃발이 있으면
      `wiring.reset()` 뒤에 다시 채우는 이 경로가 막힌다.
    """
    _모두_비운다()

    wire_registries()
    처음 = _등록_현황()
    wire_registries()

    assert _등록_현황() == 처음


# ── ③ 두 진입점이 같은 것을 등록한다 ────────────────────────────────────


def test_앱_진입점도_같은_것을_등록한다():
    """🔴 **둘이 다른 세상을 보면 CLI 로 낸 성적이 앱의 성적이 아니다.**

    ★ **값을 검사가 지어내지 않는다.** 두 경로를 차례로 태워 나온 것을 견준다.
    """
    import app.main

    _모두_비운다()
    wire_registries()
    cli = _등록_현황()

    _모두_비운다()
    importlib.reload(app.main)  # 모듈 수준 조립을 다시 돌린다 — I/O 는 없다
    앱 = _등록_현황()

    assert cli == 앱


# ── ④ 러너 진입점이 걷기 전에 부른다 ────────────────────────────────────


def test_러너가_걷기_전에_등록소를_채운다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **부르는 시점이 중요하다.** 걷고 나서 채우면 걷기는 빈 채로 돈다.

    ★ 원문을 읽지 않는다 — `walk` 대역이 **불린 그 순간의 등록 현황**을 들고 온다.
    """
    본것: dict[str, tuple[str, ...]] = {}

    def _대역(**kwargs):
        본것.update(_등록_현황())
        return WalkResult(start=kwargs["start"], end=kwargs["end"])

    monkeypatch.setattr(backtest_runner, "walk", _대역)
    _모두_비운다()

    code = backtest_runner.main(
        ["--start", "2026-01-05", "--end", "2026-01-09", "--now", "2026-09-09T10:00+09:00"]
    )

    assert code == 0
    assert 본것, "walk 대역이 안 불렸다 — 이 검사가 아무것도 안 쟀다"
    assert _빈_등록소(본것) == ()


def test_러너가_걷기_전에_안_채우면_빈_채로_걷는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **위 검사의 자기 생존.** 조립 호출을 지운 세상을 만들어 빨간불을 확인한다.

    ⚠️ 대역이 아무것도 안 보고 있으면 위 단언은 지우고도 통과한다. 같은 대역으로
      **안 채운 경로**를 태워 `_빈_등록소` 가 여섯을 다 세는지 본다.
    """
    본것: dict[str, tuple[str, ...]] = {}

    def _대역(**kwargs):
        본것.update(_등록_현황())
        return WalkResult(start=kwargs["start"], end=kwargs["end"])

    monkeypatch.setattr(backtest_runner, "walk", _대역)
    monkeypatch.setattr(backtest_runner, "wire_registries", lambda: None)
    _모두_비운다()

    backtest_runner.main(
        ["--start", "2026-01-05", "--end", "2026-01-09", "--now", "2026-09-09T10:00+09:00"]
    )

    assert len(_빈_등록소(본것)) == 6


# ── ⑤ 요약 출력이 콘솔 인코딩에서 안 죽는다 ─────────────────────────────
#
# 🔴 **걷기를 다 마치고 `print` 에서 죽었다** (2026-09-09 실측).
#
#   ```text
#   UnicodeEncodeError: 'cp949' codec can't encode character '—'
#     File "app/master/backtest_runner.py", line 401, in main
#       print(format_summary(result))
#   ```
#
#   결과를 다 계산해 놓고 성적표만 잃는다 — 다시 보려면 걷기를 통째로 또 돌려야 한다.


def _요약() -> str:
    """터졌던 그 걷기의 요약. **사유 문장을 실측 그대로 옮긴다.**

    ⚠️ 사고가 없는 결과로는 이 자리를 못 잰다 — `—` 는 사고 사유와 멈춘 사유에만
      나온다. 실제로 터진 것도 사고가 5일 연속이던 그 걷기다.
    """
    return backtest_runner.format_summary(
        WalkResult(
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
            incidents=(
                backtest_runner.WalkIncident(
                    as_of=date(2026, 1, 5),
                    reason=(
                        "판단 단계를 안 탔다 (개장: NOT_OPENED · 입고: NOT_ATTEMPTED"
                        " · 수금: NOT_ATTEMPTED) — 하루 넘김 미등록: finance, logistics"
                    ),
                ),
            ),
            stopped_at=date(2026, 1, 9),
            stopped_reason="사고가 5일 연속이라 멈춘다 (상한 5) — 이 뒤를 더 걸어도 같다",
        )
    )


def test_요약에_cp949_가_못_담는_글자가_있다():
    """🔴 **아래 검사의 자기 생존.** 터질 거리가 실제로 있는지부터 잰다.

    ⚠️ 누가 요약 문장에서 `—` 를 지워 ASCII 로 낮추면 아래 검사는 **고쳐서가 아니라
      도망가서** 초록이 된다. 여기서 먼저 빨간불이 뜬다.
    """
    with pytest.raises(UnicodeEncodeError):
        _요약().encode("cp949")


def test_cp949_스트림에_찍어도_안_죽는다(monkeypatch: pytest.MonkeyPatch):
    """★ **요약 문장이 아니라 통로를 고친다.** 한국어 요약은 그대로 나가야 한다."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp949", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    backtest_runner._use_utf8_output()
    print(_요약())
    stream.flush()

    찍힌것 = raw.getvalue().decode("utf-8")
    assert "범위" in 찍힌것, "한국어 요약이 사라졌다"
    assert "돈 날" in 찍힌것
