"""승인 약정 → 재무 전이. **계산과 쓰기의 경계를 지킨다.**

`app/finance/transition.py` 는 운영 모듈인데 시험이 하나도 없었다. 여기서 보는 것은
지금 계약으로 확정된 것들뿐이다 — Master `ArrivalLeg.amount_krw` 와 회차별
`purchase_id` 를 그대로 받아 원장에 옮긴다. 재무는 회차 금액을 배분하지 않는다.

★ DB 를 타지 않는다. 상태·정책 조회는 갈아 끼우고, `persist` 는 가짜 커넥션으로
  **무엇을 불렀고 무엇을 안 불렀는지**만 본다.
"""

import inspect
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.db import FinanceDataNotReady
from app.finance.transition import (
    H1_STATE_TYPE,
    FinanceTransitionAdapter,
    build_finance_transition,
    persist_finance_transition,
)

_MODULE = "app.finance.transition"

WED = date(2025, 12, 31)  # 수요일
THU = date(2026, 1, 1)
SAT = date(2026, 1, 3)
SUN = date(2026, 1, 4)
SIM_RUN_ID = "SIM-BURNIN-202512"

_STATE = {
    "finance_state_id": "FIN-DAY30-LOAN",
    "sim_run_id": SIM_RUN_ID,
    "state_date": WED,
    "state_type": "DAY30",
    "financing_mode": "LOAN_BASELINE",
    "current_cash_krw": Decimal("31993913.77"),
    "minimum_operating_cash_krw": Decimal(15902640),
    "committed_outflows_krw": Decimal(0),
    "unsettled_purchase_payables_krw": Decimal(0),
    "receivables_krw": Decimal("73051531.25"),
    "current_debt_krw": Decimal("45272104.184486"),
    "financial_limit_krw": Decimal("16091273.77"),
}


class _Leg:
    """`ArrivalLeg` 중 재무가 읽는 칸만."""

    def __init__(self, seq: int, purchase_date: date, amount_krw: float | None = None):
        self.seq = seq
        self.purchase_date = purchase_date
        self.amount_krw = amount_krw


class _Commitment:
    """회차는 `(seq, 매입일)` 로 준다 — 마스터가 실어 주는 모양 그대로다."""

    def __init__(
        self,
        *,
        approval_id: str = "H1-REQ-1-1",
        as_of: date = WED,
        legs=(),
        amount: float = 4_500_000.0,
    ):
        self.approval_id = approval_id
        self.as_of = as_of
        self.total_amount_krw = amount
        self.arrival_schedule = tuple(_Leg(*leg) for leg in legs)


class _Policy:
    """N5 는 달력일수다. 현재 계약값은 0(매입 당일)."""

    def __init__(self, payment_days: int | None = 0):
        self.purchase_payment_days = payment_days


_PURCHASE_IDS = {1: "PUR-H1-REQ-1-1-S1"}


def _build(commitment, *, purchase_ids=None, target=THU, policy=None, via_adapter=False):
    """마스터가 부르는 모양 그대로 부른다 — 키워드 이름까지 같다."""
    # 상태 행은 승인일에 서 있는 것으로 둔다 — 신선도 게이트는 별도 시험이 본다.
    state = dict(_STATE, state_date=commitment.as_of)
    call = FinanceTransitionAdapter().build if via_adapter else build_finance_transition
    with (
        patch(f"{_MODULE}.load_finance_state_row", return_value=state),
        patch(f"{_MODULE}.get_active_finance_policy", return_value=policy or _Policy()),
    ):
        return call(
            commitment,
            target_state_date=target,
            purchase_ids=_PURCHASE_IDS if purchase_ids is None else purchase_ids,
        )


# ---------------------------------------------------------------------------
# 가짜 커넥션 — **무엇을 안 불렀는지**가 요점이다
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, log, rowcount):
        self._log = log
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._log.append((query, params))


class _Conn:
    """`persist` 가 받은 연결. commit·rollback·close 를 부르면 여기 남는다."""

    def __init__(self, rowcount=1):
        self.executed: list = []
        self.calls: list[str] = []
        self.cursors = 0
        self._rowcount = rowcount

    def cursor(self):
        self.cursors += 1
        return _Cursor(self.executed, self._rowcount)

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


def _persist(transition, *, rowcount=1):
    conn = _Conn(rowcount)
    with patch(f"{_MODULE}.get_db_schema", return_value="haetdeul"):
        written = persist_finance_transition(conn, transition)
    return conn, written


class _LedgerCursor:
    """Payable 신규 INSERT 여부와 일별 state 누적만 모사한다."""

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = query.as_string(None)
        self.conn.executed.append((text, params))
        if ".payables" in text:
            purchase_id = params[2]
            if purchase_id in self.conn.payables:
                self.rowcount = 0
            else:
                self.conn.payables[purchase_id] = Decimal(str(params[5]))
                self.rowcount = 1
            return
        if ".finance_states" in text:
            state_id = params[0]
            target_date = params[1]
            delta = Decimal(str(params[3]))
            key = (SIM_RUN_ID, "LOAN_BASELINE", target_date)
            if key not in self.conn.states:
                self.conn.states[key] = {
                    "finance_state_id": state_id,
                    "current_cash_krw": _STATE["current_cash_krw"],
                    "receivables_krw": _STATE["receivables_krw"],
                    "unsettled_purchase_payables_krw": (
                        _STATE["unsettled_purchase_payables_krw"] + delta
                    ),
                }
            else:
                self.conn.states[key]["unsettled_purchase_payables_krw"] += delta
            self.rowcount = 1


class _LedgerConn:
    def __init__(self):
        self.payables = {}
        self.states = {}
        self.executed = []
        self.calls = []

    def cursor(self):
        return _LedgerCursor(self)

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


def _persist_to_ledger(conn, transition):
    with patch(f"{_MODULE}.get_db_schema", return_value="haetdeul"):
        return persist_finance_transition(conn, transition)


# ---------------------------------------------------------------------------
# build — 한 회차 (지금 계약으로 만들 수 있는 유일한 모양)
# ---------------------------------------------------------------------------


def test_single_leg_makes_one_payable_from_authoritative_values():
    """A·D 회차 하나면 채무 하나. 금액은 약정 총액 그대로다."""
    transition = _build(_Commitment(legs=[(1, WED)]))

    assert len(transition.payables) == 1
    payable = transition.payables[0]
    assert payable.payable_id == "AP-H1-REQ-1-1-S1"
    assert payable.purchase_id == _PURCHASE_IDS[1]
    assert payable.issued_date == WED
    assert payable.amount_krw == Decimal("4500000.0")
    assert transition.payable_total_krw == Decimal(str(_Commitment().total_amount_krw))
    assert transition.next_finance_state_id == ("FIN-DAY-SIM-BURNIN-202512-LOAN_BASELINE-20260101")


def test_due_date_is_purchase_date_plus_calendar_n5():
    """계약 만기일 = 매입일 + N5 **달력일**. 주말로 밀지 않는다."""
    friday = date(2026, 1, 2)
    transition = _build(_Commitment(legs=[(1, friday)]), policy=_Policy(1))

    # 금요일 + 1 달력일 = 토요일. 원장은 계약일을 그대로 든다.
    assert transition.payables[0].due_date == SAT
    assert transition.payables[0].due_date.isoweekday() == 6


def test_current_n5_zero_makes_due_date_equal_purchase_date():
    monday = date(2026, 1, 5)
    transition = _build(_Commitment(legs=[(1, monday)]), policy=_Policy(0))

    assert transition.payables[0].due_date == monday


def test_master_purchase_due_date_and_finance_payable_due_date_match():
    """같은 회차의 Master 구매원장 날짜와 Finance 채무 날짜는 같은 N5 식이다."""
    from app.master.commitment import build_commitment

    purchase_date = date(2026, 1, 2)
    n5 = 2
    due_date = purchase_date + timedelta(days=n5)
    commitment = build_commitment(
        request_id="REQ-1",
        as_of=WED,
        item="배추",
        scenario={
            "label": "기본",
            "total_qty_kg": 100,
            "total_amount_krw": 4_500_000,
            "split_plan": [
                {
                    "seq": 1,
                    "date": purchase_date.isoformat(),
                    "qty_kg": 100,
                    "expected_arrival_date": "2026-01-05",
                    "amount_krw": 4_500_000,
                }
            ],
        },
        inbound_lead_days=3,
        decision_seq=1,
        purchase_payment_days=n5,
    )

    transition = _build(commitment, policy=_Policy(n5))

    assert commitment.arrival_schedule[0].payment_due_date == due_date
    assert transition.payables[0].due_date == due_date


def test_approval_does_not_reduce_cash_only_unsettled_payables():
    """J **승인은 현금을 깎지 않는다.** 늘어나는 것은 미결제 매입채무다."""
    transition = _build(_Commitment(legs=[(1, WED)]))

    assert transition.next_unsettled_purchase_payables_krw == Decimal("4500000.0")
    # 전이 어디에도 현금을 줄이는 값이 없다 — 상태 행은 원천에서 이어 간다.
    assert not hasattr(transition, "current_cash_krw")
    assert transition.source_finance_state_id == "FIN-DAY30-LOAN"


# ---------------------------------------------------------------------------
# build — 세우는 자리
# ---------------------------------------------------------------------------


def test_two_legs_make_distinct_payables_from_authoritative_amounts_and_ids():
    """날짜·금액·매입 ID가 다른 두 회차는 두 채무로 선다."""
    monday = date(2026, 1, 5)
    transition = _build(
        _Commitment(legs=[(1, WED, 1_250_000), (2, monday, 3_250_000)]),
        purchase_ids={1: "PUR-S1", 2: "PUR-S2"},
    )

    assert [row.payable_id for row in transition.payables] == [
        "AP-H1-REQ-1-1-S1",
        "AP-H1-REQ-1-1-S2",
    ]
    assert [row.purchase_id for row in transition.payables] == ["PUR-S1", "PUR-S2"]
    assert [row.amount_krw for row in transition.payables] == [
        Decimal(1250000),
        Decimal(3250000),
    ]
    assert [row.due_date for row in transition.payables] == [WED, monday]
    assert transition.payable_total_krw == Decimal(4500000)
    assert transition.next_unsettled_purchase_payables_krw == Decimal(4500000)


def test_two_legs_on_the_same_purchase_date_remain_distinct():
    transition = _build(
        _Commitment(legs=[(1, WED, 2_000_000), (2, WED, 2_500_000)]),
        purchase_ids={1: "PUR-S1", 2: "PUR-S2"},
    )

    assert len(transition.payables) == 2
    assert {row.payable_id for row in transition.payables} == {
        "AP-H1-REQ-1-1-S1",
        "AP-H1-REQ-1-1-S2",
    }


@pytest.mark.parametrize(
    "legs",
    [
        [(1, WED, 2_000_000), (2, THU, None)],
        [(1, WED, None), (2, THU, None)],
    ],
)
def test_multi_leg_missing_amounts_fail_closed(legs):
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=legs), purchase_ids={1: "PUR-S1", 2: "PUR-S2"})

    assert raised.value.key == "commitment_payment_amounts"


def test_multi_leg_amount_sum_mismatch_fails_closed():
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(
            _Commitment(legs=[(1, WED, 2_000_000), (2, THU, 2_000_000)]),
            purchase_ids={1: "PUR-S1", 2: "PUR-S2"},
        )

    assert raised.value.key == "commitment_payment_amounts"


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf")])
def test_unusable_leg_amount_fails_closed(amount):
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[(1, WED, amount)]))

    assert raised.value.key == "commitment_payment_amounts"


def test_empty_arrival_schedule_fails_closed():
    """회차가 없으면 `purchase_ids` 도 비어 있다 — 채무를 세울 ID 가 없다."""
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[]), purchase_ids={})

    assert raised.value.key == "commitment_arrival_schedule"


# ---------------------------------------------------------------------------
# purchase_ids — 마스터가 만들고 재무는 **seq 로 찾아 쓰기만** 한다
# ---------------------------------------------------------------------------


def test_finance_uses_the_id_mapped_to_the_leg_seq():
    """회차 번호로 찾는다. 매핑에 다른 회차가 섞여 있어도 흔들리지 않는다."""
    transition = _build(
        _Commitment(legs=[(2, WED)]),
        purchase_ids={1: "PUR-WRONG-S1", 2: "PUR-RIGHT-S2", 3: "PUR-WRONG-S3"},
    )

    assert transition.payables[0].purchase_id == "PUR-RIGHT-S2"


def test_missing_seq_key_fails_closed():
    """H 내 회차 ID 가 없으면 세운다 — 지어내지 않는다."""
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[(1, WED)]), purchase_ids={2: "PUR-S2"})

    assert raised.value.key == "commitment_purchase_ids"


def test_missing_one_multi_leg_purchase_id_fails_closed():
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(
            _Commitment(legs=[(1, WED, 2_000_000), (2, THU, 2_500_000)]),
            purchase_ids={1: "PUR-S1"},
        )

    assert raised.value.key == "commitment_purchase_ids"


def test_single_unrelated_mapping_entry_is_not_silently_picked():
    """🔴 값이 하나뿐이라고 집으면 **엉뚱한 매입에 채무가 붙는다.**

    에러 없이 원장만 어긋나는 종류라, 하나뿐이어도 회차가 다르면 세운다.
    """
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[(1, WED)]), purchase_ids={7: "PUR-S7"})

    assert raised.value.key == "commitment_purchase_ids"


@pytest.mark.parametrize("purchase_id", ["", "   "])
def test_blank_mapped_purchase_id_fails_closed(purchase_id):
    """매입 ID 는 매입이 소유한다. 빈 값을 받아 쓰지 않는다."""
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[(1, WED)]), purchase_ids={1: purchase_id})

    assert raised.value.key == "commitment_purchase_ids"


def test_finance_never_constructs_a_purchase_id():
    """재무 원문에 `PUR-` 을 짓는 자리가 없다. ID 는 받아 쓰는 값이다."""
    import app.finance.transition as module

    with open(module.__file__, encoding="utf-8") as handle:
        body = handle.read().split('"""', 2)[2]

    assert 'f"PUR-' not in body
    assert '"PUR-' not in body


def test_missing_payment_policy_fails_closed():
    """N5 를 못 읽으면 만기일을 지어내지 않는다."""
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[(1, WED)]), policy=_Policy(None))

    assert raised.value.key == "purchase_payment_days"


def test_state_older_than_approval_day_fails_closed():
    """승인일 잔액을 다른 날 잔액으로 대신 계산하지 않는다."""
    stale = dict(_STATE, state_date=WED - timedelta(days=1))
    with (
        patch(f"{_MODULE}.load_finance_state_row", return_value=stale),
        patch(f"{_MODULE}.get_active_finance_policy", return_value=_Policy()),
        pytest.raises(FinanceDataNotReady) as raised,
    ):
        build_finance_transition(
            _Commitment(legs=[(1, WED)]),
            target_state_date=THU,
            purchase_ids=_PURCHASE_IDS,
        )

    assert raised.value.key == "historical_finance_position"


# ---------------------------------------------------------------------------
# target_state_date — 장부일은 달력일이다
# ---------------------------------------------------------------------------


def test_saturday_target_state_date_is_accepted():
    """K **토요일 상태는 정상이다.**

    매입 판단은 평일만 돌지만 장부는 매 달력일 전진한다 — 주말에도 판매와 원장
    활동이 일어난다. 재무가 평일로 미루면 그 하루가 장부에서 사라진다.
    """
    friday = date(2026, 1, 2)
    transition = _build(_Commitment(as_of=friday, legs=[(1, friday)]), target=SAT)

    assert transition.next_state_date == SAT
    assert transition.next_state_date.isoweekday() == 6


def test_sunday_target_state_date_is_accepted():
    transition = _build(_Commitment(as_of=SAT, legs=[(1, SAT)]), target=SUN)

    assert transition.next_state_date == SUN


@pytest.mark.parametrize("target", [WED, WED - timedelta(days=1)])
def test_target_state_date_must_be_after_approval(target):
    """같은 날에 상태가 둘 서면 그날의 사실을 말할 수 없다."""
    with pytest.raises(ValueError):
        _build(_Commitment(legs=[(1, WED)]), target=target)


def test_finance_does_not_import_master_execution_day():
    """L 실행일 달력은 마스터 것이다. 재무가 평일 계산을 대신 하지 않는다."""
    import app.finance.transition as module

    source = module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "next_execution_day" not in text.split('"""', 2)[2]
    assert "from app.master.execution_day" not in text
    assert "import app.master.execution_day" not in text


# ---------------------------------------------------------------------------
# persist — 연결은 부르는 쪽 것이다
# ---------------------------------------------------------------------------


def test_persist_uses_the_supplied_connection_and_never_commits():
    """M·N 받은 연결로만 쓴다. commit·rollback·close 는 부르는 쪽 몫이다."""
    conn, written = _persist(_build(_Commitment(legs=[(1, WED)])))

    assert conn.cursors == 1
    assert len(conn.executed) == 2  # payables 1건 + finance_states 1건
    assert conn.calls == []  # commit / rollback / close 어느 것도 부르지 않았다
    assert written == {"finance_states": 1, "payables": 1}


def test_persist_opens_no_connection_of_its_own():
    """자기 커넥션을 열면 마스터가 쥔 트랜잭션 밖에서 쓰게 된다."""
    with patch("app.finance.db.get_connection") as opened:
        _persist(_build(_Commitment(legs=[(1, WED)])))

    opened.assert_not_called()


def test_persist_reports_zero_rows_when_the_same_approval_is_reapplied():
    """O 같은 승인을 다시 적용하면 DB 제약이 막고 **쓴 행 수 0** 으로 돌아온다."""
    _, written = _persist(_build(_Commitment(legs=[(1, WED)])), rowcount=0)

    assert written == {"finance_states": 0, "payables": 0}


@pytest.mark.parametrize("reverse", [False, True])
def test_same_day_approvals_accumulate_in_one_state_and_retries_do_not_double_count(reverse):
    """승인 순서와 retry는 일별 상태 숫자에 영향을 주지 않는다."""
    approval_a = _build(
        _Commitment(
            approval_id="H1-REQ-A-1",
            legs=[(1, WED, 3_000_000)],
            amount=3_000_000,
        ),
        purchase_ids={1: "PUR-A-S1"},
    )
    approval_b = _build(
        _Commitment(
            approval_id="H1-REQ-B-1",
            legs=[(1, WED, 2_000_000)],
            amount=2_000_000,
        ),
        purchase_ids={1: "PUR-B-S1"},
    )
    ordered = [approval_b, approval_a] if reverse else [approval_a, approval_b]
    conn = _LedgerConn()

    first = [_persist_to_ledger(conn, plan) for plan in ordered]
    retries = [_persist_to_ledger(conn, plan) for plan in ordered]

    assert first == [
        {"finance_states": 1, "payables": 1},
        {"finance_states": 1, "payables": 1},
    ]
    assert retries == [
        {"finance_states": 0, "payables": 0},
        {"finance_states": 0, "payables": 0},
    ]
    assert conn.payables == {
        "PUR-A-S1": Decimal(3_000_000),
        "PUR-B-S1": Decimal(2_000_000),
    }
    assert len(conn.states) == 1
    (state,) = conn.states.values()
    assert state["finance_state_id"] == ("FIN-DAY-SIM-BURNIN-202512-LOAN_BASELINE-20260101")
    assert state["unsettled_purchase_payables_krw"] == Decimal(5_000_000)
    assert state["current_cash_krw"] == _STATE["current_cash_krw"]
    assert state["receivables_krw"] == _STATE["receivables_krw"]
    assert conn.calls == []


def test_state_upsert_uses_daily_axis_conflict_and_only_new_payable_delta():
    transition = _build(_Commitment(legs=[(1, WED)]))
    conn = _LedgerConn()

    _persist_to_ledger(conn, transition)
    state_query, state_params = next(item for item in conn.executed if ".finance_states" in item[0])

    assert "ON CONFLICT (sim_run_id, financing_mode, state_date)" in state_query
    assert "current_state.unsettled_purchase_payables_krw + %s" in state_query
    assert state_params[3] == transition.payable_total_krw
    assert state_params[-1] == transition.payable_total_krw


def test_existing_same_day_state_keeps_its_id_while_new_obligation_accumulates():
    """기존 E2E의 approval 기반 ID를 재작성하지 않고 날짜축 숫자만 누적한다."""
    transition = _build(
        _Commitment(
            approval_id="H1-REQ-NEW-1",
            legs=[(1, WED, 2_000_000)],
            amount=2_000_000,
        ),
        purchase_ids={1: "PUR-NEW-S1"},
    )
    conn = _LedgerConn()
    key = (SIM_RUN_ID, "LOAN_BASELINE", THU)
    conn.states[key] = {
        "finance_state_id": "FIN-H1-REQ-EXISTING-1",
        "current_cash_krw": _STATE["current_cash_krw"],
        "receivables_krw": _STATE["receivables_krw"],
        "unsettled_purchase_payables_krw": Decimal(3_000_000),
    }

    _persist_to_ledger(conn, transition)

    assert conn.states[key]["finance_state_id"] == "FIN-H1-REQ-EXISTING-1"
    assert conn.states[key]["unsettled_purchase_payables_krw"] == Decimal(5_000_000)


def test_persist_writes_each_multi_leg_payable_on_the_supplied_connection():
    transition = _build(
        _Commitment(legs=[(1, WED, 2_000_000), (2, THU, 2_500_000)]),
        purchase_ids={1: "PUR-S1", 2: "PUR-S2"},
    )

    conn, written = _persist(transition)

    assert len(conn.executed) == 3  # payables 2건 + finance_states 1건
    assert written == {"finance_states": 1, "payables": 2}
    assert conn.calls == []


def test_persist_writes_the_h1_state_type_and_carries_the_source_row():
    """상태 행은 원천에서 이어 간다 — 재고 평가액 같은 남의 숫자를 옮겨 적지 않는다."""
    conn, _ = _persist(_build(_Commitment(legs=[(1, WED)])))
    state_query, state_params = conn.executed[-1]

    assert H1_STATE_TYPE in state_params
    assert "FIN-DAY30-LOAN" in state_params  # 이어 갈 원천 행
    # 생성 컬럼은 넣지 않는다 — PostgreSQL 이 다시 계산한다.
    assert "financial_limit_krw" not in state_query.as_string(None)


# ---------------------------------------------------------------------------
# 어댑터 — 마스터가 부르는 입구. **얇은지**가 요점이다
# ---------------------------------------------------------------------------


def test_adapter_build_forwards_both_master_arguments():
    """18 마스터가 주는 두 값을 그대로 넘긴다."""
    transition = _build(_Commitment(legs=[(1, SAT)]), target=SUN, via_adapter=True)

    assert transition.next_state_date == SUN
    assert transition.payables[0].purchase_id == _PURCHASE_IDS[1]


def test_adapter_matches_the_merged_master_protocol_call_shape():
    """마스터가 실제로 쓰는 **키워드 이름 그대로** 불러 본다.

    ★ 마스터 내부(`purchase_id_for` 등)를 여기서 다시 시험하지 않는다 — 그건 마스터
      몫이다. 여기서 보는 것은 **호출 모양이 어긋나지 않는가** 하나뿐이다.
    """
    from app.master.transition import FinanceTransition as MasterFinanceProtocol

    # `isinstance` 는 쓰지 않는다 — 마스터 Protocol 은 `@runtime_checkable` 이 아니고,
    # 그걸 붙이자고 마스터 파일을 고칠 일은 아니다. 계약은 **호출 모양**이다.
    adapter = FinanceTransitionAdapter()
    master_shape = inspect.signature(MasterFinanceProtocol.build).parameters
    finance_shape = inspect.signature(adapter.build).parameters
    assert set(master_shape) - {"self"} == set(finance_shape) - {"self"}
    for name in ("target_state_date", "purchase_ids"):
        assert finance_shape[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_adapter_persist_forwards_the_supplied_connection_and_opens_none():
    """19·20 받은 연결로만 쓴다. 자기 연결을 열지 않는다."""
    transition = _build(_Commitment(legs=[(1, WED)]))
    conn = _Conn()

    with (
        patch(f"{_MODULE}.get_db_schema", return_value="haetdeul"),
        patch("app.finance.db.get_connection") as opened,
    ):
        written = FinanceTransitionAdapter().persist(conn, transition)

    opened.assert_not_called()
    assert conn.cursors == 1
    assert conn.calls == []  # commit / rollback / close 어느 것도 부르지 않았다
    assert written == {"finance_states": 1, "payables": 1}
