"""purchase_detail.py — 매입 원장에 **적힌 사실을 읽어 온다** (3-B4-E).

```text
DueInbound.purchase_id  →  purchase_items 한 줄  →  purchase_item_id · item_id
                                                     · grade · quantity_kg
                                                     · unit_price_krw_per_kg
```

★ **`receipts.py` 와 나눈 이유.** 두 책임이 다르다.

  ```text
  receipts.py         입고 정체성 — 이 건에 Receipt 가 이미 있나
  purchase_detail.py  매입 참조 해석 — 그 참조가 가리키는 매입 줄은 무엇인가
  ```

🔴 **매입 업무 규칙을 다시 계산하지 않는다.** 단가(`총액 ÷ 총량`)도, 금액 검증
   (`|line_amount − qty × unit_price| < 0.1`)도, 품목명 번역(`배추 → ITEM-BAECHU`)도
   **매입 쪽이 이미 했고 그 결과가 표에 앉아 있다.** 여기서 다시 하면 같은 사실의
   주인이 둘이 되고, 매입이 식을 바꾸는 날 두 값이 조용히 갈린다.

🔴 **`items` 표를 품목명으로 다시 뒤지지 않는다.** `inbound_receipts` 에는
   `purchase_item_id` FK 와 `item_id` FK 가 **따로** 있는데, **둘이 같은 품목을
   가리키는지 검사하는 복합 FK 도 CHECK 도 없다** (2026-09-05 카탈로그 실측).
   물류가 `InTransitItem.item`(한글 이름)으로 독립 번역하면 두 칸이 다른 품목을
   가리켜도 DB 가 통과시킨다. **찾은 그 줄의 `item_id` 를 그대로 쓰면** 두 FK 가
   구조적으로 일치한다.

🔴 **없는 값을 만들지 않는다.** 시세·예측가·평균원가·정책 기본값을 대신 쓰지 않고,
   `grade` 가 NULL 이면 NULL 그대로 나른다. 등급 사다리(#69) 전이라 지금 NULL 을
   무엇으로 바꾸면 그 추측이 로트의 등급으로 굳는다.

🔴 **`purchase_id` 를 짓거나 뜯지 않는다.** 그 ID 의 주인은 마스터다
   (`app/master/transition.py` 의 `purchase_id_for`). `inbound_id` · `approval_id`
   를 들여다보고 `PUR-…` 를 조립하지 않는다 — 받은 값으로 묻기만 한다.

★ **읽기만 한다.** INSERT · UPDATE · DELETE · DDL · advisory lock · `FOR UPDATE` 가
  없다. 잠금은 나중의 Receipt 쓰기 트랜잭션이 소유한다.

★ **스키마 실측 (현재 브랜치 `database/10_domain_schema.sql`).**

  ```text
  PRIMARY KEY   purchase_items_pkey (purchase_item_id)    단독
  UNIQUE        🔴 (purchase_id, item_id) 유일성 제약이 **없다**
  purchase_id   text NOT NULL   FK → purchases (ON DELETE CASCADE)
  item_id       text NOT NULL   FK → items
  grade         text            🔴 nullable
  quantity_kg            numeric(18,6) NOT NULL  CHECK >= 0
  unit_price_krw_per_kg  numeric(18,6) NOT NULL  CHECK >= 0
  ```

  🔴 **`purchase_id` → `purchase_items` 는 1:N 이다** (실측: detail 5행짜리 매입
     16건 · 1행짜리 1건). 지금 승인 경로가 만드는 것은 1행뿐이지만 **DB 가 그것을
     보장하지 않는다** — 그래서 첫 행을 집지 않고 *"정확히 1행"* 을 요구한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "InvalidPurchaseIdentity",
    "PurchaseDetail",
    "PurchaseDetailAmbiguous",
    "PurchaseDetailError",
    "PurchaseDetailMissing",
    "fetch_purchase_detail",
]


#: 읽어 오는 칸과 **그 순서**. SELECT 의 칸 순서와 짝이라 한쪽만 고치면 튜플
#: row_factory 에서 값이 밀린다.
_DETAIL_COLUMNS = (
    "purchase_item_id",
    "item_id",
    "grade",
    "quantity_kg",
    "unit_price_krw_per_kg",
)

#: 🔴 **둘까지만 읽는다.** 0 · 1 · 2+ 를 가르는 데 그 이상이 필요 없다.
#:
#: ★ 정확한 개수를 세도 **결정이 달라지지 않는다** — 2행이든 5행이든 우리는 어느
#:   하나도 고르지 않고 멈춘다.
_AMBIGUITY_PROBE_LIMIT = 2


class PurchaseDetailError(RuntimeError):
    """이 모듈이 내는 실패의 조상 (`receipts.ReceiptLookupError` 와 같은 결)."""


class InvalidPurchaseIdentity(PurchaseDetailError, ValueError):
    """조회 열쇠로 쓸 수 없는 `purchase_id` 다.

    🔴 **없는 열쇠로 묻지 않는다.** 물으면 0행이 나오고, 그 0행은 *"매입 줄이
       없다"* 로 읽힌다 — **없는 것과 물어보지 못한 것이 같은 답으로 뭉개진다.**

    ★ `arrival.select_due_inbound` 이 이미 같은 눈으로 걸러 준다
      (`ARRIVAL_PURCHASE_REFERENCE_MISSING`). 여기서 다시 보는 것은 이 함수가 그
      경로 밖에서도 안전해야 하기 때문이지, 저쪽을 못 믿어서가 아니다.
    """


class PurchaseDetailMissing(PurchaseDetailError, LookupError):
    """입고가 가리키는 `purchase_id` 에 매입 줄이 **하나도 없다.**

    🔴 **대체 데이터를 만들지 않는다.** 시세를 조회하지도, 품목명으로 `item_id` 를
       유추하지도, 평균원가를 쓰지도 않는다. 승인이 만든 매입 줄이 없는데 물건이
       도착했다는 것은 **무결성 문제**이지 값이 모자란 상태가 아니다.
    """


class PurchaseDetailAmbiguous(PurchaseDetailError, ValueError):
    """같은 `purchase_id` 에 매입 줄이 **둘 이상이다.**

    🔴 **어느 것도 고르지 않는다.** 첫 행도, 최신도, 한글 품목명이 맞는 것도,
       싼 것도, 등급 높은 것도 **전부 고르는 것**이다. 고른 뒤에는 버려진 줄이
       있었다는 사실조차 남지 않고, 그 줄의 단가가 로트 원가로 굳는다.

    ★ `repository.get_active_logistics_runtime_fixture`(활성 fixture 2건) ·
      `receipts.check_receipt_state`(Receipt 2건) 와 같은 규율이다.

    ⚠️ **지금 MVP 계약은 매입당 상세 1행이다.** 다품목 매입 지원은 나중의 계약
       확장이고(그때 열쇠가 `purchase_id + item_id` 로 넓어진다), 여기서 미리
       고르는 규칙을 만들지 않는다.
    """


@dataclass(frozen=True)
class PurchaseDetail:
    """매입 원장이 확정해 둔 사실. **입고 처리에 필요한 것만 담는다.**

    ★ `market_name` · `line_amount_krw` · `source_quote_id` 를 안 싣는다 —
      입고가 쓰지 않는 값이고, 실어 두면 뒤 단계가 **여기서 읽은 낡은 값**을 쓴다.
    """

    purchase_item_id: str
    item_id: str
    #: 🔴 **정규화하지 않는다.** DB 가 NULL 이면 `None` 그대로다.
    #:   `상품 → 상` 같은 임의 치환은 물류 정규화표에서도 의도적으로 비어 있다
    #:   (`repository._RAW_GRADE_NORMALIZATION`).
    grade: str | None
    quantity_kg: Decimal
    unit_price_krw_per_kg: Decimal


def _cell(row: Any, index: int, name: str) -> Any:
    """한 행에서 한 칸을 꺼낸다.

    ★ row_factory 가 무엇이냐에 따라 튜플로도 매핑으로도 온다. 커넥션을 만드는 곳은
      배선 자리이고 이 모듈은 받아 쓸 뿐이라 한쪽 모양을 강요하지 않는다
      (`receipts._cell` · `ledger._cell` 과 같은 이유다).
    """
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _text(value: Any, *, 칸: str, purchase_id: str) -> str:
    """NOT NULL 문자열 칸을 좁힌다."""
    if not isinstance(value, str) or not value.strip():
        raise TypeError(
            f"purchase_items.{칸} 를 문자열로 읽을 수 없다: {value!r}"
            f" (purchase_id={purchase_id!r})."
        )
    return value


def _numeric(value: Any, *, 칸: str, purchase_id: str) -> Decimal:
    """`numeric(18,6)` 칸을 `Decimal` 로 좁힌다.

    🔴 **`float` 을 받지 않는다.** `Decimal(float)` 은 0.1 이 갖고 있는 이진 오차를
       그대로 들여와 수량·단가에 안 보이는 꼬리를 남긴다 (`ledger._quantity` ·
       `transition.build_next_inventory` · `master/ledger._scaled` 가 모두 같은
       이유로 `Decimal(str(x))` 를 쓴다). psycopg 는 `numeric` 을 `Decimal` 로
       돌려주므로 정상 경로에서는 그대로 지나간다.

    ★ **자릿수를 다시 맞추지 않는다.** `Decimal("3587.000000")` 을 `3587` 로 줄이면
      DB 에 적힌 모양이 사라진다 — 값은 같아도 적힌 사실이 달라진다.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(
        f"purchase_items.{칸} 를 Decimal 로 읽을 수 없다: {value!r} ({type(value).__name__})"
        f" (purchase_id={purchase_id!r}). float 은 받지 않는다 — 이진 오차가 단가에 남는다."
    )


def fetch_purchase_detail(conn: Any, *, purchase_id: str) -> PurchaseDetail:
    """`purchase_id` 가 가리키는 매입 줄 **하나**를 읽는다. **읽기만 한다.**

    ```text
    0행      PurchaseDetailMissing     매입 줄이 없다 — 대체 데이터를 만들지 않는다
    1행      PurchaseDetail            적힌 사실 그대로
    2행 이상  PurchaseDetailAmbiguous   어느 것도 고르지 않는다
    ```

    🔴 **열쇠는 `purchase_id` 하나다.** 품목명·도착예정일·`inbound_id` 로 좁히지
       않는다 — 그것들은 매입 줄의 정체성이 아니고, 한글 이름으로 좁히는 순간
       물류가 품목 번역의 두 번째 주인이 된다.

    🔴 **`fetchone()` 을 쓰지 않는다.** 그것은 2행 이상을 **조용히 첫 행으로**
       돌려준다 — 무결성 위반이 정상 응답으로 나가는 자리가 정확히 거기다.

    🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.** 받은 `conn` 만 쓴다 —
       도착 → Receipt 존재 → 매입 상세 → Receipt → 검수 → Lot → 원장 IN →
       일정 정리가 **한 바깥 트랜잭션**으로 묶여야 하고, 그 커밋은 호출자가 한 번 한다.

    :param conn: 호출자가 소유한 커넥션. 이 함수는 수명을 관리하지 않는다.
    :param purchase_id: 마스터가 만든 매입 참조. 비어 있으면 안 된다.
    :raises InvalidPurchaseIdentity: `purchase_id` 가 비었거나 공백뿐일 때.
    :raises PurchaseDetailMissing: 그 참조에 매입 줄이 없을 때.
    :raises PurchaseDetailAmbiguous: 매입 줄이 둘 이상일 때.
    """
    # ★ **DB 에 묻기 전에 막는다.** 빈 열쇠로 조회하면 0행이 돌아오고, 그 0행은
    #   "매입 줄이 없다" 로 읽힌다 — 두 사실이 뭉개진다.
    if not purchase_id or not purchase_id.strip():
        raise InvalidPurchaseIdentity(
            f"매입 상세 조회에 쓸 수 없는 purchase_id 다: {purchase_id!r}."
            " 없는 열쇠로 물으면 0행이 돌아오고 그것은 '매입 줄이 없다' 로 읽힌다 —"
            " 없는 것과 물어보지 못한 것은 다른 사실이다."
            " 그리고 이 값을 물류가 지어내거나 다른 ID 에서 조립하지 않는다."
        )

    schema = sql.Identifier(get_db_schema())
    # ★ `ORDER BY purchase_item_id` 는 **깨진 경우의 메시지를 결정적으로** 만든다.
    query = sql.SQL(
        """
        SELECT purchase_item_id, item_id, grade, quantity_kg, unit_price_krw_per_kg
        FROM {}.purchase_items
        WHERE purchase_id = %s
        ORDER BY purchase_item_id
        LIMIT {}
        """
    ).format(schema, sql.Literal(_AMBIGUITY_PROBE_LIMIT))

    with conn.cursor() as cursor:
        cursor.execute(query, (purchase_id,))
        rows = cursor.fetchall()

    if not rows:
        raise PurchaseDetailMissing(
            f"매입 줄이 없다: purchase_id={purchase_id!r}."
            " 승인이 만든 매입 줄 없이 물건이 도착했다는 뜻이라 무결성 문제다 —"
            " 시세·평균원가·품목명 유추로 대신 채우지 않는다."
        )
    if len(rows) > 1:
        보인것 = [_cell(row, 0, "purchase_item_id") for row in rows]
        raise PurchaseDetailAmbiguous(
            f"한 purchase_id 에 매입 줄이 둘 이상이다: purchase_id={purchase_id!r},"
            f" {보인것!r} …."
            " 어느 것이 이 입고의 줄인지 여기서 고르지 않는다 — 첫 행도 최신도"
            " 품목명이 맞는 것도 고르지 않는다."
            " 지금 MVP 계약은 매입당 상세 1행이고, 다품목 지원은 나중의 계약 확장이다."
        )

    row = rows[0]
    값 = {name: _cell(row, index, name) for index, name in enumerate(_DETAIL_COLUMNS)}
    grade = 값["grade"]
    if grade is not None and not isinstance(grade, str):
        raise TypeError(
            f"purchase_items.grade 를 읽을 수 없다: {grade!r} (purchase_id={purchase_id!r})."
        )

    return PurchaseDetail(
        purchase_item_id=_text(
            값["purchase_item_id"], 칸="purchase_item_id", purchase_id=purchase_id
        ),
        item_id=_text(값["item_id"], 칸="item_id", purchase_id=purchase_id),
        # 🔴 **NULL 을 그대로 나른다.** 여기서 무엇으로 바꾸면 그 추측이 로트의
        #    등급으로 굳는다 — 등급 사다리(#69) 가 정해지기 전이다.
        grade=grade,
        quantity_kg=_numeric(값["quantity_kg"], 칸="quantity_kg", purchase_id=purchase_id),
        unit_price_krw_per_kg=_numeric(
            값["unit_price_krw_per_kg"], 칸="unit_price_krw_per_kg", purchase_id=purchase_id
        ),
    )
