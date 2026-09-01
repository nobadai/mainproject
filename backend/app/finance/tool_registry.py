"""Finance capability 실행 — **숫자와 판정은 전부 여기 아래에서 나온다.**

Controller 는 무엇을 부를지 정하고, 여기서는 그 capability 를 실제로 실행한다.
계산은 `app.finance.tools` 의 결정론 함수가, 판정은 `app.finance.rules` 가 만든다.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from app.finance.evidence import (
    _PAYROLL_SOURCE_KEYS,
    _branch_ref,
    _evidence,
    _optional_source_ref,
    _source_ref,
    _tool_ref,
)
from app.finance.llm.contracts import FinanceMode
from app.finance.repository import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.rules import classify_base_stress
from app.finance.schemas import CashEvent, FinancePolicy
from app.finance.state import FinanceAgentState, ScenarioPayment
from app.finance.tools import (
    build_payroll_schedule,
    calculate_finance_cap,
    derive_cash_priority,
    derive_critical_payment_dates,
    project_cashflow,
)
from app.orchestrator.contracts_core import Evidence

PRE_PURCHASE_TOOLS = frozenset(
    {
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    }
)
SCENARIO_VALIDATION_TOOLS = frozenset({"evaluate_purchase_scenario", "validate_amount_adjustment"})


class FinanceToolRegistry:
    def __init__(self, data_port: FinanceAsOfDataPort):
        self.data_port = data_port

    def names_for(self, mode: FinanceMode) -> frozenset[str]:
        return PRE_PURCHASE_TOOLS if mode == "PRE_PURCHASE" else SCENARIO_VALIDATION_TOOLS

    def execute(
        self, name: str, arguments: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        if name not in self.names_for(state.request.mode):
            raise ValueError(f"Tool {name} is not allowed for {state.request.mode}")
        return getattr(self, name)(arguments, state)

    def _context(
        self, state: FinanceAgentState
    ) -> tuple[dict[str, Any], FinancePolicy, list[CashEvent]]:
        if state.context_cache is not None:
            return state.context_cache
        ctx = state.request.context
        position = self.data_port.load_finance_position(ctx.as_of)
        policy = self.data_port.load_policy(ctx.as_of, ctx.policy_version)
        horizon = ctx.as_of + timedelta(days=policy.cashflow_projection_days)
        payroll_amount = self.data_port.load_payroll(ctx.as_of, horizon)
        if payroll_amount is None:
            raise FinanceDataNotReady("payroll_schedule")
        policy = policy.model_copy(update={"monthly_labor_cost_krw": payroll_amount})
        # 급여 출처는 fail-closed 다. `build_payroll_schedule` 도 막지만 그쪽은
        # `ValueError` 라 일반 `ERROR` 로 분류된다 — **입력이 없어서 못 내는 답**은
        # `RUNTIME_NOT_READY` 여야 재시도 가치가 제대로 남는다 (M-1 §5.1).
        for key in _PAYROLL_SOURCE_KEYS:
            _source_ref(policy, key)
        events = [
            *self.data_port.load_obligations(ctx.as_of, horizon),
            *self.data_port.load_receivables(ctx.as_of, horizon),
            *build_payroll_schedule(as_of=ctx.as_of, horizon_end=horizon, policy=policy),
        ]
        current_debt = Decimal(position["current_debt_krw"])
        if current_debt > 0:
            events.extend(self.data_port.load_debt_schedule(ctx.as_of, horizon))
        state.context_cache = (position, policy, events)
        return state.context_cache

    def assess_finance_position(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        position, policy, _ = self._context(state)
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

    def project_cashflow(self, args: dict[str, Any], state: FinanceAgentState) -> dict[str, Any]:
        del args
        position, policy, events = self._context(state)
        projection = project_cashflow(
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
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        _, policy, _ = self._context(state)
        if policy.purchase_payment_days is None:
            raise FinanceDataNotReady("purchase_payment_days")
        if state.projection is None:
            self.project_cashflow({}, state)
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
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        _, policy, events = self._context(state)
        if state.projection is None:
            self.project_cashflow({}, state)
        pressure = derive_cash_priority(
            projected_cash_min=state.projection.projected_cash_min, policy=policy
        )
        dates = [
            item.isoformat()
            for item in derive_critical_payment_dates(
                current_cash_krw=Decimal(self._context(state)[0]["current_cash_krw"]),
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

    def evaluate_purchase_scenario(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        payload = state.request.payload
        amount = Decimal(str(payload["total_amount_krw"]))
        position, policy, events = self._context(state)
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
            "payment_schedule": [
                ({
                    "seq": item.seq,
                    "purchase_date": item.purchase_date.isoformat(),
                    "payment_date": item.payment_date.isoformat(),
                    "qty_kg": str(item.qty_kg) if item.qty_kg is not None else None,
                    "amount_krw": str(item.amount_krw),
                    "amount_max_krw": str(item.amount_max_krw),
                    "basis": item.basis,
                } if item.qty_kg is not None else {
                    "payment_date": item.payment_date.isoformat(),
                    "amount_krw": str(item.amount_krw),
                })
                for item in schedule
            ],
            "reason": reason,
            "rules": [{"rule_id": rule_id, "status": "PASS" if verdict == "ok" else "FAIL"}],
            "evidence": [
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
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        axis = args.get("axis", "amount")
        if axis != "amount":
            raise ValueError("Finance may adjust only the amount axis")
        candidate = Decimal(str(args["candidate_amount_krw"]))
        if candidate < 0:
            raise ValueError("candidate amount must not be negative")
        self._context(state)
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
        if default_payment_days is None:
            raise FinanceDataNotReady("purchase_payment_days")
        payment_date = as_of + timedelta(days=default_payment_days)
        if not as_of < payment_date <= horizon:
            raise FinanceDataNotReady("default_purchase_payment_date")
        return (
            ScenarioPayment(
                seq=1,
                purchase_date=as_of,
                payment_date=payment_date,
                qty_kg=None,
                amount_krw=amount,
                amount_max_krw=amount,
                basis="non_split_policy_reconstruction",
            ),
        )
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
