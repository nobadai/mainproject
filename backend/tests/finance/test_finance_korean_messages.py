"""사용자에게 보이는 재무 문장은 한국어다 — **기계 계약 값은 그대로다.**

★ 나누는 기준이 중요하다.
    사람이 읽는 것  : `reasoning` · 시나리오 판정 사유 · 준비되지 않음 설명
    기계가 읽는 것  : runtime_status · verdict · rule_id · policy key · missing_data
                     식별자 · Tool 이름 · source_ref · validation 필드 경로

  뒤쪽을 번역하면 프론트가 받던 구조 계약이 깨진다. 앞쪽만 한국어로 바꾼다.

★ 숫자 비소유도 그대로다. 설명은 여전히 **고정 문장을 고르는** 구조라, 한국어로
  바꿨다고 LLM 이 재무 숫자를 새로 쓸 자리가 생기지 않는다.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import adapter, messages
from app.finance.application.orchestration import FinanceAgentController
from app.finance.db import FinanceDataNotReady
from app.finance.llm.finalizer import _FINAL_EXPLANATIONS
from app.finance.llm.planner import ToolAction
from app.master.envelope import AgentRequest, ExecutionContext
from tests.finance.test_finance_adapter import _AdapterPlanner, _Context

AS_OF = date(2025, 12, 31)
_HANGUL = re.compile(r"[가-힣]")
_LATIN_SENTENCE = re.compile(r"[A-Za-z]{4,}")


def _req(mode: str = "PRE_PURCHASE", payload: dict | None = None) -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(
            request_id="REQ-KO",
            as_of=AS_OF,
            trigger="USER_REQUEST",
            policy_version="POLICY-V1",
        ),
        agent="finance",
        mode=mode,
        payload=payload or {},
    )


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "FinanceAgentController",
        lambda port: FinanceAgentController(port, _AdapterPlanner()),
    )
    monkeypatch.setattr("app.finance.execution.save_finance_execution", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_load_context", lambda: _Context())


def _assert_korean(text: str, label: str) -> None:
    assert _HANGUL.search(text), f"{label}: 한국어가 아니다 -> {text!r}"
    assert not _LATIN_SENTENCE.search(text), f"{label}: 영어 문장이 남아 있다 -> {text!r}"


# ---------------------------------------------------------------------------
# 확정 설명 — Finalizer 가 고르는 고정 문장
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(_FINAL_EXPLANATIONS))
def test_final_explanations_are_korean(key):
    _assert_korean(_FINAL_EXPLANATIONS[key], key)


def test_explanation_keys_stay_machine_contract():
    """★ 키는 번역하지 않는다 — Finalizer 가 이 이름으로 고른다."""
    assert set(_FINAL_EXPLANATIONS) == {
        "PRE_BOUNDARY",
        "SCENARIO_ACCEPT",
        "SCENARIO_CONDITIONAL",
        "SCENARIO_REJECT",
    }


def test_explanations_contain_no_numbers():
    """설명은 숫자를 만들지 않는다 — `_validate_ready_reasoning` 과 같은 규율이다."""
    for key, text in _FINAL_EXPLANATIONS.items():
        assert not re.search(r"\d", text), key


# ---------------------------------------------------------------------------
# 실제 실행에서 나가는 문장
# ---------------------------------------------------------------------------


def test_pre_purchase_reasoning_is_korean():
    reply, _meta = adapter.finance_port(_req())
    assert reply.runtime_status == "READY"
    _assert_korean(reply.reasoning, "PRE_PURCHASE reasoning")


def test_scenario_validation_reasoning_and_reason_are_korean(purchase_payload):
    reply, _meta = adapter.finance_port(_req("SCENARIO_VALIDATION", purchase_payload))
    assert reply.runtime_status == "READY"
    _assert_korean(reply.reasoning, "SCENARIO_VALIDATION reasoning")

    verdict = reply.payload["verdicts"][0]
    _assert_korean(verdict["reason"], "verdict.reason")
    # 판정·규칙 이름은 기계 계약이라 그대로다.
    assert verdict["verdict"] in {"ok", "conditional", "reject"}
    assert verdict["rule_id"] == "FIN-BASE-STRESS"


def test_invalid_scenario_input_message_is_korean_but_field_paths_are_not():
    reply, _meta = adapter.finance_port(_req("SCENARIO_VALIDATION", {"scenarios": []}))

    assert reply.runtime_status == "ERROR"
    _assert_korean(reply.reasoning, "invalid scenario reasoning")
    # 🔴 어느 필드가 틀렸는지는 **기계가 읽는 경로**다. 번역하면 못 찾는다.
    assert reply.payload["validation_errors"]
    assert all(_HANGUL.search(item) is None for item in reply.payload["validation_errors"])


def test_boundary_not_ready_message_is_korean(monkeypatch):
    monkeypatch.setattr(adapter, "_load_context", lambda: None)
    with patch("app.finance.adapter.save_finance_execution"):
        reply, _meta = adapter.finance_port(_req())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    _assert_korean(reply.reasoning, "not-ready reasoning")
    # missing_data 는 식별자다 — 번역 대상이 아니다.
    assert set(reply.missing_data) == {"finance_state", "finance_policy"}


def test_persistence_failure_message_is_korean():
    with patch(
        "app.finance.execution.save_finance_execution", side_effect=RuntimeError("db down")
    ):
        reply, _meta = adapter.finance_port(_req())

    assert reply.runtime_status == "ERROR"
    _assert_korean(reply.reasoning, "persistence failure reasoning")


def test_data_not_ready_message_is_korean_and_keeps_its_key():
    """준비되지 않음 사유는 Controller 경로에서 그대로 `reasoning` 이 된다.

    ★ 문장은 한국어지만 **키는 그대로 붙어 나간다** — `missing_data` 와 같은 식별자라
      번역하면 무엇이 없는지 기계가 못 찾는다.
    """
    error = FinanceDataNotReady("payroll_schedule")
    message = str(error)

    assert _HANGUL.search(message), message
    assert error.key == "payroll_schedule"
    assert "payroll_schedule" in message
    # 키를 뺀 나머지에는 영어 문장이 없다.
    assert not _LATIN_SENTENCE.search(message.replace("payroll_schedule", ""))


def test_runtime_not_ready_reasoning_from_controller_is_korean():
    """Controller 경로의 `RUNTIME_NOT_READY` 사유도 사용자에게 그대로 보인다."""

    def _load(_self, as_of, horizon):
        del as_of, horizon

    with patch.object(adapter._RuntimeContextDataPort, "load_payroll", _load):
        reply, _meta = adapter.finance_port(_req())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("payroll_schedule",)
    assert _HANGUL.search(reply.reasoning), reply.reasoning


# ---------------------------------------------------------------------------
# 기계 계약은 그대로여야 한다
# ---------------------------------------------------------------------------


def test_machine_contract_values_are_not_translated(purchase_payload):
    reply, metadata = adapter.finance_port(_req("SCENARIO_VALIDATION", purchase_payload))

    assert reply.runtime_status in {"READY", "RUNTIME_NOT_READY", "ERROR"}
    assert reply.business_status in {"ok", "conditional", "reject", "skipped"}
    assert metadata.llm_status in {"SUCCESS", "FALLBACK", "DISABLED", "SKIPPED_TEMPLATE"}
    for tool in metadata.used_tools:
        assert _HANGUL.search(tool) is None, tool
    for evidence in reply.evidences:
        assert _HANGUL.search(evidence.claim) is None, evidence.claim


def test_finance_generated_refs_are_english_only():
    """재무가 **스스로 만든** 참조에는 한국어가 없다.

    ★ 시나리오 분기 ref 는 예외다 — 거기 들어가는 시나리오 id 는 매입이 준 값이라
      (예: `기본`) 재무가 번역한 것이 아니다. 재무 소유 경로만 본다.
    """
    reply, _meta = adapter.finance_port(_req())
    for evidence in reply.evidences:
        for ref in evidence.ref_ids:
            assert _HANGUL.search(ref) is None, ref


def test_pre_purchase_payload_keys_stay_english():
    reply, _meta = adapter.finance_port(_req())
    for key in reply.payload:
        assert _HANGUL.search(key) is None, key
    assert "finance_cap_amount_krw" in reply.payload


def test_evidence_source_refs_are_untouched():
    """`source_ref` 는 따라가는 주소다 — 번역하면 아무 데도 닿지 않는다."""
    reply, _meta = adapter.finance_port(_req())
    by_claim = {item.claim: item for item in reply.evidences}
    assert by_claim["purchase_payment_days"].ref_ids == ("FINANCE-DECISION-20260827:N5",)
    assert by_claim["available_cash"].ref_ids == ("FIN-STATE-1",)


def test_dept_meta_field_names_stay_english():
    """Critic 이 읽는 이름이다 — 번역하면 검사가 대상을 못 찾는다."""
    import json

    _reply, metadata = adapter.finance_port(_req())
    dept = next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_dept_meta"
    )
    for name in dept["inputs_used"]["finance_cap_amount_krw"]:
        assert _HANGUL.search(name) is None, name
    for name in dept["produced_fields"]:
        assert _HANGUL.search(name) is None, name


def test_status_query_reasoning_is_korean():
    reply, _meta = adapter.finance_port(_req("STATUS_QUERY"))
    assert reply.runtime_status == "READY"
    _assert_korean(reply.reasoning, "STATUS_QUERY reasoning")


def test_finance_amounts_are_untouched_by_localization():
    """문장만 바꿨다 — **금액과 판정은 그대로다.**

    스텁 컨텍스트(`_Context`)의 결정론 상한이다. 한국어화가 계산에 손대면 여기가
    먼저 깨진다.
    """
    reply, _meta = adapter.finance_port(_req())
    assert reply.payload["finance_cap_amount_krw"] == Decimal(37_000_000)
    assert reply.business_status == "ok"


# ---------------------------------------------------------------------------
# 사용자 문장에는 구현 용어가 없다
# ---------------------------------------------------------------------------

#: 사용자 문장에 나오면 안 되는 말.
#:
#: ★ 읽는 사람은 Agent 를 만들지 않았다. 이 낱말이 보인다는 것은 **우리가 내부 사정을
#:   설명으로 내보냈다**는 뜻이고, 그러면 사용자는 무엇을 해야 하는지 알 수 없다.
_IMPLEMENTATION_TERMS = (
    "tool", "capability", "dependency", "planner", "registry", "harness",
    "observation", "replan", "langchain", "chatmodel", "runtime", "agent",
    "structuredtool", "finalizer", "adapter", "schema", "payload", "state",
    "ready", "reject", "conditional", "verdict", "rule", "trace",
)

#: 사용자 문장에 노출되면 안 되는 **기계 식별자**.
_MACHINE_IDENTIFIERS = (
    "assess_finance_position", "project_cashflow", "calculate_purchase_finance_cap",
    "analyze_payment_pressure", "evaluate_purchase_scenario", "validate_amount_adjustment",
    "finance_position", "cashflow_projection", "finance_cap", "payment_pressure",
    "scenario_evaluation", "amount_adjustment_validation",
    "finance_cap_amount_krw", "base_projected_cash_min", "purchase_payment_days",
    "RUNTIME_NOT_READY", "TOOL_NOT_EXECUTABLE", "DEPENDENCY_NOT_SATISFIED",
    "FIN-BASE-STRESS", "FIN-BASE-MIN-CASH",
)


def _assert_user_facing(text: str, label: str) -> None:
    """사람에게 나가는 문장이 지켜야 하는 것 전부를 한자리에서 본다."""
    _assert_korean(text, label)
    lowered = text.lower()
    for term in _IMPLEMENTATION_TERMS:
        assert term not in lowered, f"{label}: 구현 용어 {term!r} 가 보인다 -> {text!r}"
    for identifier in _MACHINE_IDENTIFIERS:
        assert identifier not in text, f"{label}: 기계 식별자 {identifier!r} -> {text!r}"


@pytest.mark.parametrize(
    "name",
    sorted(
        name
        for name, value in vars(messages).items()
        if name.isupper() and isinstance(value, str) and not name.startswith("_")
    ),
)
def test_every_user_facing_message_is_business_korean(name):
    """`messages` 는 **사용자 문장만** 담는다 — 하나라도 구현 용어가 섞이면 여기서 걸린다."""
    _assert_user_facing(getattr(messages, name), name)


@pytest.mark.parametrize("key", sorted(messages.FINANCE_EXPLANATIONS))
def test_final_explanations_are_business_korean(key):
    _assert_user_facing(messages.FINANCE_EXPLANATIONS[key], key)


def test_business_status_gets_its_own_user_sentence():
    """★ `ok` · `conditional` · `reject` 는 **사용자가 할 일이 다르다.**

    한 문장으로 묶으면 조정하면 되는 건과 아예 어려운 건이 같은 말로 나간다.
    """
    sentences = {
        status: messages.explanation_for("SCENARIO_VALIDATION", status)
        for status in ("ok", "conditional", "reject")
    }
    assert len(set(sentences.values())) == 3
    for status, text in sentences.items():
        _assert_user_facing(text, status)
    # 열거값 자체는 그대로다 — 번역 대상이 아니다.
    assert messages.explanation_keys("SCENARIO_VALIDATION", "reject") == ["SCENARIO_REJECT"]


def test_llm_and_deterministic_paths_say_the_same_thing():
    """🔴 LLM 경로만 다듬으면, 모델이 죽은 날에만 사용자에게 다른 말투가 나간다."""
    from app.finance.application.orchestration import fallback_reasoning
    from app.finance.llm.finalizer import DeterministicFinanceFinalizer

    finalizer = DeterministicFinanceFinalizer()
    for mode, status in (
        ("PRE_PURCHASE", "ok"),
        ("SCENARIO_VALIDATION", "ok"),
        ("SCENARIO_VALIDATION", "conditional"),
        ("SCENARIO_VALIDATION", "reject"),
    ):
        chosen = finalizer.finalize(mode=mode, business_status=status, evidences=())
        assert chosen == fallback_reasoning(mode, status)
        _assert_user_facing(chosen, f"{mode}/{status}")


def test_finalizer_prompt_requires_korean_business_language():
    """Finalizer 규율은 **무엇을 쓰지 말라**까지 적는다 — 고정 문장 구조 위의 2차 방어다."""
    from app.finance.llm.finalizer import _FINALIZER_SYSTEM_PROMPT

    lowered = _FINALIZER_SYSTEM_PROMPT.lower()
    for required in ("korean", "business", "never change the verdict", "langchain", "harness"):
        assert required in lowered, required
    assert "invent" in lowered or "calculate" in lowered


# ---------------------------------------------------------------------------
# 실제 실행 — 결과마다 사용자가 받는 문장
# ---------------------------------------------------------------------------


def _scenario(amount: int, max_price: int) -> dict:
    return {
        "proposal_id": "P-KO",
        "scenario_id": "S-KO",
        "total_amount_krw": amount,
        "total_qty_kg": 100,
        "max_price": max_price,
        "split_plan": [{"seq": 1, "date": "2025-01-01", "qty_kg": 100}],
        "meta": {"as_of": "2025-01-01"},
    }


def _run(mode: str, payload: dict | None = None, *, port=None, finalizer=None):
    from tests.finance.test_finance_harness_langchain import (
        Finalizer,
        Port,
        ScriptedPlanner,
    )
    from tests.finance.test_finance_harness_langchain import request as harness_request

    plan = (
        [
            ToolAction("assess_finance_position"),
            ToolAction("project_cashflow"),
            ToolAction("calculate_purchase_finance_cap"),
            ToolAction("analyze_payment_pressure"),
            ToolAction(finalize=True),
        ]
        if mode == "PRE_PURCHASE"
        else [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction("validate_amount_adjustment", {"axis": "amount"}),
            ToolAction(finalize=True),
        ]
    )
    with patch("app.finance.execution.save_finance_execution"):
        return FinanceAgentController(
            port or Port(), ScriptedPlanner(plan), finalizer or Finalizer()
        ).run(harness_request(mode, payload))


@pytest.mark.parametrize(
    ("amount", "max_price", "expected"),
    [(700, 8, "ok"), (800, 9, "conditional"), (1000, 10, "reject")],
)
def test_each_business_result_reaches_the_user_in_business_korean(amount, max_price, expected):
    """열거값은 그대로, **사용자 문장은 결과마다 다르게.**"""
    reply, _metadata = _run("SCENARIO_VALIDATION", _scenario(amount, max_price))

    assert reply.runtime_status == "READY"
    assert reply.business_status == expected  # 기계 계약은 번역하지 않는다
    _assert_user_facing(reply.reasoning, f"{expected} reasoning")
    _assert_user_facing(reply.payload["reason"], f"{expected} verdict.reason")


def test_ready_pre_purchase_explains_what_was_calculated():
    reply, _metadata = _run("PRE_PURCHASE")

    assert reply.runtime_status == "READY"
    _assert_user_facing(reply.reasoning, "PRE_PURCHASE reasoning")
    # 금액은 문장이 아니라 payload 와 Evidence 가 든다.
    assert not re.search(r"\d", reply.reasoning)
    assert reply.payload["finance_cap_amount_krw"]


def test_runtime_not_ready_tells_the_user_what_happens_next():
    """무엇이 없는지는 식별자로, 사용자에게는 **다음에 할 일**로."""
    from tests.finance.test_finance_harness_langchain import Port

    class _NoPayroll(Port):
        def load_payroll(self, as_of, horizon):
            del as_of, horizon

    reply, _metadata = _run("PRE_PURCHASE", port=_NoPayroll())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.missing_data == ("payroll_schedule",)  # 식별자는 그대로
    assert reply.reasoning == messages.NOT_READY
    _assert_user_facing(reply.reasoning, "not-ready reasoning")


def test_internal_failure_does_not_leak_the_technical_reason_to_the_user():
    """🔴 사용자에게 예외 문자열을 보여 주지 않는다 — 하지만 **잃지도 않는다.**"""
    import json

    from tests.finance.test_finance_harness_langchain import Port

    class _Broken(Port):
        def load_finance_position(self, as_of):
            del as_of
            raise AttributeError("this is a real programming bug, not a missing fact")

    reply, metadata = _run("PRE_PURCHASE", port=_Broken())

    assert reply.runtime_status == "ERROR"
    assert reply.reasoning == messages.INTERNAL_FAILURE
    _assert_user_facing(reply.reasoning, "internal failure reasoning")
    trace = next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_harness_trace"
    )
    assert "programming bug" in trace["failure_reason"]


def test_trace_stays_developer_oriented():
    """★ 개발자가 읽는 자리는 **번역하지 않는다.** 검사가 대상을 못 찾게 된다."""
    import json

    _reply, metadata = _run("PRE_PURCHASE")
    trace = next(
        json.loads(item)
        for item in metadata.observations
        if json.loads(item).get("observation_type") == "finance_harness_trace"
    )
    for field in (
        "executed_tools", "tool_calls", "llm_calls", "replans", "denials", "steps",
        "runtime_status", "llm_status", "rules_applied",
    ):
        assert field in trace, field
    step = trace["steps"][0]
    for field in (
        "requested_tool", "executable_tools", "selected_tool", "executed_tool",
        "completed_capabilities", "missing_capabilities", "denied_reason",
    ):
        assert field in step, field
    for name in trace["executed_tools"]:
        assert _HANGUL.search(name) is None, name


def test_explanation_cannot_change_the_verdict_or_add_numbers():
    """설명은 이미 확정된 결과를 **말로 옮길 뿐**이다."""

    class _LyingFinalizer:
        model = "lying-finalizer"

        def __init__(self):
            self.attempts = 0

        def finalize(self, *, mode, business_status, evidences):
            del mode, business_status, evidences
            self.attempts += 1
            # 판정을 뒤집으려 하고, 없던 숫자를 만들어 낸다.
            return "매입 가능 금액은 999999원이며 그대로 진행하셔도 됩니다."

    reply, metadata = _run(
        "SCENARIO_VALIDATION", _scenario(1000, 10), finalizer=_LyingFinalizer()
    )

    assert reply.business_status == "reject"  # 판정은 Rule 이 정한 그대로다
    assert reply.payload["verdict"] == "reject"
    assert "999999" not in reply.reasoning
    # 숫자를 실은 설명은 버려지고 결정론 문장이 나간다.
    assert reply.reasoning == messages.explanation_for("SCENARIO_VALIDATION", "reject")
    assert metadata.llm_status == "FALLBACK"
