"""Finalizer 를 **언제 부르는가** — 고를 설명이 실제로 여럿인가.

Finance Finalizer 는 문장을 쓰지 않는다. `explanation_keys()` 가 허용한 키 중 하나를
고르고, 사용자가 읽는 문장은 `FINANCE_EXPLANATIONS[key]` 에서 나온다. 그래서 허용 키가
하나뿐인 결과에서는 **모델이 무엇을 답하든 같은 문장**이 나간다.

이 파일이 잠그는 것은 넷이다.

    ① 허용 키가 하나뿐인 조합에서 LLM 문장과 결정론 문장이 같은가
    ② 그런 조합에서 provider 를 부르지 않는가
    ③ 허용 키가 둘 이상이면 **예전처럼** Finalizer 를 부르는가
    ④ 그렇게 줄여도 사용자에게 나가는 문장이 한 글자도 안 바뀌는가

🔴 ③ 이 이 파일의 핵심이다. 이번 변경은 **Finalizer 제거가 아니라** «고를 것이 없을 때
   묻지 않기» 다. 훗날 어떤 상태에 설명 후보가 둘 생기면 모델 경로가 저절로 살아나야 한다.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from app.finance.application.orchestration import (
    FinanceAgentController,
    fallback_reasoning,
)
from app.finance.llm.planner import DeterministicFinancePlanner
from app.finance.user_messages import FINANCE_EXPLANATIONS, explanation_keys
from tests.finance.test_finance_harness_langchain import (
    Port,
    request,
    scenario_payload,
)

MODES = ("PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION")

#: 재무가 실제로 내보내는 업무 상태 + 모르는 값 하나.
#:
#: ★ 모르는 값을 넣는 이유는 `explanation_keys` 의 마지막 갈래(«판단 못 함»)도
#:   singleton 인지 확인하기 위해서다. 그 자리가 비면 승인성 문장이 새어 나간다.
STATUSES = ("ok", "conditional", "reject", "skipped", "review_required", "unknown")


def _sales_payload() -> dict[str, Any]:
    return {
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
    }


def _request_for(mode: str):
    if mode == "SCENARIO_VALIDATION":
        return request(mode, {"scenarios": [scenario_payload()]})
    if mode == "SALES_VALIDATION":
        return request(mode, _sales_payload())
    return request(mode)


class _CountingFinalizer:
    """불린 횟수와 그때 받은 입력을 적는다. 문장은 **정본을 그대로** 고른다.

    ★ 가짜가 자기 문장을 들고 있으면 이 파일이 «사용자에게 나가는 진짜 문장» 이 아니라
      가짜의 문장을 검사하게 된다.
    """

    model = "counting-finalizer"

    def __init__(self, *, raises: bool = False) -> None:
        self.attempts = 0
        self.raises = raises
        self.seen: list[tuple[str, str, bool]] = []

    def finalize(self, *, mode, business_status, evidences, has_verified_adjustment=False):
        del evidences
        self.attempts += 1
        self.seen.append((mode, business_status, has_verified_adjustment))
        if self.raises:
            raise RuntimeError("finalizer provider is unavailable")
        return fallback_reasoning(
            mode, business_status, has_verified_adjustment=has_verified_adjustment
        )


class _LlmPlanner:
    """결정론 선택을 그대로 쓰되 **모델이 켜진 경로**로 보이게 감싼다.

    ★ `DeterministicFinancePlanner` 를 그대로 주입하면 Controller 가 그것을 «LLM 꺼짐»
      으로 읽어 `llm_status` 가 늘 `DISABLED` 가 된다 — 그러면 FALLBACK 의미를 못 본다.
    """

    model = "wrapped-deterministic-planner"

    def __init__(self) -> None:
        self._inner = DeterministicFinancePlanner()
        self.attempts = 0

    def decide(self, **kwargs: Any):
        self.attempts += 1
        self._inner.attempts = 0
        return self._inner.decide(**kwargs)


def _run(mode: str, finalizer, *, llm_enabled: bool = False):
    planner = _LlmPlanner() if llm_enabled else DeterministicFinancePlanner()
    with patch("app.finance.execution.save_finance_execution"):
        return FinanceAgentController(Port(), planner, finalizer).run(
            _request_for(mode)
        )


def _trace(metadata) -> dict[str, Any]:
    return next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_harness_trace"
    )



# ── ① 설명 후보는 지금 전부 하나다 ───────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("has_adjustment", [False, True])
def test_every_explanation_has_exactly_one_allowed_key(mode, status, has_adjustment):
    """🔴 **이번 최적화의 전제다.** 여기가 깨지면 조건부 생략의 근거가 사라진다.

    후보가 하나뿐이라는 것은 모델에게 물어도 답이 정해져 있다는 뜻이다. 반대로 언젠가
    어떤 상태에 후보가 둘 생기면 이 검사가 실패해서 **그 사실을 먼저 알려 준다** —
    그때는 조건부 경로가 자동으로 모델을 다시 부른다.
    """
    allowed = explanation_keys(mode, status, has_verified_adjustment=has_adjustment)

    assert len(allowed) == 1, (mode, status, has_adjustment, allowed)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("has_adjustment", [False, True])
def test_the_only_key_and_the_deterministic_sentence_agree(mode, status, has_adjustment):
    """후보가 하나면 모델이 고를 수 있는 문장과 결정론 문장이 **같은 문자열**이다."""
    allowed = explanation_keys(mode, status, has_verified_adjustment=has_adjustment)
    only = FINANCE_EXPLANATIONS[allowed[0]]
    deterministic = fallback_reasoning(
        mode, status, has_verified_adjustment=has_adjustment
    )

    assert only == deterministic, (mode, status, has_adjustment)


# ── ② 후보가 하나인 실행은 provider 를 부르지 않는다 ────────────────────


@pytest.mark.parametrize(
    ("mode", "expected_finalizer_calls"),
    [
        ("PRE_PURCHASE", 0),
        ("SCENARIO_VALIDATION", 0),
        # 판매는 이 fixture 에서 답이 서지 않아 예전에도 부르지 않았다.
        ("SALES_VALIDATION", 0),
    ],
)
def test_a_single_candidate_never_reaches_the_finalizer(mode, expected_finalizer_calls):
    """🔴 **자리 수를 고정한다.** 여기가 다시 늘면 provider 왕복이 조용히 돌아온 것이다."""
    finalizer = _CountingFinalizer()
    _reply, _metadata = _run(mode, finalizer)

    assert finalizer.attempts == expected_finalizer_calls


def test_the_scenario_run_explains_itself_without_any_provider_call():
    """SCENARIO 는 Planner 도 Finalizer 도 부르지 않는다 — provider 왕복 0."""
    finalizer = _CountingFinalizer()
    reply, metadata = _run("SCENARIO_VALIDATION", finalizer)
    trace = _trace(metadata)

    assert trace["llm_calls"] == 0  # Planner
    assert finalizer.attempts == 0  # Finalizer
    assert reply.runtime_status == "READY"
    #  그런데도 설명은 나간다 — 결정론이 가진 정본 문장이다.
    assert reply.reasoning
    assert reply.reasoning == fallback_reasoning(
        "SCENARIO_VALIDATION",
        reply.business_status,
        has_verified_adjustment=bool(reply.suggested_adjustments),
    )


# ── ③ 고를 것이 생기면 예전 경로가 살아난다 ─────────────────────────────


def test_two_candidates_bring_the_finalizer_back():
    """🔴 **이번 변경은 Finalizer 제거가 아니다.**

    후보가 둘이 되는 날 모델이 다시 불려야 한다. 그렇지 않으면 이 구조는 «지금은 후보가
    하나니까» 가 아니라 «앞으로도 안 묻겠다» 가 된다 — 둘은 다른 결정이다.
    """
    finalizer = _CountingFinalizer()
    real_keys = explanation_keys

    def two_keys(mode, business_status, *, has_verified_adjustment=False):
        first = real_keys(
            mode, business_status, has_verified_adjustment=has_verified_adjustment
        )
        #  실제로 존재하는 키 두 개를 준다 — 없는 키를 주면 문장 조회가 먼저 터진다.
        return [first[0], "SCENARIO_NOT_CONCLUDED"]

    with patch(
        "app.finance.application.orchestration.explanation_keys", side_effect=two_keys
    ):
        reply, _metadata = _run("SCENARIO_VALIDATION", finalizer)

    assert finalizer.attempts == 1
    assert finalizer.seen[0][0] == "SCENARIO_VALIDATION"
    assert reply.runtime_status == "READY"


def test_a_failing_finalizer_still_answers_when_there_was_a_choice():
    """고를 것이 있어 불렀는데 모델이 죽으면 **예전 대체 계약** 그대로다."""
    finalizer = _CountingFinalizer(raises=True)
    real_keys = explanation_keys

    def two_keys(mode, business_status, *, has_verified_adjustment=False):
        first = real_keys(
            mode, business_status, has_verified_adjustment=has_verified_adjustment
        )
        return [first[0], "SCENARIO_NOT_CONCLUDED"]

    with patch(
        "app.finance.application.orchestration.explanation_keys", side_effect=two_keys
    ):
        reply, metadata = _run("SCENARIO_VALIDATION", finalizer, llm_enabled=True)

    assert finalizer.attempts == 1
    assert reply.runtime_status == "READY"
    assert metadata.llm_status == "FALLBACK"
    assert reply.reasoning == fallback_reasoning(
        "SCENARIO_VALIDATION",
        reply.business_status,
        has_verified_adjustment=bool(reply.suggested_adjustments),
    )


def test_no_candidate_at_all_is_not_quietly_accepted():
    """후보가 0이면 **조용히 넘기지 않는다.**

    지금 계약상 일어날 수 없는 상태다. 일어난다면 고를 문장이 없다는 뜻이고, 그때
    아무 문장이나 고르면 보지도 않은 결과에 설명이 붙는다. 기존 실패 계약으로 닫힌다.
    """
    finalizer = _CountingFinalizer()
    from app.finance import user_messages as messages

    with patch(
        "app.finance.application.orchestration.explanation_keys", return_value=[]
    ):
        reply, _metadata = _run("SCENARIO_VALIDATION", finalizer, llm_enabled=True)

    #  모델을 부르지도, 없는 키로 문장을 만들지도 않는다.
    assert finalizer.attempts == 0
    #  업무 결과는 그대로 서 있고, 설명만 «말할 수 없다» 로 닫힌다.
    assert reply.runtime_status == "READY"
    assert reply.reasoning == messages.INTERNAL_FAILURE


# ── ④ 부르지 않은 실행의 provider 관측이 거짓이 되지 않는다 ─────────────


def test_a_run_that_called_nothing_does_not_look_like_it_used_a_provider():
    """🔴 **안 불렀는데 부른 것처럼 읽히면 안 된다.**

    `finance_llm_provider` 관측은 LLM 설정이 켜져 있으면 늘 실린다. 그 칸이 뜻하는 것은
    **설정된 provider 와 지금 유효한 provider** 이지 «이번 실행이 provider 를 썼다» 가
    아니다. 그래서 한 번도 부르지 않은 실행에서는 대체가 켜지지 않은 채 남아야 한다.

    ★ 이 검사가 없으면, 훗날 누가 이 관측을 «호출 여부» 로 읽어도 아무도 못 막는다.
    """
    finalizer = _CountingFinalizer()
    reply, metadata = _run("SCENARIO_VALIDATION", finalizer, llm_enabled=True)

    assert finalizer.attempts == 0
    assert _trace(metadata)["llm_calls"] == 0
    assert metadata.llm_status == "SKIPPED_TEMPLATE"
    assert metadata.llm_attempts == 0
    assert reply.runtime_status == "READY"

    observations = [json.loads(item) for item in metadata.observations]
    provider = [
        item
        for item in observations
        if item.get("observation_type") == "finance_llm_provider"
    ]
    #  주입한 Planner 는 설정 provider 가 아니므로 이 관측 자체가 없다. 있다면
    #  «대체가 일어났다» 로 읽힐 값이 없어야 한다.
    for item in provider:
        assert item["provider_fallback_used"] is False
        assert item["provider_fallback_reason"] is None
