"""부채 원천 준비 상태 — **없는 의무를 증명하라고 요구하지 않는다.**

🔴 예전에는 `current_debt_krw` 와 무관하게 부채 정책을 읽고, 행이 없으면
   `DEBT_SERVICE` 를 unresolved 로 올렸다. 그러면 **빚이 없는 회사가 "부채 원천을
   확인하지 못했다"** 고 말한다 — 확인할 부채가 애초에 없는데도. 그 unresolved 는
   아래로 흘러 *"재무가 뭔가 못 읽었다"* 로 읽히고, 실제로는 아무 문제가 없다.

★ 반대편은 그대로 fail-closed 다. 부채 상환은 현금흐름에서 **가장 확실한 유출**이라,
  빠뜨린 투영은 틀린 게 아니라 **낙관적으로 틀린다** — 그 상한으로 매입이 실행된다.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.finance.infrastructure.finance_state_repository import (
    _get_current_finance_state_row,
    get_current_finance_runtime_context,
)
from app.finance.repository import FinanceDataNotReady, PostgresFinanceAsOfDataPort
from app.finance.schemas import FinanceSnapshot
from tests.finance.test_finance_policy_repository import _debt_rows, _rows

#: `patch()` 대상 모듈 경로 — 소유 모듈을 직접 가리킨다.
_STATE_REPO = "app.finance.infrastructure.finance_state_repository"


def _snapshot(debt: Decimal) -> FinanceSnapshot:
    return FinanceSnapshot(
        finance_state_id="FIN-DEBT-TEST",
        sim_run_id="SIM-DEBT-TEST",
        snapshot_id="FIN-DEBT-TEST",
        state_date=date(2025, 12, 31),
        state_type="DAY30",
        financing_mode="LOAN_BASELINE",
        current_cash_krw=Decimal(50_000_000),
        minimum_operating_cash_krw=Decimal(10_000_000),
        committed_outflows_krw=Decimal(0),
        unsettled_purchase_payables_krw=Decimal(0),
        receivables_krw=Decimal(0),
        current_debt_krw=debt,
        financial_limit_krw=Decimal(40_000_000),
    )


def _context(
    *, debt: Decimal, debt_rows: list[dict[str, object]] | None, projection_days: int = 30
):
    """일정 원천은 비우고 **부채 축만** 본다.

    `debt_rows=None` 은 부채 정책 행이 아예 없는 상태다 —
    `_build_finance_debt_policy` 가 `LookupError` 를 낸다.

    ★ `projection_days` 를 열어 둔 이유: 상환일이 월말이라 기본 30일 창
      (2025-12-31 → 2026-01-30)에는 **하루 차이로** 아무 상환도 들어오지 않는다.
      그건 결함이 아니라 산술이므로, 일정이 실제로 생기는지 보려면 창을 넓혀야 한다.
    """
    policy_rows = [
        dict(row, value_numeric=Decimal(projection_days))
        if row["policy_key"] == "cashflow_projection_days"
        else row
        for row in _rows()
    ]

    def fetch_all(query, params=None):
        del query
        # 정책 조회는 (domain, version, scope) 3개를 바인딩한다. 일정 조회는 아니다.
        if params is not None and list(params)[:1] == ["finance"]:
            return policy_rows if debt_rows is None else policy_rows + debt_rows
        return []

    with (
        patch(f"{_STATE_REPO}.get_db_schema", return_value="configured_schema"),
        patch(f"{_STATE_REPO}.fetch_all", side_effect=fetch_all),
        patch(
            f"{_STATE_REPO}.get_current_finance_snapshot",
            return_value=_snapshot(debt),
        ),
    ):
        return get_current_finance_runtime_context()


def test_zero_debt_without_debt_policy_is_ready():
    """① 부채 0 + 부채 정책 없음 = 정상. unresolved 를 만들지 않는다."""
    context = _context(debt=Decimal(0), debt_rows=None)

    assert context.unresolved_sources == ()
    assert context.debt_policy is None
    assert not [event for event in context.cash_events if event.event_type == "DEBT_SERVICE"]


def test_positive_debt_without_debt_policy_is_not_ready():
    """② 부채 있음 + 정책 없음 = 준비되지 않음. **감추지 않는다.**"""
    context = _context(debt=Decimal("45272104.184486"), debt_rows=None)

    assert "DEBT_SERVICE" in context.unresolved_sources
    assert context.debt_policy is None
    assert not [event for event in context.cash_events if event.event_type == "DEBT_SERVICE"]


def test_positive_debt_with_inconsistent_principal_is_not_ready():
    """③ 원금이 재무 상태와 어긋나면 준비되지 않음.

    정책이 있다고 통과시키면 **다른 빚의 상환 일정**으로 현금을 투영하게 된다.
    """
    context = _context(debt=Decimal("11111111.11"), debt_rows=_debt_rows())

    assert "DEBT_SERVICE" in context.unresolved_sources
    assert context.debt_policy is None
    assert not [event for event in context.cash_events if event.event_type == "DEBT_SERVICE"]


def test_positive_debt_with_valid_policy_builds_debt_cash_events():
    """④ 부채 있음 + 정책 일치 = 상환 일정이 현금흐름에 들어간다.

    상환일이 월말이라 기본 30일 창에는 하루 차이로 안 들어온다 — 일정 생성 자체를
    보려면 창을 넓혀야 한다 (`_context` 의 `projection_days` 주석 참고).
    """
    context = _context(
        debt=Decimal("45272104.184486"), debt_rows=_debt_rows(), projection_days=91
    )

    assert context.unresolved_sources == ()
    assert context.debt_policy is not None
    assert context.debt_policy.debt_principal_krw == Decimal("45272104.184486")

    debt_events = [event for event in context.cash_events if event.event_type == "DEBT_SERVICE"]
    assert debt_events, "부채가 있으면 상환 유출이 투영에 들어가야 한다"
    assert all(event.direction == "OUTFLOW" for event in debt_events)
    # 지어낸 출처가 아니라 정책이 실제로 들고 있는 ref 를 단다.
    assert all(event.source_ref for event in debt_events)


# ---------------------------------------------------------------------------
# 음수 부채 — **"빚 없음"으로 읽히면 안 된다**
#
# 🔴 `current_debt_krw > 0` 로만 갈라 놓으면 음수가 0 과 같은 쪽에 떨어진다. 그러면
#    잘못된 DB 상태가 *"확인할 부채가 없다"* 는 정상 응답으로 둔갑하고, 부채 정책
#    검증도 상환 일정도 통째로 건너뛴다. 그 상한으로 매입이 실행된다.
# ---------------------------------------------------------------------------


def _raw_state_row(debt: Decimal) -> dict[str, object]:
    """`v_current_finance_state` 가 돌려주는 것과 같은 **원시 행**."""
    return {
        "finance_state_id": "FIN-DEBT-TEST",
        "sim_run_id": "SIM-DEBT-TEST",
        "state_date": date(2025, 12, 31),
        "state_type": "DAY30",
        "financing_mode": "LOAN_BASELINE",
        "current_cash_krw": Decimal(50_000_000),
        "minimum_operating_cash_krw": Decimal(10_000_000),
        "committed_outflows_krw": Decimal(0),
        "unsettled_purchase_payables_krw": Decimal(0),
        "receivables_krw": Decimal(0),
        "current_debt_krw": debt,
        "financial_limit_krw": Decimal(40_000_000),
    }


def test_negative_debt_is_rejected_at_the_raw_row_boundary():
    """⑤ 음수 부채는 원천 행에서 막힌다 — 두 런타임 경로의 공통 입구다."""
    with (
        patch(f"{_STATE_REPO}.get_db_schema", return_value="configured_schema"),
        patch(f"{_STATE_REPO}.fetch_one", return_value=_raw_state_row(Decimal(-1))),
        pytest.raises(FinanceDataNotReady) as raised,
    ):
        _get_current_finance_state_row()

    assert raised.value.key == "finance_state_debt_invalid"


def test_negative_debt_cannot_reach_the_runtime_context():
    """⑤ 컨텍스트 경로: 음수 부채가 `unresolved 없음` 으로 통과하지 않는다."""
    with (
        patch(f"{_STATE_REPO}.get_db_schema", return_value="configured_schema"),
        patch(f"{_STATE_REPO}.fetch_one", return_value=_raw_state_row(Decimal("-0.01"))),
        patch(f"{_STATE_REPO}.fetch_all", return_value=[]),
        pytest.raises(FinanceDataNotReady),
    ):
        get_current_finance_runtime_context()


def test_negative_debt_cannot_bypass_through_the_as_of_data_port():
    """⑥ AsOf DataPort 경로도 막힌다.

    ★ 이 경로는 **원시 dict 를 그대로** 쓴다 — `FinanceSnapshot` 검증을 거치지 않으므로
      스키마 제약만 믿으면 여기로 음수가 빠져나간다. 그래서 원천 행에서 막아야 한다.
    """
    port = PostgresFinanceAsOfDataPort()
    with (
        patch(f"{_STATE_REPO}.get_db_schema", return_value="configured_schema"),
        patch(f"{_STATE_REPO}.fetch_one", return_value=_raw_state_row(Decimal(-1))),
        pytest.raises(FinanceDataNotReady) as raised,
    ):
        port.load_finance_position(date(2025, 12, 31))

    assert raised.value.key == "finance_state_debt_invalid"
    # 부채 일정 조회까지 가지도 못한다 — 상태 자체를 못 믿기 때문이다.
    with (
        patch(f"{_STATE_REPO}.get_db_schema", return_value="configured_schema"),
        patch(f"{_STATE_REPO}.fetch_one", return_value=_raw_state_row(Decimal(-1))),
        pytest.raises(FinanceDataNotReady),
    ):
        port.load_debt_schedule(date(2025, 12, 31), date(2026, 1, 30))


def test_finance_snapshot_schema_also_rejects_negative_debt():
    """스키마 제약은 **이중 방어**다. 원천 행 검증을 대체하지 않는다.

    경계가 둘인 이유: 컨텍스트 경로는 Snapshot 을 거치지만 AsOf DataPort 는 거치지
    않는다. 한쪽만 두면 다른 쪽이 뚫린다.
    """
    with pytest.raises(ValidationError):
        FinanceSnapshot(snapshot_id=None, **_raw_state_row(Decimal(-1)))

    # 0 은 유효하다 — 빚이 없는 것은 잘못된 상태가 아니다.
    snapshot = FinanceSnapshot(snapshot_id=None, **_raw_state_row(Decimal(0)))
    assert snapshot.current_debt_krw == 0
