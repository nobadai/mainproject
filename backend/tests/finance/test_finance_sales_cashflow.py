"""Finance Sales Core Phase 3 — 판매 시나리오 현금흐름 오버레이.

★ 이 파일이 지키는 것은 **BASE 와 SCENARIO 의 분리**다.
    · BASE 는 제안이 있든 없든 값이 같다
    · 제안 회수는 확정 채권(RECEIVABLE)이 되지 않는다
    · horizon 밖 회수일은 날짜를 옮기지 않고 사실 그대로 드러낸다
    · depends_on_projected_inflow 는 정의가 하나뿐이고 시험된다
  판정(안전한가·승인할 것인가)은 여기 없다 — Rule 계층 몫이다.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.finance.schemas import CashEvent
from app.finance.tools import (
    build_proposed_sales_collection_event,
    calculate_collection_date,
    project_cashflow,
    project_sales_scenario_cashflow,
)

AS_OF = date(2026, 3, 1)
HORIZON = date(2026, 6, 1)
CURRENT_CASH = Decimal(10_000_000)


def _payable(day: date, amount: str, ref: str = "P-1") -> CashEvent:
    return CashEvent(
        event_date=day,
        event_type="PURCHASE_PAYABLE",
        amount_krw=Decimal(amount),
        direction="OUTFLOW",
        ref_id=ref,
        source_ref="PAYABLE:P-1",
    )


def _proposed(
    day: date = date(2026, 4, 10),
    amount: str = "3000000",
    *,
    ref: str = "SC-001",
    source_ref: str = "SALES-REPLY:R-9",
) -> CashEvent:
    return build_proposed_sales_collection_event(
        proposal_ref=ref,
        collection_date=day,
        sales_amount_krw=Decimal(amount),
        source_ref=source_ref,
    )


def _overlay(
    *,
    base: list[CashEvent] | None = None,
    proposed: CashEvent | None = None,
    horizon_end: date = HORIZON,
):
    return project_sales_scenario_cashflow(
        as_of=AS_OF,
        current_cash_krw=CURRENT_CASH,
        horizon_end=horizon_end,
        base_cash_events=base if base is not None else [_payable(date(2026, 4, 1), "8000000")],
        proposed_collection=proposed if proposed is not None else _proposed(),
    )


# ---------------------------------------------------------------------------
# BASE 불변
# ---------------------------------------------------------------------------


def test_base_projection_is_identical_to_projecting_without_the_proposal():
    base_events = [_payable(date(2026, 4, 1), "8000000")]
    standalone = project_cashflow(
        as_of=AS_OF,
        current_cash_krw=CURRENT_CASH,
        horizon_end=HORIZON,
        cash_events=list(base_events),
    )

    result = _overlay(base=base_events)

    assert result.base_projection == standalone
    assert result.base_projected_cash_min == standalone.projected_cash_min
    assert result.base_projected_cash_min_date == standalone.projected_cash_min_date


def test_caller_event_list_is_not_mutated_by_the_overlay():
    base_events = [_payable(date(2026, 4, 1), "8000000")]

    _overlay(base=base_events)

    assert len(base_events) == 1
    assert base_events[0].event_type == "PURCHASE_PAYABLE"


def test_scenario_adds_only_the_proposed_inflow():
    result = _overlay()

    # 4/1 에 8,000,000 나가고 4/10 에 3,000,000 들어온다.
    assert result.base_projected_cash_min == Decimal(2_000_000)
    assert result.scenario_projected_cash_min == Decimal(2_000_000)
    scenario_dates = {
        point.projection_date for point in result.scenario_projection.projected_cash_by_date
    }
    assert date(2026, 4, 10) in scenario_dates
    assert date(2026, 4, 10) not in {
        point.projection_date for point in result.base_projection.projected_cash_by_date
    }


def test_scenario_differs_from_base_only_on_and_after_the_collection_date():
    result = _overlay()

    base_by_date = {
        point.projection_date: point.cash_balance_krw
        for point in result.base_projection.projected_cash_by_date
    }
    for point in result.scenario_projection.projected_cash_by_date:
        if point.projection_date < date(2026, 4, 10):
            assert point.cash_balance_krw == base_by_date[point.projection_date]
    assert result.scenario_projection.projected_cash_by_date[-1].cash_balance_krw == Decimal(
        5_000_000
    )


# ---------------------------------------------------------------------------
# 회수일 — Phase 1 산술을 그대로 쓴다
# ---------------------------------------------------------------------------


def test_collection_date_comes_from_phase_one_arithmetic():
    collection = calculate_collection_date(reference_date=date(2026, 3, 10), payment_days=30)

    result = _overlay(proposed=_proposed(collection))

    assert result.collection_date == date(2026, 4, 9)


def test_same_day_collection_is_not_projected_because_horizon_starts_after_as_of():
    # D+0 을 as_of 로 잡으면 as_of < event_date 조건에 걸린다 — 날짜를 옮기지 않는다.
    result = _overlay(proposed=_proposed(AS_OF))

    assert result.collection_date == AS_OF
    assert result.collection_within_horizon is False
    assert result.scenario_projected_cash_min == result.base_projected_cash_min


def test_same_day_collection_after_as_of_is_projected():
    reference = date(2026, 4, 5)
    collection = calculate_collection_date(reference_date=reference, payment_days=0)

    result = _overlay(proposed=_proposed(collection))

    assert result.collection_date == reference
    assert result.collection_within_horizon is True


def test_month_boundary_collection():
    collection = calculate_collection_date(reference_date=date(2026, 3, 31), payment_days=1)

    result = _overlay(proposed=_proposed(collection))

    assert result.collection_date == date(2026, 4, 1)
    assert result.collection_within_horizon is True


def test_year_boundary_collection_lands_outside_a_short_horizon():
    collection = calculate_collection_date(reference_date=date(2026, 12, 20), payment_days=30)

    result = _overlay(proposed=_proposed(collection))

    assert result.collection_date == date(2027, 1, 19)
    assert result.collection_within_horizon is False


def test_leap_day_collection_is_kept_exactly():
    collection = calculate_collection_date(reference_date=date(2028, 2, 28), payment_days=1)

    result = _overlay(proposed=_proposed(collection), horizon_end=date(2028, 6, 1))

    assert result.collection_date == date(2028, 2, 29)


# ---------------------------------------------------------------------------
# Horizon — 날짜를 옮기지도, horizon 을 늘리지도 않는다
# ---------------------------------------------------------------------------


def test_collection_outside_horizon_is_exposed_not_moved():
    outside = date(2026, 8, 1)

    result = _overlay(proposed=_proposed(outside))

    assert result.collection_date == outside
    assert result.collection_within_horizon is False
    # horizon 을 몰래 늘리지 않는다.
    assert result.scenario_projection.horizon_end == HORIZON
    # 그리고 그 유입은 SCENARIO 에 반영되지 않는다.
    assert result.scenario_projected_cash_min == result.base_projected_cash_min
    assert result.depends_on_projected_inflow is False


# ---------------------------------------------------------------------------
# depends_on_projected_inflow — 정의가 하나뿐이다
# ---------------------------------------------------------------------------


def test_depends_on_projected_inflow_is_true_when_the_inflow_lifts_the_minimum():
    # 유입이 최저점(4/20 지급) 앞에 들어와 최저 현금을 끌어올린다.
    result = _overlay(
        base=[
            _payable(date(2026, 4, 1), "8000000", ref="P-1"),
            _payable(date(2026, 4, 20), "3000000", ref="P-2"),
        ],
        proposed=_proposed(date(2026, 4, 10), "3000000"),
    )

    assert result.base_projected_cash_min == Decimal(-1_000_000)
    assert result.scenario_projected_cash_min == Decimal(2_000_000)
    assert result.depends_on_projected_inflow is True


def test_depends_on_projected_inflow_is_false_when_the_minimum_is_unchanged():
    # 최저점이 유입보다 앞이면 유입은 최저 현금을 바꾸지 못한다.
    result = _overlay(proposed=_proposed(date(2026, 5, 1), "3000000"))

    assert result.scenario_projected_cash_min == result.base_projected_cash_min
    assert result.depends_on_projected_inflow is False


def test_zero_amount_collection_stays_a_zero_event_and_changes_nothing():
    result = _overlay(proposed=_proposed(amount="0"))

    assert result.collection_amount_krw == Decimal(0)
    assert result.scenario_projected_cash_min == result.base_projected_cash_min
    assert result.depends_on_projected_inflow is False
    # 0원 회수도 Event 로는 존재한다 — "없음"이 아니다.
    assert result.proposed_collection_ref_id.startswith("SALES-PROPOSAL:")


# ---------------------------------------------------------------------------
# 제안 회수는 확정 채권이 아니다
# ---------------------------------------------------------------------------


def test_proposed_collection_never_appears_as_an_actual_receivable():
    result = _overlay()

    assert result.collection_amount_krw == Decimal(3_000_000)
    # BASE 에는 제안이 없다.
    assert result.base_projection != result.scenario_projection


def test_base_events_carrying_a_proposed_collection_are_rejected():
    # 제안이 BASE 로 승격되는 경로를 막는다.
    with pytest.raises(ValueError):
        _overlay(base=[_payable(date(2026, 4, 1), "8000000"), _proposed()])


def test_a_confirmed_receivable_cannot_be_passed_as_the_proposal():
    confirmed = CashEvent(
        event_date=date(2026, 4, 10),
        event_type="RECEIVABLE",
        amount_krw=Decimal(3_000_000),
        direction="INFLOW",
        ref_id="AR-1",
        source_ref="RECEIVABLE:AR-1",
    )

    with pytest.raises(ValueError):
        _overlay(proposed=confirmed)


def test_an_outflow_cannot_be_passed_as_a_proposed_collection():
    outflow = CashEvent(
        event_date=date(2026, 4, 10),
        event_type="PROPOSED_SALES_COLLECTION",
        amount_krw=Decimal(3_000_000),
        direction="OUTFLOW",
        ref_id="SALES-PROPOSAL:SC-001:2026-04-10",
        source_ref="SALES-REPLY:R-9",
    )

    with pytest.raises(ValueError):
        _overlay(proposed=outflow)


# ---------------------------------------------------------------------------
# 입력 방어 · 계보
# ---------------------------------------------------------------------------


def test_duplicate_base_events_are_still_rejected_through_the_overlay():
    # 오버레이가 기존 투영 엔진의 중복 방어를 우회하지 않는다.
    duplicated = _payable(date(2026, 4, 1), "8000000", ref="P-1")

    with pytest.raises(ValueError):
        _overlay(base=[duplicated, duplicated])


def test_a_second_proposed_collection_cannot_enter_through_base_events():
    # 제안은 정확히 하나다 — 두 번째를 BASE 로 밀어 넣는 경로가 막혀 있다.
    with pytest.raises(ValueError):
        _overlay(base=[_proposed(date(2026, 4, 10))], proposed=_proposed(date(2026, 4, 10)))


def test_negative_sales_amount_is_rejected():
    with pytest.raises(ValueError):
        build_proposed_sales_collection_event(
            proposal_ref="SC-001",
            collection_date=date(2026, 4, 10),
            sales_amount_krw=Decimal(-1),
            source_ref="SALES-REPLY:R-9",
        )


def test_blank_proposal_ref_is_rejected():
    with pytest.raises(ValueError):
        build_proposed_sales_collection_event(
            proposal_ref="  ",
            collection_date=date(2026, 4, 10),
            sales_amount_krw=Decimal(1),
            source_ref="SALES-REPLY:R-9",
        )


def test_blank_source_ref_is_rejected_so_lineage_cannot_be_dropped():
    with pytest.raises(ValueError):
        build_proposed_sales_collection_event(
            proposal_ref="SC-001",
            collection_date=date(2026, 4, 10),
            sales_amount_krw=Decimal(1),
            source_ref="",
        )


def test_source_lineage_survives_onto_the_event():
    event = _proposed(source_ref="SALES-REPLY:R-42")

    assert event.source_ref == "SALES-REPLY:R-42"
    assert event.ref_id == "SALES-PROPOSAL:SC-001:2026-04-10"
    assert event.event_type == "PROPOSED_SALES_COLLECTION"
    assert event.direction == "INFLOW"


def test_ref_id_is_stable_for_the_same_proposal_and_date():
    assert _proposed().ref_id == _proposed().ref_id
