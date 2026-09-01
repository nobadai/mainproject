"""실행 계약과 상한을 지키는 **application guard**.

이 파일이 소유하는 것
    재무 payload 계약 검증 · 시나리오 식별 · 중첩 산출 검증
    재계획 상한 · Planner 가 실은 인자의 원천 확인
    설명 문장 규율 (숫자 금지)

여기 **없는 것**
    최소현금 비교 · BASE/STRESS 판정 · Finance Cap · 현금 우선도
    → 그건 재무 **업무 Rule** 이고 `app.finance.rules` · `capabilities` 소유다.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from app.finance.llm.contracts import FinancePlannerFailure, ToolAction
from app.finance.repository import FinanceDataNotReady
from app.finance.state import FinanceAgentState
from app.master.envelope import AgentReply, AgentRequest


def _short_reason(reason: str) -> str:
    return " ".join(reason.split())[:160]


def _validate_ready_reasoning(reasoning: str) -> None:
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", reasoning.strip()) if part]
    if not reasoning.strip() or len(sentences) > 3:
        raise ValueError("Finance reasoning must contain one to three sentences")
    if re.search(r"\d", reasoning):
        raise ValueError("Finance reasoning must not introduce numeric claims")



def _validate_finance_payload(request: AgentRequest) -> None:
    if request.mode != "SCENARIO_VALIDATION":
        return
    raw_scenarios = request.payload.get("scenarios")
    scenarios = raw_scenarios if raw_scenarios is not None else [request.payload]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        raise ValueError("SCENARIO_VALIDATION requires one to three scenarios")
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise TypeError("each Finance scenario must be an object")
        scenario_id = _scenario_identity(scenario)
        if scenario_id in scenario_ids:
            raise ValueError("scenario_id must be unique within the request")
        scenario_ids.add(scenario_id)
        try:
            amount = Decimal(str(scenario["total_amount_krw"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("total_amount_krw must be a valid number") from exc
        if (
            isinstance(scenario.get("total_amount_krw"), bool)
            or not amount.is_finite()
            or amount <= 0
        ):
            raise ValueError("total_amount_krw must be a positive finite number")
        schedule = scenario.get("payment_schedule")
        if schedule is None:
            continue
        if not isinstance(schedule, list) or not schedule:
            raise ValueError("payment_schedule must be a non-empty list")
        total = Decimal(0)
        for payment in schedule:
            if not isinstance(payment, dict):
                raise TypeError("each payment_schedule entry must be an object")
            try:
                date.fromisoformat(str(payment["payment_date"]))
                payment_amount = Decimal(str(payment["amount_krw"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("payment_schedule date and amount must be valid") from exc
            if (
                isinstance(payment.get("amount_krw"), bool)
                or not payment_amount.is_finite()
                or payment_amount <= 0
            ):
                raise ValueError("payment_schedule amount must be positive and finite")
            total += payment_amount
        if total != amount:
            raise ValueError("payment_schedule amount sum must equal total_amount_krw")


def _scenario_identity(scenario: dict[str, Any]) -> str:
    """scenario_id가 없으면 Purchase가 보장하는 non-empty label을 identity로 사용한다."""
    if "scenario_id" in scenario:
        scenario_id = scenario["scenario_id"]
        if isinstance(scenario_id, str) and scenario_id.strip():
            return scenario_id.strip()
        raise ValueError("scenario_id must be a non-empty string when present")
    label = scenario.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    raise ValueError("label must be a non-empty string when scenario_id is absent")


def validate_finance_scenario_output(reply: AgentReply) -> tuple[str, ...]:
    """공통 Envelope를 넘어 Finance가 소유한 중첩 시나리오 계보를 검증한다."""
    if reply.runtime_status != "READY" or reply.mode != "SCENARIO_VALIDATION":
        return ()
    scenarios = reply.payload.get("verdicts")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        return ("payload.verdicts must contain one to three results",)
    # 유지하는 단일 시나리오 호환 형식은 branch Evidence를 공통 Envelope 수준에 둔다.
    # 문서화된 복수 시나리오 계약은 이를 중첩한다.
    if reply.payload.get("scenario_id") is not None and len(scenarios) == 1:
        return ()
    findings: list[str] = []
    seen: set[str] = set()
    nested_adjustment_refs: set[str] = set()
    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id") if isinstance(scenario, dict) else None
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in seen:
            findings.append("scenario result ids must be non-empty and unique")
            continue
        seen.add(scenario_id)
        if scenario.get("adjustability") not in {"NOT_NEEDED", "ADJUSTABLE", "NOT_ADJUSTABLE"}:
            findings.append(f"{scenario_id}: invalid adjustability")
        evidence = scenario.get("evidences")
        claims = {item.get("claim") for item in evidence} if isinstance(evidence, list) else set()
        required = {
            "finance_cap_amount_krw",
            "scenario_projected_cash_min",
            "payment_schedule",
            "verdict",
            "adjustability",
        }
        if not required <= claims:
            findings.append(f"{scenario_id}: nested Evidence is incomplete")
        for item in evidence if isinstance(evidence, list) else ():
            for ref in item.get("ref_ids", ()):
                if str(ref).startswith("FIN-AGENT:") and scenario_id not in str(ref):
                    findings.append(f"{scenario_id}: cross-branch Evidence ref")
        adjustments = scenario.get("suggested_adjustments", [])
        if scenario.get("adjustability") == "ADJUSTABLE" and not adjustments:
            findings.append(f"{scenario_id}: verified adjustment is missing")
        if scenario.get("adjustability") != "ADJUSTABLE" and adjustments:
            findings.append(f"{scenario_id}: unexpected adjustment")
        for adjustment in adjustments:
            refs = adjustment.get("ref_ids", ())
            if (
                adjustment.get("axis") != "amount"
                or not refs
                or not all(scenario_id in str(ref) for ref in refs)
            ):
                findings.append(f"{scenario_id}: adjustment lineage is invalid")
            nested_adjustment_refs.update(str(ref) for ref in refs)
        if adjustments and scenario.get("verdict") == "ok":
            findings.append(f"{scenario_id}: adjustment must not rewrite reject to ok")
    top_refs = {
        str(ref)
        for adjustment in reply.suggested_adjustments
        for ref in adjustment.ref_ids
    }
    if top_refs != nested_adjustment_refs:
        findings.append("top-level and nested Finance adjustments differ")
    return tuple(dict.fromkeys(findings))


def guard_replan(
    state: FinanceAgentState, total_replans: int, detail: dict[str, Any], *, max_replans: int
) -> int:
    if total_replans >= max_replans:
        # 되묻기에는 상한이 있다. 넘으면 최종 실패다 — 계약 위반을 무한히 숨기지
        # 않는다. `FinancePlannerFailure` 로 올려 이력에 FALLBACK 으로 남긴다.
        raise FinancePlannerFailure(
            "required Finance capability planning did not complete"
        )
    state.replans += 1
    state.observations.append(
        {"branch_id": state.branch_id, "type": "GUARD", **detail}
    )
    return total_replans + 1


def source_owned_arguments(action: ToolAction, state: FinanceAgentState) -> dict[str, Any]:
    if action.tool_name != "validate_amount_adjustment":
        return action.arguments
    if action.arguments.get("axis", "amount") != "amount":
        raise ValueError("Finance may adjust only the amount axis")
    source_amount = next(
        (
            state.request.payload[key]
            for key in ("candidate_amount_krw", "proposed_amount_krw")
            if state.request.payload.get(key) is not None
        ),
        state.scenario_cap,
    )
    if source_amount is None:
        raise FinanceDataNotReady("amount_adjustment_source")
    return {"axis": "amount", "candidate_amount_krw": source_amount}
