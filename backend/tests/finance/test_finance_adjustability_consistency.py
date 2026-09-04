"""매입 시나리오 검증의 `adjustability` — 기계 계약과 사람 문장이 갈리지 않는다.

★ 이 파일이 지키는 것.
    · `NOT_ADJUSTABLE` 은 **검증을 하고도 대안이 없었다**는 뜻이다
    · 검증을 안 한 것을 대안 없음으로 포장하지 않는다
    · Planner 가 Tool 을 고르든 말든 최종 계약은 같다
    · 검증된 금액 대안이 없으면 사용자 문장이 "조정하면 된다" 고 단정하지 않는다
    · 재무가 검증하지 않는 축(지급 시기)을 문장에서 권하지 않는다

🔴 예전에는 `conditional` 에 금액 대안 검증이 필수가 아니었다. 그래서 Planner 가
   Tool 을 안 고르면 검증을 **안 한 것**이 `NOT_ADJUSTABLE` 로 나갔고, 문장은 그와
   무관하게 "매입 금액이나 지급 시기를 조정하시면 안전합니다" 라고 말했다.
"""

from unittest.mock import patch

import pytest

from app.finance import messages
from app.finance.application.harness import _ADJUSTMENT_REQUIRED_VERDICTS
from app.finance.application.orchestration import FinanceAgentController
from app.finance.llm.planner import ToolAction
from tests.finance.test_finance_agent import Planner, Port, request, scenario


def _run(payload, actions, finalizer=None):
    planner = Planner(actions)
    with patch("app.finance.execution.save_finance_execution"):
        controller = (
            FinanceAgentController(Port(), planner, finalizer=finalizer)
            if finalizer is not None
            else FinanceAgentController(Port(), planner)
        )
        return controller.run(request("SCENARIO_VALIDATION", payload))


_EVALUATE = ToolAction("evaluate_purchase_scenario")
_VALIDATE = ToolAction("validate_amount_adjustment", {"axis": "amount"})
_FINALIZE = ToolAction(finalize=True)


# ---------------------------------------------------------------------------
# Case 1 — ok 는 조정할 이유가 없다
# ---------------------------------------------------------------------------


def test_ok_scenario_is_not_needed_and_carries_no_adjustment():
    reply, _ = _run(scenario("S1", 100), [_EVALUATE, _FINALIZE])

    assert reply.payload["verdict"] == "ok"
    assert reply.payload["adjustability"] == "NOT_NEEDED"
    assert reply.suggested_adjustments == ()


def test_ok_scenario_does_not_require_the_adjustment_tool():
    _, metadata = _run(scenario("S1", 100), [_EVALUATE, _FINALIZE])

    # 조정할 이유가 없는 결과에 조정 검증을 요구하면 없는 일을 시키는 것이다.
    assert "validate_amount_adjustment" not in metadata.used_tools


# ---------------------------------------------------------------------------
# Case 2·3 — 검증했고 대안이 있으면 ADJUSTABLE
# ---------------------------------------------------------------------------


def test_non_ok_scenario_with_a_verified_alternative_is_adjustable():
    reply, metadata = _run(
        scenario("S1", 1000, payment_schedule=None), [_EVALUATE, _VALIDATE, _FINALIZE]
    )

    assert reply.payload["verdict"] in {"conditional", "reject"}
    assert reply.payload["adjustability"] == "ADJUSTABLE"
    assert reply.suggested_adjustments
    assert reply.suggested_adjustments[0].axis == "amount"
    assert "validate_amount_adjustment" in metadata.used_tools


def test_verified_alternative_is_a_positive_amount():
    reply, _ = _run(
        scenario("S1", 1000, payment_schedule=None), [_EVALUATE, _VALIDATE, _FINALIZE]
    )

    assert reply.suggested_adjustments[0].target_value > 0


# ---------------------------------------------------------------------------
# Case 4 — 미실행을 대안 없음으로 포장하지 않는다  ← 이번 수정의 핵심
# ---------------------------------------------------------------------------


def test_conditional_requires_the_amount_adjustment_capability():
    """🔴 예전에는 `reject` 만 필수였다. `conditional` 이 빠져 있었던 것이 구멍이다."""
    assert _ADJUSTMENT_REQUIRED_VERDICTS == frozenset({"reject", "conditional"})


def test_planner_skipping_the_tool_cannot_finalize_a_non_ok_scenario():
    """Planner 가 금액 대안 Tool 을 안 골라도 그대로 끝나지 않는다.

    Tool 을 고르지 않고 바로 종료를 요청하면 Harness 가 되묻고, 상한까지 계속
    거르면 실행이 실패로 접힌다 — 검증 안 된 상태가 정상 완료로 나가지 않는다.
    """
    reply, metadata = _run(
        scenario("S1", 1000, payment_schedule=None),
        [_EVALUATE, *[_FINALIZE] * 12],
    )

    # 정상 완료가 아니다. 검증되지 않은 NOT_ADJUSTABLE 이 사용자에게 나가지 않는다.
    assert reply.runtime_status != "READY"
    assert reply.payload.get("adjustability") != "NOT_ADJUSTABLE"
    assert "validate_amount_adjustment" not in metadata.used_tools


def test_skipping_the_tool_is_recorded_as_a_replan_not_a_silent_pass():
    _, metadata = _run(
        scenario("S1", 1000, payment_schedule=None),
        [_EVALUATE, *[_FINALIZE] * 12],
    )

    # 되묻은 사실이 이력에 남는다 — 조용히 통과하지 않았다.
    assert metadata.replans > 0


def test_unvalidated_non_ok_scenario_never_reaches_the_result_builder():
    """결과 조립 단계에도 방어가 있다 — 두 층이 같은 사실을 지킨다."""
    from app.finance.application.orchestration import _amount_adjustment_was_evaluated
    from app.finance.state import FinanceAgentState

    state = FinanceAgentState(request=request("SCENARIO_VALIDATION", scenario("S1", 1000)))
    state.tool_order = ["evaluate_purchase_scenario"]

    assert _amount_adjustment_was_evaluated(state) is False


def test_base_violation_is_the_documented_exception():
    """평소 흐름 자체가 최소 현금을 밑돌면 상한이 0 으로 확정된다.

    어떤 금액도 안전하지 않다는 답이 결정론 판정에서 이미 나왔으므로 Tool 을 부르지
    않는다 — 유일한 예외이고, 숨기지 않고 여기 못 박는다.
    """
    from app.finance.application.orchestration import _amount_adjustment_was_evaluated
    from app.finance.state import FinanceAgentState

    state = FinanceAgentState(request=request("SCENARIO_VALIDATION", scenario("S1", 1000)))
    state.tool_order = ["evaluate_purchase_scenario"]
    state.base_state_violated = True

    assert _amount_adjustment_was_evaluated(state) is True


# ---------------------------------------------------------------------------
# Case 5 — 실제로 조정 불가
# ---------------------------------------------------------------------------


def test_validated_but_unsafe_alternative_is_not_adjustable_with_no_adjustment():
    """검증은 돌았는데 안전한 **양수** 대안이 없으면 NOT_ADJUSTABLE 이다.

    제안이 들고 온 대안 금액이 0 이면 검증은 통과하지만 진행할 수 있는 금액이
    아니다 — 조정 대안으로 내보내지 않는다.
    """
    reply, metadata = _run(
        scenario("S1", 1000, payment_schedule=None, candidate_amount_krw=0),
        [_EVALUATE, _VALIDATE, _FINALIZE],
    )

    assert reply.runtime_status == "READY"
    assert reply.payload["verdict"] != "ok"
    assert reply.payload["adjustability"] == "NOT_ADJUSTABLE"
    assert reply.suggested_adjustments == ()
    # 미실행과 구분된다 — 실제로 돌았다.
    assert "validate_amount_adjustment" in metadata.used_tools


def test_not_adjustable_result_does_not_promise_an_adjustment_to_the_user():
    """Case 6 의 실제 실행판 — 기계가 '대안 없음' 이면 문장도 조정을 약속하지 않는다."""
    reply, _ = _run(
        scenario("S1", 1000, payment_schedule=None, candidate_amount_krw=0),
        [_EVALUATE, _VALIDATE, _FINALIZE],
    )

    assert reply.payload["adjustability"] == "NOT_ADJUSTABLE"
    assert "조정 범위" not in reply.reasoning
    assert "조정하시면" not in reply.reasoning


# ---------------------------------------------------------------------------
# Case 6·7 — 사람 문장이 기계 계약과 어긋나지 않는다
# ---------------------------------------------------------------------------


def test_conditional_sentence_does_not_promise_an_adjustment():
    text = messages.FINANCE_EXPLANATIONS["SCENARIO_CONDITIONAL"]

    assert "조정하시면" not in text
    assert "조정하면" not in text


def test_no_explanation_recommends_changing_payment_timing():
    """재무가 검증하는 조정축은 금액 하나다 — 지급 시기를 권하지 않는다."""
    for key, text in messages.FINANCE_EXPLANATIONS.items():
        assert "지급 시기" not in text, key
        assert "지급일" not in text, key


def test_adjustable_sentence_is_only_available_with_a_verified_adjustment():
    without = messages.explanation_keys("SCENARIO_VALIDATION", "conditional")
    with_adjustment = messages.explanation_keys(
        "SCENARIO_VALIDATION", "conditional", has_verified_adjustment=True
    )

    assert without == ["SCENARIO_CONDITIONAL"]
    assert with_adjustment == ["SCENARIO_CONDITIONAL_ADJUSTABLE"]


def test_reject_sentence_pair_follows_the_same_rule():
    assert messages.explanation_keys("SCENARIO_VALIDATION", "reject") == ["SCENARIO_REJECT"]
    assert messages.explanation_keys(
        "SCENARIO_VALIDATION", "reject", has_verified_adjustment=True
    ) == ["SCENARIO_REJECT_ADJUSTABLE"]


def test_only_the_adjustable_sentences_mention_an_adjustment_range():
    for key, text in messages.FINANCE_EXPLANATIONS.items():
        if "조정 범위" in text:
            assert key.endswith("_ADJUSTABLE"), key


def test_adjustable_run_explanation_matches_the_machine_contract():
    reply, _ = _run(
        scenario("S1", 1000, payment_schedule=None), [_EVALUATE, _VALIDATE, _FINALIZE]
    )

    assert reply.payload["adjustability"] == "ADJUSTABLE"
    # 검증된 대안이 실렸으므로 그 사실을 말하는 문장이 나간다.
    assert reply.reasoning == messages.explanation_for(
        "SCENARIO_VALIDATION",
        reply.business_status,
        has_verified_adjustment=True,
    )


# ---------------------------------------------------------------------------
# Case 8 — Provider 가 달라도 업무 의미는 같다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("business_status", ["conditional", "reject"])
@pytest.mark.parametrize("has_adjustment", [True, False])
def test_every_explanation_path_selects_the_same_key(business_status, has_adjustment):
    """LLM Finalizer 와 결정론 대체 경로가 같은 정본을 본다."""
    from app.finance.llm.finalizer import DeterministicFinanceFinalizer

    allowed = messages.explanation_keys(
        "SCENARIO_VALIDATION", business_status, has_verified_adjustment=has_adjustment
    )
    deterministic = DeterministicFinanceFinalizer().finalize(
        mode="SCENARIO_VALIDATION",
        business_status=business_status,
        evidences=(),
        has_verified_adjustment=has_adjustment,
    )

    assert deterministic == messages.FINANCE_EXPLANATIONS[allowed[0]]
    assert len(allowed) == 1, "고를 수 있는 키가 하나면 Provider 가 달라도 결과가 같다"


def test_llm_and_fallback_agree_on_the_same_run():
    """같은 실행에서 모델 성공 경로와 대체 경로가 같은 뜻의 문장을 낸다."""

    class _FailingFinalizer:
        model = "failing"

        def __init__(self):
            self.attempts = 0

        def finalize(self, **kwargs):
            self.attempts += 1
            raise RuntimeError("provider down")

    scenario_payload = scenario("S1", 1000, payment_schedule=None)
    ok_reply, _ = _run(scenario_payload, [_EVALUATE, _VALIDATE, _FINALIZE])
    fallback_reply, _ = _run(
        scenario_payload, [_EVALUATE, _VALIDATE, _FINALIZE], finalizer=_FailingFinalizer()
    )

    assert ok_reply.business_status == fallback_reply.business_status
    assert ok_reply.payload["adjustability"] == fallback_reply.payload["adjustability"]
    assert ok_reply.reasoning == fallback_reply.reasoning
