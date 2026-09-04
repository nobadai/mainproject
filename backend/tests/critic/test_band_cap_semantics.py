"""`cap_by_date` 는 **net** 이다 — 확정 점유를 두 번 세지 않는다.

2026-09-03 · #181 · 물류 회신.

🔴 전에는 `확정 점유[d] + 도착분` 을 cap 과 비교했다. 그런데 그 cap 을 만드는
유일한 곳이 **이미 확정 점유를 뺀 값**을 돌려준다.

```python
# app/logistics/tools.py:270
max(0, guaranteed_capacity_kg - projected_occupancy)
```

**한 번 빠지고 한 번 더해져** 확정분을 두 번 셌다.

★ 뜻을 net 으로 정한 근거는 다수결이 아니라 **생산자**다 (물류 IO Contract §6).
  쓰는 자리 셋 중 둘(물류 `_available_capacity` · 매입 PR #179)이 net 이고
  여기만 gross 였다.

⚠️ **프로덕션 결과는 안 바뀐다.** `confirmed_occupancy_by_date` 를 운영 경로에서
  아무도 안 채운다 — 늘 `0` 이라 두 식이 같았다. **채우는 날 조용히 틀리는 것**을
  막는 판이고, 그래서 이 파일이 필요하다. 그 칸을 채워야만 차이가 보인다.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import timedelta

from fixtures import AS_OF, FIXTURE_ITEMS, make_snapshot

from app.contracts.core import Band, ClipResult, SplitLeg
from app.orchestrator.band import check_occupancy_detailed

D2 = AS_OF + timedelta(days=2)
D5 = AS_OF + timedelta(days=5)

SNAP = make_snapshot()


def _band(cap_by_date: dict) -> Band:
    return Band(
        {i: 0.0 for i in FIXTURE_ITEMS},
        {i: 1e9 for i in FIXTURE_ITEMS},
        1e9,
        1e12,
        {},
        cap_by_date,
    )


def _clip(*legs: tuple[int, float, object]) -> ClipResult:
    """회차별 (offset, 수량, 도착일) 로 매입안 하나를 만든다."""
    plan = tuple(SplitLeg(off, {"배추": qty}, eta) for off, qty, eta in legs)
    total = sum(qty for _, qty, _ in legs)
    return ClipResult(
        scenario_id="SCN-CAP",
        qty_kg={"배추": total},
        clipped_qty_kg={"배추": total},
        clipped_split_plan=plan,
    )


# ── ① 핵심 — 확정 점유를 더하지 않는다 ──────────────────────────────────────


def test_확정_점유가_있어도_도착분만_본다():
    """🔴 **이 파일의 주장이다.**

    확정 점유 900 · 잔여 여유 1,000 · 매입안 도착 800 이면 **통과**다.
    cap 이 net 이라 900 은 이미 1,000 안에 반영돼 있다.

    전 코드는 `900 + 800 = 1,700 > 1,000` 으로 읽어 **없는 위반을 만들었다.**
    """
    snap = dc_replace(SNAP, confirmed_occupancy_by_date={D2: 900.0})
    result = check_occupancy_detailed(_clip((0, 800.0, D2)), _band({D2: 1_000.0}), snap)

    assert result.ran
    assert result.problems == (), f"확정 점유를 두 번 셌다: {result.problems}"


def test_도착분이_여유를_넘으면_지적한다():
    """반대 방향 — 느슨해지기만 하면 검사가 죽은 것이다."""
    snap = dc_replace(SNAP, confirmed_occupancy_by_date={D2: 900.0})
    result = check_occupancy_detailed(_clip((0, 1_200.0, D2)), _band({D2: 1_000.0}), snap)

    assert len(result.problems) == 1
    assert "1,200" in result.problems[0]


def test_사유_문장이_잔여_여유라고_말한다():
    """*"점유 > cap"* 으로 적으면 읽는 사람이 gross 로 오해한다."""
    result = check_occupancy_detailed(_clip((0, 1_200.0, D2)), _band({D2: 1_000.0}), SNAP)

    assert "잔여 여유" in result.problems[0]
    assert "확정 점유가 빠진 값" in result.problems[0]


# ── ② 프로덕션 등가 — 지금은 결과가 안 바뀐다 ───────────────────────────────


def test_확정_점유가_비면_전후가_같다():
    """⚠️ **운영 경로가 이 상태다.** 그래서 이번 변경이 산출을 안 흔든다.

    `confirmed_occupancy_by_date` 를 채우는 곳이 앱에 없다 — 마스터
    `critic_bridge` 도 안 보낸다. 늘 비어 있으므로 `확정 + 도착분 == 도착분` 이다.
    """
    clip = _clip((0, 800.0, D2))
    band = _band({D2: 1_000.0})

    empty = check_occupancy_detailed(
        clip, band, dc_replace(SNAP, confirmed_occupancy_by_date={})
    )
    assert empty.problems == ()

    over = check_occupancy_detailed(_clip((0, 1_100.0, D2)), band, SNAP)
    assert len(over.problems) == 1


# ── ③ 누적 — 앞 회차가 뒤 날짜까지 쌓인다 ───────────────────────────────────


def test_앞_회차가_뒤_날짜까지_점유로_남는다():
    """물류·매입과 같은 규약이다 (`arrival <= day`).

    1회차 600 이 D2 에 도착하면 D5 계산에도 그 600 이 들어 있어야 한다.
    """
    clip = _clip((0, 600.0, D2), (3, 600.0, D5))
    result = check_occupancy_detailed(clip, _band({D2: 1_000.0, D5: 1_000.0}), SNAP)

    assert result.ran
    assert len(result.problems) == 1, "D5 에서 600+600=1,200 이 1,000 을 넘어야 한다"
    assert str(D5) in result.problems[0]
    assert "1,200" in result.problems[0]


def test_각_날짜를_따로_보지_않는다():
    """🔴 누적을 안 하면 회차를 쪼갤수록 검사를 안 받는다.

    회차마다 600 씩이라 **날짜별로만 보면 둘 다 통과**한다. 누적해야 걸린다.
    """
    clip = _clip((0, 600.0, D2), (3, 600.0, D5))
    result = check_occupancy_detailed(clip, _band({D2: 1_000.0, D5: 1_000.0}), SNAP)

    assert result.problems, "회차를 쪼개면 검사를 피하는 구조가 된다"
