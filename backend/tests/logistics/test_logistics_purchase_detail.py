"""매입 상세 조회의 **규율** 검사 (3-B4-E). DB 를 부르지 않는다.

가짜 커넥션·커서로 잰다. 여기서 재는 것은 값이 DB 에 있는지가 아니라 **물류가
소유한 규율 다섯**이다.

```text
열쇠가 purchase_id 하나인가   품목명으로 좁히면 물류가 번역의 두 번째 주인이 된다
0 · 1 · 2+ 를 가르나           첫 행을 조용히 고르지 않는다
적힌 값을 그대로 나르나        grade NULL · Decimal 자릿수를 손대지 않는다
매입 규칙을 다시 계산 안 하나  단가도 금액 검증도 매입이 이미 했다
아무것도 안 쓰나               읽기 전용 · 커밋/롤백/자체 커넥션 없음
```
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import purchase_detail
from app.logistics.purchase_detail import (
    InvalidPurchaseIdentity,
    PurchaseDetail,
    PurchaseDetailAmbiguous,
    PurchaseDetailMissing,
    fetch_purchase_detail,
)

PURCHASE_ID = "PUR-THRU-20260105-BAECHU-D1-S1"
PITEM_ID = "PITEM-THRU-20260105-BAECHU-D1-S1-BAECHU"

#: `_DETAIL_COLUMNS` 와 **같은 순서**여야 한다 — 가짜 커서가 튜플로도 답하기 때문이다.
_칸순서 = ("purchase_item_id", "item_id", "grade", "quantity_kg", "unit_price_krw_per_kg")


@pytest.fixture(autouse=True)
def 스키마이름을_고정한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_db_schema()` 는 `DB_SCHEMA` 환경변수를 읽는다 — 여기서 끊는다.

    🔴 환경변수에 기대면 `pytest tests/logistics` 단독 실행에서 깨진다.
    """
    monkeypatch.setattr(purchase_detail, "get_db_schema", lambda: "haetdeul")


def _줄(
    *,
    purchase_item_id: str = PITEM_ID,
    purchase_id: str = PURCHASE_ID,
    item_id: str = "ITEM-BAECHU",
    grade: str | None = None,
    quantity_kg: Decimal = Decimal("3587.000000"),
    unit_price_krw_per_kg: Decimal = Decimal("854.000000"),
) -> dict[str, Any]:
    return {
        "purchase_item_id": purchase_item_id,
        "purchase_id": purchase_id,
        "item_id": item_id,
        "grade": grade,
        "quantity_kg": quantity_kg,
        "unit_price_krw_per_kg": unit_price_krw_per_kg,
    }


class 가짜커서:
    """작은 `purchase_items` 표를 들고 **파라미터로 실제로 거른다.**

    ★ SQL 문자열만 보는 검사는 *"열쇠가 맞나"* 를 못 잰다. 넘어온 값으로 진짜 필터를
      걸고 `LIMIT` 도 그대로 적용해, 세 번째 줄이 잘려서 숨는 일이 없는지 본다.
    """

    def __init__(self, table: list[dict[str, Any]], log: list[Any], *, 매핑행: bool) -> None:
        self._table = table
        self._매핑행 = 매핑행
        self.log = log
        self._rows: list[Any] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        text = str(query)
        self.log.append((text, params))
        assert isinstance(params, tuple), "열쇠를 튜플로 넘겨야 한다"
        (purchase_id,) = params
        matched = sorted(
            (row for row in self._table if row["purchase_id"] == purchase_id),
            key=lambda row: row["purchase_item_id"],
        )
        한계 = re.search(r"LIMIT\s*'?\), Literal\((\d+)\)", text) or re.search(
            r"Literal\((\d+)\)", text
        )
        if 한계:
            matched = matched[: int(한계.group(1))]
        self._rows = [
            {name: row[name] for name in _칸순서}
            if self._매핑행
            else tuple(row[name] for name in _칸순서)
            for row in matched
        ]

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        raise AssertionError(
            "fetchone 을 쓰면 2행 이상이 조용히 첫 행으로 나간다 —"
            " 무결성 위반이 정상 응답이 되는 자리다"
        )


class 가짜커넥션:
    def __init__(self, table: list[dict[str, Any]] | None = None, *, 매핑행: bool = False) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.log: list[Any] = []
        self.커서들: list[가짜커서] = []
        self._table = table or []
        self._매핑행 = 매핑행

    def cursor(self) -> 가짜커서:
        cur = 가짜커서(self._table, self.log, 매핑행=self._매핑행)
        self.커서들.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"items 를 뒤지지 않는다"* 고 **설명하는 문장**이 조회로
       잡힌다. 설명과 실행문은 다른 것이고, 잠가야 할 것은 후자다.

    ★ 문자열 리터럴은 남긴다 — SQL 이 문자열 안에 있어서다.
    """
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                코드 = 코드.replace(docstring, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _원문() -> str:
    return Path(purchase_detail.__file__).read_text(encoding="utf-8")


# ── 1~4. 적힌 사실을 그대로 나른다 ──────────────────────────────────────


def test_1_한_줄이면_그대로_돌려준다():
    conn = 가짜커넥션([_줄()])

    상세 = fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    assert 상세 == PurchaseDetail(
        purchase_item_id=PITEM_ID,
        item_id="ITEM-BAECHU",
        grade=None,
        quantity_kg=Decimal("3587.000000"),
        unit_price_krw_per_kg=Decimal("854.000000"),
    )


def test_2_grade_가_NULL_이면_None_그대로다():
    """🔴 **정규화하지 않는다.** 등급 사다리(#69) 전이라 지금 무엇으로 바꾸면
    그 추측이 로트의 등급으로 굳는다.
    """
    conn = 가짜커넥션([_줄(grade=None)])

    assert fetch_purchase_detail(conn, purchase_id=PURCHASE_ID).grade is None


def test_2b_grade_에_값이_있으면_그_값_그대로다():
    """★ `상품 → 상` 같은 임의 치환을 넣지 않는다 (물류 정규화표도 의도적으로 비어 있다)."""
    conn = 가짜커넥션([_줄(grade="상품")])

    assert fetch_purchase_detail(conn, purchase_id=PURCHASE_ID).grade == "상품"


def test_3_수량의_Decimal_자릿수가_보존된다():
    """★ `3587.000000` 을 `3587` 로 줄이지 않는다 — 값은 같아도 **적힌 사실이 달라진다.**"""
    conn = 가짜커넥션([_줄(quantity_kg=Decimal("3587.000000"))])

    수량 = fetch_purchase_detail(conn, purchase_id=PURCHASE_ID).quantity_kg

    assert isinstance(수량, Decimal)
    assert str(수량) == "3587.000000"


def test_4_단가의_Decimal_자릿수가_보존된다():
    conn = 가짜커넥션([_줄(unit_price_krw_per_kg=Decimal("854.000000"))])

    단가 = fetch_purchase_detail(conn, purchase_id=PURCHASE_ID).unit_price_krw_per_kg

    assert isinstance(단가, Decimal)
    assert str(단가) == "854.000000"


def test_4b_float_은_받지_않는다():
    """🔴 `Decimal(float)` 은 0.1 의 이진 오차를 그대로 들여와 단가에 안 보이는 꼬리를
    남긴다 (`ledger` · `transition` · `master/ledger._scaled` 가 모두 같은 규율이다).
    """
    conn = 가짜커넥션([_줄(unit_price_krw_per_kg=854.0)])  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="float"):
        fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)


def test_4c_매입_금액_규칙을_다시_계산하지_않는다():
    """🔴 단가(`총액 ÷ 총량`)도 금액 검증도 **매입이 이미 했다.**

    다시 하면 같은 사실의 주인이 둘이 되고, 매입이 식을 바꾸는 날 조용히 갈린다.
    """
    코드 = _코드만(_원문())

    for 금지 in ("line_amount", "total_amount", "/ total", "ROUND_HALF_UP", "quantize"):
        assert 금지 not in 코드, f"{금지} — 매입 계산을 복제하고 있다"


# ── 5~8. 0 · 1 · 2+ 를 가른다 ───────────────────────────────────────────


def test_5_행이_없으면_Missing_이다():
    """🔴 대체 데이터를 만들지 않는다 — 시세·평균원가·품목명 유추 전부."""
    conn = 가짜커넥션([])

    with pytest.raises(PurchaseDetailMissing) as 오류:
        fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    assert PURCHASE_ID in str(오류.value)


def test_6_두_행이면_Ambiguous_다():
    conn = 가짜커넥션([_줄(purchase_item_id="PITEM-B"), _줄(purchase_item_id="PITEM-A")])

    with pytest.raises(PurchaseDetailAmbiguous) as 오류:
        fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    메시지 = str(오류.value)
    assert "PITEM-A" in 메시지 and "PITEM-B" in 메시지, "부딪힌 줄이 보여야 조사할 수 있다"


def test_7_세_행이어도_Ambiguous_다():
    """★ `LIMIT 2` 가 세 번째 줄을 잘라도 **모호함은 그대로 잡힌다.**"""
    conn = 가짜커넥션(
        [
            _줄(purchase_item_id="PITEM-A", item_id="ITEM-BAECHU"),
            _줄(purchase_item_id="PITEM-B", item_id="ITEM-MU"),
            _줄(purchase_item_id="PITEM-C", item_id="ITEM-YANGPA"),
        ]
    )

    with pytest.raises(PurchaseDetailAmbiguous):
        fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)


def test_8_첫_행을_조용히_고르지_않는다():
    """🔴 첫 행도 · 최신도 · 한글 품목명이 맞는 것도 · 싼 것도 · 등급 높은 것도
    **전부 고르는 것**이다. 고른 뒤에는 버려진 줄이 있었다는 사실조차 안 남는다.
    """
    conn = 가짜커넥션(
        [
            _줄(purchase_item_id="PITEM-A", grade="상품", unit_price_krw_per_kg=Decimal(100)),
            _줄(purchase_item_id="PITEM-B", grade=None, unit_price_krw_per_kg=Decimal(900)),
        ]
    )

    with pytest.raises(PurchaseDetailAmbiguous):
        fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    코드 = _코드만(_원문())
    assert "rows[0]" in 코드, "1행일 때만 첫 원소를 쓴다 — 그 앞에 개수 검사가 있다"
    assert "fetchone" not in 코드


# ── 9~10. 없는 열쇠로 묻지 않는다 ───────────────────────────────────────


@pytest.mark.parametrize(
    "빈값", ["", "   ", "\t", "\n", None], ids=["빈문자열", "공백", "탭", "줄바꿈", "None"]
)
def test_9_10_빈_purchase_id_는_DB_에_묻기도_전에_막힌다(빈값: str | None):
    """🔴 **없는 것과 물어보지 못한 것은 다른 사실이다.**

    빈 열쇠로 조회하면 0행이 돌아오고 그 0행은 *"매입 줄이 없다"* 로 읽힌다.
    """
    conn = 가짜커넥션([_줄()])

    with pytest.raises(InvalidPurchaseIdentity):
        fetch_purchase_detail(conn, purchase_id=빈값)  # type: ignore[arg-type]

    assert conn.log == [], "질의를 보내기 전에 멈춰야 한다"
    assert conn.커서들 == [], "커서도 열지 않는다"


# ── 11~13. 열쇠는 purchase_id 하나다 ───────────────────────────────────


def test_11_열쇠가_purchase_id_하나뿐이다():
    conn = 가짜커넥션([_줄()])

    fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    질의, 파라미터 = conn.log[0]
    assert 파라미터 == (PURCHASE_ID,), "열쇠는 하나뿐이다"
    assert "purchase_id = %s" in 질의
    assert "purchase_items" in 질의
    assert "ORDER BY purchase_item_id" in 질의
    assert "Literal(2)" in 질의, "0·1·2+ 를 가르려면 둘까지 읽어야 한다"


def test_11b_다른_purchase_id_의_줄은_안_섞인다():
    conn = 가짜커넥션([_줄(purchase_id="PUR-OTHER-D1-S1", purchase_item_id="PITEM-OTHER")])

    with pytest.raises(PurchaseDetailMissing):
        fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)


def test_12_13_items_표나_품목명으로_찾지_않는다():
    """🔴 **`inbound_receipts` 는 `purchase_item_id` 와 `item_id` 를 따로 든다.**

    둘이 같은 품목인지 검사하는 복합 FK 도 CHECK 도 없어서(실측), 물류가 한글
    이름으로 독립 번역하면 두 칸이 다른 품목을 가리켜도 DB 가 통과시킨다.
    찾은 그 줄의 `item_id` 를 그대로 써야 두 FK 가 구조적으로 일치한다.
    """
    코드 = _코드만(_원문())

    assert "items" not in 코드.replace("purchase_items", ""), "items 표를 뒤지고 있다"
    assert "item_name" not in 코드
    assert "InTransitItem" not in 코드
    assert "배추" not in 코드, "한글 품목명이 코드에 있다"


def test_13b_시세나_예측가를_쓰지_않는다():
    코드 = _코드만(_원문())

    for 금지 in ("market_quote", "market_price", "forecast", "average", "auction", "policy"):
        assert 금지 not in 코드.lower(), f"{금지} — 없는 값을 만들고 있다"


# ── 14~18. 남의 파트도 쓰기도 없다 ──────────────────────────────────────


def test_14_15_다른_파트를_임포트하지_않는다():
    """★ 매입·마스터 코드를 끌어오지 않는다 — **표만 읽는다.**"""
    tree = ast.parse(_원문())
    모듈: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            모듈.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            모듈.add(node.module)

    남의것 = [
        name
        for name in 모듈
        if name.startswith(("app.master", "app.purchase", "app.finance", "app.sales"))
    ]
    assert 남의것 == [], 남의것
    assert 모듈 <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "decimal",
        "typing",
        "psycopg",
        "app.logistics.db",
    }, 모듈


def test_16_쓰기_문장이_없다():
    """🔴 이 단계는 **읽기 전용**이다."""
    코드 = _코드만(_원문())

    for 금지 in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER"):
        assert 금지 not in 코드, f"{금지} 가 있다"


def test_16b_잠금을_걸지_않는다():
    """★ 잠금은 나중의 Receipt 쓰기 트랜잭션이 소유한다."""
    코드 = _코드만(_원문())

    assert "pg_advisory" not in 코드
    assert "FOR UPDATE" not in 코드


def test_17_커밋도_롤백도_하지_않는다():
    코드 = _코드만(_원문())
    assert "commit" not in 코드
    assert "rollback" not in 코드

    conn = 가짜커넥션([_줄()])
    fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_18_자기_커넥션을_열지_않는다():
    """🔴 커넥션을 새로 열면 바깥 트랜잭션 **밖에서** 읽게 된다.

    ★ 그래서 `repository.fetch_all` 도 쓰지 않는다 — 그쪽이 자기 커넥션을 연다.
    """
    코드 = _코드만(_원문())
    assert "get_connection" not in 코드
    assert "fetch_all" not in 코드

    conn = 가짜커넥션([_줄()])
    fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    assert len(conn.커서들) == 1
    assert conn.closed == 0, "받은 커넥션을 닫지 않는다"


# ── 19~20. row_factory 를 강요하지 않는다 ───────────────────────────────


@pytest.mark.parametrize("매핑행", [False, True], ids=["튜플행", "매핑행"])
def test_19_20_row_factory_가_무엇이든_같은_답이다(매핑행: bool):
    """★ 커넥션을 만드는 곳은 배선 자리다 — 이 모듈이 한쪽 모양을 강요하지 않는다."""
    conn = 가짜커넥션([_줄()], 매핑행=매핑행)

    상세 = fetch_purchase_detail(conn, purchase_id=PURCHASE_ID)

    assert 상세.purchase_item_id == PITEM_ID
    assert 상세.item_id == "ITEM-BAECHU"
    assert 상세.quantity_kg == Decimal("3587.000000")


# ── 실데이터 모양 회귀 ──────────────────────────────────────────────────


def test_현재_실데이터_모양을_고정한다():
    """★ **2026-09-05 실 DB 실측 모양 그대로다.**

    ```text
    purchase_id            PUR-THRU-20260105-BAECHU-D1-S1
    → purchase_item_id     PITEM-THRU-20260105-BAECHU-D1-S1-BAECHU
    → item_id              ITEM-BAECHU
    → grade                None          ← 승인이 등급을 안 실었다
    → quantity_kg          3587.000000   ← in_transit 의 3587.0 과 같은 값이다
    → unit_price_krw_per_kg 854.000000
    ```
    """
    conn = 가짜커넥션([_줄()])

    상세 = fetch_purchase_detail(conn, purchase_id="PUR-THRU-20260105-BAECHU-D1-S1")

    assert 상세.purchase_item_id == "PITEM-THRU-20260105-BAECHU-D1-S1-BAECHU"
    assert 상세.item_id == "ITEM-BAECHU"
    assert 상세.grade is None
    assert 상세.quantity_kg == Decimal("3587.000000")
    assert 상세.unit_price_krw_per_kg == Decimal("854.000000")
    # ★ 물류 일정의 수량과 **값으로는** 같다 (Decimal 은 자릿수를 무시하고 비교한다).
    assert 상세.quantity_kg == Decimal("3587.0")


# ── 아직 답하지 않는 것 ─────────────────────────────────────────────────


def test_수량_대조를_이_단계에서_정하지_않았다():
    """⚠️ **일정 수량 ↔ 매입 수량 대조 규칙을 여기 넣지 않았다.**

    두 값의 출처는 같은 `leg.qty_kg` 지만 **가공이 다르다.**

    ```text
    매입   master/ledger._scaled  →  6자리로 quantize (ROUND_HALF_UP)
    물류   transition             →  Decimal(str(qty))  자릿수 그대로
    ```

    🔴 소수 6자리를 넘는 수량(예: 1000kg 을 3회차로 나눈 333.3333333333333)에서
       **두 값이 실제로 달라진다.** 지금 근거로는 "정확히 같다"를 계약으로 못 박을 수
       없어, 대조 규칙은 Receipt 단계로 넘긴다 (보고서에 적었다).
    """
    코드 = _코드만(_원문())

    assert "scheduled_quantity" not in 코드
    assert "validate_inbound_quantity" not in 코드
