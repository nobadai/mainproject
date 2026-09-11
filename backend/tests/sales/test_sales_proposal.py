from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.sales.proposal import run_proposal, self_check_scenarios
from app.sales.schemas import SalesProposalInput


def _request(**overrides):
    data = {
        "business_mode": "CONTRACT_PROPOSAL_NEW",
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": 5000,
            "preferred_unit_price_krw": 2000,
            "preferred_delivery_date": "2026-09-10",
            "allow_additional_sourcing": True,
        },
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000}],
                "supply_capacity_by_date": [
                    {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 3000}
                ],
            },
            "delivery_feasibility": {
                "status": "UNRESOLVED",
                "daily_outbound_capacity_kg": 5000,
                "reason_codes": ["OUTBOUND_EVIDENCE_UNRESOLVED"],
            },
        },
    }
    data.update(overrides)
    return SalesProposalInput.model_validate(data)


def _forecast(item="배추"):
    as_of = date(2026, 9, 1)
    return {
        "as_of": as_of.isoformat(),
        "item": item,
        "target_kind": "AUC",
        "unit": "원/kg",
        "current_price": 9999,
        "horizon_days": 1,
        "model_version": "test",
        "generated_at": datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
        "daily": [
            {
                "date": (as_of + timedelta(days=1)).isoformat(),
                "predicted": 9999,
                "lower": 9000,
                "upper": 11000,
            }
        ],
    }


def test_final_core_generates_three_typed_scenarios_without_fake_intermediate(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request())

    assert [s.scenario_type for s in reply.scenarios] == ["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
    assert [s.objective for s in reply.scenarios] == [
        "RISK_DEFENSE",
        "BALANCE",
        "SALES_OPPORTUNITY",
    ]
    assert reply.scenarios[0].quantity_kg == Decimal(3000)
    assert reply.scenarios[0].supply.required_additional_quantity_kg == Decimal(0)
    assert reply.scenarios[2].supply.required_additional_quantity_kg == Decimal(2000)
    assert "ADDITIONAL_SUPPLY_CONTEXT" in reply.scenarios[2].required_validations
    assert reply.scenarios[1].variant_collapsed is True
    assert reply.scenarios[1].variant_collapsed_reason
    assert "DELIVERY_FEASIBILITY_CONTEXT" in reply.missing_capabilities


def test_price_is_null_and_amount_does_not_use_market_or_invented_value(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        user_request={"item": "배추", "requested_quantity_kg": 5000},
        ml_context=_forecast(),
    )
    reply = run_proposal(request)

    assert all(s.unit_price_krw is None and s.sales_amount_krw is None for s in reply.scenarios)
    assert all("PRICE_CONTEXT_REQUIRED" in s.uncertainties for s in reply.scenarios)


def test_refeed_creates_new_lineage_and_keeps_purchase_reference_conditional(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        is_refeed=True,
        feedback_attempt=1,
        feedback={
            "attempt": 1,
            "domain_replies": [
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    "reply_ref": "PUR-1",
                    "runtime_status": "READY",
                    "business_status": "ok",
                    "payload": {"procurable_quantity_kg": 2000, "risks": []},
                }
            ],
            "scenario_feedback": [{"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}],
        },
    )
    reply = run_proposal(request)
    confirmed, aggressive = reply.scenarios[0], reply.scenarios[2]

    assert aggressive.scenario_id == "SALES-001-C-R1"
    assert aggressive.parent_scenario_id == "SALES-001-C"
    assert aggressive.revision == 1
    assert aggressive.conditional_purchase is True
    assert "PUR-1" in aggressive.evidence_refs
    assert "PUR-1" not in confirmed.evidence_refs
    assert confirmed.conditional_purchase is False


def test_second_refeed_attempt_creates_r2_lineage(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(is_refeed=True, feedback_attempt=2))
    scenario = reply.scenarios[0]
    assert scenario.scenario_id == "SALES-001-A-R2"
    assert scenario.parent_scenario_id == "SALES-001-A"
    assert scenario.revision == 2


def test_finance_fail_remains_visible_but_is_not_recommended(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(
        _request(
            feedback={
                "attempt": 1,
                "domain_replies": [
                    {
                        "source_agent": "finance",
                        "capability": "FINANCIAL_VALIDATION",
                        "reply_ref": "FIN-1",
                        "runtime_status": "READY",
                        "business_status": "reject",
                    }
                ],
                "scenario_feedback": [
                    {"scenario_id": "SALES-001-A", "reply_refs": ["FIN-1"]},
                    {"scenario_id": "SALES-001-B", "reply_refs": ["FIN-1"]},
                    {"scenario_id": "SALES-001-C", "reply_refs": ["FIN-1"]},
                ],
            }
        )
    )

    assert all("FINANCE_FAIL" in scenario.risks for scenario in reply.scenarios)
    assert reply.recommended_scenario_id is None
    assert reply.recommendation.recommended_candidate_id is None


def test_self_check_rejects_amount_mutation():
    scenario = run_proposal(_request()).scenarios[0]
    scenario.sales_amount_krw = Decimal(1)
    check = self_check_scenarios([scenario])
    assert check.passed is False
    assert "SALES_AMOUNT_INCONSISTENT" in check.issue_codes


def test_zero_confirmed_supply_is_not_missing_value(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 0},
            "sellable_supply": {
                "status": "READY",
                "supply_capacity_by_date": [
                    {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 0}
                ],
            },
            "delivery_feasibility": {"status": "UNRESOLVED"},
        }
    )
    reply = run_proposal(request)
    assert reply.scenarios[0].supply.confirmed_quantity_kg == Decimal(0)
    assert reply.scenarios[0].supply.required_additional_quantity_kg == Decimal(0)
    assert reply.scenarios[2].supply.required_additional_quantity_kg == Decimal(5000)


@pytest.mark.parametrize(
    "business_mode",
    [
        "CONTRACT_FULFILLMENT",
        "CONTRACT_PROPOSAL_NEW",
        "CONTRACT_PROPOSAL_RENEWAL",
        "SPOT_SALES",
    ],
)
def test_all_sales_business_modes_are_represented_without_implicit_policy(
    monkeypatch, business_mode
):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    extra = {
        "contract_context": {
            "contract_quantity_kg": 5000,
            "contract_unit_price_krw": 2000,
        }
    }
    reply = run_proposal(_request(business_mode=business_mode, **extra))

    assert all(scenario.business_mode == business_mode for scenario in reply.scenarios)
    if business_mode == "CONTRACT_FULFILLMENT":
        # 원계약은 공격안에 남기고, 공급 부족 보수안은 별도 조정안으로 표현한다.
        assert reply.scenarios[2].quantity_kg == Decimal(5000)
        assert reply.scenarios[0].quantity_kg == Decimal(3000)
        assert reply.scenarios[0].sales_decision_axes == ["QUANTITY"]


def test_delivery_date_uses_exact_logistics_vector_not_query_scope_max(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        user_request={
            "item": "배추",
            "requested_quantity_kg": 6000,
            "preferred_delivery_date": "2026-09-10",
        },
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 7000},
            "sellable_supply": {
                "status": "READY",
                "supply_capacity_by_date": [
                    {
                        "date": "2026-09-10",
                        "confirmed_sellable_quantity_kg": 4200,
                        "freshness_unresolved_inbound_quantity_kg": 1800,
                        "uncertainties": ["CONFIRMED_INBOUND_FRESHNESS_UNRESOLVED"],
                    }
                ],
            },
            "delivery_feasibility": {"status": "UNRESOLVED", "daily_outbound_capacity_kg": 5000},
            "evidence_refs": ["LOG-1"],
        },
    )
    scenario = run_proposal(request).scenarios[2]
    assert scenario.supply.confirmed_quantity_kg == Decimal(4200)
    assert scenario.supply.required_additional_quantity_kg == Decimal(1800)
    assert "CONFIRMED_INBOUND_FRESHNESS_UNRESOLVED" in scenario.uncertainties
    assert "LOG-1" in scenario.evidence_refs


def test_missing_delivery_vector_does_not_fall_back_to_scope_max(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 7000},
            "sellable_supply": {"status": "READY", "supply_capacity_by_date": []},
            "delivery_feasibility": {"status": "UNRESOLVED"},
        }
    )
    scenario = run_proposal(request).scenarios[2]
    assert scenario.supply.confirmed_quantity_kg is None
    assert scenario.supply.required_additional_quantity_kg is None
    assert "SELLABLE_SUPPLY_CONTEXT" in scenario.required_validations


def test_no_date_uses_current_inventory_view_without_summing_lots(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        user_request={"item": "배추", "requested_quantity_kg": 8000},
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 9999},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 7000}],
                "lot_constraints": [
                    {"lot_id": "A", "item": "배추", "available_qty_kg": 4000},
                    {"lot_id": "B", "item": "배추", "available_qty_kg": 4000},
                ],
            },
            "delivery_feasibility": {"status": "UNRESOLVED"},
        },
    )
    scenario = run_proposal(request).scenarios[2]
    assert scenario.supply.confirmed_quantity_kg == Decimal(7000)
    assert scenario.supply.required_additional_quantity_kg == Decimal(1000)


@pytest.mark.parametrize(
    ("business_mode", "extra", "reason"),
    [
        ("CONTRACT_FULFILLMENT", {}, "CONTRACT_CONTEXT_REQUIRED"),
        ("CONTRACT_PROPOSAL_RENEWAL", {}, "PREVIOUS_CONTRACT_CONTEXT_REQUIRED"),
    ],
)
def test_contract_modes_without_authoritative_contract_are_input_incomplete(
    monkeypatch, business_mode, extra, reason
):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(business_mode=business_mode, **extra))
    assert reply.status == "INPUT_INCOMPLETE"
    assert reply.scenarios == []
    assert reason in reply.missing_data


@pytest.mark.parametrize(
    ("context_key", "context", "reason"),
    [
        ("logistics_context", {"query_scope": {"item": "무"}}, "LOGISTICS_ITEM_MISMATCH"),
        ("ml_context", _forecast("양파"), "ML_ITEM_MISMATCH"),
    ],
)
def test_item_mismatch_is_input_incomplete(monkeypatch, context_key, context, reason):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(**{context_key: context}))
    assert reply.status == "INPUT_INCOMPLETE"
    assert reason in reply.missing_data


def test_feedback_distribution_uses_reply_refs_and_rejects_unknown_ref(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    valid = _request(
        feedback={
            "domain_replies": [
                {
                    "source_agent": "finance",
                    "capability": "FINANCIAL_VALIDATION",
                    "reply_ref": "FIN-A",
                    "runtime_status": "READY",
                    "business_status": "reject",
                }
            ],
            "scenario_feedback": [{"scenario_id": "SALES-001-A", "reply_refs": ["FIN-A"]}],
        }
    )
    reply = run_proposal(valid)
    trace = next(item for item in reply.decision_trace if item.candidate_id == "SALES-001-A")
    assert trace.finance_verdict == "FAIL"
    assert trace.recommended is False
    assert "FINANCE_FAIL" not in reply.scenarios[1].risks

    invalid = _request(
        feedback={"scenario_feedback": [{"scenario_id": "SALES-001-A", "reply_refs": ["UNKNOWN"]}]}
    )
    invalid_reply = run_proposal(invalid)
    assert invalid_reply.status == "INPUT_INCOMPLETE"
    assert "SCENARIO_FEEDBACK_UNKNOWN_REPLY_REF" in invalid_reply.missing_data


def test_proposal_reply_exposes_state_and_llm_alias(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(is_refeed=True, feedback_attempt=2))
    assert reply.status == "SCENARIOS_GENERATED"
    assert reply.is_refeed is True
    assert reply.feedback_attempt == 2
    assert reply.llm == reply.recommendation


def _purchase_feedback(*, status="ok", fulfillable=None, quantity=1500, risks=None):
    return {
        "domain_replies": [
            {
                "source_agent": "purchase",
                "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                "reply_ref": "PUR-1",
                "runtime_status": "READY",
                "business_status": status,
                "payload": {
                    "fulfillable": fulfillable,
                    "procurable_quantity_kg": quantity,
                    "binding_constraint": None,
                    "expected_purchase_date": None,
                    "expected_arrival_date": None,
                    "risks": risks or [],
                    "rationale": [],
                },
            }
        ],
        "scenario_feedback": [{"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}],
    }


def _resolved_purchase_request(**purchase_kwargs):
    feedback = _purchase_feedback(**purchase_kwargs)
    feedback["domain_replies"].append(
        {
            "source_agent": "finance",
            "capability": "FINANCIAL_VALIDATION",
            "reply_ref": "FIN-1",
            "runtime_status": "READY",
            "business_status": "ok",
            "payload": {"finance_verdict": "PASS"},
        }
    )
    feedback["scenario_feedback"][0]["reply_refs"].append("FIN-1")
    return _request(
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000}],
                "supply_capacity_by_date": [
                    {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 3000}
                ],
            },
            "delivery_feasibility": {"status": "READY"},
        },
        feedback=feedback,
    )


@pytest.mark.parametrize("fulfillable", [True, False, None])
def test_purchase_positive_supply_is_conditional_and_clipped_to_supported_quantity(
    monkeypatch, fulfillable
):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_resolved_purchase_request(fulfillable=fulfillable))
    conservative, aggressive = reply.scenarios[0], reply.scenarios[2]
    assert aggressive.conditional_purchase is True
    assert aggressive.quantity_kg == Decimal(4500)
    assert aggressive.unmet_quantity_kg == Decimal(500)
    assert aggressive.supply.confirmed_quantity_kg == Decimal(3000)
    # Original shortage stays separate from conditional Purchase supply and unmet quantity.
    assert aggressive.supply.required_additional_quantity_kg == Decimal(2000)
    assert aggressive.supply.conditional_quantity_kg == Decimal(1500)
    assert aggressive.status == "CONDITIONAL"
    assert "PUR-1" in aggressive.evidence_refs
    assert "PUR-1" not in conservative.evidence_refs
    assert conservative.conditional_purchase is False


def test_purchase_skipped_zero_is_resolved_but_not_conditional(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(feedback=_purchase_feedback(status="skipped", quantity=0)))
    aggressive = reply.scenarios[2]
    assert aggressive.conditional_purchase is False
    assert aggressive.supply.required_additional_quantity_kg == Decimal(2000)
    assert aggressive.supply.conditional_quantity_kg == Decimal(0)
    assert aggressive.unmet_quantity_kg == Decimal(2000)

    resolved = run_proposal(_resolved_purchase_request(status="skipped", quantity=0))
    trace = next(item for item in resolved.decision_trace if item.candidate_id == "SALES-001-C")
    assert trace.status == "INFEASIBLE"
    assert "ADDITIONAL_SUPPLY_CONTEXT" not in aggressive.required_validations


def test_purchase_risks_are_preserved_without_redefining_business_status(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    raw_risk = "입고일 기준 창고 점유 검사 보류 — 물류 입고 소요일이 미확정입니다"
    reply = run_proposal(
        _request(feedback=_purchase_feedback(fulfillable=None, quantity=1500, risks=[raw_risk]))
    )
    aggressive = reply.scenarios[2]
    assert raw_risk in aggressive.risks
    assert aggressive.conditional_purchase is True
    assert aggressive.domain_replies[0].business_status == "ok"


"""최종 `/proposal` Core 가 **혼자서도** 같은 답을 내는가.

★ 이 파일이 지키는 것.
    · 전달된 입력만으로 돈다 — 계산 중 어느 저장소도 다시 읽지 않는다
    · 같은 입력이면 업무 숫자·판정·계보가 같다 (LLM 이 바뀌어도)
    · 레거시 `/allocation` 흐름을 끌어다 쓰지 않는다
    · Mode 마다 상업조건의 **출발점**이 다르고, 그 출발점을 바꿔치지 않는다

Master 연동 전에도 독립 실행 가능하다는 설계를 실제로 못 박는다.
"""

import ast
import pathlib

import pytest

independence_LOGISTICS = {
    "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
    "sellable_supply": {
        "status": "READY",
        "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000}],
        "supply_capacity_by_date": [
            {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 3000}
        ],
    },
    "delivery_feasibility": {
        "status": "READY",
        "daily_outbound_capacity_kg": 5000,
        "reason_codes": [],
    },
}

independence_CONTRACT = {
    "contract_id": "C-1",
    "partner_id": "P-1",
    "item": "배추",
    "contract_quantity_kg": 4000,
    "contract_unit_price_krw": 1800,
    "contract_delivery_date": "2026-09-10",
    "contract_payment_days": 20,
    "contract_payment_terms_type": "INSTALLMENT",
    "contract_term_days": 90,
    "source_ref": "CONTRACT:C-1",
}


@pytest.fixture(autouse=True)
def independence_deterministic(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


def independence_request(mode="SPOT_SALES", user=None, contract=None, **over):
    data = {
        "business_mode": mode,
        "user_request": {"item": "배추", **(user or {})},
        "logistics_context": independence_LOGISTICS,
    }
    if contract is not None:
        data["contract_context"] = contract
    data.update(over)
    return SalesProposalInput.model_validate(data)


def independence_user(**over):
    base = {
        "requested_quantity_kg": 3000,
        "preferred_unit_price_krw": 2000,
        "preferred_delivery_date": "2026-09-10",
    }
    base.update(over)
    return base


def independence_facts(scenario):
    """업무 숫자·판정·계보만 뽑는다 — 사람이 읽는 문장은 뺀다."""
    return (
        scenario.scenario_id,
        scenario.parent_scenario_id,
        scenario.revision,
        scenario.scenario_type,
        scenario.quantity_kg,
        scenario.unit_price_krw,
        scenario.sales_amount_krw,
        scenario.delivery_date,
        scenario.payment_days,
        scenario.payment_terms_type,
        scenario.source_ref,
        scenario.supply.confirmed_quantity_kg,
        scenario.supply.required_additional_quantity_kg,
        scenario.supply.conditional_quantity_kg,
        scenario.supply.dependency_ref,
        tuple(scenario.required_validations),
        tuple(scenario.evidence_refs),
        scenario.variant_collapsed,
        scenario.variant_collapsed_reason,
        scenario.conditional_purchase,
    )


# ---------------------------------------------------------------------------
# 저장소를 다시 읽지 않는다
# ---------------------------------------------------------------------------


def test_proposal_core_runs_without_touching_any_repository(monkeypatch):
    """🔴 계산 중 저장소를 다시 읽으면 같은 입력이 날마다 다른 답을 낸다.

    Sales DB 접근 함수를 전부 폭발시켜 두고도 Core 가 끝까지 돈다는 것은,
    전달된 입력만으로 계산했다는 뜻이다.
    """

    def _explode(*args, **kwargs):
        raise AssertionError("proposal core must not read a repository")

    for name in ("fetch_one", "fetch_all", "execute_returning_one", "get_db_schema"):
        monkeypatch.setattr(f"app.sales.db.{name}", _explode, raising=True)

    reply = run_proposal(independence_request(user=independence_user()))

    assert reply.status == "SCENARIOS_GENERATED"
    assert len(reply.scenarios) == 3


def test_proposal_module_does_not_import_any_repository():
    """구조로도 확인한다 — Core 는 저장소 모듈을 import 하지 않는다."""
    import app.sales.proposal as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in ("app.sales.db", "app.sales.runs", "app.sales.runs"):
        assert forbidden not in imported, forbidden


# ---------------------------------------------------------------------------
# 레거시와 섞이지 않는다
# ---------------------------------------------------------------------------


def test_proposal_core_does_not_call_the_legacy_allocation_flow():
    """최종 Core 는 레거시 Cycle B 를 끌어다 쓰지 않는다."""
    import app.sales.proposal as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for legacy in ("run_allocation", "run_floor_reply", "_self_check"):
        assert legacy not in called, legacy


def test_proposal_core_does_not_use_the_legacy_external_validation_contract():
    """신규 Domain Reply 정본은 SalesDomainReply 다."""
    import app.sales.proposal as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    assert "ExternalValidationResult" not in imported_names
    assert "SalesAllocationInput" not in imported_names
    assert "SalesDomainReply" in imported_names


# ---------------------------------------------------------------------------
# 결정론 — 같은 입력이면 같은 사실
# ---------------------------------------------------------------------------


def test_same_input_produces_identical_business_facts():
    request = independence_request(user=independence_user())

    first = run_proposal(request)
    second = run_proposal(request)

    assert [independence_facts(s) for s in first.scenarios] == [
        independence_facts(s) for s in second.scenarios
    ]
    assert first.self_check.issue_codes == second.self_check.issue_codes
    assert first.missing_data == second.missing_data
    assert first.missing_capabilities == second.missing_capabilities


def test_determinism_holds_for_a_refeed_run():
    request = independence_request(
        user=independence_user(requested_quantity_kg=5000),
        is_refeed=True,
        feedback_attempt=1,
        feedback={
            "attempt": 1,
            "domain_replies": [
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    "reply_ref": "PUR-1",
                    "runtime_status": "READY",
                    "business_status": "ok",
                    "payload": {"procurable_quantity_kg": 1500, "risks": ["R1"]},
                }
            ],
            "scenario_feedback": [
                {"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}
            ],
        },
    )

    first = run_proposal(request)
    second = run_proposal(request)

    assert [independence_facts(s) for s in first.scenarios] == [
        independence_facts(s) for s in second.scenarios
    ]


def test_business_facts_do_not_depend_on_the_llm(monkeypatch):
    """해석 문장이 달라져도 업무 숫자·판정은 그대로다."""
    request = independence_request(user=independence_user())
    with_llm_off = run_proposal(request)

    monkeypatch.setattr(
        "app.sales.proposal.interpret_candidates",
        lambda candidates, **_kwargs: with_llm_off.llm.model_copy(
            update={"summary": "완전히 다른 문장"}
        ),
    )
    with_other_text = run_proposal(request)

    assert [independence_facts(s) for s in with_llm_off.scenarios] == [
        independence_facts(s) for s in with_other_text.scenarios
    ]


# ---------------------------------------------------------------------------
# Mode 별 상업조건 출발점
# ---------------------------------------------------------------------------


def test_fulfillment_takes_every_commercial_fact_from_the_contract():
    """계약 이행에서 사용자 선호가 계약을 조용히 덮지 않는다."""
    reply = run_proposal(
        independence_request(
            "CONTRACT_FULFILLMENT",
            user=independence_user(
                requested_quantity_kg=9999,
                preferred_unit_price_krw=9999,
                preferred_payment_days=99,
            ),
            contract=independence_CONTRACT,
        )
    )
    scenario = reply.scenarios[1]

    assert scenario.unit_price_krw == Decimal(1800)
    assert scenario.payment_days == 20
    assert scenario.payment_terms_type == "INSTALLMENT"
    assert scenario.contract_term_days == 90
    assert scenario.source_ref == "CONTRACT:C-1"


def test_new_without_price_leaves_it_unknown_and_says_so():
    reply = run_proposal(
        independence_request(
            "CONTRACT_PROPOSAL_NEW",
            user={"requested_quantity_kg": 3000, "preferred_delivery_date": "2026-09-10"},
        )
    )
    scenario = reply.scenarios[1]

    assert scenario.unit_price_krw is None
    assert scenario.sales_amount_krw is None
    assert "PRICE_CONTEXT_REQUIRED" in scenario.uncertainties


def test_new_without_quantity_is_input_incomplete():
    """⚠️ **물류 확정 수량도 없을 때**만 막힌다 (2026-09-11).

    ★★ 종전에는 *"사람이 수량을 안 주면 막는다"* 였다. 그런데 자동 걷기에는 사람이
      없고, 206일 내내 안이 **0건**이었다. 이제 물류가
      `sellable_supply.inventory_by_item` 으로 **확정한 수량**이 있으면 그것을 쓴다.

    🔴 **막는 규율 자체는 그대로다** — 사람도 물류도 수량을 말하지 않으면 여전히
       `PROPOSAL_QUANTITY_REQUIRED` 다. 지어내지 않는다.
    """
    물류_수량없음 = {
        **independence_LOGISTICS,
        "sellable_supply": {
            **independence_LOGISTICS["sellable_supply"],
            "inventory_by_item": [],
        },
    }
    reply = run_proposal(
        independence_request(
            "CONTRACT_PROPOSAL_NEW",
            user={"preferred_unit_price_krw": 2000},
            logistics_context=물류_수량없음,
        )
    )

    assert reply.status == "INPUT_INCOMPLETE"
    assert "PROPOSAL_QUANTITY_REQUIRED" in reply.missing_data
    assert reply.scenarios == []


def test_renewal_takes_unstated_axes_from_the_previous_contract():
    reply = run_proposal(
        independence_request(
            "CONTRACT_PROPOSAL_RENEWAL",
            user={"preferred_unit_price_krw": 2500},
            contract=independence_CONTRACT,
        )
    )
    scenario = reply.scenarios[1]

    # 사용자가 말한 축은 사용자 값, 말하지 않은 축은 이전 계약 값.
    assert scenario.unit_price_krw == Decimal(2500)
    assert scenario.payment_days == 20
    assert scenario.contract_term_days == 90


def test_spot_without_a_contract_does_not_invent_contract_terms():
    reply = run_proposal(independence_request("SPOT_SALES", user=independence_user()))
    scenario = reply.scenarios[1]

    assert scenario.contract_term_days is None
    assert scenario.payment_days is None
    assert scenario.payment_terms_type is None


def test_spot_without_quantity_is_input_incomplete():
    """⚠️ **물류 확정 수량도 없을 때**만 막힌다 (2026-09-11).

    ★★ 종전에는 *"사람이 수량을 안 주면 막는다"* 였다. 그런데 자동 걷기에는 사람이
      없고, 206일 내내 안이 **0건**이었다. 이제 물류가
      `sellable_supply.inventory_by_item` 으로 **확정한 수량**이 있으면 그것을 쓴다.

    🔴 **막는 규율 자체는 그대로다** — 사람도 물류도 수량을 말하지 않으면 여전히
       `PROPOSAL_QUANTITY_REQUIRED` 다. 지어내지 않는다.
    """
    물류_수량없음 = {
        **independence_LOGISTICS,
        "sellable_supply": {
            **independence_LOGISTICS["sellable_supply"],
            "inventory_by_item": [],
        },
    }
    reply = run_proposal(
        independence_request(
            "SPOT_SALES",
            user={"preferred_unit_price_krw": 2000},
            logistics_context=물류_수량없음,
        )
    )

    assert reply.status == "INPUT_INCOMPLETE"
    assert "PROPOSAL_QUANTITY_REQUIRED" in reply.missing_data


# ---------------------------------------------------------------------------
# ML 은 가격 생성기가 아니다
# ---------------------------------------------------------------------------


def independence_forecast(item="배추", predicted=9999):
    return {
        "as_of": "2026-09-01",
        "item": item,
        "target_kind": "AUC",
        "unit": "원/kg",
        "current_price": predicted,
        "horizon_days": 1,
        "model_version": "test",
        "generated_at": "2026-09-01T00:00:00+00:00",
        "daily": [
            {
                "date": "2026-09-02",
                "predicted": predicted,
                "lower": predicted - 1000,
                "upper": predicted + 1000,
            }
        ],
    }


def test_ml_forecast_never_becomes_the_selling_price():
    """🔴 예측가는 근거이지 판매가가 아니다."""
    reply = run_proposal(
        independence_request(
            "SPOT_SALES",
            user={"requested_quantity_kg": 3000, "preferred_delivery_date": "2026-09-10"},
            ml_context=independence_forecast(predicted=9999),
        )
    )
    scenario = reply.scenarios[1]

    assert scenario.unit_price_krw is None
    assert scenario.sales_amount_krw is None
    assert "9999" not in str(scenario.unit_price_krw)
    assert "PRICE_CONTEXT_REQUIRED" in scenario.uncertainties


def test_ml_forecast_does_not_change_a_user_given_price():
    reply = run_proposal(
        independence_request(
            "SPOT_SALES",
            user=independence_user(),
            ml_context=independence_forecast(predicted=9999),
        )
    )

    assert reply.scenarios[1].unit_price_krw == Decimal(2000)


"""A/B/C 의미 · 세 수량 · 계보 · 근거 — 조용히 섞이지 않는가.

★ 이 파일이 지키는 것.
    · 세 수량은 서로 다른 사실이다 (확정 / 부족 / 조건부 확보 가능)
    · 확정 0kg 은 사실이고 확정 미수신은 모름이다
    · 권위 있는 중간값이 없으면 만들지 않고 collapse 로 남긴다
    · 계보는 Sales 가 소유한다 — 외부 문자열에서 만들지 않는다
    · source_ref 와 evidence_refs 는 역할이 다르다

각 검사에는 **뒤집으면 깨지는 역검사**를 함께 둔다 — 임의 비율·평균·0 대체가
들어오면 여기서 걸린다.
"""


import pytest


@pytest.fixture(autouse=True)
def boundaries_deterministic(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


def boundaries_logistics(confirmed, *, date="2026-09-10", item="배추"):
    """confirmed 가 None 이면 그 날짜의 권위 수량 자체를 주지 않는다."""
    capacity = (
        [] if confirmed is None else [{"date": date, "confirmed_sellable_quantity_kg": confirmed}]
    )
    return {
        "query_scope": {"item": item, "max_confirmed_sellable_quantity_kg": 9999},
        "sellable_supply": {
            "status": "READY",
            "inventory_by_item": [{"item": item, "available_qty_kg": 9999}],
            "supply_capacity_by_date": capacity,
        },
        "delivery_feasibility": {
            "status": "READY",
            "daily_outbound_capacity_kg": 9999,
            "reason_codes": [],
        },
    }


def boundaries_request(requested, confirmed, **over):
    data = {
        "business_mode": "SPOT_SALES",
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": requested,
            "preferred_unit_price_krw": 2000,
            "preferred_delivery_date": "2026-09-10",
            "allow_additional_sourcing": True,
        },
        "logistics_context": boundaries_logistics(confirmed),
    }
    data.update(over)
    return SalesProposalInput.model_validate(data)


def boundaries_types(reply):
    return {scenario.scenario_type: scenario for scenario in reply.scenarios}


# ---------------------------------------------------------------------------
# 세 수량 — 공급 매트릭스
# ---------------------------------------------------------------------------


def test_confirmed_above_request_needs_no_additional_supply():
    scenarios = boundaries_types(run_proposal(boundaries_request(1000, 3000)))

    for scenario in scenarios.values():
        assert scenario.supply.required_additional_quantity_kg == Decimal(0)
        assert scenario.supply.additional_supply_required is False


def test_confirmed_equal_to_request_needs_no_additional_supply():
    scenarios = boundaries_types(run_proposal(boundaries_request(3000, 3000)))

    aggressive = scenarios["AGGRESSIVE"]
    assert aggressive.supply.confirmed_quantity_kg == Decimal(3000)
    assert aggressive.supply.required_additional_quantity_kg == Decimal(0)


def test_confirmed_below_request_computes_the_shortfall():
    aggressive = boundaries_types(run_proposal(boundaries_request(5000, 3000)))["AGGRESSIVE"]

    assert aggressive.supply.confirmed_quantity_kg == Decimal(3000)
    assert aggressive.supply.required_additional_quantity_kg == Decimal(2000)
    assert aggressive.supply.additional_supply_required is True


def test_zero_confirmed_supply_is_a_fact_not_a_missing_context():
    """🔴 0kg 확인과 확정 수량 미수신은 다른 사실이다."""
    aggressive = boundaries_types(run_proposal(boundaries_request(100, 0)))["AGGRESSIVE"]

    assert aggressive.supply.confirmed_quantity_kg == Decimal(0)
    assert aggressive.supply.required_additional_quantity_kg == Decimal(100)


def test_absent_confirmed_supply_stays_unknown_and_asks_for_context():
    aggressive = boundaries_types(run_proposal(boundaries_request(100, None)))["AGGRESSIVE"]

    assert aggressive.supply.confirmed_quantity_kg is None
    # 모르는 것을 0 으로 바꾸면 부족량이 100 으로 확정돼 버린다.
    assert aggressive.supply.required_additional_quantity_kg is None
    assert "SELLABLE_SUPPLY_CONTEXT" in aggressive.required_validations


def test_shortfall_is_never_copied_into_conditional_quantity():
    """🔴 '더 필요한 양' 과 '확보 가능하다고 확인된 양' 은 다른 사실이다."""
    aggressive = boundaries_types(run_proposal(boundaries_request(5000, 3000)))["AGGRESSIVE"]

    assert aggressive.supply.required_additional_quantity_kg == Decimal(2000)
    # 매입이 확인해 주기 전까지 조건부는 모름이다.
    assert aggressive.supply.conditional_quantity_kg is None
    assert aggressive.supply.dependency_ref is None


def test_conditional_supply_is_never_summed_into_confirmed():
    reply = run_proposal(
        boundaries_request(
            5000,
            3000,
            is_refeed=True,
            feedback_attempt=1,
            feedback={
                "attempt": 1,
                "domain_replies": [
                    {
                        "source_agent": "purchase",
                        "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                        "reply_ref": "PUR-1",
                        "runtime_status": "READY",
                        "business_status": "ok",
                        "payload": {"procurable_quantity_kg": 2000, "risks": []},
                    }
                ],
                "scenario_feedback": [
                    {"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}
                ],
            },
        )
    )
    supply = boundaries_types(reply)["AGGRESSIVE"].supply

    assert supply.confirmed_quantity_kg == Decimal(3000)
    assert supply.conditional_quantity_kg == Decimal(2000)
    # 합쳤다면 5000 이 됐을 것이다.
    assert supply.confirmed_quantity_kg != Decimal(5000)


# ---------------------------------------------------------------------------
# A/B/C 의미
# ---------------------------------------------------------------------------


def test_conservative_uses_the_authoritative_confirmed_quantity():
    """보수안은 확정 공급량을 쓴다 — 임의 비율을 만들지 않는다."""
    scenarios = boundaries_types(run_proposal(boundaries_request(5000, 3000)))
    conservative = scenarios["CONSERVATIVE"]

    assert conservative.quantity_kg == Decimal(3000)
    assert "QUANTITY" in conservative.sales_decision_axes


@pytest.mark.parametrize("ratio", ["0.8", "0.9", "0.95"])
def test_conservative_never_uses_an_invented_ratio(ratio):
    conservative = boundaries_types(run_proposal(boundaries_request(5000, 3000)))["CONSERVATIVE"]
    invented = Decimal(5000) * Decimal(ratio)

    assert conservative.quantity_kg != invented


def test_balanced_collapses_instead_of_inventing_a_middle_quantity():
    """🔴 확정과 요청 사이의 중간값을 만들 권위 있는 근거가 없다."""
    balanced = boundaries_types(run_proposal(boundaries_request(5000, 3000)))["BALANCED"]

    assert balanced.variant_collapsed is True
    assert balanced.variant_collapsed_reason == "AUTHORITATIVE_INTERMEDIATE_OPTION_UNAVAILABLE"
    # 평균(4000)도, 임의 비율도 아니다.
    assert balanced.quantity_kg == Decimal(5000)
    assert balanced.quantity_kg != Decimal(4000)


def test_collapse_always_carries_a_reason():
    reply = run_proposal(boundaries_request(5000, 3000))

    for scenario in reply.scenarios:
        if scenario.variant_collapsed:
            assert scenario.variant_collapsed_reason


def test_scenario_types_come_from_the_closed_vocabulary():
    """유형 **어휘**는 계약이다 (`ScenarioType` 이 닫힌 Literal).

    ★ 반면 "항상 정확히 세 개" 는 스키마가 강제하지 않는다 —
      `SalesProposalReply.scenarios` 에 길이 제약이 없다. 그래서 개수를 계약처럼
      잠그지 않고, 어휘와 유형 유일성만 고정한다. 개수 정책이 확정되면 그때 넣는다.
    """
    from typing import get_args

    from app.sales.schemas import ScenarioType

    reply = run_proposal(boundaries_request(1000, 3000))
    types = [scenario.scenario_type for scenario in reply.scenarios]

    assert types, "안이 하나도 없으면 이 검사는 아무것도 지키지 못한다"
    assert set(types) <= set(get_args(ScenarioType))
    # 같은 유형이 두 번 나오면 어느 쪽이 그 유형인지 알 수 없다.
    assert len(types) == len(set(types))


def test_scenario_count_is_not_locked_by_the_schema():
    """개수를 계약으로 승격하지 않았다는 사실 자체를 남긴다."""
    from app.sales.schemas import SalesProposalReply

    metadata = SalesProposalReply.model_fields["scenarios"].metadata

    assert not metadata, f"길이 제약이 생겼다면 개수 계약을 다시 판단한다: {metadata}"


def test_converged_scenarios_do_not_pretend_to_be_different_numbers():
    """🔴 값이 같아졌으면 같다고 말한다 — 다른 안인 척 숫자를 벌리지 않는다."""
    reply = run_proposal(boundaries_request(1000, 3000))
    quantities = {s.scenario_type: s.quantity_kg for s in reply.scenarios}

    # 확정 공급이 요청보다 많아 어느 유형도 수량을 좁힐 근거가 없다.
    assert set(quantities.values()) == {Decimal(1000)}
    for scenario in reply.scenarios:
        if scenario.quantity_kg == Decimal(1000) and scenario.scenario_type != "AGGRESSIVE":
            # 같은 숫자를 내놓는 안은 그 사실을 collapse 로 밝힌다.
            assert scenario.variant_collapsed is True


def test_aggressive_keeps_the_requested_quantity_without_clipping():
    aggressive = boundaries_types(run_proposal(boundaries_request(5000, 3000)))["AGGRESSIVE"]

    assert aggressive.quantity_kg == Decimal(5000)
    assert "ADDITIONAL_SUPPLY_CONTEXT" in aggressive.required_validations


# ---------------------------------------------------------------------------
# 계보 — Sales 가 소유한다
# ---------------------------------------------------------------------------


def boundaries_refeed(attempt, replies=(), feedback_scenarios=()):
    return boundaries_request(
        5000,
        3000,
        is_refeed=True,
        feedback_attempt=attempt,
        feedback={
            "attempt": attempt,
            "domain_replies": list(replies),
            "scenario_feedback": list(feedback_scenarios),
        },
    )


def test_initial_run_has_no_parent_and_revision_zero():
    reply = run_proposal(boundaries_request(5000, 3000))

    for scenario in reply.scenarios:
        assert scenario.revision == 0
        assert scenario.parent_scenario_id is None
        assert "-R" not in scenario.scenario_id


@pytest.mark.parametrize("attempt", [1, 2])
def test_refeed_lineage_is_generated_by_sales(attempt):
    reply = run_proposal(boundaries_refeed(attempt))

    for scenario in reply.scenarios:
        assert scenario.revision == attempt
        assert scenario.scenario_id.endswith(f"-R{attempt}")
        assert scenario.parent_scenario_id == scenario.scenario_id.rsplit("-R", 1)[0]
        assert scenario.parent_scenario_id.startswith("SALES-001-")


def test_lineage_is_not_derived_from_an_external_reply_string():
    """외부 회신의 ref 문자열이 계보를 만들지 않는다."""
    reply = run_proposal(
        boundaries_refeed(
            1,
            replies=[
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    # 계보처럼 생긴 문자열을 일부러 넣는다.
                    "reply_ref": "SALES-001-C-R9",
                    "runtime_status": "READY",
                    "business_status": "ok",
                    "payload": {"procurable_quantity_kg": 100, "risks": []},
                }
            ],
            feedback_scenarios=[
                {"scenario_id": "SALES-001-C", "reply_refs": ["SALES-001-C-R9"]}
            ],
        )
    )
    aggressive = boundaries_types(reply)["AGGRESSIVE"]

    # 회신 ref 를 파싱해 R9 을 만들지 않는다 — 회차는 Sales 의 attempt 가 정한다.
    assert aggressive.revision == 1
    assert aggressive.scenario_id == "SALES-001-C-R1"
    assert aggressive.supply.dependency_ref == "SALES-001-C-R9"


def test_self_check_passes_for_a_clean_initial_run():
    reply = run_proposal(boundaries_request(5000, 3000))

    assert reply.self_check.passed is True, reply.self_check.issue_codes


# ---------------------------------------------------------------------------
# source_ref 와 evidence_refs 는 역할이 다르다
# ---------------------------------------------------------------------------

boundaries_CONTRACT = {
    "contract_id": "C-1",
    "partner_id": "P-1",
    "item": "배추",
    "contract_quantity_kg": 4000,
    "contract_unit_price_krw": 1800,
    "contract_delivery_date": "2026-09-10",
    "contract_payment_days": 20,
    "contract_term_days": 90,
    "source_ref": "CONTRACT:C-1",
}


def test_source_ref_is_not_the_first_evidence_ref():
    """🔴 배열 첫 번째를 고르는 것은 근거가 아니라 우연이다."""
    logistics = boundaries_logistics(3000)
    # 잘못 고를 수 있는 후보를 일부러 둔다.
    logistics["evidence_refs"] = ["LOG-EV-1", "LOG-EV-2"]

    reply = run_proposal(
        SalesProposalInput.model_validate(
            {
                "business_mode": "SPOT_SALES",
                "user_request": {
                    "item": "배추",
                    "requested_quantity_kg": 3000,
                    "preferred_unit_price_krw": 2000,
                    "preferred_delivery_date": "2026-09-10",
                },
                "logistics_context": logistics,
            }
        )
    )
    scenario = reply.scenarios[2]

    # 사용자 ref 를 안 줬으므로 출처는 없다. 보조 근거가 있어도 승격되지 않는다.
    assert "LOG-EV-1" in scenario.evidence_refs
    assert scenario.source_ref is None
    assert scenario.source_ref != scenario.evidence_refs[0]


def test_evidence_refs_are_broader_than_the_single_source():
    reply = run_proposal(
        SalesProposalInput.model_validate(
            {
                "business_mode": "CONTRACT_FULFILLMENT",
                "user_request": {"item": "배추"},
                "contract_context": boundaries_CONTRACT,
                "logistics_context": boundaries_logistics(4000),
            }
        )
    )
    scenario = reply.scenarios[1]

    assert scenario.source_ref == "CONTRACT:C-1"
    # 계약은 출처이자 근거지만, 근거 목록은 그보다 넓을 수 있다.
    assert "CONTRACT:C-1" in scenario.evidence_refs


def test_evidence_refs_are_deduplicated_and_carry_reply_refs():
    reply = run_proposal(
        boundaries_refeed(
            1,
            replies=[
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    "reply_ref": "PUR-1",
                    "runtime_status": "READY",
                    "business_status": "ok",
                    "payload": {"procurable_quantity_kg": 100, "risks": []},
                }
            ],
            feedback_scenarios=[{"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}],
        )
    )
    aggressive = boundaries_types(reply)["AGGRESSIVE"]

    assert "PUR-1" in aggressive.evidence_refs
    assert len(aggressive.evidence_refs) == len(set(aggressive.evidence_refs))
