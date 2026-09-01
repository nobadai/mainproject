"""재무 실행 컨텍스트 적재.

이 파일이 소유하는 것
    재무 상태 · 정책 · 급여 · 채무/채권 · 부채 일정 · 컨텍스트 캐시
    `FinanceDataNotReady` 전파

여기 **없는 것**
    지급 일정 재구성 (`payment_schedule`)
    금액 계산 (`app.finance.tools`)
    판정 (`app.finance.rules`)

★ 한 실행에서 **한 번만 읽고** 분기 간에 공유한다. 분기마다 다시 읽으면 같은 요청 안에서
  서로 다른 상태를 볼 수 있다.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.finance.evidence import _PAYROLL_SOURCE_KEYS, _source_ref
from app.finance.repository import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.schemas import CashEvent, FinancePolicy
from app.finance.state import FinanceAgentState
from app.finance.tools import build_payroll_schedule


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
