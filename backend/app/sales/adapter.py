"""Sales AgentRequest adapter.

Master가 라우팅과 도메인 간 orchestration을 소유한다. 이 adapter는 Master envelope을
Sales proposal core로 옮기고, typed Sales 결과를 AgentReply로 되돌리는 경계다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.sales.console_proposals import ConsoleSalesProposalsResponse, get_console_sales_proposals
from app.sales.llm.runtime import load_settings
from app.sales.partner_profile import get_partner_profile
from app.sales.proposal import run_proposal
from app.sales.runs import list_sales_runs, save_sales_agent_run
from app.sales.schemas import (
    SalesAgentRunResponse,
    SalesProposalInput,
    SalesProposalReply,
)

AGENT_NAME = "sales"
logger = logging.getLogger(__name__)


def sales_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """Master가 호출하는 Sales port."""
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
                "판매안을 생성했지만 표시할 수 있는 안이 없습니다. 실행 상태를 다시 확인해 주세요."
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
    return SalesProposalInput.model_validate(with_partner_payment_days(data))


def with_partner_payment_days(
    data: Mapping[str, Any],
    *,
    lookup: Callable[[str], int | None] | None = None,
) -> dict[str, Any]:
    """요청이 결제일수를 말하지 않았으면 **거래처 계약 결제일수**를 싣는다.

    ```text
    요청이 결제일수를 들고 있다             → 그대로 둔다 (사람·걷기 규칙이 정한 값이 이긴다)
    계약 이행                               → 그대로 둔다 (계약서가 정본이다)
    갱신인데 이전 계약이 결제일수를 들고 있다 → 그대로 둔다 (계약 상속이 이긴다)
    그 밖, 거래처가 있고 계약 결제일수가 있다 → partners.sales_collection_days 를 싣는다
    거래처 계약값을 못 읽었다                → 그대로 둔다 → 재무가 «결제일수 없음» 으로 닫는다
    ```

    ★ **정본은 거래처 계약이다** (`partners.sales_collection_days`). 화면 입력칸에 30 을
      미리 채워 두거나 코드에 일수를 박으면, 거래처와 7일 결제로 계약을 바꾼 날에도
      판매안은 30일로 선다.

    🔴 **0일은 값이다** — «당일 결제» 라는 정해진 조건이라 `None` 과 가른다.
    """
    user = data.get("user_request")
    if not isinstance(user, Mapping) or user.get("preferred_payment_days") is not None:
        return dict(data)
    mode = data.get("business_mode")
    contract = data.get("contract_context")
    contract = contract if isinstance(contract, Mapping) else {}
    if mode == "CONTRACT_FULFILLMENT":
        return dict(data)
    if mode == "CONTRACT_PROPOSAL_RENEWAL" and contract.get("contract_payment_days") is not None:
        return dict(data)
    partner_id = user.get("partner_id") or contract.get("partner_id")
    if not isinstance(partner_id, str) or not partner_id.strip():
        return dict(data)
    days = (lookup or _partner_contract_payment_days)(partner_id)
    if days is None:
        return dict(data)
    return {**data, "user_request": {**user, "preferred_payment_days": days}}


def _partner_contract_payment_days(partner_id: str) -> int | None:
    """거래처 계약 결제일수. **못 읽으면 `None` 이고 지어내지 않는다.**"""
    try:
        profile = get_partner_profile(partner_id=partner_id)
    except Exception:  # noqa: BLE001 - 못 읽은 계약을 기본값으로 메우지 않는다.
        logger.warning("partner contract payment days unavailable: %s", partner_id)
        return None
    if profile is None:
        return None
    return profile.sales_collection_days


def _proposal_payload(proposal: SalesProposalReply) -> Mapping[str, Any]:
    """봉투에 실을 모양. 🔴 **`by_alias=True` 다** (2026-09-11).

    ★ `SalesScenario.sales_amount_krw` 가 전선에서 `reported_sales_amount_krw` 로
      나가야 재무가 읽는다 (`REQUIRED_SALES_INPUT_FIELDS`). 그 이유는 그 칸 선언에
      적혀 있다 — **재무가 이 값을 다시 세서 맞대 보기 때문**이다.

    🔴 **마스터가 이름을 바꾸지 않는다.** 마스터는 안을 그대로 나르고, 이름의 주인은
       내는 쪽이다. 여기서 안 실으면 마스터가 번역기가 되어야 한다.
    """
    return proposal.model_dump(mode="json", by_alias=True)


def _reasoning(proposal: SalesProposalReply) -> str:
    if proposal.status == "INPUT_INCOMPLETE":
        return "판매안을 만들기 위해 필요한 정보가 부족합니다. 부족한 항목을 확인해 주세요."
    return "판매안을 준비했습니다. 각 안의 수량, 납기, 재무 검토 상태를 확인해 주세요."


def _status_query(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    run_id = _run_id()
    try:
        #  🔴 **거르기 전에 넓게 읽는다.** `list_sales_runs` 에는 실행 축 필터가 없어
        #     5건만 받아 거르면, 그 5건이 전부 남의 실행일 때 «이력이 없습니다» 가 된다.
        #     실제로 2026-01-26 이 그랬다 — 같은 날 다른 실행의 기록이 더 최신이었다.
        runs = list_sales_runs(as_of=request.context.as_of, limit=_STATUS_SCAN_LIMIT)
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

    #  🔴 **이 실행의 이력만 답한다.** `list_sales_runs` 에는 실행 축 필터가 없어
    #     그대로 내면 남의 실행 이력이 «우리 판매 진행 상황» 으로 나간다.
    scoped = [run for run in runs if _run_axis(run) == request.context.sim_run_id][
        :_STATUS_RUN_LIMIT
    ]
    proposals = _today_proposals(request)
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,
        mode=request.mode,
        run_id=str(scoped[0].run_id) if scoped else run_id,
        runtime_status="READY",
        business_status="ok",
        payload={
            "as_of": request.context.as_of.isoformat(),
            **status_facts(proposals, has_history=bool(scoped)),
        },
        reasoning=_status_reasoning(scoped),
    )
    return reply, _metadata(
        request, reply.run_id, tools=("list_sales_runs", "get_console_sales_proposals")
    )


def _today_proposals(request: AgentRequest) -> ConsoleSalesProposalsResponse | None:
    """그날의 판매안과 재무 판정. **못 읽으면 `None` 이다** — 빈 목록으로 바꾸지 않는다."""
    sim_run_id = request.context.sim_run_id
    if not sim_run_id:
        return None
    try:
        return get_console_sales_proposals(sim_run_id=sim_run_id, as_of=request.context.as_of)
    except Exception:  # noqa: BLE001 - 못 읽은 것을 «판매안 없음» 으로 말하지 않는다.
        logger.warning("sales status could not read today's proposals: %s", sim_run_id)
        return None


#: 안의 성격을 사람 말로. 모르는 값은 원문 대신 «판매안» 이다.
_SCENARIO_WORDS = {"CONSERVATIVE": "안정 우선", "BALANCED": "균형", "AGGRESSIVE": "판매 기회 우선"}


def status_facts(
    proposals: ConsoleSalesProposalsResponse | None, *, has_history: bool
) -> dict[str, str]:
    """판매 진행 상황을 **사람이 읽는 사실**로 만든다.

    🔴 **키가 곧 화면 글자다.** 마스터는 부서가 낸 키를 이름 그대로 사실 줄로 편다
       (`master/answer.py` · `_LABEL.get(key, key)`). 그래서 `request_id` ·
       `SCENARIOS_GENERATED` · `FINANCIAL_VALIDATION` 같은 기계용 키와 값을 여기 실으면
       그대로 사용자 말풍선에 나간다 — 실측으로 그렇게 나왔다.

    🔴 **숫자를 지어내지 않는다.** 세는 것은 금일 판매안 read model 이 돌려준 행뿐이고,
       재무 검토 상태는 판매 1차 회신의 «못 받은 검증» 이 아니라 **재무가 남긴 판정**이다.
       (1차 회신은 되먹임 전이라 늘 «재무 검토 미완» 으로 남아, 이미 판정이 난 안까지
       검토 전으로 읽혔다.)

    ★ 비교 · 선택 · 확정은 대화의 판매안 카드와 판매 화면이 한다. 여기서는 요약만 한다.
    """
    if proposals is None:
        return {"오늘 판매안": "판매안 정보를 읽지 못했습니다. 판매 화면에서 다시 확인해 주세요."}
    rows = proposals.rows
    if not rows:
        if proposals.request_count == 0:
            text = (
                "이 날짜에는 판매가 돌지 않았습니다."
                if not has_history
                else "이 날짜에 만든 판매안이 없습니다."
            )
        elif proposals.hidden_zero_quantity > 0:
            text = "팔 수 있는 물량이 없어 판매안이 서지 않았습니다."
        else:
            text = "판매가 돌았지만 판매안을 만들지 못했습니다."
        return {"오늘 판매안": text}

    per_item: dict[str, int] = {}
    for row in rows:
        name = row.item or "품목 미상"
        per_item[name] = per_item.get(name, 0) + 1
    facts: dict[str, str] = {
        "오늘 검토 중인 판매": " · ".join(
            f"{name} 판매안 {count}개" for name, count in per_item.items()
        ),
    }

    facts["재무 검토"] = review_sentence([row.finance_verdict for row in rows])

    need_collection = [
        row
        for row in rows
        if row.required_collection_before_sale_krw is not None
        and row.required_collection_before_sale_krw > 0
    ]
    if need_collection:
        facts["선회수 필요"] = (
            f"{len(need_collection)}개 안은 기존 미수금을 먼저 회수해야 현재 여신한도 안에서 "
            "판매할 수 있습니다"
        )

    recommended = [row for row in rows if row.recommended]
    if recommended:
        facts["추천 판매안"] = " · ".join(
            f"{row.item or '품목 미상'} {_SCENARIO_WORDS.get(row.scenario_type or '', '판매안')}"
            for row in recommended
        )

    confirmed = [row for row in rows if row.sale_status is not None]
    facts["확정된 판매"] = (
        " · ".join(
            f"{row.item or '품목 미상'} {_SCENARIO_WORDS.get(row.scenario_type or '', '판매안')}"
            for row in confirmed
        )
        if confirmed
        else "아직 없습니다"
    )
    return facts


def review_sentence(verdicts: list[str | None]) -> str:
    """재무 검토 상태를 **한 문장으로.** 판정 코드를 세서 고르기만 한다.

    ```text
    모두 진행 어려움            현재 조건으로 바로 진행하기 어려운 판매안이 N개 있습니다
    모두 진행 가능              현재 조건에서 진행 가능한 판매안이 준비되어 있습니다
    확인 필요 · 검토 전이 있다   재무 검토가 필요한 판매안이 있습니다 (+ 진행 가능 N개)
    진행 가능과 어려움만 섞였다  진행 가능한 판매안 N개와 … 어려운 판매안 M개가 있습니다
    ```

    🔴 **모르는 판정은 «확인 필요» 쪽으로 센다** — 통과로 뭉치지 않는다.
    """
    passed = verdicts.count("PASS")
    failed = verdicts.count("FAIL")
    pending = len(verdicts) - passed - failed
    if verdicts and failed == len(verdicts):
        return f"현재 조건으로 바로 진행하기 어려운 판매안이 {failed}개 있습니다"
    if verdicts and passed == len(verdicts):
        return "현재 조건에서 진행 가능한 판매안이 준비되어 있습니다"
    if pending:
        extra = f" (진행 가능한 판매안 {passed}개)" if passed else ""
        return f"재무 검토가 필요한 판매안이 있습니다{extra}"
    return (
        f"진행 가능한 판매안 {passed}개와 "
        f"현재 조건으로 진행하기 어려운 판매안 {failed}개가 있습니다"
    )


#: 실행 축으로 거르기 전에 훑는 범위. 같은 날 여러 실행이 섞여 있어도 우리 것이 남는다.
_STATUS_SCAN_LIMIT = 200

#: 답에 싣는 최대 실행 수. 사람이 읽는 요약이라 길게 낼 이유가 없다.
_STATUS_RUN_LIMIT = 5


def _run_axis(run: SalesAgentRunResponse) -> str | None:
    """이 실행 기록이 어느 실행 축에 속하는지. **봉투 안에 적혀 있다.**"""
    context = run.request_payload.get("context")
    if not isinstance(context, dict):
        return None
    axis = context.get("sim_run_id")
    return None if axis is None else str(axis)


def _status_reasoning(runs: list[SalesAgentRunResponse]) -> str:
    if not runs:
        return "이 실행에는 조회할 판매 판단 이력이 없습니다."
    return (
        f"최근 판매 판단 {len(runs)}건을 조회했습니다. "
        "안의 자세한 내용과 재무 검토 결과는 판매 화면의 금일 판매안에서 볼 수 있습니다."
    )


def _invalid_input(
    request: AgentRequest, run_id: str, exc: ValidationError
) -> tuple[AgentReply, ExecutionMetadata]:
    return _contract_error(
        request,
        run_id,
        payload={
            "validation_errors": [
                ".".join(str(part) for part in item["loc"]) or item["type"] for item in exc.errors()
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
