"""재무 판매 검증이 **결정론으로 계산되는 조정안**을 낸다.

🔴 **이 조정안 하나가 되먹임의 조건이다.** 마스터는 *"통과 후보가 없고 부서가 낸
  대안도 없으면 다시 묻지 않는다"* (`sales_flow._run`). 재무가 `[]` 만 돌려주던
  동안 되먹임은 구조적으로 한 번도 돌 수 없었다 — 실측에서 판매 요청 9,937 건의
  `feedback_attempt` 가 전부 0 이었다.

★ **억지로 만들지 않는다.** 계산할 수 없거나 근거 ref 가 없으면 예전처럼 조정 없음이다.
"""

from __future__ import annotations

from app.finance.capabilities.sales import CREDIT_LIMIT_EXCEEDED, build_sales_adjustments


def _payload(**over) -> dict[str, object]:
    base: dict[str, object] = {
        "scenario_id": "SALES-001-B",
        "status": "EVALUATED",
        "finance_verdict": "FAIL",
        "reason_codes": [CREDIT_LIMIT_EXCEEDED],
        "max_finance_allowed_amount_krw": 6_000_000,
        "financial_summary": {"recalculated_sales_amount_krw": 14_500_000},
        "evidence_refs": ["RCV-1001"],
    }
    base.update(over)
    return base


def test_여신_초과에는_금액_상한을_대안으로_낸다():
    """한도 - 현재 채권 = 지금 더 팔 수 있는 최대 금액. 재무가 이미 센 값이다."""
    adjustments = build_sales_adjustments(_payload())

    assert len(adjustments) == 1
    assert adjustments[0]["target_value"] == 6_000_000
    assert adjustments[0]["unit"] == "KRW"
    assert adjustments[0]["reason"] == CREDIT_LIMIT_EXCEEDED
    assert adjustments[0]["scenario_labels"] == ["SALES-001-B"]


def test_근거가_없으면_조정도_없다():
    """🔴 따라가면 아무 데도 닿지 않는 조정은 권위 있는 대안이 아니다."""
    assert build_sales_adjustments(_payload(evidence_refs=[])) == []


def test_상한이_제안을_이미_덮으면_줄일_것이_없다():
    assert build_sales_adjustments(_payload(max_finance_allowed_amount_krw=20_000_000)) == []


def test_여신_초과가_아니면_조정을_만들지_않는다():
    """마진이 안 서는 것은 금액 상한으로 풀리는 문제가 아니다."""
    assert build_sales_adjustments(_payload(reason_codes=["SALES_MARGIN_BELOW_MINIMUM"])) == []


def test_상한을_못_세면_조정을_만들지_않는다():
    """한도를 못 읽은 실행에서 0 을 상한으로 내면 **모든 판매가 막힌다.**"""
    assert build_sales_adjustments(_payload(max_finance_allowed_amount_krw=None)) == []


def test_제안_금액을_모르면_그래도_상한을_낸다():
    """금액을 못 읽은 것과 상한이 넉넉한 것은 다르다 — 상한은 그대로 사실이다."""
    adjustments = build_sales_adjustments(_payload(financial_summary=None))

    assert len(adjustments) == 1
    assert adjustments[0]["target_value"] == 6_000_000


def test_조정값을_재무가_다시_세지_않는다():
    """★ `max_finance_allowed_amount_krw` 를 그대로 옮긴다 — 다시 세면 주인이 둘이 된다."""
    adjustments = build_sales_adjustments(_payload(max_finance_allowed_amount_krw=1234))

    assert adjustments[0]["target_value"] == 1234
