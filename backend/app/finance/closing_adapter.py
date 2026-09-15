"""Finance adapter for Master's existing daily-closing registry."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.finance.closing import close_day
from app.master.closing import ClosingPartOut

__all__ = ["FinanceClosingAdapter"]


class FinanceClosingAdapter:
    """Translate the Finance close result into Master's structural closing result."""

    def close(self, conn: Any, *, as_of: date, sim_run_id: str) -> ClosingPartOut:
        result = close_day(as_of=as_of, sim_run_id=sim_run_id, conn=conn)
        return ClosingPartOut(
            part=result.part,
            status=result.status,
            reason=result.reason,
            closed=result.closed,
            created=result.created,
        )
