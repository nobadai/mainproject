"""Sales AgentRequest adapter.

Master owns routing and cross-domain orchestration. This adapter only translates
the Master envelope into the Sales-owned proposal core, then carries the typed
Sales result back in an AgentReply.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.sales.llm.runtime import load_settings
from app.sales.proposal import run_proposal
from app.sales.run_repository import save_sales_agent_run
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
            reason=(
                "판매안을 생성했지만 표시할 수 있는 안이 없습니다. "
                "실행 상태를 다시 확인해 주세요."
            ),
        )

    runtime_status = "READY"
    business_status = "ok"
    missing_data = tuple(proposal.missing_data)
    missing_capability = tuple(proposal.missing_capabilities)
    if proposal.status == "INPUT_INCOMPLETE":
        if not missing_data:
            return _contract_error(
                request,
                run_id,
                payload={"validation_errors": ["missing_data"]},
                reason="판매안을 만들기 위한 정보 확인 중 문제가 발생했습니다. 다시 시도해 주세요.",
            )
        runtime_status = "RUNTIME_NOT_READY"
        business_status = "skipped"
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
        missing_capability=missing_capability,
        additional_validation_required=bool(missing_capability),
        reasoning=_reasoning(proposal),
    )
    save_sales_agent_run(
        run_id=UUID(run_id),
        cycle="SALES",
        as_of=request.context.as_of,
        snapshot_id=None,
        runtime_status=runtime_status,
        request_payload=asdict(request),
        response_payload=asdict(reply),
    )
    return reply, _metadata(request, run_id, proposal=proposal, tools=("run_proposal",))


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


def _reasoning(proposal: SalesProposalReply) -> str:
    if proposal.status == "INPUT_INCOMPLETE":
        return "판매안을 만들기 위해 필요한 정보가 부족합니다. 부족한 항목을 확인해 주세요."
    return "판매안을 준비했습니다. 각 안의 수량, 납기, 재무 검토 상태를 확인해 주세요."


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
            reasoning="최근 판매 판단 이력을 확인할 수 없습니다.",
        )
        return reply, _metadata(request, run_id, tools=("list_sales_runs",))

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,
        mode=request.mode,
        run_id=str(runs[0].run_id) if runs else run_id,
        runtime_status="READY",
        business_status="ok",
        payload={
            "as_of": request.context.as_of.isoformat(),
            "recent_runs": [run.model_dump(mode="json") for run in runs],
        },
        reasoning="최근 판매 판단 이력을 조회했습니다.",
    )
    return reply, _metadata(request, reply.run_id, tools=("list_sales_runs",))


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
        reason="판매 요청 정보를 확인하지 못했습니다. 입력한 판매 조건을 다시 확인해 주세요.",
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
        reasoning="요청하신 판매 기능은 아직 연결되지 않았습니다.",
    )
    return reply, _metadata(request, run_id, tools=())


def _metadata(
    request: AgentRequest,
    run_id: str,
    *,
    proposal: SalesProposalReply | None = None,
    tools: tuple[str, ...],
) -> ExecutionMetadata:
    settings = load_settings()
    llm_status = "DISABLED" if not settings.enabled else "SKIPPED_TEMPLATE"
    llm_model = settings.model
    llm_attempts = 0
    llm_fallback_used = False
    if proposal is not None:
        llm_status = proposal.llm.status
        llm_model = proposal.llm.llm_model or ""
        llm_attempts = proposal.llm.llm_attempts
        llm_fallback_used = proposal.llm.llm_fallback_used
    return ExecutionMetadata(
        run_id=run_id,
        request_id=request.context.request_id,
        agent=AGENT_NAME,
        used_tools=tools,
        tool_order=tuple(range(1, len(tools) + 1)),
        llm_status=llm_status,
        llm_model=llm_model,
        llm_attempts=llm_attempts,
        llm_fallback_used=llm_fallback_used,
    )


def _run_id() -> str:
    return str(uuid4())
