"""마감이 **출발 채권과 실행 중 발생분을 분리해서** 대조한다.

🔴 이 검사들이 없을 때 걷기 206일의 마감이 **0건**이었다. 시작을 물려받은 실행은
   `receivables` 표가 **0행**인데 `finance_states.receivables_krw` 에는 물려받은
   21,922,555원이 들어 있어, 표 하나만 세는 대조가 매일 어긋났다.

★ 재무가 확정해 준 기준 여섯 (원문).

  ```text
  ① `FIN-DAY30-LOAN` 의 `receivables_krw` 는 그 실행의 Opening AR Carry 로 유지
  ② 지금 번인 `receivables` 행을 그대로 이관하지 않는다
     — 12/31 이후 수금분이 이미 반영돼 있어 1/1 출발점으로 가져오면 시간축이 뒤집힌다
  ③ 없는 과거 수금 사건이나 채권별 12/31 잔액을 임의로 복원하지 않는다
  ④ 마감에서는
       Opening AR Carry + 그 Walk 에서 발행되어 남아 있는 receivables
     와 `finance_states.receivables_krw` 를 대조한다
  ⑤ 기존 `receivables_balance_mismatch` fail-closed 원칙은 유지
  ⑥ Opening AR 은 근거가 없으므로 개별 수금 가능한 채권으로 지어내지 않는다
  ```

★★ *"대조를 없애는 것이 아니라 **Opening Balance 와 실행 중 발생분을 분리해서**
   대조하는 방식"* — 재무의 말 그대로다.

⚠️ 전부 **대역**이다. 실 DB 를 타지 않는다.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import closing
from app.finance.db import FinanceDataNotReady, InventorySnapshot

WALK_RUN_ID = "SIM-WALK-2026"
BURNIN_RUN_ID = "SIM-BURNIN-202512"
BASELINE_STATE_ID = "FIN-DAY30-LOAN"
LOAN_MODE = "LOAN_BASELINE"

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 7, 31)
AS_OF = date(2026, 1, 1)

#: 실측 — 번인 마지막 상태가 들고 있던 채권 잔액. 이것이 걷기의 Opening AR Carry 다.
MEASURED_CARRY = Decimal(21_922_555)


class _Cursor:
    """`_load_closing_facts` 가 실제로 던지는 조회만 흉내낸다.

    🔴 `receivables` 표는 **읽기 전용**이다. 쓰기 문장이 오면 그대로 기록해 두고,
      검사가 *"한 행도 만들지 않았다"* 를 그 기록으로 확인한다.
    """

    def __init__(self, conn):
        self.conn = conn
        self.rows: list = []
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

        if "sim_runs" in text:
            self.rows = [(PERIOD_START, PERIOD_END, LOAN_MODE, self.conn.config_json)]
        elif ".finance_states" in text and "finance_state_id = %s" in text:
            self.rows = [
                {
                    "sim_run_id": row["sim_run_id"],
                    "financing_mode": row["financing_mode"],
                    "current_cash_krw": row["current_cash_krw"],
                    "receivables_krw": row["receivables_krw"],
                    "current_debt_krw": row["current_debt_krw"],
                }
                for row in self.conn.baseline_states
                if row["finance_state_id"] == params[0]
            ]
        elif ".finance_states" in text and "state_date = %s" in text:
            self.rows = [
                {
                    "financing_mode": LOAN_MODE,
                    "current_cash_krw": self.conn.cash,
                    "receivables_krw": self.conn.state_receivables,
                    "current_debt_krw": self.conn.debt,
                }
            ]
        elif ".finance_states" in text and "state_date < %s" in text:
            self.rows = []
        elif "SUM(original_amount_krw)" in text:
            self.rows = [{"amount": self.conn.issued_receivables}]
        elif "SUM(outstanding_amount_krw)" in text:
            # 🟢 실행축으로 걸러서 센다 — 남의 실행 채권은 이 합에 들어오지 않는다.
            self.conn.outstanding_params.append(params)
            self.rows = [{"amount": self.conn.outstanding_by_run.get(params[0], Decimal(0))}]
        elif ".payables" in text or ".expenses" in text:
            self.rows = []
        elif "SUM(total_amount_krw)" in text:
            self.rows = [{"amount": Decimal(0)}]
        elif "INSERT INTO" in text and "daily_closings" in text:
            self.conn.closings[(params["sim_run_id"], params["close_date"])] = dict(params)
            self.rowcount = 1
        elif "UPDATE" in text and "daily_closings" in text:
            self.conn.closings[(params["sim_run_id"], params["close_date"])].update(params)
            self.rowcount = 1
        else:
            raise AssertionError(text)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(
        self,
        *,
        state_receivables: Decimal,
        outstanding_by_run: dict[str, Decimal],
        config_json: dict | None = None,
        baseline_states=(),
        issued_receivables: Decimal = Decimal(0),
        cash: Decimal = Decimal(60_000_000),
        debt: Decimal = Decimal(0),
    ):
        self.state_receivables = state_receivables
        self.outstanding_by_run = dict(outstanding_by_run)
        self.config_json = {} if config_json is None else config_json
        self.baseline_states = [deepcopy(row) for row in baseline_states]
        self.issued_receivables = issued_receivables
        self.cash = cash
        self.debt = debt
        self.closings: dict = {}
        self.executed: list = []
        self.outstanding_params: list = []

    def cursor(self):
        return _Cursor(self)


def _baseline_row(*, receivables: Decimal, sim_run_id: str = BURNIN_RUN_ID):
    return {
        "finance_state_id": BASELINE_STATE_ID,
        "sim_run_id": sim_run_id,
        "financing_mode": LOAN_MODE,
        "current_cash_krw": Decimal(50_000_000),
        "receivables_krw": receivables,
        "current_debt_krw": Decimal(0),
    }


def _carry_config(from_sim_run_id: str = BURNIN_RUN_ID):
    return {
        "baseline": {
            "finance_state_id": BASELINE_STATE_ID,
            "from_sim_run_id": from_sim_run_id,
        }
    }


def _walk_conn(
    *,
    carry: Decimal = MEASURED_CARRY,
    in_run_outstanding: Decimal = Decimal(0),
    state_receivables: Decimal | None = None,
    issued: Decimal | None = None,
    outstanding_by_run: dict[str, Decimal] | None = None,
):
    """출발 채권을 물려받은 걷기 하루.

    ★ `issued` 를 안 주면 **그날 발행한 만큼이 그대로 남아 있는** 하루가 된다. 남아
      있는 분보다 적게 발행한 날은 있을 수 없어서, 그런 하루는 `_collection_delta` 가
      먼저 막는다 — 이 파일이 보려는 것은 그 가드가 아니다.
    """
    return _Conn(
        state_receivables=(
            carry + in_run_outstanding if state_receivables is None else state_receivables
        ),
        outstanding_by_run=(
            {WALK_RUN_ID: in_run_outstanding}
            if outstanding_by_run is None
            else outstanding_by_run
        ),
        config_json=_carry_config(),
        baseline_states=[_baseline_row(receivables=carry)],
        issued_receivables=in_run_outstanding if issued is None else issued,
    )


def _close(conn, *, as_of=AS_OF, sim_run_id=WALK_RUN_ID):
    return closing.FinanceDayClosing().close(conn, as_of=as_of, sim_run_id=sim_run_id)


def _row(conn, *, as_of=AS_OF, sim_run_id=WALK_RUN_ID):
    return conn.closings[(sim_run_id, as_of)]


@pytest.fixture(autouse=True)
def _schema():
    snapshot = InventorySnapshot(Decimal(123), Decimal(456), Decimal(456))
    with (
        patch("app.finance.closing.get_db_schema", return_value="haetdeul"),
        patch("app.finance.closing.load_inventory_snapshot_as_of", return_value=snapshot),
    ):
        yield


# ---------------------------------------------------------------------------
# A ─ baseline 이 있으면 Opening AR Carry + 실행 발행 잔여로 대조한다
# ---------------------------------------------------------------------------


def test_the_inherited_opening_balance_is_counted_alongside_the_walk_receivables():
    """🔴 **이 파일에서 가장 중요한 검사.**

    걷기 첫날은 `receivables` 가 0행이고 상태에는 물려받은 21,922,555 가 있다. 출발분을
    빼고 표만 세면 `0 != 21,922,555` 로 어긋나 **그날 마감이 통째로 사라진다** — 실측
    206일이 그렇게 0건이 됐다.
    """
    conn = _walk_conn(carry=MEASURED_CARRY, in_run_outstanding=Decimal(0))

    result = _close(conn)

    assert result.status == "CLOSED"
    assert _row(conn)["receivables_balance_krw"] == MEASURED_CARRY


def test_receivables_issued_during_the_walk_are_added_on_top_of_the_carry():
    """출발분은 그대로 있고, 그 위에 이 실행에서 발행돼 남은 분이 얹힌다."""
    conn = _walk_conn(carry=MEASURED_CARRY, in_run_outstanding=Decimal(3_000_000))

    _close(conn)

    assert _row(conn)["receivables_balance_krw"] == MEASURED_CARRY + Decimal(3_000_000)


def test_the_carry_is_read_from_the_baseline_state_row_not_from_config_json():
    """★ 숫자의 주인은 `finance_states` 원본 행 하나다.

    `config_json` 에 채권 숫자가 섞여 있어도 쓰지 않는다 — 복제한 숫자는 원본이 바뀌는
    날 조용히 갈린다.
    """
    conn = _walk_conn(carry=MEASURED_CARRY)
    conn.config_json["baseline"]["receivables_krw"] = 888_888_888

    _close(conn)

    assert _row(conn)["receivables_balance_krw"] == MEASURED_CARRY


def test_the_carry_is_still_applied_after_the_first_day():
    """🔴 출발분은 **첫날만의 것이 아니다.**

    둘째 날부터 carry 를 빼면 걷기 2일차부터 다시 전부 막힌다 — 고친 것이 하루뿐이면
    206일 중 205일이 그대로 0건이다.
    """
    conn = _walk_conn(carry=MEASURED_CARRY, in_run_outstanding=Decimal(500_000))
    day2 = date(2026, 1, 2)

    result = _close(conn, as_of=day2)

    assert result.status == "CLOSED"
    assert _row(conn, as_of=day2)["receivables_balance_krw"] == MEASURED_CARRY + Decimal(
        500_000
    )


# ---------------------------------------------------------------------------
# B ─ baseline 선언이 없으면 carry = 0 이고 식이 종전과 같다 (번인 회귀)
# ---------------------------------------------------------------------------


def test_a_run_without_a_baseline_declaration_carries_nothing():
    """🔴 **번인 회귀.** `SIM-BURNIN-202512` 는 `config_json.baseline` 이 없다.

    선언이 없으면 carry 는 0 이고 대조식은 글자 그대로 종전과 같다 — 이미 통과해 있는
    번인 30행이 이 변경으로 흔들리지 않는다.
    """
    conn = _Conn(
        state_receivables=Decimal(700),
        outstanding_by_run={BURNIN_RUN_ID: Decimal(700)},
        issued_receivables=Decimal(700),
    )

    result = _close(conn, sim_run_id=BURNIN_RUN_ID)

    assert result.status == "CLOSED"
    assert _row(conn, sim_run_id=BURNIN_RUN_ID)["receivables_balance_krw"] == Decimal(700)


def test_a_run_without_a_baseline_never_reads_a_baseline_state_row():
    """선언이 없는 실행은 물려받을 것이 없으므로 그 행을 찾지도 않는다."""
    conn = _Conn(
        state_receivables=Decimal(700),
        outstanding_by_run={BURNIN_RUN_ID: Decimal(700)},
        issued_receivables=Decimal(700),
    )

    _close(conn, sim_run_id=BURNIN_RUN_ID)

    assert not any("finance_state_id = %s" in text for text, _ in conn.executed)


def test_a_run_without_a_baseline_still_blocks_when_the_ledger_disagrees():
    """★ carry 가 0 이어도 **fail-closed 는 그대로다** — 번인 쪽 계약이 느슨해지지 않는다."""
    conn = _Conn(
        state_receivables=Decimal(700),
        outstanding_by_run={BURNIN_RUN_ID: Decimal(500)},
        issued_receivables=Decimal(700),
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        _close(conn, sim_run_id=BURNIN_RUN_ID)

    assert raised.value.key == "receivables_balance_mismatch"
    assert not conn.closings


# ---------------------------------------------------------------------------
# C ─ 대조가 틀리면 여전히 막는다 (재무 확정 기준 ⑤)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state_receivables", "note"),
    [
        pytest.param(MEASURED_CARRY + Decimal(1), "carry 보다 1원 많다", id="one-won-over"),
        pytest.param(MEASURED_CARRY - Decimal(1), "carry 보다 1원 적다", id="one-won-under"),
        pytest.param(Decimal(0), "출발분이 통째로 빠졌다", id="carry-dropped"),
    ],
)
def test_a_mismatch_still_blocks_the_close(state_receivables, note):
    """🔴 **대조를 없앤 것이 아니다.** 왼쪽을 둘로 나눴을 뿐이고, 어긋나면 종전대로 막는다.

    분리 대조가 *"어차피 맞춰지는 식"* 이 되면 그 순간 이 가드는 장식이 된다.
    """
    conn = _walk_conn(
        carry=MEASURED_CARRY,
        in_run_outstanding=Decimal(0),
        state_receivables=state_receivables,
        issued=Decimal(1),
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        _close(conn)

    assert raised.value.key == "receivables_balance_mismatch", note
    assert not conn.closings


def test_the_walk_portion_alone_no_longer_satisfies_the_check():
    """출발분을 물려받은 실행에서 표 합만 맞아떨어지는 것은 **맞은 것이 아니다.**"""
    conn = _walk_conn(
        carry=MEASURED_CARRY,
        in_run_outstanding=Decimal(3_000_000),
        state_receivables=Decimal(3_000_000),
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        _close(conn)

    assert raised.value.key == "receivables_balance_mismatch"


# ---------------------------------------------------------------------------
# D ─ Opening AR 로 채권 행을 만들지 않는다 (재무 확정 기준 ③ · ⑥)
# ---------------------------------------------------------------------------


def test_the_carry_never_becomes_a_receivable_row():
    """🔴 재무 확정 기준 ⑥ — *"Opening AR 은 근거가 없으므로 개별 수금 가능한 채권으로
      지어내지 않는다."*

    ★ 채권별 12/31 잔액이 남아 있지 않다. 합계 하나를 행으로 쪼개면 **없는 만기와 없는
      거래처**를 지어내는 것이고, 그 행들은 이후 수금 일정에 그대로 섞여 든다.
    """
    conn = _walk_conn(carry=MEASURED_CARRY, in_run_outstanding=Decimal(3_000_000))

    _close(conn)

    # `\b` 로 표 이름만 잡는다 — `receivables_balance_krw` 는 마감 표의 칸이지 채권 표가 아니다.
    writes = [
        text
        for text, _ in conn.executed
        if re.search(r"\breceivables\b", text)
        and re.search(r"\b(INSERT|UPDATE|DELETE)\b", text)
    ]
    assert writes == [], f"채권 표에 쓰는 문장이 나갔다: {writes}"


def test_the_carry_does_not_reconstruct_past_collection_events():
    """재무 확정 기준 ③ — 없는 과거 수금 사건을 복원하지 않는다.

    ★ 물려받은 덩어리는 수금되지 않으므로, 그날 잡히는 수금은 전부 **이 실행에서 발행된**
      채권에서만 나온다. 출발분이 얼마든 그 값은 달라지지 않는다.
    """
    conn = _walk_conn(
        carry=MEASURED_CARRY,
        in_run_outstanding=Decimal(1_500_000),
        issued=Decimal(2_000_000),
    )

    _close(conn)

    assert _row(conn)["collection_cash_in_krw"] == Decimal(500_000)


# ---------------------------------------------------------------------------
# E ─ 다른 실행의 receivables 를 안 센다
# ---------------------------------------------------------------------------


def test_another_runs_receivables_are_not_counted():
    """🟢 표 합은 **이 실행축**으로만 걸러 센다.

    출발점을 준 번인의 채권 행이 아직 표에 남아 있어도 걷기의 합에 들어오면 안 된다 —
    그것이 재무 확정 기준 ② 가 막는 *"시간축이 뒤집힌다"* 다.
    """
    conn = _walk_conn(
        carry=MEASURED_CARRY,
        in_run_outstanding=Decimal(3_000_000),
        outstanding_by_run={
            WALK_RUN_ID: Decimal(3_000_000),
            BURNIN_RUN_ID: Decimal(13_067_455),
        },
    )

    _close(conn)

    assert _row(conn)["receivables_balance_krw"] == MEASURED_CARRY + Decimal(3_000_000)
    assert [params[0] for params in conn.outstanding_params] == [WALK_RUN_ID]


def test_a_baseline_pointing_at_another_run_still_blocks():
    """계보가 어긋난 포인터는 **에러 없이 숫자만 바꾼다** — 그래서 carry 도 그 가드 뒤에 있다."""
    conn = _Conn(
        state_receivables=MEASURED_CARRY,
        outstanding_by_run={WALK_RUN_ID: Decimal(0)},
        config_json=_carry_config(),
        baseline_states=[_baseline_row(receivables=MEASURED_CARRY, sim_run_id="SIM-SOMEONE-ELSE")],
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        _close(conn)

    assert raised.value.key == "baseline_finance_state_invalid"
    assert not conn.closings
