# ─────────────────────────────────────────────────────────────────────────────
# STATUS: 공용 계약 — 매출채권 연체 구간의 **한 벌짜리 어휘와 규칙**
#
#   누가 쓰나
#     재무   `app/finance/aging.py`               정본 경로 (이 규칙을 그대로 다시 낸다)
#            `app/finance/console_receivables.py` AR Aging 화면
#     판매   `app/sales/console_collections.py`   수금 화면
#            `app/sales/console_partners.py`      거래처 상세의 채권
#
# 🔴 **왜 재무 파일이 아니라 여기인가.** 규칙의 주인은 재무가 맞다. 그런데 판매가
#    `app.finance` 를 직접 임포트하면 두 부서가 실행 계층에서 붙고, 마스터가 중개할
#    자리가 사라진다 — `tests/finance/test_finance_sales_orchestration_boundary.py`
#    가 그 경계를 지키고 있다. 그래서 **구현은 공용 계약에 두고, 재무가 자기 이름으로
#    다시 낸다.** 규칙은 한 벌이고, 경계도 그대로다.
#
# 🔴 **판매가 자기 Aging 을 만들지 않는다.** 두 벌이 되면 수금 화면과 채권 화면이 같은
#    채권을 다른 구간에 넣고, 둘 다 자기 규칙으로는 옳다 — 그때 어느 쪽이 맞는지
#    아무도 답할 수 없다.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import date
from decimal import Decimal
from typing import Literal

AgingBucket = Literal["CURRENT", "1_7", "8_30", "30_PLUS", "PAID"]


def classify_receivable_aging(
    *, outstanding_amount_krw: Decimal | None, due_date: date, as_of: date
) -> tuple[AgingBucket, int | None]:
    """Classify a stored receivable without treating unknown money as zero."""
    if outstanding_amount_krw is None:
        raise ValueError("outstanding_amount_krw is required for aging")
    if outstanding_amount_krw <= 0:
        return "PAID", None
    days_overdue = (as_of - due_date).days
    if days_overdue <= 0:
        return "CURRENT", 0
    if days_overdue <= 7:
        return "1_7", days_overdue
    if days_overdue <= 30:
        return "8_30", days_overdue
    return "30_PLUS", days_overdue
