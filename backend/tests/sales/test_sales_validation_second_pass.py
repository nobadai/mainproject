"""외부 검증 결과를 받은 두 번째 판매 실행이 설명까지 완주하는가.

★ **판매 LLM 은 첫 실행에서 돌지 않는 것이 맞다.** 첫 실행의 안은 아직 재무 검증을
  안 받았고(`required_validations` 에 `FINANCIAL_VALIDATION` 이 남는다), 그런 안은
  `UNRESOLVED` 다. 설명할 후보가 없으므로 `SKIPPED_TEMPLATE` 로 끝난다.

🔴 **문제는 두 번째 실행이 한 번도 없었다는 것이다.** 실측(2026-09-14)에서 저장된
   판매 실행 7,978건이 전부 `is_refeed=false` 였고, 판매 저장소의 안에 재무 판정이
   실린 실행은 0건이었다. 그래서 모든 안이 `UNRESOLVED` 로 남았고 Gemini 는 한 번도
   불리지 않았다 — 설정이 아니라 **배선**이 빠진 것이다.

★ 이 파일은 **기존 계약만으로** 두 번째 실행이 도는지 증명한다. 새 mode 도, 새 DTO 도,
  새 표도 만들지 않는다. `SalesProposalInput.feedback` 하나로 된다면 남은 일은
  마스터가 그 입력을 만들어 다시 부르는 것뿐이다.

🔴 **결정론 핵심을 LLM 에 넘기지 않는다.** 수량·가격·판정·순위·추천 ID 는 판매의
   결정론 코어가 정하고, LLM 은 그 결과를 설명만 한다. 아래 검사들이 그 경계를 잠근다.
"""

from decimal import Decimal
from typing import Any

import pytest

from app.sales import proposal as proposal_module
from app.sales.llm import runtime as llm_runtime
from app.sales.llm.runtime import LlmInterpretationOutput
from app.sales.proposal import run_proposal
from app.sales.ranking import rank_scenarios
from app.sales.schemas import SalesProposalInput

FINANCE_REF = "FIN-REPLY-SALES-001-A"


def _request_payload(**overrides: Any) -> dict[str, Any]:
    """마스터가 실제로 보내는 모양의 최소 입력.

    ⚠️ 물류 맥락을 채워 둔다. 그것이 없으면 `SELLABLE_SUPPLY_CONTEXT` 와
      `DELIVERY_FEASIBILITY_CONTEXT` 가 미결로 남아, 재무만 풀어도 안이 `UNRESOLVED`
      에서 못 벗어난다 — 이 파일이 보려는 것은 **재무 검증 되먹임**이다.
    """
    data: dict[str, Any] = {
        "business_mode": "CONTRACT_PROPOSAL_NEW",
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": "5000",
            "preferred_unit_price_krw": "2000",
            "preferred_delivery_date": "2026-01-15",
            "preferred_payment_days": 30,
            "preferred_payment_terms_type": "SINGLE",
            "source_ref": "CONSOLE-SALES-REQUEST:2026-01-07:KIMCHI_FACTORY_001:배추",
        },
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": "5000"},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": "5000"}],
                "supply_capacity_by_date": [
                    {"date": "2026-01-15", "confirmed_sellable_quantity_kg": "5000"}
                ],
            },
            "delivery_feasibility": {"status": "READY", "daily_outbound_capacity_kg": "5000"},
        },
    }
    data.update(overrides)
    return data


def _finance_reply_payload(verdict: str = "PASS") -> dict[str, Any]:
    """재무가 실제로 내는 `SALES_VALIDATION` 회신에서 판매가 읽는 부분.

    ★ 칸 이름을 지어내지 않는다. `SalesFinanceReplySubset` 이 읽는 것만 담는다.
    """
    return {
        "finance_verdict": verdict,
        "financial_summary": {
            "contribution_margin_krw": "3000000",
            "contribution_margin_rate": "0.30",
            "scenario_projected_cash_min": "12000000",
            "depends_on_projected_inflow": False,
            "overdue_ar_krw": "0",
        },
        "reason_codes": ["SALES_MARGIN_MEETS_WARNING"],
        "missing_data": [],
        "evidence_refs": ["DB:finance_states/sim_run_id=SIM-SALES-2PASS"],
    }


def _feedback(scenario_ids: list[str], *, verdict: str = "PASS", attempt: int = 1):
    """마스터가 모아 돌려주는 외부 검증 결과.

    🔴 **연결은 `scenario_id → reply_refs → reply_ref` 다.** `SalesDomainReply` 의
       `scenario_id` 칸은 옛 호환용이고, 실제 분배 키는 `scenario_feedback` 이다.
    """
    return {
        "original_run_id": "RUN-PASS-1",
        "attempt": attempt,
        "domain_replies": [
            {
                "source_agent": "finance",
                "capability": "FINANCIAL_VALIDATION",
                "reply_ref": f"FIN-REPLY-{scenario_id}",
                "runtime_status": "READY",
                "business_status": "ok",
                "payload": _finance_reply_payload(verdict),
            }
            for scenario_id in scenario_ids
        ],
        "scenario_feedback": [
            {"scenario_id": scenario_id, "reply_refs": [f"FIN-REPLY-{scenario_id}"]}
            for scenario_id in scenario_ids
        ],
    }


def _pass_one() -> Any:
    return run_proposal(SalesProposalInput.model_validate(_request_payload()))


def _pass_two(*, verdict: str = "PASS", attempt: int = 1, scenario_ids=None) -> Any:
    first = _pass_one()
    ids = scenario_ids or [scenario.scenario_id for scenario in first.scenarios]
    return run_proposal(
        SalesProposalInput.model_validate(
            _request_payload(
                is_refeed=True,
                feedback_attempt=attempt,
                feedback=_feedback(ids, verdict=verdict, attempt=attempt),
            )
        )
    )


class _GeminiSpy:
    """Gemini 호출을 세는 대역. **판매 결정을 대신하지 않는다.**"""

    def __init__(self, *, raises: bool = False, hijack_recommendation: str | None = None):
        self.calls = 0
        self.raises = raises
        self.hijack = hijack_recommendation
        self.seen_context: list[Any] = []

    def __call__(self, context, settings):
        self.calls += 1
        self.seen_context.append(context)
        if self.raises:
            raise RuntimeError("gemini is unavailable")
        #  🔴 **실제 반환 계약을 쓴다.** 다른 모양을 돌려주면 `_validated` 가 터지고
        #     그 예외를 `interpret_candidates` 가 삼켜 FALLBACK 이 된다 — 검사가
        #     «모델이 죽었다» 를 «모델이 성공했다» 로 잘못 읽게 된다.
        named = self.hijack or (context[0].candidate_id if context else "SALES-001-A")
        return LlmInterpretationOutput(
            recommended_candidate_id=named,
            summary="설명을 붙였습니다.",
            recommendation_reason="근거가 있는 조건을 우선합니다.",
            risk_explanation="확인이 필요합니다.",
            user_message="조건을 확인해 주세요.",
        )


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_GEMINI_API_KEY", "test-key")
    spy = _GeminiSpy()
    monkeypatch.setattr(llm_runtime, "_call_gemini", spy)
    return spy


# ── A. 최초 실행 ──────────────────────────────────────────────────────────


def test_the_first_pass_leaves_every_candidate_unresolved(gemini):
    """① 외부 검증 전이다. 안은 아직 판정을 못 받았고 그렇게 남아야 한다."""
    reply = _pass_one()

    assert reply.scenarios
    for scenario in reply.scenarios:
        assert scenario.status == "UNRESOLVED"
        assert "FINANCIAL_VALIDATION" in scenario.required_validations


def test_the_first_pass_never_calls_the_model(gemini):
    """🔴 설명할 후보가 없는데 부르면, 모델이 «아직 판정 안 난 안» 을 설명하게 된다."""
    reply = _pass_one()

    assert reply.llm.status == "SKIPPED_TEMPLATE"
    assert reply.llm.llm_attempts == 0
    assert gemini.calls == 0


# ── B. 검증 결과를 넣은 두 번째 실행 ──────────────────────────────────────


def test_the_second_pass_resolves_the_financial_validation(gemini):
    """② 재무 회신이 붙으면 그 안의 미결 검증이 사라진다."""
    reply = _pass_two()

    assert reply.scenarios
    for scenario in reply.scenarios:
        assert "FINANCIAL_VALIDATION" not in scenario.required_validations


def test_the_second_pass_produces_a_decidable_candidate(gemini):
    """③ 판정을 받은 안은 더 이상 «모름» 이 아니다."""
    reply = _pass_two()

    statuses = {scenario.status for scenario in reply.scenarios}
    assert statuses & {"EXECUTABLE", "CONDITIONAL"}, statuses


def test_the_second_pass_carries_the_finance_verdict_onto_the_scenario(gemini):
    reply = _pass_two()

    verdicts = {scenario.finance_verdict for scenario in reply.scenarios}
    assert verdicts == {"PASS"}


def test_the_second_pass_walks_the_whole_graph():
    """④ 되먹임 → 평가 → 순위 → 자체검증 → 최종 순서로 실제로 지나가야 한다.

    ★ `decision_trace` 는 후보별 결과라 노드 순서를 담지 않는다. 그래서 그래프 상태의
      `agent_trace` 를 직접 본다 — 회신 계약을 넓히지 않고 확인하는 방법이다.
    """
    from app.sales.graph import _graph

    first = _pass_one()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    request = SalesProposalInput.model_validate(
        _request_payload(is_refeed=True, feedback_attempt=1, feedback=_feedback(ids))
    )
    final_state = _graph().invoke({"request": request})

    stages = [entry.get("stage") for entry in final_state.get("agent_trace", [])]
    expected = [
        "apply_feedback",
        "evaluate_candidates",
        "rank_candidates",
        "self_check",
        "interpret_recommendation",
        "final_recommendation",
    ]
    assert [stage for stage in stages if stage in expected] == expected, stages


def test_the_second_pass_ranks_before_it_recommends(gemini):
    """순위가 먼저 서고 그 첫 자리가 추천이 된다."""
    reply = _pass_two()

    ranked = [trace for trace in reply.decision_trace if trace.rank is not None]
    assert ranked, reply.decision_trace
    first = min(ranked, key=lambda trace: trace.rank)
    assert first.recommended is True
    assert first.candidate_id == reply.recommended_scenario_id


def test_the_second_pass_calls_the_model_exactly_once(gemini):
    """⑤ 설명은 한 번이면 된다. 여러 번 부르면 같은 안에 다른 말이 붙는다."""
    reply = _pass_two()

    assert reply.llm.status == "SUCCESS"
    assert reply.llm.llm_attempts == 1
    assert gemini.calls == 1


# ── C. 추천은 LLM 이 정하지 않는다 ────────────────────────────────────────


def test_the_recommendation_is_decided_before_the_model_is_called(gemini):
    """🔴 추천 ID 는 결정론 순위가 정한다."""
    reply = _pass_two()
    expected = rank_scenarios(reply.scenarios)[0].scenario_id

    assert reply.recommended_scenario_id == expected


def test_a_model_naming_another_candidate_does_not_move_the_recommendation(monkeypatch):
    """🔴 모델이 다른 안을 추천해도 업무 추천은 그대로다.

    ★ 모델이 고를 수 있는 것은 **설명**이지 후보가 아니다.
    """
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_GEMINI_API_KEY", "test-key")
    first = _pass_one()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    hijack = _GeminiSpy(hijack_recommendation=ids[-1])
    monkeypatch.setattr(llm_runtime, "_call_gemini", hijack)

    reply = run_proposal(
        SalesProposalInput.model_validate(
            _request_payload(
                is_refeed=True, feedback_attempt=1, feedback=_feedback(ids)
            )
        )
    )
    expected = rank_scenarios(reply.scenarios)[0].scenario_id

    assert hijack.calls == 1
    assert reply.recommended_scenario_id == expected


def test_the_model_never_sees_quantities_or_prices(gemini):
    """🔴 모델에게 숫자를 주지 않는다. 없는 값은 지어낼 수도 없다."""
    _pass_two()

    assert gemini.calls == 1
    for candidate in gemini.seen_context[0]:
        fields = candidate.model_dump()
        for forbidden in ("quantity_kg", "unit_price_krw", "sales_amount_krw", "delivery_date"):
            assert forbidden not in fields
        #  라벨만 간다 — 숫자가 한 칸도 없다.
        assert set(fields) <= {
            "candidate_id",
            "strategy_label",
            "adjustment_axis",
            "conditional",
            "risk_labels",
            "uncertainty_labels",
        }, sorted(fields)


# ── D. 설명 경로가 달라도 업무 값은 같다 ──────────────────────────────────

#: 설명 계층이 바뀌어도 **절대 흔들리면 안 되는** 업무 값.
_BUSINESS_FIELDS = (
    "scenario_id",
    "status",
    "finance_verdict",
    "quantity_kg",
    "unit_price_krw",
    "sales_amount_krw",
    "delivery_date",
    "payment_days",
    "payment_terms_type",
    "contract_term_days",
    "contribution_margin_krw",
    "contribution_margin_rate",
    "scenario_projected_cash_min",
    "depends_on_projected_inflow",
)


def _business_view(reply) -> Any:
    return (
        reply.recommended_scenario_id,
        [
            tuple(getattr(scenario, field) for field in _BUSINESS_FIELDS)
            for scenario in reply.scenarios
        ],
    )


def test_success_fallback_and_disabled_agree_on_every_business_value(monkeypatch):
    """🔴 모델이 죽든 꺼지든 **같은 숫자와 같은 추천**이 나와야 한다."""
    monkeypatch.setenv("SALES_GEMINI_API_KEY", "test-key")

    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    ok = _GeminiSpy()
    monkeypatch.setattr(llm_runtime, "_call_gemini", ok)
    success = _pass_two()

    broken = _GeminiSpy(raises=True)
    monkeypatch.setattr(llm_runtime, "_call_gemini", broken)
    fallback = _pass_two()

    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    never = _GeminiSpy()
    monkeypatch.setattr(llm_runtime, "_call_gemini", never)
    disabled = _pass_two()

    assert (success.llm.status, success.llm.llm_attempts, ok.calls) == ("SUCCESS", 1, 1)
    assert (fallback.llm.status, fallback.llm.llm_attempts, broken.calls) == ("FALLBACK", 1, 1)
    assert (disabled.llm.status, disabled.llm.llm_attempts, never.calls) == ("DISABLED", 0, 0)

    assert _business_view(success) == _business_view(fallback) == _business_view(disabled)


# ── E. 회신은 제 안에만 붙는다 ────────────────────────────────────────────


def test_a_reply_reaches_only_the_candidate_it_names(gemini):
    """🔴 회신이 섞이면 검증 안 받은 안이 통과한 것으로 읽힌다."""
    first = _pass_one()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    only_first = ids[:1]

    reply = run_proposal(
        SalesProposalInput.model_validate(
            _request_payload(
                is_refeed=True, feedback_attempt=1, feedback=_feedback(only_first)
            )
        )
    )

    resolved = {
        scenario.scenario_id: "FINANCIAL_VALIDATION" in scenario.required_validations
        for scenario in reply.scenarios
    }
    named = [key for key, still_required in resolved.items() if not still_required]
    assert named, resolved
    for scenario in reply.scenarios:
        if scenario.scenario_id in named:
            assert scenario.finance_verdict == "PASS"
        else:
            assert scenario.finance_verdict is None


# ── F. 검증만 받은 실행은 상업조건을 바꾸지 않는다 ────────────────────────

#: 조정안이 없는 되먹임에서 **절대 달라지면 안 되는** 상업조건.
_COMMERCIAL_FIELDS = (
    "item",
    "partner_id",
    "quantity_kg",
    "unit_price_krw",
    "sales_amount_krw",
    "delivery_date",
    "payment_days",
    "payment_terms_type",
    "contract_term_days",
)


def test_validation_only_feedback_does_not_move_the_commercial_terms(gemini):
    """🔴 재무가 «봤다» 고 했을 뿐인데 파는 조건이 바뀌면, 검증한 안과 다른 안이 된다."""
    first = _pass_one()
    second = _pass_two()

    before = {
        scenario.scenario_id: tuple(getattr(scenario, f) for f in _COMMERCIAL_FIELDS)
        for scenario in first.scenarios
    }
    for scenario in second.scenarios:
        #  ⚠️ 계보가 바뀌어도 **원안 기준으로** 대조한다 — 아래 계보 검사가 그 사실을 잠근다.
        original = scenario.parent_scenario_id or scenario.scenario_id
        assert original in before, (original, sorted(before))
        assert tuple(getattr(scenario, f) for f in _COMMERCIAL_FIELDS) == before[original]


def test_validation_only_feedback_records_its_lineage(gemini):
    """되먹임 계보는 기존 계약대로 남는다. **바뀐다면 그것이 기록되어야 한다.**"""
    second = _pass_two()

    for scenario in second.scenarios:
        if scenario.parent_scenario_id is None:
            assert scenario.revision == 0
        else:
            assert scenario.revision >= 1
            assert scenario.scenario_id.startswith(scenario.parent_scenario_id)


# ── G. 판정이 나쁘면 설명까지 가지 않는다 ─────────────────────────────────


def test_a_failing_finance_verdict_keeps_the_candidate_out_of_the_explanation(monkeypatch):
    """🔴 재무가 막은 안을 모델이 설명하면, 막힌 안이 제안처럼 읽힌다."""
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_GEMINI_API_KEY", "test-key")
    spy = _GeminiSpy()
    monkeypatch.setattr(llm_runtime, "_call_gemini", spy)

    reply = _pass_two(verdict="FAIL")

    #  막힌 안은 제안 목록에서 빠진다. 남은 후보가 없으므로 설명할 것도 없다.
    assert reply.scenarios == []
    assert reply.recommended_scenario_id is None
    assert spy.calls == 0
    assert reply.llm.status == "SKIPPED_TEMPLATE"
    assert reply.llm.llm_attempts == 0

    #  ★ **지우지 않고 기록한다.** 왜 빠졌는지가 결정 이력에 남아야 되짚을 수 있다.
    assert [trace.status for trace in reply.decision_trace] == ["INFEASIBLE"] * 3
    assert {trace.finance_verdict for trace in reply.decision_trace} == {"FAIL"}
    for trace in reply.decision_trace:
        assert "REJECTED_BY_FEEDBACK" in trace.exclusion_reasons


def test_the_deterministic_core_owns_the_numbers(gemini):
    """🔴 모델을 거치고도 숫자의 주인은 판매의 결정론 코어다."""
    reply = _pass_two()
    direct = proposal_module._generate_scenarios(
        SalesProposalInput.model_validate(_request_payload())
    )
    by_id = {scenario.scenario_id: scenario for scenario in direct}

    for scenario in reply.scenarios:
        original = scenario.parent_scenario_id or scenario.scenario_id
        source = by_id[original]
        assert scenario.quantity_kg == source.quantity_kg
        assert scenario.unit_price_krw == source.unit_price_krw
        assert scenario.sales_amount_krw == source.sales_amount_krw


def test_the_reported_amount_is_quantity_times_price(gemini):
    """금액은 결정론으로 세운다 — 모델이 만든 값이 아니다."""
    reply = _pass_two()

    for scenario in reply.scenarios:
        if scenario.quantity_kg is None or scenario.unit_price_krw is None:
            continue
        assert scenario.sales_amount_krw == (
            Decimal(scenario.quantity_kg) * Decimal(scenario.unit_price_krw)
        )


# ── G. 설명은 별도 node 다 ────────────────────────────────────────────────
#
# ★ 결정론이 정하고 모델이 설명한다는 말은 **그래프에서 눈으로 보여야** 한다.
#   예전에는 모델 호출이 최종 조립 함수 안에 숨어 있어서, 코드만 보고는 추천이
#   모델보다 먼저 정해지는지 알 수 없었다. 아래 검사들이 그 순서를 잠근다.


def _state_of(reply_input) -> Any:
    from app.sales.graph import _graph

    return _graph().invoke({"request": reply_input})


def _pass_two_request(scenario_ids) -> SalesProposalInput:
    return SalesProposalInput.model_validate(
        _request_payload(
            is_refeed=True, feedback_attempt=1, feedback=_feedback(scenario_ids)
        )
    )


def test_the_first_pass_also_walks_through_the_explanation_node(gemini):
    """첫 실행도 같은 길을 지난다 — 다만 설명할 후보가 없을 뿐이다."""
    state = _state_of(SalesProposalInput.model_validate(_request_payload()))

    assert [entry.get("stage") for entry in state["agent_trace"]] == [
        "prepare_context",
        "classify_situation",
        "generate_candidates",
        "determine_validations",
        "self_check",
        "interpret_recommendation",
        "final_recommendation",
    ]
    #  🔴 **node 를 세웠다고 모델을 부르지 않는다.** 판정 전 안을 설명하면
    #     보지 않은 안이 제안처럼 읽힌다.
    assert gemini.calls == 0
    assert state["reply"].llm.status == "SKIPPED_TEMPLATE"
    assert state["reply"].llm.llm_attempts == 0


def test_the_explanation_node_runs_after_ranking(gemini):
    """순위가 먼저다. 모델은 이미 정해진 것을 말로 옮긴다."""
    first = _pass_one()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    stages = [
        entry.get("stage") for entry in _state_of(_pass_two_request(ids))["agent_trace"]
    ]

    assert stages.index("rank_candidates") < stages.index("interpret_recommendation")
    assert stages.index("self_check") < stages.index("interpret_recommendation")
    assert stages.index("interpret_recommendation") < stages.index("final_recommendation")


def test_the_recommendation_is_already_settled_when_the_node_starts(gemini):
    """🔴 설명 node 가 받은 추천과 최종 답장의 추천이 같다.

    ★ 모델이 추천을 **움직일 수 없다**는 것을 그래프 기록으로 확인한다.
    """
    first = _pass_one()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    state = _state_of(_pass_two_request(ids))
    entry = next(
        item
        for item in state["agent_trace"]
        if item.get("stage") == "interpret_recommendation"
    )

    assert entry["recommended_scenario_id"] == state["reply"].recommended_scenario_id
    assert entry["recommended_scenario_id"] == state["ranked_candidate_ids"][0]
    assert entry["llm_status"] == state["reply"].llm.status
    assert entry["llm_attempts"] == state["reply"].llm.llm_attempts


def test_the_final_node_only_assembles_and_never_calls_the_model(monkeypatch):
    """🔴 최종 조립은 모델을 부르지 않는다 — 앞에서 만든 설명을 옮겨 담을 뿐이다."""
    from app.sales.graph import _final_recommendation
    from app.sales.schemas import SalesRecommendation

    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_GEMINI_API_KEY", "test-key")
    spy = _GeminiSpy()
    monkeypatch.setattr(llm_runtime, "_call_gemini", spy)

    first = _pass_one()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    state = _state_of(_pass_two_request(ids))
    calls_before = spy.calls

    sentinel = SalesRecommendation(
        status="SUCCESS",
        recommended_candidate_id=state["recommendation_id"],
        summary="앞 node 가 만든 설명이다.",
        recommendation_reason="이 문장이 그대로 답장에 실려야 한다.",
        risk_explanation="바뀌면 최종 조립이 모델을 다시 부른 것이다.",
        llm_attempts=1,
    )
    assembled = _final_recommendation({**state, "recommendation": sentinel})

    assert spy.calls == calls_before
    assert assembled["reply"].llm is sentinel
    assert assembled["reply"].recommendation is sentinel
    assert assembled["reply"].recommended_scenario_id == state["recommendation_id"]


def _commercial_snapshot(scenarios) -> list[tuple]:
    return [
        (
            scenario.scenario_id,
            scenario.status,
            scenario.finance_verdict,
            scenario.quantity_kg,
            scenario.unit_price_krw,
            scenario.sales_amount_krw,
            scenario.delivery_date,
            scenario.payment_days,
            scenario.payment_terms_type,
        )
        for scenario in scenarios
    ]


def test_the_explanation_node_does_not_touch_the_business_values(monkeypatch):
    """설명 node 에 들어간 값과 답장에 실린 값이 같다.

    ★ node 가 **불린 그 순간**의 후보를 찍어 두고 최종 답장과 맞춘다. 최종 상태끼리
      비교하면 같은 객체를 두 번 보는 셈이라 아무것도 증명하지 못한다.
    """
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_runtime, "_call_gemini", _GeminiSpy())

    seen: list[list[tuple]] = []
    original = proposal_module._interpret_scenarios

    def watching(scenarios, fixed_recommendation):
        seen.append(_commercial_snapshot(scenarios))
        return original(scenarios, fixed_recommendation)

    monkeypatch.setattr(proposal_module, "_interpret_scenarios", watching)

    first = _pass_one()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    state = _state_of(_pass_two_request(ids))

    #  첫 실행과 두 번째 실행에서 각각 한 번씩, 모두 두 번 불린다.
    assert len(seen) == 2, seen
    assert seen[-1] == _commercial_snapshot(state["reply"].scenarios)
