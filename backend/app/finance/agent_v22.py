"""Finance v2.2 tool-using agent entry path.

This module deliberately does not import ``FinanceSnapshot``.  The legacy
procurement/sales services remain compatibility-only while this path uses the
M-1 execution context as its boundary.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import uuid4

from app.finance.repository import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.run_repository import save_finance_v22_run
from app.finance.schemas import CashEvent, FinancePolicy
from app.finance.tools import (
    build_payroll_schedule,
    calculate_finance_cap,
    derive_cash_priority,
    project_cashflow,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment

FinanceMode = Literal["PRE_PURCHASE", "SCENARIO_VALIDATION"]
Adjustability = Literal["NOT_NEEDED", "ADJUSTABLE", "NOT_ADJUSTABLE"]

DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_MAX_REPLANS = 2

PRE_PURCHASE_TOOLS = frozenset(
    {
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    }
)
SCENARIO_VALIDATION_TOOLS = frozenset({"evaluate_purchase_scenario", "validate_amount_adjustment"})


@dataclass(frozen=True)
class ToolAction:
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    finalize: bool = False


class FinancePlanner(Protocol):
    model: str
    attempts: int

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction: ...


class OllamaFinancePlanner:
    """LLM planner whose output is limited to an allowed tool call or finalize."""

    def __init__(self) -> None:
        self.model = os.getenv("LLM_MODEL", "gemma3:4b")
        self.base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        self.attempts = 0

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction:
        self.attempts += 1
        schema = {
            "type": "object",
            "properties": {
                "tool_name": {"type": ["string", "null"], "enum": [*sorted(allowed_tools), None]},
                "arguments": {"type": "object"},
                "reason": {"type": "string"},
                "finalize": {"type": "boolean"},
            },
            "required": ["tool_name", "arguments", "reason", "finalize"],
            "additionalProperties": False,
        }
        prompt = {
            "mode": request.mode,
            "business_payload": dict(request.payload),
            "allowed_tools": sorted(allowed_tools),
            "observations": observations,
            "missing_capabilities": missing_capabilities,
            "tool_argument_contracts": {
                "assess_finance_position": {},
                "project_cashflow": {},
                "calculate_purchase_finance_cap": {},
                "analyze_payment_pressure": {},
                "evaluate_purchase_scenario": {},
                "validate_amount_adjustment": {
                    "axis": "amount",
                    "candidate_amount_krw": (
                        "copy the exact finance_cap_amount_krw from a prior observation; "
                        "never create a number"
                    ),
                },
            },
        }
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": schema,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You plan Finance capability calls. Select only an allowed tool. "
                        "Never calculate or invent financial numbers or policy values. "
                        "Use observations only. When missing_capabilities is non-empty, "
                        "you MUST set finalize=false and select exactly one tool from "
                        "missing_capabilities. You may set finalize=true only when "
                        "missing_capabilities is empty; then tool_name must be null."
                        " For validate_amount_adjustment, copy the observed deterministic "
                        "finance_cap_amount_krw exactly and set axis to amount."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, default=str)},
            ],
            "options": {"temperature": 0},
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw = json.loads(response.read().decode())
        content = json.loads(raw["message"]["content"])
        return ToolAction(**content)


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
            raise FinanceDataNotReady("payroll_amount")
        policy = policy.model_copy(
            update={"payroll_date": 10, "monthly_labor_cost_krw": payroll_amount}
        )
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
        return {
            "available_cash": str(position["current_cash_krw"]),
            "minimum_cash_balance_krw": str(policy.minimum_cash_balance_krw),
            "payroll_payment_day": 10,
            "evidence": [
                _evidence(
                    "available_cash", position["current_cash_krw"], "krw", "finance-position"
                ),
                _evidence(
                    "minimum_cash_balance_krw",
                    policy.minimum_cash_balance_krw,
                    "krw",
                    policy.source_refs["minimum_cash_balance_krw"],
                ),
                Evidence(
                    claim="payroll_payment_day",
                    source="persona",
                    ref_ids=("D-FIN-01",),
                    value=10,
                    unit="day_of_month",
                    evidence_grade="SIM_FIXED",
                    evidence_detail="Finance v2.2 D-FIN-01 approved policy",
                ),
            ],
        }

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
            "projected_cash_min": str(projection.projected_cash_min),
            "critical_cash_date": projection.projected_cash_min_date.isoformat(),
            "evidence": [
                _evidence(
                    "projected_cash_min",
                    projection.projected_cash_min,
                    "krw",
                    "cashflow-projection",
                )
            ],
        }

    def calculate_purchase_finance_cap(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        _, policy, _ = self._context(state)
        if state.projection is None:
            self.project_cashflow({}, state)
        cap = calculate_finance_cap(base_projection=state.projection, policy=policy)
        return {
            "finance_cap_amount_krw": str(cap),
            "evidence": [_evidence("finance_cap_amount_krw", cap, "krw", "finance-cap")],
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
        dates = sorted(
            {event.event_date.isoformat() for event in events if event.direction == "OUTFLOW"}
        )
        ratio = state.projection.projected_cash_min / policy.minimum_cash_balance_krw
        return {
            "payment_pressure": pressure,
            "critical_payment_dates": dates,
            "evidence": [
                _evidence(
                    "payment_pressure",
                    ratio,
                    "ratio",
                    policy.source_refs["cash_priority_reference"],
                ),
                _evidence("critical_payment_dates", len(dates), "count", "payment-schedule"),
            ],
        }

    def evaluate_purchase_scenario(
        self, args: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        del args
        payload = state.request.payload
        amount = Decimal(str(payload["total_amount_krw"]))
        schedule = payload.get("payment_schedule")
        if schedule is not None:
            total = sum((Decimal(str(row["amount_krw"])) for row in schedule), Decimal(0))
            if total != amount:
                raise ValueError("payment_schedule amount sum must equal total_amount_krw")
        _, policy, _ = self._context(state)
        if schedule is None and policy.purchase_payment_days is None:
            raise FinanceDataNotReady("purchase_payment_days")
        if state.projection is None:
            self.project_cashflow({}, state)
        cap = calculate_finance_cap(base_projection=state.projection, policy=policy)
        verdict = "ok" if amount <= cap else "reject"
        return {
            "scenario_id": payload["scenario_id"],
            "verdict": verdict,
            "adjustability": "NOT_NEEDED" if verdict == "ok" else "NOT_ADJUSTABLE",
            "finance_cap_amount_krw": str(cap),
            "reason": "Finance cap rule passed." if verdict == "ok" else "Finance cap rule failed.",
            "rules": [{"rule_id": "FIN-CAP", "status": "PASS" if verdict == "ok" else "FAIL"}],
            "evidence": [
                _evidence("finance_cap_amount_krw", cap, "krw", "finance-cap"),
                _evidence("verdict", amount <= cap, "boolean", "FIN-CAP"),
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
        _, policy, _ = self._context(state)
        if state.projection is None:
            self.project_cashflow({}, state)
        cap = calculate_finance_cap(base_projection=state.projection, policy=policy)
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
                _evidence("candidate_amount_krw", candidate, "krw", "validated-adjustment"),
                _evidence("validation_status", valid, "boolean", "FIN-CAP"),
            ],
        }


@dataclass
class FinanceAgentState:
    request: AgentRequest
    observations: list[dict[str, Any]] = field(default_factory=list)
    tool_order: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    replans: int = 0
    context_cache: tuple[dict[str, Any], FinancePolicy, list[CashEvent]] | None = None
    projection: Any = None


class FinanceAgentController:
    def __init__(
        self,
        data_port: FinanceAsOfDataPort,
        planner: FinancePlanner | None = None,
        *,
        max_tool_calls: int | None = None,
        max_replans: int | None = None,
    ):
        self.registry = FinanceToolRegistry(data_port)
        self.planner = planner or OllamaFinancePlanner()
        self.max_tool_calls = max_tool_calls or int(
            os.getenv("FINANCE_MAX_TOOL_CALLS", str(DEFAULT_MAX_TOOL_CALLS))
        )
        self.max_replans = (
            max_replans
            if max_replans is not None
            else int(os.getenv("FINANCE_MAX_REPLANS", str(DEFAULT_MAX_REPLANS)))
        )

    def run(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        if request.agent != "finance" or request.mode not in (
            "PRE_PURCHASE",
            "SCENARIO_VALIDATION",
        ):
            raise ValueError("Finance v2.2 supports only its two core modes")
        started = time.monotonic()
        run_id = str(uuid4())
        state = FinanceAgentState(request)
        runtime_status: Literal["READY", "RUNTIME_NOT_READY", "ERROR"] = "READY"
        missing_data: tuple[str, ...] = ()
        llm_status: Literal["SUCCESS", "FALLBACK", "DISABLED"] = "SUCCESS"
        error_reason = ""
        seen: set[str] = set()
        required = set(
            PRE_PURCHASE_TOOLS if request.mode == "PRE_PURCHASE" else {"evaluate_purchase_scenario"}
        )
        try:
            while len(state.tool_order) < self.max_tool_calls:
                missing = tuple(sorted(required - set(state.tool_order)))
                planner_tools = (
                    frozenset(missing) if missing else self.registry.names_for(request.mode)
                )
                action = self.planner.decide(
                    request=request,
                    allowed_tools=planner_tools,
                    observations=tuple(state.observations),
                    missing_capabilities=missing,
                )
                if action.finalize:
                    if missing:
                        if state.replans >= self.max_replans:
                            raise RuntimeError(
                                "required Finance capability planning did not complete"
                            )
                        state.replans += 1
                        state.observations.append({"type": "GUARD", "unresolved": list(missing)})
                        continue
                    break
                if action.tool_name is None:
                    raise RuntimeError("planner returned neither a tool nor finalize")
                if missing and action.tool_name not in missing:
                    if state.replans >= self.max_replans:
                        raise RuntimeError(
                            "planner repeatedly selected an already resolved Finance capability"
                        )
                    state.replans += 1
                    state.observations.append(
                        {
                            "type": "GUARD",
                            "rejected_tool": action.tool_name,
                            "unresolved": list(missing),
                        }
                    )
                    continue
                signature = json.dumps(
                    [action.tool_name, action.arguments], sort_keys=True, default=str
                )
                if signature in seen:
                    raise RuntimeError("duplicate unresolved Finance tool call blocked")
                seen.add(signature)
                arguments = action.arguments
                if action.tool_name == "validate_amount_adjustment":
                    axis = arguments.get("axis", "amount")
                    if axis != "amount":
                        raise ValueError("Finance may adjust only the amount axis")
                    source_amount = next(
                        (
                            request.payload[key]
                            for key in ("candidate_amount_krw", "proposed_amount_krw")
                            if request.payload.get(key) is not None
                        ),
                        None,
                    )
                    if source_amount is None:
                        source_amount = next(
                            (
                                item["result"]["finance_cap_amount_krw"]
                                for item in reversed(state.observations)
                                if item.get("tool") == "evaluate_purchase_scenario"
                            ),
                            None,
                        )
                    if source_amount is None:
                        raise FinanceDataNotReady("amount_adjustment_source")
                    arguments = {
                        "axis": "amount",
                        "candidate_amount_krw": source_amount,
                    }
                observation = self.registry.execute(action.tool_name, arguments, state)
                state.tool_order.append(action.tool_name)
                state.observations.append({"tool": action.tool_name, "result": observation})
                state.rules.extend(item["rule_id"] for item in observation.get("rules", []))
                if (
                    action.tool_name == "evaluate_purchase_scenario"
                    and observation.get("verdict") == "reject"
                ):
                    required.add("validate_amount_adjustment")
            else:
                raise RuntimeError("Finance tool call limit exceeded")
            if required - set(state.tool_order):
                raise RuntimeError("required Finance capability execution did not complete")
        except FinanceDataNotReady as exc:
            runtime_status, missing_data, error_reason = "RUNTIME_NOT_READY", (exc.key,), str(exc)
        except Exception as exc:  # noqa: BLE001 - boundary converts execution failures to ERROR.
            runtime_status, llm_status, error_reason = "ERROR", "DISABLED", str(exc)

        payload, evidences, business_status, adjustments = self._finalize(state, runtime_status)
        elapsed = int((time.monotonic() - started) * 1000)
        metadata = ExecutionMetadata(
            run_id=run_id,
            request_id=request.context.request_id,
            agent="finance",
            used_tools=tuple(state.tool_order),
            tool_order=tuple(range(1, len(state.tool_order) + 1)),
            observations=tuple(
                json.dumps(o, default=str, sort_keys=True) for o in state.observations
            ),
            rules_applied=tuple(state.rules),
            replans=state.replans,
            llm_status=llm_status,
            llm_model=self.planner.model,
            llm_attempts=self.planner.attempts,
            llm_fallback_used=False,
            elapsed_ms=elapsed,
        )
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="finance",
            mode=request.mode,
            run_id=run_id,
            runtime_status=runtime_status,
            business_status=business_status,
            payload=payload,
            evidences=tuple(evidences),
            suggested_adjustments=tuple(adjustments),
            reasoning=error_reason[:240],
            missing_data=missing_data,
        )
        save_finance_v22_run(request=request, reply=reply, metadata=metadata)
        return reply, metadata

    def _finalize(
        self, state: FinanceAgentState, runtime_status: str
    ) -> tuple[dict[str, Any], list[Evidence], str, list[SuggestedAdjustment]]:
        if runtime_status != "READY":
            return {}, [], "skipped", []
        payload: dict[str, Any] = {}
        evidences: list[Evidence] = []
        for observation in state.observations:
            result = observation.get("result", {})
            for key, value in result.items():
                if key not in {"evidence", "rules"}:
                    payload[key] = _json_value(value)
            evidences.extend(result.get("evidence", []))
        business_status = payload.get("verdict", "ok")
        adjustments: list[SuggestedAdjustment] = []
        validation = next(
            (
                o["result"]
                for o in reversed(state.observations)
                if o.get("tool") == "validate_amount_adjustment"
                and o["result"]["validation_status"] == "PASS"
            ),
            None,
        )
        if request_verdict := payload.get("verdict"):
            if request_verdict == "ok":
                payload["adjustability"] = "NOT_NEEDED"
            elif validation:
                payload["adjustability"] = "ADJUSTABLE"
                adjustments.append(
                    SuggestedAdjustment(
                        dept="finance",
                        axis="amount",
                        target_value=float(validation["candidate_amount_krw"]),
                        unit="krw",
                        reason="Verified Finance amount alternative.",
                        ref_ids=("validated-adjustment",),
                    )
                )
            else:
                payload["adjustability"] = "NOT_ADJUSTABLE"
        return payload, evidences, business_status, adjustments


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def _evidence(claim: str, value: Any, unit: str, ref_id: str) -> Evidence:
    numeric = float(value)
    return Evidence(
        claim=claim,
        source="tool_calc",
        ref_ids=(ref_id,),
        value=numeric,
        unit=unit,
        evidence_grade="OFFICIAL",
    )
