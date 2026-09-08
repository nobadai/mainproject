"""Finance-owned deterministic collection fixture source.

This module does not create collection facts from due dates or receivable
balances. It only exposes caller-authored ``CollectionEvent`` rows so Master can
carry them to ``apply_explicit_collection`` on the matching simulation day.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.finance.collection import CollectionEvent

FixtureEvidenceGrade = Literal["SIM_FIXED"]


@dataclass(frozen=True)
class DeterministicCollectionFixtureSource:
    """Explicit collection event source for simulation fixtures."""

    events: tuple[CollectionEvent, ...] = ()
    evidence_grade: FixtureEvidenceGrade = "SIM_FIXED"
    source_ref: str = "finance_collection_fixture"

    @classmethod
    def from_events(
        cls,
        events: Iterable[CollectionEvent],
        *,
        source_ref: str = "finance_collection_fixture",
    ) -> DeterministicCollectionFixtureSource:
        return cls(events=tuple(events), source_ref=source_ref)

    def events_for_date(
        self,
        *,
        sim_run_id: str,
        financing_mode: str,
        as_of: date,
    ) -> tuple[CollectionEvent, ...]:
        """Return only explicitly declared events on the exact runtime axis."""
        return tuple(
            event
            for event in self.events
            if event.sim_run_id == sim_run_id
            and event.financing_mode == financing_mode
            and event.collection_date == as_of
        )
