from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.finance.schemas import FinanceReviewRequest, FinanceReviewResponse


@pytest.fixture
def purchase_agent_mock_output():
    return {
        "meta": {
            "as_of": "2026-08-20",
            "item": "배추",
            "agent_version": "v0.3",
            "is_refeed": False,
            "feedback_attempt": 0,
        },
        "scenarios": [
            {
                "label": "기본",
                "total_quantity_ton": 18,
                "max_price": 850,
                "timing": "today",
                "split_plan": [
                    {"seq": 1, "date": "2026-08-20", "quantity_ton": 12},
                    {"seq": 2, "date": "2026-08-23", "quantity_ton": 6},
                ],
                "sourcing_plan": [
                    {
                        "market": "가락",
                        "grade": "상",
                        "quantity_ton": 10,
                        "unit_price": 850,
                    },
                    {
                        "market": "가락",
                        "grade": "중",
                        "quantity_ton": 8,
                        "unit_price": 720,
                    },
                ],
                "expected_margin_rate": 0.12,
                "expected_cost": 14_260_000,
                "rationale": [{"source": "예측", "claim": "2주 후 +14%, 신뢰구간 ±4%"}],
                "risks": ["중품 8톤은 잔여신선도 6일 내 소진 필요"],
            }
        ],
    }


def review_request(mock_output):
    return {
        "proposal_id": "proposal-001",
        "scenario_id": "scenario-001",
        "purchase_meta": mock_output["meta"],
        "scenario": mock_output["scenarios"][0],
    }


def test_request_accepts_purchase_agent_contract(purchase_agent_mock_output):
    request = FinanceReviewRequest.model_validate(review_request(purchase_agent_mock_output))

    assert request.purchase_meta.item == "배추"
    assert request.scenario.total_quantity_ton == 18
    assert request.scenario.split_plan[1].quantity_ton == 6
    assert request.scenario.expected_cost == 14_260_000


def test_request_preserves_mismatched_expected_cost_for_later_finance_validation(
    purchase_agent_mock_output,
):
    mismatch_output = deepcopy(purchase_agent_mock_output)
    mismatch_output["scenarios"][0]["expected_cost"] = 13_640_000

    request = FinanceReviewRequest.model_validate(review_request(mismatch_output))

    assert request.scenario.expected_cost == 13_640_000


def test_request_does_not_accept_finance_state(purchase_agent_mock_output):
    payload = review_request(purchase_agent_mock_output)
    payload["current_cash"] = 100_000_000

    with pytest.raises(ValidationError):
        FinanceReviewRequest.model_validate(payload)


def test_request_rejects_missing_contract_field(purchase_agent_mock_output):
    payload = review_request(purchase_agent_mock_output)
    del payload["scenario"]["expected_cost"]

    with pytest.raises(ValidationError):
        FinanceReviewRequest.model_validate(payload)


def test_response_accepts_finance_contract():
    response = FinanceReviewResponse.model_validate(
        {
            "proposal_id": "proposal-001",
            "scenario_id": "scenario-001",
            "verdict": "conditional",
            "max_feasible_amount_krw": 12_000_000,
            "hard_constraints": ["가용 자금 한도 이내"],
            "soft_warnings": ["현금 여유 감소"],
            "reasoning": ["제안 금액이 현재 허용 한도를 초과함"],
            "evidences": [{"source": "finance_state", "claim": "가용 한도 1,200만원"}],
            "suggested_adjustment": {
                "axis": "amount",
                "description": "매입 금액을 1,200만원 이하로 조정",
                "evidences": [{"source": "finance_state", "claim": "가용 한도 1,200만원"}],
            },
        }
    )

    assert response.agent == "finance"
    assert response.suggested_adjustment is not None
    assert response.suggested_adjustment.axis == "amount"


@pytest.mark.parametrize("verdict", ["approve", "pending", ""])
def test_response_rejects_unknown_verdict(verdict):
    with pytest.raises(ValidationError):
        FinanceReviewResponse.model_validate(
            {
                "proposal_id": "proposal-001",
                "scenario_id": "scenario-001",
                "verdict": verdict,
                "max_feasible_amount_krw": 0,
                "hard_constraints": [],
                "soft_warnings": [],
                "reasoning": [],
                "evidences": [],
                "suggested_adjustment": None,
            }
        )


def test_suggested_adjustment_rejects_non_amount_axis():
    with pytest.raises(ValidationError):
        FinanceReviewResponse.model_validate(
            {
                "proposal_id": "proposal-001",
                "scenario_id": "scenario-001",
                "verdict": "reject",
                "max_feasible_amount_krw": 0,
                "hard_constraints": [],
                "soft_warnings": [],
                "reasoning": [],
                "evidences": [],
                "suggested_adjustment": {
                    "axis": "quantity",
                    "description": "수량 조정",
                    "evidences": [],
                },
            }
        )
