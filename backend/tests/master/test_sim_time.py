"""**`sim_time.phase_instant` 는 `as_of` 에서 파생하고, 시계를 읽지 않는다.**

★ 이 파일이 지키는 것은 결국 하나다 — **같은 시뮬레이션 입력을 다시 돌리면 장부에
  같은 시각이 적힌다.** 물류 `decided_at` 이 이 값을 받아 `inventory_allocations`
  에 NOT NULL 로 들어가므로, 여기가 흔들리면 재현이 통째로 깨진다.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from app.master import sim_time
from app.master.clock import SCHEDULE_START, SEOUL
from app.master.sim_time import PHASES, phase_instant

_AS_OF = date(2026, 9, 8)


def test_여섯_단계가_선언_순서대로_증가한다():
    """**각각 다른 시각이고, `PHASES` 순서대로 커진다.**

    🔴 두 단계가 같은 시각이면 장부에서 순서가 사라진다 — 그것이 이 모듈의
      존재 이유 자체다.
    """
    moments = [phase_instant(_AS_OF, phase) for phase in PHASES]

    assert len(set(moments)) == len(PHASES), (
        f"단계마다 다른 시각이어야 한다: {dict(zip(PHASES, moments))}"
    )
    assert moments == sorted(moments), f"선언 순서대로 증가해야 한다: {dict(zip(PHASES, moments))}"


def test_기준점은_스케줄러가_깨어나는_시각이다():
    """★ **09:30 의 주인은 `clock.SCHEDULE_START` 다.**

    ⚠️ 여기서 `time(9, 30)` 을 새로 적으면 마감을 옮기는 날 둘이 조용히 갈린다.
      그래서 상수를 비교하지 않고 **그 상수로 기대값을 만든다.**
    """
    first = phase_instant(_AS_OF, PHASES[0])

    assert first.timetz().replace(tzinfo=None) == SCHEDULE_START
    assert first.date() == _AS_OF


def test_단계_간격은_정확히_일분이다():
    """**순서 표지는 1분 간격이다.** 🔴 이 분에 업무적 의미는 없다."""
    moments = [phase_instant(_AS_OF, phase) for phase in PHASES]
    gaps = [b - a for a, b in pairwise(moments)]

    assert gaps == [timedelta(minutes=1)] * (len(PHASES) - 1), f"간격이 다르다: {gaps}"


def test_결과는_시간대를_단_KST_다():
    """🔴 **naive 면 실패다.**

    물류가 `decided_at.tzinfo is None` 을 직접 막는다 — naive 를 넘기면 거기서
    깨지고, 우리는 그 이유를 여기서 먼저 안다.
    """
    for phase in PHASES:
        moment = phase_instant(_AS_OF, phase)
        assert moment.tzinfo is not None, f"{phase} 가 naive 다"
        assert moment.tzinfo is SEOUL, f"{phase} 의 시간대가 clock.SEOUL 이 아니다"
        assert moment.utcoffset() == timedelta(hours=9), (
            f"{phase} 가 KST 가 아니다: {moment.utcoffset()}"
        )


def test_같은_입력이면_같은_값이다():
    """**두 번 불러 같아야 한다.** 시계를 읽으면 여기서 갈린다."""
    for phase in PHASES:
        assert phase_instant(_AS_OF, phase) == phase_instant(_AS_OF, phase)


def test_as_of_가_다르면_날짜가_따라_바뀐다():
    """**날짜는 `as_of` 가 정한다.** 그것 말고 이 함수에 입력이 없다."""
    other = date(2027, 1, 2)

    for phase in PHASES:
        here = phase_instant(_AS_OF, phase)
        there = phase_instant(other, phase)
        assert here.date() == _AS_OF
        assert there.date() == other
        assert here.timetz() == there.timetz(), "날만 다르고 시각은 같아야 한다"


def test_모르는_단계는_막는다():
    """🔴 **조용히 기본값을 내면 안 된다.**

    기본값을 내면 오타 하나가 *"DAY_OPEN 과 같은 시각"* 으로 조용히 적히고,
    장부에서는 두 사건이 동시에 일어난 것으로 보인다.
    """
    for unknown in ("SHIPPED", "ALLOCATED", "OPEN", "day_open", ""):
        with pytest.raises(ValueError):
            phase_instant(_AS_OF, unknown)  # type: ignore[arg-type]


# ── 이 모듈이 시계를 안 읽는지 ────────────────────────────────────────────
#
# 🟡 **repo 전체의 벽시계 금지는 `test_clock_is_the_only_wall_clock.py` 가 소유한다.**
#   여기가 따로 보는 것은 그 스캐너가 **못 보는 것** 이다 — `clock.seoul_now()` 와
#   `clock.today_in_seoul()` 은 `datetime.now` 가 아니라서 그 검사를 통과하지만,
#   이 모듈이 부르는 순간 `as_of` 파생이 아니라 **오늘로 답하기** 시작한다.

_SOURCE = Path(sim_time.__file__)

#: 이 모듈이 부르면 안 되는 호출. **벽시계 셋 + clock 의 시계 함수 둘.**
_FORBIDDEN = frozenset(
    {"datetime.now", "date.today", "datetime.utcnow", "seoul_now", "today_in_seoul"}
)


def _called_names(source: str) -> set[str]:
    """호출되는 이름들. `datetime.now()` → `datetime.now`, `seoul_now()` → `seoul_now`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        found.add(name)
        # `clock.seoul_now()` 처럼 점이 붙어도 끝 이름으로 한 번 더 넣는다.
        found.add(name.split(".")[-1])
    return found


def test_스캐너가_실제로_시계를_찾아낸다():
    """🔴 **자기 생존 검사.** 0건을 세는 것과 못 보는 것을 가른다.

    ★ 이게 없으면 스캐너가 망가진 날 **공짜 초록**이 난다 — 그때는 이 모듈이
      벽시계를 열 줄 적어도 아래 검사가 통과한다.
    """
    sample = (
        "import clock\nfrom datetime import date, datetime\n"
        + "\n".join(f"x = {call}()" for call in ("datetime.now", "date.today", "datetime.utcnow"))
        + "\ny = clock.seoul_now()\nz = clock.today_in_seoul()\n"
    )

    detected = _called_names(sample) & _FORBIDDEN

    assert detected == _FORBIDDEN, f"스캐너가 못 잡는 것이 있다: {_FORBIDDEN - detected}"


def test_스캐너가_읽은_것이_정말_이_모듈이다():
    """🔴 **자기 생존 검사 두 번째.** 빈 파일을 훑고 초록을 내면 안 된다."""
    source = _SOURCE.read_text(encoding="utf-8")

    assert _SOURCE.name == "sim_time.py", f"엉뚱한 파일을 본다: {_SOURCE}"
    assert "def phase_instant" in source, "sim_time.py 를 읽은 것이 맞는지 확인 못 한다"


def test_이_모듈은_시계를_안_읽는다():
    """**`as_of` 에서 파생할 뿐, 지금이 몇 시인지 묻지 않는다.**

    ⚠️ 고치는 법은 그 자리에서 시계를 지우고 **`as_of` 를 인자로 받는 것**이다.
      `clock.seoul_now()` 를 부르는 것도 답이 아니다 — 그 순간 백테스트가 오늘로
      답하기 시작한다 (`clock.py` docstring 과 같은 규율).
    """
    hits = _called_names(_SOURCE.read_text(encoding="utf-8")) & _FORBIDDEN

    assert not hits, f"sim_time.py 가 시계를 읽는다: {sorted(hits)}"


def test_기준점_상수를_새로_만들지_않는다():
    """★ **09:30 을 여기 적지 않았다.** 주인은 `scheduler.py` 하나다.

    🔴 값이 같아서 안 보이는 중복이 제일 오래 산다 — `revalidation.py` 의 `_KST`
      가 그랬다.
    """
    source = _SOURCE.read_text(encoding="utf-8")
    literals = {
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "time"
    }

    assert not literals, f"시각 상수를 새로 만들었다: {literals}"
    assert "from app.master.clock import SCHEDULE_START" in source


def test_시각_모듈이_스케줄러를_안_들인다():
    """🔴 **방향을 잠근다** (2026-09-08).

    `sim_time` 이 `scheduler` 를 들이면 `sim_time → scheduler → service` 가 되고,
    `service` 가 `phase_instant` 를 쓰는 날 **순환이 난다.** 그 경로는 가설이 아니다 —
    `sim_time` 이 먹여 살리려는 자리가 정확히 `scheduler` 가 모는 자리다.

    ★ 그래서 `SCHEDULE_START` 의 집을 leaf 인 `clock.py` 로 옮겼다. 이 검사는
      **되돌아가는 것**을 막는다.
    """
    import ast
    import pathlib

    source = pathlib.Path(sim_time.__file__).read_text(encoding="utf-8")
    나무 = ast.parse(source)

    들인_것 = {
        마디.module for 마디 in ast.walk(나무) if isinstance(마디, ast.ImportFrom) and 마디.module
    } | {
        별칭.name for 마디 in ast.walk(나무) if isinstance(마디, ast.Import) for 별칭 in 마디.names
    }

    # ★ 자기 생존 검사 — 스캐너가 실제로 임포트를 찾았는가. 0건을 세면 공짜 초록이다.
    assert "app.master.clock" in 들인_것, f"스캐너가 임포트를 못 찾았다: {sorted(들인_것)}"

    막힌 = {이름 for 이름 in 들인_것 if "scheduler" in 이름 or "service" in 이름}
    assert not 막힌, (
        f"sim_time 이 {sorted(막힌)} 을 들였다 — 순환이 난다."
        " 시각 상수는 leaf 인 app.master.clock 에서 가져온다"
    )
