"""
adapters/finance.py — 재무 에이전트 접점 (마스터 ↔ 재무)

    AgentPort = (AgentRequest) -> (AgentReply, ExecutionMetadata)

★ **어댑터는 계산하지 않는다.**
  숫자는 전부 `app.finance.tools` 의 결정론 함수가 만든다. 여기가 하는 일은
  **번역**뿐이다 — 봉투를 풀어 도메인 함수를 부르고, 결과를 봉투에 담는다.
  어댑터가 값을 만들면 §1.2-3(LLM·중간 계층의 숫자 생성 금지)이 무너진다.

★ **`as_of` 는 마스터가 준 것을 쓴다** (§1.2-6).
  재무 Repository 가 "오늘"을 스스로 정하면 백테스트가 성립하지 않는다.

★ **실패를 예외로 올리지 않는다.**
  `MasterRunner._invoke` 가 잡아 `ERROR` 회신으로 바꾸지만, **입력이 없어서 못 내는 답**
  은 예외가 아니라 `RUNTIME_NOT_READY` 다. 둘은 재시도 가치가 다르다 (M-1 §5.1).

재무 확정분 근거 — 2026-08-27 재무 파트 v2.3 검토 회신 · M-1 §8.2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.finance.agent import FinanceAgentController, _calculate_schedule_cap, _scenario_schedule, _schedule_events
from app.finance.repository import FinanceDataNotReady, get_current_finance_runtime_context
from app.finance.rules import classify_base_stress
from app.finance.schemas import CashflowProjection, FinancePolicy, FinanceRuntimeContext
from app.finance.service import run_finance_procurement_with_context
from app.finance.tools import (
    build_payroll_schedule,
    calculate_finance_cap,
    calculate_projected_cash_min,
    derive_cash_priority,
    project_cashflow,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.orchestrator.contracts_core import Evidence
from app.purchase_agent.schemas import PurchaseProposal

# 재무 1차 Tool Set — T-FIN-01~06 (2026-08-27 확정). 그날 실제로 부른 것만 남긴다.
_T_POSITION = "assess_finance_position"
_T_CASHFLOW = "project_cashflow"
_T_CAP = "calculate_purchase_finance_cap"
_T_PRESSURE = "analyze_payment_pressure"

# 매입이 읽는 판정 필드. 대문자라 휴리스틱도 잡지만, 소문자로 바뀌어도 살아남게 선언한다.
_JUDGMENT_FIELDS = ("payment_pressure",)

_POLICY_KEYS_IN_USE: tuple[str, ...] = (
    "purchase_payment_days",
    "payroll_date",
    "monthly_labor_cost_krw",
    "minimum_cash_balance_krw",
    "cashflow_projection_days",
)
"""이 어댑터의 산출에 실제로 들어가는 정책값.

★ **전부 `source_refs` 에 있어야 한다** — 없으면 DB 가 아니라 Schema default 다.
  Repository 가 그 키를 조회하지 않으면 Pydantic 기본값이 대신 쓰이는데, 값은 멀쩡히
  나오고 에러도 안 난다. **DB 를 고쳐도 반영되지 않는다는 사실만 조용히 숨는다.**

  실제로 `payroll_date` 가 그 상태였다(2026-08-27 재무 후속회신 §3). DB(10)와
  default(10)가 우연히 같아 양쪽 다 눈치채지 못했다.

목록을 여기 두는 이유는 재무 Policy 에 필드가 늘어도 **우리가 쓰는 것만** 보기 위해서다."""

_PAYROLL_SOURCE_KEYS: tuple[str, ...] = ("monthly_labor_cost_krw", "payroll_date")
"""이 둘은 **출처가 없으면 계산 자체가 안 된다** (재무 #63 · M-23).

나머지 정책값은 출처가 없어도 값은 쓸 수 있어 `missing_data` 로 밝히고 지나가지만,
급여는 다르다 — 출처 없는 급여 이벤트를 만들지 않기로 재무가 정했으므로 **급여 유출이
통째로 빠진다.** 그 상태의 `finance_cap` 은 틀린 게 아니라 **낙관적으로 틀린다.**"""


def finance_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """마스터가 부르는 유일한 접점."""
    if request.mode == "PRE_PURCHASE":
        return _controller_pre_purchase(request)
    if request.mode == "STATUS_QUERY":
        return _status_query(request)
    if request.mode == "SCENARIO_VALIDATION":
        return _controller_scenario_validation(request)
    return _not_implemented(request)


class _RuntimeContextDataPort:
    """이미 고정된 Finance Runtime 컨텍스트를 Agent Tool에 제공한다.

    Master Policy 버전은 오케스트레이션 요청을 식별한다. Finance Policy는 별도
    버전을 가지므로, Controller는 이 경계에서 실제로 읽은 버전을 사용한다.
    """

    def __init__(self, context: FinanceRuntimeContext):
        self.context = context

    def _check_as_of(self, as_of: date) -> None:
        if self.context.snapshot.state_date != as_of:
            raise FinanceDataNotReady("historical_finance_position")

    def load_finance_position(self, as_of: date) -> dict[str, object]:
        self._check_as_of(as_of)
        return self.context.snapshot.model_dump()

    def load_policy(self, as_of: date, policy_version: str) -> FinancePolicy:
        self._check_as_of(as_of)
        # ``policy_version``은 Master 실행 컨텍스트의 값이며 Finance Policy 선택자가
        # 아니다. 이 Controller 실행에서 사용할 Finance Policy 버전은 고정된 Runtime
        # 컨텍스트가 정본이다.
        del policy_version
        policy = self.context.policy
        if isinstance(policy, FinancePolicy):
            return policy
        raw = {
                field: getattr(policy, field, None)
                for field in FinancePolicy.model_fields
                if field not in {"usage_scope"}
            }
        refs = dict(raw["source_refs"])
        for key in (
            "minimum_cash_balance_krw",
            "payroll_date",
            "purchase_payment_days",
            "cash_priority_reference",
            "cash_priority_high_ratio",
            "cash_priority_medium_ratio",
            "margin_defense_floor_rate",
        ):
            refs.setdefault(key, f"finance-policy:{policy.policy_version}:{key}")
        return FinancePolicy.model_validate(raw | {"usage_scope": "AGENT_MVP_DEMO", "source_refs": refs})

    def _events(self, direction: str) -> list:
        return [event for event in self.context.cash_events if event.direction == direction]

    def load_obligations(self, as_of: date, horizon: date) -> list:
        self._check_as_of(as_of)
        return self._events("OUTFLOW")

    def load_receivables(self, as_of: date, horizon: date) -> list:
        self._check_as_of(as_of)
        return self._events("INFLOW")

    def load_payroll(self, as_of: date, horizon: date) -> Decimal | None:
        self._check_as_of(as_of)
        return self.context.policy.monthly_labor_cost_krw

    def load_debt_schedule(self, as_of: date, horizon: date) -> list:
        self._check_as_of(as_of)
        return []


def _controller_request(request: AgentRequest, context: FinanceRuntimeContext) -> AgentRequest:
    """Master 컨텍스트를 다시 쓰지 않고 Data Port가 Finance 자체 Policy를 해석하게 한다."""
    del context
    return request


def _controller_pre_purchase(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    context, not_ready = _controller_boundary(request)
    if not_ready is not None:
        return not_ready
    assert context is not None
    return _controller_run(request, context)


def _controller_scenario_validation(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    run_id = _run_id(request)
    try:
        _purchase_proposal(request.payload)
    except ValidationError as exc:
        return _invalid_scenario_input(request, run_id, exc)
    proposal = _purchase_proposal(request.payload)
    if proposal.meta.as_of != request.context.as_of:
        return _invalid_scenario_as_of(request, run_id, proposal.meta.as_of)
    context, not_ready = _controller_boundary(request)
    if not_ready is not None:
        return not_ready
    assert context is not None
    return _controller_run(request, context)


def _controller_run(
    request: AgentRequest, context: FinanceRuntimeContext
) -> tuple[AgentReply, ExecutionMetadata]:
    """레거시 업무 계약 주석만 보강하고 Controller 메타데이터는 그대로 유지한다."""
    reply, metadata = FinanceAgentController(_RuntimeContextDataPort(context)).run(
        _controller_request(request, context)
    )
    if reply.runtime_status != "READY" or request.mode != "PRE_PURCHASE":
        return reply, metadata
    missing = list(reply.missing_data)
    if getattr(context.policy, "margin_defense_floor_rate", None) is None:
        missing.append("margin_defense_floor_rate")
    missing.extend(
        f"{key}@policy_source_ref"
        for key in _POLICY_KEYS_IN_USE
        if key not in context.policy.source_refs
    )
    payload = dict(reply.payload)
    if getattr(context.policy, "margin_defense_floor_rate", None) is None:
        payload.pop("margin_defense_floor_rate", None)
    return replace(
        reply,
        payload=payload,
        missing_data=tuple(dict.fromkeys(missing)),
        judgment_fields=_JUDGMENT_FIELDS,
    ), metadata


def _controller_boundary(
    request: AgentRequest,
) -> tuple[FinanceRuntimeContext | None, tuple[AgentReply, ExecutionMetadata] | None]:
    """Controller 위임 전에 Adapter 수준의 준비 상태 의미를 보존한다."""
    run_id = _run_id(request)
    context = _load_context()
    if context is None:
        return None, _not_ready(
            request, run_id, [_T_POSITION],
            missing=("finance_state", "finance_policy"),
            reason="재무 상태 또는 정책을 읽지 못했다",
        )
    if context.snapshot.state_date != request.context.as_of:
        return None, _not_ready(
            request, run_id, [_T_POSITION],
            missing=(f"finance_state@{request.context.as_of.isoformat()}",),
            reason=f"재무 상태 기준일이 {context.snapshot.state_date} 다 — 요청은 {request.context.as_of} 다",
        )
    payroll_refs = tuple(
        f"{key}@policy_source_ref" for key in _PAYROLL_SOURCE_KEYS if not context.policy.source_refs.get(key)
    )
    if payroll_refs:
        return None, _not_ready(
            request, run_id, [_T_POSITION],
            missing=payroll_refs,
            reason="급여 정책값의 출처가 없어 현금 투영을 만들지 못했다 (M-23)",
        )
    if context.policy.purchase_payment_days is None:
        return None, _not_ready(
            request, run_id, [_T_POSITION],
            missing=("purchase_payment_days",),
            reason="매입 지급일을 산출할 purchase_payment_days 정책값이 없다.",
        )
    return context, None


# ---------------------------------------------------------------------------
# STATUS_QUERY — "지금 자금 상황" 조회
# ---------------------------------------------------------------------------


def _status_query(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """묻기만 하는 요청. **경계가 아니라 상태**를 돌려준다.

    ★ `PRE_PURCHASE` 와 계산은 같고 **싣는 것이 다르다.** `finance_cap` ·
      `purchase_payment_days` 같은 값은 *매입 판단을 위한 경계*라 "지금 자금 상황"
      을 묻는 사람에게는 답이 아니다.

    ★ **급여 출처가 없어도 답을 낸다 — 다만 낼 수 있는 것만.**
      `PRE_PURCHASE` 는 급여 출처가 없으면 통째로 멈춘다. 급여 유출이 빠진 투영으로
      만든 `finance_cap` 은 **낙관적으로 틀리고**, 그 상한으로 매입이 실행되기 때문이다.
      조회는 실행으로 이어지지 않으므로 **현재 현금처럼 투영이 필요 없는 값은 답하고**,
      투영이 필요한 값만 빼고 이름을 밝힌다 (§3.7.6 — 못 한 것을 한 척하지 않는다).
    """
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_POSITION]

    context = _load_context()
    if context is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("finance_state", "finance_policy"),
            reason="재무 상태 또는 정책을 읽지 못했다",
        )

    state_date = context.snapshot.state_date
    if state_date != as_of:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=(f"finance_state@{as_of.isoformat()}",),
            reason=f"재무 상태 기준일이 {state_date} 다 — 요청은 {as_of} 다",
        )

    policy = context.policy
    ref = context.snapshot.finance_state_id
    payload: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "state_date": state_date.isoformat(),
        "available_cash": _num(context.snapshot.current_cash_krw),
        "minimum_cash_balance_krw": _num(policy.minimum_cash_balance_krw),
        "policy_version_used": policy.policy_version,
    }
    evidences = (
        _ev("available_cash", context.snapshot.current_cash_krw, "KRW", ref, "재무 상태 현재 잔액"),
        _ev(
            "minimum_cash_balance_krw",
            policy.minimum_cash_balance_krw,
            "KRW",
            # 🔴 값이 Policy 에서 왔으면 근거도 Policy 를 가리켜야 한다.
            #    스냅샷 id(FIN-DAY30-LOAN)를 달면 *"재무 상태 행에서 온 수"* 라고
            #    말하는 것이라 **거짓 출처**다. 나중에 따라가면 엉뚱한 곳에 닿는다.
            _policy_ref(policy, "minimum_cash_balance_krw", ref),
            f"Finance Policy {policy.policy_version} · 1개월 급여 Reserve",
            grade="SIM_FIXED",
        ),
    )

    missing: list[str] = []
    payroll_refs = tuple(
        f"{key}@policy_source_ref"
        for key in _PAYROLL_SOURCE_KEYS
        if not policy.source_refs.get(key)
    )
    if payroll_refs:
        # 투영이 필요한 값만 뺀다. 현재 잔액은 그대로 답한다.
        missing.extend(payroll_refs)
    else:
        tools.extend((_T_CASHFLOW, _T_PRESSURE))
        horizon_end = as_of + timedelta(days=policy.cashflow_projection_days)
        events = (
            *context.cash_events,
            *build_payroll_schedule(as_of=as_of, horizon_end=horizon_end, policy=policy),
        )
        projection = project_cashflow(
            as_of=as_of,
            current_cash_krw=context.snapshot.current_cash_krw,
            horizon_end=horizon_end,
            cash_events=events,
        )
        outflows = _outflow_by_date([e for e in events if as_of < e.event_date <= horizon_end])
        cash_min = calculate_projected_cash_min(projection)
        pressure = derive_cash_priority(projected_cash_min=cash_min, policy=policy)
        payload["projection_days"] = policy.cashflow_projection_days
        payload["projected_cash_min"] = _num(cash_min)
        payload["payment_pressure"] = pressure
        payload["critical_payment_dates"] = _critical_payment_dates(
            projection, outflows, policy.minimum_cash_balance_krw
        )
        evidences = (
            *evidences,
            _ev(
                "projected_cash_min",
                cash_min,
                "KRW",
                ref,
                f"D+{policy.cashflow_projection_days} 투영 최저",
            ),
            _ev(
                "payment_pressure",
                _ratio(cash_min, policy.minimum_cash_balance_krw),
                "ratio",
                ref,
                f"투영최저/최소현금 = 임계 {policy.cash_priority_high_ratio}"
                f"/{policy.cash_priority_medium_ratio} → {pressure}",
            ),
            _ev(
                "projection_days",
                policy.cashflow_projection_days,
                "day",
                _policy_ref(policy, "cashflow_projection_days", ref),
                "현금 투영 Horizon — Finance Policy 값",
                grade="SIM_FIXED",
            ),
            _ev(
                # ★ 목록의 **개수**가 아니라 그 목록을 만든 **임계값**을 넣는다 (`_ev` 규율).
                #   개수를 넣으면 "왜 그날이 위험일인가" 에 아무 답이 안 된다.
                "critical_payment_dates",
                policy.minimum_cash_balance_krw,
                "KRW",
                _policy_ref(policy, "minimum_cash_balance_krw", ref),
                f"이 임계 미만으로 떨어지는 지급일 {len(payload['critical_payment_dates'])} 건 "
                f"(D+{policy.cashflow_projection_days} 투영)",
                grade="SIM_FIXED",
            ),
        )

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=as_of,
        agent="finance",
        mode=request.mode,
        run_id=run_id,
        runtime_status="READY",
        business_status="ok",
        payload=payload,
        evidences=evidences,
        judgment_fields=_JUDGMENT_FIELDS if "payment_pressure" in payload else (),
        missing_data=tuple(missing),
        reasoning="현재 자금 상태를 조회했다."
        if not missing
        else "현재 잔액은 답했고, 급여 출처가 없어 현금 투영은 내지 못했다.",
    )
    return reply, _meta(request, run_id, tools)


# ---------------------------------------------------------------------------
# PRE_PURCHASE — 경계 제공 (M-1 §8.2 · 7필드)
# ---------------------------------------------------------------------------


def _pre_purchase(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_POSITION]

    context = _load_context()
    if context is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("finance_state", "finance_policy"),
            reason="재무 상태 또는 정책을 읽지 못했다",
        )

    # ★ as_of 대조 — 재무 상태의 기준일이 다르면 그날의 사실이 아니다 (§1.2-6)
    state_date = context.snapshot.state_date
    if state_date != as_of:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=(f"finance_state@{as_of.isoformat()}",),
            reason=f"재무 상태 기준일이 {state_date} 다 — 요청은 {as_of} 다",
        )

    policy = context.policy
    horizon_end = as_of + timedelta(days=policy.cashflow_projection_days)

    # 🔴 급여 출처가 없으면 **투영을 만들지 않는다** (2026-08-27 재무 #63).
    #
    #    재무가 `build_payroll_schedule` 을 fail-closed 로 바꿨다 — 출처 없는 급여
    #    이벤트를 만들지 않는다(M-23). 옳은 방향이라 여기서도 그 뜻을 따른다.
    #
    #    ★ 다만 **예외로 새게 두지 않는다.** 그대로 두면 `MasterRunner` 가 `ERROR`
    #      로 바꾸는데, 입력이 없어서 못 내는 답은 `RUNTIME_NOT_READY` 다 (M-1 §5.1).
    #      둘은 재시도 가치가 다르다.
    #
    #    ★ **READY 로 두고 이름만 밝히면 안 된다.** 급여 유출이 통째로 빠진 투영은
    #      `finance_cap` 을 낙관적으로 부풀린다 — 숫자는 나오고 에러도 안 난다.
    payroll_refs = tuple(
        f"{key}@policy_source_ref"
        for key in _PAYROLL_SOURCE_KEYS
        if not policy.source_refs.get(key)
    )
    if payroll_refs:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=payroll_refs,
            reason="급여 정책값의 출처가 없어 현금 투영을 만들지 못했다 (M-23)",
        )

    tools.append(_T_CASHFLOW)
    events = (
        *context.cash_events,
        *build_payroll_schedule(as_of=as_of, horizon_end=horizon_end, policy=policy),
    )
    projection = project_cashflow(
        as_of=as_of,
        current_cash_krw=context.snapshot.current_cash_krw,
        horizon_end=horizon_end,
        cash_events=events,
    )
    # 투영 구간 밖의 이벤트는 투영에 안 들어가므로 유출 집계에서도 뺀다
    outflows = _outflow_by_date([e for e in events if as_of < e.event_date <= horizon_end])

    tools.append(_T_CAP)
    cap = calculate_finance_cap(base_projection=projection, policy=policy)
    cash_min = calculate_projected_cash_min(projection)

    tools.append(_T_PRESSURE)
    pressure = derive_cash_priority(projected_cash_min=cash_min, policy=policy)

    ref = context.snapshot.finance_state_id
    payload: dict[str, Any] = {
        "available_cash": _num(context.snapshot.current_cash_krw),
        "finance_cap_amount_krw": _num(cap),
        "base_projected_cash_min": _num(cash_min),
        "purchase_payment_days": policy.purchase_payment_days,
        "payment_pressure": pressure,
        # 지급이 몰린 날 — 매입의 분할 회차 충돌 검사용 (2026-08-27 재무 정의)
        "critical_payment_dates": _critical_payment_dates(
            projection, outflows, policy.minimum_cash_balance_krw
        ),
        # ★ 재현 4종의 하나 (§3.2.4). 마스터가 준 policy_version 과 다를 수 있어 밝힌다 — M-20
        "policy_version_used": policy.policy_version,
    }

    # `margin_defense_floor_rate` 는 재무 Policy 소유다 (M-19 해소).
    #
    # ★ 어댑터가 계산하지 않는다 — `break_even_cm + 0.02` 를 여기서 만들면 두 곳에서
    #   같은 값을 계산하게 되고, N9 후 재산정 때 한쪽만 바뀐다 (2026-08-27 재무 확인).
    #   읽어서 싣기만 한다. 없으면 0 으로 채우지 않고 이름을 밝힌다 (§1.2-10).
    floor_rate = policy.margin_defense_floor_rate
    missing: list[str] = []
    if floor_rate is None:
        missing.append("margin_defense_floor_rate")
    else:
        payload["margin_defense_floor_rate"] = _num(floor_rate)

    # 🔴 정책값이 DB 에서 온 것인지 확인한다 (2026-08-27 재무 후속회신 §3).
    #    값이 아니라 **출처**의 문제이므로 READY 는 유지하고 이름만 밝힌다.
    missing.extend(
        f"{key}@policy_source_ref" for key in _POLICY_KEYS_IN_USE if key not in policy.source_refs
    )

    evidences = (
        _ev("available_cash", context.snapshot.current_cash_krw, "KRW", ref),
        _ev(
            "base_projected_cash_min",
            cash_min,
            "KRW",
            ref,
            f"D+{policy.cashflow_projection_days} 투영 최저 (승인 전 기준)",
        ),
        _ev(
            "finance_cap_amount_krw",
            cap,
            "KRW",
            ref,
            f"지급일 D+{policy.purchase_payment_days} 이후 최저잔액 − 최소현금 (최종 상한)",
        ),
        _ev(
            "payment_pressure",
            _ratio(cash_min, policy.minimum_cash_balance_krw),
            "ratio",
            ref,
            f"투영최저/최소현금 = 임계 {policy.cash_priority_high_ratio}"
            f"/{policy.cash_priority_medium_ratio} → {pressure}",
        ),
        _ev(
            "purchase_payment_days",
            policy.purchase_payment_days,
            "days",
            ref,
            f"Finance Policy {policy.policy_version} · N5 확정값 (calendar day). "
            "2026-08-27 재무 파트 확정",
            grade="SIM_FIXED",  # 외부 통계값이 아니라 가상회사 운영 Policy 다
        ),
        _ev(
            "critical_payment_dates",
            policy.minimum_cash_balance_krw,
            "KRW",
            ref,
            "지급일 중 (지급 후 잔액 < 최소현금) ∪ (일일 유출 최대). 판정 임계 = 최소현금 보유선",
        ),
    )
    if floor_rate is not None:
        evidences = (
            *evidences,
            _ev(
                "margin_defense_floor_rate",
                floor_rate,
                "ratio",
                policy.source_refs.get("margin_defense_floor_rate", ref),
                "재무 Policy 값. 거치기 손익분기 CM + 2%p · N9 후 재산정 대상",
                grade="SIM_FIXED",
            ),
        )

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=as_of,
        agent="finance",
        mode=request.mode,
        run_id=run_id,
        runtime_status="READY",
        business_status="ok",
        payload=payload,
        evidences=evidences,
        judgment_fields=_JUDGMENT_FIELDS,
        missing_data=tuple(missing),
        reasoning="재무 경계를 산출했다.",
    )
    return reply, _meta(request, run_id, tools)


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — Purchase proposal을 Finance 판정 입력으로 번역
# ---------------------------------------------------------------------------


def _scenario_validation(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """현재 Purchase 출력을 재무값 재생성 없이 검증한다.

    Purchase Schema를 정본으로 유지한다. Adapter는 직렬화된 계약만 검증하고,
    기본 컨텍스트/Rule에는 기존 Finance 매입 엔진을 재사용한 뒤 이미 권위가 있는
    지급 일정을 Overlay한다.
    """
    run_id = _run_id(request)
    try:
        proposal = _purchase_proposal(request.payload)
    except ValidationError as exc:
        return _invalid_scenario_input(request, run_id, exc)

    if proposal.meta.as_of != request.context.as_of:
        return _invalid_scenario_as_of(request, run_id, proposal.meta.as_of)

    context = _load_context()
    if context is None:
        return _not_ready(
            request,
            run_id,
            [_T_POSITION],
            missing=("finance_state", "finance_policy"),
            reason="재무 상태 또는 정책을 읽지 못했다",
        )
    if context.snapshot.state_date != request.context.as_of:
        return _not_ready(
            request,
            run_id,
            [_T_POSITION],
            missing=(f"finance_state@{request.context.as_of.isoformat()}",),
            reason=(
                f"재무 상태 기준일이 {context.snapshot.state_date} 다 — "
                f"요청은 {request.context.as_of} 다"
            ),
        )

    payroll_refs = tuple(
        f"{key}@policy_source_ref"
        for key in _PAYROLL_SOURCE_KEYS
        if not context.policy.source_refs.get(key)
    )
    if payroll_refs:
        return _not_ready(
            request,
            run_id,
            [_T_POSITION],
            missing=payroll_refs,
            reason="급여 정책값의 출처가 없어 현금 투영을 만들지 못했다 (M-23)",
        )
    if context.policy.purchase_payment_days is None:
        return _not_ready(
            request,
            run_id,
            [_T_POSITION, _T_CASHFLOW, _T_CAP],
            missing=("purchase_payment_days",),
            reason="매입 지급일을 산출할 purchase_payment_days 정책값이 없다.",
        )

    # 제안과 독립적인 Finance 상태 및 보고 금액 비교에는 기존 시나리오 엔진과
    # Runtime Rule을 호출한다.
    base_result = run_finance_procurement_with_context(proposal, context)
    if base_result.runtime_status != "READY":
        return _not_ready(
            request,
            run_id,
            [_T_POSITION, _T_CASHFLOW, _T_CAP],
            missing=tuple(base_result.hard_constraints) or ("finance_runtime",),
            reason="재무 실행 입력이 준비되지 않아 시나리오를 판정하지 못했다.",
        )

    policy = context.policy
    as_of = request.context.as_of
    horizon = as_of + timedelta(days=policy.cashflow_projection_days)
    base_events = (
        *context.cash_events,
        *build_payroll_schedule(as_of=as_of, horizon_end=horizon, policy=policy),
    )
    base_projection = project_cashflow(
        as_of=as_of,
        current_cash_krw=context.snapshot.current_cash_krw,
        horizon_end=horizon,
        cash_events=base_events,
    )
    base_violated = base_projection.projected_cash_min < policy.minimum_cash_balance_krw

    verdicts: list[dict[str, Any]] = []
    evidences: list[Evidence] = []
    for index, scenario in enumerate(proposal.scenarios):
        raw = scenario.model_dump(mode="json")
        raw["scenario_id"] = scenario.label
        schedule = _scenario_schedule(
            scenario=raw,
            as_of=as_of,
            horizon=horizon,
            default_payment_days=policy.purchase_payment_days,
        )
        base_with_scenario = project_cashflow(
            as_of=as_of,
            current_cash_krw=context.snapshot.current_cash_krw,
            horizon_end=horizon,
            cash_events=[*base_events, *_schedule_events(scenario.label, schedule, stress=False)],
        )
        stress_with_scenario = project_cashflow(
            as_of=as_of,
            current_cash_krw=context.snapshot.current_cash_krw,
            horizon_end=horizon,
            cash_events=[*base_events, *_schedule_events(scenario.label, schedule, stress=True)],
        )
        cap = _calculate_schedule_cap(
            base_projection=base_projection,
            schedule=schedule,
            total_amount=Decimal(scenario.total_amount_krw),
            minimum_cash=policy.minimum_cash_balance_krw,
        )
        scenario_status = classify_base_stress(
            base_safe=base_with_scenario.projected_cash_min >= policy.minimum_cash_balance_krw,
            stress_safe=stress_with_scenario.projected_cash_min >= policy.minimum_cash_balance_krw,
        )
        if base_violated or base_result.verdict == "FAIL":
            verdict, rule_id, reason = (
                "reject",
                "FIN-BASE-MIN-CASH",
                "Base Finance minimum-cash rule failed.",
            )
        elif scenario_status == "ok":
            verdict, rule_id, reason = "ok", "FIN-BASE-STRESS", "BASE and STRESS passed."
        elif scenario_status == "conditional":
            verdict, rule_id, reason = (
                "conditional",
                "FIN-BASE-STRESS",
                "BASE passed and STRESS failed.",
            )
        else:
            verdict, rule_id, reason = "reject", "FIN-BASE-STRESS", "BASE failed."

        result = {
            "scenario_id": scenario.label,
            "verdict": verdict,
            "adjustability": "NOT_NEEDED" if verdict == "ok" else "NOT_ADJUSTABLE",
            "finance_cap_amount_krw": _num(cap),
            "scenario_projected_cash_min": _num(base_with_scenario.projected_cash_min),
            "stress_projected_cash_min": _num(stress_with_scenario.projected_cash_min),
            "critical_cash_date": base_with_scenario.projected_cash_min_date.isoformat(),
            "payment_schedule": [
                {
                    "payment_date": payment.payment_date.isoformat(),
                    "amount_krw": _num(payment.amount_krw),
                    "amount_max_krw": _num(payment.amount_max_krw),
                }
                for payment in schedule
            ],
            "reason": reason,
            "rule_id": rule_id,
        }
        verdicts.append(result)
        ref = context.snapshot.finance_state_id
        evidences.extend(
            (
                _ev(f"verdicts[{index}].finance_cap_amount_krw", cap, "KRW", ref),
                _ev(
                    f"verdicts[{index}].scenario_projected_cash_min",
                    base_with_scenario.projected_cash_min, "KRW", ref,
                ),
                _ev(
                    f"verdicts[{index}].stress_projected_cash_min",
                    stress_with_scenario.projected_cash_min, "KRW", ref,
                ),
            )
        )

    business = "reject" if any(v["verdict"] == "reject" for v in verdicts) else (
        "conditional" if any(v["verdict"] == "conditional" for v in verdicts) else "ok"
    )
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=as_of,
        agent="finance",
        mode=request.mode,
        run_id=run_id,
        runtime_status="READY",
        business_status=business,
        payload={"verdicts": verdicts},
        evidences=tuple(evidences),
        reasoning="Purchase scenarios were evaluated with Finance cashflow rules.",
    )
    return reply, _meta(
        request, run_id, [_T_POSITION, _T_CASHFLOW, _T_CAP, "evaluate_purchase_scenario"]
    )


def _purchase_proposal(payload: Mapping[str, Any]) -> PurchaseProposal:
    """Envelope 전용 키를 버린 뒤 실제 Purchase 계약을 검증한다."""
    fields = PurchaseProposal.model_fields
    return PurchaseProposal.model_validate(
        {key: value for key, value in payload.items() if key in fields}
    )


def _invalid_scenario_input(
    request: AgentRequest, run_id: str, exc: ValidationError
) -> tuple[AgentReply, ExecutionMetadata]:
    reply = AgentReply(
        request_id=request.context.request_id, as_of=request.context.as_of, agent="finance",
        mode=request.mode, run_id=run_id, runtime_status="ERROR", business_status="skipped",
        payload={
            "validation_errors": [
                ".".join(str(part) for part in item["loc"]) or item["type"]
                for item in exc.errors()
            ]
        },
        reasoning="Purchase scenario input failed Finance contract validation.",
    )
    return reply, _meta(request, run_id, [])


def _invalid_scenario_as_of(
    request: AgentRequest, run_id: str, proposal_as_of: date
) -> tuple[AgentReply, ExecutionMetadata]:
    reply = AgentReply(
        request_id=request.context.request_id, as_of=request.context.as_of, agent="finance",
        mode=request.mode, run_id=run_id, runtime_status="ERROR", business_status="skipped",
        payload={"validation_errors": ["proposal.meta.as_of"]},
        reasoning="Purchase proposal as-of does not match the Master request.",
    )
    return reply, _meta(request, run_id, [])


def _not_implemented(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """★ 시나리오 판정은 **매입 시나리오 필드명이 확정돼야** 붙는다.

    재무의 판정 엔진(`run_finance_procurement_with_context`)은 이미 있다. 막힌 것은
    **번역**이다 — 마스터가 받는 `scenarios[]` 의 키 이름을 아직 받지 못했다.

    추측해서 매핑하면 **숫자는 나오는데 틀린 값을 판정하게 된다.** 그건 에러도 안 나고
    검증도 통과한다 — §1.2-10 이 막으려는 바로 그 종류다.

    `skipped` 로 돌려주므로 Flow 는 끝까지 돌고, **못 본 사실이 verdicts 와 실행 계획에
    남는다** (§3.7.6).
    """
    run_id = _run_id(request)
    # 🔴 mode 마다 못 하는 **이유가 다르다.** 예전에는 어느 mode 로 와도
    #    "매입 시나리오 필드명이 확정되지 않아" 를 냈는데, 그건 `SCENARIO_VALIDATION`
    #    의 사유일 뿐이다. 다른 mode 에 그대로 붙이면 **거짓 사유가 이력에 남고**,
    #    마스터가 사용자에게 요청할 대상을 잘못 알려 준다.
    if request.mode == "SCENARIO_VALIDATION":
        missing = ("purchase_scenario_schema",)
        reason = "매입 시나리오 필드명이 확정되지 않아 판정하지 못했다."
    else:
        missing = (f"{request.mode}_translation",)
        reason = f"{request.mode} 는 재무 어댑터에 아직 없다."
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent="finance",
        mode=request.mode,
        run_id=run_id,
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=missing,
        missing_capability=(f"{request.mode} 번역",),
        reasoning=reason,
    )
    return reply, _meta(request, run_id, [])


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _load_context() -> FinanceRuntimeContext | None:
    try:
        return get_current_finance_runtime_context()
    except Exception:  # noqa: BLE001 — 없는 것은 예외가 아니라 상태다
        return None


def _critical_payment_dates(
    projection: CashflowProjection,
    outflow_by_date: Mapping[date, Decimal],
    floor: Decimal,
) -> list[str]:
    """**지급일 중** 위험한 날 (2026-08-27 재무 정의).

    ```text
    critical_payment_dates = minimum_cash 위반 지급일  ∪  최대 일일 유출 지급일
    ```

    ★ **제 초안을 재무가 되돌렸다.** 처음에는 *"잔액이 최소현금 아래인 날 + 투영
      최저일"* 로 정의했는데, 그건 `critical_cash_date` 지 `critical_payment_dates` 가
      아니다 — **현금이 위험한 날**과 **지급이 몰린 날**은 다르다.

      매입은 이 값으로 *"분할 회차 지급일이 이미 지급부담이 큰 날과 겹치는가"* 를
      본다. 지급이 없는 날은 겹칠 수가 없으므로 **후보는 지급일뿐이다.**

    따라서 실제 예정 유출이 있는 날만 후보로 두고, 그중 두 종류를 고른다.
    """
    payment_dates = {d for d, amount in outflow_by_date.items() if amount > 0}
    if not payment_dates:
        return []

    balance_at = {p.projection_date: p.cash_balance_krw for p in projection.projected_cash_by_date}
    picked = {d for d in payment_dates if balance_at.get(d, floor) < floor}

    peak = max(outflow_by_date[d] for d in payment_dates)
    picked |= {d for d in payment_dates if outflow_by_date[d] == peak}

    return sorted(d.isoformat() for d in picked)


def _outflow_by_date(events: Sequence[Any]) -> dict[date, Decimal]:
    """날짜별 **유출** 합계. 유입은 세지 않는다 — 지급 집중도를 보는 값이다."""
    out: dict[date, Decimal] = {}
    for event in events:
        if event.direction == "INFLOW":
            continue
        out[event.event_date] = out.get(event.event_date, Decimal(0)) + event.amount_krw
    return out


def _critical_cash_date(projection: CashflowProjection) -> str:
    """현금 잔액이 가장 낮은 날 — **지급 집중도가 아니라 현금 위험**이다 (재무 구분)."""
    return projection.projected_cash_min_date.isoformat()


def _ratio(numerator: Decimal, denominator: Decimal) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _num(value: Decimal | float) -> float:
    return float(value)


def _policy_ref(policy: FinancePolicy, key: str, fallback: str) -> str:
    """정책값 근거는 **Policy 의 출처**를 가리킨다.

    없으면 스냅샷 참조로 떨어진다 — 그 사실 자체는 `missing_data` 의
    `{key}@policy_source_ref` 가 따로 남긴다.
    """
    return policy.source_refs.get(key) or fallback


def _ev(
    claim: str,
    value: Any,
    unit: str,
    ref: str,
    detail: str = "",
    grade: str = "OFFICIAL",
) -> Evidence:
    """★ `value` 에는 **판정을 만든 근거 수치**를 넣는다.

    목록형 claim 에 항목 **개수**를 넣고 싶어지는데, 그건 답의 길이를 세어 답이라고
    적는 것이다. 나중에 *"왜 그날이 위험일인가"* 를 보는 사람에게 아무것도 말해 주지
    않는다. **그 목록을 만든 임계값**을 넣는다.
    """
    return Evidence(
        claim=claim,
        source="finance",
        ref_ids=(ref,),
        value=float(value),
        unit=unit,
        evidence_grade=grade,
        evidence_detail=detail,
    )


def _run_id(request: AgentRequest) -> str:
    return f"FIN-{request.context.request_id}-{request.call_seq}"


def _meta(request: AgentRequest, run_id: str, tools: Sequence[str]) -> ExecutionMetadata:
    return ExecutionMetadata(
        run_id=run_id,
        request_id=request.context.request_id,
        agent="finance",
        used_tools=tuple(tools),
        tool_order=tuple(range(1, len(tools) + 1)),
        llm_status="DISABLED",
    )


def _not_ready(
    request: AgentRequest,
    run_id: str,
    tools: Sequence[str],
    *,
    missing: tuple[str, ...],
    reason: str,
) -> tuple[AgentReply, ExecutionMetadata]:
    """입력이 없어서 못 낸 답. **`ERROR` 가 아니다** — 다시 불러도 같다 (M-1 §5.1)."""
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent="finance",
        mode=request.mode,
        run_id=run_id,
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=missing,
        reasoning=reason,
    )
    return reply, _meta(request, run_id, tools)
