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

from app.finance import adapter
from app.finance.agent import FinanceAgentController
from app.finance.llm.finalizer import _FINAL_EXPLANATIONS
from app.finance.repository import FinanceDataNotReady
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
    monkeypatch.setattr("app.finance.run_repository.save_finance_execution", lambda **_kwargs: None)
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
    assert set(_FINAL_EXPLANATIONS) == {"PRE_BOUNDARY", "SCENARIO_REJECT", "SCENARIO_ACCEPT"}


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
        "app.finance.run_repository.save_finance_execution", side_effect=RuntimeError("db down")
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
