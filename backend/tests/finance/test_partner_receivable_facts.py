"""거래처 매출채권을 **실 원장에서** 읽는다 — 못 읽은 것과 0원을 가른다.

★ 이 파일이 지키는 것.
    · 미회수는 `OPEN` · `PARTIAL` 뿐이다 (`COLLECTED` · `WRITEOFF` 는 빠진다)
    · 연체는 `due_date < as_of` 하나로 정해진다 — 당일은 아직 연체가 아니다
    · 조회 0건은 **채권이 0원이라는 사실**이고, 조회 실패는 사실이 아니다
    · 다른 sim_run · 다른 거래처 · 아직 오지 않은 날짜가 섞이지 않는다
    · 금액은 Decimal 로 남는다

🔴 여기서 가장 쉽게 무너지는 곳은 **빈 결과의 뜻**이다. 연결이 끊겨도, 조회가 실패해도
   결과는 똑같이 "행 0건" 처럼 보인다. 그것을 0원 채권으로 읽으면 연체가 있는 거래처가
   깨끗한 거래처가 되고, 아무도 그 사실을 눈치채지 못한다.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import db
from app.finance.db import FinanceDataNotReady, load_partner_receivables
from app.finance.sales_models import PartnerReceivable
from app.finance.tools import summarize_partner_receivables

AS_OF = date(2025, 12, 31)


def _row(receivable_id, *, due, status="OPEN", outstanding="100000"):
    return {
        "receivable_id": receivable_id,
        "due_date": due,
        "outstanding_amount_krw": Decimal(outstanding),
        "status": status,
    }


def _load(rows, *, sim_run_id="SIM-1", partner_id="P-1", as_of=AS_OF):
    """실 조회 자리에 행을 놓고 loader 를 그대로 돌린다."""
    captured: dict[str, object] = {}

    def _fetch(query, params):
        captured["query"] = query.as_string(None)
        captured["params"] = params
        if isinstance(rows, Exception):
            raise rows
        return rows

    with (
        patch.object(db, "fetch_all", _fetch),
        patch.object(db, "get_db_schema", return_value="haetdeul"),
    ):
        loaded = load_partner_receivables(
            sim_run_id=sim_run_id, as_of=as_of, partner_id=partner_id
        )
    return loaded, captured


def _facts(rows, *, partner_id="P-1", as_of=AS_OF):
    loaded, _ = _load(rows, partner_id=partner_id, as_of=as_of)
    return summarize_partner_receivables(
        partner_id=partner_id, as_of=as_of, receivables=loaded
    )


# ---------------------------------------------------------------------------
# 채권 없음 — 0원은 사실이다
# ---------------------------------------------------------------------------


def test_a_partner_with_no_open_receivables_has_zero_ar():
    """🔴 조회가 성공했고 미회수 행이 0건일 때만 0원이다."""
    facts = _facts([])

    assert facts.current_ar_krw == Decimal(0)
    assert facts.overdue_ar_krw == Decimal(0)
    assert facts.open_receivable_count == 0
    assert facts.overdue_receivable_count == 0
    assert facts.source_refs == ()


# ---------------------------------------------------------------------------
# 미연체 · 연체 · 당일
# ---------------------------------------------------------------------------


def test_an_open_receivable_due_later_is_ar_but_not_overdue():
    facts = _facts([_row("AR-1", due=date(2026, 1, 5))])

    assert facts.current_ar_krw == Decimal(100000)
    assert facts.overdue_ar_krw == Decimal(0)
    assert facts.open_receivable_count == 1
    assert facts.overdue_receivable_count == 0


def test_an_open_receivable_past_its_due_date_is_overdue():
    facts = _facts([_row("AR-1", due=date(2025, 12, 30))])

    assert facts.current_ar_krw == Decimal(100000)
    assert facts.overdue_ar_krw == Decimal(100000)
    assert facts.overdue_receivable_count == 1


def test_a_receivable_due_today_is_not_yet_overdue():
    """🔴 만기 당일은 아직 늦지 않았다 — `<` 가 `<=` 로 새면 하루가 통째로 연체가 된다."""
    facts = _facts([_row("AR-1", due=AS_OF)])

    assert facts.current_ar_krw == Decimal(100000)
    assert facts.overdue_ar_krw == Decimal(0)
    assert facts.overdue_receivable_count == 0


def test_no_grace_period_is_invented_around_the_due_date():
    """3일·7일 같은 유예를 만들지 않는다 — 하루만 지나도 연체다."""
    facts = _facts([_row("AR-1", due=date(2025, 12, 30))])

    assert facts.overdue_ar_krw == Decimal(100000)


# ---------------------------------------------------------------------------
# 상태 어휘 — 무엇이 미회수인가
# ---------------------------------------------------------------------------


def test_partial_collection_still_leaves_outstanding_ar():
    """일부만 받은 채권은 남은 금액만큼 그대로 미회수다."""
    facts = _facts([_row("AR-1", due=date(2026, 1, 5), status="PARTIAL", outstanding="40000")])

    assert facts.current_ar_krw == Decimal(40000)
    assert facts.open_receivable_count == 1


def test_a_partial_receivable_past_due_is_overdue_too():
    facts = _facts([_row("AR-1", due=date(2025, 12, 1), status="PARTIAL", outstanding="40000")])

    assert facts.overdue_ar_krw == Decimal(40000)
    assert facts.overdue_receivable_count == 1


@pytest.mark.parametrize("settled", ["COLLECTED", "WRITEOFF"])
def test_settled_receivables_are_not_ar(settled):
    """받았거나 털어낸 채권은 미회수도 연체도 아니다."""
    facts = _facts([_row("AR-1", due=date(2025, 1, 1), status=settled)])

    assert facts.current_ar_krw == Decimal(0)
    assert facts.overdue_ar_krw == Decimal(0)
    assert facts.open_receivable_count == 0
    assert facts.overdue_receivable_count == 0


def test_the_loader_does_not_narrow_the_status_vocabulary_itself():
    """★ 무엇이 미회수인지는 **집계가 소유한다** — SQL 에 또 적으면 둘이 갈라진다."""
    loaded, captured = _load(
        [
            _row("AR-1", due=date(2026, 1, 5)),
            _row("AR-2", due=date(2026, 1, 5), status="COLLECTED"),
        ]
    )

    # 원장 행은 상태를 잃지 않고 그대로 올라온다.
    assert [item.status for item in loaded] == ["OPEN", "COLLECTED"]
    assert "COLLECTED" not in str(captured["query"])
    assert "WRITEOFF" not in str(captured["query"])


def test_an_unknown_status_is_not_guessed():
    """어휘 밖의 상태는 미회수인지 아닌지 알 수 없다 — 지어내지 않고 선다."""
    with pytest.raises(FinanceDataNotReady):
        _load([_row("AR-1", due=date(2026, 1, 5), status="DISPUTED")])


# ---------------------------------------------------------------------------
# 경계 — 다른 run · 다른 거래처 · 아직 오지 않은 날짜
# ---------------------------------------------------------------------------


def test_only_the_requested_partner_is_asked_for():
    _, captured = _load([], partner_id="P-9")

    assert "s.customer_partner_id = %s" in str(captured["query"])
    assert "P-9" in list(captured["params"])


def test_both_ledgers_are_pinned_to_the_same_simulation_run():
    """🔴 한쪽만 걸면 다른 실행의 판매 Header 를 타고 남의 채권이 딸려 들어온다."""
    _, captured = _load([], sim_run_id="SIM-7")
    query = str(captured["query"])

    assert "r.sim_run_id = %s" in query
    assert "s.sim_run_id = %s" in query
    # 조인 자체도 같은 run 안에서만 이어진다.
    assert "AND s.sim_run_id = r.sim_run_id" in query
    assert list(captured["params"])[:2] == ["SIM-7", "SIM-7"]


def test_nothing_issued_after_as_of_is_read():
    """🔴 오늘 이후 발행된 채권을 과거 기준일에서 읽으면 as-of 재현이 깨진다."""
    _, captured = _load([], as_of=date(2025, 6, 30))
    query = str(captured["query"])

    assert "r.issued_date <= %s" in query
    assert "s.sale_date <= %s" in query
    assert list(captured["params"])[3:] == [date(2025, 6, 30), date(2025, 6, 30)]


def test_the_row_order_is_stable_across_runs():
    """정렬을 새 업무 의미로 만들지 않는다 — 안정적인 식별자 순서면 된다."""
    _, captured = _load([])

    assert "ORDER BY r.receivable_id" in str(captured["query"])


# ---------------------------------------------------------------------------
# 못 읽은 것은 사실이 아니다
# ---------------------------------------------------------------------------


def test_a_connection_failure_is_not_an_empty_ledger():
    with pytest.raises(FinanceDataNotReady) as caught:
        _load(RuntimeError("connection refused"))

    assert caught.value.key == "partner_receivables"


def test_a_query_failure_is_not_an_empty_ledger():
    with pytest.raises(FinanceDataNotReady):
        _load(LookupError("relation does not exist"))


def test_a_missing_column_is_not_a_zero_amount():
    """🔴 칸이 없는 것을 0원으로 읽으면 채권이 조용히 사라진다."""
    row = _row("AR-1", due=date(2026, 1, 5))
    del row["outstanding_amount_krw"]

    with pytest.raises(FinanceDataNotReady):
        _load([row])


def test_a_null_amount_is_not_a_zero_amount():
    with pytest.raises(FinanceDataNotReady):
        _load([{**_row("AR-1", due=date(2026, 1, 5)), "outstanding_amount_krw": None}])


def test_a_float_amount_is_refused_rather_than_rounded():
    """업무 금액을 float 로 받지 않는다 — 이미 정밀도를 잃은 값이다."""
    with pytest.raises(FinanceDataNotReady):
        _load([{**_row("AR-1", due=date(2026, 1, 5)), "outstanding_amount_krw": 100000.0}])


def test_a_boolean_is_not_read_as_a_number():
    with pytest.raises(FinanceDataNotReady):
        _load([{**_row("AR-1", due=date(2026, 1, 5)), "outstanding_amount_krw": True}])


def test_a_blank_partner_is_a_contract_error_not_an_empty_ledger():
    with pytest.raises(ValueError):
        _load([], partner_id="   ")


# ---------------------------------------------------------------------------
# 옮겨 담기 — 금액과 출처를 잃지 않는다
# ---------------------------------------------------------------------------


def test_amounts_stay_decimal_through_the_loader():
    loaded, _ = _load([_row("AR-1", due=date(2026, 1, 5), outstanding="123456.789012")])

    assert loaded[0].outstanding_amount_krw == Decimal("123456.789012")
    assert isinstance(loaded[0].outstanding_amount_krw, Decimal)


def test_the_source_ref_points_at_the_real_ledger_row():
    """★ 새 ref 규약을 만들지 않는다 — 이미 현금 Event 가 쓰는 값 그대로다."""
    loaded, _ = _load([_row("AR-77", due=date(2026, 1, 5))])

    assert loaded[0].source_ref == "AR-77"
    assert loaded[0].receivable_id == "AR-77"


def test_facts_carry_the_refs_of_the_rows_that_made_them():
    facts = _facts(
        [
            _row("AR-1", due=date(2026, 1, 5)),
            _row("AR-2", due=date(2025, 12, 1)),
            _row("AR-3", due=date(2025, 1, 1), status="COLLECTED"),
        ]
    )

    # 집계에 들어간 행만 출처로 남는다 — 빠진 행을 근거로 삼지 않는다.
    assert facts.source_refs == ("AR-1", "AR-2")
    assert facts.current_ar_krw == Decimal(200000)
    assert facts.overdue_ar_krw == Decimal(100000)


def test_the_same_ledger_yields_the_same_facts_twice():
    rows = [_row("AR-1", due=date(2026, 1, 5)), _row("AR-2", due=date(2025, 12, 1))]

    assert _facts(rows) == _facts(rows)


def test_the_facts_carry_no_risk_score_or_grade():
    """사실만 만든다 — 등급·점수·비율은 권위 있는 계약이 없으면 추측이다."""
    facts = _facts([_row("AR-1", due=date(2025, 12, 1))])

    assert set(PartnerReceivable.model_fields) == {
        "receivable_id",
        "due_date",
        "outstanding_amount_krw",
        "status",
        "source_ref",
    }
    assert not hasattr(facts, "risk_score")
    assert not hasattr(facts, "partner_grade")
