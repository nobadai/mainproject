from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.finance.collection import CollectionEvent
from app.finance.collection_fixture import DeterministicCollectionFixtureSource


def _event(
    day: date,
    target: object,
    *,
    sim_run_id: str = "SIM-1",
    financing_mode: str = "LOAN_BASELINE",
    receivable_id: str = "AR-1",
) -> CollectionEvent:
    return CollectionEvent(
        sim_run_id=sim_run_id,
        financing_mode=financing_mode,
        collection_date=day,
        receivable_id=receivable_id,
        target_received_total_krw=target,
    )


def test_returns_only_explicit_events_for_the_requested_date():
    event = _event(date(2026, 1, 15), Decimal(4_000_000))
    source = DeterministicCollectionFixtureSource.from_events([event], source_ref="TEST")

    assert source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    ) == (event,)


def test_other_dates_return_empty_without_due_date_inference():
    source = DeterministicCollectionFixtureSource.from_events(
        [_event(date(2026, 1, 15), Decimal(4_000_000))],
        source_ref="TEST",
    )

    assert source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 14),
    ) == ()


def test_sim_run_id_isolated():
    source = DeterministicCollectionFixtureSource.from_events(
        [_event(date(2026, 1, 15), Decimal(4_000_000), sim_run_id="SIM-1")],
        source_ref="TEST",
    )

    assert source.events_for_date(
        sim_run_id="SIM-2",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    ) == ()


def test_financing_mode_isolated():
    loan = _event(date(2026, 1, 15), Decimal(4_000_000), financing_mode="LOAN_BASELINE")
    base = _event(date(2026, 1, 15), Decimal(1_000_000), financing_mode="BASE_NO_LOAN")
    source = DeterministicCollectionFixtureSource.from_events([loan, base], source_ref="TEST")

    assert source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    ) == (loan,)


def test_cumulative_target_is_preserved_exactly():
    event = _event(date(2026, 1, 15), Decimal("4000000.123456"))
    source = DeterministicCollectionFixtureSource.from_events([event], source_ref="TEST")

    got = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )

    assert got[0].target_received_total_krw == Decimal("4000000.123456")


def test_repeated_lookup_is_deterministic():
    events = (
        _event(date(2026, 1, 15), Decimal(4_000_000), receivable_id="AR-1"),
        _event(date(2026, 1, 15), Decimal(2_000_000), receivable_id="AR-2"),
    )
    source = DeterministicCollectionFixtureSource(events=events, source_ref="TEST")

    first = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )
    second = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )

    assert first == events
    assert second == events


def test_provider_reuses_existing_collection_event_type():
    event = _event(date(2026, 1, 15), Decimal(4_000_000))
    source = DeterministicCollectionFixtureSource.from_events([event], source_ref="TEST")

    got = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )

    assert isinstance(got[0], CollectionEvent)


def test_source_marks_sim_fixed_fixture_evidence():
    source = DeterministicCollectionFixtureSource.from_events([], source_ref="TEST")

    assert source.evidence_grade == "SIM_FIXED"
    assert source.source_ref == "TEST"
