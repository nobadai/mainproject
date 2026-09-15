"""판매 운영 화면이 사용하는 활성 품목 정본 조회."""

from psycopg import sql
from pydantic import BaseModel

from app.sales.db import fetch_all, get_db_schema


class ConsoleItem(BaseModel):
    item_id: str
    item_code: str
    item_name: str
    base_unit: str


class ConsoleItemsResponse(BaseModel):
    rows: list[ConsoleItem]


def get_console_items() -> ConsoleItemsResponse:
    """공용 ``items`` 원장에서 판매 화면이 선택할 활성 품목을 읽는다."""
    rows = fetch_all(
        sql.SQL("""
            SELECT item_id, item_code, item_name, base_unit
            FROM {}.items
            WHERE mvp_active
            ORDER BY item_name, item_id
        """).format(sql.Identifier(get_db_schema()))
    )
    return ConsoleItemsResponse(rows=[ConsoleItem.model_validate(row) for row in rows])
