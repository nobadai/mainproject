"""첫 화면 「현금 잔고」 그래프는 **선택된 실행의 재무 일마감**을 그린다.

🔴 **실 DB 에 닿지 않는다.** `get_finance_cashflow` 를 대역으로 바꿔 «무엇을 넘기고
   무엇을 그리는가» 만 본다.

```text
① finance/query.py 에 예전 고정 곡선 상수 이름이 0건이다 (AST)
② SHOWN_SIM_RUN_ID 와 요청 as_of 가 get_finance_cashflow 까지 간다
③ 대역 두 벌을 주면 그래프가 각 대역 값과 정확히 같다 (앞 실행 숫자가 안 남는다)
④ 날짜축 정렬: 마감이 없는 날 · 기준일 뒤는 None
⑤ 대출 제외 / 대출 포함 = 행의 칸 ÷ 1e6
⑥ 화면 문장에 실제 sim_run_id · as_of 가 들어간다
```
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.api.calendar import build_axis
from app.api.finance import query as finance_query
from app.api.shown_run import SHOWN_SIM_RUN_ID
from app.finance.schemas import FinanceCashflowResponse, FinanceClosingItem, FinanceDashboardMeta

_FINANCE_QUERY = Path(__file__).resolve().parents[2] / "app" / "api" / "finance" / "query.py"
_OLD_NAMES = {"_DASH_ACTUAL", "_DASH_PROJ", "_DASH_FLOOR"}

#: SHOWN_AS_OF 와 다른 두 날. 기준일을 고정값으로 바꿔 넘기면 잡힌다.
AS_OF_A = date(2026, 1, 13)
AS_OF_B = date(2026, 1, 20)


def _row(
    close_date: date,
    base: int,
    loan: int,
    minimum: int | None = 5_000_000,
) -> FinanceClosingItem:
    return FinanceClosingItem(
        close_date=close_date,
        day_no=close_date.toordinal(),
        purchase_cash_out_krw=Decimal(0),
        logistics_cash_out_krw=Decimal(0),
        payroll_interest_cash_out_krw=Decimal(0),
        sales_recognized_krw=Decimal(0),
        collection_cash_in_krw=Decimal(0),
        base_net_cash_krw=Decimal(0),
        base_cash_balance_krw=Decimal(base),
        loan_execution_krw=Decimal(0),
        loan_cash_balance_krw=Decimal(loan),
        minimum_operating_cash_krw=None if minimum is None else Decimal(minimum),
        base_operating_buffer_krw=None,
        loan_operating_buffer_krw=None,
        receivables_balance_krw=Decimal(0),
        inventory_qty_kg=Decimal(0),
        accounting_inventory_cost_krw=Decimal(0),
    )


def _stub(rows: list[FinanceClosingItem], seen: list[dict] | None = None):
    def 현금흐름(*, sim_run_id: str, as_of: date, days: int) -> FinanceCashflowResponse:
        if seen is not None:
            seen.append({"sim_run_id": sim_run_id, "as_of": as_of, "days": days})
        return FinanceCashflowResponse(
            meta=FinanceDashboardMeta(sim_run_id=sim_run_id, as_of=as_of, data_type="SIMULATION"),
            cashflow=rows,
        )

    return 현금흐름


def _series(chart, name: str) -> list[float | None]:
    return next(s.data for s in chart.series if s.name == name)


# ── ① 예전 고정 곡선 ────────────────────────────────────────────────────


def test_재무_query_에_예전_고정_곡선_상수가_없다():
    files = [p for p in [_FINANCE_QUERY] if p.is_file()]
    assert files, f"스캔한 파일이 0개다 — 경로가 틀렸다: {_FINANCE_QUERY}"

    found: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            name = (
                node.id if isinstance(node, ast.Name)
                else node.attr if isinstance(node, ast.Attribute)
                else None
            )
            if name in _OLD_NAMES:
                found.append(f"{name}:{node.lineno}")
    assert not found, f"고정 곡선 상수가 남아 있다: {found}"


# ── ② 넘기는 값 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("as_of", [AS_OF_A, AS_OF_B])
def test_보는_실행과_요청_기준일을_현금흐름까지_넘긴다(monkeypatch, as_of):
    seen: list[dict] = []
    monkeypatch.setattr(finance_query, "get_finance_cashflow", _stub([], seen))
    axis = build_axis(as_of)

    finance_query.dashboard_cash(axis)

    assert len(seen) == 1
    assert seen[0]["sim_run_id"] == SHOWN_SIM_RUN_ID
    assert seen[0]["as_of"] == as_of
    assert seen[0]["days"] >= len(axis.days)


# ── ③ ④ ⑤ 그리는 값 ────────────────────────────────────────────────────


def test_두_실행_대역을_주면_각_대역_값을_그대로_그린다(monkeypatch):
    axis = build_axis(AS_OF_A)
    days = [date.fromisoformat(d.date) for d in axis.days]
    첫째 = [_row(day, 8_680_000 - i * 1_000_000, 53_950_000 - i * 500_000)
          for i, day in enumerate(days[: axis.as_of_index + 1])]
    둘째 = [_row(day, -12_150_000 + i * 250_000, 33_120_000 + i * 125_000)
          for i, day in enumerate(days[: axis.as_of_index + 1])]
    기대: dict[str, dict[str, list[float | None]]] = {}
    for key, rows in (("첫째", 첫째), ("둘째", 둘째)):
        tail = [None] * (len(days) - len(rows))
        기대[key] = {
            "대출 제외": [float(r.base_cash_balance_krw / Decimal(1_000_000)) for r in rows] + tail,
            "대출 포함": [float(r.loan_cash_balance_krw / Decimal(1_000_000)) for r in rows] + tail,
        }

    monkeypatch.setattr(finance_query, "get_finance_cashflow", _stub(첫째))
    chart_1 = finance_query.dashboard_cash(axis)
    monkeypatch.setattr(finance_query, "get_finance_cashflow", _stub(둘째))
    chart_2 = finance_query.dashboard_cash(axis)

    for chart, key in ((chart_1, "첫째"), (chart_2, "둘째")):
        assert _series(chart, "대출 제외") == 기대[key]["대출 제외"], key
        assert _series(chart, "대출 포함") == 기대[key]["대출 포함"], key
    assert _series(chart_2, "대출 제외") != _series(chart_1, "대출 제외")


def test_날짜축에_맞춰_넣고_빠진_날과_기준일_뒤는_공란이다(monkeypatch):
    axis = build_axis(AS_OF_A)
    days = [date.fromisoformat(d.date) for d in axis.days]
    at = axis.as_of_index
    빠진_날 = {1, 4}
    rows = [
        _row(day, 1_000_000 * (i + 1), 2_000_000 * (i + 1))
        for i, day in enumerate(days[: at + 1])
        if i not in 빠진_날
    ]
    #  기준일 다음 날 행이 섞여 들어와도 그리지 않는다.
    rows.append(_row(AS_OF_A + timedelta(days=1), 99_000_000, 99_000_000))
    monkeypatch.setattr(finance_query, "get_finance_cashflow", _stub(rows))

    chart = finance_query.dashboard_cash(axis)
    base = _series(chart, "대출 제외")
    loan = _series(chart, "대출 포함")

    assert len(base) == len(loan) == len(days)
    for i in range(len(days)):
        if i in 빠진_날 or i > at:
            assert base[i] is None and loan[i] is None, i
        else:
            assert base[i] == float((i + 1) * 1_000_000 / 1_000_000), i
            assert loan[i] == float((i + 1) * 2_000_000 / 1_000_000), i


def test_최소_운영현금은_행에_있을_때만_선이_된다(monkeypatch):
    axis = build_axis(AS_OF_A)
    day = AS_OF_A
    monkeypatch.setattr(
        finance_query, "get_finance_cashflow", _stub([_row(day, 1_000_000, 2_000_000, None)])
    )
    names = [s.name for s in finance_query.dashboard_cash(axis).series]
    assert names == ["대출 제외", "대출 포함"]

    monkeypatch.setattr(
        finance_query,
        "get_finance_cashflow",
        _stub([_row(day, 1_000_000, 2_000_000, 3_500_000)]),
    )
    chart = finance_query.dashboard_cash(axis)
    assert [s.name for s in chart.series] == ["대출 제외", "대출 포함", "최소 운영현금"]
    assert _series(chart, "최소 운영현금")[axis.as_of_index] == 3.5


def test_음수를_포함해_눈금이_데이터를_덮는다(monkeypatch):
    axis = build_axis(AS_OF_A)
    days = [date.fromisoformat(d.date) for d in axis.days]
    rows = [_row(days[0], 8_680_000, 53_950_000), _row(AS_OF_A, -12_150_000, 33_120_000)]
    monkeypatch.setattr(finance_query, "get_finance_cashflow", _stub(rows))

    chart = finance_query.dashboard_cash(axis)

    assert chart.y_unit == "M"
    assert chart.y_min < -12.15 and chart.y_max > 53.95
    assert 3 <= len(chart.y_ticks) <= 5
    assert chart.y_ticks[0] == chart.y_min and chart.y_ticks[-1] == chart.y_max
    assert not chart.markers


def test_기록이_없으면_공란과_없다는_문장이다(monkeypatch):
    axis = build_axis(AS_OF_A)
    monkeypatch.setattr(finance_query, "get_finance_cashflow", _stub([]))

    chart = finance_query.dashboard_cash(axis)

    assert all(v is None for s in chart.series for v in s.data)
    assert all(len(s.data) == len(axis.days) for s in chart.series)
    assert "이 실행·기준일에 현금 기록이 없습니다" in chart.note.text


# ── ⑥ 화면 문장 ─────────────────────────────────────────────────────────


def test_화면_문장이_실제_실행과_기준일을_따라간다(monkeypatch):
    texts = []
    for as_of in (AS_OF_A, AS_OF_B):
        monkeypatch.setattr(
            finance_query, "get_finance_cashflow", _stub([_row(as_of, 1_000_000, 2_000_000)])
        )
        chart = finance_query.dashboard_cash(build_axis(as_of))
        texts.append(chart.note.text)
        shown = f"보고 있는 실행: {SHOWN_SIM_RUN_ID} · 기준일: {as_of.isoformat()}"
        assert shown in chart.note.text
    assert texts[0] != texts[1]
    assert "아직 안 일어난 일" not in texts[0]
