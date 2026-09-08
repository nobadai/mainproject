"""화면용 API — 여섯 탭이 계약대로 값을 내는가.

★ **이 테스트가 있는 이유.** 부서가 자기 `query.py` 를 채울 때, 응답 모양을
  같이 바꿔 버리면 화면이 **조용히 빈 칸**이 됩니다. 오류가 안 나서 아무도
  모릅니다. 그래서 모양만 여기서 잡아 둡니다.

★ **값은 안 봅니다. 모양만 봅니다.** 부서가 예시값을 실제 값으로 바꾸면
  숫자는 당연히 달라집니다. 숫자를 잡으면 채우는 사람이 테스트를 지우게
  되고, 그러면 검사가 사라집니다.

★ 라우터만 격리해 띄웁니다 — `app.main` 전체는 psycopg 를 요구합니다
  (`tests/master/test_master_api.py` 와 같은 이유).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.finance import query as finance_query
from app.api.router import router
from app.api.sales import query as sales_query
from app.finance.schemas import (
    FinanceCashflowResponse,
    FinanceCashflowSummary,
    FinanceClosingItem,
    FinanceDashboardMeta,
    FinanceDashboardResponse,
    FinancePayableSummary,
    FinanceReceivableSummary,
    FinanceStateView,
)
from app.sales.schemas import (
    SalesCollectionStatusSummary,
    SalesDashboardMeta,
    SalesDashboardResponse,
    SalesDashboardSummary,
    SalesHistoryItem,
    SalesItemSummary,
    SalesReceivableItem,
)

AS_OF = "2026-01-06"
FIN_AS_OF = "2025-12-31"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(sales_query, "get_sales_dashboard", _sales_dashboard_stub)
    monkeypatch.setattr(finance_query, "get_finance_dashboard", _finance_dashboard_stub)
    monkeypatch.setattr(finance_query, "get_finance_cashflow", _finance_cashflow_stub)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


#: (주소, 파라미터). 부서가 탭을 늘리면 여기에 한 줄 더합니다.
TABS = [
    ("/api/dashboard", {"as_of": AS_OF}),
    ("/api/forecast", {"as_of": AS_OF, "item": "배추"}),
    ("/api/purchase", {"as_of": AS_OF}),
    ("/api/finance", {"as_of": FIN_AS_OF, "state": "base"}),
    ("/api/logistics", {"as_of": AS_OF, "pane": "stock"}),
    ("/api/sales", {"as_of": FIN_AS_OF}),
]


@pytest.mark.parametrize(("path", "params"), TABS)
def test_탭이_열린다(client, path, params):
    """여섯 탭 모두 200 이어야 한다. 하나라도 500 이면 그 화면은 통째로 빈다."""
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(("path", "params"), TABS)
def test_쓰기는_막혀_있다(client, path, params):
    """`/api` 아래는 **읽기만** 한다.

    쓰기는 기존 경로(`POST /master/…/decision`)가 한다. 여기에 POST 가 생기면
    같은 일을 두 군데서 하게 되고, «어느 쪽으로 승인했나» 가 기록에서 갈린다.
    """
    assert client.post(path, params=params).status_code == 405


def test_없는_값은_400_이지_500_이_아니다(client):
    """무엇을 잘못 넣었는지 **문장으로** 알려줘야 한다."""
    for path, params, bad in [
        ("/api/forecast", {"as_of": AS_OF}, {"item": "딸기"}),
        ("/api/finance", {"as_of": FIN_AS_OF}, {"state": "없는상태"}),
        ("/api/logistics", {"as_of": AS_OF}, {"pane": "없는화면"}),
    ]:
        response = client.get(path, params={**params, **bad})
        assert response.status_code == 400, response.text
        assert "가능:" in response.json()["detail"]


def test_재무_state가_없는_날짜도_400이다(monkeypatch):
    monkeypatch.setattr(finance_query, "get_finance_dashboard", _empty_finance_dashboard_stub)
    monkeypatch.setattr(finance_query, "get_finance_cashflow", _finance_cashflow_stub)
    app = FastAPI()
    app.include_router(router)
    local_client = TestClient(app)

    response = local_client.get("/api/finance", params={"as_of": AS_OF, "state": "base"})

    assert response.status_code == 400
    assert "Finance state was not found: BASE_NO_LOAN" == response.json()["detail"]


def test_예시값인지_아닌지를_반드시_밝힌다(client):
    """`Source.filled` 가 없으면 화면이 「예시값」 딱지를 못 붙인다.

    딱지가 없으면 보는 사람이 **데모 숫자를 실적으로 읽습니다.**
    """
    for path, params in TABS:
        body = client.get(path, params=params).json()
        sources = body.get("sources") or [body.get("source")]
        assert sources and all(s is not None for s in sources), path
        for source in sources:
            assert isinstance(source["filled"], bool), path
            assert source["owner"], path


def test_판매_화면은_dashboard_service_값을_쓴다(client):
    body = client.get("/api/sales", params={"as_of": FIN_AS_OF}).json()

    assert body["source"]["filled"] is True
    assert body["stats"][0]["raw"] == 1_234_567
    assert body["stats"][3]["raw"] == 650_000
    summary = next(card for card in body["cards"] if card["key"] == "summary")
    assert summary["stats"][1]["value"] == "완료 1 · 일부 1 · 예정 1"
    recent = next(card for card in body["cards"] if card["key"] == "recent")
    assert [row["no"] for row in recent["table"]["rows"]] == ["SALE-002", "SALE-001"]


def test_판매_화면은_요청_as_of를_service에_그대로_넘긴다(monkeypatch):
    seen: dict[str, date] = {}

    def spy(sim_run_id: str, as_of: date) -> SalesDashboardResponse:
        seen["as_of"] = as_of
        return _sales_dashboard_stub(sim_run_id, as_of)

    monkeypatch.setattr(sales_query, "get_sales_dashboard", spy)

    sales_query.build(date(2026, 1, 6))

    assert seen["as_of"] == date(2026, 1, 6)


def test_판매_수금_차트는_남은_금액만_쓴다(client):
    body = client.get("/api/sales", params={"as_of": FIN_AS_OF}).json()
    receivables = next(card for card in body["cards"] if card["key"] == "ar")

    assert receivables["chart"]["series"][0]["data"] == [0.65]
    rows = receivables["table"]["rows"]
    assert next(row for row in rows if row["status"] == "수금 완료")["d_day"] is None
    assert next(row for row in rows if row["status"] == "일부 수금")["d_day"] == 3
    assert next(row for row in rows if row["status"] == "수금 예정")["d_day"] == 4


def test_재무_화면은_financing_mode로_state를_고른다(client):
    base = client.get("/api/finance", params={"as_of": FIN_AS_OF, "state": "base"}).json()
    loan = client.get("/api/finance", params={"as_of": FIN_AS_OF, "state": "loan"}).json()

    assert base["source"]["filled"] is True
    assert base["stats"][0]["raw"] == 100_000
    assert base["stats"][3]["raw"] == 0
    assert "BASE_NO_LOAN" in base["explain"]["text"]
    assert loan["stats"][0]["raw"] == 500_000
    assert loan["stats"][3]["raw"] == 300_000
    assert "LOAN_BASELINE" in loan["explain"]["text"]


def test_재무_화면은_요청_as_of를_service에_그대로_넘긴다(monkeypatch):
    seen: dict[str, date] = {}

    def dashboard_spy(sim_run_id: str, as_of: date) -> FinanceDashboardResponse:
        seen["dashboard_as_of"] = as_of
        return _finance_dashboard_stub(sim_run_id, as_of)

    def cashflow_spy(sim_run_id: str, as_of: date, days: int) -> FinanceCashflowResponse:
        seen["cashflow_as_of"] = as_of
        return _finance_cashflow_stub(sim_run_id, as_of, days)

    monkeypatch.setattr(finance_query, "get_finance_dashboard", dashboard_spy)
    monkeypatch.setattr(finance_query, "get_finance_cashflow", cashflow_spy)

    finance_query.build(date(2026, 1, 6), "base")

    assert seen == {
        "dashboard_as_of": date(2026, 1, 6),
        "cashflow_as_of": date(2026, 1, 6),
    }


def test_재무_화면은_cashflow와_ledger를_쓴다(client):
    body = client.get("/api/finance", params={"as_of": FIN_AS_OF, "state": "base"}).json()

    assert body["cash_chart"]["series"][0]["data"] == [0.09, 0.1]
    assert body["cash_chart"]["series"][1]["data"] == [0.49, 0.5]
    assert body["flows"][4]["value"] == "12만원"
    assert body["balances"][0]["raw"] == 1_000
    assert body["balances"][2]["raw"] == 0
    assert [row["d"] for row in body["closings"]["rows"]] == ["2025-12-31", "2025-12-30"]


def test_그래프_계열은_날짜축과_길이가_같다(client):
    """★ 길이가 어긋나면 선이 **엉뚱한 날짜에 그려진다.**

    화면은 칸 번호로만 위치를 잡는다. 계열이 짧으면 조용히 왼쪽으로 밀린다 —
    오류가 안 나서 «그날 값이 그랬구나» 로 읽히는 것이 제일 위험하다.
    """
    body = client.get("/api/dashboard", params={"as_of": AS_OF}).json()
    n = len(body["axis"]["days"])
    for key in ("cash_chart", "stock_chart"):
        for series in body[key]["series"]:
            assert len(series["data"]) == n, f"{key} · {series['name']}"


def test_눈금_글자를_주면_눈금_수와_같아야_한다(client):
    """`y_labels` 는 `y_ticks` 와 짝이다. 짧으면 아래쪽 눈금이 글자를 잃는다."""
    body = client.get("/api/dashboard", params={"as_of": AS_OF}).json()
    for key in ("cash_chart", "stock_chart"):
        chart = body[key]
        if chart["y_labels"]:
            assert len(chart["y_labels"]) == len(chart["y_ticks"]), key


def test_대시보드는_숫자를_만들지_않는다(client):
    """대시보드 값은 **부서 탭의 값과 같아야** 한다.

    같은 값을 두 군데서 계산하면 언젠가 갈라진다. 실제로 갈라졌었다 —
    재고 요약이 4,550kg 인데 그래프 끝은 14,600kg 이었다.
    """
    dash = client.get("/api/dashboard", params={"as_of": AS_OF}).json()
    stock_stat = next(s for s in dash["stats"] if "재고" in s["label"])
    last_actual = [v for v in dash["stock_chart"]["series"][0]["data"] if v is not None][-1]
    assert stock_stat["raw"] == last_actual

    logistics = client.get("/api/logistics", params={"as_of": AS_OF, "pane": "stock"}).json()
    pane = next(p for p in logistics["panes"] if p["key"] == "stock")
    assert stock_stat["raw"] == pane["stats"][0]["raw"]

    forecast = client.get("/api/forecast", params={"as_of": AS_OF, "item": "배추"}).json()
    assert dash["forecast_cards"] == forecast["cards"]


def test_표의_칸_이름이_행에_있다(client):
    """머리에 있는데 행에 없으면 그 칸은 **통째로 공란**이 된다.

    부서가 `columns` 만 고치고 `rows` 를 안 고치는 실수를 여기서 잡는다.
    """
    checks = [
        ("/api/purchase", {"as_of": AS_OF}, ["committed"]),
        ("/api/finance", {"as_of": FIN_AS_OF, "state": "base"}, ["closings"]),
        ("/api/forecast", {"as_of": AS_OF, "item": "배추"}, ["accuracy", "quality"]),
    ]
    for path, params, keys in checks:
        body = client.get(path, params=params).json()
        for key in keys:
            table = body[key]
            names = {c["key"] for c in table["columns"]}
            for row in table["rows"]:
                missing = names - set(row)
                assert not missing, f"{path} · {key} · 빠진 칸 {missing}"


def _sales_dashboard_stub(sim_run_id: str, as_of: date) -> SalesDashboardResponse:
    assert sim_run_id == sales_query.BURN_IN_SIM_RUN_ID
    return SalesDashboardResponse(
        meta=SalesDashboardMeta(sim_run_id=sim_run_id, as_of=as_of, data_type="SIMULATION"),
        summary=SalesDashboardSummary(
            sales_count=2,
            customer_count=1,
            total_sales_quantity_kg=Decimal(1500),
            total_sales_amount_krw=Decimal(1234567),
            contribution_profit_krw=Decimal(246913),
            contribution_margin_pct=Decimal("20.00"),
            received_amount_krw=Decimal(584567),
            outstanding_receivables_krw=Decimal(650000),
        ),
        collection_summary={
            "COLLECTED": SalesCollectionStatusSummary(
                count=1,
                sales_amount_krw=Decimal(584567),
            ),
            "PARTIAL": SalesCollectionStatusSummary(count=1, sales_amount_krw=Decimal(250000)),
            "OPEN": SalesCollectionStatusSummary(count=1, sales_amount_krw=Decimal(400000)),
        },
        items=[
            SalesItemSummary(
                item_id="ITEM-A",
                item_name="품목A",
                line_count=2,
                total_quantity_kg=Decimal(1500),
                sales_amount_krw=Decimal(1234567),
                contribution_profit_krw=Decimal(246913),
                contribution_margin_pct=Decimal("20.00"),
                avg_unit_price_krw_per_kg=Decimal("823.044667"),
            )
        ],
        recent_sales=[
            SalesHistoryItem(
                sale_id="SALE-002",
                sale_date=date(2025, 12, 31),
                customer_partner_id="PARTNER-1",
                partner_name="거래처",
                total_quantity_kg=Decimal(500),
                total_amount_krw=Decimal(400000),
                contribution_profit_krw=Decimal(80000),
                contribution_margin_pct=Decimal("20.00"),
                collection_due_date=date(2026, 1, 4),
                collection_status="OPEN",
                collection_status_label="수금 예정",
                order_status="SHIPPED",
            ),
            SalesHistoryItem(
                sale_id="SALE-001",
                sale_date=date(2025, 12, 30),
                customer_partner_id="PARTNER-1",
                partner_name="거래처",
                total_quantity_kg=Decimal(1000),
                total_amount_krw=Decimal(834567),
                contribution_profit_krw=Decimal(166913),
                contribution_margin_pct=Decimal("20.00"),
                collection_due_date=date(2026, 1, 3),
                collection_status="PARTIAL",
                collection_status_label="일부 수금",
                order_status="SHIPPED",
            ),
        ],
        receivables=[
            SalesReceivableItem(
                receivable_id="AR-C",
                sale_id="SALE-C",
                sale_date=date(2025, 12, 29),
                customer_partner_id="PARTNER-1",
                partner_name="거래처",
                issued_date=date(2025, 12, 29),
                due_date=date(2026, 1, 2),
                original_amount_krw=Decimal(584567),
                received_amount_krw=Decimal(584567),
                outstanding_amount_krw=Decimal(0),
                status="COLLECTED",
                display_status="수금 완료",
                d_day=None,
            ),
            SalesReceivableItem(
                receivable_id="AR-P",
                sale_id="SALE-001",
                sale_date=date(2025, 12, 30),
                customer_partner_id="PARTNER-1",
                partner_name="거래처",
                issued_date=date(2025, 12, 30),
                due_date=date(2026, 1, 3),
                original_amount_krw=Decimal(834567),
                received_amount_krw=Decimal(584567),
                outstanding_amount_krw=Decimal(250000),
                status="PARTIAL",
                display_status="일부 수금",
                d_day=3,
            ),
            SalesReceivableItem(
                receivable_id="AR-O",
                sale_id="SALE-002",
                sale_date=date(2025, 12, 31),
                customer_partner_id="PARTNER-1",
                partner_name="거래처",
                issued_date=date(2025, 12, 31),
                due_date=date(2026, 1, 3),
                original_amount_krw=Decimal(400000),
                received_amount_krw=Decimal(0),
                outstanding_amount_krw=Decimal(400000),
                status="OPEN",
                display_status="수금 예정",
                d_day=4,
            ),
        ],
    )


def _finance_dashboard_stub(sim_run_id: str, as_of: date) -> FinanceDashboardResponse:
    assert sim_run_id == finance_query.BURN_IN_SIM_RUN_ID
    return FinanceDashboardResponse(
        meta=FinanceDashboardMeta(sim_run_id=sim_run_id, as_of=as_of, data_type="SIMULATION"),
        states=[
            _finance_state("FS-LOAN", as_of, "LOAN_BASELINE", Decimal(500000), Decimal(300000)),
            _finance_state("FS-BASE", as_of, "BASE_NO_LOAN", Decimal(100000), Decimal(0)),
        ],
        cashflow_summary=FinanceCashflowSummary(
            purchase_cash_out_krw=Decimal(200000),
            logistics_cash_out_krw=Decimal(30000),
            payroll_interest_cash_out_krw=Decimal(40000),
            sales_recognized_krw=Decimal(500000),
            collection_cash_in_krw=Decimal(123456),
            base_net_cash_krw=Decimal(230000),
            loan_execution_krw=Decimal(400),
        ),
        ledger_summary={
            "receivables": FinanceReceivableSummary(
                count=3,
                collected_count=1,
                partial_count=1,
                open_count=1,
                original_amount_krw=Decimal(1000),
                received_amount_krw=Decimal(350),
                outstanding_amount_krw=Decimal(650),
                overdue_amount_krw=Decimal(0),
            ),
            "payables": FinancePayableSummary(
                count=1,
                original_amount_krw=Decimal(700),
                paid_amount_krw=Decimal(700),
                outstanding_amount_krw=Decimal(0),
                overdue_amount_krw=Decimal(0),
            ),
        },
        receivables=[],
        payables=[],
        expenses=[],
        recent_closings=[
            _closing(date(2025, 12, 31), 2, Decimal(100000), Decimal(500000)),
            _closing(date(2025, 12, 30), 1, Decimal(90000), Decimal(490000)),
        ],
    )


def _empty_finance_dashboard_stub(sim_run_id: str, as_of: date) -> FinanceDashboardResponse:
    assert sim_run_id == finance_query.BURN_IN_SIM_RUN_ID
    assert as_of == date(2026, 1, 6)
    return FinanceDashboardResponse(
        meta=FinanceDashboardMeta(sim_run_id=sim_run_id, as_of=as_of, data_type="SIMULATION"),
        states=[],
        cashflow_summary=FinanceCashflowSummary(
            purchase_cash_out_krw=Decimal(0),
            logistics_cash_out_krw=Decimal(0),
            payroll_interest_cash_out_krw=Decimal(0),
            sales_recognized_krw=Decimal(0),
            collection_cash_in_krw=Decimal(0),
            base_net_cash_krw=Decimal(0),
            loan_execution_krw=Decimal(0),
        ),
        ledger_summary={
            "receivables": FinanceReceivableSummary(
                count=0,
                collected_count=0,
                partial_count=0,
                open_count=0,
                original_amount_krw=Decimal(0),
                received_amount_krw=Decimal(0),
                outstanding_amount_krw=Decimal(0),
                overdue_amount_krw=Decimal(0),
            ),
            "payables": FinancePayableSummary(
                count=0,
                original_amount_krw=Decimal(0),
                paid_amount_krw=Decimal(0),
                outstanding_amount_krw=Decimal(0),
                overdue_amount_krw=Decimal(0),
            ),
        },
        receivables=[],
        payables=[],
        expenses=[],
        recent_closings=[],
    )


def _finance_cashflow_stub(
    sim_run_id: str,
    as_of: date,
    days: int,
) -> FinanceCashflowResponse:
    assert sim_run_id == finance_query.BURN_IN_SIM_RUN_ID
    return FinanceCashflowResponse(
        meta=FinanceDashboardMeta(sim_run_id=sim_run_id, as_of=as_of, data_type="SIMULATION"),
        cashflow=[
            _closing(date(2025, 12, 30), 1, Decimal(90000), Decimal(490000)),
            _closing(date(2025, 12, 31), 2, Decimal(100000), Decimal(500000)),
        ][:days],
    )



def _finance_state(
    finance_state_id: str,
    state_date: date,
    financing_mode: str,
    cash: Decimal,
    debt: Decimal,
) -> FinanceStateView:
    minimum = Decimal(150000)
    return FinanceStateView(
        finance_state_id=finance_state_id,
        state_date=state_date,
        state_type="DAILY_CLOSING",
        financing_mode=financing_mode,
        current_cash_krw=cash,
        minimum_operating_cash_krw=minimum,
        operating_cash_buffer_krw=cash - minimum,
        committed_outflows_krw=Decimal(0),
        unsettled_purchase_payables_krw=Decimal(0),
        receivables_krw=Decimal(650),
        inventory_book_value_krw=Decimal(800),
        operational_inventory_value_krw=Decimal(750),
        current_debt_krw=debt,
        financial_limit_krw=Decimal(1000000),
        recommended_loan_amount_krw=None,
        note="fixture note",
    )


def _closing(
    close_date: date,
    day_no: int,
    base_cash: Decimal,
    loan_cash: Decimal,
) -> FinanceClosingItem:
    return FinanceClosingItem(
        close_date=close_date,
        day_no=day_no,
        purchase_cash_out_krw=Decimal(200),
        logistics_cash_out_krw=Decimal(30),
        payroll_interest_cash_out_krw=Decimal(40),
        sales_recognized_krw=Decimal(500),
        collection_cash_in_krw=Decimal(123),
        base_net_cash_krw=Decimal(230),
        base_cash_balance_krw=base_cash,
        loan_execution_krw=Decimal(400),
        loan_cash_balance_krw=loan_cash,
        minimum_operating_cash_krw=Decimal(150000),
        base_operating_buffer_krw=base_cash - Decimal(150000),
        loan_operating_buffer_krw=loan_cash - Decimal(150000),
        receivables_balance_krw=Decimal(650),
        inventory_qty_kg=Decimal(900),
        accounting_inventory_cost_krw=Decimal(800),
    )
