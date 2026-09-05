"""Deterministic identity for one Finance runtime axis on one calendar date."""

from __future__ import annotations

from datetime import date

__all__ = ["daily_finance_state_id"]


def daily_finance_state_id(*, sim_run_id: str, financing_mode: str, state_date: date) -> str:
    """Return the state ID shared by transition and explicit day opening.

    The database invariant is the tuple ``(sim_run_id, financing_mode, state_date)``. Keeping
    those axes in the readable ID makes creation order irrelevant: approvals and ``open_day``
    all address the same state.
    """
    return f"FIN-DAY-{sim_run_id}-{financing_mode}-{state_date:%Y%m%d}"
