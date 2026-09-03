"""SALES_VALIDATION 실행 왕복 — Controller 부터 실행이력 저장까지.

★ 이 파일이 지키는 것은 **판정이 저장까지 닿는가**다.

    request.mode = SALES_VALIDATION
      → finance_port 가 판매 분기로 보낸다
      → Harness 가 판매 Tool 만 고른다
      → 결정론 결과가 나온다
      → AgentReply · ExecutionMetadata 가 선다
      → finance_agent_runs_v22 저장이 그 mode 를 받는다

★ **정책이 없어도 저장은 된다.** 판매 마진·결제일수·여신 정책은 아직 저장소에
  없어서 오늘 판정은 RUNTIME_NOT_READY 로 닫힌다. 그 실행도 이력에 남아야 한다 —
  "왜 못 봤는지" 가 남지 않으면 나중에 아무도 이유를 알 수 없다.

★ 가짜 정책을 넣어 초록불을 만들지 않는다. 없는 것은 없는 채로 시험한다.
"""

from datetime import date
from unittest.mock import patch
from uuid import UUID

import pytest

from app.finance.adapter import finance_port
from app.master.envelope import AgentRequest, ExecutionContext

_EXECUTION = "app.finance.execution"


def _request(payload=None, *, mode="SALES_VALIDATION"):
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-sales-1",
            as_of=date(2025, 12, 31),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode=mode,
        payload=payload if payload is not None else _sales_payload(),
    )


def _sales_payload(**overrides):
    payload = {
        "scenario_id": "SC-001",
        "partner_id": "P-100",
        "item": "red_pepper",
        "quantity_kg": "100",
        "unit_price_krw": "10000",
        "reported_sales_amount_krw": "1000000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": "2026-01-05",
        "source_ref": "SALES-REPLY:R-9",
        # 재무가 안 쓰는 영업 키가 섞여 있어도 깨지지 않아야 한다.
        "objective": "MAXIMIZE_MARGIN",
        "scenario_type": "WHAT_IF",
    }
    payload.update(overrides)
    return payload


def _run(request, finance_context):
    """실제 DB·LLM 없이 Controller 를 돌리고, 저장 호출 인자를 되돌려 준다.

    ★ LLM 을 끄는 이유는 속도가 아니라 **무엇을 시험하는지** 때문이다. 여기서 보는
      것은 결정론 실행 경로와 저장이다 — 모델이 살았는지 죽었는지에 따라 결과가
      달라지면 그것은 이 검사가 답할 질문이 아니다. 설명 문장은 대체 경로가 낸다.
    """
    saved: dict[str, object] = {}

    def _capture(query, params):
        saved["params"] = params
        return {"run_id": UUID("00000000-0000-0000-0000-000000000009")}

    with (
        patch(
            "app.finance.adapter.get_current_finance_runtime_context",
            return_value=finance_context,
        ),
        patch("app.finance.llm.planner.finance_llm_enabled", return_value=False),
        patch("app.finance.adapter.finance_llm_enabled", return_value=False),
        patch(f"{_EXECUTION}.get_db_schema", return_value="haetdeul"),
        patch(f"{_EXECUTION}.execute_returning_one", side_effect=_capture),
    ):
        reply, metadata = finance_port(request)
    return reply, metadata, saved


# ---------------------------------------------------------------------------
# Controller 가 판매 mode 를 받는다
# ---------------------------------------------------------------------------


def test_sales_validation_reaches_the_controller(finance_context):
    reply, _metadata, _ = _run(_request(), finance_context)

    assert reply.mode == "SALES_VALIDATION"
    assert reply.agent == "finance"
    # 미구현 경로로 떨어지지 않았다.
    assert "SALES_VALIDATION_translation" not in reply.missing_data


def test_sales_run_reaches_run_history_persistence(finance_context):
    _, _, saved = _run(_request(), finance_context)

    assert "params" in saved, "실행이력 저장이 호출되지 않았다"
    params = saved["params"]
    # mode 는 네 번째 인자다 (run_id, request_id, agent, mode, ...).
    assert params[2] == "finance"
    assert params[3] == "SALES_VALIDATION"


def test_persisted_request_payload_keeps_the_whole_sales_proposal(finance_context):
    _, _, saved = _run(_request(), finance_context)
    request_payload = saved["params"][10].obj

    # 재무가 안 쓰는 키까지 그대로 남는다 — 나중에 왜 그렇게 판정했는지 되짚을 수 있다.
    assert request_payload["scenario_id"] == "SC-001"
    assert request_payload["objective"] == "MAXIMIZE_MARGIN"


# ---------------------------------------------------------------------------
# 정책이 없는 오늘의 경로 — 닫히되 저장된다
# ---------------------------------------------------------------------------


def test_missing_sales_policy_is_runtime_not_ready_and_still_persists(finance_context):
    reply, _, saved = _run(_request(), finance_context)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.payload.get("finance_verdict") is None
    # 그래도 이력에는 남는다.
    assert saved["params"][3] == "SALES_VALIDATION"
    assert saved["params"][8] == "RUNTIME_NOT_READY"
    assert saved["params"][9] == "skipped"


def test_missing_authoritative_fact_names_survive_into_the_reply(finance_context):
    """★ 이름이 남는 대상이 **정책에서 사실로** 바뀌었다.

    마진 임계값·최대 결제일수는 이제 MVP 정책이 공급한다. 여전히 없는 것은 거래처가
    소유한 여신한도와 채권 사실이고, 그것들은 재무가 만들 수 없다.
    """
    reply, _, _ = _run(_request(), finance_context)
    missing = reply.payload.get("missing_data", [])

    for fact in ("partner_credit_limit_krw", "partner_receivable_facts"):
        assert fact in missing, fact
    for supplied in (
        "finance_minimum_margin_rate",
        "finance_warning_margin_rate",
        "max_finance_allowed_payment_terms_days",
    ):
        assert supplied not in missing, supplied


def test_no_sales_policy_value_is_invented(finance_context):
    reply, _, _ = _run(_request(), finance_context)
    summary = reply.payload.get("financial_summary") or {}

    # 없는 정책 자리에 숫자가 들어차지 않았다.
    assert summary.get("credit_limit_krw") is None
    assert summary.get("available_credit_krw") is None
    assert reply.payload["data_quality"] == "INCOMPLETE"


# ---------------------------------------------------------------------------
# 결정론 결과는 실제로 계산된다
# ---------------------------------------------------------------------------


def test_deterministic_facts_are_produced_even_while_the_verdict_is_closed(finance_context):
    reply, _, _ = _run(_request(), finance_context)
    summary = reply.payload["financial_summary"]

    assert summary["recalculated_sales_amount_krw"] == 1000000
    assert summary["amount_match"] is True
    # 최소 현금과 현금흐름은 실재하는 재무 자료라 실제로 계산된다.
    assert summary["base_projected_cash_min"] is not None
    assert summary["scenario_projected_cash_min"] is not None
    assert summary["collection_date"] == date(2026, 2, 4)


def test_amount_mismatch_is_recorded_through_the_whole_path(finance_context):
    request = _request(_sales_payload(reported_sales_amount_krw="900000"))

    reply, _, _ = _run(request, finance_context)

    assert reply.payload["financial_summary"]["amount_match"] is False
    assert "SALES_AMOUNT_MISMATCH" in reply.payload["reason_codes"]


# ---------------------------------------------------------------------------
# 제안이 미완성인 경우 — 재무 고장이 아니다
# ---------------------------------------------------------------------------


def test_incomplete_sales_input_is_skipped_not_an_error(finance_context):
    payload = _sales_payload()
    del payload["unit_price_krw"]

    reply, _, saved = _run(_request(payload), finance_context)

    assert reply.runtime_status == "READY"
    assert reply.business_status == "skipped"
    assert reply.payload["status"] == "INPUT_INCOMPLETE"
    assert reply.payload["missing_fields"] == ["unit_price_krw"]
    # 미완성 제안도 이력에 남는다.
    assert saved["params"][3] == "SALES_VALIDATION"


# ---------------------------------------------------------------------------
# 매입 경로는 그대로다
# ---------------------------------------------------------------------------


def test_purchase_pre_purchase_still_persists_its_own_mode(finance_context):
    _, _, saved = _run(_request(payload={}, mode="PRE_PURCHASE"), finance_context)

    assert saved["params"][3] == "PRE_PURCHASE"


def test_sales_tools_are_the_only_tools_used_in_sales_mode(finance_context):
    _, metadata, _ = _run(_request(), finance_context)

    assert set(metadata.used_tools) <= {"evaluate_sales_scenario"}
    for purchase_tool in (
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "evaluate_purchase_scenario",
    ):
        assert purchase_tool not in metadata.used_tools


@pytest.mark.parametrize("mode", ["PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION"])
def test_every_controller_mode_is_accepted_by_run_history(mode):
    from app.finance.adapter import _CONTROLLER_MODES

    assert mode in _CONTROLLER_MODES
