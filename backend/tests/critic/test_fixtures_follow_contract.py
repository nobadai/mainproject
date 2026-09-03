"""Critic 픽스처가 계약 품목을 따르는지 (2026-09-03 피마늘 제외).

🔴 픽스처가 품목을 **따로 세고 있었다** (`ITEMS4`). 계약이 셋이 된 날
`tests/critic/test_critic_v0_4.py` 가 12건 깨졌고, 깨진 것은 계약이 아니라
픽스처였다 — 픽스처가 계약을 못 따라간 것이다.

이제 `FIXTURE_ITEMS = ITEMS` 로 계약에서 가져온다. 이 파일은 그 연결이
끊기면 운다.
"""

from __future__ import annotations

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
