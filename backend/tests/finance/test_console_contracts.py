"""운영 화면이 기대는 읽기 계약. **화면이 믿는 것을 여기서 잠근다.**

★ 화면은 백엔드가 준 순서와 단위를 그대로 믿는다. 그 믿음이 코드에 적혀 있지 않으면,
  정렬이나 단위를 바꾼 날 화면만 조용히 틀린다.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.api.finance.console_routes import MAX_CASHFLOW_DAYS
from app.finance import dashboard
from app.sales import dashboard as sales_dashboard
from app.sales.console_trend import MAX_TREND_DAYS


class _Reader:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls: list[tuple[str, list]] = []

    def __call__(self, query, params=None):
        self.calls.append((str(query), list(params or [])))
        return list(self.rows)


# ── 최근 마감 순서 ────────────────────────────────────────────────────────


def test_recent_closings_come_back_newest_first(monkeypatch):
    """🔴 화면이 «마지막 마감» 을 고를 때 기대는 순서다.

    이 순서를 바꾸면 배열 끝을 읽던 화면이 **가장 오래된 마감**을 최신으로 적는다 —
    실제로 2026-01-26 화면에 2026-01-15 잔액이 나왔다.
    """
    reader = _Reader()
    monkeypatch.setattr(dashboard, "fetch_all", reader)
    monkeypatch.setattr(dashboard, "get_db_schema", lambda: "haetdeul")

    dashboard.load_recent_closings(sim_run_id="RUN", as_of=date(2026, 1, 26), limit=10)

    query, params = reader.calls[0]
    assert "ORDER BY close_date DESC" in query
    assert params == ["RUN", date(2026, 1, 26), 10]


def test_recent_closings_never_reach_past_the_as_of(monkeypatch):
    """미래 마감이 섞이면 화면이 기준일보다 뒤의 잔액을 «현재» 로 적는다."""
    reader = _Reader()
    monkeypatch.setattr(dashboard, "fetch_all", reader)
    monkeypatch.setattr(dashboard, "get_db_schema", lambda: "haetdeul")

    dashboard.load_recent_closings(sim_run_id="RUN", as_of=date(2026, 1, 26), limit=10)

    assert "close_date <= %s" in reader.calls[0][0]


# ── 조회 기간 상한 ────────────────────────────────────────────────────────


def test_the_cashflow_window_covers_a_whole_run():
    """자금 흐름 «전체» 가 실제로 전 기간을 덮어야 한다.

    상한이 30 이던 때에는 화면의 «전체» 버튼이 422 를 받았고, 같은 실행의 변동폭이
    20,832,701 원인데 9,041,102 원으로만 보였다.
    """
    assert MAX_CASHFLOW_DAYS >= 400


def test_the_sales_trend_window_covers_a_whole_run():
    assert MAX_TREND_DAYS >= 400


# ── 공헌이익률 단위 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profit", "sales", "expected"),
    [
        (Decimal(6800970), Decimal(19524800), Decimal("34.83")),
        (Decimal(0), Decimal(1000), Decimal("0.00")),
        (Decimal(1000), Decimal(1000), Decimal("100.00")),
        (Decimal(1000), Decimal(0), Decimal(0)),
    ],
)
def test_contribution_margin_pct_is_percentage_points_not_a_ratio(profit, sales, expected):
    """🔴 **단위가 계약이다.**

    `contribution_margin_pct` 는 이미 100 을 곱한 값이라 `34.83` 으로 온다. 화면이 이것을
    0~1 비율 formatter 에 넣으면 한 번 더 곱해져 **3483.0%** 가 된다 — 실제로 그랬다.
    """
    assert sales_dashboard._pct(profit, sales) == expected


def test_a_ratio_would_never_look_like_a_percentage_point():
    """비율이었다면 0~1 안에 있어야 한다. 34.83 은 그 범위 밖이다."""
    value = sales_dashboard._pct(Decimal(6800970), Decimal(19524800))

    assert value > 1
    assert value < 100
