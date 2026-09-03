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
from decimal import Decimal

import pytest

from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput

_LOGISTICS = {
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

_CONTRACT = {
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
def _deterministic(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


def _request(mode="SPOT_SALES", user=None, contract=None, **over):
    data = {
        "business_mode": mode,
        "user_request": {"item": "배추", **(user or {})},
        "logistics_context": _LOGISTICS,
    }
    if contract is not None:
        data["contract_context"] = contract
    data.update(over)
    return SalesProposalInput.model_validate(data)


def _user(**over):
    base = {
        "requested_quantity_kg": 3000,
        "preferred_unit_price_krw": 2000,
        "preferred_delivery_date": "2026-09-10",
    }
    base.update(over)
    return base


def _facts(scenario):
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

    reply = run_proposal(_request(user=_user()))

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

    for forbidden in ("app.sales.db", "app.sales.run_repository", "app.sales.service"):
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
    request = _request(user=_user())

    first = run_proposal(request)
    second = run_proposal(request)

    assert [_facts(s) for s in first.scenarios] == [_facts(s) for s in second.scenarios]
    assert first.self_check.issue_codes == second.self_check.issue_codes
    assert first.missing_data == second.missing_data
    assert first.missing_capabilities == second.missing_capabilities


def test_determinism_holds_for_a_refeed_run():
    request = _request(
        user=_user(requested_quantity_kg=5000),
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

    assert [_facts(s) for s in first.scenarios] == [_facts(s) for s in second.scenarios]


def test_business_facts_do_not_depend_on_the_llm(monkeypatch):
    """해석 문장이 달라져도 업무 숫자·판정은 그대로다."""
    request = _request(user=_user())
    with_llm_off = run_proposal(request)

    monkeypatch.setattr(
        "app.sales.proposal.interpret_candidates",
        lambda candidates: with_llm_off.llm.model_copy(
            update={"summary": "완전히 다른 문장"}
        ),
    )
    with_other_text = run_proposal(request)

    assert [_facts(s) for s in with_llm_off.scenarios] == [
        _facts(s) for s in with_other_text.scenarios
    ]


# ---------------------------------------------------------------------------
# Mode 별 상업조건 출발점
# ---------------------------------------------------------------------------


def test_fulfillment_takes_every_commercial_fact_from_the_contract():
    """계약 이행에서 사용자 선호가 계약을 조용히 덮지 않는다."""
    reply = run_proposal(
        _request(
            "CONTRACT_FULFILLMENT",
            user=_user(
                requested_quantity_kg=9999,
                preferred_unit_price_krw=9999,
                preferred_payment_days=99,
            ),
            contract=_CONTRACT,
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
        _request(
            "CONTRACT_PROPOSAL_NEW",
            user={"requested_quantity_kg": 3000, "preferred_delivery_date": "2026-09-10"},
        )
    )
    scenario = reply.scenarios[1]

    assert scenario.unit_price_krw is None
    assert scenario.sales_amount_krw is None
    assert "PRICE_CONTEXT_REQUIRED" in scenario.uncertainties


def test_new_without_quantity_is_input_incomplete():
    reply = run_proposal(
        _request("CONTRACT_PROPOSAL_NEW", user={"preferred_unit_price_krw": 2000})
    )

    assert reply.status == "INPUT_INCOMPLETE"
    assert "PROPOSAL_QUANTITY_REQUIRED" in reply.missing_data
    assert reply.scenarios == []


def test_renewal_takes_unstated_axes_from_the_previous_contract():
    reply = run_proposal(
        _request(
            "CONTRACT_PROPOSAL_RENEWAL",
            user={"preferred_unit_price_krw": 2500},
            contract=_CONTRACT,
        )
    )
    scenario = reply.scenarios[1]

    # 사용자가 말한 축은 사용자 값, 말하지 않은 축은 이전 계약 값.
    assert scenario.unit_price_krw == Decimal(2500)
    assert scenario.payment_days == 20
    assert scenario.contract_term_days == 90


def test_spot_without_a_contract_does_not_invent_contract_terms():
    reply = run_proposal(_request("SPOT_SALES", user=_user()))
    scenario = reply.scenarios[1]

    assert scenario.contract_term_days is None
    assert scenario.payment_days is None
    assert scenario.payment_terms_type is None


def test_spot_without_quantity_is_input_incomplete():
    reply = run_proposal(_request("SPOT_SALES", user={"preferred_unit_price_krw": 2000}))

    assert reply.status == "INPUT_INCOMPLETE"
    assert "PROPOSAL_QUANTITY_REQUIRED" in reply.missing_data


# ---------------------------------------------------------------------------
# ML 은 가격 생성기가 아니다
# ---------------------------------------------------------------------------


def _forecast(item="배추", predicted=9999):
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
        _request(
            "SPOT_SALES",
            user={"requested_quantity_kg": 3000, "preferred_delivery_date": "2026-09-10"},
            ml_context=_forecast(predicted=9999),
        )
    )
    scenario = reply.scenarios[1]

    assert scenario.unit_price_krw is None
    assert scenario.sales_amount_krw is None
    assert "9999" not in str(scenario.unit_price_krw)
    assert "PRICE_CONTEXT_REQUIRED" in scenario.uncertainties


def test_ml_forecast_does_not_change_a_user_given_price():
    reply = run_proposal(
        _request("SPOT_SALES", user=_user(), ml_context=_forecast(predicted=9999))
    )

    assert reply.scenarios[1].unit_price_krw == Decimal(2000)
