"""거래처 여신한도 정본 — **한도는 정책이 아니라 거래처가 소유한 사실이다.**

🔴 이 표가 생기기 전에는 `partners` 에도 `agent_policy_config` 에도 한도 컬럼이 없어
   `FIN-SALES-CREDIT` · `FIN-SALES-AR-CAPACITY` 두 규칙이 **항상 닫혀 있었다.**
   판매 재무검증이 `partner_credit_limit_krw` 미비로 늘 `RUNTIME_NOT_READY` 였다.

★ 이 파일이 지키는 것은 하나다.

    ```text
    Decimal(0)  여신한도가 0원이라는 사실   → 판정한다 (어떤 제안도 한도를 넘는다)
    None        한도가 아직 확정되지 않음   → 판정하지 않는다
    ```

  둘을 같은 값으로 만들면 *"한도를 다 썼다"* 와 *"한도를 모른다"* 가 같은 답을 내고,
  그때 나오는 것은 오류가 아니라 **틀린 판정**이다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.db import FinanceDataNotReady, load_partner_credit_limit

_DB = "app.finance.db"
AS_OF = date(2026, 4, 1)
PARTNER = "KIMCHI_FACTORY_001"


class _Rows:
    """`fetch_all` 대역. **질의가 실제로 좁히는지**도 같이 본다."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, list]] = []

    def __call__(self, query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.calls.append((text, list(params or [])))
        return list(self.rows)


def _row(amount, limit_id="PCL-1"):
    return {"partner_credit_limit_id": limit_id, "credit_limit_krw": amount}


def _load(rows, *, as_of=AS_OF, partner_id=PARTNER):
    fetch_all = _Rows(rows)
    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", fetch_all),
    ):
        return load_partner_credit_limit(as_of=as_of, partner_id=partner_id), fetch_all


# ---------------------------------------------------------------------------
# 활성 한도 조회
# ---------------------------------------------------------------------------


def test_an_active_limit_is_read_as_is():
    limit, _ = _load([_row(Decimal(10_000_000))])

    assert limit == Decimal(10_000_000)


def test_the_query_is_scoped_to_the_partner_and_the_day():
    _limit, fetch_all = _load([_row(Decimal(10_000_000))])

    text, params = fetch_all.calls[0]
    assert "partner_id = %s" in text
    assert "effective_from <= %s" in text
    assert "effective_to IS NULL OR effective_to >= %s" in text
    assert "is_active" in text
    assert params == [PARTNER, AS_OF, AS_OF]


def test_a_different_partner_is_never_substituted():
    """★ 한 거래처의 한도를 다른 거래처에 쓰지 않는다 — 질의가 이름을 싣는다."""
    _limit, fetch_all = _load([_row(Decimal(1))], partner_id="P-OTHER")

    assert fetch_all.calls[0][1][0] == "P-OTHER"


# ---------------------------------------------------------------------------
# 0 과 None
# ---------------------------------------------------------------------------


def test_a_zero_limit_is_a_fact_not_a_missing_limit():
    """🔴 0원 한도는 **실제 사실**이다. 판정을 닫는 `None` 과 다르다."""
    limit, _ = _load([_row(Decimal(0))])

    assert limit == Decimal(0)
    assert limit is not None


def test_no_row_means_undetermined_not_zero():
    """🔴 행이 없으면 **모르는 것**이다. 0 으로도 무한대로도 바꾸지 않는다."""
    limit, _ = _load([])

    assert limit is None


def test_a_missing_limit_is_not_turned_into_a_pass():
    """미확정을 큰 수로 메우면 어떤 제안도 한도 안으로 들어간다."""
    limit, _ = _load([])

    assert limit is None


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------


def test_two_active_limits_on_the_same_day_block_instead_of_guessing():
    """★ 어느 한도로 판정했는지 되짚을 수 없는 상태에서 하나를 집지 않는다."""
    with pytest.raises(FinanceDataNotReady) as raised:
        _load([_row(Decimal(10_000_000), "PCL-1"), _row(Decimal(5_000_000), "PCL-2")])

    assert raised.value.key == "partner_credit_limit_ambiguous"


def test_a_broken_lookup_is_not_an_absent_limit():
    """조회 실패는 *"한도 없음"* 이 아니다 — 실행이 서야 한다."""
    with (
        patch(f"{_DB}.get_db_schema", return_value="haetdeul"),
        patch(f"{_DB}.fetch_all", side_effect=RuntimeError("connection refused")),
        pytest.raises(FinanceDataNotReady) as raised,
    ):
        load_partner_credit_limit(as_of=AS_OF, partner_id=PARTNER)

    assert raised.value.key == "partner_credit_limit"


@pytest.mark.parametrize(
    "amount", [None, "10000000", 10_000_000.0, True], ids=["null", "text", "float", "bool"]
)
def test_an_unreadable_amount_blocks_instead_of_being_coerced(amount):
    """🔴 float 로 바꾸지 않는다. 못 읽은 금액은 못 읽은 것이다."""
    with pytest.raises(FinanceDataNotReady):
        _load([_row(amount)])


def test_a_negative_limit_blocks():
    """한도는 음수일 수 없다 — DB CHECK 와 같은 방향으로 한 겹 더 막는다."""
    with pytest.raises(FinanceDataNotReady):
        _load([_row(Decimal(-1))])


def test_a_blank_partner_is_refused_outright():
    with pytest.raises(ValueError):
        load_partner_credit_limit(as_of=AS_OF, partner_id="   ")


# ---------------------------------------------------------------------------
# 판정까지 — 한도가 있으면 여신 규칙이 실제로 돈다
# ---------------------------------------------------------------------------


def _credit(limit, *, ar, sales_amount):
    from app.finance.capabilities.sales import evaluate_receivable_capacity
    from app.finance.sales_validation import PartnerReceivableFacts

    facts = PartnerReceivableFacts(
        partner_id=PARTNER,
        as_of=AS_OF,
        current_ar_krw=ar,
        overdue_ar_krw=Decimal(0),
        open_receivable_count=1,
        overdue_receivable_count=0,
        source_refs=("AR-1",),
    )
    return evaluate_receivable_capacity(
        sales_amount_krw=sales_amount, receivable_facts=facts, credit_limit_krw=limit
    )


def test_a_proposal_inside_the_limit_is_judged():
    result = _credit(Decimal(10_000_000), ar=Decimal(1_000_000), sales_amount=Decimal(2_000_000))

    assert result["rule"]["runtime_status"] == "READY"
    assert result["rule"]["verdict"] is not None
    assert "partner_credit_limit_krw" not in result["missing_data"]


def test_a_proposal_over_the_limit_is_judged_too():
    """★ 한도 초과는 **판정**이다 — 자료 미비가 아니다."""
    result = _credit(Decimal(1_000_000), ar=Decimal(900_000), sales_amount=Decimal(5_000_000))

    assert result["rule"]["runtime_status"] == "READY"
    assert result["rule"]["verdict"] in {"FAIL", "REVIEW_REQUIRED"}


def test_a_zero_limit_still_produces_a_verdict():
    """0원 한도는 판정한다 — 어떤 제안도 그 한도를 넘는다."""
    result = _credit(Decimal(0), ar=Decimal(0), sales_amount=Decimal(1))

    assert result["rule"]["runtime_status"] == "READY"
    assert result["rule"]["verdict"] is not None
    assert "partner_credit_limit_krw" not in result["missing_data"]


def test_an_undetermined_limit_closes_the_credit_rule():
    result = _credit(None, ar=Decimal(0), sales_amount=Decimal(1))

    assert result["rule"]["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["rule"]["verdict"] is None
    assert "partner_credit_limit_krw" in result["missing_data"]
