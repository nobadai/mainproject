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

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.finance.repository import get_current_finance_runtime_context
from app.finance.schemas import CashflowProjection, FinanceRuntimeContext
from app.finance.tools import (
    build_payroll_schedule,
    calculate_finance_cap,
    calculate_projected_cash_min,
    derive_cash_priority,
    project_cashflow,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.orchestrator.contracts_core import Evidence

# 재무 1차 Tool Set — T-FIN-01~06 (2026-08-27 확정). 그날 실제로 부른 것만 남긴다.
_T_POSITION = "assess_finance_position"
_T_CASHFLOW = "project_cashflow"
_T_CAP = "calculate_purchase_finance_cap"
_T_PRESSURE = "analyze_payment_pressure"

# 매입이 읽는 판정 필드. 대문자라 휴리스틱도 잡지만, 소문자로 바뀌어도 살아남게 선언한다.
_JUDGMENT_FIELDS = ("payment_pressure",)


def finance_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """마스터가 부르는 유일한 접점."""
    if request.mode == "PRE_PURCHASE":
        return _pre_purchase(request)
    return _not_implemented(request)


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

    tools.append(_T_CASHFLOW)
    projection = project_cashflow(
        as_of=as_of,
        current_cash_krw=context.snapshot.current_cash_krw,
        horizon_end=horizon_end,
        cash_events=(
            *context.cash_events,
            *build_payroll_schedule(as_of=as_of, horizon_end=horizon_end, policy=policy),
        ),
    )

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
        "critical_payment_dates": _critical_dates(projection, policy.minimum_cash_balance_krw),
        # ★ 재현 4종의 하나 (§3.2.4). 마스터가 준 policy_version 과 다를 수 있어 밝힌다 — M-20
        "policy_version_used": policy.policy_version,
    }

    # 🔴 `margin_defense_floor_rate` 는 재무 payload 로 오기로 했는데(M-19 해소)
    #    구현된 FinancePolicy 에 그 필드가 없다. 0 이나 임의값으로 채우지 않는다 (§1.2-10).
    missing = () if hasattr(policy, "margin_defense_floor_rate") else ("margin_defense_floor_rate",)
    if not missing:
        payload["margin_defense_floor_rate"] = _num(policy.margin_defense_floor_rate)

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
            "판정 임계 = 최소현금 보유선. 투영 잔액이 이 값 아래인 날 + 투영 최저일",
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
        missing_data=missing,
        reasoning="재무 경계를 산출했다.",
    )
    return reply, _meta(request, run_id, tools)


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — 아직 못 한다
# ---------------------------------------------------------------------------


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
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent="finance",
        mode=request.mode,
        run_id=run_id,
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=("purchase_scenario_schema",),
        missing_capability=(f"{request.mode} 번역",),
        reasoning="매입 시나리오 필드명이 확정되지 않아 판정하지 못했다.",
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


def _critical_dates(projection: CashflowProjection, floor: Decimal) -> list[str]:
    """현금이 최소 보유선 아래로 내려가는 날 + 바닥 나는 날.

    ⚠️ **판정 규칙은 재무 확인이 필요하다.** 지금은 `minimum_cash_balance_krw` 미만으로
    정의했다 — 정책값에 묶인 결정론 규칙이라 재현은 되지만, "지급이 몰리는 날"의 재무
    정의와 다를 수 있다. `evidence_detail` 에 규칙을 적어 두었으니 다르면 알려 주십시오.
    """
    dates = {
        p.projection_date.isoformat()
        for p in projection.projected_cash_by_date
        if p.cash_balance_krw < floor
    }
    dates.add(projection.projected_cash_min_date.isoformat())
    return sorted(dates)


def _ratio(numerator: Decimal, denominator: Decimal) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _num(value: Decimal | float) -> float:
    return float(value)


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

