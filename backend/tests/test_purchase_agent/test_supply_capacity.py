"""판매 추가공급 경계 계산 — **못 지킬 수를 안 내는 것**을 잠근다 (E4-7).

이 파일이 지키는 것 넷::

    · 단가를 등급 **이름**이 아니라 **값**으로 고른다 (「특」이 늘 최고가가 아니다)
    · 나눗셈에 쓴 단가와 회신에 싣는 단가가 같은 수다 (마스터 조건)
    · `0`(읽은 값)과 `None`(못 읽음)이 안 섞인다 (규칙 3)
    · 한쪽만 알고 확정하지 않는다 — 판매는 이 수를 **확보 가능량**으로 읽는다

🔴 **설정 검사에 값 비교를 쓰지 않는다** (규칙 8). 선언을 실제로 바꿔 판정이
  따라 움직이는지 본다.
"""

import copy

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.supply_capacity import (
    SupplyCapacity,
    compute_supply_capacity,
    pick_unit_price,
)


def _quotes(**by_grade: int) -> list[dict]:
    return [{"market": "가락", "grade": g, "price": p} for g, p in by_grade.items()]


@pytest.fixture
def constraints() -> dict:
    return load_constraints()


# ---------------------------------------------------------------------------
# 1. 단가 — 이름이 아니라 값
# ---------------------------------------------------------------------------


def test_단가는_그날_최고가_등급에서_온다(constraints):
    assert pick_unit_price(_quotes(특=1850, 상=1650, 중=1450), constraints) == (1850, "특")


def test_특이_최고가가_아닌_날에는_특을_안_고른다(constraints):
    """🔴 관통 실측에 실재한 모양 — 배추 `중 550 > 특 278`.

    등급 2개 이상인 331일 중 44일(13.3%)이 이랬다. 이름으로 박았으면 그날
    278원으로 나눠 **최대 97.8% 더 많은 양**을 약속했을 것이다.
    """
    assert pick_unit_price(_quotes(중=550, 특=278), constraints) == (550, "중")


def test_등급이_하나뿐인_날도_답이_나온다(constraints):
    """배추는 관통 216일 중 209일이 이 모양이다 — 선택이 없는 것이지 오류가 아니다."""
    assert pick_unit_price(_quotes(특=1200), constraints) == (1200, "특")


def test_시세가_없으면_단가가_없다(constraints):
    assert pick_unit_price([], constraints) is None


@pytest.mark.parametrize("price", [0, -100])
def test_0원과_음수는_단가로_안_쓴다(constraints, price):
    """0원 낙찰은 시세가 아니다 — 나누면 ZeroDivisionError 이거나 음수 수량이 된다."""
    assert pick_unit_price(_quotes(특=price, 상=900), constraints) == (900, "상")


def test_쓸_수_있는_시세가_하나도_없으면_None(constraints):
    assert pick_unit_price(_quotes(특=0, 상=-1), constraints) is None


# ---------------------------------------------------------------------------
# 2. 🔴 규칙 8 — 선언을 바꾸면 판정이 따라오나
# ---------------------------------------------------------------------------


def test_모르는_단가_기준이_선언되면_멈춘다(constraints):
    """선언만 바꾸고 코드를 안 열면 **조용히 옛 기준으로 도는 일**이 없어야 한다."""
    mutated = copy.deepcopy(constraints)
    mutated["supply_capacity"]["unit_price_basis"] = "lowest_grade"
    with pytest.raises(ValueError, match="모르는 단가 기준"):
        pick_unit_price(_quotes(특=1850, 중=1450), mutated)


def test_선언을_지우면_멈춘다(constraints):
    """기본값으로 메우지 않는다 — 기준 없이 도는 것이 제일 나쁘다."""
    mutated = copy.deepcopy(constraints)
    del mutated["supply_capacity"]["unit_price_basis"]
    with pytest.raises(KeyError):
        pick_unit_price(_quotes(특=1850), mutated)


def test_선언이_실제로_highest_grade다(constraints):
    """코드가 구현한 값을 선언과 대조한다 — 선언만 바꾸면 위 검사가 멈춘다 (규칙 8 반대 방향)."""
    assert constraints["supply_capacity"]["unit_price_basis"] == "highest_grade"


# ---------------------------------------------------------------------------
# 3. 무엇이 막았나 — basis
# ---------------------------------------------------------------------------


def test_창고가_더_좁으면_창고가_막은_것이다(constraints):
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=500,
        finance_cap_amount_krw=2_000_000,  # 2,000kg
        constraints=constraints,
    )
    assert (got.procurable_quantity_kg, got.basis) == (500, "warehouse")
    assert got.risks == ()


def test_재무가_더_좁으면_돈이_막은_것이다(constraints):
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=5000,
        finance_cap_amount_krw=1_200_000,  # 1,200kg
        constraints=constraints,
    )
    assert (got.procurable_quantity_kg, got.basis) == (1200, "finance")


def test_동률이면_한쪽만_풀어도_안_는다고_말한다(constraints):
    """창고만 빌려 놓고 늘기를 기다리는 일이 없게 한다."""
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=1500,
        finance_cap_amount_krw=1_500_000,  # 같은 1,500kg
        constraints=constraints,
    )
    assert got.procurable_quantity_kg == 1500
    assert got.risks and "한쪽만 풀어도" in got.risks[0]


def test_단가는_한_번만_구해_나눗셈과_회신에_같이_쓴다(constraints):
    """🔴 마스터 조건 — 두 값이 갈리면 어느 쪽이 참인지 아무도 말해 주지 않는다."""
    got = compute_supply_capacity(
        quotes=_quotes(중=1600, 특=1000),  # 최고가는 「중」
        warehouse_free_kg=10_000,
        finance_cap_amount_krw=8_000_000,
        constraints=constraints,
    )
    assert got.expected_unit_price_krw == 1600
    assert got.unit_price_grade == "중"
    assert got.procurable_quantity_kg == 8_000_000 // 1600  # 회신 단가로 나눈 값과 같다


# ---------------------------------------------------------------------------
# 4. 🔴 규칙 3 — `0` 과 `None`
# ---------------------------------------------------------------------------


def test_창고_0은_읽은_값이라_0kg_을_답한다(constraints):
    """*"자리가 없다"* 는 사실이다. `None`(못 읽음)으로 뭉개지 않는다."""
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=0,
        finance_cap_amount_krw=5_000_000,
        constraints=constraints,
    )
    assert got.procurable_quantity_kg == 0
    assert got.basis == "warehouse"


def test_재무_0도_읽은_값이다(constraints):
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=5000,
        finance_cap_amount_krw=0,
        constraints=constraints,
    )
    assert got.procurable_quantity_kg == 0
    assert got.basis == "finance"


@pytest.mark.parametrize(
    ("warehouse", "finance", "말"),
    [
        (None, 5_000_000, "창고 여유를 받지 못했다"),
        (5000, None, "매입 가능액을 받지 못했다"),
    ],
)
def test_한쪽만_알면_확정하지_않는다(constraints, warehouse, finance, 말):
    """*"적어도 이보다 작다"* 를 **확보 가능량**으로 내면 못 지킬 수를 내는 것이다."""
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=warehouse,
        finance_cap_amount_krw=finance,
        constraints=constraints,
    )
    assert got.procurable_quantity_kg is None
    assert got.basis == "unknown"
    assert any(말 in r for r in got.risks)


def test_둘_다_0이면_0kg_이지_모르는_것이_아니다(constraints):
    """🔴 **변이 시험이 찾은 구멍이다** (2026-09-10).

    ``is None`` 을 ``not …`` 으로 바꿔도 위아래 검사가 전부 통과했다 — `0` 이
    「못 읽었다」로 넘어가는데 아무도 안 울었다. `(0, 0)` 은 *"창고도 돈도 지금
    없다"* 는 **읽은 사실**이고, 판매는 그걸 «확인함» 으로 받아야 한다.
    """
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=0,
        finance_cap_amount_krw=0,
        constraints=constraints,
    )
    assert got.procurable_quantity_kg == 0
    assert got.basis != "unknown"


def test_둘_다_못_받으면_아직_모른다고_답한다(constraints):
    got = compute_supply_capacity(
        quotes=_quotes(특=1000),
        warehouse_free_kg=None,
        finance_cap_amount_krw=None,
        constraints=constraints,
    )
    assert (got.procurable_quantity_kg, got.basis) == (None, "unknown")
    # ★ 사유가 **한 줄**이어야 한다 — 둘 다 못 받았는데 두 줄로 적으면 읽는 쪽이
    #   *"둘 중 하나는 받았나"* 로 헷갈린다.
    assert len(got.risks) == 1
    assert "둘 다 받지 못했다" in got.risks[0]


def test_재료가_없어도_단가는_답한다(constraints):
    """단가는 우리 값이라 남을 안 기다린다 — 답할 수 있는 것은 답한다."""
    got = compute_supply_capacity(
        quotes=_quotes(특=1400),
        warehouse_free_kg=None,
        finance_cap_amount_krw=None,
        constraints=constraints,
    )
    assert (got.expected_unit_price_krw, got.unit_price_grade) == (1400, "특")


def test_시세가_없으면_단가도_수량도_없다(constraints):
    got = compute_supply_capacity(
        quotes=[],
        warehouse_free_kg=5000,
        finance_cap_amount_krw=5_000_000,
        constraints=constraints,
    )
    assert got.procurable_quantity_kg is None
    assert got.expected_unit_price_krw is None
    assert got.basis == "unknown"
    assert any("시세를 읽지 못해" in r for r in got.risks)


# ---------------------------------------------------------------------------
# 5. 불변조건 — 「확인 안 함」이 「위험 없음」이 되지 않게
# ---------------------------------------------------------------------------


def test_수량이_없는데_사유가_없으면_못_만든다():
    """판매 독스트링이 막으려던 바로 그 사고를 우리 쪽에서도 막는다."""
    with pytest.raises(ValueError, match="사유가 없다"):
        SupplyCapacity(None, 1000, "특", "unknown", ())


def test_무엇이_막았는지_모르는데_수량이_있으면_못_만든다():
    with pytest.raises(ValueError, match="수량"):
        SupplyCapacity(500, 1000, "특", "unknown", ("어떤 사유",))


def test_모든_경로가_불변조건을_지킨다(constraints):
    """위 케이스들을 한 번에 훑어 `__post_init__` 이 실제로 걸리는 길이 없게 한다."""
    cases = [
        (None, None),
        (None, 5_000_000),
        (5000, None),
        (0, 0),
        (5000, 5_000_000),
    ]
    for warehouse, finance in cases:
        for quotes in ([], _quotes(특=1000), _quotes(중=1600, 특=1000)):
            got = compute_supply_capacity(
                quotes=quotes,
                warehouse_free_kg=warehouse,
                finance_cap_amount_krw=finance,
                constraints=constraints,
            )
            assert isinstance(got, SupplyCapacity)
            if got.procurable_quantity_kg is None:
                assert got.risks, (warehouse, finance, quotes)
