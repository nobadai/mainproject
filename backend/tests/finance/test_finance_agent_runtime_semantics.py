"""LLMStatus · bounded replan · Provider 대체 의미를 실제 실행으로 고정한다.

이 파일이 지키는 것은 **의미**다. 값이 나오는지가 아니라, 같은 값이 나올 때
`llm_status` 와 `replans` 가 그날 실제로 있었던 일을 말하는지를 본다.

실 LLM 을 부르지 않는다 — Planner/Finalizer 는 전부 fake 다.
"""

import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import messages
from app.finance.application.orchestration import FinanceAgentController
from app.finance.db import FinanceDataNotReady
from app.finance.llm.client import finance_llm_enabled
from app.finance.llm.finalizer import DeterministicFinanceFinalizer
from app.finance.llm.planner import (
    DeterministicFinancePlanner,
    FinancePlannerContractViolation,
    FinancePlannerUnavailable,
    ToolAction,
)
from app.finance.schemas import FinancePolicy
from app.master.envelope import AgentRequest, ExecutionContext


class Port:
    """Controller 가 쓰는 최소 Finance DataPort."""

    def load_finance_position(self, as_of):
        del as_of
        return {
            "finance_state_id": "FIN-STATE-SEMANTICS",
            "current_cash_krw": Decimal(1000),
            "current_debt_krw": Decimal(0),
        }

    def load_policy(self, as_of, policy_version):
        del as_of, policy_version
        return FinancePolicy(
            purchase_payment_days=1,
            payroll_date=10,
            monthly_labor_cost_krw=Decimal(100),
            minimum_cash_balance_krw=Decimal(100),
            cashflow_projection_days=30,
            cash_priority_reference="minimum_cash_balance_krw",
            cash_priority_high_ratio=Decimal(1),
            cash_priority_medium_ratio=Decimal(2),
            policy_version="v1.3-PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            source_refs={
                "payroll_date": "POL-PAYROLL-DATE",
                "monthly_labor_cost_krw": "FACT-PAYROLL-AMOUNT",
                "purchase_payment_days": "policy:purchase-days",
                "minimum_cash_balance_krw": "policy:min-cash",
                "cash_priority_reference": "policy:pressure",
                "cash_priority_high_ratio": "policy:pressure-high",
                "cash_priority_medium_ratio": "policy:pressure-medium",
            },
        )

    def load_payroll(self, as_of, horizon):
        del as_of, horizon
        return Decimal(100)

    def load_obligations(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_receivables(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_debt_schedule(self, as_of, horizon):
        del as_of, horizon
        return []


class NoRefPort(Port):
    """Evidence 를 받칠 정책 출처가 없는 그날의 사실."""

    def load_policy(self, as_of, policy_version):
        policy = super().load_policy(as_of, policy_version)
        return policy.model_copy(
            update={
                "source_refs": {
                    key: value
                    for key, value in policy.source_refs.items()
                    if key != "minimum_cash_balance_krw"
                }
            }
        )


class BrokenPort(Port):
    """실제 programmer bug — 계약에 없는 예외."""

    def load_finance_position(self, as_of):
        del as_of
        raise AttributeError("this is a real programming bug, not a missing fact")


class ScriptedPlanner:
    """대본대로 답하는 Planner. `attempts` 는 실제 호출 수를 센다."""

    model = "scripted-planner"

    def __init__(self, actions):
        self.actions = list(actions)
        self.attempts = 0
        self.seen_allowed = []

    def decide(self, *, allowed_tools, missing_capabilities, **_kwargs):
        del missing_capabilities
        self.attempts += 1
        self.seen_allowed.append(frozenset(allowed_tools))
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class ScriptedFinalizer:
    model = "scripted-finalizer"

    def __init__(self, *, fail: bool = False):
        self.attempts = 0
        self.fail = fail

    def finalize(self, *, mode, business_status, evidences, has_verified_adjustment=False):
        """★ 문장은 정본(`messages`)에서 고른다 — 가짜가 자기 말투를 갖지 않는다."""
        del evidences
        self.attempts += 1
        if self.fail:
            raise TimeoutError("finalization timeout")
        return messages.explanation_for(mode, business_status)


class UnavailablePlanner:
    """Provider 계층이 모두 실행 불가라고 분류한 Planner."""

    model = "unavailable-llm-planner"

    def __init__(self):
        self.attempts = 0

    def decide(self, **_kwargs):
        self.attempts += 1
        raise FinancePlannerUnavailable("all configured Finance LLM providers unavailable")


def request(mode="PRE_PURCHASE", payload=None):
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-semantics",
            as_of=date(2025, 1, 1),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode=mode,
        payload=payload or {},
    )


def pre_purchase_plan():
    return [
        ToolAction("assess_finance_position"),
        ToolAction("project_cashflow"),
        ToolAction("calculate_purchase_finance_cap"),
        ToolAction("analyze_payment_pressure"),
        ToolAction(finalize=True),
    ]


def scenario_payload(amount, *, max_price=None):
    quantity = Decimal(100)
    return {
        "proposal_id": "P-1",
        "scenario_id": "S-1",
        "total_amount_krw": amount,
        "total_qty_kg": quantity,
        "max_price": max_price if max_price is not None else Decimal(amount) / quantity,
        "split_plan": [{"seq": 1, "date": "2025-01-01", "qty_kg": quantity}],
        "meta": {"as_of": "2025-01-01"},
    }


def sales_payload():
    return {
        "scenario_id": "SC-1",
        "partner_id": "P-1",
        "item": "red_pepper",
        "quantity_kg": "100",
        "unit_price_krw": "10000",
        "reported_sales_amount_krw": "1000000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": "2025-01-05",
        "source_ref": "SALES:R-1",
    }


@pytest.fixture(autouse=True)
def _no_persistence():
    with patch("app.finance.execution.save_finance_execution"):
        yield


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch):
    """`.env` 가 테스트 결과를 바꾸지 않게 한다 — 설정 의미만 본다."""
    monkeypatch.setattr("app.finance.llm.client._load_finance_environment", lambda: None)


# ---------------------------------------------------------------------------
# §12 LLM 활성화 우선순위
# ---------------------------------------------------------------------------


def test_finance_llm_enabled_prefers_the_finance_key(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("FINANCE_LLM_ENABLED", "true")
    assert finance_llm_enabled() is True


def test_finance_llm_falls_back_to_the_shared_key(monkeypatch):
    monkeypatch.delenv("FINANCE_LLM_ENABLED", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "false")
    assert finance_llm_enabled() is False


def test_finance_llm_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("FINANCE_LLM_ENABLED", raising=False)
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    assert finance_llm_enabled() is True


def test_finance_provider_does_not_inherit_the_global_provider(monkeypatch):
    """★ 전역을 ollama 로 둔 배포에서도 재무는 Gemini 다 (§12)."""
    from app.finance.llm.client import _finance_provider_name

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("FINANCE_LLM_PROVIDER", raising=False)
    assert _finance_provider_name() == "gemini"


def test_disabled_finance_llm_builds_no_provider(monkeypatch):
    """껐으면 Provider 를 만들지 않는다 — API 키도 로컬 서버도 확인하러 나가지 않는다."""
    from app.finance.llm.planner import _configured_finance_llms

    monkeypatch.setenv("FINANCE_LLM_ENABLED", "false")
    planner, finalizer, provider_state = _configured_finance_llms()

    assert isinstance(planner, DeterministicFinancePlanner)
    assert isinstance(finalizer, DeterministicFinanceFinalizer)
    assert provider_state is None


# ---------------------------------------------------------------------------
# §13 LLMStatus 의미
# ---------------------------------------------------------------------------


def test_llm_disabled_runs_deterministically_and_records_disabled(monkeypatch):
    """끈 상태 — 결정론 Planner 가 Tool 을 고르고 이력에는 `DISABLED` 가 남는다."""
    monkeypatch.setenv("FINANCE_LLM_ENABLED", "false")
    reply, metadata = FinanceAgentController(Port()).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.llm_status == "DISABLED"
    assert metadata.llm_fallback_used is False
    # 껐어도 Tool 은 전부 돌았다 — 숫자는 여전히 결정론 Tool 이 만든다.
    # ★ `project_cashflow` 가 **이력에 있다.** capability 소유가 1:1 이라 cap 과
    #   압박도가 남의 capability 를 대신 채우지 않고, 선행 실행은 Harness 가 드러내
    #   놓고 강제한다 — 예전처럼 안에서 몰래 도는 투영이 없다.
    assert set(metadata.used_tools) == {
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    }
    assert reply.payload["finance_cap_amount_krw"]


def test_llm_enabled_but_never_invoked_is_skipped_not_disabled():
    """🔴 켜 뒀는데 부를 일이 없었던 실행을 `DISABLED` 로 적으면 안 된다 (§13).

    Controller 가 첫 Planner 호출 전에 접히면 호출 수는 0 이지만, 그것은
    *"LLM 을 안 켰다"* 가 아니라 *"이번엔 부를 일이 없었다"* 다.
    """
    planner = ScriptedPlanner([])
    controller = FinanceAgentController(Port(), planner, ScriptedFinalizer())
    # payload 검증 단계에서 접힌다 — Planner 까지 가지 않는다.
    reply, metadata = controller.run(
        request("SCENARIO_VALIDATION", {"scenarios": []})
    )

    assert reply.runtime_status == "ERROR"
    assert planner.attempts == 0
    assert metadata.llm_attempts == 0
    assert metadata.llm_status == "SKIPPED_TEMPLATE"


def test_llm_success_is_recorded_when_the_model_answered():
    planner = ScriptedPlanner(pre_purchase_plan())
    finalizer = ScriptedFinalizer()
    reply, metadata = FinanceAgentController(Port(), planner, finalizer).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.llm_status == "SUCCESS"
    assert metadata.llm_fallback_used is False
    assert metadata.llm_attempts == planner.attempts + finalizer.attempts


def test_finalizer_failure_falls_back_deterministically():
    """Finalizer 가 죽어도 답은 나간다 — 검증된 Evidence 가 이미 있기 때문이다."""
    planner = ScriptedPlanner(pre_purchase_plan())
    finalizer = ScriptedFinalizer(fail=True)
    reply, metadata = FinanceAgentController(Port(), planner, finalizer).run(request())

    assert reply.runtime_status == "READY"
    # ★ 문장을 그대로 적지 않는다 — 말투는 바뀔 수 있고, 지켜야 하는 것은
    #   **LLM 경로와 대체 경로가 같은 설명을 낸다**는 것이다.
    assert reply.reasoning == messages.explanation_for("PRE_PURCHASE", "ok")
    assert metadata.llm_status == "FALLBACK"
    assert metadata.llm_fallback_used is True


def test_disabled_finalizer_fallback_stays_disabled(monkeypatch):
    """끈 상태에서 결정론 설명을 쓴 것은 `FALLBACK` 이 아니다 — 애초에 안 불렀다."""
    monkeypatch.setenv("FINANCE_LLM_ENABLED", "false")

    class _BrokenDeterministicFinalizer(DeterministicFinanceFinalizer):
        def finalize(self, **kwargs):
            del kwargs
            self.attempts += 1
            raise TimeoutError("unreachable in practice")

    controller = FinanceAgentController(Port())
    controller.finalizer = _BrokenDeterministicFinalizer()
    _reply, metadata = controller.run(request())

    assert metadata.llm_status == "DISABLED"
    assert metadata.llm_fallback_used is False


# ---------------------------------------------------------------------------
# §15 bounded replan
# ---------------------------------------------------------------------------


def test_contract_violation_is_replanned_not_failed():
    """🔴 예전에는 계약 위반이 `decide()` 안에서 예외가 되어 실행 전체를 접었다.

    그래서 `_guard_replan` 은 있으나 마나였고 `metadata.replans` 는 늘 0 이었다.
    회복 가능한 잘못은 **왜 반려됐는지 알려주고 다시 묻는다.**
    """
    planner = ScriptedPlanner(
        [
            FinancePlannerContractViolation("selected a tool outside allowed_tools"),
            *pre_purchase_plan(),
        ]
    )
    reply, metadata = FinanceAgentController(Port(), planner, ScriptedFinalizer()).run(
        request()
    )

    assert reply.runtime_status == "READY"
    assert metadata.replans == 1
    assert planner.attempts == 6  # 반려 1 + 정상 5


def test_replan_guard_reaches_the_next_planner_prompt():
    """반려 사유는 GUARD observation 으로 남아 다음 호출 입력에 들어간다."""
    planner = ScriptedPlanner(
        [
            FinancePlannerContractViolation("finalized while capabilities were missing"),
            *pre_purchase_plan(),
        ]
    )
    _reply, metadata = FinanceAgentController(Port(), planner, ScriptedFinalizer()).run(
        request()
    )

    guards = [item for item in metadata.observations if '"type": "GUARD"' in item]
    assert len(guards) == 1
    assert "finalized while capabilities were missing" in guards[0]


def test_disallowed_tool_selection_is_replanned():
    """검증을 하지 않는 Planner 가 허용 밖 Tool 을 골라도 Controller 가 잡는다."""
    planner = ScriptedPlanner(
        [ToolAction("evaluate_purchase_scenario"), *pre_purchase_plan()]
    )
    reply, metadata = FinanceAgentController(Port(), planner, ScriptedFinalizer()).run(
        request()
    )

    assert reply.runtime_status == "READY"
    assert metadata.replans == 1
    assert "evaluate_purchase_scenario" not in metadata.used_tools


def test_replan_is_bounded_and_then_fails():
    """무한히 되묻지 않는다. 상한을 넘으면 최종 실패이고 `FALLBACK` 으로 남는다."""
    planner = ScriptedPlanner([FinancePlannerContractViolation("bad") for _ in range(10)])
    reply, metadata = FinanceAgentController(
        Port(), planner, ScriptedFinalizer(), max_replans=2
    ).run(request())

    assert reply.runtime_status == "ERROR"
    assert reply.payload == {}
    assert metadata.replans == 2
    assert metadata.llm_status == "FALLBACK"
    assert metadata.llm_fallback_used is True


def test_provider_outage_fails_immediately_without_replanning():
    """Provider 장애는 되물어도 같다 — replan 으로 숨기지 않는다."""
    planner = ScriptedPlanner([TimeoutError("provider is down")])
    reply, metadata = FinanceAgentController(Port(), planner, ScriptedFinalizer()).run(
        request()
    )

    assert reply.runtime_status == "ERROR"
    assert metadata.replans == 0
    assert metadata.llm_status == "FALLBACK"


# ---------------------------------------------------------------------------
# §16 RUNTIME_NOT_READY 와 ERROR 의 구분
# ---------------------------------------------------------------------------


def test_missing_non_payroll_source_ref_keeps_running_and_reports_it():
    """★ 급여만 특별하다 — 나머지 정책 출처가 없어도 실행은 계속한다.

    다만 세 가지를 동시에 지킨다.
      · 지어낸 ref 를 붙이지 않는다
      · 근거를 못 다는 claim 은 payload 에서도 뺀다 (숫자만 남기면 봉투가 문다)
      · 뺐다는 사실을 `missing_data` 로 밝힌다
    """
    planner = ScriptedPlanner(pre_purchase_plan())
    reply, metadata = FinanceAgentController(
        NoRefPort(), planner, ScriptedFinalizer()
    ).run(request())

    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    # 계산은 그대로 돌았다 — Finance Cap 은 나온다.
    assert reply.payload["finance_cap_amount_krw"]
    assert set(metadata.used_tools) >= {"calculate_purchase_finance_cap"}

    # 근거를 못 다는 두 claim 만 빠졌다.
    assert "minimum_cash_balance_krw" not in reply.payload
    assert "critical_payment_dates" not in reply.payload
    assert "minimum_cash_balance_krw@policy_source_ref" in reply.missing_data

    # 지어낸 ref 가 없다. 정책값 근거가 스냅샷 id 로 떨어지지도 않는다 —
    # `available_cash` 처럼 **실제로 재무 상태 행에서 온 값**만 그 id 를 쓴다.
    for evidence in reply.evidences:
        for ref in evidence.ref_ids:
            assert not ref.startswith("finance-policy:")
        if evidence.source == "persona":
            assert "FIN-STATE-SEMANTICS" not in evidence.ref_ids


def test_missing_payroll_source_ref_stays_fail_closed():
    """급여 출처가 없으면 계산 자체가 성립하지 않는다 — 여기만 실행을 세운다.

    출처 없는 급여 이벤트를 만들지 않으므로(재무 #63 · M-23) 급여 유출이 통째로
    빠지고, 그 상태의 `finance_cap` 은 **낙관적으로 틀린다.**
    """

    class _NoPayrollRef(Port):
        def load_policy(self, as_of, policy_version):
            policy = super().load_policy(as_of, policy_version)
            return policy.model_copy(
                update={
                    "source_refs": {
                        key: value
                        for key, value in policy.source_refs.items()
                        if key != "payroll_date"
                    }
                }
            )

    planner = ScriptedPlanner(pre_purchase_plan())
    reply, _metadata = FinanceAgentController(
        _NoPayrollRef(), planner, ScriptedFinalizer()
    ).run(request())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("payroll_date@policy_source_ref",)
    assert reply.payload == {}
    assert reply.evidences == ()


def test_missing_data_port_fact_is_not_ready():
    class _NoPayroll(Port):
        def load_payroll(self, as_of, horizon):
            del as_of, horizon

    planner = ScriptedPlanner(pre_purchase_plan())
    reply, _metadata = FinanceAgentController(
        _NoPayroll(), planner, ScriptedFinalizer()
    ).run(request())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("payroll_schedule",)


def test_programmer_bug_stays_an_error():
    """★ 모든 예외를 `FinanceDataNotReady` 로 접으면 진짜 버그가 조용히 숨는다."""
    planner = ScriptedPlanner(pre_purchase_plan())
    reply, metadata = FinanceAgentController(
        BrokenPort(), planner, ScriptedFinalizer()
    ).run(request())

    assert reply.runtime_status == "ERROR"
    assert reply.missing_data == ()
    # 사용자는 **다음에 할 일**을 받는다. 스택 조각을 받지 않는다.
    assert reply.reasoning == messages.INTERNAL_FAILURE
    # 기술적 사유는 사라지지 않는다 — 개발자가 읽는 Trace 에 그대로 남는다.
    trace = next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_harness_trace"
    )
    assert "programming bug" in trace["failure_reason"]
    assert trace["failure_kind"] == "INTERNAL"


def test_finance_data_not_ready_carries_its_key():
    with pytest.raises(FinanceDataNotReady) as raised:
        Port().load_debt_schedule  # noqa: B018 - 계약 확인용
        raise FinanceDataNotReady("debt_policy")
    assert raised.value.key == "debt_policy"


# ---------------------------------------------------------------------------
# §17 Provider 대체와 deterministic fallback 은 다른 것이다
# ---------------------------------------------------------------------------


def test_provider_fallback_is_not_a_deterministic_fallback(monkeypatch):
    """Gemini→Gemma 는 **LLM 이 답한 것**이다. `FALLBACK` 이 아니라 `SUCCESS` 다.

    대체 사실 자체는 observations 의 `finance_llm_provider` 로 남아 추적된다.
    """
    import json
    import urllib.error

    from app.finance.llm.planner import (
        _AvailabilityFallbackFinancePlanner,
        _ProviderFallbackState,
    )

    monkeypatch.setenv("FINANCE_LLM_ENABLED", "true")
    state = _ProviderFallbackState(primary_provider="gemini", effective_provider="gemini")

    class _DownGemini:
        model = "gemini-3.5-flash-lite"
        attempts = 0

        def decide(self, **_kwargs):
            self.attempts += 1
            raise urllib.error.HTTPError("url", 429, "rate limited", {}, None)

    gemma = ScriptedPlanner(pre_purchase_plan())
    gemma.model = "gemma3:4b"
    planner = _AvailabilityFallbackFinancePlanner(_DownGemini(), gemma, state)
    controller = FinanceAgentController(Port(), planner, ScriptedFinalizer())
    controller._provider_state = state
    controller.llm_enabled = True

    reply, metadata = controller.run(request())

    assert reply.runtime_status == "READY"
    # Provider 는 바뀌었지만 LLM 은 답했다.
    assert metadata.llm_status == "SUCCESS"
    assert metadata.llm_fallback_used is False
    assert planner.model == "gemma3:4b"  # 대체 이후의 유효 모델

    observation = next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_llm_provider"
    )
    assert observation["primary_provider"] == "gemini"
    assert observation["effective_provider"] == "ollama"
    assert observation["provider_fallback_used"] is True
    assert observation["provider_fallback_reason"] == "HTTP_429"


def test_calculations_survive_a_provider_change(monkeypatch):
    """Provider 가 바뀌어도 Tool/Rule 결과는 같아야 한다 (§17)."""
    monkeypatch.setenv("FINANCE_LLM_ENABLED", "true")
    first, _ = FinanceAgentController(
        Port(), ScriptedPlanner(pre_purchase_plan()), ScriptedFinalizer()
    ).run(request())
    second, _ = FinanceAgentController(
        Port(), ScriptedPlanner(pre_purchase_plan()), ScriptedFinalizer()
    ).run(request())

    assert first.payload == second.payload
    assert [item.claim for item in first.evidences] == [
        item.claim for item in second.evidences
    ]


def _business_identity(reply):
    return (
        reply.runtime_status,
        reply.business_status,
        reply.payload,
        reply.evidences,
        reply.suggested_adjustments,
    )


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        (scenario_payload(600), "ok"),
        (scenario_payload(700, max_price=Decimal(9)), "conditional"),
        (scenario_payload(900), "reject"),
    ],
)
def test_planner_unavailability_preserves_each_scenario_business_result(
    payload, expected_status
):
    """ok/conditional/reject와 검증된 조정은 Planner 상태가 아니라 Rule이 정한다."""
    expected, expected_metadata = FinanceAgentController(
        Port(), DeterministicFinancePlanner()
    ).run(request("SCENARIO_VALIDATION", payload))
    actual, metadata = FinanceAgentController(Port(), UnavailablePlanner()).run(
        request("SCENARIO_VALIDATION", payload)
    )

    assert expected.business_status == expected_status
    assert _business_identity(actual) == _business_identity(expected)
    assert metadata.used_tools == expected_metadata.used_tools
    assert metadata.rules_applied == expected_metadata.rules_applied
    assert metadata.llm_status == "FALLBACK"
    assert metadata.llm_fallback_used is True


def test_planner_unavailability_preserves_sales_runtime_not_ready_meaning():
    """실제 여신/채권 사실 부재는 LLM fallback으로 READY가 되지 않는다."""
    expected, expected_metadata = FinanceAgentController(
        Port(), DeterministicFinancePlanner()
    ).run(request("SALES_VALIDATION", sales_payload()))
    actual, metadata = FinanceAgentController(Port(), UnavailablePlanner()).run(
        request("SALES_VALIDATION", sales_payload())
    )

    assert expected.runtime_status == "RUNTIME_NOT_READY"
    assert _business_identity(actual) == _business_identity(expected)
    assert metadata.used_tools == expected_metadata.used_tools == (
        "evaluate_sales_scenario",
    )
    assert metadata.llm_status == "FALLBACK"


def test_planner_unavailability_does_not_hide_finance_data_failure():
    class _MissingFinancePosition(Port):
        def load_finance_position(self, as_of):
            del as_of
            raise FinanceDataNotReady("finance_position")

    reply, metadata = FinanceAgentController(
        _MissingFinancePosition(), UnavailablePlanner()
    ).run(request())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("finance_position",)
    assert metadata.llm_status == "FALLBACK"


def test_planner_unavailability_does_not_hide_deterministic_tool_failure():
    reply, metadata = FinanceAgentController(BrokenPort(), UnavailablePlanner()).run(
        request()
    )

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert metadata.llm_status == "FALLBACK"


def test_planner_fallback_keeps_the_existing_tool_budget():
    planner = ScriptedPlanner(
        [
            ToolAction("assess_finance_position"),
            FinancePlannerUnavailable("providers unavailable"),
        ]
    )
    reply, metadata = FinanceAgentController(
        Port(), planner, max_tool_calls=3
    ).run(request())

    trace = next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_harness_trace"
    )
    assert reply.runtime_status == "ERROR"
    assert trace["tool_calls"] == trace["max_tool_calls"] == 3
    assert metadata.llm_status == "FALLBACK"


def test_planner_fallback_keeps_the_existing_replan_count():
    planner = ScriptedPlanner(
        [
            FinancePlannerContractViolation("invalid tool selection"),
            FinancePlannerUnavailable("providers unavailable"),
        ]
    )
    reply, metadata = FinanceAgentController(Port(), planner).run(request())

    assert reply.runtime_status == "READY"
    assert metadata.replans == 1
    assert metadata.llm_status == "FALLBACK"


# ---------------------------------------------------------------------------
# 숫자 소유권 — Planner 는 숫자를 만들 수 없다
# ---------------------------------------------------------------------------


def test_planner_supplied_numbers_never_reach_the_payload():
    """★ Planner 가 인자에 숫자를 실어도 Controller 가 원천 값으로 덮어쓴다.

    `validate_amount_adjustment` 는 Finance 가 유일하게 값을 받는 Tool 이라
    여기가 뚫리면 LLM 이 만든 금액이 결과에 들어간다.
    """
    payload = {
        "proposal_id": "P-1",
        "scenario_id": "S-1",
        "total_amount_krw": 100000,
        # 지급 일정이 없으면 재무는 **제출된 사실**에서 재구성한다 — 그 사실이 없으면
        # STRESS 금액을 만들 수 없어 fail-closed 다.
        "total_qty_kg": 100,
        "max_price": 1000,
        "split_plan": [{"seq": 1, "date": "2025-01-01", "qty_kg": 100}],
        "meta": {"as_of": "2025-01-01"},
    }
    invented = 999_999_999
    planner = ScriptedPlanner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction(
                "validate_amount_adjustment",
                {"axis": "amount", "candidate_amount_krw": invented},
            ),
            ToolAction(finalize=True),
        ]
    )
    reply, _metadata = FinanceAgentController(
        Port(), planner, ScriptedFinalizer()
    ).run(request("SCENARIO_VALIDATION", payload))

    assert reply.runtime_status == "READY"
    assert float(invented) not in [
        value for value in reply.payload.values() if isinstance(value, (int, float))
    ]
    for evidence in reply.evidences:
        assert evidence.value != float(invented)


def test_reasoning_may_not_introduce_numbers():
    """설명은 검증된 Evidence 를 가리킬 뿐, 새 숫자를 만들 수 없다."""

    class _NumericFinalizer(ScriptedFinalizer):
        def finalize(self, **kwargs):
            del kwargs
            self.attempts += 1
            return "Finance cap is 12345 KRW."

    planner = ScriptedPlanner(pre_purchase_plan())
    reply, metadata = FinanceAgentController(
        Port(), planner, _NumericFinalizer()
    ).run(request())

    assert "12345" not in reply.reasoning
    assert metadata.llm_status == "FALLBACK"


def test_deterministic_planner_only_picks_from_allowed_tools():
    """결정론 Planner 도 허용 밖 Tool 을 고르지 않는다 — 선택만 대신한다."""
    planner = DeterministicFinancePlanner()
    action = planner.decide(
        request=request(),
        allowed_tools=frozenset({"assess_finance_position"}),
        observations=(),
        missing_capabilities=("finance_position",),
    )
    assert action.tool_name == "assess_finance_position"
    assert action.finalize is False

    done = planner.decide(
        request=request(),
        allowed_tools=frozenset({"assess_finance_position"}),
        observations=(),
        missing_capabilities=(),
    )
    assert done.finalize is True
    assert done.tool_name is None
