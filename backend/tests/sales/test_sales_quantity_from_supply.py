"""사람이 수량을 말하지 않은 판매 요청을 **물류가 확정한 수량**으로 세운다 (2026-09-11).

🔴 **자동 걷기에는 사람이 없다.** 206일 내내 판매를 부르는데 그 자리에서 수량을 말해
   줄 사람이 없어 `PROPOSAL_QUANTITY_REQUIRED` 로 막혔고, **안이 0건**이었다.

```text
물류 PRE_SALES → 마스터 봉투 → 판매      ← 매입 PRE_PURCHASE 와 **같은 길**이다
그 봉투의 `sellable_supply.inventory_by_item` 에 물류가 **확정한 수량**이 실린다
판매가 그것을 **안 읽고** 있었다
```

★★ **`lot_constraints` 는 안 읽는다.** 그 모델이 *"Lot 은 근거 컨텍스트이며 Sales 가
  이를 합산하거나 필터링하지 않는다"* 고 못박고 있다. 로트를 더하면 판매가 물류의
  가용 판정(비-ACTIVE · 신선도 만료 · 예약분)을 **다시 하는 것**이 된다.
"""

from __future__ import annotations

from decimal import Decimal

from app.sales.proposal import _confirmed_sellable_qty
from app.sales.schemas import SalesProposalInput
from tests.sales.test_sales_proposal import _request


def _수량없는요청(**over):
    데이터 = _request(**over)
    사용자 = dict(데이터.user_request.model_dump())
    사용자["requested_quantity_kg"] = None
    return SalesProposalInput.model_validate(
        {**데이터.model_dump(), "business_mode": "SPOT_SALES", "user_request": 사용자}
    )


def test_사람이_수량을_안_주면_물류_확정값을_쓴다() -> None:
    """🔴 **이것이 206일을 0건으로 만든 자리다.**"""
    요청 = _수량없는요청()

    assert _confirmed_sellable_qty(요청) == Decimal(3000)


def test_사람이_준_수량이_있으면_그것이_이긴다() -> None:
    """★ 물류 값은 **사람이 말하지 않은 자리만** 채운다."""
    from app.sales.proposal import _baseline

    수량, *_ = _baseline(_request(business_mode="SPOT_SALES"))

    assert 수량 == Decimal(5000), "사람이 5,000 을 말했는데 물류 값 3,000 이 이겼다"


def test_확정값이_없으면_안_쓴다() -> None:
    """⚠️ 없는 것을 지어내지 않는다 — 그때는 종전대로 막혀야 한다."""
    요청 = _수량없는요청()
    비운것 = 요청.model_dump()
    비운것["logistics_context"]["sellable_supply"]["inventory_by_item"] = []

    assert _confirmed_sellable_qty(SalesProposalInput.model_validate(비운것)) is None


def test_로트만_있으면_합산하지_않는다() -> None:
    """🔴 **계약이 금지한 자리다.** 로트를 더하면 물류의 가용 판정을 다시 하는 것이다.

    ★★ 실측에서 로트에는 3,587kg 이 있는데 `inventory_by_item` 은 **빈 목록**이었다.
      물류가 신선도 만료를 제외한 결과였다 — 로트를 더했으면 **상한 재고를 팔았다.**
    """
    요청 = _수량없는요청()
    바꾼것 = 요청.model_dump()
    바꾼것["logistics_context"]["sellable_supply"]["inventory_by_item"] = []
    바꾼것["logistics_context"]["sellable_supply"]["lot_constraints"] = [
        {"lot_id": "LOT-1", "item": "배추", "available_qty_kg": 3587, "status": "ACTIVE"}
    ]

    assert _confirmed_sellable_qty(SalesProposalInput.model_validate(바꾼것)) is None


def test_READY_가_아니면_안_쓴다() -> None:
    """⚠️ **「모른다」를 「0」으로도 「있다」로도 읽지 않는다.**"""
    요청 = _수량없는요청()
    바꾼것 = 요청.model_dump()
    바꾼것["logistics_context"]["sellable_supply"]["status"] = "UNRESOLVED"

    assert _confirmed_sellable_qty(SalesProposalInput.model_validate(바꾼것)) is None


def test_다른_품목의_수량을_안_가져온다() -> None:
    """★ 요청한 품목만 본다."""
    요청 = _수량없는요청()
    바꾼것 = 요청.model_dump()
    바꾼것["logistics_context"]["sellable_supply"]["inventory_by_item"] = [
        {"item": "무", "available_qty_kg": 9999}
    ]

    assert _confirmed_sellable_qty(SalesProposalInput.model_validate(바꾼것)) is None


def test_안이_실제로_그_수량으로_선다() -> None:
    """🔴 **찾는 것과 쓰는 것은 다르다.**

    ★★ `_confirmed_sellable_qty` 가 값을 돌려줘도 `_baseline` 이 안 쓰면 안은 여전히
      수량 없이 선다. 변이(`if quantity is None` 을 죽임)로 이 구멍을 찾았다 —
      위 검사 여섯이 전부 통과했다.
    """
    from app.sales.proposal import _baseline

    수량, *_ = _baseline(_수량없는요청())

    assert 수량 == Decimal(3000), "물류가 확정한 수량이 안에 안 실렸다"
