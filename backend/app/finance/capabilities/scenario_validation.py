"""SCENARIO_VALIDATION capability — 제출된 시나리오를 **검증**한다.
★ 재무는 매입 제안을 고쳐 쓰지 않는다. 제출된 사실을 읽고 BASE/STRESS 를 투영해
  판정할 뿐이고, 조정은 금액 축에서만 제안한다.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.finance.capabilities.payment_schedule import (
    _calculate_schedule_cap,
    _payment_row,
    _scenario_schedule,
    _schedule_events,
)
from app.finance.capabilities.runtime_context import load_context
from app.finance.evidence import _branch_ref, _evidence, _tool_ref
from app.finance.repository import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.rules import classify_base_stress
from app.finance.state import FinanceAgentState
from app.finance.tools import project_cashflow


def evaluate_purchase_scenario(
data_port: FinanceAsOfDataPort, args: dict[str, Any], state: FinanceAgentState
) -> dict[str, Any]:
    del args
    payload = state.request.payload
    amount = Decimal(str(payload["total_amount_krw"]))
    position, policy, events = load_context(data_port, state)
    horizon = state.request.context.as_of + timedelta(days=policy.cashflow_projection_days)
    schedule = _scenario_schedule(
        scenario=payload,
        as_of=state.request.context.as_of,
        horizon=horizon,
        default_payment_days=policy.purchase_payment_days,
    )
    base_projection = project_cashflow(
        as_of=state.request.context.as_of,
        current_cash_krw=Decimal(position["current_cash_krw"]),
        horizon_end=horizon,
        cash_events=events,
    )
    base_scenario_projection = project_cashflow(
        as_of=state.request.context.as_of,
        current_cash_krw=Decimal(position["current_cash_krw"]),
        horizon_end=horizon,
        cash_events=[
            *events,
            *_schedule_events(payload["scenario_id"], schedule, stress=False),
        ],
    )
    stress_scenario_projection = project_cashflow(
        as_of=state.request.context.as_of,
        current_cash_krw=Decimal(position["current_cash_krw"]),
        horizon_end=horizon,
        cash_events=[
            *events,
            *_schedule_events(payload["scenario_id"], schedule, stress=True),
        ],
    )
    cap = _calculate_schedule_cap(
        base_projection=base_projection,
        schedule=schedule,
        total_amount=amount,
        minimum_cash=policy.minimum_cash_balance_krw,
    )
    state.projection = base_projection
    state.scenario_projection = base_scenario_projection
    state.scenario_schedule = schedule
    state.base_state_violated = (
        base_projection.projected_cash_min < policy.minimum_cash_balance_krw
    )
    base_safe = base_scenario_projection.projected_cash_min >= policy.minimum_cash_balance_krw
    stress_safe = (
        stress_scenario_projection.projected_cash_min >= policy.minimum_cash_balance_krw
    )
    scenario_verdict = classify_base_stress(base_safe=base_safe, stress_safe=stress_safe)
    if state.base_state_violated:
        cap = Decimal(0)
        verdict = "reject"
        rule_id = "FIN-BASE-MIN-CASH"
        reason = "기본 상태에서 재무 최소현금 규칙을 충족하지 못했습니다."
    elif scenario_verdict == "ok":
        verdict, rule_id, reason = (
            "ok",
            "FIN-BASE-STRESS",
            "기본과 스트레스 시나리오를 모두 통과했습니다.",
        )
    elif scenario_verdict == "conditional":
        verdict, rule_id, reason = (
            "conditional",
            "FIN-BASE-STRESS",
            "기본은 통과했고 스트레스 시나리오에서 미달했습니다.",
        )
    else:
        verdict, rule_id, reason = (
            "reject",
            "FIN-BASE-STRESS",
            "기본 시나리오에서 미달했습니다.",
        )
    state.scenario_cap = cap
    scenario_ref = str(payload["scenario_id"])
    return {
        "scenario_id": payload["scenario_id"],
        "verdict": verdict,
        "adjustability": "NOT_NEEDED" if verdict == "ok" else "NOT_ADJUSTABLE",
        "finance_cap_amount_krw": str(cap),
        "scenario_projected_cash_min": str(base_scenario_projection.projected_cash_min),
        "stress_projected_cash_min": str(stress_scenario_projection.projected_cash_min),
        "critical_cash_date": base_scenario_projection.projected_cash_min_date.isoformat(),
        "rule_id": rule_id,
        # 🔴 한 배열 안에서 **행 모양이 갈리지 않는다.** 예전에는 분할 건과 재구성
        #    건이 서로 다른 키 집합을 냈다 — 읽는 쪽이 매번 어느 모양인지 확인해야
        #    했고, 없는 키를 조용히 놓치기 쉬웠다.
        #
        # ★ 이것은 **재무 검증 메타데이터**다. 고쳐 쓴 매입 제안이 아니다.
        "payment_schedule": [_payment_row(item) for item in schedule],
        "reason": reason,
        "rules": [{"rule_id": rule_id, "status": "PASS" if verdict == "ok" else "FAIL"}],
        "evidence": [
            # ⚠️ `1` 은 **시나리오 id 가 아니다.** 실제 식별자는 `ref_ids` 에 있다.
            #
            #    공용 `Evidence.value` 가 `float` 필수라, 문자열 식별자를 실을 자리가
            #    없어서 "이 시나리오가 있다"는 존재 표시로 1 을 넣는다. 값을 시나리오
            #    번호로 읽으면 안 된다.
            #
            #    제대로 고치려면 공용 계약(`orchestrator.contracts_core.Evidence`)이
            #    비수치 식별을 허용해야 한다 — 재무 밖이라 이번 범위에서 바꾸지 않는다
            #    (CROSS-DOMAIN). 현재 이 값을 읽는 소비자는 없다(Critic 의 재무 claim
            #    목록에도 없다).
            _evidence("scenario_id", 1, "identity", scenario_ref),
            _evidence(
                "finance_cap_amount_krw",
                cap,
                "krw",
                _tool_ref("evaluate_purchase_scenario", state),
            ),
            _evidence(
                "scenario_projected_cash_min",
                base_scenario_projection.projected_cash_min,
                "krw",
                _branch_ref("cashflow", state),
            ),
            _evidence(
                "stress_projected_cash_min",
                stress_scenario_projection.projected_cash_min,
                "krw",
                _branch_ref("stress-cashflow", state),
            ),
            _evidence("verdict", verdict == "ok", "boolean", _branch_ref(rule_id, state)),
            _evidence(
                "payment_schedule",
                len(schedule),
                "payment_count",
                _branch_ref("payment-schedule", state),
            ),
            _evidence(
                "adjustability",
                0 if verdict == "ok" else 2,
                "enum_code",
                _branch_ref(rule_id, state),
            ),
        ],
    }
def validate_amount_adjustment(
data_port: FinanceAsOfDataPort, args: dict[str, Any], state: FinanceAgentState
) -> dict[str, Any]:
    axis = args.get("axis", "amount")
    if axis != "amount":
        raise ValueError("Finance may adjust only the amount axis")
    candidate = Decimal(str(args["candidate_amount_krw"]))
    if candidate < 0:
        raise ValueError("candidate amount must not be negative")
    load_context(data_port, state)
    cap = state.scenario_cap
    if cap is None:
        raise FinanceDataNotReady("scenario_finance_cap")
    source_values = {
        Decimal(str(state.request.payload[key]))
        for key in ("candidate_amount_krw", "proposed_amount_krw")
        if state.request.payload.get(key) is not None
    }
    source_values.add(cap)
    if candidate not in source_values:
        raise ValueError("candidate amount has no DB, policy, payload, or Tool evidence source")
    valid = candidate <= cap
    return {
        "candidate_amount_krw": str(candidate),
        "validation_status": "PASS" if valid else "FAIL",
        "evidence": [
            _evidence(
                "candidate_amount_krw",
                candidate,
                "krw",
                _tool_ref("validate_amount_adjustment", state),
            ),
            _evidence(
                "validation_status",
                valid,
                "boolean",
                _branch_ref("FIN-CAP", state),
            ),
        ],
    }
