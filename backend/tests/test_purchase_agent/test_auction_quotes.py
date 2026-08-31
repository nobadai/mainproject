"""#70 — 등급별 당일 경락가를 ``auction_prices_daily``에서 읽는다.

**대부분이 DB 없이 돈다.** 조회 결과를 시세 형태로 옮기는 부분(물량가중·반올림·정렬·등급
어휘·규격 필터·품종 배제)은 가짜 ``fetch``를 꽂으면 전부 시험된다. 실제 연결이 필요한 것만
``@pytest.mark.db``로 분리했다 — 스위트가 DB에 묶이면 사내망 밖에서 전원 빨간불이 되고,
그러면 아무도 스위트를 안 믿는다 (2026-08-31에 LLM 쪽에서 겪은 그것과 같은 상황).

숫자는 **2026-08-31 실측**이다. 지어낸 값이 아니라 DB에서 나온 값이라, 식이 틀어지면
여기서 먼저 드러난다.
"""

import inspect
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.purchase_agent import db, mocks, ports, quotes
from app.purchase_agent.config import CONSTRAINTS_PATH, load_constraints
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.nodes.draft_plan import draft_plan as _draft_plan
from app.purchase_agent.nodes.draft_plan import fixed_market_quotes
from app.purchase_agent.quotes import (
    auction_quote_source,
    krw_per_kg,
    missing_quote_reason,
    observed_date,
    observed_spec,
    provenance_problem,
    spec_for_item,
    stale_quote_reason,
    staleness_days,
    to_price,
)

INTEGRATION = date(2025, 12, 31)
ANCHORS = (
    date(2025, 12, 31),
    date(2026, 8, 21),
    date(2026, 8, 28),
    date(2026, 9, 4),
    date(2026, 9, 11),
)

#: 2026-08-03 배추 특 · 서울가락 · **규격 무필터** 실측 (23행 중 상위 4행).
#: 전체 합은 거래중량 410,960kg · 거래대금 385,700,450원 → 물량가중 938.5원/kg.
#: 같은 집합의 단순평균은 2,009.9원/kg — **2.1배** 갈린다. 지환님이 #70에 남긴 그 값이다.
DOD_TOTAL_VOLUME_KG = Decimal(410960)
DOD_TOTAL_AMOUNT_KRW = Decimal(385700450)
DOD_WEIGHTED = Decimal("938.5")
DOD_SIMPLE_AVERAGE = Decimal("2009.9")

#: 2025-12-31 배추 특 · 서울가락 · 그물망/파렛트 10kg 실측 (관통일).
#: 쿼리가 as_of 이전 최신 거래일 하루를 고르므로 관측일은 12-30 이다 (12-31 이 아니다).
OBSERVED = "2025-12-30"
BAECHU_1231_ROWS = [
    {"observed_date": OBSERVED, "grade": "특",
     "amount_krw": Decimal(247255700), "volume_kg": Decimal(265420)},
    {"observed_date": OBSERVED, "grade": "특",
     "amount_krw": Decimal(9641800), "volume_kg": Decimal(9940)},
]


def _flat(query: str) -> str:
    """쿼리 문자열을 공백 하나로 눌러 비교한다 — 들여쓰기가 바뀌었다고 검사가 깨지면
    검사가 SQL 의 **뜻**이 아니라 모양을 잠그고 있는 것이다."""
    return " ".join(query.split())


def _fetch_returning(rows: list[dict[str, Any]], captured: dict | None = None):
    """가짜 조회. 넘어간 쿼리·파라미터를 그대로 잡아둔다."""

    def fetch(query, params=None):
        if captured is not None:
            raw = query.as_string(None) if hasattr(query, "as_string") else str(query)
            captured["query"] = _flat(raw)
            captured["params"] = params
        return rows

    return fetch


#: "선언 자체가 없다"를 값으로 표현한다 — ``None`` 은 그 자리에 쓸 수 있는 값이라 못 쓴다.
_ABSENT = object()


def _source(rows: list[dict[str, Any]], captured: dict | None = None):
    return auction_quote_source(fetch=_fetch_returning(rows, captured))


# --------------------------------------------------------------------- 물량가중 식


def test_weighted_price_is_amount_over_volume_not_a_simple_average() -> None:
    """🔴 DoD — 2026-08-03 배추 특 물량가중 938.5. 단순평균(2,009.9)과 2.1배 갈린다.

    ``grade_unit_price``가 사중 일치 금액 축에 직접 걸리므로, 식을 단순평균으로 바꾸면
    총액이 두 배 어긋난 채 **스키마는 통과한다**. 그 구간을 여기서 막는다.
    """
    weighted = krw_per_kg(DOD_TOTAL_AMOUNT_KRW, DOD_TOTAL_VOLUME_KG)

    assert weighted.quantize(Decimal("0.1")) == DOD_WEIGHTED
    # 같은 실측 집합의 단순평균과 **다르다**는 것까지 못 박는다 — 값 하나만 보면
    # 우연히 맞을 수 있지만, 두 값의 거리는 우연으로 만들어지지 않는다.
    assert weighted < DOD_SIMPLE_AVERAGE / 2


def test_weighted_price_refuses_to_divide_by_zero_volume() -> None:
    """거래중량 0은 "단가 0원"이 아니라 **잴 수 없는 행**이다 (규칙 3)."""
    with pytest.raises(ValueError, match="거래중량"):
        krw_per_kg(Decimal(1000), Decimal(0))


def test_rounding_is_half_up_not_bankers() -> None:
    """⚠️ 내장 ``round()``는 은행가 반올림이라 ``round(938.5) == 938``이다.

    DoD 재현치가 하필 ``.5``로 끝나는 값이라 이 차이가 그대로 드러난다.
    """
    assert to_price(Decimal("938.5")) == 939
    assert to_price(Decimal("937.5")) == 938  # 내장 round였다면 938 — 여긴 우연히 같다
    assert to_price(Decimal("710.4")) == 710


# --------------------------------------------------------------------- 좌표·규격


def test_query_filters_by_spec_and_the_params_carry_it() -> None:
    """🔴 규격 고정이 실제 쿼리에 실린다.

    규격을 안 잠그면 스프레드가 등급이 아니라 **포장 크기**를 잰다 — 2025-12-31 배추
    규격 무필터로 특 1,107원 · 상 1,499원이라 스프레드가 −35.4%가 되고, 확대 판정이
    "확대 아님"으로 조용히 뒤집힌다.
    """
    captured: dict = {}
    _source(BAECHU_1231_ROWS, captured)("배추", INTEGRATION)

    assert "package_name" in captured["query"]
    assert "unit_weight_kg" in captured["query"]
    assert captured["params"]["packages"] == ["그물망", "파렛트"]
    assert captured["params"]["unit_weight_kg"] == 10


def test_query_pins_market_grades_and_the_as_of_date() -> None:
    """나머지 좌표도 파라미터로 실린다 — 시장·등급 어휘·날짜."""
    captured: dict = {}
    _source(BAECHU_1231_ROWS, captured)("배추", INTEGRATION)

    assert captured["params"]["market_category"] == "가락"
    assert captured["params"]["item"] == "배추"
    assert captured["params"]["as_of"] == INTEGRATION
    assert captured["params"]["grades"] == ["특", "상", "중", "하"]


@pytest.mark.parametrize("token", ["subclass_code", "subclass_name", "subclass"])
def test_variety_is_never_used_in_the_query(token: str) -> None:
    """🔴 품종을 고르지 않는다 (8/28 결정) — WHERE에도 GROUP BY에도 없다.

    물량가중이 품종 축을 자동으로 정리한다. 하나라도 쓰는 순간 그 결정이 깨지고,
    ML 수집·학습표와 축이 어긋난다.
    """
    captured: dict = {}
    _source(BAECHU_1231_ROWS, captured)("배추", INTEGRATION)

    assert token not in captured["query"]


def test_variety_is_absent_from_the_whole_module() -> None:
    """쿼리 한 줄만 보면 다른 함수가 몰래 품종을 거를 수 있다 — 파일 전체를 본다."""
    source_text = Path(inspect.getfile(quotes)).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source_text.splitlines() if not line.lstrip().startswith("#")
    )
    # docstring 안의 설명 문장은 제외해야 하므로, 실제 컬럼 참조 형태만 본다.
    assert "subclass_code" not in code.split('"""')[-1]
    assert "subclass_name" not in code.split('"""')[-1]


def test_grades_outside_the_declared_vocabulary_are_dropped() -> None:
    """``.``·5등·등외는 등급이 아니라 잡음이다. 양파는 그런 행이 하루 여섯 줄씩 온다."""
    rows = [
        {"observed_date": OBSERVED, "grade": "특",
         "amount_krw": Decimal(100000), "volume_kg": Decimal(100)},
        {"observed_date": OBSERVED, "grade": ".",
         "amount_krw": Decimal(999999), "volume_kg": Decimal(100)},
        {"observed_date": OBSERVED, "grade": "5등",
         "amount_krw": Decimal(999999), "volume_kg": Decimal(100)},
    ]
    result = _source(rows)("배추", INTEGRATION)

    assert [quote["grade"] for quote in result] == ["특"]


def test_grades_come_out_in_the_declared_order() -> None:
    """⑥의 근거 문장이 ``market_quotes[0]``을 대표값으로 읽는다 — 순서가 흔들리면
    같은 날 근거 문구가 달라진다."""
    rows = [
        {"observed_date": OBSERVED, "grade": "중",
         "amount_krw": Decimal(100000), "volume_kg": Decimal(100)},
        {"observed_date": OBSERVED, "grade": "특",
         "amount_krw": Decimal(300000), "volume_kg": Decimal(100)},
        {"observed_date": OBSERVED, "grade": "상",
         "amount_krw": Decimal(200000), "volume_kg": Decimal(100)},
    ]
    result = _source(rows)("배추", INTEGRATION)

    assert [quote["grade"] for quote in result] == ["특", "상", "중"]


def test_the_real_1231_rows_become_one_weighted_quote() -> None:
    """관통일 배추 — 두 규격 행이 한 등급 한 줄로 **합산되어** 933원이 된다.

    🔴 행을 합치지 않고 하나를 집으면 970원이 나온다(파렛트 10kg 행). 에러가 없고 값도
      그럴듯해서 아무도 모른다 — 4% 어긋난 단가가 사중 일치 금액 축을 그대로 타고 나간다.
    """
    result = _source(BAECHU_1231_ROWS)("배추", INTEGRATION)

    assert result == [
        {
            "market": "가락",
            "grade": "특",
            "price": 933,
            "spec": "그물망·파렛트 10kg",
            "observed_date": OBSERVED,
        }
    ]


# --------------------------------------------------------------------- 피마늘 (규칙 3)


def test_item_without_a_settled_spec_is_not_queried_at_all() -> None:
    """피마늘은 조회 규격이 미확정이다 — **아무 규격으로나 물어보지 않는다** (규칙 3).

    물어보면 값이 오고, 그 값은 우리가 뜻한 시리즈가 아니다.
    """
    calls: list = []

    def fetch(query, params=None):
        calls.append(params)
        return []

    result = auction_quote_source(fetch=fetch)("피마늘", INTEGRATION)

    assert result == []
    assert calls == []


def test_spec_for_item_returns_none_for_the_unsettled_item() -> None:
    constraints = load_constraints()

    assert spec_for_item("피마늘", constraints) is None
    assert spec_for_item("배추", constraints)["unit_weight_kg"] == 10


def test_every_purchase_item_is_declared_even_when_the_answer_is_null() -> None:
    """4품목이 모두 표에 있어야 한다. 키가 **없는** 것과 값이 **null**인 것은 다르다 —
    없으면 "빠뜨렸다"이고 null이어야 "미결이라 안 읽는다"가 된다."""
    spec_by_item = load_constraints()["market_quotes"]["spec_by_item"]

    assert set(mocks.ITEMS) <= set(spec_by_item)
    assert spec_by_item["피마늘"] is None


# --------------------------------------------------------------------- read-only 경계


@pytest.mark.parametrize("name", ["execute", "execute_many", "execute_returning_one", "commit"])
def test_the_purchase_db_module_has_no_write_helper(name: str) -> None:
    """🔴 규칙 2 — 매입은 read-only다. 다른 파트의 ``db.py``를 복사해 오면 쓰기 함수가
    따라오고, **있기만 해도** 규칙이 "지켜지고 있다"에서 "깨질 수 있다"로 내려간다."""
    assert not hasattr(db, name)


def test_the_purchase_db_module_exposes_only_read_helpers() -> None:
    public = {
        name
        for name, value in vars(db).items()
        # ``value.__module__`` 로 거른다 — 이게 없으면 ``load_dotenv``·``dict_row`` 같은
        # **import 해온 이름**까지 세어서, 정작 쓰기 함수가 추가돼도 목록 비교가 늘 깨진 채
        # 있느라 아무것도 못 잡는다.
        if inspect.isfunction(value)
        and not name.startswith("_")
        and value.__module__ == db.__name__
    }

    # ``get_db_schema`` 가 없는 것이 계약이다 — 읽을 스키마는 ``market_quotes.source``
    # 가 정한다. 헬퍼가 있으면 다음 사람이 그걸로 배선하고 ``.env`` 가 테이블을 고른다.
    assert public == {"fetch_all", "fetch_one", "get_connection"}


# --------------------------------------------------------------------- 주입 (환경변수 아님)


def test_mock_stays_the_default_so_the_suite_never_touches_the_database() -> None:
    """``source``를 안 주면 mock 이다 — 회귀 테스트 전량이 그 길을 밟는다."""
    assert ports.get_market_quotes("배추", date(2026, 8, 21)) == mocks.load_quotes(
        "배추", date(2026, 8, 21)
    )


def test_an_injected_source_replaces_the_mock_entirely() -> None:
    injected = [{"market": "가락", "grade": "특", "price": 933}]

    assert ports.get_market_quotes("배추", INTEGRATION, source=lambda i, d: injected) == injected


@pytest.mark.parametrize("anchor", ANCHORS)
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_mock_never_returns_an_empty_quote_list(item: str, anchor: date) -> None:
    """``missing_quote_reason``이 규격을 이름으로 말해도 안전한 **전제**를 잠근다.

    mock 이 빈 목록을 돌려주는 앵커가 하나라도 생기면, 그 사유가 mock 경로에서 나가면서
    쓰지도 않은 규격을 썼다고 적게 된다 — 형식만 맞고 내용이 거짓인 사유다.
    """
    assert ports.get_market_quotes(item, anchor)


# --------------------------------------------------------------------- 휴장일 (결정 d)


def _no_quotes(item: str, as_of: date) -> list[dict]:
    """휴장일 — 2026-01-01·01-02 가락은 실제로 0행이다 (신정)."""
    return []


def test_a_market_holiday_does_not_kill_the_agent() -> None:
    """🔴 결정 d — 죽지 않는다. 죽으면 오케스트레이터가 **원인을 모른다**."""
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=_no_quotes)

    assert proposal["scenarios"] == []
    assert proposal["rejected_reasons"]


def test_a_market_holiday_says_it_is_the_auction_not_our_stock() -> None:
    """사유가 "재고가 없다"로 읽히면 안 된다 — 규격과 시장·기간을 함께 적는다."""
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=_no_quotes)
    reason = proposal["rejected_reasons"][0]["reason"]

    assert "그물망·파렛트 10kg 규격" in reason
    assert "보유 재고와는 무관" in reason
    assert proposal["no_proposal_reason"]


def test_a_market_holiday_leaves_one_reason_per_plan_label() -> None:
    """소비자는 "보수는 왜 없나"를 안별로 묻는다."""
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=_no_quotes)

    assert [row["label"] for row in proposal["rejected_reasons"]] == ["보수", "기본", "공격"]


def test_the_unsettled_item_gets_its_own_reason_not_the_holiday_one() -> None:
    """피마늘이 빈 목록인 이유는 휴장이 아니라 **규격 미확정**이다. 둘을 같은 말로 적으면
    없는 원인을 보고하는 것이다."""
    reason = missing_quote_reason("피마늘", "2025-12-31", load_constraints())

    assert reason.startswith("피마늘 조회 규격이 아직 정해지지 않아")
    assert "휴장" not in reason
    # 조사를 붙이면 "피마늘는"이 된다 — 실제로 한 번 나갔다 (2026-08-31 관통).
    assert "피마늘는" not in reason


def test_the_empty_quote_guard_still_refuses_to_compute() -> None:
    """``require_non_empty``를 없앤 것이 아니다 — 사유를 낼 수 있는 자리에서 먼저 낼 뿐,
    가드 자체는 남아 "빈 값으로 조용히 계산"을 여전히 막는다."""
    with pytest.raises(ValueError, match="is empty"):
        fixed_market_quotes([])


# --------------------------------------------------------------------- 사유가 규격을 말한다


def test_a_missing_partner_grade_names_the_spec_it_looked_at() -> None:
    """🔴 배추는 규격 고정 시 특·상이 함께 잡히는 날이 2025년 300일 중 **21일**뿐이다.

    그 사유가 "재고에 상이 없다"로 읽히면 재고를 확인하러 가는 사람이 생긴다.
    """
    only_top = [
        {"market": "가락", "grade": "특", "price": 824,
         "spec": "그물망·파렛트 10kg", "observed_date": "2025-12-30"}
    ]
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: only_top)

    blocked = [risk for scenario in proposal["scenarios"] for risk in scenario["risks"]]
    said = [text for text in blocked if "등급 거래가 없다" in text]
    assert said, blocked
    assert "그물망·파렛트 10kg 규격" in said[0]
    assert "보유 재고가 아니라" in said[0]
    # ⚠️ as_of(12-31)가 아니라 **관측일(12-30)**을 말해야 한다 — 12-31 은 열리지도 않았다.
    assert "2025-12-30" in said[0]
    assert "2025-12-31" not in said[0]


def test_the_spec_in_the_reason_comes_from_the_data_not_from_constraints() -> None:
    """규격을 constraints에서 다시 읽으면 mock 경로에서 **쓰지도 않은 규격**을 적게 된다."""
    assert observed_spec([{"market": "가락", "grade": "특", "price": 1650}]) is None
    assert observed_spec([{"grade": "특", "spec": "상자·파렛트 20kg"}]) == "상자·파렛트 20kg"


def test_mock_path_keeps_its_own_wording_without_a_spec() -> None:
    """mock 은 규격 표기가 없다 — 그날 시세 그대로 말하고 없는 규격을 지어내지 않는다."""
    only_top = [{"market": "가락", "grade": "특", "price": 1850}]  # 표기 없음 = mock
    proposal = run_purchase_agent("배추", date(2026, 8, 21), quotes=lambda i, d: only_top)

    said = [
        risk
        for scenario in proposal["scenarios"]
        for risk in scenario["risks"]
        if "등급 거래가 없다" in risk
    ]
    assert said
    assert "규격" not in said[0]
    assert "당일 시세" in said[0]


# --------------------------------------------------------------------- 실 DB (마커 분리)


@pytest.mark.db
def test_dod_weighted_price_reproduces_the_measured_value() -> None:
    """🔴 DoD — 2026-08-03 배추 특 · 가락 · **규격 무필터** → 938.5 / 2,009.9.

    우리 조회 경로는 규격을 고정하지만, 이 테스트는 **식 자체**가 지환님 실측과 같은지를
    본다. 규격 필터를 빼고 같은 집합을 만들어 물량가중과 단순평균을 나란히 잰다.
    """
    row = db.fetch_one(
        """
        SELECT round(sum(trade_amount_krw) / nullif(sum(trade_volume_kg), 0), 1) AS weighted,
               round(avg(avg_auction_price_krw_per_kg), 1) AS simple_average
          FROM source_raw.auction_prices_daily
         WHERE item_name = '배추' AND market_category = '가락' AND grade_name = '특'
           AND auction_date = DATE '2026-08-03'
        """
    )

    assert row["weighted"] == DOD_WEIGHTED
    assert row["simple_average"] == DOD_SIMPLE_AVERAGE


@pytest.mark.db
def test_the_source_reads_the_prior_trading_day_from_the_real_table() -> None:
    """관통일 기준 **직전 거래일(12-30)** 배추 — 특 824원 하나.

    12-31 당일은 933원이지만 아침에는 그 값이 없다. 상·중은 12-30 에도 그 규격에서
    낙찰되지 않았다.
    """
    result = auction_quote_source()("배추", INTEGRATION)

    assert result == [
        {
            "market": "가락",
            "grade": "특",
            "price": 824,
            "spec": "그물망·파렛트 10kg",
            "observed_date": "2025-12-30",
        }
    ]


@pytest.mark.db
@pytest.mark.parametrize(
    ("item", "expected"),
    # 12-30 물량가중 정수 원/kg. 12-31 당일값(933 · 729 · 1097)이 아니다.
    [("배추", 824), ("무", 736), ("양파", 1106)],
)
def test_real_prices_now_sit_under_the_forecast_ceiling(item: str, expected: int) -> None:
    """🔴 12-31 관통이 0안이던 원인 — mock 시세가 실데이터 ``max_price``를 넘었다.

    실측 상한(D=2): 배추 992 · 무 795 · 양파 1,152. mock 상 등급은 1,650 · 1,100 · 1,300
    이라 셋 다 컷됐다. 직전 거래일 경락가로 바꾸면 셋 다 상한 아래다.
    """
    ceilings = {"배추": 992, "무": 795, "양파": 1152}
    result = auction_quote_source()(item, INTEGRATION)
    top = next(quote for quote in result if quote["grade"] == "특")

    assert top["price"] == expected
    assert top["price"] < ceilings[item]
    assert mocks.load_quotes(item, INTEGRATION)[1]["price"] > ceilings[item]


@pytest.mark.db
def test_the_real_source_never_returns_the_as_of_day_itself() -> None:
    """look-ahead 방어가 실제 조회에서도 성립하는지 — 아침엔 당일 값이 없다."""
    for item in ("배추", "무", "양파"):
        result = auction_quote_source()(item, INTEGRATION)
        assert result, item
        assert observed_date(result) < INTEGRATION.isoformat(), item


@pytest.mark.db
def test_the_real_radish_query_uses_18kg_before_2018() -> None:
    """2017년 무를 20kg 로 고정하면 244 거래일 중 64일만 잡힌다 (18kg 는 227일)."""
    result = auction_quote_source()("무", date(2017, 6, 2))

    assert result
    assert result[0]["spec"] == "상자·파렛트 18kg"
    assert observed_date(result) < "2018-01-01"


@pytest.mark.db
def test_a_real_market_holiday_still_reaches_back_but_says_how_far() -> None:
    """2026-01-01·01-02는 신정 휴장이다. 직전 거래일(2025-12-31)까지 거슬러 가되
    **며칠 전인지를 관측일이 말한다** — staleness 검사가 그 값으로 판정한다."""
    result = auction_quote_source()("배추", date(2026, 1, 3))

    assert result
    assert observed_date(result) == "2025-12-31"
    assert staleness_days(result, "2026-01-03") == 3


# --------------------------------------------------------------------- 읽을 수 없는 값


def test_a_null_aggregate_drops_the_grade_instead_of_crashing() -> None:
    """금액·중량 컬럼이 nullable 이다(사본에서 NOT NULL 이 풀렸다). 어느 등급의 값이 전부
    NULL 이면 ``sum()``이 NULL 이고, 그대로 ``Decimal(None)``을 부르면 죽는다.

    2026-08-31 실측으로는 가락 164,046행에 NULL 이 하나도 없다 — 그래도 막는 이유는
    적재되는 값이라 우리가 통제하지 못하고, **죽는 쪽은 사유를 못 내기** 때문이다.
    """
    rows = [
        {"observed_date": OBSERVED, "grade": "특",
         "amount_krw": Decimal(100000), "volume_kg": Decimal(100)},
        {"observed_date": OBSERVED, "grade": "상", "amount_krw": None, "volume_kg": Decimal(100)},
    ]
    result = _source(rows)("배추", INTEGRATION)

    assert [quote["grade"] for quote in result] == ["특"]


def test_all_grades_unreadable_becomes_the_zero_quote_path() -> None:
    """읽을 수 있는 등급이 하나도 없으면 빈 목록이다 — ③이 사유를 남기고 0안으로 끝낸다."""
    rows = [{"observed_date": OBSERVED, "grade": "특", "amount_krw": None, "volume_kg": None}]

    assert _source(rows)("배추", INTEGRATION) == []


def test_the_zero_quote_reason_speaks_about_the_whole_lookback_not_one_day() -> None:
    """🔴 쿼리가 ``auction_date < as_of`` 로 **전 기간**을 훑어 최신일을 고른다.

    그래서 빈 결과는 "그날 휴장"이 될 수 **없다** — as_of 이전 어느 날에도 그 좌표로
    쓸 수 있는 기록이 없다는 뜻이다. 쿼리를 바꾸면서 사유를 안 바꿔 한동안 틀린 말을 하고
    있었다 (Codex 2차 지적).
    """
    reason = missing_quote_reason("배추", "2025-12-31", load_constraints())

    assert "2025-12-31 이전 기간에" in reason
    assert "휴장" not in reason
    assert "보유 재고와는 무관" in reason


def test_the_zero_quote_reason_names_the_spec_that_day_actually_used() -> None:
    """🔴 2017년 무는 18kg 로 조회하는데 사유가 "20kg 규격에서"라고 말하고 있었다."""
    reason = missing_quote_reason("무", "2017-06-02", load_constraints())

    assert "상자·파렛트 18kg" in reason
    assert "20kg" not in reason


def test_a_non_positive_price_drops_the_grade() -> None:
    """``grade_unit_price``는 스키마가 ``gt=0``이라 0 이하가 한 줄만 섞여도 **제안 전체**가
    출력 경계에서 죽는다. 그 등급 하나를 빼는 쪽이 맞다."""
    rows = [
        {"observed_date": OBSERVED, "grade": "특",
         "amount_krw": Decimal(100000), "volume_kg": Decimal(100)},
        {"observed_date": OBSERVED, "grade": "상",
         "amount_krw": Decimal(0), "volume_kg": Decimal(100)},
    ]
    result = _source(rows)("배추", INTEGRATION)

    assert [quote["grade"] for quote in result] == ["특"]


# --------------------------------------------------------------------- 좌표 형태 계약


@pytest.mark.parametrize("item", ["배추", "무", "양파"])
def test_a_declared_spec_is_complete_enough_to_query_with(item: str) -> None:
    """규격이 반쯤 적힌 상태를 막는다 — ``packages``만 있고 ``unit_weight_kg``이 없으면
    조회 시점에 ``KeyError``로 죽고, 그때는 사유를 낼 자리가 이미 지나갔다."""
    spec = spec_for_item(item, load_constraints())

    assert isinstance(spec["packages"], list) and spec["packages"]
    assert all(isinstance(name, str) and name for name in spec["packages"])
    assert isinstance(spec["unit_weight_kg"], int | float)
    assert spec["unit_weight_kg"] > 0
    assert isinstance(spec["label"], str) and spec["label"]


def test_the_declared_grade_vocabulary_is_a_non_empty_list_of_names() -> None:
    grades = load_constraints()["market_quotes"]["grades"]

    assert grades and all(isinstance(grade, str) and grade for grade in grades)


# --------------------------------------------------------------------- 앞 사유를 지우지 않는다


def test_the_zero_quote_path_keeps_reasons_left_by_earlier_nodes() -> None:
    """🔴 노드 반환값은 State의 같은 키를 **통째로 대체**한다.

    지금은 ③이 ``rejected_reasons``를 처음 쓰는 노드라 결과가 같지만, ②가 사유를 남기게
    되는 날 앞의 것이 조용히 지워진다 — ⑥이 ``[*state[...], *dropped]``로 쓰는 것과 같은
    이유로 여기서도 이어 붙인다.
    """
    earlier = {"label": "보수", "reason": "앞 노드가 남긴 사유"}
    state = {
        "item": "배추",
        "date": "2025-12-31",
        "situation": "stable",
        "market_quotes": [],
        "inventory": {"lots": []},
        "confirmed_orders": {"total_kg": 1000, "orders": []},
        "rejected_reasons": [earlier],
    }
    result = _draft_plan(state)

    assert result["rejected_reasons"][0] == earlier
    assert len(result["rejected_reasons"]) == 4  # 앞 1건 + 라벨 3건


# --------------------------------------------------------------------- 🔴1 행 단위 필터


@pytest.mark.parametrize(
    "token",
    ["trade_volume_kg > 0", "trade_amount_krw IS NOT NULL", "trade_amount_krw >= 0"],
)
def test_unusable_rows_are_excluded_before_aggregation(token: str) -> None:
    """🔴 ``sum()``은 NULL 을 건너뛰는데 다른 컬럼의 합계에는 그 행이 들어간다.

    집계 뒤에 막으면 분자·분모가 서로 다른 행 집합에서 나오고, **에러 없이 단가가 2배
    틀린다** (SQL 재현: (1000,10)+(NULL,10) → 50, (1000,10)+(1000,0) → 200, 정답 100).
    한번 합쳐지면 복구가 불가능하므로 필터가 WHERE 에 있어야 한다.
    """
    captured: dict = {}
    _source(BAECHU_1231_ROWS, captured)("배추", INTEGRATION)

    assert token in captured["query"]


def test_the_row_filter_sits_in_where_not_having() -> None:
    """같은 뜻의 검사를 WHERE·HAVING 두 곳에 두면 한쪽만 바뀐다."""
    captured: dict = {}
    _source(BAECHU_1231_ROWS, captured)("배추", INTEGRATION)
    query = captured["query"]

    assert "HAVING" not in query
    assert query.index("trade_volume_kg > 0") < query.index("GROUP BY")


# --------------------------------------------------------------------- 🔴3 좌표 잠금


@pytest.mark.parametrize(
    ("key", "wrong"),
    [("weighting", "simple"), ("unit", "원/개"), ("price_kind", "WHSL")],
)
def test_a_declaration_the_code_does_not_implement_stops_the_query(
    key: str, wrong: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 선언만 있고 아무도 안 읽는 값은 **단일 소스인 척하는 주석**이다.

    ``weighting``을 ``simple``로 바꿔도 계산이 그대로면 YAML 은 사실이 아니라 사실처럼
    보이는 글자다. 바꾼 줄 알고 쓰는 것이 최악이라 조회를 멈춘다.
    """
    constraints = load_constraints()
    constraints["market_quotes"][key] = wrong
    monkeypatch.setattr("app.purchase_agent.quotes.load_constraints", lambda: constraints)

    with pytest.raises(ValueError, match="좌표 선언이 구현과 다르다"):
        _source(BAECHU_1231_ROWS)("배추", INTEGRATION)


def test_the_fixed_market_constant_reads_the_declared_coordinate(tmp_path: Path) -> None:
    """🔴 같은 사실이 두 곳에 있으면 YAML 만 바꿨을 때 정상 조회된 시세가 하류 필터에서
    전부 떨어져 **"가락 휴장"**으로 보고된다. 값이 아니라 사유가 틀리는 고장이다.

    ⚠️ **값 비교로는 이걸 증명할 수 없다.** ``FIXED_MARKET == constraints[...]`` 는 상수를
      다시 하드코딩해도 통과한다(둘 다 "가락"이라서) — 실제로 변이가 안 물렸다. 선언을
      **바꿔보고** 달라지는지를 봐야 한다.

    새 인터프리터에서 돌리는 이유: ``schemas`` 를 이 프로세스에서 reload 하면 이미 바인딩된
    클래스 객체와 갈라져 다른 테스트가 조용히 오염된다.
    """
    swapped = tmp_path / "constraints.yaml"
    original = CONSTRAINTS_PATH.read_text(encoding="utf-8")
    swapped.write_text(
        original.replace('market_category: "가락"', 'market_category: "부산"', 1),
        encoding="utf-8",
    )
    probe = (
        "from app.purchase_agent import config\n"
        f"config.CONSTRAINTS_PATH = __import__('pathlib').Path({str(swapped)!r})\n"
        "from app.purchase_agent import schemas\n"
        "print(schemas.FIXED_MARKET)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,  # 실패하는 게 이 테스트의 기대값이다
        capture_output=True,
        text=True,
        cwd=Path(inspect.getfile(quotes)).parents[2],
    )

    assert result.returncode != 0, f"선언을 바꿨는데 그대로 돌았다: {result.stdout!r}"
    assert "출력 계약 Market" in result.stderr


# --------------------------------------------------------------------- 🟡2 키 부재 vs null


def test_a_missing_item_key_is_not_treated_like_a_declared_null() -> None:
    """실수로 지운 품목이 피마늘과 같은 처분을 받으면, 그 품목은 그날부터 영원히 시세
    없이 돈다 — 아무도 모른 채."""
    constraints = load_constraints()
    del constraints["market_quotes"]["spec_by_item"]["배추"]

    with pytest.raises(KeyError, match="null 로 둔다"):
        spec_for_item("배추", constraints)


@pytest.mark.parametrize(
    "broken",
    [
        {"packages": "그물망", "unit_weight_kg": 10, "label": "x"},  # 문자열 → 글자 목록
        {"packages": [], "unit_weight_kg": 10, "label": "x"},
        {"packages": ["그물망"], "unit_weight_kg": 0, "label": "x"},
        {"packages": ["그물망"], "unit_weight_kg": 10, "label": ""},
        {"packages": ["그물망"], "label": "x"},  # unit_weight_kg 누락
    ],
)
def test_a_half_written_spec_is_refused_where_a_reason_can_still_be_given(
    broken: dict,
) -> None:
    """조회 시점에 ``KeyError``로 죽으면 사유를 낼 자리가 이미 지나갔다.

    ``packages``에 문자열을 넘기면 ``list("그물망")``이 글자 목록이 되어 **조용히 0건**이
    되는 것이 특히 나쁘다 — 에러도 없이 "그날 거래가 없었다"가 된다.
    """
    constraints = load_constraints()
    constraints["market_quotes"]["spec_by_item"]["배추"] = broken

    with pytest.raises((ValueError, TypeError)):
        spec_for_item("배추", constraints)


# --------------------------------------------------------------------- 🟡1 주입 계약


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ([{"grade": "특", "price": 933}], "필수 키 없음"),
        ([{"market": "부산", "grade": "특", "price": 933}], "가락 고정"),
        ([{"market": "가락", "grade": "특", "price": 0}], "양의 정수"),
        ([{"market": "가락", "grade": "특", "price": 933.5}], "양의 정수"),
        (["가락 933"], "매핑이어야"),
    ],
)
def test_an_injected_source_that_breaks_the_contract_is_caught_at_the_port(
    bad: list, match: str
) -> None:
    """``check_prices_exist``는 **주입 원본과 대조**하므로 원본이 틀리면 같이 틀린 채
    통과한다. 그래서 경계가 포트여야 한다.

    특히 ``market="부산"``은 하류 필터가 전부 떨어뜨려 **계약 위반이 "휴장"으로 둔갑**한다.
    """
    with pytest.raises((ValueError, TypeError), match=match):
        ports.get_market_quotes("배추", INTEGRATION, source=lambda i, d: bad)


# --------------------------------------------------------------------- 🟡3 출처 표기


def test_real_auction_quotes_are_labelled_official_not_mock() -> None:
    """🔴 실 DB 시세인데 ``SIM_FIXED`` · "(mock)"으로 나가면 **출처를 거짓으로 표시**하는
    것이다. Critic·H1 이 읽는 값이라 "데이터는 시뮬레이션 / 실행은 실제" 구분이 무너진다.

    ``MEASURED``가 아닌 이유: 그건 마스터의 입력 등급 어휘다. 우리 사다리는
    OFFICIAL > VENDOR > SIM_FIXED > ASSUMED 이고, 가락 경락 실적은 공영도매시장의 공식
    거래 기록이라 ``OFFICIAL`` 이 맞는 자리다.
    """
    real = [
        {"market": "가락", "grade": "특", "price": 824,
         "spec": "그물망·파렛트 10kg", "observed_date": "2025-12-30"}
    ]
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: real)

    quoted = [
        row
        for scenario in proposal["scenarios"]
        for row in scenario["rationale"]
        if row["source"] == "시세관측"
    ]
    assert quoted
    for row in quoted:
        assert row["evidence_grade"] == "OFFICIAL"
        assert "mock" not in row["evidence_detail"]
        assert "그물망·파렛트 10kg" in row["evidence_detail"]
        assert "물량가중" in row["evidence_detail"]


def test_mock_quotes_stay_sim_fixed_and_say_so() -> None:
    """반대 방향도 잠근다 — mock 을 OFFICIAL 로 올리면 같은 종류의 거짓이 된다."""
    proposal = run_purchase_agent("배추", date(2026, 8, 21))

    quoted = [
        row
        for scenario in proposal["scenarios"]
        for row in scenario["rationale"]
        if row["source"] == "시세관측"
    ]
    assert quoted
    for row in quoted:
        assert row["evidence_grade"] == "SIM_FIXED"
        assert "mock" in row["evidence_detail"]


# --------------------------------------------------------------------- 🟡4 원인 분류


def test_a_zero_quote_day_does_not_blame_self_check() -> None:
    """🔴 ③이 초안을 아예 안 만든 날인데 "모든 안이 self_check에서 컷됨"으로 나갔다.

    사유는 맞고 **원인 분류가 틀린** 거짓이라, 읽는 사람이 검증 로직을 들여다보게 된다.
    """
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=_no_quotes)

    assert proposal["no_proposal_reason"].startswith("안이 만들어지지 않았다")
    assert "self_check" not in proposal["no_proposal_reason"]


def test_a_real_self_check_cut_still_says_self_check() -> None:
    """반대 방향 — ⑦이 실제로 컷한 날은 그대로 self_check 이라고 말해야 한다.

    시세는 받았고 초안도 만들어졌는데 단가가 예측 상한을 넘은 날이다 — 12-31 관통에서
    mock 시세(1,650)가 실 예측 상한(992)에 부딪혔던 그 모양이다.

    ⚠️ 여기서는 **mock 예측**이라 상한이 1,731/1,781/1,901 이다(실 ML 값이 아니다).
      상한을 넘되 수량이 0으로 눌리지 않는 단가를 골라야 ⑥이 아니라 ⑦이 컷한다.
    """
    over_ceiling = [{"market": "가락", "grade": "상", "price": 2000}]
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: over_ceiling)

    assert proposal["scenarios"] == []
    assert proposal["no_proposal_reason"].startswith("모든 안이 self_check에서 컷됨")
    assert "max_price" in proposal["no_proposal_reason"]


# --------------------------------------------------------------------- look-ahead


def test_the_query_reads_before_as_of_not_the_day_itself() -> None:
    """🔴 우리는 **아침에 돈다** (상세설계 §128 · 역할계약서 §45 · CLAUDE.md).

    그 시각엔 당일 경매가 끝나지 않았고 적재는 더 늦다 — 2026-08-31 실측으로 haetdeul
    최신이 08-26(5일 전), 원천도 08-29(2일 전)였다. `= as_of`면 실운영에서 **매일 0행**이고
    그때마다 "휴장이거나 그 규격 거래가 없다"는 틀린 사유가 나간다.
    """
    captured: dict = {}
    _source(BAECHU_1231_ROWS, captured)("배추", INTEGRATION)

    assert "auction_date < %(as_of)s" in captured["query"]
    assert "auction_date = %(as_of)s" not in captured["query"]


def test_the_query_picks_one_day_not_one_day_per_grade() -> None:
    """🔴 등급별로 각자 거슬러 올라가면 스프레드가 **다른 시점의 두 가격**을 비교한다.

    2025-12-31 기준 실측: 배추 특 12-30(1일 전) · 상 11-12(1.5개월 전) · 하 2023-02-23
    (2.8년 전). 규칙 4의 "당일 시세에 실재하는 값"이 무너진다.
    """
    query = {}
    _source(BAECHU_1231_ROWS, query)("배추", INTEGRATION)

    assert "auction_date = (SELECT max(auction_date) FROM usable)" in query["query"]


def test_two_observation_dates_in_one_db_result_are_refused() -> None:
    """DB 경로 — 쿼리가 하루로 좁히므로 정상 경로에선 안 생긴다."""
    mixed = [
        {"observed_date": "2025-12-30", "grade": "특",
         "amount_krw": Decimal(100000), "volume_kg": Decimal(100)},
        {"observed_date": "2025-11-12", "grade": "상",
         "amount_krw": Decimal(200000), "volume_kg": Decimal(100)},
    ]
    with pytest.raises(ValueError, match="관측일이 하루가 아니다"):
        _source(mixed)("배추", INTEGRATION)


def test_two_observation_dates_from_an_injected_source_are_refused() -> None:
    """🔴 앞서 이 검사는 ``_materialize`` 만 봤다 — **DB 경로만** 지키고 주입은 그대로
    통과했다 (Codex 2차 지적).

    재현됐던 상태: 상 800원(12-30) + 중 600원(11-12) → 안 3개, 1.5개월 떨어진 두 가격으로
    스프레드 25%. ``check_prices_exist`` 는 (market, grade, price) 만 보므로 못 잡는다.
    """
    mixed = [
        {"market": "가락", "grade": "상", "price": 800,
         "spec": "그물망·파렛트 10kg", "observed_date": "2025-12-30"},
        {"market": "가락", "grade": "중", "price": 600,
         "spec": "그물망·파렛트 10kg", "observed_date": "2025-11-12"},
    ]
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: mixed)

    assert proposal["scenarios"] == []
    assert "관측일이 하루가 아니다" in proposal["rejected_reasons"][0]["reason"]


def test_the_observed_date_rides_on_every_quote() -> None:
    result = _source(BAECHU_1231_ROWS)("배추", INTEGRATION)

    assert all(quote["observed_date"] == OBSERVED for quote in result)
    assert observed_date(result) == OBSERVED
    assert staleness_days(result, "2025-12-31") == 1


def test_mock_quotes_carry_no_observation_date_so_staleness_is_not_measured() -> None:
    """회귀 경로가 이 검사를 만나지 않는 근거 — mock 은 표기가 없어 항상 None 이다."""
    assert observed_date(mocks.load_quotes("배추", date(2026, 8, 21))) is None
    assert staleness_days(mocks.load_quotes("배추", date(2026, 8, 21)), "2026-08-21") is None


# --------------------------------------------------------------------- 관측일이 출력에


def _wide_spread_on(observed: str):
    """스프레드가 확대된 다등급 시세. **스프레드 rationale 경로를 실제로 돌린다.**

    단일 등급이면 ⑤가 중품을 배정하지 않아 시세관측 근거가 하나뿐이고, 그러면 거기
    남아 있던 옛 ``MQ-가락-{as_of}`` 를 아무 검사도 못 본다.

    상 900 · 중 600 → 스프레드 33.3% (평시 12.1% 대비 확대 임계 18.2% 초과).
    12-31 mock 로트가 상 등급 10일이라 소진 한계 6일 → 스코어 +0.204 로 채택된다.
    셋 다 그날 max_price(992/1,007/1,079) 아래라 ⑦에 컷되지 않는다.
    """
    rows = [("특", 950), ("상", 900), ("중", 600)]
    return lambda item, as_of: [
        {"market": "가락", "grade": grade, "price": price,
         "spec": "그물망·파렛트 10kg", "observed_date": observed}
        for grade, price in rows
    ]


def test_the_rationale_says_the_observation_date_not_as_of() -> None:
    """🔴 12-30 값을 "12-31 당일 경락가"라고 적으면 그것도 거짓이다."""
    # 🔴 **다등급이어야 한다.** 단일 등급이면 스프레드 rationale 경로가 아예 안 돌아서
    #   거기 남아 있던 옛 ``MQ-가락-{as_of}`` 를 못 잡았다 (Codex 2차 지적).
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=_wide_spread_on("2025-12-30"))

    quoted = [
        row
        for scenario in proposal["scenarios"]
        for row in scenario["rationale"]
        if row["source"] == "시세관측"
    ]
    assert len(quoted) >= 2, "시세관측 근거가 하나뿐이면 스프레드 경로를 안 본 것이다"
    # 🔴 **같은 시세에서 나온 근거는 같은 좌표를 갖는다.** 대표 근거만 관측일로 바꾸고
    #   스프레드·조합 근거를 as_of 로 둬서 좌표가 갈라져 있었다 (Codex 2차 지적).
    assert {row["ref_id"] for row in quoted} == {"MQ-가락-2025-12-30"}
    for row in quoted:
        assert "2025-12-31" not in row["claim"]
    # 관측일과 나이는 **대표 근거**가 말한다 — 스프레드 근거는 소진 한계·스코어를 말한다.
    lead = next(row for row in quoted if "경락가" in row["claim"])
    assert "2025-12-30" in lead["claim"]
    assert "관측일 2025-12-30" in lead["evidence_detail"]
    assert "1일 전" in lead["evidence_detail"]


@pytest.mark.parametrize("observed", ["2025-12-31", "2026-01-05"])
def test_an_observation_on_or_after_as_of_is_refused(observed: str) -> None:
    """🔴 as_of **당일** 관측도 look-ahead 다. 아침엔 그 값이 존재하지 않는다.

    앞서 이 자리에는 "당일 관측이면 '0일 전'이라 적지 않는다"는 테스트가 있었다 —
    있으면 안 되는 상태를 **정상으로 못 박고** 있었고, 엄격한 ``< as_of`` 와 정면으로
    충돌했다 (Codex 2차 지적).
    """
    ahead = [
        {"market": "가락", "grade": "특", "price": 933,
         "spec": "그물망·파렛트 10kg", "observed_date": observed}
    ]
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: ahead)

    assert proposal["scenarios"] == []
    assert "as_of 이후" in proposal["rejected_reasons"][0]["reason"]


# --------------------------------------------------------------------- staleness


def _aged(days: int) -> list[dict]:
    observed = (INTEGRATION - timedelta(days=days)).isoformat()
    return [
        {"market": "가락", "grade": "특", "price": 824,
         "spec": "그물망·파렛트 10kg", "observed_date": observed}
    ]


@pytest.mark.parametrize("days", [1, 2])
def test_a_price_within_the_staleness_limit_is_used(days: int) -> None:
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: _aged(days))

    assert proposal["scenarios"]


@pytest.mark.parametrize("days", [3, 4, 11])
def test_a_price_past_the_staleness_limit_gives_a_reason_and_no_plan(days: int) -> None:
    """🔴 오래된 값을 당일인 척 쓰지 않는다 (규칙 3).

    지금의 5일 적재 지연도 여기 걸린다 — 조용히 5일 된 값을 쓰는 것보다 사유가 나가는
    쪽이 낫다는 판단이다.
    """
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: _aged(days))

    assert proposal["scenarios"] == []
    reason = proposal["rejected_reasons"][0]["reason"]
    assert f"{days}일 전" in reason
    assert "허용 2일" in reason
    assert proposal["no_proposal_reason"].startswith("안이 만들어지지 않았다")


def test_the_staleness_reason_does_not_guess_the_cause() -> None:
    """시장이 쉰 것과 적재가 밀린 것은 "as_of − 관측일"로만 나타나 구분할 수 없다.
    하나로 단정하면 없는 원인을 보고하게 된다."""
    proposal = run_purchase_agent("배추", INTEGRATION, quotes=lambda i, d: _aged(9))
    reason = proposal["rejected_reasons"][0]["reason"]

    assert "시장이 쉬었거나 적재가 밀린 것인데" in reason


@pytest.mark.parametrize(
    ("limit", "days", "blocked"),
    [(3, 3, False), (3, 4, True), (10, 5, False), (1, 2, True)],
)
def test_the_staleness_limit_actually_comes_from_constraints(
    limit: int, days: int, blocked: bool
) -> None:
    """임계를 코드에 박지 않는다 (규칙 7).

    ⚠️ **값 비교로는 이걸 증명할 수 없다.** ``constraints[...] == 3`` 은 코드가 3을 다시
      박아도 통과한다 — 실제로 변이가 안 물렸다(M17 과 같은 유형). 임계를 **바꿔보고**
      판정이 따라 바뀌는지를 봐야 한다.
    """
    constraints = load_constraints()
    constraints["market_quotes"]["max_staleness_days"] = limit
    reason = stale_quote_reason(_aged(days), INTEGRATION.isoformat(), constraints)

    assert (reason is not None) is blocked
    if blocked:
        assert f"허용 {limit}일" in reason


# --------------------------------------------------------------------- 읽는 테이블


@pytest.mark.parametrize(
    ("schema", "table"),
    [("source_raw", "auction_prices_daily"), ("어딘가", "다른표")],
    ids=["선언대로", "선언을_바꾸면_따라간다"],
)
def test_the_table_read_actually_comes_from_constraints(
    schema: str, table: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 읽을 스키마·테이블이 **선언에서 온다** (규칙 7·8).

    값 비교로는 증명되지 않는다 — ``cfg["source"]["schema"] == "source_raw"`` 는 코드가
    스키마를 박아도 통과한다. 선언을 **바꿔보고** 쿼리가 따라가는지를 본다.

    전에는 스키마가 ``DB_SCHEMA`` 환경변수에서 왔다. 그러면 ``.env`` 가 어느 테이블을
    읽을지 정하고, ``DB_SCHEMA=haetdeul`` 인 머신은 **3일 된 사본**을 본다
    (2026-08-31 실측: haetdeul 08-26 vs source_raw 08-29).
    """
    base = load_constraints()
    base["market_quotes"]["source"] = {"schema": schema, "table": table}
    monkeypatch.setattr("app.purchase_agent.quotes.load_constraints", lambda: base)

    captured: dict = {}
    _source([], captured)("배추", INTEGRATION)

    assert f"FROM {schema}.{table}" in captured["query"].replace('"', "")


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        (_ABSENT, KeyError),
        ("source_raw.auction_prices_daily", KeyError),
        ({"schema": "source_raw"}, ValueError),
        ({"schema": "", "table": "t"}, ValueError),
    ],
    ids=["선언_없음", "문자열_한_줄로_적음", "table_빠짐", "빈_문자열"],
)
def test_a_source_declaration_that_is_missing_or_half_written_stops_the_query(
    broken: Any, expected: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반쪽 선언은 조립 시점의 ``sql.Identifier`` 가 아니라 **여기서** 막는다.

    거기서 죽으면 무엇이 잘못됐는지 남지 않는다 — 규격 반쪽 표기를 ``spec_for_item`` 이
    먼저 막는 것과 같은 이유다.

    ⚠️ **예외 종류를 나눠 본다.** 둘 다 그냥 "터진다"로 두면 부재 검사를
      ``source or {}`` 로 바꿔도 반쪽 검사가 대신 걸려 **변이가 안 물린다**
      (실제로 안 물렸다 — 규칙 8). ``"schema.table"`` 처럼 **한 줄 문자열로 적는** 실수가
      그 차이를 드러낸다: ``.get`` 이 없어 ``AttributeError`` 로 죽는데, 그건 우리가
      낸 사유가 아니라 파이썬이 낸 것이다.
    """
    base = load_constraints()
    if broken is _ABSENT:
        del base["market_quotes"]["source"]
    else:
        base["market_quotes"]["source"] = broken
    monkeypatch.setattr("app.purchase_agent.quotes.load_constraints", lambda: base)

    with pytest.raises(expected):
        _source([], {})("배추", INTEGRATION)


# --------------------------------------------------------------------- 무 18kg


def test_the_radish_spec_switches_weight_before_2018() -> None:
    """🔴 ML spec_desc "상자·파렛트 20kg (2018년 이전 18kg)". 우리 IO명세에도
    "무 규격 2018 전 18kg(백테스트 주의)"로 적혀 있었는데 코드엔 없었다.

    20kg 고정이면 2017년 244 거래일 중 **64일만** 잡힌다(18kg 는 227일).
    """
    captured: dict = {}
    rows = [{"observed_date": "2017-06-01", "grade": "특",
             "amount_krw": Decimal(100000), "volume_kg": Decimal(100)}]
    _source(rows, captured)("무", date(2017, 6, 2))

    assert "CASE WHEN auction_date < %(spec_switch_date)s" in captured["query"]
    assert captured["params"]["spec_switch_date"] == date(2018, 1, 1)
    assert captured["params"]["unit_weight_before"] == 18
    assert captured["params"]["unit_weight_kg"] == 20


def test_items_without_a_weight_switch_keep_the_plain_condition() -> None:
    """분기가 없는 품목까지 CASE 를 붙이면 읽는 사람이 없는 전환을 찾게 된다."""
    captured: dict = {}
    _source(BAECHU_1231_ROWS, captured)("배추", INTEGRATION)

    assert "CASE WHEN" not in captured["query"]
    assert "unit_weight_kg = %(unit_weight_kg)s" in captured["query"]
    assert "spec_switch_date" not in captured["params"]


@pytest.mark.parametrize(
    ("observed", "expected"),
    [("2017-06-01", "상자·파렛트 18kg"), ("2018-01-01", "상자·파렛트 20kg"),
     ("2025-12-30", "상자·파렛트 20kg")],
)
def test_the_spec_label_tells_which_weight_that_day_used(observed: str, expected: str) -> None:
    """라벨은 사유에 그대로 실린다 — 2017년 값을 보면서 "20kg"이라고 적으면 **무엇을
    봤는지가 거짓**이 된다."""
    rows = [{"observed_date": observed, "grade": "특",
             "amount_krw": Decimal(100000), "volume_kg": Decimal(100)}]
    result = _source(rows)("무", date(2026, 1, 1))

    assert result[0]["spec"] == expected


# --------------------------------------------------------------------- 관측 표기 계약


def _marked(**over: Any) -> list[dict]:
    base = {"market": "가락", "grade": "특", "price": 824,
            "spec": "그물망·파렛트 10kg", "observed_date": "2025-12-30"}
    return [{**base, **over}]


def test_half_written_provenance_is_refused() -> None:
    """🔴 규격은 있는데 관측일이 없으면 **as_of 를 관측일인 것처럼 적게 된다.**

    재현됐던 출력: "관측일 2025-12-31 · 그물망·파렛트 10kg · 물량가중" — 12-31 은 관측된
    적이 없는 날이다 (Codex 2차 지적).
    """
    proposal = run_purchase_agent(
        "배추", INTEGRATION, quotes=lambda i, d: [
            {"market": "가락", "grade": "특", "price": 824, "spec": "그물망·파렛트 10kg"}
        ]
    )

    assert proposal["scenarios"] == []
    assert "반쪽만" in proposal["rejected_reasons"][0]["reason"]


def test_an_unparseable_observation_date_does_not_kill_the_graph() -> None:
    """🔴 결정 d — 죽으면 오케스트레이터가 원인을 못 받는다.

    전에는 ``date.fromisoformat`` 이 ``ValueError`` 로 그래프 전체를 죽였다.
    """
    proposal = run_purchase_agent(
        "배추", INTEGRATION, quotes=lambda i, d: _marked(observed_date="어제")
    )

    assert proposal["scenarios"] == []
    assert "날짜로 읽을 수 없어" in proposal["rejected_reasons"][0]["reason"]


def test_the_provenance_check_applies_to_injected_sources_too() -> None:
    """🔴 전에는 ``_materialize`` 안에만 있어 **DB 경로만** 지켰다."""
    constraints = load_constraints()

    assert provenance_problem(_marked(), "2025-12-31", constraints) is None
    assert provenance_problem(_marked(observed_date="2026-01-05"), "2025-12-31", constraints)
    assert provenance_problem(_marked(observed_date="2025-12-31"), "2025-12-31", constraints)


def test_mock_quotes_pass_the_provenance_check_untouched() -> None:
    """표기가 **둘 다 없는** 것이 mock 의 정상 상태다 — 회귀 경로가 이 검사를 안 만난다."""
    constraints = load_constraints()
    plain = mocks.load_quotes("배추", date(2026, 8, 21))

    assert provenance_problem(plain, "2026-08-21", constraints) is None


def test_a_row_without_an_observation_date_cannot_ride_along_in_the_db_path() -> None:
    """한 행만 날짜가 있으면 예전엔 "단일 날짜"로 인정해 **함께 합산**했다."""
    rows = [
        {"observed_date": OBSERVED, "grade": "특",
         "amount_krw": Decimal(100000), "volume_kg": Decimal(100)},
        {"grade": "상", "amount_krw": Decimal(200000), "volume_kg": Decimal(100)},
    ]
    with pytest.raises(ValueError, match="관측일 없는 행"):
        _source(rows)("배추", INTEGRATION)


# --------------------------------------------------------------------- before 검증


@pytest.mark.parametrize(
    "broken",
    [
        {"unit_weight_kg": 18, "label": "x"},                       # date 누락
        {"date": "2018-01-01", "unit_weight_kg": 18, "label": "x"},  # 문자열 date
        {"date": date(2018, 1, 1), "unit_weight_kg": 0, "label": "x"},
        {"date": date(2018, 1, 1), "unit_weight_kg": 18, "label": ""},
    ],
)
def test_a_half_written_weight_switch_is_refused_early(broken: dict) -> None:
    """``date`` 누락은 쿼리 파라미터를 채울 때 **늦게 KeyError** 로 터지고, 음수 중량은
    조용히 0행 → "거래 기록이 없다"는 틀린 사유로 이어진다 (Codex 2차 지적)."""
    constraints = load_constraints()
    constraints["market_quotes"]["spec_by_item"]["무"]["before"] = broken

    with pytest.raises((ValueError, TypeError)):
        spec_for_item("무", constraints)
