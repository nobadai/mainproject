"""판매 추이 — 날짜로 접는 일은 백엔드가 끝낸다."""

from datetime import date
from decimal import Decimal

from app.sales.console_trend import (
    MAX_TREND_DAYS,
    get_console_sales_trend,
)

RUN = "SIM-CHAIN-V13"
AS_OF = date(2026, 3, 31)


class _Reader:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[tuple[str, list]] = []

    def __call__(self, query, params):
        self.calls.append((str(query), list(params)))
        return [dict(row) for row in self.rows]


def _row(day: int, **over) -> dict:
    return {
        "sale_date": date(2026, 3, day),
        "sales_count": 2,
        "quantity_kg": Decimal("140.5"),
        "sales_amount_krw": Decimal(1250000),
        "contribution_profit_krw": Decimal(380000),
        **over,
    }


def _patch(monkeypatch, reader: _Reader) -> None:
    monkeypatch.setattr("app.sales.console_trend.get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr("app.sales.console_trend.fetch_all", reader)


def test_daily_points_come_back_as_stored(monkeypatch):
    reader = _Reader([_row(29), _row(30), _row(31)])
    _patch(monkeypatch, reader)

    result = get_console_sales_trend(sim_run_id=RUN, as_of=AS_OF)

    assert result.sim_run_id == RUN
    assert result.as_of == AS_OF
    assert [point.sale_date.day for point in result.rows] == [29, 30, 31]
    assert result.rows[0].sales_amount_krw == Decimal(1250000)
    assert result.rows[0].contribution_profit_krw == Decimal(380000)


def test_the_run_axis_and_the_as_of_are_both_carried(monkeypatch):
    """🔴 실행 축이 빠지면 전 실행이 한 그래프에 섞인다."""
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_trend(sim_run_id=RUN, as_of=AS_OF)

    query, params = reader.calls[0]
    assert params[0] == RUN
    assert params[1] == AS_OF
    assert "sim_run_id = %s" in query
    assert "sale_date <= %s" in query


def test_the_recent_window_is_cut_from_the_end_and_reordered(monkeypatch):
    """⚠️ 앞에서 자르면 실행 초기가 «최근» 으로 표시된다."""
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_trend(sim_run_id=RUN, as_of=AS_OF, days=30)

    query, params = reader.calls[0]
    assert params[-1] == 30
    #  안쪽은 최신부터 잘라 내고, 바깥쪽이 다시 오름차순으로 세운다.
    assert query.index("ORDER BY s.sale_date DESC") < query.index("ORDER BY sale_date ASC")


def test_an_oversized_window_is_capped_rather_than_passed_through(monkeypatch):
    reader = _Reader([])
    _patch(monkeypatch, reader)

    get_console_sales_trend(sim_run_id=RUN, as_of=AS_OF, days=99_999)

    assert reader.calls[0][1][-1] == MAX_TREND_DAYS


def test_a_run_with_no_sales_is_empty_rather_than_zero_filled(monkeypatch):
    """🔴 «판 날이 없다» 와 «0원 판 날» 은 다른 사실이다."""
    _patch(monkeypatch, _Reader([]))

    result = get_console_sales_trend(sim_run_id=RUN, as_of=AS_OF)

    assert result.rows == []


def test_a_real_zero_day_is_kept(monkeypatch):
    """반대쪽도 지키자 — 저장된 0 은 0 으로 나른다."""
    _patch(
        monkeypatch,
        _Reader(
            [
                _row(
                    31,
                    sales_count=1,
                    sales_amount_krw=Decimal(0),
                    contribution_profit_krw=Decimal(0),
                )
            ]
        ),
    )

    result = get_console_sales_trend(sim_run_id=RUN, as_of=AS_OF)

    assert len(result.rows) == 1
    assert result.rows[0].sales_amount_krw == Decimal(0)
    assert result.rows[0].sales_count == 1


def test_amounts_stay_decimal_rather_than_becoming_float(monkeypatch):
    """⚠️ float 로 바뀌면 합계가 화면에서 1원씩 어긋난다."""
    _patch(monkeypatch, _Reader([_row(31)]))

    point = get_console_sales_trend(sim_run_id=RUN, as_of=AS_OF).rows[0]

    assert isinstance(point.sales_amount_krw, Decimal)
    assert isinstance(point.quantity_kg, Decimal)
    assert isinstance(point.contribution_profit_krw, Decimal)


def test_requested_date_range_is_bound_in_the_backend_query(monkeypatch):
    reader = _Reader([])
    _patch(monkeypatch, reader)
    start = date(2026, 3, 10)
    end = date(2026, 3, 20)

    get_console_sales_trend(
        sim_run_id=RUN,
        as_of=AS_OF,
        from_date=start,
        to_date=end,
    )

    _query, params = reader.calls[0]
    assert params[1] == end
    assert params[2:4] == [start, start]
    assert params[4] == end


def test_reversed_requested_date_range_fails_closed(monkeypatch):
    _patch(monkeypatch, _Reader([]))

    import pytest

    with pytest.raises(ValueError, match="from_date"):
        get_console_sales_trend(
            sim_run_id=RUN,
            as_of=AS_OF,
            from_date=date(2026, 3, 20),
            to_date=date(2026, 3, 10),
        )
