from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.db import FinanceDataNotReady, load_inventory_snapshot_as_of


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.executed.append((query.as_string(None), params))

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, rows):
        self.cursor_value = _Cursor(rows)

    def cursor(self):
        return self.cursor_value


def _snapshot(rows):
    conn = _Conn(rows)
    with patch("app.finance.db.get_db_schema", return_value="haetdeul"):
        result = load_inventory_snapshot_as_of(
            conn,
            sim_run_id="SIM-BURNIN-202512",
            as_of=date(2025, 12, 31),
        )
    return result, conn.cursor_value.executed[0]


def test_t0_replay_uses_ledger_quantity_and_immutable_lot_cost():
    rows = [
        ("LOT-KIMCHI-015-BAECHU", Decimal("1060"), "IN", Decimal("286.92")),
        ("LOT-KIMCHI-015-MU", Decimal("880"), "IN", Decimal("61.76")),
        ("LOT-KIMCHI-015-YANGPA", Decimal("1166.666667"), "IN", Decimal("5.72")),
        ("PUR-SAFETY-001-A", Decimal("1000"), "IN", Decimal("100")),
        ("PUR-SAFETY-001-A", Decimal("1000"), "OUT", Decimal("100")),
    ]

    snapshot, (query, params) = _snapshot(rows)

    assert snapshot.quantity_kg == Decimal("354.40")
    assert snapshot.inventory_book_value_krw == Decimal("365157.33333524")
    assert snapshot.operational_inventory_value_krw == Decimal("365157.33333524")
    assert params == {"sim_run_id": "SIM-BURNIN-202512", "as_of": date(2025, 12, 31)}
    assert "move.moved_at <= %(as_of)s" in query
    assert "remaining_qty_kg" not in query


def test_in_out_and_dispose_change_snapshot_and_no_move_is_stable():
    d0, _ = _snapshot([("LOT-A", Decimal("10"), "IN", Decimal("20"))])
    d1, _ = _snapshot(
        [
            ("LOT-A", Decimal("10"), "IN", Decimal("20")),
            ("LOT-A", Decimal("10"), "OUT", Decimal("3")),
            ("LOT-A", Decimal("10"), "DISPOSE", Decimal("2")),
        ]
    )
    unchanged, _ = _snapshot([("LOT-A", Decimal("10"), "IN", Decimal("20"))])

    assert d0.inventory_book_value_krw == Decimal("200")
    assert d1.inventory_book_value_krw == Decimal("150")
    assert unchanged == d0


@pytest.mark.parametrize(
    ("rows", "key"),
    [
        ([("LOT-A", Decimal("10"), "ADJUST", Decimal("1"))], "unsupported_inventory_move_type:ADJUST"),
        (
            [
                ("LOT-A", Decimal("10"), "IN", Decimal("1")),
                ("LOT-A", Decimal("10"), "OUT", Decimal("2")),
            ],
            "negative_inventory_lot_balance:LOT-A",
        ),
    ],
)
def test_unknown_movement_and_negative_balance_fail_closed(rows, key):
    with pytest.raises(FinanceDataNotReady) as raised:
        _snapshot(rows)

    assert raised.value.key == key
