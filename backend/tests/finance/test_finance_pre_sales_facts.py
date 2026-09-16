"""PRE_SALES_FACTS — 판매 후보를 만들기 전에 재무가 내는 사실.

여기서 재는 것 다섯.

```text
① 미지급금과 비용을 가르는가            둘을 합치면 어느 쪽이 큰지 못 되짚는다
② 7일·30일 구간을 날짜로 자르는가        경계일을 포함한다
③ 0 과 None 을 가르는가                 없는 여신한도를 0 으로 만들지 않는다
④ 판정 mode 와 나뉘어 있는가             SALES_VALIDATION 을 재사용하지 않는다
⑤ 쓰기를 하지 않는가                     사실 조회는 장부를 건드리지 않는다
```
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.finance import adapter
from app.finance.capabilities.pre_sales import (
    build_obligation_facts,
    build_partner_credit_facts,
    partner_credit_limit_ref,
)
from app.finance.schemas import CashEvent
from app.finance.tools import summarize_partner_receivables
from app.master.envelope import AgentRequest, ExecutionContext, agent_allowed_modes

AS_OF = date(2026, 9, 16)


def _event(day: int, amount: int, *, event_type: str, direction: str) -> CashEvent:
    return CashEvent(
        event_date=date(2026, 9, day),
        event_type=event_type,
        amount_krw=Decimal(amount),
        direction=direction,
        ref_id=f"EV-{event_type}-{day}",
    )


def _payable(day: int, amount: int) -> CashEvent:
    return _event(day, amount, event_type="PURCHASE_PAYABLE", direction="OUTFLOW")


def _expense(day: int, amount: int) -> CashEvent:
    return _event(day, amount, event_type="COMMITTED_OUTFLOW", direction="OUTFLOW")


def _receivable(day: int, amount: int) -> CashEvent:
    return _event(day, amount, event_type="RECEIVABLE", direction="INFLOW")


# ---------------------------------------------------------------------------
# ① · ② 미지급금 집계
# ---------------------------------------------------------------------------


def test_비용은_미지급금_합계에_들어가지_않는다():
    """매입 대금과 운영비는 다른 사실이다 — 합치면 어느 쪽이 큰지 되짚을 수 없다."""
    facts = build_obligation_facts(
        as_of=AS_OF,
        obligations=[_payable(18, 3_000_000), _expense(18, 9_000_000)],
        receivables=[],
    )

    assert facts["payables_total_krw"] == Decimal(3_000_000)


def test_임박_구간은_지급일로_자르고_경계일을_포함한다():
    """`as_of + 7` 그 날짜까지가 7일 안이다."""
    facts = build_obligation_facts(
        as_of=AS_OF,
        obligations=[
            _payable(20, 1_000_000),  # D+4
            _payable(23, 2_000_000),  # D+7 — 경계일
            _payable(30, 4_000_000),  # D+14
            _payable(29, 8_000_000),  # D+13
        ],
        receivables=[],
    )

    assert facts["payables_due_7d_krw"] == Decimal(3_000_000)
    assert facts["payables_due_30d_krw"] == Decimal(15_000_000)
    assert facts["payables_total_krw"] == Decimal(15_000_000)


def test_미수금은_유입_Event_만_센다():
    facts = build_obligation_facts(
        as_of=AS_OF,
        obligations=[_payable(20, 1_000_000)],
        receivables=[_receivable(25, 7_000_000)],
    )

    assert facts["receivables_total_krw"] == Decimal(7_000_000)


# ---------------------------------------------------------------------------
# ③ 0 과 None
# ---------------------------------------------------------------------------


def _facts(*, receivables, partner_id="CUST-1"):
    return summarize_partner_receivables(
        partner_id=partner_id, as_of=AS_OF, receivables=receivables
    )


def test_여신한도가_없으면_칸을_안_만들고_이름을_남긴다():
    """🔴 `0` 으로 채우면 판매가 *"한도 0원이라 못 판다"* 로 읽는다."""
    payload, missing = build_partner_credit_facts(
        partner_id="CUST-1",
        receivable_facts=_facts(receivables=[]),
        credit_limit_krw=None,
    )

    assert "partner_credit_limit_krw" not in payload
    assert "partner_credit_available_krw" not in payload
    assert "partner_credit_limit_krw" in missing


def test_여신한도_0원은_사실이므로_그대로_싣는다():
    payload, missing = build_partner_credit_facts(
        partner_id="CUST-1",
        receivable_facts=_facts(receivables=[]),
        credit_limit_krw=Decimal(0),
    )

    assert payload["partner_credit_limit_krw"] == Decimal(0)
    assert payload["partner_credit_available_krw"] == Decimal(0)
    assert "partner_credit_limit_krw" not in missing


def test_채권이_없으면_여력도_만들지_않는다():
    """한도만 알고 채권을 모르면 여력을 셀 수 없다 — 음수 여력을 지어내지 않는다."""
    payload, missing = build_partner_credit_facts(
        partner_id="CUST-1", receivable_facts=None, credit_limit_krw=Decimal(10_000_000)
    )

    assert "partner_credit_available_krw" not in payload
    assert "partner_receivable_facts" in missing


def test_거래처를_안_밝히면_채권도_여신도_묻지_않는다():
    payload, missing = build_partner_credit_facts(
        partner_id=None, receivable_facts=None, credit_limit_krw=None
    )

    assert payload == {}
    assert missing == ["partner_id"]


def test_여신한도_근거는_따라갈_수_있는_조회_주소다():
    """지어낸 id 가 아니다 — 같은 `(partner_id, as_of)` 로 그 행에 닿는다."""
    assert partner_credit_limit_ref(partner_id="CUST-1", as_of=AS_OF) == (
        "partner_credit_limits(partner_id=CUST-1,as_of=2026-09-16)"
    )


# ---------------------------------------------------------------------------
# ④ · ⑤ mode 경계
# ---------------------------------------------------------------------------


def test_재무가_PRE_SALES_FACTS_를_받는다():
    assert "PRE_SALES_FACTS" in agent_allowed_modes("finance")


def test_판정_mode_와_나뉘어_있다():
    """합치면 *"후보 없이 불린 검증"* 이라는 모양이 생긴다."""
    modes = agent_allowed_modes("finance")

    assert {"SALES_VALIDATION", "PRE_SALES_FACTS"} <= modes


def test_사실_조회는_실행이력을_쓰지_않는다():
    """🔴 `_CONTROLLER_MODES` 에 없다 — `STATUS_QUERY` 와 같은 자리다."""
    assert "PRE_SALES_FACTS" not in adapter._CONTROLLER_MODES


def test_실행_축이_없으면_사실을_지어내지_않는다():
    """`sim_run_id` 가 비면 *"물어볼 수 없다"* 이지 *"자료가 없다"* 가 아니다."""
    request = AgentRequest(
        context=ExecutionContext(
            request_id="REQ-1",
            as_of=AS_OF,
            trigger="USER_REQUEST",
            policy_version="POLICY-V1",
        ),
        agent="finance",
        mode="PRE_SALES_FACTS",
        payload={"partner_id": "CUST-1"},
    )

    reply, _meta = adapter.finance_port(request)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.payload == {}
