"""여러 실행이 **동시에 서 있을 때** 재무가 자기 실행만 읽는가.

🔴 이 파일이 생긴 이유. 번인과 새 걷기가 공존하는 순간 첫 개장이 막혔다.

    ```text
    SIM-BURNIN-202512     / LOAN_BASELINE
    SIM-WALK-202601-LOAN  / LOAN_BASELINE
    → finance_runtime_axis_ambiguous
    ```

  fail-closed 자체는 옳았다. **질문이 틀렸다.**

    ```text
    예전  "시스템 전체에 재무 축이 하나뿐인가?"
    지금  "이번 호출의 sim_run_id 에 해당하는 재무 축은 무엇인가?"
    ```

★ 그래서 가르는 기준은 하나다.

    ```text
    다른 실행이 하나 더 있다   → 정상 (내 실행은 모호하지 않다)
    같은 실행에 축이 둘이다     → 모호 (fail-closed)
    ```

⚠️ **다른 실행으로 흘러가지 않는다.** 요청한 실행이 없으면 없는 것이고, 가장 최근
  실행이나 유일한 실행으로 대신하지 않는다 — 그 사고는 에러 없이 숫자만 바꾼다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import db as finance_db
from app.finance.day_open import FinanceDayOpening
from app.finance.db import (
    FinanceDataNotReady,
    _get_current_finance_state_row,
    get_finance_runtime_axis,
    load_finance_state_row,
)

_DB = "app.finance.db"

BURN_IN = "SIM-BURNIN-202512"
WALK = "SIM-WALK-202601-LOAN"
MODE = "LOAN_BASELINE"
AS_OF = date(2026, 1, 5)


def _axis_row(sim_run_id: str, financing_mode: str = MODE) -> dict[str, object]:
    return {"sim_run_id": sim_run_id, "financing_mode": financing_mode}


def _state_row(sim_run_id: str, *, state_date: date = AS_OF) -> dict[str, object]:
    return {
        "finance_state_id": f"FIN-{sim_run_id}-{state_date:%Y%m%d}",
        "sim_run_id": sim_run_id,
        "state_date": state_date,
        "state_type": "DAY",
        "financing_mode": MODE,
        "current_cash_krw": Decimal(53_952_691),
        "minimum_operating_cash_krw": Decimal(15_902_640),
        "committed_outflows_krw": Decimal(0),
        "unsettled_purchase_payables_krw": Decimal(0),
        "receivables_krw": Decimal(21_922_554),
        "current_debt_krw": Decimal(45_272_104),
        "financial_limit_krw": Decimal(40_000_000),
    }


class _Recorder:
    """`fetch_all` 대역. **무엇을 물었는지** 와 **무엇을 돌려줄지** 를 함께 든다.

    ★ 실제 View 처럼 행동한다 — 질의에 `WHERE sim_run_id = %s` 가 있으면 그 실행만
      돌려주고, 없으면 전부 돌려준다. 대역이 실물보다 너그러우면 통과한 검사가
      아무것도 증명하지 못한다.
    """

    def __init__(self, rows: list[dict[str, object]]):
        self.rows = rows
        self.calls: list[tuple[str, list[object]]] = []

    def __call__(self, query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.calls.append((text, list(params or [])))
        if "WHERE sim_run_id = %s" in text or "sim_run_id = %s" in text:
            wanted = params[0]
            return [row for row in self.rows if row["sim_run_id"] == wanted]
        return list(self.rows)


# ---------------------------------------------------------------------------
# Case A — 다른 실행이 있어도 요청한 실행이 정상 선택된다
# ---------------------------------------------------------------------------


def test_another_running_sim_run_does_not_make_this_one_ambiguous():
    """🔴 실 장애 재현. 번인이 함께 서 있다고 새 걷기가 막히면 안 된다."""
    fetch_all = _Recorder([_axis_row(BURN_IN), _axis_row(WALK)])

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
    ):
        axis = get_finance_runtime_axis(sim_run_id=WALK)

    assert axis["sim_run_id"] == WALK
    assert axis["financing_mode"] == MODE


def test_the_scoped_query_actually_narrows_by_sim_run_id():
    """★ 좁혔다고 말만 하고 전체를 읽으면 안 된다 — 질의에 축이 실려야 한다."""
    fetch_all = _Recorder([_axis_row(BURN_IN), _axis_row(WALK)])

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
    ):
        get_finance_runtime_axis(sim_run_id=WALK)

    text, params = fetch_all.calls[0]
    assert "sim_run_id = %s" in text
    assert params == [WALK]


def test_the_burn_in_state_is_never_used_for_the_walk_run():
    """다른 실행의 상태를 이 실행의 잔액으로 쓰지 않는다."""
    fetch_all = _Recorder([_axis_row(BURN_IN), _axis_row(WALK)])

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
    ):
        axis = get_finance_runtime_axis(sim_run_id=WALK)

    assert axis["sim_run_id"] != BURN_IN


# ---------------------------------------------------------------------------
# Case B — 요청한 실행이 없으면 없는 것이다
# ---------------------------------------------------------------------------


def test_a_missing_sim_run_never_falls_back_to_another_one():
    """🔴 **가장 위험한 fallback.** 하나뿐이라고 그것을 집으면 남의 장부로 판단한다."""
    fetch_all = _Recorder([_axis_row(BURN_IN)])

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
        pytest.raises(LookupError),
    ):
        get_finance_runtime_axis(sim_run_id="SIM-NOT-EXISTS")


# ---------------------------------------------------------------------------
# Case C — 같은 실행 안에서 축이 둘이면 그때는 모호하다
# ---------------------------------------------------------------------------


def test_two_axes_inside_one_run_stay_ambiguous():
    """★ 이것이 진짜 모호함이다 — 같은 실행에 조달 방식이 둘."""
    fetch_all = _Recorder([_axis_row(WALK, "LOAN_BASELINE"), _axis_row(WALK, "BASE_NO_LOAN")])

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
        pytest.raises(FinanceDataNotReady) as raised,
    ):
        get_finance_runtime_axis(sim_run_id=WALK)

    assert raised.value.key == "finance_runtime_axis_ambiguous"


def test_two_runs_and_two_axes_are_told_apart():
    """다른 실행 둘 = 정상 · 같은 실행의 축 둘 = 비정상. 둘을 구별한다."""
    쪼개진_실행 = _Recorder(
        [
            _axis_row(BURN_IN, "LOAN_BASELINE"),
            _axis_row(WALK, "LOAN_BASELINE"),
            _axis_row(WALK, "BASE_NO_LOAN"),
        ]
    )

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", 쪼개진_실행),
    ):
        # 번인은 축이 하나라 정상이다.
        assert get_finance_runtime_axis(sim_run_id=BURN_IN)["sim_run_id"] == BURN_IN
        # 걷기는 자기 안에서 갈렸으므로 막힌다.
        with pytest.raises(FinanceDataNotReady):
            get_finance_runtime_axis(sim_run_id=WALK)


# ---------------------------------------------------------------------------
# Case D — as_of=None 무가드 조회 금지
# ---------------------------------------------------------------------------


def test_the_unscoped_current_row_never_picks_one_of_many_runs():
    """🔴 예전에는 `fetch_one` 이라 **아무 행이나** 집혔다.

    실행이 여럿일 때 그때 나오는 것은 오류가 아니라 **남의 실행 잔액**이다.
    터지는 편이 낫다.
    """
    fetch_all = _Recorder([_state_row(BURN_IN), _state_row(WALK)])

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
        pytest.raises(FinanceDataNotReady) as raised,
    ):
        _get_current_finance_state_row()

    assert raised.value.key == "finance_runtime_axis_ambiguous"


def test_the_current_row_can_be_scoped_to_one_run():
    fetch_all = _Recorder([_state_row(BURN_IN), _state_row(WALK)])

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
    ):
        row = _get_current_finance_state_row(sim_run_id=WALK)

    assert row["sim_run_id"] == WALK


def test_finance_db_no_longer_reads_the_current_state_with_fetch_one():
    """★ 무가드 경로가 되살아나면 여기서 걸린다."""
    import inspect

    source = inspect.getsource(finance_db._get_current_finance_state_row)

    assert "fetch_one(" not in source


# ---------------------------------------------------------------------------
# load_finance_state_row — 판단·전이 경로
# ---------------------------------------------------------------------------


def test_state_row_lookup_is_scoped_to_the_requested_run():
    axis_rows = [_axis_row(BURN_IN), _axis_row(WALK)]

    def fetch_all(query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        if "v_current_finance_state" in text:
            wanted = params[0]
            return [row for row in axis_rows if row["sim_run_id"] == wanted]
        # 상태 질의 — 축이 실제로 실려 왔는지 본다.
        assert params[0] == WALK, params
        return [_state_row(WALK)]

    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
    ):
        row = load_finance_state_row(AS_OF, sim_run_id=WALK)

    assert row["sim_run_id"] == WALK


# ---------------------------------------------------------------------------
# Case E — Day Opening
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows: list[object] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.conn.executed.append((text, list(params or [])))
        self.rows = []
        self.rowcount = 0
        if "v_current_finance_state" in text:
            if "sim_run_id = %s" in text:
                wanted = params[0]
                self.rows = [row for row in self.conn.axes if row[0] == wanted]
            else:
                self.rows = list(self.conn.axes)
        elif text.lstrip().startswith("SELECT finance_state_id"):
            self.rows = [
                (row["finance_state_id"],)
                for row in self.conn.states
                if row["sim_run_id"] == params["sim_run_id"]
                and row["financing_mode"] == params["financing_mode"]
                and row["state_date"] == params["state_date"]
            ][:2]
        elif "INSERT INTO" in text:
            source = next(
                (
                    row
                    for row in self.conn.states
                    if row["sim_run_id"] == params["sim_run_id"]
                    and row["financing_mode"] == params["financing_mode"]
                    and row["state_date"] == params["carry_from"]
                ),
                None,
            )
            if source is not None:
                carried = dict(source)
                carried.update(
                    finance_state_id=params["finance_state_id"],
                    state_date=params["as_of"],
                )
                self.conn.states.append(carried)
                self.rowcount = 1
        else:
            raise AssertionError(text)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, axes, states):
        self.axes = axes
        self.states = [dict(row) for row in states]
        self.executed: list[tuple[str, list[object]]] = []

    def cursor(self):
        return _Cursor(self)


@pytest.fixture
def _inventory():
    from app.finance.db import InventorySnapshot

    with patch(
        "app.finance.day_open.load_inventory_snapshot_as_of",
        return_value=InventorySnapshot(Decimal(1), Decimal(2), Decimal(2)),
    ), patch("app.finance.day_open.get_db_schema", return_value="haetdeul"):
        yield


def test_day_opening_carries_only_the_requested_run(_inventory):
    """🔴 실 장애가 난 자리. 두 실행이 공존해도 걷기 실행만 이어야 한다."""
    carry_from = date(2026, 1, 4)
    conn = _Conn(
        axes=[(BURN_IN, MODE), (WALK, MODE)],
        states=[
            _state_row(BURN_IN, state_date=carry_from),
            _state_row(WALK, state_date=carry_from),
        ],
    )

    FinanceDayOpening(sim_run_id=WALK).open_day(conn, as_of=AS_OF, carry_from=carry_from)

    새로_선_행 = [row for row in conn.states if row["state_date"] == AS_OF]
    assert [row["sim_run_id"] for row in 새로_선_행] == [WALK]


def test_day_opening_does_not_block_just_because_another_run_exists(_inventory):
    carry_from = date(2026, 1, 4)
    conn = _Conn(
        axes=[(BURN_IN, MODE), (WALK, MODE)],
        states=[_state_row(WALK, state_date=carry_from)],
    )

    # 예전에는 여기서 finance_runtime_axis_ambiguous 가 났다.
    assert FinanceDayOpening(sim_run_id=WALK).is_open(conn, as_of=carry_from) is True


def test_day_opening_never_carries_another_run_state(_inventory):
    """번인 상태를 걷기의 전날 상태로 쓰면 실패다."""
    carry_from = date(2026, 1, 4)
    conn = _Conn(
        axes=[(BURN_IN, MODE), (WALK, MODE)],
        states=[_state_row(BURN_IN, state_date=carry_from)],
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        FinanceDayOpening(sim_run_id=WALK).open_day(
            conn, as_of=AS_OF, carry_from=carry_from
        )

    assert raised.value.key == "historical_finance_position"
    assert not [row for row in conn.states if row["sim_run_id"] == WALK]


def test_day_opening_query_carries_the_run(_inventory):
    conn = _Conn(axes=[(BURN_IN, MODE), (WALK, MODE)], states=[])

    with pytest.raises(FinanceDataNotReady):
        FinanceDayOpening(sim_run_id=WALK).open_day(
            conn, as_of=AS_OF, carry_from=date(2026, 1, 4)
        )

    축_질의 = [
        (text, params)
        for text, params in conn.executed
        if "v_current_finance_state" in text
    ]
    assert 축_질의, "축을 읽지 않았다"
    assert all("sim_run_id = %s" in text for text, _ in 축_질의)
    assert all(params == [WALK] for _, params in 축_질의)
