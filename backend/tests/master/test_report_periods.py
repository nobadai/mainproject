"""Report period slots are deterministically resolved against the frozen as_of date."""

from datetime import date

from app.master.ask_service import _period
from app.master.llm.schemas import DomainSlots, Intent


def _intent(period: str) -> Intent:
    return Intent(
        action="DOMAIN_ACTION",
        domain_action="FINANCE_REPORT_GENERATE",
        slots=DomainSlots(period=period),
        confidence="HIGH",
    )


def test_last_year_report_period_keeps_a_full_requested_year() -> None:
    assert _period(_intent("LAST_YEAR"), as_of=date(2026, 6, 12)) == (
        date(2025, 6, 12),
        date(2026, 6, 12),
    )


def test_last_30_days_report_period_is_inclusive() -> None:
    assert _period(_intent("LAST_30_DAYS"), as_of=date(2026, 6, 12)) == (
        date(2026, 5, 14),
        date(2026, 6, 12),
    )
