"""④ 회차 배분 후보 (E3-9) — **후보 집합과 선택 전 안전 검사.**

🔴 **합성 입력이다.** 운영 데이터(``SIM-CHAIN-V13``)의 안 231건은 **전부 1회차**라
다회차 배분이 한 번도 안 선다. 그래서 여기서 재는 것은 **배선과 불변식**이고,
**사업 효과가 아니다.** 효과는 운영에서 다회차가 실제로 서는 날 다시 잰다.

⚠️ 아래 비율은 **fixture 전용**이다. 선언의 값은 ``PROVISIONAL`` 이고 승인자가 없다 —
이 검사가 통과한다고 그 비율이 «검증된 후보» 가 되는 것이 아니다.
"""

import pytest

from app.purchase_agent import allocation as al

#: 합성 선언 — 🔴 **승인된 값이 아니다.** 배선을 재려고 세운 것이다.
합성선언 = {
    "status": "FIXTURE_ONLY",
    "two_rounds": {"FRONT_LOADED": [0.60], "BACK_LOADED": [0.40]},
    "three_rounds": {"FRONT_LOADED": [0.50, 0.30], "BACK_LOADED": [0.20, 0.30]},
}


def test_승인_전_선언으로는_후보를_안_세운다() -> None:
    """🔴 근거 없는 비율로 안을 만들면 나중에 그 배분이 **「검증된 것」으로 보인다.**

    `#390` 에서 무른 것과 같은 모양이라, 승인 전에는 균등 하나만 남긴다.
    """
    선언 = dict(합성선언, status=al.PROVISIONAL)
    assert list(al.allocation_candidates(선언, 3)) == ["BASE_EQUAL"]


def test_기본안이_늘_후보_안에_있다() -> None:
    """**되돌아갈 자리가 후보 안에 있어야** 실패했을 때 고를 것이 남는다."""
    for rounds in (1, 2, 3):
        assert "BASE_EQUAL" in al.allocation_candidates(합성선언, rounds)


def test_일괄이면_고를_것이_없다() -> None:
    """1회차에 후보가 여럿이면 «나누지 않은 분할» 이 생긴다."""
    assert list(al.allocation_candidates(합성선언, 1)) == ["BASE_EQUAL"]


def test_모르는_후보_이름이_선언에_들어오면_멈춘다() -> None:
    """🔴 선언만 늘면 LLM 이 그 이름을 골랐을 때 **무슨 뜻인지 아무도 모른다.**"""
    with pytest.raises(ValueError, match="모르는 배분 후보"):
        al.allocation_candidates(
            dict(합성선언, three_rounds={"SIDEWAYS": [0.3, 0.3]}), 3
        )


@pytest.mark.parametrize("rounds", [2, 3])
def test_어느_후보든_비율_합이_정확히_1이다(rounds: int) -> None:
    """⑥의 합계 검사가 ``1e-9`` 라 잔차가 남으면 걸린다."""
    for 비율 in al.allocation_candidates(합성선언, rounds).values():
        assert abs(sum(비율) - 1.0) <= 1e-9
        assert len(비율) == rounds


@pytest.mark.parametrize("rounds", [2, 3])
@pytest.mark.parametrize("total", [29, 1_435, 4_286, 12_345])
def test_합성_다회차_불변식_수량합이_총량과_같다(rounds: int, total: int) -> None:
    """🔴 **사중 일치의 한 축** — 어느 후보를 골라도 총량이 안 흔들린다.

    ⚠️ 합성 입력이다. 운영에서 다회차가 선 적이 없다.
    """
    for 이름, 비율 in al.allocation_candidates(합성선언, rounds).items():
        수량 = al.split_quantities(total, [{"ratio": r} for r in 비율])
        assert sum(수량) == total, 이름
        assert len(수량) == rounds


@pytest.mark.parametrize("rounds", [2, 3])
def test_합성_다회차_불변식_각_회차가_양수다(rounds: int) -> None:
    """0kg 회차가 생기면 «회차를 적었는데 아무것도 안 사는» 계획이 된다."""
    for 이름, 비율 in al.allocation_candidates(합성선언, rounds).items():
        수량 = al.split_quantities(4_286, [{"ratio": r} for r in 비율])
        assert all(q > 0 for q in 수량), (이름, 수량)


def test_첫_회차는_어느_후보에서도_as_of_다() -> None:
    """IO명세 §2 *"seq 1의 date = as_of"* — 배분을 바꿔도 이 자리는 안 움직인다."""
    for rounds in (2, 3):
        assert al.split_offsets(12, rounds)[0] == 0


def test_모르는_날이_하나라도_있으면_안_든다() -> None:
    """🔴 **규칙 3.** 못 본 것을 「든다」로 읽으면 모르는 것이 판정을 만든다."""
    assert al.occupancy_fits([100, 100], None, {"2026-01-07": 500}) is False
    assert al.occupancy_fits([100, 100], ["2026-01-07", "2026-01-09"], None) is False
    assert (
        al.occupancy_fits([100, 100], ["2026-01-07", "2026-01-09"], {"2026-01-07": 500})
        is False
    )


def test_누적으로_견준다() -> None:
    """🔴 ⑦ ``check_arrival_capacity`` 와 **같은 셈이다** — 앞 회차가 아직 창고에 있다.

    회차마다 따로 보면 «⑤는 된다는데 ⑦이 컷하는» 후보가 생기고, 그 안은 왜 죽었는지
    설명할 수 없다.
    """
    여유 = {"2026-01-07": 150.0, "2026-01-09": 150.0}
    도착 = ["2026-01-07", "2026-01-09"]
    assert al.occupancy_fits([100, 40], 도착, 여유) is True     # 누적 140 ≤ 150
    assert al.occupancy_fits([100, 60], 도착, 여유) is False    # 누적 160 > 150


def test_앞으로_몰수록_먼저_걸린다() -> None:
    """배분의 뜻이 검사에 그대로 나타난다 — 앞에 몰면 첫 도착일에서 먼저 막힌다."""
    여유 = {"2026-01-07": 100.0, "2026-01-09": 300.0}
    도착 = ["2026-01-07", "2026-01-09"]
    앞 = al.split_quantities(200, [{"ratio": r} for r in al.weighted_ratios([0.60], 2)])
    뒤 = al.split_quantities(200, [{"ratio": r} for r in al.weighted_ratios([0.40], 2)])
    assert al.occupancy_fits(앞, 도착, 여유) is False
    assert al.occupancy_fits(뒤, 도착, 여유) is True


def test_가중치_개수가_회차와_안_맞으면_멈춘다() -> None:
    """마지막은 잔차라 **앞 회차만 적는다** — 다 적으면 합이 1에서 밀린다."""
    with pytest.raises(ValueError, match="회차에 안 맞는다"):
        al.weighted_ratios([0.5, 0.3], 2)
