"""입고 일정 Reader 의 **거르기 규칙 셋** (`inbound_schedules.*_from`).

```text
in_transit_from        has_receipt 가 거짓인 것       운송 중
receivable_from        stock_applied 가 거짓인 것     도착 처리 대상
pending_inbound_from   stock_applied 가 거짓인 것     미래 점유 (DTO 만 다르다)
```

★ **DB 를 안 읽는다.** `load_schedule_views` 가 낸 views 를 손으로 만들어 규칙만 본다 —
  세 Reader 가 같은 views 를 나눠 쓰게 된 뒤(2026-09-15 · 화면 한 판에 같은 질의 5번),
  거르기가 `*_at` 시절과 글자 그대로 같은지가 이 파일이 잠그는 것이다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.logistics.inbound_schedules import (
    InboundScheduleView,
    in_transit_from,
    pending_inbound_from,
    receivable_from,
)

_AS_OF = date(2026, 1, 20)


def _view(inbound_id: str, *, has_receipt: bool, stock_applied: bool) -> InboundScheduleView:
    return InboundScheduleView(
        inbound_id=inbound_id,
        sim_run_id="SIM-T",
        purchase_item_id=f"PI-{inbound_id}",
        purchase_id=f"PUR-{inbound_id}",
        item_id="ITEM-BAECHU",
        item_name="배추",
        quantity_kg=Decimal(100),
        expected_arrival_date=_AS_OF,
        created_as_of=_AS_OF,
        has_receipt=has_receipt,
        stock_applied=stock_applied,
    )


#: 네 조합 전부. 실제로 서는 것은 앞 셋이다 — `stock_applied` 는 Receipt 없이 참일 수 없다.
_VIEWS = (
    _view("INB-1", has_receipt=False, stock_applied=False),  # 아직 안 왔다
    _view("INB-2", has_receipt=True, stock_applied=False),  # 왔는데 검수에서 막혔다
    _view("INB-3", has_receipt=True, stock_applied=True),  # 재고가 섰다
)


def test_운송중은_Receipt_가_없는_것만() -> None:
    assert [x.inbound_id for x in in_transit_from(_VIEWS)] == ["INB-1"]


def test_도착처리_대상은_재고가_안_선_것_전부다() -> None:
    """🔴 Receipt 존재로 빼지 않는다 — 검수에서 막힌 건은 다음 실행이 이어받는다."""
    assert [x.inbound_id for x in receivable_from(_VIEWS)] == ["INB-1", "INB-2"]


def test_미래점유는_도착처리_대상과_같은_행이고_DTO_만_다르다() -> None:
    pending = pending_inbound_from(_VIEWS)
    assert [x.inbound_id for x in pending] == ["INB-1", "INB-2"]
    assert [x.date for x in pending] == [_AS_OF, _AS_OF]
    assert [x.expected_arrival_date for x in receivable_from(_VIEWS)] == [_AS_OF, _AS_OF]


def test_빈_views_는_빈_목록이다() -> None:
    assert in_transit_from(()) == []
    assert receivable_from(()) == []
    assert pending_inbound_from(()) == []
