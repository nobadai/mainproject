"""판매 → 물류 출고 봉투가 **납품일 칸**을 들고 있는지 잰다.

🔴 **이름을 재는 검사가 여기 있는 이유** — 저장소에서 `due_date` 가 두 뜻이다.

```text
inventory_reservations.due_date   **납품** 기일
sales.collection_due_date         **수금** 기일
receivables.due_date              **수금** 기일
```

셋 다 같은 이름인데 앞의 하나만 다른 사실이다. 이미 선 물류 칸은 안 고치기로 했고
(이름 바꾸는 비용이 얻는 것보다 크다) **새로 내는 자리에서만 갈라 놓기로** 했다.
그 결정을 문서가 아니라 검사로 잠근다 — 문서는 다음 사람이 안 읽는다.
"""

from __future__ import annotations

import dataclasses
import typing
from datetime import date

from app.contracts.sales_logistics import (
    SalesOutboundReservationRequest,
    reservation_id_for_sale_item,
)


def _칸들() -> dict[str, type]:
    """`from __future__ import annotations` 때문에 주석이 문자열이라 풀어서 본다."""
    풀린 = typing.get_type_hints(SalesOutboundReservationRequest)
    return {칸.name: 풀린[칸.name] for 칸 in dataclasses.fields(SalesOutboundReservationRequest)}


def test_출고_봉투에_납품일_칸이_없다():
    """🔴 **납품일을 여기 복사하지 않는다** (2026-09-08 · 물류·판매 합의).

    `sales.sale_date` 가 정본이고 DDL 주석이 그렇게 정의한다. 같은 날짜를 이 봉투에
    복사하면 두 값이 갈리는 날이 온다 — 마스터가 그날 `sale_date` 인 판매를 골라
    `reservation_id_for_sale_item` 으로 예약 이름을 **계산**한다.

    ⚠️ 그래서 `inventory_reservations.due_date` 가 `NULL` 인 것은 결함이 아니다.
    """
    칸들 = _칸들()
    새_날짜칸 = {이름 for 이름 in 칸들 if 이름.endswith("_date") and 이름 != "as_of"}

    assert not 새_날짜칸, (
        f"출고 봉투에 날짜 칸이 생겼다: {sorted(새_날짜칸)}."
        " 납품일은 sales.sale_date 가 정본이고 여기 복사하지 않는다"
    )


def test_출고_봉투에_due_date_라는_이름의_칸은_없다():
    """🔴 **어휘 검사.** 앞으로 누가 이 봉투에 `due_date` 를 더하는 것을 막는다.

    이 봉투에서 `due_date` 는 어느 쪽을 뜻하는지 읽는 사람이 못 가른다.
    납품이면 `sales.sale_date`, 수금이면 `collection_due_date` 다.
    """
    칸들 = _칸들()

    assert "due_date" not in 칸들, (
        "출고 봉투에 due_date 칸이 생겼다 — 납품이면 sales.sale_date, "
        f"수금이면 collection_due_date 로 갈라 쓴다: {sorted(칸들)}"
    )


def test_예약_정체성_규칙은_그대로다():
    """칸을 더해도 기존 사실은 안 변했다 (회귀 방어)."""
    assert reservation_id_for_sale_item("SI-SALE-1-1") == "RSV-SI-SALE-1-1"

    for 빈값 in ("", "   "):
        try:
            reservation_id_for_sale_item(빈값)
        except ValueError:
            pass
        else:
            raise AssertionError(f"빈 sale_item_id 를 통과시켰다: {빈값!r}")
