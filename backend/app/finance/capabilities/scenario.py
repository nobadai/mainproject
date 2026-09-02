"""SCENARIO_VALIDATION capability — 제출된 시나리오를 **검증**한다.

이 파일이 소유하는 것
    지급 일정 재구성과 정규화 · BASE/STRESS 현금 사건 ·
    시나리오 투영 overlay · 시나리오 Finance Cap · 판정 조립 · 금액 대안 검증

여기 **없는 것**
    금액 공식(`tools`) · BASE/STRESS 판정 규칙(`rules`) · 컨텍스트 적재
    (`capabilities.procurement`) · 실행 통제(`application.harness`)

★ 재무는 매입 제안을 **고쳐 쓰지 않는다.** 제출된 사실을 읽고 투영해 판정할 뿐이고,
  조정은 금액 축에서만 제안한다.

★ 금액 대안은 **원천이 있는 값만** 받는다. 모델이 만든 숫자는 여기 닿지 못한다.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from app.finance import messages
from app.finance.capabilities.procurement import load_context
from app.finance.db import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.execution import _branch_ref, _evidence, _tool_ref
from app.finance.rules import classify_base_stress
from app.finance.schemas import CashEvent
from app.finance.state import FinanceAgentState, ScenarioPayment
from app.finance.tools import project_cashflow

# ---------------------------------------------------------------------------
# 지급 일정 재구성 · 정규화 · BASE/STRESS 현금 사건
# ---------------------------------------------------------------------------

def _reconstructed_payment(
    scenario: Any,
    as_of: date,
    horizon: date,
    default_payment_days: int | None,
    amount: Decimal,
) -> ScenarioPayment:
    """`payment_schedule` 이 없는 제안을 **제출된 사실만으로** 재구성한다.

    🔴 예전에는 `purchase_date=as_of` · `qty_kg=None` · `amount_max=amount` 였다.
       세 가지가 동시에 잘못됐다.

         · 매입일을 `as_of` 로 두면 N5 지급일이 **실제 매입일이 아닌 오늘**에서 계산된다.
         · STRESS 금액이 BASE 와 같으면 **BASE/STRESS 검증이 아무것도 가르지 않는다** —
           두 투영이 같은 값을 내고 늘 함께 통과한다. 검사가 있으나 마나였다.

    ★ 재무는 매입이 제출한 사실을 **읽고 파생**할 뿐, 소유하지 않는다.
        purchase_date  ← split_plan[0].date        (매입 소유)
        payment_date   ← purchase_date + N5        (재무 정책)
        qty_kg         ← total_qty_kg              (매입 소유)
        BASE  amount   ← total_amount_krw          (매입 소유)
        STRESS amount  ← total_qty_kg × max_price  (매입 소유 값에서 파생)

      파생한 STRESS 금액은 **재무 검증 메타데이터**다. 매입 제안을 고쳐 쓴 것이 아니다.

    ★ 없는 값은 지어내지 않는다. 분할이 여러 건인데 지급 일정이 없으면 금액을 어떻게
      쪼갤지는 **매입이 정할 일**이라 재무가 배분을 발명하지 않고 fail-closed 한다.
    """
    if default_payment_days is None:
        raise FinanceDataNotReady("purchase_payment_days")

    split_plan = scenario.get("split_plan")
    if not isinstance(split_plan, list) or not split_plan:
        raise FinanceDataNotReady("scenario_split_plan")
    if len(split_plan) != 1:
        # 분할이 여러 건이면 각 건의 금액은 매입만 안다.
        raise FinanceDataNotReady("scenario_payment_schedule")

    split = split_plan[0]
    raw_date = split.get("date")
    if raw_date is None:
        raise FinanceDataNotReady("scenario_split_plan_date")
    purchase_date = date.fromisoformat(str(raw_date))

    payment_date = purchase_date + timedelta(days=default_payment_days)
    if not as_of < payment_date <= horizon:
        raise FinanceDataNotReady("default_purchase_payment_date")

    qty = _positive_decimal(scenario.get("total_qty_kg"), "scenario_total_qty_kg")
    max_price = _positive_decimal(scenario.get("max_price"), "scenario_max_price")

    return ScenarioPayment(
        seq=1,
        purchase_date=purchase_date,
        payment_date=payment_date,
        qty_kg=qty,
        amount_krw=amount,
        amount_max_krw=qty * max_price,
        basis="non_split_policy_reconstruction",
    )


def _payment_row(item: ScenarioPayment) -> dict[str, Any]:
    """재무 검증 일정 한 행. **모든 행이 같은 모양이다.**"""
    return {
        "seq": item.seq,
        "purchase_date": item.purchase_date.isoformat(),
        "payment_date": item.payment_date.isoformat(),
        "qty_kg": str(item.qty_kg) if item.qty_kg is not None else None,
        "amount_krw": str(item.amount_krw),
        "amount_max_krw": str(item.amount_max_krw),
        "basis": item.basis,
    }


def _positive_decimal(value: Any, missing_key: str) -> Decimal:
    """제출된 양수 사실을 읽는다. 없거나 양수가 아니면 **멈춘다.**"""
    if value is None:
        raise FinanceDataNotReady(missing_key)
    amount = Decimal(str(value))
    if amount <= 0:
        raise FinanceDataNotReady(missing_key)
    return amount


def _scenario_schedule(
    *,
    scenario: Any,
    as_of: date,
    horizon: date,
    default_payment_days: int | None,
) -> tuple[ScenarioPayment, ...]:
    amount = Decimal(str(scenario["total_amount_krw"]))
    if amount <= 0:
        raise ValueError("total_amount_krw must be positive")
    raw_schedule = scenario.get("payment_schedule")
    if raw_schedule is None:
        return (_reconstructed_payment(scenario, as_of, horizon, default_payment_days, amount),)
    if not isinstance(raw_schedule, list) or not raw_schedule:
        raise ValueError("payment_schedule must be a non-empty list")
    split_plan = scenario.get("split_plan")
    if not isinstance(split_plan, list) or len(split_plan) != len(raw_schedule):
        raise ValueError("payment_schedule must correspond one-to-one with split_plan")
    total_qty = Decimal(str(scenario["total_qty_kg"]))
    max_price = Decimal(str(scenario["max_price"]))
    authoritative_h1 = bool(
        scenario.get("h1_authoritative") or scenario.get("authoritative_h1_payment_data")
    )
    schedule: list[ScenarioPayment] = []
    for index, (row, split) in enumerate(zip(raw_schedule, split_plan, strict=True), start=1):
        required = {
            "seq", "purchase_date", "payment_date", "qty_kg", "amount_krw",
            "amount_max_krw", "basis",
        }
        if not required <= row.keys():
            raise ValueError("payment_schedule row is missing required Finance fields")
        purchase_date = date.fromisoformat(str(row["purchase_date"]))
        payment_date = date.fromisoformat(str(row["payment_date"]))
        payment_amount = Decimal(str(row["amount_krw"]))
        max_amount = Decimal(str(row["amount_max_krw"]))
        qty = Decimal(str(row["qty_kg"]))
        basis = str(row["basis"]).strip()
        if not isinstance(payment_date, date) or not as_of < payment_date <= horizon:
            raise ValueError("payment_date must be inside the Finance projection horizon")
        if int(row["seq"]) != index or int(split["seq"]) != index:
            raise ValueError("payment_schedule and split_plan seq must align")
        if purchase_date != date.fromisoformat(str(split["date"])):
            raise ValueError("payment_schedule purchase_date must equal split_plan date")
        split_qty = Decimal(str(split.get("qty_kg", split.get("quantity_kg"))))
        if qty != split_qty:
            raise ValueError("payment_schedule qty_kg must equal split_plan qty_kg")
        if payment_amount <= 0 or max_amount <= 0 or qty <= 0:
            raise ValueError("payment_schedule amounts and qty must be positive")
        if not basis:
            raise ValueError("payment_schedule basis must be non-empty")
        if not authoritative_h1:
            if default_payment_days is None:
                raise FinanceDataNotReady("purchase_payment_days")
            if payment_date != purchase_date + timedelta(days=default_payment_days):
                raise ValueError("payment_date must equal purchase_date plus policy days before H1")
            if max_amount != qty * max_price:
                raise ValueError("amount_max_krw must equal qty_kg times max_price")
        schedule.append(
            ScenarioPayment(
                index, purchase_date, payment_date, qty, payment_amount, max_amount, basis
            )
        )
    if sum((item.amount_krw for item in schedule), Decimal(0)) != amount:
        raise ValueError("payment_schedule amount sum must equal total_amount_krw")
    if sum((item.qty_kg or Decimal(0) for item in schedule), Decimal(0)) != total_qty:
        raise ValueError("payment_schedule qty sum must equal total_qty_kg")
    return tuple(schedule)


def _schedule_events(
    scenario_id: object, schedule: tuple[ScenarioPayment, ...], *, stress: bool
) -> tuple[CashEvent, ...]:
    return tuple(
        CashEvent(
            event_date=payment.payment_date,
            event_type="EXTRA_PURCHASE",
            amount_krw=payment.amount_max_krw if stress else payment.amount_krw,
            direction="OUTFLOW",
            ref_id=(
                f"SCENARIO:{scenario_id}:{'STRESS' if stress else 'BASE'}:"
                f"{index}:{payment.payment_date.isoformat()}"
            ),
            source_ref=str(scenario_id),
        )
        for index, payment in enumerate(schedule, start=1)
    )


def _calculate_schedule_cap(
    *,
    base_projection: Any,
    schedule: tuple[ScenarioPayment, ...],
    total_amount: Decimal,
    minimum_cash: Decimal,
) -> Decimal:
    balances = {
        point.projection_date: point.cash_balance_krw
        for point in base_projection.projected_cash_by_date
    }
    dates = sorted({*balances, *(item.payment_date for item in schedule)})
    current_balance = balances[base_projection.as_of]
    paid = Decimal(0)
    bounds: list[Decimal] = []
    schedule_by_date: dict[date, Decimal] = {}
    for payment in schedule:
        schedule_by_date[payment.payment_date] = (
            schedule_by_date.get(payment.payment_date, Decimal(0)) + payment.amount_krw
        )
    for current_date in dates:
        if current_date in balances:
            current_balance = balances[current_date]
        paid += schedule_by_date.get(current_date, Decimal(0))
        if paid > 0:
            fraction = paid / total_amount
            bounds.append((current_balance - minimum_cash) / fraction)
    if not bounds:
        raise FinanceDataNotReady("scenario_payment_schedule")
    return max(Decimal(0), min(bounds).quantize(Decimal(1), rounding=ROUND_FLOOR))


# ---------------------------------------------------------------------------
# 시나리오 판정과 금액 대안 검증
# ---------------------------------------------------------------------------

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
        reason = messages.BASE_MINIMUM_CASH_VIOLATED
    elif scenario_verdict == "ok":
        verdict, rule_id, reason = (
            "ok",
            "FIN-BASE-STRESS",
            messages.SCENARIO_REASON_OK,
        )
    elif scenario_verdict == "conditional":
        verdict, rule_id, reason = (
            "conditional",
            "FIN-BASE-STRESS",
            messages.SCENARIO_REASON_CONDITIONAL,
        )
    else:
        verdict, rule_id, reason = (
            "reject",
            "FIN-BASE-STRESS",
            messages.SCENARIO_REASON_REJECT,
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
