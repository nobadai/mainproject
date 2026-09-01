"""실행 관측 — Critic 이 재무를 검사할 수 있게 하는 사이드카.

★ **선언이 아니라 관측이다.** 실제로 성공한 Tool(`state.tool_order`)과 실제로 실린
  payload 키만 보고 만든다. 목록을 손으로 적어 두고 실행과 어긋나면, Critic 은 우리가
  적은 거짓말을 검사하게 된다.
"""

from __future__ import annotations

from typing import Any

from app.finance.state import FinanceAgentState

FINANCE_CAP_CHECK_ID = "finance_cap_amount_krw"

#: Finance Cap 을 만들 때 **실제로 읽는** 재무 입력.
#:
#: ★ 이것은 선언이 아니라 **관측이어야 한다.** 그래서 실행 중 실제로 부른 Tool 을 보고
#:   골라 담는다 (`_finance_dept_meta`). 목록을 손으로 적어 두고 실행과 어긋나면,
#:   Critic 의 등급 누출 검사는 **우리가 적은 거짓말을 검사하게 된다.**
#:
#: ★ 매입 소유 입력(`qty_kg` · `grade_unit_price` · `sourcing_plan` …)은 여기 없다.
#:   PRE_PURCHASE 는 payload 가 비어 있고 Tool 이 그 값을 읽지 않기 때문이다 — 읽게
#:   되는 날이 오면 **숨기지 말고 여기에 나타나야 한다.** 그것이 이 검사의 존재 이유다.
_CAP_TOOL_INPUTS: dict[str, tuple[str, ...]] = {
    "assess_finance_position": (
        "finance_state.current_cash_krw",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
    ),
    "project_cashflow": (
        "finance_state.current_cash_krw",
        "finance_policy.cashflow_projection_days",
        "finance_cash_events.obligations",
        "finance_cash_events.receivables",
        "finance_policy.monthly_labor_cost_krw",
        "finance_policy.payroll_date",
    ),
    "calculate_purchase_finance_cap": (
        "base_projection.projected_cash_by_date",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
    ),
    "analyze_payment_pressure": (
        "base_projection.projected_cash_min",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.cash_priority_high_ratio",
        "finance_policy.cash_priority_medium_ratio",
    ),
}


def _finance_dept_meta(
    mode: str, payload: dict[str, Any], states: list[FinanceAgentState]
) -> dict[str, Any] | None:
    """이번 실행의 사용 입력·산출 필드를 **재무 자신이** 기계가 읽을 형태로 낸다.

    Critic 의 `E-GRADE-LEAK`(재무 cap 에 등급·수량이 섞였나)와 `E-AUTHORITY`(부서가
    S3 전속 판정을 냈나)는 이 둘이 없으면 아예 돌지 않는다 — 통과가 아니라 **생략**이다.

    ★ **마스터가 추측하면 안 되는 것이라 재무가 낸다.** 마스터는 Tool 이름이나
      payload 키를 보고 *"재무가 무엇을 읽었는지"* 를 알 수 없다. 모르는 것을 빈
      dict 로 보내면 Critic 은 *"금지 입력이 없다"* 로 읽고 **통과시킨다** — 모르는
      것이 통과가 되는 구조라, 마스터는 아예 안 보내고 생략으로 남겨 왔다.

    ★ **관측이지 선언이 아니다.** `inputs_used` 는 실행에서 실제로 성공한 Tool
      (`state.tool_order`)만 보고 만든다. `produced_fields` 는 실제로 실린 payload
      키다. 둘 다 실행과 어긋날 수 없다.

    PRE_PURCHASE 만 낸다 — Critic 의 두 검사가 조언자 경계 회신을 대상으로 한다.
    """
    if mode != "PRE_PURCHASE" or not states:
        return None
    executed = [tool for state in states for tool in state.tool_order]
    inputs: list[str] = []
    for tool in executed:
        for name in _CAP_TOOL_INPUTS.get(tool, ()):
            if name not in inputs:
                inputs.append(name)
    return {
        "observation_type": "finance_dept_meta",
        "inputs_used": {FINANCE_CAP_CHECK_ID: inputs},
        # 값이 `None` 인 키는 뺀다 — 어댑터가 경계에서 실제로 빼는 것과 같은 기준이다
        # (`_controller_run` 의 `margin_defense_floor_rate`). 산출하지 않은 필드를
        # 산출했다고 적으면 권한 검사가 엉뚱한 것을 본다.
        "produced_fields": sorted(key for key, value in payload.items() if value is not None),
    }
