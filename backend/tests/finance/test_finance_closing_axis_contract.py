"""하루 넘김과 일 마감이 **같은 축을 말하고 있는가.**

🔴 지금은 아니다. 재무 안에서 두 파일이 서로 다른 것을 요구한다.

    ```text
    FinanceDayOpening   실행축 하나만 전진시킨다 (v_current_finance_state)
    FinanceDayClosing   BASE_NO_LOAN 과 LOAN_BASELINE 을 같은 날 둘 다 요구한다
    ```

  그래서 하루를 정상적으로 연 다음 그날을 닫으려 하면 `base_finance_state` 로 막힌다.
  실 DB 에서 2025-12-02~12-30 이 전부 이 사유로 막힌다.

★ **이 파일은 결함을 고치지 않는다. 경계를 고정할 뿐이다.**

  `base_cash_balance_krw` 는 schema 가 *"대출 없는 Base 시나리오 현금잔액"* 이라고
  적어 둔 **반사실 값**이고 `NOT NULL` 이다. 실행축 하나로는 그 값을 만들 수 없다 —
  대출이 없었다면 애초에 같은 매입을 못 했을 것이므로 *"같은 궤적에서 원금만 뺀다"* 도
  사실이 아니다. 없는 반사실을 재무가 발명하면 그 순간 마감이 **틀린 값을 확정한다.**

  그래서 지금의 fail-closed 는 옳은 동작이고, 여기서는 그것이 **왜** 그런지를
  실행 가능한 형태로 남긴다. 계약이 정해지면 이 파일이 먼저 바뀐다.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import closing
from app.finance.closing import _load_exact_states
from app.finance.day_open import FinanceDayOpening
from app.finance.db import FinanceDataNotReady, InventorySnapshot

CARRY_FROM = date(2026, 1, 5)
AS_OF = date(2026, 1, 6)
SIM_RUN_ID = "SIM-BURNIN-202512"
LOAN_MODE = "LOAN_BASELINE"
BASE_MODE = "BASE_NO_LOAN"

#: 실 원장의 모양 그대로 — 실행축 한 건. `BASE_NO_LOAN` 은 그날 없다.
LOAN_STATE = {
    "finance_state_id": "FIN-PROOF-20260105-LOAN",
    "sim_run_id": SIM_RUN_ID,
    "state_date": CARRY_FROM,
    "state_type": "TRANSITION_PROOF_T0",
    "financing_mode": LOAN_MODE,
    "current_cash_krw": Decimal(32_000_000),
    "minimum_operating_cash_krw": Decimal(15_902_640),
    "committed_outflows_krw": Decimal(150_000),
    "unsettled_purchase_payables_krw": Decimal(4_500_000),
    "receivables_krw": Decimal("73051531.25"),
    "inventory_book_value_krw": Decimal(3_100_000),
    "operational_inventory_value_krw": Decimal(2_900_000),
    "current_debt_krw": Decimal("45272104.184486"),
    "recommended_loan_amount_krw": Decimal(12_000_000),
    "note": "proof fixture",
}


class _Cursor:
    """하루 넘김과 마감이 **같은 가짜 원장**을 보게 한다."""

    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.conn.executed.append((text, params))
        self.rows = []
        self.rowcount = 0
        if "SELECT DISTINCT sim_run_id" in text:
            self.rows = list(self.conn.axes)
        elif text.lstrip().startswith("SELECT finance_state_id"):
            self.rows = [
                (row["finance_state_id"],)
                for row in self.conn.states
                if row["sim_run_id"] == params["sim_run_id"]
                and row["financing_mode"] == params["financing_mode"]
                and row["state_date"] == params["state_date"]
            ][:2]
        elif "INSERT INTO" in text and "finance_states" in text:
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
            already = any(
                row["finance_state_id"] == params["finance_state_id"]
                for row in self.conn.states
            )
            if source is not None and not already:
                carried = deepcopy(source)
                carried.update(
                    finance_state_id=params["finance_state_id"],
                    state_date=params["as_of"],
                    state_type=params["state_type"],
                    inventory_book_value_krw=params["inventory_book_value_krw"],
                    operational_inventory_value_krw=params[
                        "operational_inventory_value_krw"
                    ],
                    note=params["note"],
                )
                self.conn.states.append(carried)
                self.rowcount = 1
        elif ".finance_states" in text and "state_date =" in text:
            # 마감이 그날의 두 축을 한꺼번에 묻는 자리.
            self.rows = [
                {
                    "financing_mode": row["financing_mode"],
                    "current_cash_krw": row["current_cash_krw"],
                    "receivables_krw": row["receivables_krw"],
                    "current_debt_krw": row["current_debt_krw"],
                }
                for row in self.conn.states
                if row["sim_run_id"] == params[0] and row["state_date"] == params[1]
            ]
        elif "sim_runs" in text:
            self.rows = [(date(2026, 1, 1), date(2026, 1, 31))]
        else:
            raise AssertionError(text)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, states=(), axes=((SIM_RUN_ID, LOAN_MODE),)):
        self.states = [deepcopy(row) for row in states]
        self.axes = list(axes)
        self.executed = []

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _schema():
    snapshot = InventorySnapshot(Decimal("123"), Decimal("456"), Decimal("456"))
    with (
        patch("app.finance.day_open.get_db_schema", return_value="haetdeul"),
        patch("app.finance.closing.get_db_schema", return_value="haetdeul"),
        patch(
            "app.finance.day_open.load_inventory_snapshot_as_of",
            return_value=snapshot,
        ),
        patch(
            "app.finance.closing.load_inventory_snapshot_as_of",
            return_value=snapshot,
        ),
    ):
        yield


def _modes(conn, state_date):
    return sorted(
        row["financing_mode"] for row in conn.states if row["state_date"] == state_date
    )


# ---------------------------------------------------------------------------
# 하루 넘김 — 실행축 하나만 전진한다
# ---------------------------------------------------------------------------


def test_day_opening_advances_only_the_active_runtime_axis():
    """★ 실행축은 `v_current_finance_state` 가 정한다 — 재무가 고르지 않는다."""
    conn = _Conn([LOAN_STATE])

    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    assert _modes(conn, AS_OF) == [LOAN_MODE]


def test_day_opening_does_not_create_the_comparison_axis():
    """🔴 하루를 열어도 `BASE_NO_LOAN` 은 생기지 않는다 — 만들 근거가 없다."""
    conn = _Conn([LOAN_STATE])

    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    assert BASE_MODE not in _modes(conn, AS_OF)


def test_day_opening_carries_the_base_axis_when_that_is_the_runtime_axis():
    """★ 재무는 축 이름을 해석하지 않는다 — 실행축이 무엇이든 그것 하나를 잇는다."""
    base_state = dict(
        LOAN_STATE,
        finance_state_id="FIN-PROOF-20260105-BASE",
        financing_mode=BASE_MODE,
        current_debt_krw=Decimal(0),
    )
    conn = _Conn([base_state], axes=((SIM_RUN_ID, BASE_MODE),))

    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    assert _modes(conn, AS_OF) == [BASE_MODE]


# ---------------------------------------------------------------------------
# 일 마감 — 같은 날 두 축을 요구한다
# ---------------------------------------------------------------------------


def test_closing_requires_both_axes_on_the_close_date():
    conn = _Conn([LOAN_STATE])
    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    states = _load_exact_states(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert set(states) == {LOAN_MODE}
    assert closing._BASE_MODE == BASE_MODE
    assert BASE_MODE not in states


def test_opened_day_cannot_be_closed_because_the_comparison_axis_is_missing():
    """🔴 **이 파일의 이유.** 정상적으로 연 하루가 그날 닫히지 않는다.

    실 DB 에서 2025-12-02~12-30 이 전부 이 사유로 막힌다 (실측).
    """
    conn = _Conn([LOAN_STATE])
    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    with pytest.raises(FinanceDataNotReady) as raised:
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert raised.value.key == "base_finance_state"


def test_the_block_is_fail_closed_and_writes_nothing():
    """★ 막히는 것은 옳다 — 없는 반사실 잔액을 지어내 확정하지 않는다."""
    conn = _Conn([LOAN_STATE])
    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    with pytest.raises(FinanceDataNotReady):
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert not any(
        "daily_closings" in text for text, _ in conn.executed
    ), "판정을 못 낸 하루가 마감 행을 남겼다"


def test_closing_never_substitutes_the_loan_axis_for_the_base_axis():
    """🔴 대출 잔액을 무차입 칸에 넣지 않는다.

    `base_cash_balance_krw` 는 schema 가 *"대출 없는 Base 시나리오 현금잔액"* 이라고
    적어 둔 값이다. 실행축 값을 그 자리에 넣으면 **대출을 쓴 잔액이 대출 없는 잔액**
    으로 기록되고, 그 거짓은 에러 없이 숫자만 바꾼다.
    """
    conn = _Conn([LOAN_STATE])
    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    with pytest.raises(FinanceDataNotReady):
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)


def test_closing_succeeds_once_both_axes_exist_on_the_day():
    """★ 두 축이 실제로 있으면 마감은 이미 돈다 — 막고 있는 것은 **자료**다."""
    base_today = dict(
        LOAN_STATE,
        finance_state_id="FIN-BASE-20260106",
        financing_mode=BASE_MODE,
        state_date=AS_OF,
        current_cash_krw=Decimal(20_000_000),
        current_debt_krw=Decimal(0),
    )
    conn = _Conn([LOAN_STATE, base_today])
    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    states = _load_exact_states(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert set(states) == {BASE_MODE, LOAN_MODE}
    assert states[BASE_MODE].current_cash_krw == Decimal(20_000_000)
    assert states[LOAN_MODE].current_cash_krw == Decimal(32_000_000)
