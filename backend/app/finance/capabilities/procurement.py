"""PRE_PURCHASE capability — 매입 실행 **경계**를 낸다.

이 파일이 소유하는 것
    실행 컨텍스트 적재(상태·정책·급여·채무/채권·부채)
    · 위치 조사 · 현금흐름 투영 · Finance Cap · 지급 압박도

여기 **없는 것**
    금액 공식 자체(`tools`) · 판정(`rules`) · 실행 통제(`application.harness`)
    · 사람이 읽는 문장(`messages`)

★ 숫자는 전부 `app.finance.tools` 의 결정론 함수가 만들고, 근거는 `execution` 의
  Evidence 규율을 따른다. 여기서 하는 일은 **무엇을 어떤 순서로 읽어 그 함수에
  넘기는가** 다.

"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.contracts.core import Evidence
from app.finance.db import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.execution import (
    _PAYROLL_SOURCE_KEYS,
    _evidence,
    _optional_source_ref,
    _source_ref,
    _tool_ref,
)
from app.finance.schemas import CashEvent, FinancePolicy
from app.finance.state import FinanceAgentState
from app.finance.tools import (
    build_payroll_schedule,
    calculate_finance_cap,
    derive_cash_priority,
    derive_critical_payment_dates,
)
from app.finance.tools import (
    project_cashflow as project_cashflow_tool,
)

# ---------------------------------------------------------------------------
# 실행 컨텍스트 적재
# ---------------------------------------------------------------------------

def load_context(
    data_port: FinanceAsOfDataPort, state: FinanceAgentState
) -> tuple[dict[str, Any], FinancePolicy, list[CashEvent]]:
    """이 실행의 재무 컨텍스트. **한 번 읽고 분기 간에 공유한다.**

    ★ 부채 일정은 `current_debt > 0` 일 때만 읽는다 — 빚이 없는 회사에 부채 정책을
      요구하지 않는다 (repository 의 부채 규율과 같은 방향).
    """
    if state.context_cache is not None:
        return state.context_cache
    ctx = state.request.context
    position = data_port.load_finance_position(ctx.as_of)
    policy = data_port.load_policy(ctx.as_of, ctx.policy_version)
    horizon = ctx.as_of + timedelta(days=policy.cashflow_projection_days)
    payroll_amount = data_port.load_payroll(ctx.as_of, horizon)
    if payroll_amount is None:
        raise FinanceDataNotReady("payroll_schedule")
    policy = policy.model_copy(update={"monthly_labor_cost_krw": payroll_amount})
    # 급여 출처는 fail-closed 다. `build_payroll_schedule` 도 막지만 그쪽은
    # `ValueError` 라 일반 `ERROR` 로 분류된다 — **입력이 없어서 못 내는 답**은
    # `RUNTIME_NOT_READY` 여야 재시도 가치가 제대로 남는다 (M-1 §5.1).
    for key in _PAYROLL_SOURCE_KEYS:
        _source_ref(policy, key)
    events = [
        *data_port.load_obligations(ctx.as_of, horizon),
        *data_port.load_receivables(ctx.as_of, horizon),
        *build_payroll_schedule(as_of=ctx.as_of, horizon_end=horizon, policy=policy),
    ]
    current_debt = Decimal(position["current_debt_krw"])
    if current_debt > 0:
        events.extend(data_port.load_debt_schedule(ctx.as_of, horizon))
    state.context_cache = (position, policy, events)
    return state.context_cache


# ---------------------------------------------------------------------------
# PRE_PURCHASE capability
# ---------------------------------------------------------------------------

class FinancePreconditionMissing(RuntimeError):
    """선행 capability 없이 Tool 이 실행됐다. **재무 사실이 없는 것이 아니다.**

    🔴 예전에는 투영이 없으면 cap·압박도 Tool 이 안에서 `project_cashflow` 를 몰래
       돌렸다. 값은 맞았지만 **이력이 실행을 말하지 않았다** — 현금흐름을 만든 적이
       없는 실행으로 남았고, `inputs_used` 도 그 사실을 따로 보정해야 했다.

    ★ 이것은 실행 순서 오류이지 `RUNTIME_NOT_READY` 가 아니다. 재무 데이터·정책은
       멀쩡히 있다 — 다시 불러도 같은 결과가 나올 종류의 문제가 아니라, Harness 가
       막았어야 할 호출이 새어 들어온 것이다.
    """


def _require_projection(state: FinanceAgentState, tool: str) -> None:
    if state.projection is None:
        raise FinancePreconditionMissing(
            f"{tool} requires the cashflow_projection capability"
        )


def assess_finance_position(
data_port: FinanceAsOfDataPort, args: dict[str, Any], state: FinanceAgentState
) -> dict[str, Any]:
    del args
    position, policy, _ = load_context(data_port, state)
    if state.request.mode == "PRE_PURCHASE" and policy.purchase_payment_days is None:
        raise FinanceDataNotReady("purchase_payment_days")
    # ★ 값과 근거는 **한 쌍으로** 실린다. 정책 출처가 없으면 그 claim 은 payload
    #   에서도 빠진다 — 숫자만 남기면 봉투가 `E-EVIDENCE-MISSING` 을 내고, 근거를
    #   지어내면 따라갈 수 없는 ref 가 남는다. 빠진 사실은 missing_data 로 밝힌다.
    result: dict[str, Any] = {
        "available_cash": str(position["current_cash_krw"]),
        "payroll_payment_day": policy.payroll_date,
        # Finance Policy 버전은 Master 실행 컨텍스트와 독립적이다. 재현을 위해
        # Finance 데이터 경계에서 실제로 읽은 버전을 반환한다.
        "policy_version_used": policy.policy_version,
    }
    evidence: list[Evidence] = [
        _evidence(
            "available_cash",
            position["current_cash_krw"],
            "krw",
            str(position["finance_state_id"]),
            source="finance",
        ),
        Evidence(
            claim="payroll_payment_day",
            source="finance",
            ref_ids=(_source_ref(policy, "payroll_date"),),
            value=policy.payroll_date,
            unit="day_of_month",
            evidence_grade="SIM_FIXED",
            evidence_detail="Finance Policy DB day-of-month value.",
        ),
    ]

    minimum_cash_ref = _optional_source_ref(policy, "minimum_cash_balance_krw", state)
    if minimum_cash_ref is not None:
        result["minimum_cash_balance_krw"] = str(policy.minimum_cash_balance_krw)
        evidence.append(
            _evidence(
                "minimum_cash_balance_krw",
                policy.minimum_cash_balance_krw,
                "krw",
                minimum_cash_ref,
                source="persona",
            )
        )

    payment_days_ref = _optional_source_ref(policy, "purchase_payment_days", state)
    if payment_days_ref is not None:
        result["purchase_payment_days"] = policy.purchase_payment_days
        evidence.append(
            _evidence(
                "purchase_payment_days",
                policy.purchase_payment_days,
                "day",
                payment_days_ref,
                source="persona",
            )
        )
        # `policy_version_used` 는 봉투 어휘라 근거가 필수는 아니다. 다만 달 수
        # 있을 때는 단다 — 어느 정책 행을 읽었는지가 재현의 핵심이다.
        evidence.append(
            Evidence(
                claim="policy_version_used",
                source="persona",
                ref_ids=(payment_days_ref,),
                value=policy.policy_version,
                unit="version",
                evidence_grade="SIM_FIXED",
                evidence_detail="Version of the Finance policy used for this execution.",
            )
        )

    if policy.margin_defense_floor_rate is None:
        # 값이 없다는 것 자체가 답이다 — 근거를 요구받지 않는다(숫자가 아니다).
        result["margin_defense_floor_rate"] = None
    else:
        margin_ref = _optional_source_ref(policy, "margin_defense_floor_rate", state)
        if margin_ref is not None:
            result["margin_defense_floor_rate"] = str(policy.margin_defense_floor_rate)
            evidence.append(
                _evidence(
                    "margin_defense_floor_rate",
                    policy.margin_defense_floor_rate,
                    "ratio",
                    margin_ref,
                    source="persona",
                )
            )

    result["evidence"] = evidence
    return result

def project_cashflow(
data_port: FinanceAsOfDataPort, args: dict[str, Any], state: FinanceAgentState
) -> dict[str, Any]:
    del args
    position, policy, events = load_context(data_port, state)
    projection = project_cashflow_tool(
        as_of=state.request.context.as_of,
        current_cash_krw=Decimal(position["current_cash_krw"]),
        horizon_end=state.request.context.as_of
        + timedelta(days=policy.cashflow_projection_days),
        cash_events=events,
    )
    state.projection = projection
    return {
        "base_projected_cash_min": str(projection.projected_cash_min),
        "evidence": [
            _evidence(
                "base_projected_cash_min",
                projection.projected_cash_min,
                "krw",
                _tool_ref("project_cashflow", state),
            )
        ],
    }

def calculate_purchase_finance_cap(
data_port: FinanceAsOfDataPort, args: dict[str, Any], state: FinanceAgentState
) -> dict[str, Any]:
    del args
    _, policy, _ = load_context(data_port, state)
    if policy.purchase_payment_days is None:
        raise FinanceDataNotReady("purchase_payment_days")
    _require_projection(state, "calculate_purchase_finance_cap")
    cap = calculate_finance_cap(base_projection=state.projection, policy=policy)
    return {
        "finance_cap_amount_krw": str(cap),
        "base_projected_cash_min": str(state.projection.projected_cash_min),
        "evidence": [
            _evidence(
                "finance_cap_amount_krw",
                cap,
                "krw",
                _tool_ref("calculate_purchase_finance_cap", state),
            ),
            _evidence(
                "base_projected_cash_min",
                state.projection.projected_cash_min,
                "krw",
                _tool_ref("project_cashflow", state),
            ),
        ],
    }

def analyze_payment_pressure(
data_port: FinanceAsOfDataPort, args: dict[str, Any], state: FinanceAgentState
) -> dict[str, Any]:
    del args
    _, policy, events = load_context(data_port, state)
    _require_projection(state, "analyze_payment_pressure")
    pressure = derive_cash_priority(
        projected_cash_min=state.projection.projected_cash_min, policy=policy
    )
    dates = [
        item.isoformat()
        for item in derive_critical_payment_dates(
            current_cash_krw=Decimal(load_context(data_port, state)[0]["current_cash_krw"]),
            cash_events=events,
            minimum_cash_balance_krw=policy.minimum_cash_balance_krw,
        )
    ]
    ratio = state.projection.projected_cash_min / policy.minimum_cash_balance_krw
    # 압박 판정은 Tool 이 이미 만들었다. 여기서 정하는 것은 **근거를 달 수 있는가**
    # 뿐이고, 못 다는 claim 은 값도 싣지 않는다 (`_optional_source_ref` 참조).
    priority_refs = [
        _optional_source_ref(policy, key, state)
        for key in (
            "cash_priority_reference",
            "cash_priority_high_ratio",
            "cash_priority_medium_ratio",
        )
    ]
    minimum_cash_ref = _optional_source_ref(policy, "minimum_cash_balance_krw", state)

    result: dict[str, Any] = {
        "base_projected_cash_min": str(state.projection.projected_cash_min),
    }
    evidence: list[Evidence] = [
        _evidence(
            "base_projected_cash_min",
            state.projection.projected_cash_min,
            "krw",
            _tool_ref("project_cashflow", state),
        ),
    ]

    if all(ref is not None for ref in priority_refs):
        result["payment_pressure"] = pressure
        evidence.append(
            Evidence(
                claim="payment_pressure",
                source="tool_calc",
                ref_ids=(
                    _tool_ref("analyze_payment_pressure", state),
                    *(ref for ref in priority_refs if ref is not None),
                ),
                value=float(ratio),
                unit="ratio",
                evidence_grade="OFFICIAL",
                evidence_detail=(
                    "base_projected_cash_min / minimum_cash_balance_krw; "
                    "compared with cash_priority_high_ratio and "
                    "cash_priority_medium_ratio."
                ),
            )
        )
    if minimum_cash_ref is not None:
        result["critical_payment_dates"] = dates
        evidence.append(
            Evidence(
                claim="critical_payment_dates",
                source="tool_calc",
                ref_ids=(
                    _tool_ref("analyze_payment_pressure", state),
                    minimum_cash_ref,
                ),
                value=float(policy.minimum_cash_balance_krw),
                unit="KRW",
                evidence_grade="SIM_FIXED",
                evidence_detail=(
                    "Payment dates whose post-payment cash is below the "
                    "Finance minimum-cash threshold, plus the maximum daily outflow date."
                ),
            )
        )

    result["evidence"] = evidence
    return result
