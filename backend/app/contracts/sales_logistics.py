# ─────────────────────────────────────────────────────────────────────────────
# STATUS: 공용 계약 — 판매가 확정한 사실을 물류 출고 경계로 넘기는 봉투
#   🔴 **소유는 마스터다** (`app/contracts/`). 칸을 내고 이름을 정하는 것이 마스터 몫이다.
#
#   누가 쓰나
#     판매   `app/sales/outbound.py`      확정 판매를 봉투로 만든다 (보내는 쪽)
#     물류   `app/logistics/sales_outbound.py`  봉투를 받아 예약 코어를 부른다 (받는 쪽)
#
#   🔴 **납품일 칸을 두지 않는다** (2026-09-08 · 물류·판매 합의).
#      `sales.sale_date` 가 납품일의 **정본**이고, DDL 주석이 그렇게 정의한다
#      (`database/10_domain_schema.sql:3534` — '판매/납품 기준일.').
#
#      ★ 여기에 같은 날짜를 **복사**하면 두 값이 갈리는 날이 온다. 대신 마스터가
#        그날 `sale_date` 인 판매를 골라 `reservation_id_for_sale_item` 으로
#        예약 이름을 **계산**한다 — 결정론이라 저장할 이유가 없다.
#
#      ⚠️ 그래서 `inventory_reservations.due_date` 가 `NULL` 로 남는 것은
#        **결함이 아니다.** 그 칸을 안 쓰기로 한 것이다.
#
#   ★ **이 판에서 필드 이름·클래스 이름은 하나도 안 바꿨다.** 물류가 이미 임포트한다.
# ─────────────────────────────────────────────────────────────────────────────
"""판매가 확정한 사실을 물류 출고 경계에 넘기는 공용 계약."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class SalesOutboundReservationRequest:
    """물류가 Lot 을 고르지 않고 **잡아 두기만** 할 수 있는 확정 판매 수량.

    ★ **납품일의 주인은 판매다.** DDL 주석이 이미 그렇게 정의한다.

    ```text
    database/10_domain_schema.sql
      sales.order_date   '고객 주문일.'
      sales.sale_date    '판매/납품 기준일.'        ← 납품 기준일이 여기다
    ```

    그리고 수금일이 그것에서 파생된다 — `app/sales/persistence.py` 가
    `request.sale_date + timedelta(days=payment_days)` 로 만든다.
    **납품일이 뿌리이고 수금일이 가지다.** 그래서 물류가 자기 날짜를 지어내지
    않고 판매가 준 것을 받는다.
    """

    reservation_id: str
    sim_run_id: str
    sale_id: str
    sale_item_id: str
    item_id: str
    quantity_kg: Decimal
    as_of: date



def reservation_id_for_sale_item(sale_item_id: str) -> str:
    """판매 품목 사실이 소유하는 결정적 예약 정체성."""

    if not isinstance(sale_item_id, str) or not sale_item_id.strip():
        raise ValueError("sale_item_id must not be blank")
    return f"RSV-{sale_item_id}"
