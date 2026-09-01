"""실행 관측 — Critic 이 재무를 검사할 수 있게 하는 사이드카.

★ **선언이 아니라 관측이다.** 실제로 성공한 Tool(`state.tool_order`)과 실제로 실린
  payload 키만 보고 만든다. 목록을 손으로 적어 두고 실행과 어긋나면, Critic 은 우리가
  적은 거짓말을 검사하게 된다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.finance.state import FinanceAgentState

FINANCE_CAP_CHECK_ID = "finance_cap_amount_krw"

#: `_context()` 가 **항상** 읽는 것. PRE_PURCHASE Tool 은 전부 이것을 거친다.
#:
#: 여기에 현금이벤트가 들어가는 이유: `_context` 는 어느 Tool 이 불렀든 채무·채권·급여를
#: 함께 읽어 투영 입력을 만든다. 한 Tool 만 그것을 "쓴다"고 적으면, 그 Tool 이
#: `tool_order` 에 없던 실행에서 **같은 읽기가 사라진 것처럼** 보인다.
_CONTEXT_INPUTS: tuple[str, ...] = (
    "finance_state.current_cash_krw",
    "finance_state.current_debt_krw",
    "finance_policy.cashflow_projection_days",
    "finance_policy.monthly_labor_cost_krw",
    "finance_policy.payroll_date",
    "finance_cash_events.obligations",
    "finance_cash_events.receivables",
)

#: 부채가 있을 때만 읽는다 (`_context` 의 `current_debt > 0` 분기).
_DEBT_CONTEXT_INPUT = "finance_cash_events.debt_service"

#: Tool 이 `_context` **위에서 추가로** 읽는 입력.
#:
#: ★ 이것은 **재무가 소유한 정적 의존 계약**이다 — 실행에서 관측한 것이 아니다.
#:   관측되는 것은 "어느 Tool 이 실제로 돌았는가"(`state.tool_order`)뿐이고, 그 Tool 이
#:   무엇을 읽는지는 여기에 적힌 대로 해석된다. 둘을 섞어 말하면 안 된다.
#:
#: ★ 그래서 **드리프트가 위험하다.** 코드가 새 입력을 읽기 시작했는데 여기를 안 고치면,
#:   Critic 의 등급 누출 검사는 *우리가 적은 것*을 검사하게 된다 — 실제로 읽은 것이
#:   아니라. 매입 소유 입력(`qty_kg` · `grade_unit_price` · `sourcing_plan` …)이 재무
#:   cap 계산에 들어오는 날이 오면 **숨기지 말고 여기에 나타나야 한다.**
_CAP_TOOL_INPUTS: dict[str, tuple[str, ...]] = {
    "assess_finance_position": (
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
        "finance_policy.margin_defense_floor_rate",
    ),
    "project_cashflow": (),
    "calculate_purchase_finance_cap": (
        "base_projection.projected_cash_by_date",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
    ),
    "analyze_payment_pressure": (
        "base_projection.projected_cash_min",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.cash_priority_reference",
        "finance_policy.cash_priority_high_ratio",
        "finance_policy.cash_priority_medium_ratio",
    ),
}

#: Tool 이 **내부에서 부르는** Tool.
#:
#: 🔴 이걸 빠뜨리면 metadata 가 실제 의존을 **적게** 말한다. `calculate_purchase_finance_cap`
#:    은 투영이 없으면 `project_cashflow` 를 직접 부르는데, 결정론 Planner 처럼
#:    `project_cashflow` 를 따로 고르지 않은 실행에서는 그 Tool 이 `tool_order` 에
#:    없다 — 그러면 cap 을 만든 현금흐름 입력이 통째로 보고에서 빠진다.
_TOOL_INTERNAL_CALLS: dict[str, tuple[str, ...]] = {
    "calculate_purchase_finance_cap": ("project_cashflow",),
    "analyze_payment_pressure": ("project_cashflow",),
}


class FinanceToolDependencyMissing(RuntimeError):
    """실행한 Tool 의 의존 계약이 없다. **조용히 0개로 보고하지 않는다.**

    비어 있는 `inputs_used` 는 Critic 이 *"금지 입력이 없다"* 로 읽고 통과시킨다 —
    모르는 것이 통과가 되는 구조라, 여기서는 크게 실패하는 편이 낫다.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"Finance tool has no declared dependency contract: {tool}")


def _resolve_tool_inputs(tool: str, *, has_debt: bool) -> list[str]:
    """Tool 하나가 읽는 입력의 **전이 폐포**.

    `_context` 공통 입력 + Tool 고유 입력 + 내부에서 부르는 Tool 의 입력.
    """
    if tool not in _CAP_TOOL_INPUTS:
        raise FinanceToolDependencyMissing(tool)
    names: list[str] = [*_CONTEXT_INPUTS]
    if has_debt:
        names.append(_DEBT_CONTEXT_INPUT)
    pending = [tool]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current not in _CAP_TOOL_INPUTS:
            raise FinanceToolDependencyMissing(current)
        names.extend(_CAP_TOOL_INPUTS[current])
        pending.extend(_TOOL_INTERNAL_CALLS.get(current, ()))
    return names


def _observed_has_debt(states: list[FinanceAgentState]) -> bool:
    """이번 실행에서 부채 일정을 실제로 읽었는가.

    `_context` 가 `current_debt > 0` 일 때만 읽으므로, 고정 선언이 아니라 그날의
    상태에서 판단한다 — 읽지 않은 것을 읽었다고 적지 않기 위해서다.
    """
    for state in states:
        cache = state.context_cache
        if cache is None:
            continue
        position = cache[0]
        debt = position.get("current_debt_krw")
        if debt is not None and Decimal(str(debt)) > 0:
            return True
    return False


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
    if not states:
        return None
    if mode == "SCENARIO_VALIDATION":
        # 시나리오 판정에는 재무 cap 검사(`E-GRADE-LEAK`)에 해당하는 축이 없다.
        # 없는 검사에 가짜 `inputs_used` 를 지어내지 않고, 권한 검사(`E-AUTHORITY`)가
        # 볼 수 있게 **실제 산출 필드만** 낸다.
        return {
            "observation_type": "finance_dept_meta",
            "inputs_used": {},
            "produced_fields": _produced_fields(payload),
        }
    if mode != "PRE_PURCHASE":
        return None
    executed = [tool for state in states for tool in state.tool_order]
    has_debt = _observed_has_debt(states)
    inputs: list[str] = []
    for tool in executed:
        for name in _resolve_tool_inputs(tool, has_debt=has_debt):
            if name not in inputs:
                inputs.append(name)
    return {
        "observation_type": "finance_dept_meta",
        "inputs_used": {FINANCE_CAP_CHECK_ID: inputs},
        "produced_fields": _produced_fields(payload),
    }


def _produced_fields(payload: dict[str, Any]) -> list[str]:
    """이번 회신에 **실제로 실린** 필드.

    값이 `None` 인 키는 뺀다 — 어댑터가 경계에서 실제로 빼는 것과 같은 기준이다
    (`_controller_run` 의 `margin_defense_floor_rate`). 산출하지 않은 필드를
    산출했다고 적으면 권한 검사가 엉뚱한 것을 본다.
    """
    return sorted(key for key, value in payload.items() if value is not None)


def _assert_dependency_contract_is_complete() -> None:
    """PRE_PURCHASE Tool 은 **전부** 의존 계약을 가져야 한다.

    기동 시점에 확인한다 — Tool 을 새로 만들고 계약을 안 적으면, 그 사실이 조용한
    `inputs_used` 누락이 아니라 **import 실패**로 즉시 드러난다.
    """
    from app.finance.tool_registry import PRE_PURCHASE_TOOLS

    undeclared = sorted(PRE_PURCHASE_TOOLS - set(_CAP_TOOL_INPUTS))
    if undeclared:
        raise FinanceToolDependencyMissing(", ".join(undeclared))
    unknown_targets = sorted(
        {target for targets in _TOOL_INTERNAL_CALLS.values() for target in targets}
        - set(_CAP_TOOL_INPUTS)
    )
    if unknown_targets:
        raise FinanceToolDependencyMissing(", ".join(unknown_targets))


_assert_dependency_contract_is_complete()
