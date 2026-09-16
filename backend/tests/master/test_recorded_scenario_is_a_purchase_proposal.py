"""**실매입 기록값 사본은 부서가 읽는 매입 제안 모양으로 간다** (2026-09-16 실측).

```text
실측  SIM-TEST-PURREC-0916 · 01-05 배추 · 300kg 270,000원 기록
      POST …/purchase-record → 422
      「기록값으로 다시 검증했더니 통과하지 못해 기록하지 않았습니다 (FAILED):
        재검증에서 막혔다: finance(ERROR/skipped), inventory(RUNTIME_NOT_READY/skipped)」

      finance    payload {"validation_errors": ["scenarios.0"]}
      inventory  missing_data ["purchase_proposal"]
```

판정이 나쁜 것이 아니라 **두 부서가 payload 를 못 읽었다.** 둘 다 매입 계약
(`app/purchase_agent/schemas.py` `PurchaseProposal`)으로 되살리는데, 기록값 사본이
`sourcing_plan[].grade_unit_price` 를 선정안 값 그대로 두어 `Scenario`
`validate_quadruple_match` 의 「`total_amount_krw == Σ(qty_kg × grade_unit_price)`」
가 깨졌다.

🔴 **이 판은 대역으로 안 잰다.** 기존 재검증 검사
(`test_procurement_revalidation.py`)의 안은 `strategy_type` · `rationale` ·
`grade_unit_price` 가 없는 **부분 모양**이라, 부서 모델로 파싱하면 그것부터 떨어진다.
그래서 여기서는 **실제 계약 모델을 임포트해** 오류 0 을 잠근다.

⚠️ **DB · LLM · 부서 실행을 안 탄다.** 만드는 payload 를 계약 모델로 검증할 뿐이다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.master.commitment import RecordedLeg
from app.master.purchase_record import recorded_scenario
from app.master.revalidation import procurement_validation_payload
from app.purchase_agent.schemas import PurchaseProposal

기준일 = date(2026, 1, 5)


def _안(*, 분할: bool) -> dict[str, Any]:
    """선정안 하나. **매입이 실제로 내는 모양이다** — 계약 필수 칸이 전부 있다."""
    안: dict[str, Any] = {
        "label": "보수",
        "strategy_type": "quantity",
        "coverage_days": 2,
        "total_qty_kg": 400,
        "total_amount_krw": 400_000,
        "max_price": 1_200,
        "cut_unit_price": 1_200,
        "margin_warning": False,
        "expected_margin_rate": 0.2,
        "split_plan": [
            {
                "seq": 1,
                "date": "2026-01-05",
                "qty_kg": 400,
                "amount_krw": 400_000,
                "expected_arrival_date": "2026-01-06",
            }
        ],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": 400, "grade_unit_price": 1_000}
        ],
        "rationale": [
            {
                "source": "예측",
                "claim": "확정수요가 는다",
                "ref_id": "FC-1",
                "evidence_grade": "SIM_FIXED",
                "evidence_detail": "예측 원문",
            }
        ],
        "risks": [],
    }
    if not 분할:
        return 안
    안["split_plan"] = [
        {
            "seq": 1,
            "date": "2026-01-05",
            "qty_kg": 200,
            "amount_krw": 200_000,
            "expected_arrival_date": "2026-01-06",
        },
        {
            "seq": 2,
            "date": "2026-01-07",
            "qty_kg": 200,
            "amount_krw": 200_000,
            "expected_arrival_date": "2026-01-08",
        },
    ]
    안["payment_schedule"] = [
        {
            "seq": 1,
            "purchase_date": "2026-01-05",
            "payment_date": "2026-01-05",
            "qty_kg": 200,
            "amount_krw": 200_000,
            "amount_max_krw": 240_000,
            "basis": "as_of_unit_price",
        },
        {
            "seq": 2,
            "purchase_date": "2026-01-07",
            "payment_date": "2026-01-07",
            "qty_kg": 200,
            "amount_krw": 200_000,
            "amount_max_krw": 240_000,
            "basis": "as_of_unit_price",
        },
    ]
    return 안


def _판정부() -> dict[str, Any]:
    """원 실행 응답의 `judgment` — 제안에서 `scenarios` 를 뺀 최상위."""
    return {
        "meta": {"as_of": 기준일.isoformat(), "item": "배추", "agent_version": "v1.4"},
        "confidence": "high",
        "situation": "stable",
        "context_docs_used": [],
        "rejected_reasons": [],
    }


def _제안(사본: dict[str, Any]) -> PurchaseProposal:
    """**부서가 하는 그대로** 되살린다 — 봉투 전용 키를 버리고 계약 모델로 검증.

    `finance/adapter._purchase_proposal` · `logistics/adapter._as_proposal` 이 둘 다
    이 두 줄이다. 실패하면 재무는 `validation_errors`, 물류는
    `missing_data=["purchase_proposal"]` 로 답한다.
    """
    payload = procurement_validation_payload(_판정부(), 사본)
    fields = PurchaseProposal.model_fields
    return PurchaseProposal.model_validate(
        {key: value for key, value in payload.items() if key in fields}
    )


def test_원_선정안은_원래_읽힌다() -> None:
    """기준선 — 손대기 전 안은 부서가 읽는다. 이게 깨지면 픽스처가 틀린 것이다."""
    assert _제안(_안(분할=False)).scenarios[0].label == "보수"


def test_기록값_사본이_계약으로_파싱된다() -> None:
    """🔴 실측 그대로: 300kg · 270,000원 · 등급 특."""
    legs = (
        RecordedLeg(
            seq=1,
            qty_kg=300,
            amount_krw=270_000,
            purchase_date=기준일,
            arrival_date=date(2026, 1, 6),
        ),
    )
    사본 = recorded_scenario(
        _안(분할=False), legs=legs, grade="특", purchase_payment_days=0
    )

    안 = _제안(사본).scenarios[0]

    assert 안.total_qty_kg == 300
    assert 안.total_amount_krw == 270_000
    assert [(줄.seq, 줄.qty_kg, 줄.amount_krw, 줄.date) for 줄 in 안.split_plan] == [
        (1, 300, 270_000, 기준일)
    ]
    assert 안.split_plan[0].expected_arrival_date == date(2026, 1, 6)
    # 🔴 빠져 있던 칸. 기록 등급 · 기록 총량 · 기록 총액에서 나온 단가다.
    assert [(줄.grade, 줄.qty_kg, 줄.grade_unit_price) for 줄 in 안.sourcing_plan] == [
        ("특", 300, 900)
    ]
    assert 안.sourcing_plan[0].market == "가락"


def test_원_안을_안_건드린다() -> None:
    """사본이다 — 원본 `sourcing_plan` 이 그대로 남아야 같은 안을 두 번 쓸 수 있다."""
    원본 = _안(분할=False)
    recorded_scenario(
        원본,
        legs=(
            RecordedLeg(
                seq=1,
                qty_kg=300,
                amount_krw=270_000,
                purchase_date=기준일,
                arrival_date=date(2026, 1, 6),
            ),
        ),
        grade="특",
        purchase_payment_days=0,
    )
    assert 원본["sourcing_plan"] == [
        {"market": "가락", "grade": "상", "qty_kg": 400, "grade_unit_price": 1_000}
    ]


def test_정수_단가로_안_떨어져도_총액이_그대로다() -> None:
    """🔴 **금액을 반올림하지 않는다.** 기록 총액은 사람이 낸 돈이고 그것이 정본이다.

    271,000 ÷ 300 = 903.33… 이라 한 줄로는 계약(정수 원/kg)을 못 만족한다. 나머지를
    한 줄 더 얹어 **합이 정확히 271,000** 이 되게 한다.
    """
    legs = (
        RecordedLeg(
            seq=1,
            qty_kg=300,
            amount_krw=271_000,
            purchase_date=기준일,
            arrival_date=date(2026, 1, 6),
        ),
    )
    안 = _제안(
        recorded_scenario(_안(분할=False), legs=legs, grade="특", purchase_payment_days=0)
    ).scenarios[0]

    assert 안.total_amount_krw == 271_000
    assert sum(줄.qty_kg for 줄 in 안.sourcing_plan) == 300
    assert sum(줄.qty_kg * 줄.grade_unit_price for 줄 in 안.sourcing_plan) == 271_000
    assert {줄.grade for 줄 in 안.sourcing_plan} == {"특"}


def test_분할_기록도_계약으로_파싱된다() -> None:
    """회차가 둘이면 `payment_schedule` 도 기록값을 따라간다 (N5=2)."""
    legs = (
        RecordedLeg(
            seq=1,
            qty_kg=150,
            amount_krw=150_000,
            purchase_date=기준일,
            arrival_date=date(2026, 1, 6),
        ),
        RecordedLeg(
            seq=2,
            qty_kg=250,
            amount_krw=260_000,
            purchase_date=date(2026, 1, 8),
            arrival_date=date(2026, 1, 9),
        ),
    )
    안 = _제안(
        recorded_scenario(_안(분할=True), legs=legs, grade="특", purchase_payment_days=2)
    ).scenarios[0]

    assert 안.total_qty_kg == 400
    assert 안.total_amount_krw == 410_000
    assert [(줄.seq, 줄.qty_kg, 줄.amount_krw) for 줄 in 안.split_plan] == [
        (1, 150, 150_000),
        (2, 250, 260_000),
    ]
    지급 = 안.payment_schedule
    assert 지급 is not None
    assert [(줄.purchase_date, 줄.payment_date, 줄.qty_kg, 줄.amount_krw) for 줄 in 지급] == [
        (기준일, date(2026, 1, 7), 150, 150_000),
        (date(2026, 1, 8), date(2026, 1, 10), 250, 260_000),
    ]
    # 재무가 `amount_max_krw == qty_kg × max_price` 를 검사한다.
    assert [줄.amount_max_krw for 줄 in 지급] == [150 * 1_200, 250 * 1_200]
    assert sum(줄.qty_kg * 줄.grade_unit_price for 줄 in 안.sourcing_plan) == 410_000


def test_소수점_수량은_통과시키지_않는다() -> None:
    """🔴 계약의 수량 칸이 정수다 — **반올림해 통과시키지 않는다.**

    기록값과 다른 값이 검증을 지나는 것보다 못 적는 것이 낫다. 실패는 부서의
    `validation_errors` 로 사람에게 그대로 나간다.
    """
    legs = (
        RecordedLeg(
            seq=1,
            qty_kg=300.5,
            amount_krw=270_000,
            purchase_date=기준일,
            arrival_date=date(2026, 1, 6),
        ),
    )
    사본 = recorded_scenario(_안(분할=False), legs=legs, grade="특", purchase_payment_days=0)
    with pytest.raises(ValidationError):
        _제안(사본)
