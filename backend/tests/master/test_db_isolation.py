"""**격리가 실제로 섰는지**를 잰다 — 격리는 조용히 새기 때문이다.

🔴 **fixture 가 안 먹어도 아무도 모른다.** `conftest.py` 의 `개장_정본_적재를_막는다`
   가 뚫리면 검사는 실 DB 를 치고, 답은 **그날 팀 공용 DB 에 행이 있느냐로 갈린다.**
   그때 나오는 빨간불은 **다른 사람 손에서 재현되지 않는다.**

★ 그래서 *"막았다"* 를 믿지 않고 **막혔는지를 직접 잰다.** 여기가 빨개지면 fixture 가
  뚫린 것이지, 잰 코드가 틀린 것이 아니다.
"""

from __future__ import annotations

import pytest
from 개장정본_격리 import 개장_정본_이름을_가져간_모듈들, 진짜_개장_정본_함수


@pytest.mark.parametrize("이름", sorted(진짜_개장_정본_함수))
def test_개장_정본_함수를_들고_있는_모듈이_하나도_안_새어_있다(이름: str) -> None:
    """⚠️ **이름을 가져간 모듈이 하나라도 안 막혀 있으면 빨개진다.**

    ★ `from X import f` 는 이름을 **복사한다.** 원본 모듈만 막으면 복사본은 진짜
      함수를 계속 부르고, 그 모듈은 실 DB 를 친다.
    """
    샌_모듈 = [
        모듈.__name__
        for 모듈 in 개장_정본_이름을_가져간_모듈들(이름)
        if getattr(모듈, 이름) is 진짜_개장_정본_함수[이름]
    ]

    assert 샌_모듈 == [], f"{이름} 이 안 막힌 모듈이 있다 — 실 DB 를 친다: {샌_모듈}"


def test_조회_이름을_가져간_모듈이_실제로_잡힌다() -> None:
    """🔴 **위 검사가 빈 목록으로 통과하는 것을 막는다.**

    ★ `day_gate` 가 `read_day_opening` 을 이름으로 가져가는 **바로 그 모듈**이다.
      여기가 목록에서 빠지면 위 검사는 아무것도 안 재고도 초록이 된다.
    """
    잡힌_모듈 = {모듈.__name__ for 모듈 in 개장_정본_이름을_가져간_모듈들("read_day_opening")}

    assert "app.master.day_gate" in 잡힌_모듈
    assert "app.master.day_opening_repository" in 잡힌_모듈


def test_적재_이름을_가져간_모듈도_실제로_잡힌다() -> None:
    """★ 조회와 **같은 구조다.** `day_open` 이 `record_day_opening` 을 가져간다."""
    잡힌_모듈 = {모듈.__name__ for 모듈 in 개장_정본_이름을_가져간_모듈들("record_day_opening")}

    assert "app.master.day_open" in 잡힌_모듈
    assert "app.master.day_opening_repository" in 잡힌_모듈
