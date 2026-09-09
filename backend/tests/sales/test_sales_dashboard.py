from datetime import date
from decimal import Decimal

from app.sales import dashboard_service

AS_OF = date(2025, 12, 31)


def test_sales_dashboard_aggregates_db_facts(monkeypatch):
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_sales_dashboard_meta",
        lambda **_: {"sim_run_id": "SIM-BURNIN-202512", "as_of": AS_OF, "data_type": "SIMULATION"},
    )
    monkeypatch.setattr(
        dashboard_service.repository,
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
        dashboard_service.repository,
        "load_collection_summary",
        lambda **_: [
            {"collection_status": "COLLECTED", "count": 6, "sales_amount_krw": Decimal(100)},
            {"collection_status": "PARTIAL", "count": 2, "sales_amount_krw": Decimal(200)},
            {"collection_status": "OPEN", "count": 7, "sales_amount_krw": Decimal(300)},
        ],
    )
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_item_summaries",
        lambda **_: [
            _item("ITEM-BAECHU", "배추", 10, "10000000"),
            _item("ITEM-MU", "무", 3, "20000000"),
            _item("ITEM-YANGPA", "양파", 2, "13881332"),
        ],
    )
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_recent_sales",
        lambda **_: [
            _sale("SALE-002", date(2025, 12, 31)),
            _sale("SALE-001", date(2025, 12, 30)),
        ],
    )
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_sales_receivables",
        lambda **_: [
            _receivable("AR-1", date(2025, 12, 20), Decimal(10), "OPEN"),
            _receivable("AR-2", date(2026, 1, 2), Decimal(0), "COLLECTED"),
            _receivable("AR-3", date(2026, 1, 3), Decimal(10), "PARTIAL"),
        ],
    )

    response = dashboard_service.get_sales_dashboard(
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
    assert response.receivables[0].display_status == "연체"
    assert response.receivables[1].display_status == "수금 완료"
    assert response.receivables[1].d_day is None
    assert response.receivables[2].display_status == "일부 수금"
    assert response.receivables[2].d_day == 3


def test_sales_dashboard_empty_unknown_sim_run(monkeypatch):
    monkeypatch.setattr(dashboard_service.repository, "load_sales_dashboard_meta", lambda **_: None)
    monkeypatch.setattr(dashboard_service.repository, "load_sales_summary", lambda **_: None)
    monkeypatch.setattr(dashboard_service.repository, "load_collection_summary", lambda **_: [])
    monkeypatch.setattr(dashboard_service.repository, "load_item_summaries", lambda **_: [])
    monkeypatch.setattr(dashboard_service.repository, "load_recent_sales", lambda **_: [])
    monkeypatch.setattr(dashboard_service.repository, "load_sales_receivables", lambda **_: [])

    response = dashboard_service.get_sales_dashboard(sim_run_id="NO-SUCH-RUN", as_of=AS_OF)

    assert response.meta.sim_run_id == "NO-SUCH-RUN"
    assert response.meta.data_type is None
    assert response.summary.sales_count == 0
    assert response.items == []


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


def _receivable(
    receivable_id: str, due_date: date, outstanding: Decimal, status: str
) -> dict[str, object]:
    return {
        "receivable_id": receivable_id,
        "sale_id": "SALE-1",
        "sale_date": AS_OF,
        "customer_partner_id": "PARTNER-1",
        "partner_name": "거래처",
        "issued_date": date(2025, 12, 1),
        "due_date": due_date,
        "original_amount_krw": Decimal(100),
        "received_amount_krw": Decimal(90),
        "outstanding_amount_krw": outstanding,
        "status": status,
    }
