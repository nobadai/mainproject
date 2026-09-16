"""PRE_SALES_FACTS — 판매 후보를 만들기 **전에** 재무가 내는 사실.

★ **판정이 아니다.** 이 파일은 `PASS`/`FAIL` 도 허용 수량도 만들지 않는다.
  그것은 후보가 만들어진 뒤 `SALES_VALIDATION` 이 하는 일이다. 두 mode 를 나눠 둔
  이유가 이것이고, 여기서 판정을 흉내 내면 나눈 의미가 사라진다.

🔴 **없는 값을 `0` 으로 채우지 않는다** (§1.2-10).

  ```text
  Decimal(0)  그 값이 0원이라는 사실       → 그대로 싣는다
  None        아직 읽지 못했다는 사실       → 칸을 안 만들고 missing 에 이름을 남긴다
  ```

  여신한도가 없는 거래처에 `0` 을 실으면 판매가 *"한도가 0원이라 아무것도 못 판다"*
  로 읽고, `10,000,000` 같은 기본값을 실으면 **없는 계약이 장부에 선다.**

★ **집계 규칙을 새로 만들지 않는다.** 미지급금·미수금의 열림 판정은 이미
  `db.FinanceAsOfDataPort.load_obligations` / `load_receivables` 가 소유한다
  (`status <> 'PAID'` · `status = 'OPEN'`). 여기는 그것이 낸 Event 를 날짜로 나눠
  더하기만 한다 — 새 회계 규칙을 세우면 같은 사실의 주인이 둘이 된다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.finance.sales_validation import PartnerReceivableFacts
from app.finance.schemas import CashEvent
from app.finance.tools import calculate_available_credit

#: 미지급금으로 세는 현금 Event 종류. **비용(`COMMITTED_OUTFLOW`)과 가른다** —
#: 둘 다 유출이지만 매입 대금과 운영비는 다른 사실이고, 판매가 둘을 합친 값 하나만
#: 받으면 어느 쪽이 큰지 되짚을 수 없다.
_PAYABLE_EVENT_TYPE = "PURCHASE_PAYABLE"

#: 임박 구간. **정책이 아니라 표시 단위다** — 판정에 쓰지 않는다.
_NEAR_TERM_DAYS: tuple[int, ...] = (7, 30)


def partner_credit_limit_ref(*, partner_id: str, as_of: date) -> str:
    """여신한도 한 행을 가리키는 근거 주소.

    ★ **지어낸 id 가 아니라 조회 주소다.** `load_partner_credit_limit` 이 그날 유효한
      행을 `(partner_id, as_of)` 하나로 고르므로, 이 주소를 그대로 따라가면 같은
      행에 닿는다 — 판매 `_ml_forecast_row_ref` 가 ML 행을 가리키는 방식과 같다.
    """
    return f"partner_credit_limits(partner_id={partner_id},as_of={as_of.isoformat()})"


def _sum(events: Sequence[CashEvent]) -> Decimal:
    return sum((event.amount_krw for event in events), Decimal(0))


def _payables(events: Sequence[CashEvent]) -> list[CashEvent]:
    return [event for event in events if event.event_type == _PAYABLE_EVENT_TYPE]


def _due_within(events: Sequence[CashEvent], *, as_of: date, days: int) -> list[CashEvent]:
    """`as_of` 로부터 `days` 일 안에 지급일이 오는 것만. **경계일을 포함한다.**"""
    limit = as_of + timedelta(days=days)
    return [event for event in events if event.event_date <= limit]


def build_partner_credit_facts(
    *,
    partner_id: str | None,
    receivable_facts: PartnerReceivableFacts | None,
    credit_limit_krw: Decimal | None,
) -> tuple[dict[str, Any], list[str]]:
    """거래처 채권과 여신 여력. **셋이 따로 없을 수 있다.**

    ```text
    partner_id 없음        거래처를 안 밝힌 요청 — 채권도 여신도 물을 수 없다
    채권 사실 없음          조회를 못 했다 (빈 목록은 "0원" 이라는 사실이지 없음이 아니다)
    여신한도 없음          그 거래처의 한도가 아직 계약으로 서지 않았다
    ```

    🔴 **여력을 한도 없이 만들지 않는다.** 한도를 모르는 채로 "여력 = -채권" 을 내면
      음수 여력이 사실처럼 보인다.
    """
    facts: dict[str, Any] = {}
    missing: list[str] = []
    if partner_id is None or not partner_id.strip():
        missing.append("partner_id")
        return facts, missing

    facts["partner_id"] = partner_id
    if receivable_facts is None:
        missing.append("partner_receivable_facts")
    else:
        facts["partner_receivable_krw"] = receivable_facts.current_ar_krw
        facts["partner_overdue_receivable_krw"] = receivable_facts.overdue_ar_krw

    if credit_limit_krw is None:
        missing.append("partner_credit_limit_krw")
    else:
        facts["partner_credit_limit_krw"] = credit_limit_krw

    if receivable_facts is not None and credit_limit_krw is not None:
        # ★ **사용액은 현재 채권이다.** 판매 제안은 아직 없으므로 여기에 더할 것이
        #   없다 — 제안을 얹은 값(`projected_partner_ar_krw`)은 검증의 몫이다.
        facts["partner_credit_used_krw"] = receivable_facts.current_ar_krw
        facts["partner_credit_available_krw"] = calculate_available_credit(
            credit_limit_krw=credit_limit_krw,
            current_partner_ar_krw=receivable_facts.current_ar_krw,
        )
    return facts, missing


def build_obligation_facts(
    *,
    as_of: date,
    obligations: Sequence[CashEvent],
    receivables: Sequence[CashEvent],
) -> dict[str, Any]:
    """미지급금·미수금 집계. **전부 `as_of` 기준 투영 구간 안의 값이다.**

    ★ 구간 밖을 합치지 않는다. 적재층이 `as_of < 날짜 <= horizon` 으로 이미 잘라서
      주므로 여기서 다시 넓히면 **투영에 안 들어간 채무가 합계에만 들어간다.**
    """
    payables = _payables(obligations)
    facts: dict[str, Any] = {
        "payables_total_krw": _sum(payables),
        "receivables_total_krw": _sum(receivables),
    }
    for days in _NEAR_TERM_DAYS:
        facts[f"payables_due_{days}d_krw"] = _sum(_due_within(payables, as_of=as_of, days=days))
    return facts
