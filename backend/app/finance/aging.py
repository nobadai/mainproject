"""Deterministic receivable-aging vocabulary shared by Finance readers."""

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
