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

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.contracts.core import Evidence
from app.finance import messages
from app.finance.application.orchestration import FinanceAgentController
from app.finance.capabilities.sales import (
    SALES_VERDICT_TO_BUSINESS_STATUS,
    build_sales_validation_payload,
    map_sales_finance_verdict,
)
from app.finance.db import FinanceDataNotReady, get_current_finance_runtime_context
from app.finance.execution import (
    _PAYROLL_SOURCE_KEYS,
    missing_source_name,
    resolve_optional_source_ref,
    save_finance_execution,
)
from app.finance.llm.client import finance_llm_enabled
from app.finance.schemas import CashflowProjection, FinancePolicy, FinanceRuntimeContext
from app.finance.tools import (
    build_payroll_schedule,
    calculate_projected_cash_min,
    derive_cash_priority,
    project_cashflow,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
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

def finance_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """마스터가 부르는 유일한 접점."""
    if request.mode == "PRE_PURCHASE":
        return _controller_pre_purchase(request)
    if request.mode == "STATUS_QUERY":
        return _status_query(request)
    if request.mode == "SCENARIO_VALIDATION":
        return _controller_scenario_validation(request)
    if request.mode == "SALES_VALIDATION":
        return _controller_sales_validation(request)
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
        # 컨텍스트가 덕타이핑 Policy 를 들고 있으면 계약 타입으로 정규화한다.
        #
        # 🔴 **없는 `source_ref` 를 지어내지 않는다.** 예전에는 여기서 빠진 키마다
        #    `finance-policy:{version}:{key}` 를 채워 넣었다. 값은 멀쩡히 나오고 에러도
        #    안 나지만, 그 ref 는 DB 어디에도 없어서 **따라가면 아무 데도 닿지 않는다.**
        #    출처가 없는 것은 감출 일이 아니라 밝힐 일이다 — `_source_ref` 가
        #    `RUNTIME_NOT_READY` + `missing_data` 로 세운다 (§16).
        raw = {
            field: getattr(policy, field, None)
            for field in FinancePolicy.model_fields
            if field not in {"usage_scope"}
        }
        return FinancePolicy.model_validate(raw | {"usage_scope": "AGENT_MVP_DEMO"})

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


def _controller_sales_validation(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """판매 제안 재무 검증. **매입 시나리오 검증 경로를 재사용하지 않는다.**

    ★ 매입처럼 `PurchaseProposal` 로 payload 를 미리 검증하지 않는다. 판매 payload 의
      필수 항목이 무엇인지는 재무 판매 Capability 가 소유하고, 빠진 것은 `ERROR` 가
      아니라 `INPUT_INCOMPLETE`(→ `skipped`) 로 나간다 — 제안이 미완성인 것은 재무
      고장이 아니다.
    """
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
        missing_source_name(key)
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
            reason=messages.CONTEXT_UNAVAILABLE,
        )
    if context.snapshot.state_date != request.context.as_of:
        return None, _not_ready(
            request, run_id, [_T_POSITION],
            missing=(f"finance_state@{request.context.as_of.isoformat()}",),
            reason=messages.AS_OF_MISMATCH,
        )
    payroll_refs = tuple(
        missing_source_name(key)
        for key in _PAYROLL_SOURCE_KEYS
        if not context.policy.source_refs.get(key)
    )
    if payroll_refs:
        return None, _not_ready(
            request, run_id, [_T_POSITION],
            missing=payroll_refs,
            reason=messages.PAYROLL_SOURCE_MISSING,
        )
    # 🔴 매입 전용 정책이 **모든 mode** 를 막고 있었다.
    #
    #    `purchase_payment_days` 는 매입 지급일 상한(`calculate_finance_cap`)에만 쓰인다 —
    #    판매 검증은 이 값을 한 번도 읽지 않는다. 그런데 공통 boundary 에 있어서, 매입
    #    지급일 정책이 없는 날에는 **판매 검증도 실행 전에 통째로 막혔다.** 재무가 판매를
    #    못 본 이유가 "매입 정책이 없어서" 가 되는 것이라 사유 자체가 거짓이다.
    #
    # ★ 매입 쪽 방어는 그대로다. 아래 두 mode 에서는 여전히 실행 전에 요구한다.
    if request.mode in _PURCHASE_POLICY_MODES and context.policy.purchase_payment_days is None:
        return None, _not_ready(
            request, run_id, [_T_POSITION],
            missing=("purchase_payment_days",),
            reason=messages.PAYMENT_DAYS_MISSING,
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
            reason=messages.CONTEXT_UNAVAILABLE,
        )

    state_date = context.snapshot.state_date
    if state_date != as_of:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=(f"finance_state@{as_of.isoformat()}",),
            reason=messages.AS_OF_MISMATCH,
        )

    policy = context.policy
    ref = context.snapshot.finance_state_id
    missing: list[str] = []
    payload: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "state_date": state_date.isoformat(),
        "available_cash": _num(context.snapshot.current_cash_krw),
        "policy_version_used": policy.policy_version,
    }
    evidences = (
        _ev("available_cash", context.snapshot.current_cash_krw, "KRW", ref, "재무 상태 현재 잔액"),
    )

    # 🔴 값이 Policy 에서 왔으면 근거도 Policy 를 가리켜야 한다. 출처가 없으면
    #    스냅샷 id 로 때우지 않고 **값과 근거를 함께 뺀다** (`_policy_ref` 참조).
    minimum_cash_ref = _policy_ref(policy, "minimum_cash_balance_krw", missing)
    if minimum_cash_ref is not None:
        payload["minimum_cash_balance_krw"] = _num(policy.minimum_cash_balance_krw)
        evidences = (
            *evidences,
            _ev(
                "minimum_cash_balance_krw",
                policy.minimum_cash_balance_krw,
                "KRW",
                minimum_cash_ref,
                f"Finance Policy {policy.policy_version} · 1개월 급여 Reserve",
                grade="SIM_FIXED",
            ),
        )

    payroll_refs = tuple(
        missing_source_name(key)
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
        payload["projected_cash_min"] = _num(cash_min)
        payload["payment_pressure"] = pressure
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
        )

        projection_days_ref = _policy_ref(policy, "cashflow_projection_days", missing)
        if projection_days_ref is not None:
            payload["projection_days"] = policy.cashflow_projection_days
            evidences = (
                *evidences,
                _ev(
                    "projection_days",
                    policy.cashflow_projection_days,
                    "day",
                    projection_days_ref,
                    "현금 투영 Horizon — Finance Policy 값",
                    grade="SIM_FIXED",
                ),
            )

        if minimum_cash_ref is not None:
            critical_dates = _critical_payment_dates(
                projection, outflows, policy.minimum_cash_balance_krw
            )
            payload["critical_payment_dates"] = critical_dates
            evidences = (
                *evidences,
                _ev(
                    # ★ 목록의 **개수**가 아니라 그 목록을 만든 **임계값**을 넣는다
                    #   (`_ev` 규율). 개수를 넣으면 "왜 그날이 위험일인가" 에 아무
                    #   답이 안 된다.
                    "critical_payment_dates",
                    policy.minimum_cash_balance_krw,
                    "KRW",
                    minimum_cash_ref,
                    f"이 임계 미만으로 떨어지는 지급일 {len(critical_dates)} 건 "
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
        reasoning=messages.STATUS_QUERY if not missing else messages.STATUS_QUERY_PARTIAL,
    )
    return reply, _meta(request, run_id, tools)


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
        reasoning=messages.INVALID_REQUEST,
    )
    return _recorded(request, reply, _meta(request, run_id, []))


def _invalid_scenario_as_of(
    request: AgentRequest, run_id: str, proposal_as_of: date
) -> tuple[AgentReply, ExecutionMetadata]:
    reply = AgentReply(
        request_id=request.context.request_id, as_of=request.context.as_of, agent="finance",
        mode=request.mode, run_id=run_id, runtime_status="ERROR", business_status="skipped",
        payload={"validation_errors": ["proposal.meta.as_of"]},
        reasoning=messages.INVALID_REQUEST_AS_OF,
    )
    return _recorded(request, reply, _meta(request, run_id, []))


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
        reason = messages.SCENARIO_SCHEMA_MISSING
    else:
        missing = (f"{request.mode}_translation",)
        reason = messages.MODE_NOT_SUPPORTED
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


def _ratio(numerator: Decimal, denominator: Decimal) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _num(value: Decimal | float) -> float:
    return float(value)


def _policy_ref(policy: FinancePolicy, key: str, missing: list[str]) -> str | None:
    """정책값 근거는 **Policy 의 출처**를 가리킨다. 없으면 `None` 이다.

    🔴 예전에는 없을 때 **스냅샷 참조로 떨어졌다.** 정책에서 온 값에 재무 상태 행의
       id(`FIN-DAY30-LOAN`)를 달면 *"재무 상태 행에서 온 수"* 라고 말하는 것이라
       **거짓 출처**다 — 나중에 따라가면 엉뚱한 곳에 닿고, 닿았다는 사실만 남는다.

    ★ 조회는 **낼 수 있는 것만 낸다** (§3.7.6). 그래서 근거를 못 다는 claim 은
      payload 에서도 빼고 `missing_data` 로 밝힌다 — 일부가 빠졌다고 조회 전체를
      세우지는 않는다. 현재 잔액처럼 근거가 멀쩡한 값은 그대로 답한다.

    규칙 자체는 `evidence.resolve_optional_source_ref` 가 갖는다. 여기서는 **어디에
    적을지**만 정한다 — 조회는 상태가 아니라 지역 목록에 모은다.
    """
    def record(name: str) -> None:
        if name not in missing:
            missing.append(name)

    return resolve_optional_source_ref(policy, key, record)


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
    """어댑터가 직접 답할 때의 실행 식별자.

    ★ **UUID 다.** 예전에는 `FIN-{request_id}-{call_seq}` 였는데, Finance 실행이력
      (`finance_agent_runs_v22.run_id`)이 `UUID PRIMARY KEY` 라서 어댑터가 직접 낸
      회신은 **저장할 방법 자체가 없었다** — Controller 에 닿지 못한 실행은 이력에
      구멍으로 남았다.

    ★ **실행 식별자이지 요청 식별자가 아니다.** 한때 uuid5 로 (request_id, call_seq,
      mode) 를 넣어 결정론을 지키려 했는데, 그러면 **같은 요청을 두 번 실행하면 같은
      run_id 가 나온다.** 이력은 append-only 이고 run_id 가 기본키라, 두 번째 실행은
      저장되지 못하고 통째로 사라진다 — 하필 재실행은 남아야 할 실행이다.
      `FinanceAgentController.run` 도 같은 이유로 `uuid4` 를 쓴다.

    ★ 요청을 되짚는 축은 따로 있다. 같은 행의 `request_id` · `call_seq` · `mode` 로
      찾으면 되고, `idx_finance_runs_v22_request` 가 그 조회를 받친다.

    `del request` — 인자는 계약을 위해 남긴다. 이 함수는 요청 내용에 의존하지 않는다.
    """
    del request
    return str(uuid4())


def _meta(request: AgentRequest, run_id: str, tools: Sequence[str]) -> ExecutionMetadata:
    """어댑터가 LLM 없이 답한 실행의 메타데이터.

    🔴 예전에는 `llm_status="DISABLED"` 로 고정이었다. 그러면 LLM 을 **켜 둔** 배포에서
       조회·미준비 회신이 전부 *"LLM 을 안 켰다"* 로 남는다. 실제로는 **켜 뒀는데 이
       경로가 부를 일이 없었다** 이고, 그것이 `SKIPPED_TEMPLATE` 다 (envelope §LLMStatus).
    """
    return ExecutionMetadata(
        run_id=run_id,
        request_id=request.context.request_id,
        agent="finance",
        used_tools=tuple(tools),
        tool_order=tuple(range(1, len(tools) + 1)),
        llm_status="SKIPPED_TEMPLATE" if finance_llm_enabled() else "DISABLED",
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
    return _recorded(request, reply, _meta(request, run_id, tools))


#: 매입 실행 정책(`purchase_payment_days`)을 **실행 전에** 요구하는 mode.
#:
#: ★ 판매 검증은 여기 없다. 그 값은 매입 지급일 상한 계산에만 쓰이므로, 판매를
#:   막을 이유가 되지 않는다 — 막으면 "매입 정책이 없어서 판매를 못 봤다" 는
#:   거짓 사유가 이력에 남는다.
_PURCHASE_POLICY_MODES: frozenset[str] = frozenset({"PRE_PURCHASE", "SCENARIO_VALIDATION"})

#: 실행이력에 남기는 mode. **닫힌 허용목록이다** — 모르는 mode 는 여기 없다.
#:
#: ★ `SALES_VALIDATION` 은 `finance_agent_runs_v22.mode` CHECK 에
#:   SALES_VALIDATION 이 들어간 뒤에 열었다 (신규 DDL + 기존 DB 마이그레이션).
#:   제약보다 먼저 열면 판정은 되는데 **저장이 전부 실패한다.**
_CONTROLLER_MODES = ("PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION")


def _recorded(
    request: AgentRequest, reply: AgentReply, metadata: ExecutionMetadata
) -> tuple[AgentReply, ExecutionMetadata]:
    """Controller 에 닿지 못한 실행도 이력에 남긴다.

    ★ **이중 저장이 아니다.** 이 경로들은 전부 `FinanceAgentController.run` 을 부르기
      *전에* 회신을 확정하고 돌아간다 — Controller 가 저장하는 실행과 겹치지 않는다.

    ★ `STATUS_QUERY` 는 여기 오지 않는다. `finance_agent_runs_v22.mode` 의 CHECK 가
      두 core mode 만 허용하기 때문이다 (아래 §UNRESOLVED). 조회 이력을 남기려면
      스키마를 고쳐야 하는데, 그것은 Finance 코드 밖이다.

    ★ 저장 실패가 **업무 답을 바꾸지 않는다.** "재무 상태를 못 읽었다"는 사실은 저장
      여부와 무관하게 참이고 재시도 가치도 같다. 대신 실패 자체는 감추지 않고
      observations 에 남겨 마스터가 실행계획에서 볼 수 있게 한다.
    """
    if request.mode not in _CONTROLLER_MODES:
        return reply, metadata
    try:
        save_finance_execution(request=request, reply=reply, metadata=metadata)
    except Exception as error:  # noqa: BLE001 - 이력 실패로 업무 회신을 뒤집지 않는다.
        failure = json.dumps(
            {
                "observation_type": "finance_run_persistence_failed",
                "reason": type(error).__name__,
            },
            sort_keys=True,
        )
        return reply, replace(metadata, observations=(*metadata.observations, failure))
    return reply, metadata


# ---------------------------------------------------------------------------
# 판매 재무 판정 봉투 매핑 — **재무가 소유한다**
#
# 표와 함수는 `capabilities/sales.py` 에 한 벌만 둔다 (Orchestration 도 같은 표를
# 읽어야 하는데, Adapter → Orchestration 방향 import 라 반대는 순환이 된다).
# 여기서 다시 내보내는 이유는 **밖에서 보는 주소를 재무 Adapter 로 고정**하기
# 위해서다 — 마스터가 이 매핑을 자기 코드에서 다시 하지 않는다.
# ---------------------------------------------------------------------------

__all__ = [
    "SALES_VERDICT_TO_BUSINESS_STATUS",
    "build_sales_validation_payload",
    "finance_port",
    "map_sales_finance_verdict",
]
