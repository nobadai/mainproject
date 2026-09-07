"""Sales AgentRequest adapter.

Master owns routing and cross-domain orchestration. This adapter only translates
the Master envelope into the Sales-owned proposal core, then carries the typed
Sales result back in an AgentReply.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput, SalesProposalReply
from app.sales.service import list_sales_runs

AGENT_NAME = "sales"


def sales_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """Master-facing Sales port."""
    if request.mode == "GENERATE_SALES_PROPOSAL":
        return _generate(request)
    if request.mode == "STATUS_QUERY":
        return _status_query(request)
    return _not_implemented(request)


def _generate(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    run_id = _run_id()
    try:
        proposal_input = _proposal_input(request, run_id)
    except ValidationError as exc:
        return _invalid_input(request, run_id, exc)

    proposal = run_proposal(proposal_input)
    if proposal.status == "SCENARIOS_GENERATED" and not proposal.scenarios:
        return _contract_error(
            request,
            run_id,
            payload={"validation_errors": ["scenarios"]},
            reason="Sales proposal core returned SCENARIOS_GENERATED without scenarios.",
        )

    runtime_status = "READY"
    business_status = "ok"
    missing_data = tuple(proposal.missing_data)
    if proposal.status == "INPUT_INCOMPLETE":
        runtime_status = "RUNTIME_NOT_READY"
        business_status = "skipped"
        missing_data = _missing_for_incomplete(proposal)

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,
        mode=request.mode,
        run_id=run_id,
        runtime_status=runtime_status,
        business_status=business_status,
        payload=_proposal_payload(proposal),
        missing_data=missing_data,
        missing_capability=tuple(proposal.missing_capabilities),
        additional_validation_required=bool(proposal.missing_capabilities),
        reasoning=_reasoning(proposal),
    )
    return reply, _metadata(request, run_id, tools=("run_proposal",))


def _proposal_input(request: AgentRequest, run_id: str) -> SalesProposalInput:
    data: dict[str, Any] = {
        key: value
        for key, value in request.payload.items()
        if key in SalesProposalInput.model_fields
    }
    feedback_attempt = request.payload.get("feedback_attempt", 0)
    data["execution_identity"] = {
        "request_id": request.context.request_id,
        "run_id": run_id,
        "as_of": request.context.as_of,
        "policy_version": request.context.policy_version,
        "feedback_attempt": feedback_attempt,
    }
    if "feedback_attempt" in request.payload:
        data["feedback_attempt"] = feedback_attempt
    if int(feedback_attempt or 0) > 0:
        data["is_refeed"] = True
    return SalesProposalInput.model_validate(data)


def _proposal_payload(proposal: SalesProposalReply) -> Mapping[str, Any]:
    return proposal.model_dump(mode="json")


def _missing_for_incomplete(proposal: SalesProposalReply) -> tuple[str, ...]:
    missing = [*proposal.missing_data]
    missing.extend(f"capability:{capability}" for capability in proposal.missing_capabilities)
    if not missing:
        missing.append("sales_proposal_input")
    return tuple(dict.fromkeys(missing))


def _reasoning(proposal: SalesProposalReply) -> str:
    if proposal.status == "INPUT_INCOMPLETE":
        return "Sales proposal input is incomplete."
    return "Sales proposal scenarios were generated."


def _status_query(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    run_id = _run_id()
    try:
        runs = list_sales_runs(as_of=request.context.as_of, limit=5)
    except Exception:  # noqa: BLE001
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=AGENT_NAME,
            mode=request.mode,
            run_id=run_id,
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            missing_data=("sales_agent_runs",),
            reasoning="Sales run history is not available.",
        )
        return reply, _metadata(request, run_id, tools=("list_sales_runs",))

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,
        mode=request.mode,
        run_id=run_id,
        runtime_status="READY",
        business_status="ok",
        payload={
            "as_of": request.context.as_of.isoformat(),
            "recent_runs": [run.model_dump(mode="json") for run in runs],
        },
        reasoning="Sales run history was queried.",
    )
    return reply, _metadata(request, run_id, tools=("list_sales_runs",))


def _invalid_input(
    request: AgentRequest, run_id: str, exc: ValidationError
) -> tuple[AgentReply, ExecutionMetadata]:
    return _contract_error(
        request,
        run_id,
        payload={
            "validation_errors": [
                ".".join(str(part) for part in item["loc"]) or item["type"]
                for item in exc.errors()
            ]
        },
        reason="Sales proposal request payload is invalid.",
    )


def _contract_error(
    request: AgentRequest,
    run_id: str,
    *,
    payload: Mapping[str, Any],
    reason: str,
) -> tuple[AgentReply, ExecutionMetadata]:
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,
        mode=request.mode,
        run_id=run_id,
        runtime_status="ERROR",
        business_status="skipped",
        payload=dict(payload),
        reasoning=reason,
    )
    return reply, _metadata(request, run_id, tools=())


def _not_implemented(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    run_id = _run_id()
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,
        mode=request.mode,
        run_id=run_id,
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=(f"{request.mode}_translation",),
        missing_capability=(f"{request.mode} translation",),
        reasoning=f"{request.mode} is not implemented in the Sales adapter.",
    )
    return reply, _metadata(request, run_id, tools=())


def _metadata(
    request: AgentRequest, run_id: str, *, tools: tuple[str, ...]
) -> ExecutionMetadata:
    return ExecutionMetadata(
        run_id=run_id,
        request_id=request.context.request_id,
        agent=AGENT_NAME,
        used_tools=tools,
        tool_order=tuple(range(1, len(tools) + 1)),
        llm_status="SKIPPED_TEMPLATE",
    )


def _run_id() -> str:
    return str(uuid4())
