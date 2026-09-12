"""조사 결과를 **사람이 결정할 수 있는 한 건**으로 바꾸고, 그 결정을 받는다.

```text
InvestigationResult          create_proposal   PROPOSED
        ↓                          ↓
  recommended option      Exception OPEN → PROPOSED   (한 트랜잭션)
        ↓
     사람 ──┬── approve_proposal ──▶ APPROVED
            ├── reject_proposal  ──▶ REJECTED   → 살아 있는 제안 0 이면 Exception 다시 OPEN
            └── expire_proposal  ──▶ EXPIRED    → 〃
```

🔴 **`APPROVED` 는 `EXECUTED` 가 아니다.** *"이 대응안으로 진행해도 좋다"* 는 사람의
   결정일 뿐이고, 승인 뒤에도 재고·판매·매입·용량은 **한 줄도 안 바뀐다.** 실제 실행은
   Commit 6 이다. 이 파일에서 `inventory_*` · `sales` · 매입 표를 쓰는 SQL 은 하나도 없다.

🔴 **LLM 이 여기 없다.** 승인·거절은 사람이 이미 내린 결정이다 — *"이 승인 괜찮을까요"*
   를 모델에게 되묻지 않고, 모델이 승인자 이름이나 거절 사유를 짓지도 않는다 (§35).

🔴 **트랜잭션의 주인이 이 파일이다.** `proposals.py` 는 커밋도 롤백도 안 한다고
   못박았고(`agent/exceptions.py` ↔ `master/inspection.py` 와 같은 나눔) 그 약속의
   반대편이 여기다. **제안 INSERT 와 Exception UPDATE 는 함께 성공하거나 함께 없다.**

⚠️ **숫자를 여기서 만들지 않는다.** `candidate_kg` · `capacity_delta_kg` ·
   `estimated_loss_krw` · `feasibility` 는 Commit 3 의 `estimate_action_impact` 가 이미
   낸 값이고, 제안은 그것을 **옮기기만** 한다 (§51).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import ValidationError

from app.logistics.agent import proposals as repository
from app.logistics.agent.investigation import (
    ACTION_DECISION_OWNERS,
    EvaluatedOption,
    InvestigationResult,
    jsonable,
)
from app.logistics.agent.proposals import (
    ProposalRow,
    project_proposal_at,
    proposal_key_for,
)
from app.logistics.agent.schemas import LIVE_STATUSES as LIVE_EXCEPTION_STATUSES
from app.logistics.agent.tool_dispatch import ActionImpactArguments
from app.logistics.agent.tools import SUPPORTED_ACTIONS

__all__ = [
    "ACTION_UNSUPPORTED",
    "EXCEPTION_MISMATCH",
    "EXCEPTION_NOT_FOUND",
    "EXCEPTION_NOT_LIVE",
    "IMPACT_INFEASIBLE",
    "IMPACT_MISSING",
    "LIVE_PROPOSAL_EXISTS",
    "NO_RECOMMENDED_OPTION",
    "OPTION_REJECTED",
    "PARAMETERS_INVALID",
    "ProposalNotFound",
    "ProposalOutcome",
    "ProposalStateConflict",
    "approve_proposal",
    "create_proposal",
    "expire_proposal",
    "get_proposal",
    "list_proposals",
    "proposal_payload",
    "reject_proposal",
    "select_proposal_option",
]


# ══════════════════════════════════════════════════════════════════════════
#  «제안을 안 만든 이유» 의 어휘 — 🔴 조용히 0 건으로 끝내지 않는다
# ══════════════════════════════════════════════════════════════════════════

#: 조사가 추천할 대안을 하나도 못 골랐다 (`recommended_index is None`).
NO_RECOMMENDED_OPTION = "NO_RECOMMENDED_OPTION"
#: 추천 번호가 후보 목록 밖이다.
RECOMMENDED_INDEX_OUT_OF_RANGE = "RECOMMENDED_INDEX_OUT_OF_RANGE"
#: 추천된 후보가 이미 결정론에 의해 버려진 것이다 (§15).
OPTION_REJECTED = "OPTION_REJECTED"
#: 카탈로그 밖 행동이다 (§10.1).
ACTION_UNSUPPORTED = "ACTION_UNSUPPORTED"
#: 영향을 못 쟀다 — `estimate_action_impact` 를 아예 못 불렀다.
IMPACT_MISSING = "IMPACT_MISSING"
#: 결정론 계산기가 **«불가»** 라고 답했다. 🔴 «못 쟀다»(UNRESOLVED) 와 다르다.
IMPACT_INFEASIBLE = "IMPACT_INFEASIBLE"
#: 인자가 행동 스키마를 못 지난다 (§50 — Commit 4 의 모델을 그대로 쓴다).
PARAMETERS_INVALID = "PARAMETERS_INVALID"
#: 그 Exception 이 이 실행에 없다.
EXCEPTION_NOT_FOUND = "EXCEPTION_NOT_FOUND"
#: 조사 결과가 가리키는 Exception 과 요청이 다르다.
EXCEPTION_MISMATCH = "EXCEPTION_MISMATCH"
#: 이미 닫힌(RESOLVED · DISMISSED) 문제에는 대응안을 안 세운다.
EXCEPTION_NOT_LIVE = "EXCEPTION_NOT_LIVE"
#: 같은 문제에 살아 있는 대응안이 **이미 있다** (§47 · §48).
LIVE_PROPOSAL_EXISTS = "LIVE_PROPOSAL_EXISTS"


class ProposalNotFound(LookupError):
    """그 실행에 그 제안이 없다."""


class ProposalStateConflict(RuntimeError):
    """지금 상태에서 할 수 없는 결정이다.

    ```text
    ALREADY_APPROVED   이미 승인됐다 — 다른 사람이거나 다른 날이다
    ALREADY_REJECTED   이미 거절됐다 — 〃
    ALREADY_EXPIRED    이미 만료됐다 — 〃
    STATE_CONFLICT     그 밖 (거절된 것을 승인하려는 등)
    ```

    ★ **같은 사람이 같은 날 같은 내용으로** 다시 부른 것은 충돌이 아니라 **재시도**다 —
      그때는 예외가 아니라 기존 행이 그대로 돌아온다 (§38 · §39).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


OutcomeStatus = Literal["CREATED", "REUSED", "SKIPPED"]


@dataclass(frozen=True)
class ProposalOutcome:
    """제안을 만들려 한 결과. **예외 대신 이것을 돌려준다.**

    ```text
    CREATED   새 행이 섰다
    REUSED    같은 뜻의 살아 있는 제안이 이미 있었다 — 재시도다 (§12)
    SKIPPED   안 만들었다. 왜 안 만들었는지는 `reason` 에 있다
    ```

    🔴 **`SKIPPED` 를 «실패» 로 읽지 않는다.** 추천할 대안이 없는 조사는 정상이고, 그때
       제안 0 건은 **옳은 답**이다 (§53). 다만 *"왜 0 건인가"* 를 못 대면 안 된다 —
       `InspectionOut.NOTHING_DUE` 와 `FAILED` 를 안 접는 것과 같은 규율이다.
    """

    status: OutcomeStatus
    reason: str = ""
    proposal: ProposalRow | None = None

    @property
    def created(self) -> bool:
        return self.status == "CREATED"


# ══════════════════════════════════════════════════════════════════════════
#  어떤 후보가 제안이 되나 — **순수 함수다. DB 를 안 본다**
# ══════════════════════════════════════════════════════════════════════════


def select_proposal_option(
    result: InvestigationResult,
) -> tuple[EvaluatedOption | None, str]:
    """조사 결과에서 저장할 후보 하나. 없으면 `(None, 사유)`.

    ```text
    recommended_index 가 없다            → 제안 0 건
    추천 후보가 결정론에 버려졌다          → 제안 0 건 (§15)
    카탈로그 밖 행동이다                  → 제안 0 건
    영향을 못 불렀다                      → 제안 0 건
    결정론이 «불가» 라고 답했다            → 제안 0 건
    영향이 UNRESOLVED 다                 → ✅ **제안한다** (§15)
    ```

    🔴 **«못 쟀다» 와 «불가» 를 가른다.** `UNRESOLVED` 는 입력이 모자라 못 쟀다는
       뜻이고, 그 사실을 그대로 실어 사람에게 보이는 것이 정직하다 — 숫자를 지어내
       `FEASIBLE` 로 바꾸지 않는다. 반대로 `INFEASIBLE` 은 계산기가 **재 보고 «안
       된다»** 고 답한 것이다. 그것을 승인 화면에 올리면 사람이 불가능한 일을 승인한다.

    ⚠️ **두 번째 후보로 미끄러지지 않는다.** 추천이 버려졌으면 제안 0 건이다 —
       조사가 고르지 않은 안을 저장이 대신 고르면, 그 선택의 주인이 아무도 아니게 된다.
    """
    index = result.recommended_index
    if index is None:
        return None, NO_RECOMMENDED_OPTION
    if not 0 <= index < len(result.options):
        return None, f"{RECOMMENDED_INDEX_OUT_OF_RANGE}:{index}"

    option = result.options[index]
    if not option.accepted:
        return None, f"{OPTION_REJECTED}:{option.rejected_reason}"
    if option.action not in SUPPORTED_ACTIONS:
        return None, f"{ACTION_UNSUPPORTED}:{option.action}"
    if option.impact is None:
        return None, IMPACT_MISSING
    if option.impact.feasibility == "UNSUPPORTED":
        return None, f"{ACTION_UNSUPPORTED}:{option.action}"
    if option.impact.feasibility == "INFEASIBLE":
        return None, IMPACT_INFEASIBLE
    return option, ""


def _evidence_snapshot(result: InvestigationResult, option: EvaluatedOption) -> list[Any]:
    """인용된 근거를 **실제 Tool 호출 한 벌로** 남긴다.

    🔴 **번호만 적지 않는다.** `evidence_refs` 는 그 조사 안의 순번인데 조사 자체가 DB 에
       안 남으므로(§44), 번호만 저장하면 나중에 *"왜 이 제안이 나왔나"* 를 되짚을 수
       없다 — 가리킬 곳이 없는 포인터가 된다 (§43).
    """
    cited = set(option.evidence_refs)
    return [
        {
            "sequence": record.sequence,
            "tool_name": record.tool_name,
            "arguments": jsonable(dict(record.arguments)),
            "observed_as_of": jsonable(record.observed_as_of),
        }
        for record in result.tool_calls
        if record.sequence in cited
    ]


# ══════════════════════════════════════════════════════════════════════════
#  생성 — 🔴 제안 INSERT 와 Exception UPDATE 는 **함께** 성공한다
# ══════════════════════════════════════════════════════════════════════════


def create_proposal(
    conn: Any,
    *,
    result: InvestigationResult,
    as_of: date,
    proposed_by: str,
    investigation_id: str | None = None,
    supersedes: str | None = None,
) -> ProposalOutcome:
    """조사 결과 하나를 **저장 가능한 대응안 한 건**으로 바꾼다.

    ```text
    ① 어떤 후보인가        select_proposal_option (순수)
    ② 누가 결정하나        🔴 ACTION_DECISION_OWNERS 에서 **다시 계산** (§49)
    ③ 인자가 맞나          Commit 4 의 argument model 그대로 (§50)
    ④ 문제가 살아 있나      RESOLVED · DISMISSED 에는 안 세운다
    ⑤ 이미 있나            같은 지문이면 재시도 · 다른 안이면 안 세운다 (§47 · §48)
    ⑥ 한 트랜잭션          INSERT + Exception OPEN→PROPOSED  ← 반쪽 상태를 안 남긴다
    ```

    🔴 **`decision_owner` 를 모델 값으로 저장하지 않는다.** 누가 결정하는가는 역할 경계
       (§9.1)이고, 모델이 *"이건 물류가 정하면 됩니다"* 라고 적어도 그 말이 표에 실리면
       안 된다.

    🔴 **`observed_as_of` 는 조사 값 그대로 옮긴다.** `None` 이면 `None` 이다 — 제안일 ·
       승인일 · 현재시각으로 메우지 않는다 (§22).

    :param as_of: 시뮬레이션 영업일. 🔴 `date.today()` 를 안 쓴다 (§20).
    :param supersedes: 이 제안이 **대체할** 살아 있는 제안 (§29). 주면 그것을 먼저
        `SUPERSEDED` 로 닫고 새 행이 `previous_proposal_id` 로 가리킨다.
        ⚠️ **자동 대체는 없다** — 부르는 쪽이 명시할 때만 일어난다.
    :returns: 만들었나 · 재시도였나 · 안 만들었나 + 그 사유. 🔴 **예외를 안 쓴다.**
    """
    if not proposed_by.strip():
        raise ValueError("proposed_by 가 비었다 — 누가 올렸는지 못 대는 제안은 만들지 않는다")
    if as_of != result.as_of:
        raise ValueError(
            f"as_of 가 조사와 다르다 (요청 {as_of} · 조사 {result.as_of}) —"
            " 다른 영업일의 조사로 오늘 제안을 세우지 않는다"
        )

    option, reason = select_proposal_option(result)
    if option is None:
        return ProposalOutcome(status="SKIPPED", reason=reason)

    # 🔴 모델이 적어 준 소유 부서를 안 믿는다. 카탈로그가 정한다.
    owner = ACTION_DECISION_OWNERS.get(option.action)
    if owner is None:
        return ProposalOutcome(
            status="SKIPPED", reason=f"{ACTION_UNSUPPORTED}:{option.action}"
        )

    try:
        # ★ 새 스키마를 만들지 않는다 — guard 가 쓰는 그 모델이다 (§50).
        checked = ActionImpactArguments.model_validate(
            {"action": option.action, "parameters": dict(option.parameters)}
        )
    except ValidationError as error:
        return ProposalOutcome(
            status="SKIPPED", reason=f"{PARAMETERS_INVALID}:{error.error_count()}건"
        )
    parameters = checked.parameters

    status = repository.exception_status(
        conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id
    )
    if status is None:
        return ProposalOutcome(
            status="SKIPPED", reason=f"{EXCEPTION_NOT_FOUND}:{result.exception_id}"
        )
    if status not in LIVE_EXCEPTION_STATUSES:
        # 🔴 Exception 의 «살아 있다» 는 OPEN·PROPOSED 다 — 제안의 것(PROPOSED·APPROVED)과
        #    **값이 다르다.** 두 상태기계를 섞으면 RESOLVED 된 문제에 제안이 선다.
        return ProposalOutcome(status="SKIPPED", reason=f"{EXCEPTION_NOT_LIVE}:{status}")

    key = proposal_key_for(
        sim_run_id=result.sim_run_id,
        exception_id=result.exception_id,
        action_type=option.action,
        parameters=parameters,
    )
    live = repository.live_proposals_for(
        conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id
    )
    for existing in live:
        if existing.proposal_id == supersedes:
            continue
        if existing.proposal_key == key:
            # 같은 뜻의 제안이 이미 서 있다 — 네트워크 재시도다. 새 행을 안 만든다.
            return ProposalOutcome(status="REUSED", reason="", proposal=existing)
        return ProposalOutcome(
            status="SKIPPED", reason=f"{LIVE_PROPOSAL_EXISTS}:{existing.proposal_id}"
        )

    row = ProposalRow(
        proposal_id=repository.next_proposal_id(conn, exception_id=result.exception_id),
        sim_run_id=result.sim_run_id,
        exception_id=result.exception_id,
        investigation_id=investigation_id,
        proposal_key=key,
        status="PROPOSED",
        action_type=option.action,
        decision_owner=owner,
        parameters=jsonable(parameters),
        impact=jsonable(option.impact),
        evidence_refs=tuple(_evidence_snapshot(result, option)),
        rationale=option.rationale,
        source_finish_reason=result.finish_reason.value,
        source_llm_status=result.llm_status,
        proposed_as_of=as_of,
        proposed_by=proposed_by,
        # 🔴 조사가 낸 값 그대로. 여기서 만들지 않는다.
        observed_as_of=result.observed_as_of,
        previous_proposal_id=supersedes,
    )

    try:
        if supersedes is not None:
            _supersede(conn, sim_run_id=result.sim_run_id, proposal_id=supersedes, as_of=as_of)
        repository.insert_proposal(conn, row=row)
        moved = repository.mark_exception_proposed(
            conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id, as_of=as_of
        )
        if moved != 1:
            # 🔴 읽고 나서 쓰는 사이에 누가 문제를 닫았다. **제안만 남기지 않는다.**
            raise ProposalStateConflict(
                "STATE_CONFLICT",
                f"{result.exception_id} 가 제안을 세우는 사이에 살아 있는 상태가 아니게 됐다",
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ProposalOutcome(status="CREATED", proposal=row)


def _supersede(conn: Any, *, sim_run_id: str, proposal_id: str, as_of: date) -> None:
    """이전 제안을 닫아 자리를 비운다. **새 제안이 서기 전에** 해야 한다.

    ⚠️ 부분 유일 인덱스가 «한 문제에 살아 있는 제안 하나» 를 강제하므로, 닫지 않고
       새 행을 넣으면 DB 가 막는다.
    """
    closed = repository.transition_proposal(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="SUPERSEDED",
        as_of=as_of,
        from_status="PROPOSED",
    )
    if closed != 1:
        raise ProposalStateConflict(
            "STATE_CONFLICT", f"{proposal_id} 는 지금 PROPOSED 가 아니어서 대체할 수 없다"
        )


# ══════════════════════════════════════════════════════════════════════════
#  사람의 결정 — 🔴 LLM 을 부르지 않는다
# ══════════════════════════════════════════════════════════════════════════


def approve_proposal(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    as_of: date,
    approved_by: str,
    note: str | None = None,
) -> ProposalRow:
    """사람이 **«진행해도 좋다»** 고 정했다. 🔴 **실행이 아니다.**

    ```text
    APPROVED  =  이 대응안을 진행해도 좋다는 사람의 결정
    ≠            판매가 일어났다 · 매입 일정이 바뀌었다 · 재고가 폐기됐다
    ```

    🔴 **Exception 을 닫지 않는다** (§26 · §28). 아직 아무 행동도 안 했고, Exception 은
       *문제* 의 상태이지 *대응* 의 상태가 아니다 — 두 상태기계를 섞지 않는다.
       (`logistics_exceptions.status` 에 `APPROVED` 라는 값이 애초에 없다.)

    🔴 **타 부서를 부르지 않는다.** `SALES_PRIORITY_REQUEST` 승인은 *"영업에 우선판매를
       요청해도 좋다"* 까지다 — Sales · Purchase 모듈 호출은 Commit 6 이다.
    """
    return _decide(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="APPROVED",
        as_of=as_of,
        actor=approved_by,
        note=note,
        # ⚠️ 승인은 Exception 을 안 건드린다 — 되돌릴 것도 없다.
        reopen_exception=False,
    )


def reject_proposal(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    as_of: date,
    rejected_by: str,
    rejection_reason: str,
) -> ProposalRow:
    """사람이 **«이 안은 아니다»** 라고 정했다.

    🔴 **사유가 비면 안 받는다.** *"왜 아닌가"* 가 없으면 다음 조사가 같은 안을 또 올린다.

    ★ 거절 뒤 그 문제에 살아 있는 대응안이 하나도 안 남으면 Exception 은 다시
      `OPEN` 이다 (§27) — *"문제는 그대로인데 대응안이 없다"* 가 정확한 상태다.
      ⚠️ 다른 살아 있는 제안이 있으면 `PROPOSED` 를 유지한다.
    """
    if not rejection_reason.strip():
        raise ValueError("rejection_reason 이 비었다 — 사유 없는 거절은 다음 조사에 안 남는다")
    return _decide(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="REJECTED",
        as_of=as_of,
        actor=rejected_by,
        note=rejection_reason,
        reopen_exception=True,
    )


def expire_proposal(
    conn: Any, *, sim_run_id: str, proposal_id: str, as_of: date
) -> ProposalRow:
    """기한이 지나 **사람이 볼 필요가 없어졌다.**

    🔴 **시간 기반 자동 작업이 아니다** (§30). 부르는 쪽이 «오늘은 며칠» 을 들고 와서
       명시적으로 닫는다 — Scheduler 에 붙는 만료 task 는 이번 Commit 에 없다.

    ⚠️ 행위자 칸이 없다. 사람이 내린 결정이 아니라 기한 경과이고, 없는 사람 이름을
       지어내 적는 것보다 **비워 두는 것**이 정직하다.
    """
    return _decide(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="EXPIRED",
        as_of=as_of,
        actor=None,
        note=None,
        reopen_exception=True,
    )


def _decide(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    to_status: str,
    as_of: date,
    actor: str | None,
    note: str | None,
    reopen_exception: bool,
) -> ProposalRow:
    """결정 하나. **세 갈래를 가린다: 재시도 · 충돌 · 정상.**

    ```text
    이미 그 상태 + 같은 사람 + 같은 날   →  기존 행 그대로 (재시도 · §38 · §39)
    이미 그 상태 + 다른 사람/다른 날      →  ALREADY_{상태}
    아예 다른 상태                        →  STATE_CONFLICT
    PROPOSED                              →  옮긴다
    ```

    🔴 **옮기는 UPDATE 에 `AND status = 'PROPOSED'` 가 있다** (§37). 두 사람이 동시에
       눌러도 한 번만 먹고, 늦은 쪽은 `0` 을 받아 위의 세 갈래로 다시 내려간다 —
       읽고 나서 쓰는 사이의 lost update 를 응용 코드로는 못 막는다.
    """
    if actor is not None and not actor.strip():
        raise ValueError("결정한 사람이 비었다 — 빈 문자열은 «모른다» 를 «있다» 로 위장한다")

    row = repository.select_proposal(conn, sim_run_id=sim_run_id, proposal_id=proposal_id)
    if row is None:
        raise ProposalNotFound(f"{sim_run_id} 에 {proposal_id} 가 없다")
    if as_of < row.proposed_as_of:
        raise ValueError(
            f"as_of({as_of}) 가 제안일({row.proposed_as_of}) 보다 앞선다 —"
            " 제안되기 전에 결정할 수 없다"
        )

    settled = _settled(row, to_status=to_status, as_of=as_of, actor=actor, note=note)
    if settled is not None:
        return settled
    if row.status != "PROPOSED":
        raise _conflict(row, to_status=to_status)

    try:
        changed = repository.transition_proposal(
            conn,
            sim_run_id=sim_run_id,
            proposal_id=proposal_id,
            to_status=to_status,
            as_of=as_of,
            from_status="PROPOSED",
            actor=actor,
            note=note,
        )
        if changed == 0:
            # 🔴 읽고 나서 쓰는 사이에 남이 먼저 옮겼다. 바뀐 행이 0 이라 되돌릴
            #    것은 없고, 다시 읽어 **재시도인지 충돌인지** 를 가린다.
            fresh = repository.select_proposal(
                conn, sim_run_id=sim_run_id, proposal_id=proposal_id
            )
            if fresh is None:
                raise ProposalNotFound(f"{sim_run_id} 에 {proposal_id} 가 없다")
            raced = _settled(fresh, to_status=to_status, as_of=as_of, actor=actor, note=note)
            if raced is not None:
                return raced
            raise _conflict(fresh, to_status=to_status)

        if reopen_exception:
            _reopen_if_nothing_lives(conn, sim_run_id=sim_run_id, exception_id=row.exception_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    decided = repository.select_proposal(conn, sim_run_id=sim_run_id, proposal_id=proposal_id)
    if decided is None:  # pragma: no cover - 방금 1 행을 바꾸고 커밋했다.
        raise ProposalNotFound(f"{sim_run_id} 에 {proposal_id} 가 없다")
    return decided


def _reopen_if_nothing_lives(conn: Any, *, sim_run_id: str, exception_id: str) -> None:
    """살아 있는 대응안이 0 이면 Exception 을 다시 `OPEN` 으로 (§27).

    ⚠️ **`RESOLVED` · `DISMISSED` 를 되살리지 않는다** — 재탐지가 이미 닫은 문제를 제안
       거절이 다시 열면, 조건이 사라진 문제가 장부에 남는다. 그 방어는
       `reopen_exception` 의 `WHERE status = 'PROPOSED'` 에 있다.
    """
    if repository.live_proposals_for(conn, sim_run_id=sim_run_id, exception_id=exception_id):
        return
    repository.reopen_exception(conn, sim_run_id=sim_run_id, exception_id=exception_id)


def _settled(
    row: ProposalRow, *, to_status: str, as_of: date, actor: str | None, note: str | None
) -> ProposalRow | None:
    """**같은 결정을 한 번 더 보낸 것**인가. 아니면 `None`.

    ★ 네트워크가 끊겨 같은 요청이 두 번 들어오는 일은 흔하다. 그때 두 번째를 충돌로
      돌려보내면 사람은 *"승인이 안 됐나"* 하고 또 누른다.
    """
    if row.status != to_status:
        return None
    dates = {
        "APPROVED": row.approved_as_of,
        "REJECTED": row.rejected_as_of,
        "EXPIRED": row.expired_as_of,
        "SUPERSEDED": row.superseded_as_of,
    }
    if dates.get(to_status) != as_of:
        return None
    if to_status == "APPROVED" and row.approved_by != actor:
        return None
    if to_status == "REJECTED" and (row.rejected_by != actor or row.rejection_reason != note):
        return None
    return row


def _conflict(row: ProposalRow, *, to_status: str) -> ProposalStateConflict:
    """🔴 **이미 끝난 결정을 뒤집지 않는다** (§6 · §58).

    ⚠️ 어휘가 둘이다: 이미 그 상태면 `ALREADY_{상태}`(다른 사람 · 다른 날이 이미 정했다),
       그 밖이면 `STATE_CONFLICT`(거절된 것을 승인하려는 등). 둘 다 같은 예외 종류다.
    """
    if row.status == to_status:
        return ProposalStateConflict(
            f"ALREADY_{to_status}",
            f"{row.proposal_id} 는 이미 {to_status} 다 — 다른 사람이거나 다른 날의 결정이다",
        )
    return ProposalStateConflict(
        "STATE_CONFLICT",
        f"{row.proposal_id} 는 {row.status} 라 {to_status} 로 갈 수 없다",
    )


# ══════════════════════════════════════════════════════════════════════════
#  조회 — 🔴 **그날** 값이다
# ══════════════════════════════════════════════════════════════════════════


def get_proposal(
    conn: Any, *, sim_run_id: str, proposal_id: str, as_of: date
) -> ProposalRow | None:
    """**그날** 이 제안. 그날 아직 없었으면 `None`.

    🔴 지금 상태를 과거로 쓰지 않고, 미래 detail 도 안 흘린다 (§41 · §42).
    """
    row = repository.select_proposal(conn, sim_run_id=sim_run_id, proposal_id=proposal_id)
    return None if row is None else project_proposal_at(row, as_of=as_of)


def list_proposals(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    exception_id: str | None = None,
    statuses: Sequence[str] | None = None,
) -> tuple[ProposalRow, ...]:
    """**그날** 살아 있던(=이미 제안된) 대응안들.

    :param statuses: **그날 상태** 로 거른다 — 지금 상태가 아니다. 오늘 거절된 제안도
        D6 조회에서는 `PROPOSED` 다.
    """
    wanted = None if statuses is None else set(statuses)
    projected: list[ProposalRow] = []
    for row in repository.select_proposals(
        conn, sim_run_id=sim_run_id, exception_id=exception_id
    ):
        at_date = project_proposal_at(row, as_of=as_of)
        if at_date is None:
            continue
        if wanted is not None and at_date.status not in wanted:
            continue
        projected.append(at_date)
    return tuple(projected)


def proposal_payload(row: ProposalRow) -> Mapping[str, Any]:
    """전선에 실을 수 있는 모양 하나. **여기서 값을 바꾸지 않는다.**

    ★ Commit 7 의 API 가 자기 직렬화를 따로 짜다가 `Decimal` 을 `float` 으로 낮추는 일을
      막으려고 미리 한 자리에 둔다 — `jsonable` 이 그것을 문자열로 낸다.
    """
    return jsonable(row)
