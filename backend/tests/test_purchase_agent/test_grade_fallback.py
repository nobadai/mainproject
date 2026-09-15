"""⑤ 기준등급 폴백 — **사다리를 안 내려가되 그중 싼 것** (`#574`).

기준등급이 그날 시세에 없을 때 무엇을 살지 정하는 자리다. 전에는 `max(prices)` —
가장 비싼 등급이었고, 실데이터에서 그것이 **사다리 맨 아래**를 고른 날이 있었다.

⚠️ **여기 입력은 전부 합성이다.** mock 시세(`quotes_normal`·`quotes_wide`)는 세 등급이
다 있고 선언 기준등급 `"상"` 이 **실재해서 폴백이 아예 안 탄다** — 앵커를 돌려서는 이
경로를 한 번도 밟지 못한다. 그래서 시세 dict 를 손으로 만든다.

★ 실 경로의 수는 별도로 쟀고 `#574` 에 있다 (걷기 전수 476줄 중 44줄이 기준등급보다 낮은
등급 · 「하」 3줄 · 폴백이 탄 80일 중 기준등급 위에 등급이 둘 이상이던 날 0일).
**그 실측을 이 파일이 대신하지 않는다** — 여기가 잠그는 것은 *"규칙이 규칙대로 도는가"*
뿐이고, *"실 경로에서 몇 건인가"* 는 DB 로만 답한다.
"""

from copy import deepcopy

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.allocate_sourcing import base_grade_for


@pytest.fixture
def constraints() -> dict:
    return load_constraints()


# 2026-01-22 양파가 본 시세 (2026-01-21 · 물량가중 · 하한 1,000kg 통과분).
# 실측값 그대로다 — 「하」가 최고가라 옛 규칙이 「하」를 골랐다.
REAL_DAY = {"특": 972, "중": 992, "하": 1100}


def test_declared_reference_grade_wins_when_it_is_quoted(constraints: dict) -> None:
    """기준등급이 있으면 폴백이 아예 안 탄다 — 그게 정상 경로다."""
    prices = {"특": 1850, "상": 1650, "중": 1450}
    assert base_grade_for(prices, "상", constraints) == "상"


def test_fallback_does_not_go_below_the_ladder(constraints: dict) -> None:
    """🔴 `#574` 의 그 날. 「하」가 최고가여도 **위쪽**에서 고른다."""
    assert base_grade_for(REAL_DAY, "상", constraints) == "특"


def test_fallback_takes_the_cheapest_among_those_at_or_above(constraints: dict) -> None:
    """㉠ 규칙 — 위쪽이 여럿이면 **그중 싼 것**이다.

    ⚠️ 기준등급(`"중"`)을 시세에서 빼야 폴백이 탄다. 넣어 두면 그냥 그것을 쓴다.

    실데이터에는 아직 이런 날이 없다(폴백 80일 중 **기준등급 위에 등급이 둘 이상이던 날
    0일**). 규칙이 «가장 비싼 것»으로 조용히 되돌아가는 것을 막으려고 합성으로 잠근다.
    """
    prices = {"특": 2000, "상": 1800, "하": 900}
    assert base_grade_for(prices, "중", constraints) == "상"


def test_unknown_grades_do_not_get_a_rank(constraints: dict) -> None:
    """선언 어휘 밖 등급은 사다리에 자리가 없다 — 순위를 지어내지 않는다 (규칙 3)."""
    prices = {"등외": 5000, "특": 900, "중": 950}
    assert base_grade_for(prices, "상", constraints) == "특"


def test_everything_below_still_yields_a_grade(constraints: dict) -> None:
    """위쪽이 비면 **안을 못 만드는 대신** 아래에서 고르고 그 사실을 고지한다.

    ``None`` 을 돌려주면 그날 안이 통째로 사라진다. 「기준등급 아래를 샀다」를 적고 내는
    편이 낫다 — 고지는 ``_grade_fallback_risks`` 가 실제 등급 이름으로 낸다.
    """
    prices = {"중": 1000, "하": 1200}
    assert base_grade_for(prices, "상", constraints) == "하"


# ── 규칙 8 — 선언을 바꾸면 판정이 따라 바뀌는가 ─────────────────────────────


def test_ladder_order_is_read_from_the_declaration(constraints: dict) -> None:
    """🔴 사다리를 **뒤집으면** 폴백이 반대쪽을 골라야 한다.

    코드가 `["특","상","중","하"]` 를 박고 있으면 이 검사가 안 문다.
    """
    assert base_grade_for(REAL_DAY, "상", constraints) == "특"

    flipped = deepcopy(constraints)
    flipped["market_quotes"]["grades"] = ["하", "중", "상", "특"]
    # 뒤집으면 「상」 위쪽이 `하`·`중` 이 되고, 그중 싼 것은 `중`(992) 이다.
    # 🔴 「하」가 아니다 — ㉠ 은 위쪽에서 **싼 것**을 고르지 맨 위를 고르지 않는다.
    assert base_grade_for(REAL_DAY, "상", flipped) == "중"


def test_reference_grade_is_read_from_the_declaration(constraints: dict) -> None:
    """기준등급을 바꾸면 사다리에서 자르는 위치가 따라 움직인다.

    ⚠️ 두 경우 다 기준등급이 시세에 **없어야** 폴백이 탄다.
    """
    without_mid = {"특": 2000, "상": 1800, "하": 900}
    assert base_grade_for(without_mid, "중", constraints) == "상"

    without_low = {"특": 2000, "상": 1800, "중": 1000}
    assert base_grade_for(without_low, "하", constraints) == "중"


def test_declared_vocabulary_order_is_high_grade_first(constraints: dict) -> None:
    """선언 자체를 잠근다 — 목록이 뒤섞이면 폴백이 조용히 아래를 고른다.

    ⚠️ 값 대조가 아니다. **순서가 사다리라는 계약**을 잠그는 것이고, 어휘가 늘어나면
    (`#69` 가 등급을 다시 가르는 날) 이 검사가 먼저 걸려야 맞다.
    """
    grades = load_constraints()["market_quotes"]["grades"]
    assert grades == ["특", "상", "중", "하"], (
        "높은 등급부터여야 한다 — base_grade_for 가 이 순서를 사다리로 읽는다"
    )


# ── ③ 과 ⑤ 는 **갈렸다** — 같은 값으로 묶지 않는다 ──────────────────────────


def test_the_two_fallbacks_deliberately_disagree(constraints: dict) -> None:
    """🔴 ③ 의 `max` 와 ⑤ 의 사다리 폴백이 **다른 답을 내는 것이 맞다** (`#574`).

    두 자리는 `68fa68f` → `a1a03c5` 로 문장과 식이 복사되며 같아졌는데 뜻은 반대였다.

    ```text
    ③ reference_unit_price   쓰이는 곳이 cash_cap_kg 하나 — 높게 잡아야 보수적이다
    ⑤ base_grade_for         무엇을 살지 정한다 — 사다리를 안 내려가야 한다
    ```

    ⚠️ 둘을 같은 값으로 잠그는 검사를 두면 한쪽을 고칠 때 다른 쪽이 따라가야 하는 것처럼
      보인다. **지금은 값이 달라야 맞다.** 그래서 「갈렸다」를 잠근다.
    """
    from app.purchase_agent.nodes.draft_plan import reference_unit_price

    quotes = [{"market": "가락", "grade": g, "price": p} for g, p in REAL_DAY.items()]
    estimated = reference_unit_price(quotes, "상")
    bought = base_grade_for(REAL_DAY, "상", constraints)

    assert estimated == 1100, "③ 은 상한 계산용이라 가장 비싼 값을 그대로 쓴다"
    assert bought == "특", "⑤ 는 무엇을 살지라 사다리 위에서 고른다"
    assert REAL_DAY[bought] != estimated, "두 자리가 같은 답을 내면 한쪽이 되돌아간 것이다"
