"""판매 후보 입력이 공용 품목 정본을 사용하는지 검증한다."""

from app.sales.console_items import get_console_items


def test_console_items_reads_only_active_master_items_in_stored_order(monkeypatch):
    captured: dict[str, object] = {}

    def fetch_all(statement, params=None):
        captured["statement"] = str(statement)
        captured["params"] = params
        return [
            {
                "item_id": "ITEM-CABBAGE",
                "item_code": "BAECHU",
                "item_name": "배추",
                "base_unit": "kg",
            },
            {
                "item_id": "ITEM-RADISH",
                "item_code": "MU",
                "item_name": "무",
                "base_unit": "kg",
            },
        ]

    monkeypatch.setattr("app.sales.console_items.get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr("app.sales.console_items.fetch_all", fetch_all)

    response = get_console_items()

    assert [row.item_name for row in response.rows] == ["배추", "무"]
    assert [row.item_id for row in response.rows] == ["ITEM-CABBAGE", "ITEM-RADISH"]
    assert "Identifier('haetdeul')" in captured["statement"]
    assert "SQL('.items" in captured["statement"]
    assert "WHERE mvp_active" in captured["statement"]
    assert captured["params"] is None
