"""승인 약정 → 재무 전이. **계산과 쓰기의 경계를 지킨다.**

`app/finance/transition.py` 는 운영 모듈인데 시험이 하나도 없었다. 여기서 보는 것은
지금 계약으로 확정된 것들뿐이다 — 회차별 지급액과 회차별 `purchase_id` 를 실어 주는
계약이 아직 없으므로, 그 위에 서는 분할 매입 시험은 짓지 않는다. 없는 계약 위에 세운
시험은 계약이 오는 날 같이 틀린다.

★ DB 를 타지 않는다. 상태·정책 조회는 갈아 끼우고, `persist` 는 가짜 커넥션으로
  **무엇을 불렀고 무엇을 안 불렀는지**만 본다.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.db import FinanceDataNotReady
from app.finance.transition import (
    H1_STATE_TYPE,
    build_finance_transition,
    persist_finance_transition,
)

_MODULE = "app.finance.transition"

WED = date(2025, 12, 31)  # 수요일
THU = date(2026, 1, 1)
SAT = date(2026, 1, 3)
SUN = date(2026, 1, 4)

_STATE = {
    "finance_state_id": "FIN-DAY30-LOAN",
    "sim_run_id": "SIM-BURNIN-202512",
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
    """`ArrivalLeg` 중 재무가 읽는 칸만. **회차별 금액 칸은 아직 계약에 없다.**"""

    def __init__(self, purchase_date: date):
        self.purchase_date = purchase_date


class _Commitment:
    def __init__(self, *, as_of: date = WED, legs=(), amount: float = 4_500_000.0):
        self.approval_id = "H1-REQ-1-1"
        self.as_of = as_of
        self.total_amount_krw = amount
        self.arrival_schedule = tuple(_Leg(d) for d in legs)


class _Policy:
    """N5 는 달력일수다. 현재 계약값은 0(매입 당일)."""

    def __init__(self, payment_days: int | None = 0):
        self.purchase_payment_days = payment_days


def _build(commitment, *, purchase_id="PUR-REQ-1-S1", target=THU, policy=None):
    # 상태 행은 승인일에 서 있는 것으로 둔다 — 신선도 게이트는 별도 시험이 본다.
    state = dict(_STATE, state_date=commitment.as_of)
    with (
        patch(f"{_MODULE}.load_finance_state_row", return_value=state),
        patch(f"{_MODULE}.get_active_finance_policy", return_value=policy or _Policy()),
    ):
        return build_finance_transition(
            commitment, purchase_id=purchase_id, target_state_date=target
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


# ---------------------------------------------------------------------------
# build — 한 회차 (지금 계약으로 만들 수 있는 유일한 모양)
# ---------------------------------------------------------------------------

def test_single_leg_makes_one_payable_from_authoritative_values():
    """A·D 회차 하나면 채무 하나. 금액은 약정 총액 그대로다."""
    transition = _build(_Commitment(legs=[WED]))

    assert len(transition.payables) == 1
    payable = transition.payables[0]
    assert payable.purchase_id == "PUR-REQ-1-S1"
    assert payable.issued_date == WED
    assert payable.amount_krw == Decimal("4500000.0")
    assert transition.payable_total_krw == Decimal(str(_Commitment().total_amount_krw))


def test_due_date_is_purchase_date_plus_calendar_n5():
    """계약 만기일 = 매입일 + N5 **달력일**. 주말로 밀지 않는다."""
    friday = date(2026, 1, 2)
    transition = _build(_Commitment(legs=[friday]), policy=_Policy(1))

    # 금요일 + 1 달력일 = 토요일. 원장은 계약일을 그대로 든다.
    assert transition.payables[0].due_date == SAT
    assert transition.payables[0].due_date.isoweekday() == 6


def test_approval_does_not_reduce_cash_only_unsettled_payables():
    """J **승인은 현금을 깎지 않는다.** 늘어나는 것은 미결제 매입채무다."""
    transition = _build(_Commitment(legs=[WED]))

    assert transition.next_unsettled_purchase_payables_krw == Decimal("4500000.0")
    # 전이 어디에도 현금을 줄이는 값이 없다 — 상태 행은 원천에서 이어 간다.
    assert not hasattr(transition, "current_cash_krw")
    assert transition.source_finance_state_id == "FIN-DAY30-LOAN"


# ---------------------------------------------------------------------------
# build — 세우는 자리
# ---------------------------------------------------------------------------

def test_multiple_purchase_dates_fail_closed_without_a_per_leg_amount_contract():
    """E 매입일이 여러 날인데 **회차별 지급액 계약이 없으면 세운다.**

    지금 `ArrivalLeg` 에는 금액 칸 자체가 없다. 약정이 드는 금액은 총액 하나뿐이라,
    매입일이 갈리는 순간 어느 날 얼마가 나가는지 말할 방법이 없다.

    🔴 수량 비율로 쪼개면 회차마다 단가가 다른 분할 매입에서 조용히 틀린 채무가
       생긴다. 회차별 지급액을 실어 주는 계약이 서기 전까지 여기는 닫혀 있어야 한다.
    """
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[WED, date(2026, 1, 5)]))

    assert raised.value.key == "commitment_payment_amounts"


@pytest.mark.parametrize("purchase_id", ["", "   "])
def test_blank_purchase_id_fails_closed(purchase_id):
    """H 매입 ID 는 매입이 소유한다. 재무가 지어내지 않고, 빈 값도 받지 않는다."""
    with pytest.raises(ValueError):
        _build(_Commitment(legs=[WED]), purchase_id=purchase_id)


def test_missing_payment_policy_fails_closed():
    """N5 를 못 읽으면 만기일을 지어내지 않는다."""
    with pytest.raises(FinanceDataNotReady) as raised:
        _build(_Commitment(legs=[WED]), policy=_Policy(None))

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
            _Commitment(legs=[WED]), purchase_id="PUR-1", target_state_date=THU
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
    transition = _build(_Commitment(as_of=friday, legs=[friday]), target=SAT)

    assert transition.next_state_date == SAT
    assert transition.next_state_date.isoweekday() == 6


def test_sunday_target_state_date_is_accepted():
    transition = _build(_Commitment(as_of=SAT, legs=[SAT]), target=SUN)

    assert transition.next_state_date == SUN


@pytest.mark.parametrize("target", [WED, WED - timedelta(days=1)])
def test_target_state_date_must_be_after_approval(target):
    """같은 날에 상태가 둘 서면 그날의 사실을 말할 수 없다."""
    with pytest.raises(ValueError):
        _build(_Commitment(legs=[WED]), target=target)


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
    conn, written = _persist(_build(_Commitment(legs=[WED])))

    assert conn.cursors == 1
    assert len(conn.executed) == 2  # payables 1건 + finance_states 1건
    assert conn.calls == []  # commit / rollback / close 어느 것도 부르지 않았다
    assert written == {"finance_states": 1, "payables": 1}


def test_persist_opens_no_connection_of_its_own():
    """자기 커넥션을 열면 마스터가 쥔 트랜잭션 밖에서 쓰게 된다."""
    with patch("app.finance.db.get_connection") as opened:
        _persist(_build(_Commitment(legs=[WED])))

    opened.assert_not_called()


def test_persist_reports_zero_rows_when_the_same_approval_is_reapplied():
    """O 같은 승인을 다시 적용하면 DB 제약이 막고 **쓴 행 수 0** 으로 돌아온다."""
    _, written = _persist(_build(_Commitment(legs=[WED])), rowcount=0)

    assert written == {"finance_states": 0, "payables": 0}


def test_persist_writes_the_h1_state_type_and_carries_the_source_row():
    """상태 행은 원천에서 이어 간다 — 재고 평가액 같은 남의 숫자를 옮겨 적지 않는다."""
    conn, _ = _persist(_build(_Commitment(legs=[WED])))
    state_query, state_params = conn.executed[-1]

    assert H1_STATE_TYPE in state_params
    assert "FIN-DAY30-LOAN" in state_params  # 이어 갈 원천 행
    # 생성 컬럼은 넣지 않는다 — PostgreSQL 이 다시 계산한다.
    assert "financial_limit_krw" not in state_query.as_string(None)
