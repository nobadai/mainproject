from datetime import date
from decimal import Decimal

from app.sales import dashboard

AS_OF = date(2025, 12, 31)


def test_sales_dashboard_aggregates_db_facts(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "load_sales_dashboard_meta",
        lambda **_: {"sim_run_id": "SIM-BURNIN-202512", "as_of": AS_OF, "data_type": "SIMULATION"},
    )
    monkeypatch.setattr(
        dashboard,
        "load_sales_summary",
        lambda **_: {
            "sales_count": 15,
            "customer_count": 1,
            "total_sales_quantity_kg": Decimal(26580),
            "total_sales_amount_krw": Decimal(43881332),
            "contribution_profit_krw": Decimal(8776266),
            "received_amount_krw": Decimal(21958777),
            "outstanding_receivables_krw": Decimal(21922555),
        },
    )
    monkeypatch.setattr(
        dashboard,
        "load_collection_summary",
        lambda **_: [
            {"collection_status": "COLLECTED", "count": 6, "sales_amount_krw": Decimal(100)},
            {"collection_status": "PARTIAL", "count": 2, "sales_amount_krw": Decimal(200)},
            {"collection_status": "OPEN", "count": 7, "sales_amount_krw": Decimal(300)},
        ],
    )
    monkeypatch.setattr(
        dashboard,
        "load_item_summaries",
        lambda **_: [
            _item("ITEM-BAECHU", "배추", 10, "10000000"),
            _item("ITEM-MU", "무", 3, "20000000"),
            _item("ITEM-YANGPA", "양파", 2, "13881332"),
        ],
    )
    monkeypatch.setattr(
        dashboard,
        "load_recent_sales",
        lambda **_: [
            _sale("SALE-002", date(2025, 12, 31)),
            _sale("SALE-001", date(2025, 12, 30)),
        ],
    )
    monkeypatch.setattr(
        dashboard,
        "load_today_confirmed_sales",
        lambda **_: [_confirmed_sale("SALE-002", AS_OF, date(2026, 1, 3))],
    )
    monkeypatch.setattr(
        dashboard,
        "load_sales_receivables",
        lambda **_: [
            _receivable("AR-1", date(2025, 12, 20), Decimal(10), "OPEN"),
            _receivable("AR-2", date(2026, 1, 2), Decimal(0), "COLLECTED"),
            _receivable("AR-3", date(2026, 1, 3), Decimal(10), "PARTIAL"),
        ],
    )

    response = dashboard.get_sales_dashboard(
        sim_run_id="SIM-BURNIN-202512", as_of=AS_OF
    )

    assert response.meta.data_type == "SIMULATION"
    assert response.summary.sales_count == 15
    assert response.summary.total_sales_quantity_kg == Decimal(26580)
    assert response.summary.total_sales_amount_krw == Decimal(43881332)
    assert response.summary.contribution_profit_krw == Decimal(8776266)
    assert response.summary.contribution_margin_pct == Decimal("20.00")
    assert response.summary.received_amount_krw == Decimal(21958777)
    assert response.summary.outstanding_receivables_krw == Decimal(21922555)
    assert [item.item_name for item in response.items] == ["배추", "무", "양파"]
    assert sum(item.sales_amount_krw for item in response.items) == Decimal(43881332)
    assert response.collection_summary["COLLECTED"].count == 6
    assert response.collection_summary["PARTIAL"].count == 2
    assert response.collection_summary["OPEN"].count == 7
    assert [sale.sale_id for sale in response.recent_sales] == ["SALE-002", "SALE-001"]
    assert response.today_confirmed_sales[0].order_date == AS_OF
    assert response.today_confirmed_sales[0].sale_date == date(2026, 1, 3)
    assert response.receivables[0].display_status == "연체"
    assert response.receivables[1].display_status == "수금 완료"
    assert response.receivables[1].d_day is None
    assert response.receivables[2].display_status == "일부 수금"
    assert response.receivables[2].d_day == 3


def test_sales_dashboard_empty_unknown_sim_run(monkeypatch):
    monkeypatch.setattr(dashboard, "load_sales_dashboard_meta", lambda **_: None)
    monkeypatch.setattr(dashboard, "load_sales_summary", lambda **_: None)
    monkeypatch.setattr(dashboard, "load_collection_summary", lambda **_: [])
    monkeypatch.setattr(dashboard, "load_item_summaries", lambda **_: [])
    monkeypatch.setattr(dashboard, "load_recent_sales", lambda **_: [])
    monkeypatch.setattr(dashboard, "load_today_confirmed_sales", lambda **_: [])
    monkeypatch.setattr(dashboard, "load_sales_receivables", lambda **_: [])

    response = dashboard.get_sales_dashboard(sim_run_id="NO-SUCH-RUN", as_of=AS_OF)

    assert response.meta.sim_run_id == "NO-SUCH-RUN"
    assert response.meta.data_type is None
    assert response.summary.sales_count == 0
    assert response.items == []


def test_today_confirmed_sales_uses_confirmation_date_not_delivery_date(monkeypatch):
    captured: dict[str, object] = {}

    def fake_fetch_all(query, params):
        captured["query"] = str(query)
        captured["params"] = params
        return []

    monkeypatch.setattr(dashboard, "fetch_all", fake_fetch_all)

    assert (
        dashboard.load_today_confirmed_sales(
            sim_run_id="SIM-20260914", as_of=date(2026, 9, 14)
        )
        == []
    )

    query = str(captured["query"])
    assert "s.order_date = %s" in query
    assert "s.sale_date <= %s" not in query
    assert "s.order_status IN ('CONFIRMED', 'DELIVERED')" in query
    assert captured["params"] == ["SIM-20260914", date(2026, 9, 14)]


def test_today_confirmed_sales_preserves_future_delivery_date():
    rows = [_confirmed_sale("SALE-1", date(2026, 9, 14), date(2026, 9, 17))]

    confirmed = dashboard._today_confirmed_sales(rows)

    assert confirmed[0].order_date == date(2026, 9, 14)
    assert confirmed[0].sale_date == date(2026, 9, 17)
    assert confirmed[0].order_status == "CONFIRMED"


def _item(item_id: str, item_name: str, count: int, amount: str) -> dict[str, object]:
    return {
        "item_id": item_id,
        "item_name": item_name,
        "line_count": count,
        "total_quantity_kg": Decimal(1),
        "sales_amount_krw": Decimal(amount),
        "contribution_profit_krw": Decimal(amount) * Decimal("0.2"),
        "avg_unit_price_krw_per_kg": Decimal(amount),
    }


def _sale(sale_id: str, sale_date: date) -> dict[str, object]:
    return {
        "sale_id": sale_id,
        "order_date": sale_date,
        "sale_date": sale_date,
        "customer_partner_id": "PARTNER-1",
        "partner_name": "거래처",
        "total_quantity_kg": Decimal(1),
        "total_amount_krw": Decimal(100),
        "contribution_profit_krw": Decimal(20),
        "collection_due_date": AS_OF,
        "collection_status": "PARTIAL",
        "order_status": "DELIVERED",
    }


def _confirmed_sale(sale_id: str, order_date: date, sale_date: date) -> dict[str, object]:
    return {
        "sale_id": sale_id,
        "order_date": order_date,
        "sale_date": sale_date,
        "customer_partner_id": "PARTNER-1",
        "partner_name": "테스트거래처A",
        "item_id": "ITEM-BAECHU",
        "item_name": "배추",
        "quantity_kg": Decimal(500),
        "unit_price_krw_per_kg": Decimal(1200),
        "line_amount_krw": Decimal(600000),
        "order_status": "CONFIRMED",
    }


def _receivable(
    receivable_id: str, due_date: date, outstanding: Decimal, status: str
) -> dict[str, object]:
    """🔴 **금액과 상태가 서로 맞는 행만 만든다.**

    전에는 어느 상태든 `received 90 / original 100` 이라, «다 받았다» 고 적힌 행의
    잔액이 10원이었다. 화면이 상태를 금액에서 다시 세게 되면서 그 모순이 드러난다 —
    검사가 쓰는 사실부터 말이 되어야 한다.
    """
    original = Decimal(100)
    received = original - outstanding
    return {
        "receivable_id": receivable_id,
        "sale_id": "SALE-1",
        "sale_date": AS_OF,
        "customer_partner_id": "PARTNER-1",
        "partner_name": "거래처",
        "issued_date": date(2025, 12, 1),
        "due_date": due_date,
        "original_amount_krw": original,
        "received_amount_krw": received,
        "outstanding_amount_krw": outstanding,
        "status": status,
    }
