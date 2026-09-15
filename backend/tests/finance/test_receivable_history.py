"""기준일 시점의 채권 상태 — **미래 수금은 과거 화면에 들어가지 않는다.**

🔴 이 파일은 재무와 판매 **두 벌**을 함께 잠근다. 두 도메인은 서로를 import 하지 않는
   것이 계약이라 규칙이 두 곳에 있고, 갈리는 순간 판정이 갈린다.
"""

import pathlib
from decimal import Decimal

import pytest

from app.finance import receivable_history as finance_history
from app.sales import receivable_history as sales_history

MODULES = pytest.mark.parametrize(
    "history", [finance_history, sales_history], ids=["finance", "sales"]
)

ORIGINAL = Decimal(282426)


# ── 상태 규칙 ─────────────────────────────────────────────────────────────


@MODULES
def test_no_collection_yet_is_open(history):
    """① 수금 전 날짜 — 원금 전액이 미수다."""
    assert (
        history.projected_status(
            original_amount_krw=ORIGINAL, received_amount_krw=Decimal(0)
        )
        == "OPEN"
    )


@MODULES
def test_a_partial_collection_is_partial(history):
    """② 부분 수금 후 날짜 — 일부만 미수다."""
    assert (
        history.projected_status(
            original_amount_krw=ORIGINAL, received_amount_krw=Decimal(100000)
        )
        == "PARTIAL"
    )


@MODULES
def test_a_full_collection_is_collected(history):
    """③ 완납 이후 날짜 — 미수가 0이다."""
    assert (
        history.projected_status(
            original_amount_krw=ORIGINAL, received_amount_krw=ORIGINAL
        )
        == "COLLECTED"
    )


@MODULES
def test_a_zero_collection_is_open_not_partial(history):
    """⑥ 0원 수금은 «일부 받았다» 가 아니다. 0 과 «받았다» 를 가른다."""
    assert (
        history.projected_status(
            original_amount_krw=ORIGINAL, received_amount_krw=Decimal(0)
        )
        == "OPEN"
    )


@MODULES
def test_an_overshooting_total_is_still_collected(history):
    """누적이 원금을 넘겨 들어와도 «더 받았다» 라는 상태를 새로 만들지 않는다."""
    assert (
        history.projected_status(
            original_amount_krw=ORIGINAL, received_amount_krw=ORIGINAL + Decimal(1)
        )
        == "COLLECTED"
    )


@MODULES
def test_a_zero_amount_receivable_is_collected_rather_than_open(history):
    """원금이 0이면 받을 것이 없다 — 영원히 OPEN 으로 남지 않는다."""
    assert (
        history.projected_status(
            original_amount_krw=Decimal(0), received_amount_krw=Decimal(0)
        )
        == "COLLECTED"
    )


# ── SQL 계약 ──────────────────────────────────────────────────────────────


@MODULES
def test_only_events_up_to_the_as_of_are_used(history):
    """④ 미래 수금 event 가 과거 as_of 에 반영되지 않는다."""
    text = str(history.history_join("haetdeul"))

    assert "collection_date <= %s" in text
    assert "collection_date >=" not in text
    assert "collection_date =" not in text


@MODULES
def test_the_last_cumulative_total_is_taken_not_the_sum(history):
    """`target_received_total_krw` 는 누적값이다 — 합이 아니라 마지막 값을 쓴다."""
    text = str(history.history_join("haetdeul"))

    assert "ORDER BY event.collection_date DESC" in text
    assert "LIMIT 1" in text
    assert "SUM(" not in text


@MODULES
def test_same_day_events_resolve_to_the_largest_cumulative(history):
    """⑦ 같은 날 event 가 여럿이면 누적이 가장 큰 것이 그날의 상태다."""
    text = str(history.history_join("haetdeul"))

    assert "event.target_received_total_krw DESC" in text


@MODULES
def test_the_run_axis_is_carried_into_the_event_lookup(history):
    """🔴 실행 축이 빠지면 다른 실행의 수금이 이 채권에 붙는다."""
    text = str(history.history_join("haetdeul"))

    assert "event.sim_run_id = r.sim_run_id" in text
    assert "event.receivable_id = r.receivable_id" in text


@MODULES
def test_a_receivable_without_events_reads_as_nothing_received(history):
    """⑤ event 가 없는 채권 — 0 으로 읽되 LEFT JOIN 이라 행이 사라지지 않는다."""
    text = str(history.history_join("haetdeul"))
    columns = str(history.history_columns())

    assert "LEFT JOIN LATERAL" in text
    assert "COALESCE(collected.target_received_total_krw, 0)" in columns


@MODULES
def test_outstanding_is_derived_from_the_original_not_the_stored_column(history):
    """🔴 `r.outstanding_amount_krw` 를 읽으면 덮인 값이 나온다."""
    columns = str(history.history_columns())

    assert "r.original_amount_krw - COALESCE(" in columns
    assert "r.outstanding_amount_krw" not in columns
    assert "r.received_amount_krw" not in columns


@MODULES
def test_the_schema_is_quoted_rather_than_interpolated(history):
    """스키마 이름을 문자열로 이어 붙이지 않는다."""
    rendered = str(history.history_join("haetdeul"))

    assert "Identifier('haetdeul')" in rendered
    assert "haetdeul.master_collection_events" not in rendered


# ── 두 벌이 갈리지 않도록 ──────────────────────────────────────────────────


def test_finance_and_sales_share_one_projection_rule():
    """⑨ 같은 채권에 대해 재무와 판매가 다른 미수를 말하면 안 된다."""
    assert str(finance_history.history_join("haetdeul")) == str(
        sales_history.history_join("haetdeul")
    )
    assert str(finance_history.history_columns()) == str(sales_history.history_columns())


@pytest.mark.parametrize(
    ("original", "received"),
    [
        (Decimal(0), Decimal(0)),
        (ORIGINAL, Decimal(0)),
        (ORIGINAL, Decimal(1)),
        (ORIGINAL, ORIGINAL - Decimal(1)),
        (ORIGINAL, ORIGINAL),
        (ORIGINAL, ORIGINAL + Decimal(1)),
    ],
)
def test_finance_and_sales_agree_on_every_status_boundary(original, received):
    assert finance_history.projected_status(
        original_amount_krw=original, received_amount_krw=received
    ) == sales_history.projected_status(
        original_amount_krw=original, received_amount_krw=received
    )


def test_the_two_modules_stay_byte_identical_in_their_rule_bodies():
    """한쪽만 고치는 날을 빨간불로 만든다.

    ⚠️ 머리말(docstring)은 도메인마다 다를 수 있으므로 **규칙 본문만** 대조한다.
    """
    root = pathlib.Path("app")
    marker = "_ZERO = Decimal(0)"
    finance_body = (root / "finance" / "receivable_history.py").read_text(encoding="utf-8")
    sales_body = (root / "sales" / "receivable_history.py").read_text(encoding="utf-8")

    assert finance_body.split(marker, 1)[1] == sales_body.split(marker, 1)[1]
