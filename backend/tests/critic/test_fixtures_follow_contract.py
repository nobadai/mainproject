"""Critic 픽스처가 계약 품목을 따르는지 (2026-09-03 피마늘 제외).

🔴 픽스처가 품목을 **따로 세고 있었다** (`ITEMS4`). 계약이 셋이 된 날
`tests/critic/test_critic_v0_4.py` 가 12건 깨졌고, 깨진 것은 계약이 아니라
픽스처였다 — 픽스처가 계약을 못 따라간 것이다.

이제 `FIXTURE_ITEMS = ITEMS` 로 계약에서 가져온다. 이 파일은 그 연결이
끊기면 운다.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from fixtures import DEMAND, FIXTURE_ITEMS, MIX_AMOUNT, PRICE_BASE, PRICE_HIGH
from fixtures_cycle_b import CONTRACT_PRICE

from app.contracts.core import ITEMS


def test_픽스처_품목이_계약_그_객체다():
    """값이 같은 것으로는 부족하다. **같은 객체**여야 따로 셀 자리가 없다."""
    assert FIXTURE_ITEMS is ITEMS


def test_픽스처_표들이_계약_품목을_정확히_덮는다():
    tables = {
        "PRICE_BASE": PRICE_BASE,
        "PRICE_HIGH": PRICE_HIGH,
        "DEMAND": DEMAND,
        "MIX_AMOUNT": MIX_AMOUNT,
        "CONTRACT_PRICE": CONTRACT_PRICE,
    }
    for name, table in tables.items():
        assert set(table) == set(ITEMS), f"{name} 이 계약과 다르다: {sorted(table)}"


def test_mix_비중은_정규화하지_않았다():
    """⚠️ 합이 0.918 인 것은 실수가 아니라 결정이다.

    피마늘 몫 0.082 를 빼고 **정의서 원값을 그대로 뒀다**. 게이팅은 최대값만
    보므로(`max(...) < 0.70`) 정규화해도 결과가 같고, 지어낸 값을 남기지 않는 쪽을
    골랐다. 누가 무심코 정규화하면 여기가 알려 준다.
    """
    assert abs(sum(MIX_AMOUNT.values()) - 0.918) < 1e-9
    assert max(MIX_AMOUNT.values()) == MIX_AMOUNT["배추"] == 0.812


# ---------------------------------------------------------------------------
# 기준일 — sim_start_date (M-24 D-2 · 2026-09-03 확정)
# ---------------------------------------------------------------------------

#: 절대 날짜를 써도 되는 자리. **늘리려면 이유가 있어야 한다.**
_ABSOLUTE_DATE_ALLOWED = {
    "fixtures.py",  # AS_OF 그 자신
}


def _absolute_dates(path: Path) -> list[tuple[int, tuple[int, ...]]]:
    """`date(YYYY, M, D)` 처럼 **상수만으로 세운** 날짜의 줄 번호와 값."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, tuple[int, ...]]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "date" or len(node.args) != 3:
            continue
        if all(isinstance(a, ast.Constant) and isinstance(a.value, int) for a in node.args):
            out.append((node.lineno, tuple(a.value for a in node.args)))
    return out


def test_기준일이_확정값이다():
    """🔴 오래 `date(2023, 3, 15)` 플레이스홀더였다.

    네 파트가 모두 READY 인 날이 이 하루뿐이라 선택지가 없었다 (`M-24` §1.4).
    """
    from fixtures import AS_OF

    assert AS_OF == date(2025, 12, 31), (
        "기준일이 바뀌었다 — sim_start_date 는 M-24 D-2 로 확정된 값이다. "
        "바꾸려면 네 파트 데이터가 그 날에 다 있는지부터 재라"
    )


def test_옛_플레이스홀더가_안_남았다():
    """⚠️ **먼저 스캐너가 도는지부터 단언한다.**

    `(2023, 3, 15) not in hits` 만 쓰면 `hits` 가 비어도 통과한다 — 스캐너가
    고장 나도 초록불이다. 지금 있는 값을 먼저 찾아야 없는 값도 믿을 수 있다.
    """
    hits = [v for _, v in _absolute_dates(Path(__file__).parent / "fixtures.py")]

    assert (2025, 12, 31) in hits, "스캐너가 AS_OF 조차 못 찾는다 — 검사가 공허하다"
    assert (2023, 3, 15) not in hits, "옛 플레이스홀더가 남아 있다"


def test_기준일_말고는_절대_날짜를_안_쓴다():
    """★ **이것이 교체를 한 줄로 만든 규율이다.**

    다른 날짜를 전부 `AS_OF + timedelta(...)` 로 쓰기 때문에 기준일 하나만
    바꾸면 픽스처 전체가 따라온다. 절대 날짜를 흩뿌리면 다음 교체가 스무 곳이 된다.
    """
    here = Path(__file__).parent
    mine = Path(__file__).name
    offenders = {
        path.name: hits
        for path in sorted(here.glob("*.py"))
        if path.name not in _ABSOLUTE_DATE_ALLOWED and path.name != mine
        for hits in [_absolute_dates(path)]
        if hits
    }

    assert not offenders, (
        f"절대 날짜가 섞였다: {offenders}. AS_OF + timedelta(...) 로 쓰거나, "
        f"기준일과 무관한 값이면 _ABSOLUTE_DATE_ALLOWED 에 이유와 함께 더해라"
    )
