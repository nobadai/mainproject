"""계약 테스트와 mock 테스트가 공유하는 정상 제안 픽스처.

두 파일이 이 값을 함께 본다. ``test_mocks.py``가 *"픽스처의 sourcing 단가가 당일 mock
시세에 실재하는가"*(규칙 4)를 검사하는데, 거기서 숫자를 다시 타이핑하면 검사 대상과
기준이 같은 손에서 나와 아무것도 증명하지 못한다.

숫자는 IO명세 §2 / 상세설계 §5의 예시 JSON과 일치한다 — 예시 JSON은 mock 스펙이자 실제
산출물 계약이다(IO명세 §3 "삼위일체").
"""

AS_OF = "2026-08-21"


def _proposal() -> dict:
    """사중 일치를 만족하는 정상 제안.

    수량: 4500 == 4500(split) == 3000 + 1500(sourcing)
    금액: 3000 x 1650 + 1500 x 1450 = 4,950,000 + 2,175,000 = 7,125,000
          (kg x 원/kg = 원 — 단위가 맞아떨어져 변환 계수가 없다)
    """
    return {
        "meta": {
            "as_of": AS_OF,
            "item": "배추",
            "agent_version": "v1.1",
            "is_refeed": False,
            "feedback_attempt": 0,
        },
        "scenarios": [
            {
                "label": "기본",
                "strategy_type": "quantity",
                "coverage_days": 5,
                "total_qty_kg": 4500,
                "total_amount_krw": 7125000,
                "max_price": 1750,
                "margin_warning": False,
                "split_plan": [{"seq": 1, "date": AS_OF, "qty_kg": 4500}],
                "sourcing_plan": [
                    {
                        "market": "가락",
                        "grade": "상",
                        "qty_kg": 3000,
                        "grade_unit_price": 1650,
                    },
                    {
                        "market": "가락",
                        "grade": "중",
                        "qty_kg": 1500,
                        "grade_unit_price": 1450,
                    },
                ],
                "expected_margin_rate": 0.30,
                "rationale": [
                    {
                        "source": "예측",
                        "claim": "2주 후 +14%, ±3%",
                        "ref_id": "FC-K-0821",
                        "evidence_grade": "OFFICIAL",
                        "evidence_detail": "ML 경락가 예측 q50",
                    }
                ],
                "risks": ["중품 1,500kg은 잔여신선도 6일 내 소진 필요"],
            }
        ],
        "confidence": "high",
        "situation": "stable",
        "context_docs_used": ["DOC-3"],
        "rejected_reasons": [],
    }
