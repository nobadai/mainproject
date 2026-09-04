"""SALES_VALIDATION 1~3안 batch — 마스터가 그대로 나를 수 있는 안별 결과.

★ 이 파일이 지키는 것.
    · 단일 payload 는 batch 도입 뒤에도 **모양이 그대로다**
    · 1·2·3안은 받고 0안·4안은 계약 오류다
    · 입력 안과 출력 결과가 `scenario_id` 로 짝지어진다 (순서·index 아님)
    · top-level 업무 상태를 재무 안에서 끝낸다 — 마스터가 다시 계산하지 않는다
    · 판정 못 한 안이 섞이면 `ok` 로 올리지 않는다 (skipped ≠ ok)

★ 재무 정책(마진·여신·결제일수)이 아직 저장소에 없으므로 오늘 판정은 대부분
  RUNTIME_NOT_READY 로 닫힌다. 가짜 정책을 넣어 초록불을 만들지 않는다 — 대신
  판정 자체를 다루는 검사는 aggregate 함수를 직접 쓴다.
"""

from datetime import date
from unittest.mock import patch
from uuid import UUID

import pytest

from app.finance.adapter import finance_port
from app.finance.application.orchestration import (
    aggregate_sales_business_status,
    branch_requests,
)
from app.master.envelope import AgentRequest, ExecutionContext

_EXECUTION = "app.finance.execution"


def _scenario(scenario_id: str, **over):
    payload = {
        "scenario_id": scenario_id,
        "partner_id": "P-100",
        "item": "red_pepper",
        "quantity_kg": "100",
        "unit_price_krw": "10000",
        "reported_sales_amount_krw": "1000000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": "2026-01-05",
        "source_ref": f"SALES-REPLY:{scenario_id}",
    }
    payload.update(over)
    return payload


def _request(payload):
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-sales-batch",
            as_of=date(2025, 12, 31),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode="SALES_VALIDATION",
        payload=payload,
    )


def _run(request, finance_context):
    saved: dict[str, object] = {}

    def _capture(query, params):
        saved["params"] = params
        return {"run_id": UUID("00000000-0000-0000-0000-000000000009")}

    with (
        patch(
            "app.finance.adapter.get_current_finance_runtime_context",
            return_value=finance_context,
        ),
        patch("app.finance.adapter.load_partner_receivables", return_value=[]),
        patch("app.finance.llm.planner.finance_llm_enabled", return_value=False),
        patch("app.finance.adapter.finance_llm_enabled", return_value=False),
        patch(f"{_EXECUTION}.get_db_schema", return_value="haetdeul"),
        patch(f"{_EXECUTION}.execute_returning_one", side_effect=_capture),
    ):
        reply, metadata = finance_port(request)
    return reply, metadata, saved


# ---------------------------------------------------------------------------
# 단일 경로 회귀 — batch 가 기존 모양을 바꾸지 않는다
# ---------------------------------------------------------------------------


def test_single_payload_keeps_its_original_shape(finance_context):
    reply, _, _ = _run(_request(_scenario("SC-001")), finance_context)

    # 단일 요청은 예전처럼 결과가 top-level 에 그대로 있다.
    assert "scenario_results" not in reply.payload
    assert reply.payload["scenario_id"] == "SC-001"
    assert "financial_summary" in reply.payload


def test_single_payload_is_not_branched():
    branches = branch_requests(_request(_scenario("SC-001")))

    assert len(branches) == 1
    assert branches[0].payload["scenario_id"] == "SC-001"


# ---------------------------------------------------------------------------
# batch 개수 계약
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 3])
def test_one_to_three_scenarios_are_accepted(count):
    scenarios = [_scenario(f"SC-{index}") for index in range(count)]

    branches = branch_requests(_request({"scenarios": scenarios}))

    assert len(branches) == count
    assert [branch.payload["scenario_id"] for branch in branches] == [
        f"SC-{index}" for index in range(count)
    ]


def test_zero_scenarios_are_rejected():
    with pytest.raises(ValueError):
        branch_requests(_request({"scenarios": []}))


def test_four_scenarios_are_rejected():
    with pytest.raises(ValueError):
        branch_requests(_request({"scenarios": [_scenario(f"SC-{i}") for i in range(4)]}))


def test_non_object_scenario_is_rejected():
    with pytest.raises(TypeError):
        branch_requests(_request({"scenarios": ["SC-1"]}))


def test_duplicate_scenario_id_is_rejected(finance_context):
    from app.finance.application.harness import _validate_finance_payload

    request = _request({"scenarios": [_scenario("SC-1"), _scenario("SC-1")]})

    # 같은 id 가 둘이면 결과를 안과 짝지을 수 없다.
    with pytest.raises(ValueError):
        _validate_finance_payload(request)


def test_missing_scenario_id_is_not_a_contract_error():
    from app.finance.application.harness import _validate_finance_payload

    scenario = _scenario("SC-1")
    del scenario["scenario_id"]

    # 식별자 부재는 업무 사실 부족이지 요청 모양 오류가 아니다 — 안에서 다룬다.
    _validate_finance_payload(_request({"scenarios": [scenario]}))


# ---------------------------------------------------------------------------
# scenario identity 보존
# ---------------------------------------------------------------------------


def test_each_input_scenario_gets_its_own_result(finance_context):
    scenarios = [_scenario("SC-A"), _scenario("SC-B"), _scenario("SC-C")]

    reply, _, _ = _run(_request({"scenarios": scenarios}), finance_context)

    results = reply.payload["scenario_results"]
    assert len(results) == 3
    assert [result["scenario_id"] for result in results] == ["SC-A", "SC-B", "SC-C"]


def test_results_are_joined_by_scenario_id_not_by_index(finance_context):
    """입력 값이 다른 안이 자기 결과를 들고 있어야 한다."""
    scenarios = [
        _scenario("SC-A", quantity_kg="100", reported_sales_amount_krw="1000000"),
        _scenario("SC-B", quantity_kg="250", reported_sales_amount_krw="2500000"),
        _scenario("SC-C", quantity_kg="7", reported_sales_amount_krw="70000"),
    ]

    reply, _, _ = _run(_request({"scenarios": scenarios}), finance_context)

    by_id = {
        result["scenario_id"]: result["financial_summary"]["recalculated_sales_amount_krw"]
        for result in reply.payload["scenario_results"]
    }
    assert by_id == {"SC-A": 1000000, "SC-B": 2500000, "SC-C": 70000}


def test_each_result_stays_self_contained(finance_context):
    reply, _, _ = _run(
        _request({"scenarios": [_scenario("SC-A"), _scenario("SC-B")]}), finance_context
    )

    for result in reply.payload["scenario_results"]:
        # 안 하나만 떼어 봐도 왜 그런 판정인지 알 수 있어야 한다.
        for key in (
            "status",
            "finance_verdict",
            "scenario_id",
            "financial_summary",
            "rule_results",
            "reason_codes",
            "missing_fields",
            "missing_data",
            "data_quality",
        ):
            assert key in result, key


def test_one_incomplete_scenario_does_not_break_the_others(finance_context):
    incomplete = _scenario("SC-B")
    del incomplete["unit_price_krw"]

    reply, _, _ = _run(
        _request({"scenarios": [_scenario("SC-A"), incomplete, _scenario("SC-C")]}),
        finance_context,
    )

    by_id = {result["scenario_id"]: result for result in reply.payload["scenario_results"]}
    assert by_id["SC-B"]["status"] == "INPUT_INCOMPLETE"
    assert by_id["SC-B"]["missing_fields"] == ["unit_price_krw"]
    # 나머지 안은 계속 계산된다.
    assert by_id["SC-A"]["financial_summary"]["recalculated_sales_amount_krw"] == 1000000


# ---------------------------------------------------------------------------
# top-level 업무 상태 — 재무 안에서 끝낸다
# ---------------------------------------------------------------------------


def _evaluated(verdict):
    return {"status": "EVALUATED", "finance_verdict": verdict}


def _unevaluated(status):
    return {"status": status, "finance_verdict": None}


def test_all_pass_aggregates_to_ok():
    results = [_evaluated("PASS")] * 3

    assert aggregate_sales_business_status(results) == "ok"


def test_one_review_required_aggregates_to_conditional():
    results = [_evaluated("PASS"), _evaluated("REVIEW_REQUIRED"), _evaluated("PASS")]

    assert aggregate_sales_business_status(results) == "conditional"


def test_one_fail_aggregates_to_reject():
    results = [_evaluated("PASS"), _evaluated("FAIL"), _evaluated("PASS")]

    assert aggregate_sales_business_status(results) == "reject"


def test_fail_outranks_review_required():
    results = [_evaluated("REVIEW_REQUIRED"), _evaluated("FAIL")]

    assert aggregate_sales_business_status(results) == "reject"


def test_a_fail_still_rejects_even_when_another_scenario_was_not_evaluated():
    # 실제로 막힌 안은 막힌 것이다 — 못 본 안이 있다고 사실이 사라지지 않는다.
    results = [_unevaluated("RUNTIME_NOT_READY"), _evaluated("FAIL")]

    assert aggregate_sales_business_status(results) == "reject"


def test_unevaluated_scenario_prevents_a_plain_ok():
    """🔴 여기가 핵심이다. 보지 않은 안이 통과한 것으로 읽히면 안 된다."""
    for unevaluated in ("INPUT_INCOMPLETE", "RUNTIME_NOT_READY", "ERROR"):
        results = [_evaluated("PASS"), _unevaluated(unevaluated), _evaluated("PASS")]

        assert aggregate_sales_business_status(results) == "skipped", unevaluated


def test_all_unevaluated_is_skipped_not_ok_and_not_reject():
    results = [_unevaluated("RUNTIME_NOT_READY")] * 2

    status = aggregate_sales_business_status(results)
    assert status == "skipped"
    assert status not in {"ok", "reject"}


def test_null_verdict_never_becomes_pass():
    results = [{"status": "EVALUATED", "finance_verdict": None}]

    assert aggregate_sales_business_status(results) == "skipped"


# ---------------------------------------------------------------------------
# runtime 의미 — 정책 없음은 재무 사정, 사실 없음은 제안 사정
# ---------------------------------------------------------------------------


def test_missing_policy_makes_the_whole_batch_runtime_not_ready(finance_context):
    reply, _, saved = _run(
        _request({"scenarios": [_scenario("SC-A"), _scenario("SC-B")]}), finance_context
    )

    # 판매 정책이 저장소에 없으므로 두 안 모두 재무 사정으로 못 본다.
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    for result in reply.payload["scenario_results"]:
        assert result["status"] == "RUNTIME_NOT_READY"
        assert result["finance_verdict"] is None
    # 그래도 실행이력에는 남는다.
    assert saved["params"][3] == "SALES_VALIDATION"


def test_incomplete_input_is_not_a_finance_runtime_failure(finance_context):
    incomplete = _scenario("SC-A")
    del incomplete["quantity_kg"]

    reply, _, _ = _run(_request({"scenarios": [incomplete]}), finance_context)

    result = reply.payload["scenario_results"][0]
    assert result["status"] == "INPUT_INCOMPLETE"
    # 제안에 사실이 빠진 것은 재무 고장이 아니다.
    assert reply.runtime_status == "READY"
    assert reply.business_status == "skipped"


def test_mixed_runtime_does_not_overwrite_an_evaluated_scenario(finance_context):
    """일부만 못 본 상태를 통째로 RUNTIME_NOT_READY 로 덮지 않는다."""
    incomplete = _scenario("SC-B")
    del incomplete["quantity_kg"]

    reply, _, _ = _run(
        _request({"scenarios": [_scenario("SC-A"), incomplete]}), finance_context
    )

    statuses = {r["scenario_id"]: r["status"] for r in reply.payload["scenario_results"]}
    assert statuses["SC-B"] == "INPUT_INCOMPLETE"
    # 섞였으므로 top-level 은 READY 로 두고 안별 status 가 사실을 나른다.
    assert reply.runtime_status == "READY"
    assert reply.business_status == "skipped"


def test_batch_never_invents_a_sales_policy_number(finance_context):
    reply, _, _ = _run(_request({"scenarios": [_scenario("SC-A")]}), finance_context)

    result = reply.payload["scenario_results"][0]
    summary = result["financial_summary"] or {}
    # 여신한도는 여전히 없다 — 채권을 읽었다고 한도가 생기지 않는다.
    assert summary.get("credit_limit_krw") is None
    assert summary.get("contribution_margin_rate") is None
    assert "partner_credit_limit_krw" in result["missing_data"]
    assert "partner_receivable_facts" not in result["missing_data"]


# ---------------------------------------------------------------------------
# 0 != NULL
# ---------------------------------------------------------------------------


def test_zero_quantity_is_a_value_not_a_missing_fact(finance_context):
    reply, _, _ = _run(
        _request({"scenarios": [_scenario("SC-A", quantity_kg="0",
                                          reported_sales_amount_krw="0")]}),
        finance_context,
    )

    result = reply.payload["scenario_results"][0]
    # 0 은 실제 값이므로 INPUT_INCOMPLETE 가 아니다.
    assert result["status"] != "INPUT_INCOMPLETE"
    assert "quantity_kg" not in result["missing_fields"]
    assert result["financial_summary"]["recalculated_sales_amount_krw"] == 0


def test_null_payment_days_is_not_read_as_zero(finance_context):
    reply, _, _ = _run(
        _request({"scenarios": [_scenario("SC-A", payment_days=None)]}), finance_context
    )

    result = reply.payload["scenario_results"][0]
    summary = result["financial_summary"] or {}
    # 결제일수를 못 받은 것을 D+0 으로 바꾸지 않는다.
    assert summary.get("collection_date") is None


# ---------------------------------------------------------------------------
# Runtime boundary — 매입 전용 정책이 판매를 막지 않는다
# ---------------------------------------------------------------------------


def _context_without_purchase_payment_days(finance_context):
    policy = finance_context.policy.model_copy(update={"purchase_payment_days": None})
    return finance_context.model_copy(update={"policy": policy})


def test_missing_purchase_policy_does_not_block_sales_validation(finance_context):
    """🔴 예전에는 매입 지급일 정책이 없으면 **판매 검증도** 실행 전에 막혔다.

    그 값은 매입 지급일 상한 계산에만 쓰인다 — 판매는 한 번도 읽지 않는다. 그런데
    공통 boundary 에 있어서 "매입 정책이 없어서 판매를 못 봤다" 는 거짓 사유가
    이력에 남았다.
    """
    context = _context_without_purchase_payment_days(finance_context)

    reply, _, _ = _run(_request({"scenarios": [_scenario("SC-A")]}), context)

    assert "purchase_payment_days" not in reply.missing_data
    # 실제로 실행되어 안별 결과가 나왔다.
    assert reply.payload["scenario_results"][0]["scenario_id"] == "SC-A"


def test_missing_purchase_policy_still_blocks_pre_purchase(finance_context):
    context = _context_without_purchase_payment_days(finance_context)
    request = AgentRequest(
        context=ExecutionContext(
            request_id="req-pre",
            as_of=date(2025, 12, 31),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode="PRE_PURCHASE",
        payload={},
    )

    reply, _, _ = _run(request, context)

    # 매입 쪽 방어는 그대로다.
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert "purchase_payment_days" in reply.missing_data


def test_as_of_mismatch_still_blocks_sales_validation(finance_context):
    """공통 방어(as_of 일치)는 판매에서도 그대로 산다."""
    request = AgentRequest(
        context=ExecutionContext(
            request_id="req-sales-asof",
            as_of=date(2026, 6, 30),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode="SALES_VALIDATION",
        payload={"scenarios": [_scenario("SC-A")]},
    )

    reply, _, _ = _run(request, finance_context)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert any("finance_state@" in item for item in reply.missing_data)


# ---------------------------------------------------------------------------
# Mixed incomplete / runtime — 못 본 안이 통과로 승격되지 않는다
# ---------------------------------------------------------------------------


def test_pass_incomplete_pass_does_not_become_ok():
    results = [
        _evaluated("PASS"),
        _unevaluated("INPUT_INCOMPLETE"),
        _evaluated("PASS"),
    ]

    assert aggregate_sales_business_status(results) == "skipped"


def test_pass_runtime_not_ready_pass_does_not_become_ok():
    results = [
        _evaluated("PASS"),
        _unevaluated("RUNTIME_NOT_READY"),
        _evaluated("PASS"),
    ]

    status = aggregate_sales_business_status(results)
    assert status == "skipped"
    # RUNTIME_NOT_READY 를 reject 로 바꾸지 않는다.
    assert status != "reject"


def test_runtime_not_ready_is_never_turned_into_a_verdict():
    for status in ("INPUT_INCOMPLETE", "RUNTIME_NOT_READY", "ERROR"):
        aggregated = aggregate_sales_business_status([_unevaluated(status)])

        assert aggregated == "skipped", status
        assert aggregated not in {"ok", "reject", "conditional"}


def test_review_required_with_an_unevaluated_scenario_stays_conditional():
    # 실제 판정(conditional)은 살아 있고, 못 본 안 때문에 ok 로 올라가지도 않는다.
    results = [_evaluated("REVIEW_REQUIRED"), _unevaluated("INPUT_INCOMPLETE")]

    assert aggregate_sales_business_status(results) == "conditional"
