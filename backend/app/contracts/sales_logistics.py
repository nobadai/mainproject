# ─────────────────────────────────────────────────────────────────────────────
# STATUS: 공용 계약 — 판매가 확정한 사실을 물류 출고 경계로 넘기는 봉투
#   🔴 **소유는 마스터다** (`app/contracts/`). 칸을 내고 이름을 정하는 것이 마스터 몫이다.
#
#   누가 쓰나
#     판매   `app/sales/outbound.py`      확정 판매를 봉투로 만든다 (보내는 쪽)
#     물류   `app/logistics/sales_outbound.py`  봉투를 받아 예약 코어를 부른다 (받는 쪽)
#
#   ⚠️ **미완 ① — `delivery_date` 를 채우는 쪽이 아직 없다.**
#      칸은 여기 섰지만 판매(`app/sales/outbound.py`)가 아직 안 채운다. 그 배선은
#      판매 소관이라 이 판에서 안 했다. 채워지기 전까지 이 봉투를 만드는 곳은
#      전부 `TypeError` 다 — **조용히 통과하지 않는다**는 것이 의도다.
#      🔴 기본값을 두지 않는다. 기본값이 곧 업무 규칙이 되고, 아무도 그것을 정한
#      적이 없다 (`app/logistics/inbound_execution.py` 가 같은 규율을 적어 뒀다).
#
#   ⚠️ **미완 ② — 물류가 `delivery_date` 를 코어로 안 넘긴다.**
#      `reserve_stock(..., due_date=)` 이 이미 그 칸을 받는데
#      `reserve_confirmed_sale` 이 안 넘겨서 `inventory_reservations.due_date` 가
#      전부 `NULL` 이다. 그 배선은 물류 소관이다 (PR #421).
#
#   ★ **이 판에서 필드 이름·클래스 이름은 하나도 안 바꿨다.** 물류가 이미 임포트한다.
#      더한 것은 `delivery_date` 하나뿐이다.
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

    # 🔴 **`due_date` 라고 부르지 않는다 — 저장소에서 그 이름이 두 뜻이다.**
    #
    #   inventory_reservations.due_date   **납품** 기일
    #   sales.collection_due_date         **수금** 기일 (= sale_date + payment_days)
    #   receivables.due_date              **수금** 기일 (같은 값)
    #
    #   셋 다 `due_date` 인데 앞의 하나만 다른 사실이다. 이미 선 물류 칸
    #   (`inventory_reservations.due_date`)은 안 고친다 — 이름 바꾸는 비용이 얻는
    #   것보다 크다. **새로 내는 자리에서만 갈라 놓는다** (2026-09-08 판매·물류 통보).
    delivery_date: date
    """물류 `due_date` 로 간다 (**납품** 기일).

    ⚠️ `sales.collection_due_date`(**수금** 기일)와 **다른 사실**이다.
    """


def reservation_id_for_sale_item(sale_item_id: str) -> str:
    """판매 품목 사실이 소유하는 결정적 예약 정체성."""

    if not isinstance(sale_item_id, str) or not sale_item_id.strip():
        raise ValueError("sale_item_id must not be blank")
    return f"RSV-{sale_item_id}"
