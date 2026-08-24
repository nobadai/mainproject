"""mock 데이터 + ports 구현 검사 (백로그 E1-1·E1-2·E1-3).

mock은 장식용 샘플이 아니라 **노드 단위 테스트 4종의 입력**이다. 그래서 여기서는
"파일이 파싱되는가"가 아니라 **"시나리오 이름이 뜻하는 조건을 데이터가 실제로 만족하는가"**를
검사한다. 누가 숫자를 만져 이름과 내용이 어긋나면 그 순간 빨간불이 뜬다.

**운영 임계**(ci_width·트리거·비율)는 전부 ``load_constraints()``로 읽는다 — 테스트에
하드코딩하면 규칙 7이 뚫린다. 반면 아래 자릿수 밴드는 운영 임계가 아니라 **테스트 sentinel**
이다(constraints.yaml에 없고, 있어서도 안 된다). 그래서 이 파일에 직접 적는다.
"""

import json
import operator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from _fixtures import AS_OF, _proposal

from app.purchase_agent import mocks, ports
from app.purchase_agent.config import load_constraints

MOCK_DIR = Path(mocks.__file__).parent
MOCK_FILES = sorted(MOCK_DIR.glob("*.json"))
AGENT_DIR = Path(ports.__file__).parent

# 앵커일 = 시나리오 키 (mocks/scenarios.json). 이름은 CLAUDE.md "작업 방식"의 4개 테스트명.
RISING = date(2026, 8, 21)
FALLING = date(2026, 8, 28)
UNCERTAIN = date(2026, 9, 4)
SPREAD_WIDE = date(2026, 9, 11)
ANCHORS = (RISING, FALLING, UNCERTAIN, SPREAD_WIDE)

DOC_TYPES = ["관측월보", "기상", "작년동기"]

# 미결 파라미터(N4·N5). mock에 등장하면 안 된다 — 0으로 슬쩍 들어오는 경로 차단 (규칙 3).
PENDING_KEYS = ("inbound_lead_days", "purchase_payment_days")

# ── 단위 자릿수 밴드 ────────────────────────────────────────────────────────
# 단위 사고는 타입 검사로 안 잡힌다. ton 값이 섞여도 int는 int라 조용히 통과하고
# 숫자만 1000배 틀린다. 자릿수 범위가 유일한 방어선이다.
QTY_MIN, QTY_MAX = 1_000, 1_000_000  # kg — ton이면 한 자리~세 자리로 떨어져 즉시 걸린다
PRICE_MIN, PRICE_MAX = 100, 20_000  # 원/kg — 원/ton이면 100만 단위라 걸린다
PRICE_KEYS = frozenset(
    {"price", "predicted", "lower", "upper", "current_price", "grade_unit_price"}
)


def _scalar_leaves(node: object, key: str = "", path: str = "$") -> list[tuple[str, str, object]]:
    """중첩 구조를 재귀 순회해 (경로, 키, 값)을 **타입 가리지 않고** 모은다.

    정수만 모으면 ``qty_kg: 12.0``이나 ``price: "1650"``이 검사 대상에서 통째로 빠져
    "전부 통과"라는 거짓 안심을 준다 — 타입 검사는 밴드 검사보다 먼저 와야 한다.

    ``_``로 시작하는 키는 설명용이라 건너뛴다(포트 반환값에 실리지 않는다).
    필드를 나중에 추가해도 자동으로 검사망에 들어오게 하려고 키 목록이 아니라 순회로 짰다.
    """
    if isinstance(node, dict):
        return [
            leaf
            for k, v in node.items()
            if not k.startswith("_")
            for leaf in _scalar_leaves(v, k, f"{path}.{k}")
        ]
    if isinstance(node, list):
        return [
            leaf for i, v in enumerate(node) for leaf in _scalar_leaves(v, key, f"{path}[{i}]")
        ]
    return [(path, key, node)]


def _assert_whole_number(value: object, where: str) -> int:
    """``bool``을 먼저 거른다 — ``isinstance(True, int)``가 참이라 그냥 두면 1로 통과한다."""
    assert not isinstance(value, bool), f"{where} = {value!r} — bool은 수량·가격이 될 수 없다"
    assert isinstance(value, int), f"{where} = {value!r} ({type(value).__name__}) — 정수가 아니다"
    return value


def _assert_unit_bands(payload: object, where: str) -> None:
    for path, key, value in _scalar_leaves(payload):
        if key.endswith("_kg"):
            _assert_whole_number(value, f"{where}{path}")
            assert QTY_MIN <= value <= QTY_MAX, f"{where}{path} = {value} — kg 자릿수 이탈"
        elif key in PRICE_KEYS:
            _assert_whole_number(value, f"{where}{path}")
            assert PRICE_MIN <= value <= PRICE_MAX, f"{where}{path} = {value} — 원/kg 자릿수 이탈"


@pytest.mark.parametrize("path", MOCK_FILES, ids=lambda p: p.name)
def test_mock_files_keep_kg_and_won_per_kg_magnitudes(path: Path) -> None:
    """수량은 정수 kg, 가격은 정수 원/kg. ton 혼입 방어 — 저장된 JSON 쪽."""
    _assert_unit_bands(json.loads(path.read_text(encoding="utf-8")), f"{path.name}:")


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
def test_port_outputs_keep_kg_and_won_per_kg_magnitudes(as_of: date) -> None:
    """같은 밴드를 **포트 반환값**에도 적용한다 — 로더가 단위를 망가뜨릴 여지까지 막는다."""
    for item in mocks.ITEMS:
        _assert_unit_bands(ports.get_forecast(item, as_of), f"forecast/{item}:")
        _assert_unit_bands(ports.get_market_quotes(item, as_of), f"quotes/{item}:")
        _assert_unit_bands(ports.get_inventory(item, as_of), f"inventory/{item}:")
        _assert_unit_bands(ports.get_confirmed_orders(item, as_of), f"orders/{item}:")
    assert ports.get_projected_cash_min(as_of, 30) >= 1_000_000


@pytest.mark.parametrize("path", MOCK_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("key", PENDING_KEYS)
def test_mock_never_carries_pending_parameters(path: Path, key: str) -> None:
    """N4·N5는 mock에도 없어야 한다. 값이 아니라 **계산을 막는 장치**다 (규칙 3)."""
    assert key not in path.read_text(encoding="utf-8")


def test_agent_sources_never_read_the_wall_clock() -> None:
    """규칙 1 — 패키지 어디에도 벽시계 호출이 없다. 날짜는 항상 as_of로 주입된다."""
    offenders = [
        path.relative_to(AGENT_DIR).as_posix()
        for path in sorted(AGENT_DIR.rglob("*.py"))
        if "date.today()" in path.read_text(encoding="utf-8")
        or "datetime.now()" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# ── 시나리오 구분이 데이터에 드러나는가 ─────────────────────────────────────


ITEM_KEYED_FILES = [
    path
    for path in MOCK_FILES
    if path.name.startswith(("forecast_", "quotes_")) or path.name in {"inventory.json", "orders.json"}
]


@pytest.mark.parametrize("path", ITEM_KEYED_FILES, ids=lambda p: p.name)
def test_every_mock_file_covers_every_item(path: Path) -> None:
    """품목 하나가 빠지면 그 품목의 포트만 조용히 KeyError를 낸다 — 데이터에서 먼저 막는다."""
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    assert set(items) == set(mocks.ITEMS)


def test_anchor_days_match_the_scenarios_file() -> None:
    """테스트 상수와 scenarios.json이 따로 놀지 않게 못 박는다."""
    anchors = json.loads((MOCK_DIR / "scenarios.json").read_text(encoding="utf-8"))["anchors"]
    assert sorted(anchors) == sorted(d.isoformat() for d in ANCHORS)
    assert [anchors[d.isoformat()]["name"] for d in ANCHORS] == [
        "mock_rising",
        "mock_falling",
        "mock_uncertain",
        "grade_spread_wide",
    ]


#: 시나리오별 기대 판정. 판정은 **기준일(D+14) 한 줄**로 내려진다 (상세설계 §4-①).
SITUATION_BY_ANCHOR = (
    (RISING, "stable"),
    (FALLING, "stable"),
    (UNCERTAIN, "uncertain"),
    (SPREAD_WIDE, "stable"),
)

#: constraints의 ``ci_width_comparison`` 문자열을 실제 연산으로. 테스트가 비교 방향까지
#: 파일에서 읽어야 임계와 연산이 따로 노는 사고를 잡을 수 있다.
_COMPARISONS = {">=": operator.ge, ">": operator.gt}


def _judgment_row(item: str, as_of: date) -> dict:
    day = load_constraints()["situation"]["ci_judgment_day"]
    return ports.get_forecast(item, as_of)["daily"][day - 1]


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_judgment_day_row_is_the_fourteenth_calendar_day(item: str, as_of: date) -> None:
    """``ci_judgment_day: 14`` → ``daily[13]``. 이 매핑은 daily가 D+1 시작이라는 데 걸려 있다.

    daily 시작이 D+0으로 바뀌면 index 13은 D+13이 되고, 상황 분류가 하루 밀린 채 조용히
    돈다. 그래서 index를 믿지 않고 **날짜로 되짚어** 확인한다.
    """
    day = load_constraints()["situation"]["ci_judgment_day"]
    assert _judgment_row(item, as_of)["date"] == (as_of + timedelta(days=day)).isoformat()


@pytest.mark.parametrize(("as_of", "expected"), SITUATION_BY_ANCHOR, ids=lambda v: str(v))
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_judgment_day_ci_width_classifies_each_scenario(
    item: str, as_of: date, expected: str
) -> None:
    """실제 판정 경로(기준일 한 줄)로 계산해도 시나리오 이름과 같은 결과가 나오는가.

    아래 ``max``/``min`` 검사는 전 구간을 보므로 더 강하지만, ① 노드가 쓸 식은 이것이다.
    둘을 함께 두는 이유: 전 구간 검사는 데이터가 고르다는 걸, 이 검사는 **확정된 규칙이
    의도한 판정을 낸다**는 걸 각각 지킨다.
    """
    situation = load_constraints()["situation"]
    exceeds = _COMPARISONS[situation["ci_width_comparison"]]
    row = _judgment_row(item, as_of)
    ci_width = (row["upper"] - row["lower"]) / row["predicted"]
    verdict = "uncertain" if exceeds(ci_width, situation["ci_width_threshold"]) else "stable"
    assert verdict == expected


def _ci_widths(item: str, as_of: date) -> list[float]:
    """ci_width = (upper − lower) / predicted — 상세설계 §4-①."""
    daily = ports.get_forecast(item, as_of)["daily"]
    return [(row["upper"] - row["lower"]) / row["predicted"] for row in daily]


def _rise_rate_2w(item: str, as_of: date) -> float:
    """2주 후 상승률. 판정 기준일과 **같은 날**을 본다 — 상세설계 §4-①이 든 D+14 채택 근거가
    "상황 분류와 상승률이 하나의 질문이 된다"였으므로, 여기서도 같은 상수를 읽는다."""
    forecast = ports.get_forecast(item, as_of)
    return _judgment_row(item, as_of)["predicted"] / forecast["current_price"] - 1


def _predicted(item: str, as_of: date) -> list[int]:
    return [row["predicted"] for row in ports.get_forecast(item, as_of)["daily"]]


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_rising_is_stable_and_clears_the_pre_purchase_trigger(item: str) -> None:
    """mock_rising → 선매입(D 큰 안)이 나오려면 stable이면서 timing 축이 열려야 한다."""
    constraints = load_constraints()
    assert max(_ci_widths(item, RISING)) < constraints["situation"]["ci_width_threshold"]
    assert _rise_rate_2w(item, RISING) >= constraints["triggers"]["pre_purchase_rise_rate"]
    predicted = _predicted(item, RISING)
    assert predicted == sorted(predicted)
    assert len(set(predicted)) == len(predicted), "지속 상승이면 같은 값이 연달아 나오지 않는다"


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_falling_is_stable_and_misses_the_pre_purchase_trigger(item: str) -> None:
    """mock_falling → 최소 매입(D=2). 하락 궤적이면 선매입 트리거가 열리면 안 된다."""
    constraints = load_constraints()
    assert max(_ci_widths(item, FALLING)) < constraints["situation"]["ci_width_threshold"]
    rise = _rise_rate_2w(item, FALLING)
    assert rise < 0
    assert rise < constraints["triggers"]["pre_purchase_rise_rate"]
    predicted = _predicted(item, FALLING)
    assert predicted == sorted(predicted, reverse=True)
    assert len(set(predicted)) == len(predicted)


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_uncertain_exceeds_the_confidence_interval_threshold(item: str) -> None:
    """mock_uncertain → 보수·기본 2안만. 전 구간이 임계 이상이어야 판정이 흔들리지 않는다."""
    threshold = load_constraints()["situation"]["ci_width_threshold"]
    assert min(_ci_widths(item, UNCERTAIN)) >= threshold


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_uncertain_does_not_also_trip_the_pre_purchase_trigger(item: str) -> None:
    """uncertain 날에 timing 트리거까지 열리면 무엇 때문에 안이 줄었는지 알 수 없다."""
    trigger = load_constraints()["triggers"]["pre_purchase_rise_rate"]
    assert _rise_rate_2w(item, UNCERTAIN) < trigger


def _grade_prices(item: str, as_of: date) -> dict[str, int]:
    return {quote["grade"]: quote["price"] for quote in ports.get_market_quotes(item, as_of)}


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_wide_spread_clears_the_widening_threshold(item: str) -> None:
    """grade_spread_wide → 중품 비중 상승. 상-중 격차가 평시 대비 임계를 넘어야 한다."""
    ratio = load_constraints()["triggers"]["grade_spread_widening_ratio"]
    normal, wide = _grade_prices(item, RISING), _grade_prices(item, SPREAD_WIDE)
    normal_spread = (normal["상"] - normal["중"]) / normal["상"]
    wide_spread = (wide["상"] - wide["중"]) / wide["상"]
    assert wide_spread >= normal_spread * (1 + ratio)


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_wide_spread_moves_only_the_mid_grade(item: str) -> None:
    """변수를 하나만 움직였는지 확인 — 특·상이 같이 흔들리면 ⑤ 노드 테스트의 인과가 흐려진다."""
    normal, wide = _grade_prices(item, RISING), _grade_prices(item, SPREAD_WIDE)
    assert wide["특"] == normal["특"]
    assert wide["상"] == normal["상"]
    assert wide["중"] < normal["중"]


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_spread_wide_day_stays_stable_so_the_grade_axis_is_isolated(item: str) -> None:
    """등급만 보려면 그날이 uncertain이면 안 된다 — 안 개수가 줄어 배분을 볼 여지가 사라진다."""
    threshold = load_constraints()["situation"]["ci_width_threshold"]
    assert max(_ci_widths(item, SPREAD_WIDE)) < threshold


# ── 계약(IO명세 §1) 형태 1:1 ────────────────────────────────────────────────

FORECAST_KEYS = {
    "generated_at",
    "item",
    "unit",
    "current_price",
    "horizon_days",
    "daily",
    "model_version",
}
DAILY_KEYS = {"date", "predicted", "lower", "upper"}
QUOTE_KEYS = {"market", "grade", "price"}
INVENTORY_KEYS = {"as_of", "item", "lots", "warehouse_free_kg", "rental_cap_kg"}
LOT_KEYS = {"lot_id", "grade", "stocked_at", "remaining_kg", "shelf_life_days"}
ORDERS_KEYS = {"as_of", "item", "orders", "total_kg"}
ORDER_KEYS = {"sale_id", "qty_kg", "due_date"}
DOCUMENT_KEYS = {"doc_id", "source", "doc_type", "item", "title", "published_at", "content"}


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_forecast_matches_the_io_spec_shape(item: str, as_of: date) -> None:
    """E1-1 DoD — ML 스키마 1:1. 설명용 ``_`` 키가 새어나오지 않는 것까지 포함한다."""
    forecast = ports.get_forecast(item, as_of)
    assert set(forecast) == FORECAST_KEYS
    assert forecast["item"] == item
    assert forecast["unit"] == "원/kg"
    assert forecast["horizon_days"] == 18 == len(forecast["daily"])
    assert all(set(row) == DAILY_KEYS for row in forecast["daily"])
    assert [row["date"] for row in forecast["daily"]] == [
        (as_of + timedelta(days=n)).isoformat() for n in range(1, 19)
    ]


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_forecast_batch_is_not_generated_after_as_of(item: str, as_of: date) -> None:
    """``generated_at <= as_of`` — 미래 배치를 읽으면 백테스트 성적이 무효가 된다."""
    generated_at = ports.get_forecast(item, as_of)["generated_at"]
    stamp = datetime.fromisoformat(generated_at)  # 형식이 깨지면 여기서 터진다
    assert stamp.tzinfo is not None, "타임존 없는 시각은 어느 시점인지 확정되지 않는다"
    assert stamp.utcoffset() == timedelta(hours=9), "KST(+09:00) 고정 (IO명세 §1-①)"
    assert stamp.date() <= as_of


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_quotes_match_the_io_spec_shape(item: str, as_of: date) -> None:
    quotes = ports.get_market_quotes(item, as_of)
    assert isinstance(quotes, list)
    assert len(quotes) >= 2, "등급이 2개 미만이면 배분 판단이 무의미하다 (IO명세 §1-②)"
    assert all(set(quote) == QUOTE_KEYS for quote in quotes)
    assert {quote["market"] for quote in quotes} == {"가락"}
    grades = [quote["grade"] for quote in quotes]
    assert len(set(grades)) == len(grades), "같은 등급이 두 번 나오면 단가가 모호해진다"


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_inventory_and_orders_match_the_io_spec_shape(item: str, as_of: date) -> None:
    inventory = ports.get_inventory(item, as_of)
    assert set(inventory) == INVENTORY_KEYS
    assert inventory["as_of"] == as_of.isoformat()
    assert all(set(lot) == LOT_KEYS for lot in inventory["lots"])
    assert all(date.fromisoformat(lot["stocked_at"]) <= as_of for lot in inventory["lots"])

    orders = ports.get_confirmed_orders(item, as_of)
    assert set(orders) == ORDERS_KEYS
    assert all(set(order) == ORDER_KEYS for order in orders["orders"])
    assert all(date.fromisoformat(order["due_date"]) >= as_of for order in orders["orders"])
    assert orders["total_kg"] == sum(order["qty_kg"] for order in orders["orders"])


@pytest.mark.parametrize("item", mocks.ITEMS)
def test_rental_cap_follows_the_constraints_ratio(item: str) -> None:
    """외부임차 한도 = 창고 여유 × 30%. 상수는 constraints.yaml 단일 소스다 (규칙 7)."""
    inventory = ports.get_inventory(item, RISING)
    ratio = load_constraints()["warehouse"]["rental_cap_ratio"]
    assert inventory["rental_cap_kg"] == round(inventory["warehouse_free_kg"] * ratio)


# ── 문서 기준일(2026-08-21) 재현 ────────────────────────────────────────────


def test_anchor_day_matches_the_fixture_as_of() -> None:
    """픽스처와 mock이 같은 날을 가리키는지부터 못 박는다."""
    assert AS_OF == RISING.isoformat()


def test_inventory_reproduces_the_io_spec_example() -> None:
    assert ports.get_inventory("배추", RISING) == {
        "as_of": "2026-08-21",
        "item": "배추",
        "lots": [
            {
                "lot_id": 12,
                "grade": "상",
                "stocked_at": "2026-08-17",
                "remaining_kg": 3000,
                "shelf_life_days": 10,
            }
        ],
        "warehouse_free_kg": 12000,
        "rental_cap_kg": 3600,
    }


def test_orders_reproduce_the_io_spec_example() -> None:
    assert ports.get_confirmed_orders("배추", RISING) == {
        "as_of": "2026-08-21",
        "item": "배추",
        "orders": [
            {"sale_id": 7, "qty_kg": 12000, "due_date": "2026-08-24"},
            {"sale_id": 9, "qty_kg": 6000, "due_date": "2026-08-29"},
        ],
        "total_kg": 18000,
    }


def test_remaining_freshness_is_the_six_days_the_fixture_talks_about() -> None:
    """잔여신선도 = shelf_life − (as_of − stocked_at). 픽스처 risks의 '6일'과 같은 6이어야 한다."""
    lot = ports.get_inventory("배추", RISING)["lots"][0]
    elapsed = (RISING - date.fromisoformat(lot["stocked_at"])).days
    assert lot["shelf_life_days"] - elapsed == 6
    assert "6일" in _proposal()["scenarios"][0]["risks"][0]


def test_fixture_sourcing_prices_exist_in_the_same_day_quotes() -> None:
    """규칙 4 — sourcing_plan의 등급·단가는 **당일 시세에 실재하는 값만**.

    계약(schemas)과 데이터(mock)를 잇는 다리다. 여기가 깨지면 ⑦ self_check가 즉시 컷한다.
    """
    quotes = {
        (quote["grade"], quote["price"]) for quote in ports.get_market_quotes("배추", RISING)
    }
    lines = _proposal()["scenarios"][0]["sourcing_plan"]
    assert lines
    for line in lines:
        assert line["market"] == "가락"
        assert (line["grade"], line["grade_unit_price"]) in quotes


# ── look-ahead 방어 (문서) ──────────────────────────────────────────────────


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_documents_never_leak_future_publications(item: str, as_of: date) -> None:
    """전 품목·전 앵커일. 배추만 검사하면 다른 품목의 빈 코퍼스를 못 본다."""
    documents = ports.get_context_docs(item, as_of, DOC_TYPES)
    assert documents, f"{item}에 읽을 문서가 없으면 ② 컨텍스트 루프가 헛돈다"
    assert all(set(doc) == DOCUMENT_KEYS for doc in documents)
    assert all(date.fromisoformat(doc["published_at"]) <= as_of for doc in documents)
    assert all(doc["item"] == item for doc in documents)


def test_document_ids_are_unique_across_the_corpus() -> None:
    """``DOC-{doc_id}`` 참조 규약(IO명세 §1-⑥)이 성립하려면 id가 유일해야 한다."""
    corpus = json.loads((MOCK_DIR / "documents.json").read_text(encoding="utf-8"))["documents"]
    ids = [doc["doc_id"] for doc in corpus]
    assert len(set(ids)) == len(ids)


def test_future_document_is_invisible_until_it_is_published() -> None:
    """'보이면 안 되는 문서'를 코퍼스에 넣어둬야 필터가 작동함을 증명할 수 있다."""

    def visible(as_of: date) -> set[int]:
        return {doc["doc_id"] for doc in ports.get_context_docs("배추", as_of, ["관측월보"])}

    assert 6 not in visible(RISING), "DOC-6(2026-09-05)이 8/21에 보이면 look-ahead다"
    assert 6 in visible(SPREAD_WIDE)


@pytest.mark.parametrize("record", [{"doc_id": 99}, {"doc_id": 99, "published_at": ""}])
def test_document_without_published_at_is_refused(record: dict) -> None:
    """건너뛰지 않고 **거부**한다 — 조용히 빠지면 근거가 비어도 아무도 모른다 (IO명세 §1-⑥)."""
    with pytest.raises(ValueError, match="published_at"):
        mocks.filter_by_published_at([record], RISING)


def test_document_type_filter_selects_only_what_was_asked() -> None:
    documents = ports.get_context_docs("배추", UNCERTAIN, ["기상"])
    assert {doc["doc_type"] for doc in documents} == {"기상"}


def test_empty_doc_types_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ports.get_context_docs("배추", UNCERTAIN, [])


def test_unknown_doc_type_is_rejected() -> None:
    """오타를 빈 목록으로 돌려주면 '읽을 문서가 없었다'와 구분되지 않는다."""
    with pytest.raises(ValueError, match="unknown doc_types"):
        ports.get_context_docs("배추", UNCERTAIN, ["잡지"])


# ── days 창 · 미지 입력 ─────────────────────────────────────────────────────


def test_days_window_filters_orders_and_recomputes_the_total() -> None:
    """``days``는 장식용 파라미터가 아니다 — 실제로 거르고 total_kg도 다시 더한다."""
    full = ports.get_confirmed_orders("배추", RISING)
    narrow = ports.get_confirmed_orders("배추", RISING, days=5)
    assert full["total_kg"] == 18000
    assert len(narrow["orders"]) == 1
    assert narrow["total_kg"] == 12000 == sum(o["qty_kg"] for o in narrow["orders"])


def test_negative_days_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ports.get_confirmed_orders("배추", RISING, days=-1)


def test_zero_days_window_returns_an_empty_order_book() -> None:
    """0은 "확정된 0"이다 — 조회 창이 없으면 주문도 없고 합계도 0이다 (규칙 3)."""
    orders = ports.get_confirmed_orders("배추", RISING, days=0)
    assert orders["orders"] == []
    assert orders["total_kg"] == 0


def test_wide_days_window_keeps_every_order() -> None:
    assert ports.get_confirmed_orders("배추", RISING, days=365)["total_kg"] == 18000


@pytest.mark.parametrize("days", [True, False, 3.5, "5", None], ids=repr)
def test_non_integer_days_window_is_rejected(days: object) -> None:
    """``days=True``가 ``days=1``로 둔갑하면 주문이 통째로 사라진 채 조용히 통과한다."""
    with pytest.raises(TypeError, match="days must be an int"):
        ports.get_confirmed_orders("배추", RISING, days=days)


@pytest.mark.parametrize("horizon", [True, 30.0, "30", None], ids=repr)
def test_non_integer_cash_horizon_is_rejected(horizon: object) -> None:
    """``"30"``이 통하면 지평 표기가 두 가지가 되어 조회 키가 갈라진다."""
    with pytest.raises(TypeError, match="horizon_days must be an int"):
        ports.get_projected_cash_min(RISING, horizon)


@pytest.mark.parametrize("as_of", ANCHORS, ids=lambda d: d.isoformat())
@pytest.mark.parametrize("item", mocks.ITEMS)
def test_every_port_returns_data_on_every_anchor_day(item: str, as_of: date) -> None:
    """6개 포트 × 4품목 × 4앵커일이 전부 살아 있다 (E1-3 DoD).

    시그니처만 있던 단계의 ``NotImplementedError`` 검사를 대체한다. 한 품목만 돌면
    나머지 품목의 mock이 비어 있어도 초록불이 뜬다 — 실제로 그런 구멍이 있었다.
    """
    assert ports.get_forecast(item, as_of)["daily"]
    assert ports.get_market_quotes(item, as_of)
    assert ports.get_inventory(item, as_of)["lots"]
    assert ports.get_confirmed_orders(item, as_of)["orders"]
    assert ports.get_projected_cash_min(as_of, 30) > 0
    assert ports.get_context_docs(item, as_of, DOC_TYPES)


def test_unknown_item_is_rejected() -> None:
    """빈 dict를 돌려주면 노드가 0으로 계산해버린다."""
    with pytest.raises(ValueError, match="unknown item"):
        ports.get_forecast("건고추", RISING)


#: 앵커일이 아닌 날. 어느 포트로 들어와도 같은 대접을 받아야 한다.
NOT_AN_ANCHOR = date(2026, 8, 22)

PORT_CALLS = (
    ("get_forecast", lambda as_of: ports.get_forecast("배추", as_of)),
    ("get_market_quotes", lambda as_of: ports.get_market_quotes("배추", as_of)),
    ("get_inventory", lambda as_of: ports.get_inventory("배추", as_of)),
    ("get_confirmed_orders", lambda as_of: ports.get_confirmed_orders("배추", as_of)),
    ("get_projected_cash_min", lambda as_of: ports.get_projected_cash_min(as_of, 30)),
    ("get_context_docs", lambda as_of: ports.get_context_docs("배추", as_of, ["관측월보"])),
)


@pytest.mark.parametrize(("name", "call"), PORT_CALLS, ids=[name for name, _ in PORT_CALLS])
def test_unknown_as_of_is_rejected_by_every_port(name: str, call: object) -> None:
    """포트 하나만 날짜 검증을 빼먹으면 그 경로로 앵커 밖 데이터가 새어 들어온다."""
    with pytest.raises(KeyError, match="no mock scenario"):
        call(NOT_AN_ANCHOR)


def test_unknown_cash_horizon_is_rejected() -> None:
    """30일치 숫자를 60일 질문에 돌려주면 운전자본 갭을 놓친다 (IO명세 §1-⑤)."""
    with pytest.raises(KeyError, match="no mock cash"):
        ports.get_projected_cash_min(RISING, 60)


# ── constraints 로더 ────────────────────────────────────────────────────────


def test_load_constraints_returns_a_fresh_object_each_call() -> None:
    """캐시하지 않는다 — feedback의 constraint 덮어쓰기가 다른 노드로 새면 안 된다."""
    first, second = load_constraints(), load_constraints()
    assert first == second
    assert first is not second
    first["situation"]["ci_width_threshold"] = 999
    assert load_constraints()["situation"]["ci_width_threshold"] != 999


def test_load_constraints_rejects_a_file_missing_sections(tmp_path: Path) -> None:
    """YAML 오타를 노드 실행 도중이 아니라 로드 시점에 잡는다."""
    broken = tmp_path / "constraints.yaml"
    broken.write_text('version: "1.1"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="missing required sections"):
        load_constraints(broken)


@pytest.mark.parametrize("broken_value", ["[]", "{}", '"보수 2"'], ids=["list", "empty", "scalar"])
def test_constraints_section_must_be_a_non_empty_mapping(
    tmp_path: Path, broken_value: str
) -> None:
    """섹션 키만 있고 속이 무너진 파일은 존재 검사를 그대로 통과한다.

    ``pending: []``이면 ``pending["inbound_lead_days"]``가 노드 안에서 늦게 터지고,
    그때는 어느 파일이 문제인지 알기 어렵다.
    """
    source = (Path(__file__).resolve().parents[2] / "app/purchase_agent/constraints.yaml").read_text(
        encoding="utf-8"
    )
    broken = tmp_path / "constraints.yaml"
    broken.write_text(
        source.replace("pending:\n", f"pending: {broken_value}\n_pending_disabled:\n"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a non-empty mapping"):
        load_constraints(broken)


def test_load_constraints_rejects_a_non_mapping_file(tmp_path: Path) -> None:
    broken = tmp_path / "constraints.yaml"
    broken.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(TypeError, match="mapping"):
        load_constraints(broken)
