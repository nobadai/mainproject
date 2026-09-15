"""Historical 예약의 **존재일은 판매 확정일(`sales.order_date`)** 이다 — 납품일이 아니다.

```text
확정 (D)     sales_approval.confirm_approved_sale 이 확정 직후 예약을 건다  (2026-09-12)
납품 (D+1)   outbound_flow 가 같은 예약에 할당·출고를 붙인다
```

🔴 **종전 `sale_date <= as_of` 는 확정일 화면에서 그 예약을 숨겼다.** 그날 Runtime 은 이미
   그 몫을 잡고 있는데(`outbound.item_free_stock_qty` 의 미할당 예약) Historical 만 하루
   늦게 보여, 같은 화면의 예약 목록과 판매가능량이 서로 다른 날을 가리켰다.

★ **DB 를 치지 않는다.** SQL 문장과 문서화 문자열을 읽어 규칙이 그 칸에 서 있는지만 본다 —
  실제 조회는 `test_logistics_agent_tools_db.py` 가 실 PostgreSQL 에서 한다.
"""

from __future__ import annotations

import inspect

from app.logistics import console_service, historical_repository


def _코드만(func: object) -> str:
    """docstring 을 걷어낸 **실제로 실행되는 코드**."""
    source = inspect.getsource(func)  # type: ignore[arg-type]
    doc = inspect.getdoc(func) or ""
    return source.replace(doc, "", 1)


def test_존재_조건이_order_date_다() -> None:
    """🔴 잠금. `sale_date` 로 되돌리면 확정일 예약이 하루 늦게 나타난다."""
    코드 = _코드만(historical_repository.reservation_state_at)

    assert "s.order_date <= %(as_of)s" in 코드, "존재 조건이 order_date 가 아니다"
    assert "s.sale_date <= %(as_of)s" not in 코드, "sale_date 로 존재를 자르는 줄이 남아 있다"


def test_문서가_같은_말을_한다() -> None:
    """★ 코드와 설명이 갈리면 다음 사람이 옛 전제로 되돌린다 — 세 자리를 같이 잠근다."""
    for doc in (
        inspect.getdoc(historical_repository.reservation_state_at),
        inspect.getdoc(historical_repository.HistoricalReservation),
        inspect.getdoc(console_service.get_outbound_console),
    ):
        assert doc is not None
        존재_줄 = [줄 for 줄 in doc.splitlines() if "존재" in 줄 and "<= as_of" in 줄]
        assert 존재_줄, f"존재 규칙 줄이 없다: {doc.splitlines()[0]}"
        for 줄 in 존재_줄:
            assert "order_date <= as_of" in 줄, 줄
            #  ★ «종전에는 sale_date 였다» 같은 설명은 두되, 규칙 줄에는 못 남는다.
            assert "sale_date <= as_of" not in 줄, 줄


def test_미래_확정_출고는_그대로_납품일_축이다() -> None:
    """⚠️ `outbound_schedule_at` 은 «아직 안 나간 판매» 를 묻는 자리라 `sale_date > as_of` 가 맞다.

    존재일을 옮기면서 이쪽까지 따라 바꾸면 확정일에 그 판매가 «미래 출고» 에서 사라진다.
    """
    doc = inspect.getdoc(historical_repository.outbound_schedule_at) or ""

    assert "sale_date > as_of" in doc
