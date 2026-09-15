"""Master 수금 source 계약에 연결되는 Finance adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.finance.collection import (
    CollectionEvent,
    DeterministicCollectionFixtureSource,
    FinanceCollectionConflict,
    apply_explicit_collection,
)
from app.finance.db import FinanceDataNotReady
from app.master.collection import CollectionPartOut

CollectionEventSource = DeterministicCollectionFixtureSource


@dataclass
class FinanceCollectionSource:
    sim_run_id: str
    financing_mode: str
    source: CollectionEventSource

    def collect(
        self,
        conn: Any,
        *,
        as_of: date,
    ) -> CollectionPartOut:
        events = self.source.events_for_date(
            sim_run_id=self.sim_run_id,
            financing_mode=self.financing_mode,
            as_of=as_of,
        )
        if not events:
            return CollectionPartOut(part="finance", status="NOTHING_DUE")

        collected: list[str] = []
        for event in events:
            if not _matches_axis(
                event,
                sim_run_id=self.sim_run_id,
                financing_mode=self.financing_mode,
            ):
                return CollectionPartOut(
                    part="finance",
                    status="BLOCKED",
                    reason="수금 event의 실행 기준이 현재 Finance adapter와 일치하지 않습니다.",
                    collected=collected,
                )
            try:
                plan = apply_explicit_collection(conn, event)
            except (FinanceCollectionConflict, FinanceDataNotReady, LookupError, ValueError) as exc:
                return CollectionPartOut(
                    part="finance",
                    status="BLOCKED",
                    reason=str(exc),
                    collected=collected,
                )
            if plan.delta_received_krw > 0:
                collected.append(plan.receivable_id)
        if not collected:
            return CollectionPartOut(part="finance", status="NOTHING_DUE")
        return CollectionPartOut(part="finance", status="COLLECTED", collected=collected)


def _matches_axis(event: CollectionEvent, *, sim_run_id: str, financing_mode: str) -> bool:
    return event.sim_run_id == sim_run_id and event.financing_mode == financing_mode
