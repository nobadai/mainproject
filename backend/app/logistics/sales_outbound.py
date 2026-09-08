"""Logistics adapter for Sales-confirmed outbound reservation requests.

```text
SalesOutboundReservationRequest  (공용 DTO · 판매와 물류가 함께 쓴다)
   → reserve_confirmed_sale            전량 아니면 멈춘다      (사람 · 일반 경로)
   → reserve_confirmed_sale_available  확보되는 만큼만 잡는다  (시뮬레이션 경로)
```

🔴 **두 문이 같은 DTO 를 받는다.** 시뮬레이션이라고 다른 요청 모양을 만들지 않는다 —
   *"무엇을 누구에게 얼마나 파는가"* 는 판매 사실이고 경로에 따라 달라지지 않는다.
   갈리는 것은 **모자랄 때 멈출 것인가** 하나뿐이다.

⚠️ **`SalesOutboundReservationRequest` 를 안 고친다.** 이 파일은 그 DTO 를 풀어
   물류 코어에 넘기기만 한다 — 필드를 더하는 것은 판매와 함께 정할 일이다.

★ **Master 가 코어를 직접 부르지 않게 하는 자리다.** 이 경계가 없으면 Master 가
  DTO 를 뜯어 `outbound.reserve_available_stock` 에 손으로 넘겨야 하고, 그러면
  판매→물류 어댑터를 우회하는 두 번째 길이 생긴다.
"""

from __future__ import annotations

from typing import Any

from app.contracts.sales_logistics import SalesOutboundReservationRequest
from app.logistics.outbound import ReservationResult, reserve_available_stock, reserve_stock

__all__ = [
    "reserve_confirmed_sale",
    "reserve_confirmed_sale_available",
]


def reserve_confirmed_sale(
    conn: Any,
    request: SalesOutboundReservationRequest,
) -> ReservationResult:
    """Reserve stock for a confirmed sale without allocating or shipping any Lot.

    🔴 **전량 아니면 멈춘다.** 가용재고가 모자라면 `InvalidOutboundRequest` 다 —
       `reserve_stock` 의 fail-closed 계약 그대로이고, 이 문은 그것을 안 바꾼다.
    """

    return reserve_stock(
        conn,
        reservation_id=request.reservation_id,
        sim_run_id=request.sim_run_id,
        item_id=request.item_id,
        required_qty_kg=request.quantity_kg,
        sale_id=request.sale_id,
        as_of=request.as_of,
    )


def reserve_confirmed_sale_available(
    conn: Any,
    request: SalesOutboundReservationRequest,
) -> ReservationResult:
    """확정된 판매분을 **확보되는 만큼만** 잡아 둔다 (시뮬레이션 경로).

    ```text
    required_qty_kg = request.quantity_kg   🔴 판매 요구량 그대로. 물류가 안 줄인다
    reserved_qty_kg = 실제 확보량           ← 이 값만 모자랄 수 있다
    ```

    ★ **재실행이 채운다.** 새 재고가 들어온 뒤 같은 요청을 다시 넘기면
      `required_qty_kg` 까지 추가로 확보한다 (`reserve_available_stock` 의 top-up).

    ⚠️ **판매 수량을 물류가 고쳐 넘기지 않는다.** `quantity_kg` 는 그대로 요구량으로
       가고, 못 잡은 몫은 `ReservationResult.reserved_qty_kg` 로 **보이게** 남는다.
    """

    return reserve_available_stock(
        conn,
        reservation_id=request.reservation_id,
        sim_run_id=request.sim_run_id,
        item_id=request.item_id,
        required_qty_kg=request.quantity_kg,
        sale_id=request.sale_id,
        as_of=request.as_of,
    )
