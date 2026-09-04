"""Sales 최종 Proposal 이 재무 검증까지 **실제 함수로** 닿는가.

    run_proposal            영업이 자기 안을 세운다
      → build_financial_validation_request   자기 사실을 재무 이름으로 옮긴다
      → parse_sales_validation_input         재무가 자기 계약으로 읽는다
      → run_sales_validation                 재무가 자기 규칙으로 판정한다

★ 이 파일이 지키는 것.
    · 마스터의 rename 로직 없이도 두 계약이 맞물린다
    · 금액은 **옮겨지기만** 한다 — 영업이 고쳐 보내지 않고 재무가 다시 계산한다
    · 영업이 모르는 사실은 재무 쪽에서 `INPUT_INCOMPLETE` 로 드러난다
    · 실 채권 원장이 회수위험 판정까지 이어진다

🔴 **두 Agent 를 여기서 붙이는 것이 아니다.** production 코드에는 여전히 서로를
   부르는 곳이 없다 — 실제 호출은 마스터가 한다. 이 파일은 마스터가 붙였을 때
   양쪽 계약이 맞는지를 **테스트에서만** 확인한다.
"""

import ast
import pathlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.finance.capabilities.sales import (
    parse_sales_validation_input,
    run_sales_validation,
    sales_business_status,
)
from app.finance.sales_models import PartnerReceivable
from app.sales.finance_validation import (
    build_financial_validation_batch,
    build_financial_validation_request,
)
from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput

AS_OF = date(2026, 9, 3)

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


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


def _user(**over):
    base = {
        "item": "배추",
        "partner_id": "P-100",
        "requested_quantity_kg": 3000,
        "preferred_unit_price_krw": 2000,
        "preferred_delivery_date": "2026-09-10",
        "preferred_payment_terms_type": "SINGLE",
        "preferred_payment_days": 20,
        "source_ref": "USER:U-1",
    }
    base.update(over)
    return base


def _proposal(**over):
    return run_proposal(
        SalesProposalInput.model_validate(
            {
                "business_mode": "SPOT_SALES",
                "user_request": _user(**over),
                "logistics_context": _LOGISTICS,
            }
        )
    )


class _LedgerPort:
    """재무가 채권을 읽는 자리. 마스터도 DB 도 흉내내지 않는다."""

    def __init__(self, *receivables):
        self.receivables = list(receivables)

    def load_partner_receivables(self, as_of, partner_id):
        del as_of, partner_id
        return list(self.receivables)


def _receivable(receivable_id, *, due, outstanding="100000"):
    return PartnerReceivable(
        receivable_id=receivable_id,
        due_date=due,
        outstanding_amount_krw=Decimal(outstanding),
        status="OPEN",
        source_ref=receivable_id,
    )


def _finance(payload, port=None):
    state = SimpleNamespace(
        request=SimpleNamespace(payload=payload, context=SimpleNamespace(as_of=AS_OF))
    )
    return run_sales_validation(port or _LedgerPort(), {}, state)


def _rule(result, rule_id):
    for rule in result["rule_results"]:
        if rule["rule_id"] == rule_id:
            return rule
    raise AssertionError(f"{rule_id} 규칙 결과가 없다")


# ---------------------------------------------------------------------------
# 완성된 제안 — 마스터의 rename 없이 맞물린다
# ---------------------------------------------------------------------------


def test_a_complete_proposal_is_read_by_the_finance_parser():
    scenario = _proposal().scenarios[0]

    payload, unresolved = build_financial_validation_request(scenario)
    parsed, missing = parse_sales_validation_input(payload)

    assert unresolved == ()
    assert missing == ()
    assert parsed is not None


def test_the_facts_survive_the_crossing_unchanged():
    """이름만 바뀐다 — 값이 바뀌면 두 Agent 가 다른 제안을 보고 있는 것이다."""
    scenario = _proposal().scenarios[0]

    parsed, _ = parse_sales_validation_input(
        build_financial_validation_request(scenario).payload
    )

    assert parsed.scenario_id == scenario.scenario_id
    assert parsed.partner_id == scenario.partner_id
    assert parsed.item == scenario.item
    assert parsed.quantity_kg == scenario.quantity_kg
    assert parsed.unit_price_krw == scenario.unit_price_krw
    assert parsed.reported_sales_amount_krw == scenario.sales_amount_krw
    assert parsed.payment_terms_type == scenario.payment_terms_type
    assert parsed.payment_days == scenario.payment_days
    assert parsed.source_ref == scenario.source_ref


def test_every_scenario_of_the_batch_crosses():
    reply = _proposal()

    batch, unresolved = build_financial_validation_batch(list(reply.scenarios))

    assert unresolved == ()
    assert len(batch["scenarios"]) == len(reply.scenarios)
    for payload in batch["scenarios"]:
        parsed, missing = parse_sales_validation_input(payload)
        assert missing == (), payload["scenario_id"]
        assert parsed is not None


def test_the_crossing_is_deterministic():
    """같은 제안을 두 번 넘기면 재무가 보는 사실도 같다."""
    scenario = _proposal().scenarios[0]

    assert (
        build_financial_validation_request(scenario)
        == build_financial_validation_request(scenario)
    )


# ---------------------------------------------------------------------------
# 금액은 옮겨지기만 한다 — 대조는 재무가 한다
# ---------------------------------------------------------------------------


def test_finance_recalculates_the_amount_and_confirms_it():
    scenario = _proposal().scenarios[0]

    result = _finance(build_financial_validation_request(scenario).payload)
    summary = result["financial_summary"]

    assert summary["reported_sales_amount_krw"] == scenario.sales_amount_krw
    assert summary["recalculated_sales_amount_krw"] == scenario.sales_amount_krw
    assert summary["amount_match"] is True
    assert _rule(result, "FIN-SALES-AMOUNT")["verdict"] == "PASS"


def test_a_reported_amount_that_disagrees_is_carried_not_corrected():
    """🔴 영업이 보고한 금액을 projection 이 고쳐 보내면 재무가 대조할 것이 없어진다.

    보고 9,000 · 재계산 10,000 이면 **불일치가 그대로 드러나야** 한다.
    """
    scenario = _proposal().scenarios[0].model_copy(
        update={
            "quantity_kg": Decimal(10),
            "unit_price_krw": Decimal(1000),
            "sales_amount_krw": Decimal(9000),
        }
    )

    payload, _ = build_financial_validation_request(scenario)

    # projection 은 보고 금액을 그대로 나른다 — 10,000 으로 고치지 않는다.
    assert payload["reported_sales_amount_krw"] == "9000"

    summary = _finance(payload)["financial_summary"]
    assert summary["reported_sales_amount_krw"] == Decimal(9000)
    assert summary["recalculated_sales_amount_krw"] == Decimal(10000)
    assert summary["amount_match"] is False
    assert summary["amount_difference_krw"] != 0


# ---------------------------------------------------------------------------
# 미완성 제안 — 재무 쪽에서 드러난다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "absent",
    [
        "partner_id",
        "quantity_kg",
        "unit_price_krw",
        "sales_amount_krw",
        "payment_terms_type",
        "source_ref",
    ],
)
def test_an_absent_fact_reaches_finance_as_an_incomplete_input(absent):
    """🔴 없는 값을 영업이 메우면 재무는 미완성 제안을 완성된 제안으로 본다."""
    scenario = _proposal().scenarios[0].model_copy(update={absent: None})

    payload, unresolved = build_financial_validation_request(scenario)
    parsed, missing = parse_sales_validation_input(payload)

    assert parsed is None
    assert missing != ()
    # 영업이 못 채운 것과 재무가 못 받은 것이 같은 사실이어야 한다.
    assert unresolved != ()


def test_an_incomplete_proposal_is_not_a_finance_failure():
    """제안이 미완성인 것은 재무 고장이 아니다 — 판정을 내리지 않을 뿐이다."""
    scenario = _proposal().scenarios[0].model_copy(update={"partner_id": None})

    result = _finance(build_financial_validation_request(scenario).payload)

    assert result["status"] == "INPUT_INCOMPLETE"
    assert result["finance_verdict"] is None
    assert result["missing_fields"] == ["partner_id"]
    # 🔴 못 본 것은 거절이 아니다 — `reject` 로 나가면 재무가 막은 것이 된다.
    assert sales_business_status(result) == "skipped"


def test_an_absent_payment_terms_type_does_not_arrive_as_single():
    scenario = _proposal().scenarios[0].model_copy(update={"payment_terms_type": None})

    payload, unresolved = build_financial_validation_request(scenario)

    assert "payment_terms_type" not in payload
    assert "payment_terms_type" in unresolved


def test_an_absent_source_ref_does_not_arrive_as_an_evidence_ref():
    scenario = _proposal().scenarios[0].model_copy(update={"source_ref": None})

    payload, unresolved = build_financial_validation_request(scenario)

    assert "source_ref" not in payload
    assert "source_ref" in unresolved
    for ref in scenario.evidence_refs:
        assert ref not in payload.values()


# ---------------------------------------------------------------------------
# 실 채권 원장이 회수위험까지 이어진다
# ---------------------------------------------------------------------------


def test_a_clean_ledger_lets_collection_risk_pass():
    payload, _ = build_financial_validation_request(_proposal().scenarios[0])

    result = _finance(payload, _LedgerPort())

    assert _rule(result, "FIN-SALES-COLLECTION-RISK")["verdict"] == "PASS"


def test_an_overdue_ledger_makes_collection_risk_ask_for_review():
    payload, _ = build_financial_validation_request(_proposal().scenarios[0])

    result = _finance(payload, _LedgerPort(_receivable("AR-1", due=date(2026, 9, 1))))

    assert _rule(result, "FIN-SALES-COLLECTION-RISK")["verdict"] == "REVIEW_REQUIRED"
    assert result["financial_summary"]["overdue_ar_krw"] == Decimal(100000)


def test_credit_is_still_closed_on_the_far_side_of_the_crossing():
    """🔴 제안이 완성됐다고 없는 여신한도가 생기지 않는다."""
    payload, _ = build_financial_validation_request(_proposal().scenarios[0])

    result = _finance(payload, _LedgerPort())

    assert _rule(result, "FIN-SALES-CREDIT")["runtime_status"] == "RUNTIME_NOT_READY"
    assert "partner_credit_limit_krw" in result["missing_data"]


def test_a_complete_proposal_still_does_not_get_a_finance_verdict_today():
    """★ 권위 있는 원가가 없으면 마진을 만들 수 없다 — 그래서 종합은 열리지 않는다.

    이 검사가 깨진다면 어딘가에서 원가를 지어내기 시작했다는 뜻이다.
    """
    payload, _ = build_financial_validation_request(_proposal().scenarios[0])

    result = _finance(payload)

    assert result["finance_verdict"] is None
    assert "authoritative_inventory_cost_basis" in result["missing_data"]


# ---------------------------------------------------------------------------
# 계층은 붙지 않았다
# ---------------------------------------------------------------------------


def _imports(package: str) -> set[tuple[str, str]]:
    root = pathlib.Path(__file__).resolve().parents[2] / "app" / package
    found: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update((path.name, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add((path.name, node.module))
    return found


def test_no_sales_production_module_imports_finance():
    """🔴 실행 계층을 직접 묶으면 마스터가 중개할 자리가 사라진다."""
    leaks = [item for item in _imports("sales") if item[1].startswith("app.finance")]

    assert leaks == []


def test_no_finance_production_module_imports_sales():
    """반대 방향도 같다 — 영업의 화면 계약이 재무 판정의 계약이 되면 안 된다."""
    leaks = [item for item in _imports("finance") if item[1].startswith("app.sales")]

    assert leaks == []


def test_the_projection_does_not_reach_out_to_anyone():
    """호출도 없다 — payload 를 만들고 끝난다."""
    import app.sales.finance_validation as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    for reaching_out in ("post", "get", "request", "run", "finance_port"):
        assert reaching_out not in called, reaching_out


def test_the_batch_shape_survives_finance_request_validation():
    """★ 재무는 batch 요청의 **모양**을 자기 계약으로 한 번 더 본다.

    안 개수와 `scenario_id` 유일성이 그 계약이다 — 여기서 통과하지 못하면
    마스터가 나른 순간 계약 오류가 된다.
    """
    from app.finance.application.harness import _validate_sales_payload

    batch, _ = build_financial_validation_batch(list(_proposal().scenarios))

    _validate_sales_payload(
        SimpleNamespace(payload=batch)  # type: ignore[arg-type]
    )

    ids = [scenario["scenario_id"] for scenario in batch["scenarios"]]
    assert len(set(ids)) == len(ids)
