"""검증된 Tool 결과에서 **업무 결과**를 확정한다.

이 파일이 소유하는 것
    Tool 관측 → payload/Evidence 투영 · 시나리오 verdict 취합 · 조정 제안 조립
    Finalizer 실패 시 결정론 설명 선택

여기 **없는 것**
    금액·판정 생성. 여기 오는 값은 이미 `capabilities` 가 만든 것이고, 이 파일은
    **옮겨 담을 뿐**이다. 설명도 고정 문장을 고를 뿐 새 숫자를 쓰지 않는다.

★ `llm/finalizer.py` 와 이름이 비슷하지만 층이 다르다 — 그쪽은 Provider 호출이고
  여기는 업무 결과 확정이다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.finance.evidence import (
    _adjustment_from_dict,
    _branch_ref,
    _evidence,
    _evidence_dict,
    _evidence_from_dict,
    _indexed_verdict_evidence,
    _json_value,
    _tool_ref,
)
from app.finance.llm.finalizer import _FINAL_EXPLANATIONS
from app.finance.state import FinanceAgentState
from app.master.envelope import AgentRequest
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment

#: Tool 결과에서 **업무 회신(payload)으로 올리지 않는** 키.
#:
#: `evidence` · `rules` 는 봉투의 다른 자리로 간다. `critical_cash_date` 는 성격이
#: 다르다 — 설계서가 **Trace/Run History 항목**으로 못박은 값이다.
#: 빼도 추적성은 잃지 않는다: Tool 결과 전체가 observation 으로 남는다.
_NON_PAYLOAD_RESULT_KEYS = frozenset({"evidence", "rules", "critical_cash_date"})


def build_business_result(
    request: AgentRequest, states: list[FinanceAgentState], runtime_status: str
) -> tuple[dict[str, Any], list[Evidence], str, list[SuggestedAdjustment]]:
    if runtime_status != "READY":
        return {}, [], "skipped", []
    if request.mode == "SCENARIO_VALIDATION":
        results = [scenario_result(state) for state in states]
        verdicts = [result["verdict"] for result in results]
        status = (
            "reject"
            if "reject" in verdicts
            else "conditional"
            if "conditional" in verdicts
            else "ok"
        )
        indexed_evidence = _indexed_verdict_evidence(results)
        if "scenarios" in request.payload:
            adjustments = [
                _adjustment_from_dict(adjustment)
                for result in results
                for adjustment in result["suggested_adjustments"]
            ]
            return {"verdicts": results}, indexed_evidence, status, adjustments
        result = results[0]
        branch_evidence = [_evidence_from_dict(item) for item in result.pop("evidences")]
        branch_adjustments = result.pop("suggested_adjustments")
        adjustments = [_adjustment_from_dict(item) for item in branch_adjustments]
        return (
            {"verdicts": [dict(result)], **result},
            [*indexed_evidence, *branch_evidence],
            status,
            adjustments,
        )

    state = states[0]
    payload: dict[str, Any] = {}
    evidences: list[Evidence] = []
    for observation in state.observations:
        result = observation.get("result", {})
        for key, value in result.items():
            if key not in _NON_PAYLOAD_RESULT_KEYS:
                payload[key] = _json_value(value)
        evidences.extend(result.get("evidence", []))
    evidence_by_claim = {item.claim: item for item in evidences}
    return payload, list(evidence_by_claim.values()), "ok", []


def scenario_result(state: FinanceAgentState) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    evidence: list[Evidence] = []
    for observation in state.observations:
        result = observation.get("result", {})
        for key, value in result.items():
            if key not in _NON_PAYLOAD_RESULT_KEYS:
                payload[key] = _json_value(value)
        evidence.extend(result.get("evidence", []))
    validation = next(
        (
            item["result"]
            for item in reversed(state.observations)
            if item.get("tool") == "validate_amount_adjustment"
            and item["result"]["validation_status"] == "PASS"
        ),
        None,
    )
    adjustments: list[dict[str, Any]] = []
    if payload["verdict"] == "ok":
        payload["adjustability"] = "NOT_NEEDED"
    elif validation and Decimal(str(validation["candidate_amount_krw"])) > 0:
        payload["adjustability"] = "ADJUSTABLE"
        adjustments.append(
            {
                "dept": "finance",
                "axis": "amount",
                "target_value": float(validation["candidate_amount_krw"]),
                "unit": "krw",
                "reason": "Verified Finance amount alternative.",
                "ref_ids": [_tool_ref("validate_amount_adjustment", state)],
            }
        )
    else:
        payload["adjustability"] = "NOT_ADJUSTABLE"
    evidence = [item for item in evidence if item.claim != "adjustability"]
    adjustability_code = {
        "NOT_NEEDED": 0,
        "ADJUSTABLE": 1,
        "NOT_ADJUSTABLE": 2,
    }[payload["adjustability"]]
    evidence.append(
        _evidence(
            "adjustability",
            adjustability_code,
            "enum_code",
            _branch_ref("adjustability", state),
        )
    )
    payload["evidences"] = [_evidence_dict(item) for item in evidence]
    payload["suggested_adjustments"] = adjustments
    return payload


def fallback_reasoning(mode: str, business: str) -> str:
    if mode == "PRE_PURCHASE":
        return _FINAL_EXPLANATIONS["PRE_BOUNDARY"]
    if business == "reject":
        return _FINAL_EXPLANATIONS["SCENARIO_REJECT"]
    return _FINAL_EXPLANATIONS["SCENARIO_ACCEPT"]
