"""`logistics_action_proposals` 한 표만 읽고 쓴다. **업무 판단이 여기 없다.**

```text
읽기   select_proposal          한 건 (지금 값 그대로)
       select_proposals         목록
       live_proposals_for       그 문제에 **살아 있는**(PROPOSED·APPROVED) 대응안
       project_proposal_at      🔴 **그날** 상태로 접는다 — 순수 함수 · DB 안 본다
쓰기   insert_proposal          INSERT (status=PROPOSED)
       transition_proposal      UPDATE ... WHERE status = {지금 상태}  ← 동시성 방어
       mark_exception_proposed  Exception OPEN → PROPOSED
       reopen_exception         Exception PROPOSED → OPEN
```

🔴 **커밋도 롤백도 안 한다.** 트랜잭션의 주인은 `proposal_service` 다
   (`agent/exceptions.py` ↔ `master/inspection.py` 와 같은 나눔).

🔴 **상태를 여기서 정하지 않는다.** 무엇을 승인하고 무엇을 거절할지는 서비스가 정하고,
   이 파일은 그 결정을 표에 옮기기만 한다. 다만 *"지금 그 상태가 맞을 때만 옮긴다"* 는
   조건은 **SQL 안에** 있다 — 두 사람이 동시에 승인을 눌러도 한 번만 먹는 자리가
   여기이기 때문이다 (§37).

⚠️ **`created_at` · `updated_at` 을 `ProposalRow` 에 안 싣는다.** 감사용 벽시계이고
   업무 판단에 쓰면 안 되는 값이라, 애초에 손에 안 잡히게 둔다 (`ExceptionRow` 와 같다).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal

from psycopg import errors as pg_errors
from psycopg import sql

from app.logistics.agent.investigation import jsonable
from app.logistics.db import get_db_schema

__all__ = [
    "DECISION_DETAIL_COLUMNS",
    "LIVE_PROPOSAL_STATUSES",
    "PROPOSAL_STATUSES",
    "TERMINAL_DATE_STATUSES",
    "ProposalInvariantViolation",
    "ProposalRow",
    "ProposalStatus",
    "accept_exception_risk",
    "exception_risk_accepted_as_of",
    "exception_status",
    "insert_proposal",
    "is_unique_violation",
    "latest_terminal_as_of",
    "live_proposals_for",
    "mark_exception_proposed",
    "next_proposal_id",
    "project_proposal_at",
    "proposal_key_for",
    "record_execution_outcome",
    "reopen_exception",
    "select_proposal",
    "select_proposals",
    "transition_proposal",
]


# ══════════════════════════════════════════════════════════════════════════
#  상태 어휘 — DB CHECK 와 **같은 목록이어야 한다**
# ══════════════════════════════════════════════════════════════════════════

ProposalStatus = Literal[
    "PROPOSED",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "SUPERSEDED",
    "EXECUTED",
    "FAILED",
]

#: ⚠️ `EXECUTED` · `FAILED` 는 **어휘로만** 있다 — Commit 5 의 Production 코드는 이 둘로
#:    전이시키지 않는다. 실행은 Commit 6 이다.
PROPOSAL_STATUSES: tuple[str, ...] = (
    "PROPOSED",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "SUPERSEDED",
    "EXECUTED",
    "FAILED",
)

#: 🔴 **«살아 있다» 에 `APPROVED` 가 들어간다.** 승인은 끝이 아니라 실행 대기다 —
#:    여기서 빼면 같은 문제에 승인 대기 제안과 새 제안이 동시에 서게 된다 (§47).
LIVE_PROPOSAL_STATUSES: tuple[str, ...] = ("PROPOSED", "APPROVED")

#: 🔴 **끝난 날 칸 → 그 날이 뜻하는 상태.** 여기 `approved_as_of` 가 **없다** (Commit 6) —
#:    승인은 끝이 아니라 **실행 대기**다. 이 사전은 «이 제안이 언제 살아 있기를 그쳤나» 를
#:    답하고, 그것이 다음 제안의 날짜 하한이 된다 (`latest_terminal_as_of`).
TERMINAL_DATE_STATUSES: Mapping[str, str] = {
    "rejected_as_of": "REJECTED",
    "expired_as_of": "EXPIRED",
    "superseded_as_of": "SUPERSEDED",
    "executed_as_of": "EXECUTED",
    "failed_as_of": "FAILED",
}

#: 🔴 **과거 재현의 우선순위다 — 순서가 곧 규칙이다** (§20).
#:
#: ```text
#: D5 제안 · D6 승인 · D8 실행   →  D5 PROPOSED · D7 APPROVED · D9 EXECUTED
#: ```
#:
#: ⚠️ `EXECUTED` 가 `APPROVED` 보다 **앞에** 있다. 실행된 행은 승인일도 함께 들고 있어서
#:    (`executed_as_of >= approved_as_of`) 둘 다 «도착» 하는데, 그날 상태는 나중 것이다.
_STATUS_PRECEDENCE: tuple[tuple[str, str], ...] = (
    ("EXECUTED", "executed_as_of"),
    ("FAILED", "failed_as_of"),
    ("APPROVED", "approved_as_of"),
    ("REJECTED", "rejected_as_of"),
    ("EXPIRED", "expired_as_of"),
    ("SUPERSEDED", "superseded_as_of"),
)

#: 한 제안에 **하나만** 올 수 있는 결정들. 승인과 실행은 서로 다른 축이라 여기 안 묶인다.
_EXCLUSIVE_DECISIONS: frozenset[str] = frozenset({"APPROVED", "REJECTED", "EXPIRED", "SUPERSEDED"})

#: 상태마다 «그 결정이 남긴 칸». 🔴 **그날 안 일어난 결정의 칸은 비워서 낸다** (§42) —
#:    D10 의 거절 사유가 D6 조회에 보이면 그날 없던 사실이 과거에 생긴다.
DECISION_DETAIL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "APPROVED": ("approved_as_of", "approved_by", "approval_note"),
    "REJECTED": ("rejected_as_of", "rejected_by", "rejection_reason"),
    "EXPIRED": ("expired_as_of",),
    "SUPERSEDED": ("superseded_as_of",),
    # Commit 6 — 실행도 «그날 안 일어났으면 안 보인다».
    "EXECUTED": ("executed_as_of", "executed_by", "execution_result"),
    # 🔴 `executed_by` 가 **양쪽에** 있다. 실패에도 «돌리려 한 주체» 가 적히므로
    #    실패 쪽에서 빠뜨리면 D8 의 사람이 D7 조회에 보인다 (future detail leak).
    "FAILED": ("failed_as_of", "executed_by", "failure_code", "failure_reason"),
}

#: 비울 때 넣는 값. 🔴 `execution_result` 만 `None` 이 아니라 **빈 사전**이다 — 그 칸의
#:    타입이 «지도» 이고, 비었다는 것과 없다는 것을 굳이 가를 이유가 없다.
_BLANK_VALUES: Mapping[str, Any] = {"execution_result": {}}


class ProposalInvariantViolation(RuntimeError):
    """그날 상태를 **못 고른다.** 🔴 하나를 골라 답하지 않고 멈춘다 (fail-closed).

    ```text
    끝난 날이 둘           DB CHECK 가 막지만, 손으로 고친 행이 있을 수 있다
    EXECUTED/FAILED 인데   `executed_as_of`/`failed_as_of` 가 비었다 — 칸은 Commit 6 에
    그 날이 없다           섰고 production 에서는 DB CHECK 가 막는다
    ```

    ★ 추측해서 답하면 *"그날 이 제안은 승인 상태였다"* 는 **거짓 사실**이 조회에 실린다.
    """


# ══════════════════════════════════════════════════════════════════════════
#  행 — 칸 이름이 DB 와 같다
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, kw_only=True)
class ProposalRow:
    """`logistics_action_proposals` 한 행.

    🔴 **`impact` 를 여기서 셈하지 않는다.** Commit 3 의 `estimate_action_impact` 가 낸
       답을 그대로 들고 있는 칸이다 (§51).
    """

    proposal_id: str
    sim_run_id: str
    exception_id: str
    status: str
    action_type: str
    #: 🔴 `ACTION_DECISION_OWNERS` 가 정한다 — 모델이 고른 값을 저장하지 않는다 (§49).
    decision_owner: str
    parameters: Mapping[str, Any]
    impact: Mapping[str, Any]
    evidence_refs: tuple[Mapping[str, Any], ...]
    proposed_as_of: date
    proposed_by: str
    #: 조사가 낸 값 그대로. `None` 이면 `None` 이다 (§22).
    observed_as_of: date | None = None
    proposal_key: str = ""
    #: 🔴 이 제안을 낸 조사 (`logistics_investigations` · Commit 7).
    #: ⚠️ 손으로 세운 제안에는 가리킬 조사가 없어서 **여전히 nullable 이다.**
    investigation_id: str | None = None
    rationale: str = ""
    source_finish_reason: str | None = None
    source_llm_status: str | None = None
    approved_as_of: date | None = None
    rejected_as_of: date | None = None
    expired_as_of: date | None = None
    superseded_as_of: date | None = None
    approved_by: str | None = None
    rejected_by: str | None = None
    approval_note: str | None = None
    rejection_reason: str | None = None
    previous_proposal_id: str | None = None
    # ── 실행 축 (Commit 6) ───────────────────────────────────────────
    #: 🔴 `approved_as_of` 와 **함께** 선다 — 승인은 끝이 아니라 실행 대기다.
    executed_as_of: date | None = None
    failed_as_of: date | None = None
    #: ⚠️ `decision_owner`(실행 책임 부서) 와 다른 축 — 실제로 돌린 주체다.
    executed_by: str | None = None
    #: 무엇을 실행했고 어느 정본 행을 가리키나. 🔴 업무 표 snapshot 이 아니다.
    execution_result: Mapping[str, Any] = field(default_factory=dict)
    #: 🔴 **확정된 실패만.** «모른다» 를 여기 적지 않는다.
    failure_code: str | None = None
    failure_reason: str | None = None

    @property
    def live(self) -> bool:
        """실행 대기 중인가. ⚠️ `APPROVED` 도 참이다 — 승인은 끝이 아니다."""
        return self.status in LIVE_PROPOSAL_STATUSES


_COLUMNS = (
    "proposal_id",
    "sim_run_id",
    "exception_id",
    "investigation_id",
    "proposal_key",
    "action_type",
    "decision_owner",
    "parameters_json",
    "impact_json",
    "evidence_refs_json",
    "rationale",
    "source_finish_reason",
    "source_llm_status",
    "status",
    "proposed_as_of",
    "approved_as_of",
    "rejected_as_of",
    "expired_as_of",
    "superseded_as_of",
    "observed_as_of",
    "proposed_by",
    "approved_by",
    "rejected_by",
    "approval_note",
    "rejection_reason",
    "previous_proposal_id",
    "executed_as_of",
    "failed_as_of",
    "executed_by",
    "execution_result_json",
    "failure_code",
    "failure_reason",
)


def is_unique_violation(error: BaseException) -> bool:
    """이 실패가 **«누가 먼저 같은 자리를 잡았다»** 인가.

    🔴 **이 값이 업무 답을 정하지 않는다.** 참이라는 것은 *"다시 읽어 봐야 한다"* 는
       뜻일 뿐이다 — 같은 요청이 이미 들어갔을 수도, 남이 **다른** 안을 세웠을 수도
       있고, 그 둘은 전혀 다른 답이다 (`proposal_service._settle_after_race`).

    ★ 드라이버 예외를 아는 자리를 여기 하나로 묶는다. 서비스가 `psycopg` 를 직접
      알게 되면 «SQL 은 저장소에만» 이라는 나눔이 조용히 새어 나간다.
    """
    return isinstance(error, pg_errors.UniqueViolation)


def _schema() -> sql.Identifier:
    return sql.Identifier(get_db_schema())


def _rows(conn: Any, query: sql.Composed, params: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _json(value: Any, *, fallback: Any) -> Any:
    """jsonb 를 문자열로 돌려주는 드라이버 설정에도 같은 값을 낸다."""
    if value is None:
        return fallback
    return json.loads(value) if isinstance(value, str) else value


def _row(raw: Mapping[str, Any]) -> ProposalRow:
    evidence = _json(raw["evidence_refs_json"], fallback=[])
    return ProposalRow(
        proposal_id=raw["proposal_id"],
        sim_run_id=raw["sim_run_id"],
        exception_id=raw["exception_id"],
        investigation_id=raw["investigation_id"],
        proposal_key=raw["proposal_key"],
        action_type=raw["action_type"],
        decision_owner=raw["decision_owner"],
        parameters=_json(raw["parameters_json"], fallback={}),
        impact=_json(raw["impact_json"], fallback={}),
        evidence_refs=tuple(evidence),
        rationale=raw["rationale"] or "",
        source_finish_reason=raw["source_finish_reason"],
        source_llm_status=raw["source_llm_status"],
        status=raw["status"],
        proposed_as_of=raw["proposed_as_of"],
        approved_as_of=raw["approved_as_of"],
        rejected_as_of=raw["rejected_as_of"],
        expired_as_of=raw["expired_as_of"],
        superseded_as_of=raw["superseded_as_of"],
        observed_as_of=raw["observed_as_of"],
        proposed_by=raw["proposed_by"],
        approved_by=raw["approved_by"],
        rejected_by=raw["rejected_by"],
        approval_note=raw["approval_note"],
        rejection_reason=raw["rejection_reason"],
        previous_proposal_id=raw["previous_proposal_id"],
        executed_as_of=raw["executed_as_of"],
        failed_as_of=raw["failed_as_of"],
        executed_by=raw["executed_by"],
        execution_result=_json(raw["execution_result_json"], fallback={}),
        failure_code=raw["failure_code"],
        failure_reason=raw["failure_reason"],
    )


def _select(where: sql.SQL, order: str = "proposal_id") -> sql.Composed:
    return sql.SQL(
        "SELECT {columns} FROM {schema}.logistics_action_proposals WHERE {where}"
        " ORDER BY {order}"
    ).format(
        columns=sql.SQL(", ").join(sql.Identifier(name) for name in _COLUMNS),
        schema=_schema(),
        where=where,
        order=sql.Identifier(order),
    )


# ══════════════════════════════════════════════════════════════════════════
#  지문과 이름 — 같은 뜻의 제안을 알아보기 위한 것들
# ══════════════════════════════════════════════════════════════════════════


def proposal_key_for(
    *,
    sim_run_id: str,
    exception_id: str,
    action_type: str,
    parameters: Mapping[str, Any],
) -> str:
    """같은 뜻의 제안이면 같은 값. **재시도를 알아보는 지문이다.**

    ```text
    같은 실행 · 같은 문제 · 같은 행동 · 같은 인자  →  같은 지문
    ```

    🔴 **유일 제약이 아니다** (§12). 거절된 뒤 같은 안을 다시 올리는 것은 정상이고,
       그때는 같은 지문의 새 행이 선다. 이 값이 막는 것은 *"네트워크가 끊겨 한 번 더
       보낸"* 경우뿐이다.

    ⚠️ **조사 식별자를 지문에 안 넣는다** (Commit 7 에 조사가 DB 에 남게 된 뒤에도).
       조사는 부를 때마다 새 ID 를 받으므로, 섞으면 **지문이 늘 달라져 아무것도 못
       막는다** — 그런데 이 지문이 막아야 하는 것은 *"같은 뜻의 제안이 두 번 들어온"*
       경우이고, 다시 조사해서 같은 안을 다시 올린 것도 그 «같은 뜻» 이다.

    ★ `Decimal` 은 `jsonable` 이 문자열로 낮춘다. `float` 로 낮추면 같은 수량이
      실행마다 다른 지문이 된다.
    """
    payload = json.dumps(
        {
            "sim_run_id": sim_run_id,
            "exception_id": exception_id,
            "action_type": action_type,
            "parameters": jsonable(dict(parameters)),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def next_proposal_id(conn: Any, *, exception_id: str) -> str:
    """`PRP-{exception_id}-{n}`. **어느 문제의 몇 번째 대응안인가.**

    ★ `exception_id_for` 와 같은 규율이다 — 이름이 겹치면 조용히 덮는 대신 다음 번호를
      찾는다. PK 충돌로 승인 화면이 터지는 것보다 낫다.
    """
    used = {
        raw["proposal_id"]
        for raw in _rows(
            conn,
            sql.SQL(
                "SELECT proposal_id FROM {schema}.logistics_action_proposals"
                " WHERE exception_id = %s"
            ).format(schema=_schema()),
            [exception_id],
        )
    }
    number = len(used) + 1
    while f"PRP-{exception_id}-{number}" in used:
        number += 1
    return f"PRP-{exception_id}-{number}"


# ══════════════════════════════════════════════════════════════════════════
#  읽기
# ══════════════════════════════════════════════════════════════════════════


def select_proposal(conn: Any, *, sim_run_id: str, proposal_id: str) -> ProposalRow | None:
    """한 건의 **지금** 값. 🔴 과거로 접는 것은 `project_proposal_at` 이 한다."""
    rows = _rows(
        conn,
        _select(sql.SQL("sim_run_id = %(sim)s AND proposal_id = %(proposal)s")),
        {"sim": sim_run_id, "proposal": proposal_id},
    )
    return _row(rows[0]) if rows else None


def select_proposals(
    conn: Any,
    *,
    sim_run_id: str,
    exception_id: str | None = None,
    statuses: Sequence[str] | None = None,
) -> tuple[ProposalRow, ...]:
    """실행 축 안의 제안들. 🔴 **`sim_run_id` 가 빠진 조회를 만들지 않는다** (§60)."""
    where = [sql.SQL("sim_run_id = %(sim)s")]
    params: dict[str, Any] = {"sim": sim_run_id}
    if exception_id is not None:
        where.append(sql.SQL("exception_id = %(exception)s"))
        params["exception"] = exception_id
    if statuses is not None:
        where.append(sql.SQL("status = ANY(%(statuses)s)"))
        params["statuses"] = list(statuses)
    rows = _rows(conn, _select(sql.SQL(" AND ").join(where)), params)
    return tuple(_row(raw) for raw in rows)


def live_proposals_for(
    conn: Any, *, sim_run_id: str, exception_id: str
) -> tuple[ProposalRow, ...]:
    """그 문제에 **살아 있는** 대응안. ⚠️ `APPROVED` 도 살아 있다 (실행 전이다)."""
    return select_proposals(
        conn,
        sim_run_id=sim_run_id,
        exception_id=exception_id,
        statuses=LIVE_PROPOSAL_STATUSES,
    )


def exception_status(conn: Any, *, sim_run_id: str, exception_id: str) -> str | None:
    """그 Exception 의 지금 상태. 없으면 `None`.

    ★ `agent/exceptions.py` 를 안 부르는 이유: 저쪽 읽기는 **살아 있는 행 전부**를 내는
      목록 조회라, 여기 필요한 *"이 한 건이 지금 무슨 상태인가"* 와 모양이 다르다.
    """
    rows = _rows(
        conn,
        sql.SQL(
            "SELECT status FROM {schema}.logistics_exceptions"
            " WHERE sim_run_id = %(sim)s AND exception_id = %(exception)s"
        ).format(schema=_schema()),
        {"sim": sim_run_id, "exception": exception_id},
    )
    return rows[0]["status"] if rows else None


# ══════════════════════════════════════════════════════════════════════════
#  과거 재현 — **순수 함수다. DB 를 안 본다**
# ══════════════════════════════════════════════════════════════════════════


def project_proposal_at(row: ProposalRow, *, as_of: date) -> ProposalRow | None:
    """**그날** 이 제안은 어떤 상태였나. 없었으면 `None`.

    ```text
    proposed_as_of > as_of      아직 없다                    → None
    executed_as_of <= as_of     EXECUTED       ← 승인일도 함께 도착하지만 나중 것이 이긴다
    failed_as_of   <= as_of     FAILED
    approved_as_of <= as_of     APPROVED
    rejected_as_of <= as_of     REJECTED
    expired_as_of  <= as_of     EXPIRED
    superseded_as_of <= as_of   SUPERSEDED
    그 밖                        PROPOSED
    ```

    🔴 **승인은 끝이 아니다** (Commit 6). `D5 제안 · D6 승인 · D8 실행` 이 정상 흐름이라
       한 행이 승인일과 실행일을 **함께** 든다 — 그래서 «끝난 날은 하나» 로 접으면 안 되고
       **우선순위**로 골라야 한다.

    🔴 **지금 값을 과거로 쓰지 않는다.** `row.status` 를 그대로 내면 D8 에 실행된 제안이
       D7 조회에서도 실행됨으로 보인다 — 그날은 아직 승인 대기였다.

    🔴 **미래 detail 을 안 흘린다** (§21 · §42). **칸 묶음마다 자기 날짜로** 가린다 —
       그래서 `EXECUTED` 조회에도 승인자는 보이고(그 일은 실제로 일어났다), `APPROVED`
       조회에는 `executed_by` · `execution_result` 가 안 보인다.

    ⚠️ 못 고르면 **답하지 않는다** — `ProposalInvariantViolation` 을 올린다.
    """
    if row.proposed_as_of > as_of:
        return None
    for status, column in _STATUS_PRECEDENCE:
        if row.status == status and getattr(row, column) is None:
            # 🔴 상태는 그 날인데 그날을 못 댄다 — 날짜 없이 상태만 옮기면 그 전날
            #    조회에도 그 상태가 뜬다.
            raise ProposalInvariantViolation(
                f"{row.proposal_id} 의 상태 {status} 에 {column} 이 없다"
                " — 언제 그렇게 됐는지를 못 대면 과거로 접을 수 없다"
            )

    landed = {
        status
        for status, column in _STATUS_PRECEDENCE
        if (when := getattr(row, column)) is not None and when <= as_of
    }
    if "EXECUTED" in landed and "FAILED" in landed:
        raise ProposalInvariantViolation(
            f"{row.proposal_id} 는 {as_of} 까지 실행과 실패가 모두 적혀 있다"
            " — 한 제안이 두 결말을 가질 수 없다"
        )
    exclusive = sorted(landed & _EXCLUSIVE_DECISIONS)
    if len(exclusive) > 1:
        raise ProposalInvariantViolation(
            f"{row.proposal_id} 에 {as_of} 까지 결정이 {len(exclusive)} 개다"
            f" ({exclusive}) — 그날 상태를 고를 수 없다"
        )
    if (landed & {"EXECUTED", "FAILED"}) and "APPROVED" not in landed:
        raise ProposalInvariantViolation(
            f"{row.proposal_id} 는 승인 없이 실행된 것으로 적혀 있다"
            " — 누가 진행해도 좋다고 했는지를 못 댄다"
        )

    status = next(
        (one for one, _ in _STATUS_PRECEDENCE if one in landed),
        "PROPOSED",
    )
    # ★ 그날 **실제로 일어난** 결정의 칸은 남긴다 — 실행된 제안의 승인자도 그중 하나다.
    #   상태 하나만 보고 가리면 그 사실이 사라진다.
    #
    # 🔴 **먼저 «보일 칸» 을 다 모은 뒤에 지운다.** `executed_by` 처럼 두 묶음이 **같은
    #    칸을 공유**하기 때문이다 — 묶음을 하나씩 돌며 지우면, 실행된 제안에서
    #    «실패 묶음이 안 왔다» 는 이유로 그 칸이 지워진다.
    visible = {
        name
        for decided, columns in DECISION_DETAIL_COLUMNS.items()
        if decided in landed
        for name in columns
    }
    blanked: dict[str, Any] = {
        name: _BLANK_VALUES.get(name)
        for decided, columns in DECISION_DETAIL_COLUMNS.items()
        if decided not in landed
        for name in columns
        if name not in visible
    }
    return replace(row, status=status, **blanked)


def latest_terminal_as_of(
    rows: Sequence[ProposalRow],
) -> tuple[date | None, tuple[str, ...]]:
    """이 문제의 기존 제안들이 **마지막으로 끝난 날**. 없으면 `None`.

    **«끝난 날» 은 그 제안이 더는 실행 대기가 아니게 된 날이다.**

    ```text
    REJECTED     rejected_as_of
    EXPIRED      expired_as_of
    SUPERSEDED   superseded_as_of
    EXECUTED     executed_as_of      ← Commit 6 에서 칸이 섰다
    FAILED       failed_as_of        ← 〃
    ```

    ```text
    P1  D5 제안 · D6 거절            →  (D6, ())
    P1  D6 승인 · D8 실행            →  (D8, ())   ← 승인일이 아니라 **실행일**이다
    P1  D6 승인 · 아직 실행 전        →  (None, ()) ← 끝난 날이 없다 (승인은 끝이 아니다)
    ```

    🔴 **`APPROVED` 는 여기 안 센다** (§17). 승인은 **실행 대기**라, 승인일을 끝으로 세면
       `D6 승인 · D8 실행` 뒤에 오는 새 제안이 «D6 이후면 된다» 고 답하게 되고 그러면
       D7 조회에서 살아 있는 제안이 둘이 된다.

    🔴 **새 제안이 그날보다 앞서면 «그날 살아 있던 제안» 이 둘이 된다.** 부분 유일
       인덱스는 «지금» 상태만 보므로 이 겹침을 못 막는다 — 과거로 접었을 때만 보인다.

    ⚠️ **끝난 상태인데 그 날을 못 대는 행은 통과시키지 않는다.** production 에서는 DB
       CHECK 가 그런 행을 막지만(`ck_…_executed` · `ck_…_failed`), 손으로 고친 행이
       섞이면 순서를 못 세우므로 **이름을 돌려주고 부르는 쪽이 멈춘다.**

    :returns: `(마지막으로 끝난 날, 순서를 못 세우는 제안 ID 들)`.
        🔴 **아무것도 안 읽고 안 쓴다** — 순수 함수다.
    """
    landed: list[date] = []
    unorderable: list[str] = []
    for row in rows:
        dates = [
            when
            for column in TERMINAL_DATE_STATUSES
            if (when := getattr(row, column)) is not None
        ]
        if dates:
            landed.extend(dates)
        elif row.status in {"EXECUTED", "FAILED"}:
            unorderable.append(row.proposal_id)
    return (max(landed) if landed else None, tuple(unorderable))


# ══════════════════════════════════════════════════════════════════════════
#  쓰기 — 🔴 커밋하지 않는다
# ══════════════════════════════════════════════════════════════════════════


def insert_proposal(conn: Any, *, row: ProposalRow) -> ProposalRow:
    """새 대응안 한 행. 🔴 **`status` 는 `PROPOSED` 로만 만든다.**

    ★ 승인된 제안을 «처음부터 승인된 채» 넣는 길을 안 열어 둔다 — 그러면 사람이 언제
      무엇을 봤는지가 기록에서 사라진다.
    """
    if row.status != "PROPOSED":
        raise ValueError(f"새 제안은 PROPOSED 로만 선다 (받은 값: {row.status})")
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {schema}.logistics_action_proposals (
                    proposal_id, sim_run_id, exception_id, investigation_id, proposal_key,
                    action_type, decision_owner, parameters_json, impact_json,
                    evidence_refs_json, rationale, source_finish_reason, source_llm_status,
                    status, proposed_as_of, observed_as_of, proposed_by, previous_proposal_id
                ) VALUES (
                    %(proposal_id)s, %(sim)s, %(exception)s, %(investigation)s, %(key)s,
                    %(action)s, %(owner)s, %(parameters)s::jsonb, %(impact)s::jsonb,
                    %(evidence)s::jsonb, %(rationale)s, %(finish)s, %(llm)s,
                    'PROPOSED', %(proposed)s, %(observed)s, %(by)s, %(previous)s
                )
                """
            ).format(schema=_schema()),
            {
                "proposal_id": row.proposal_id,
                "sim": row.sim_run_id,
                "exception": row.exception_id,
                "investigation": row.investigation_id,
                "key": row.proposal_key,
                "action": row.action_type,
                "owner": row.decision_owner,
                "parameters": _dumps(row.parameters),
                "impact": _dumps(row.impact),
                "evidence": _dumps(list(row.evidence_refs)),
                "rationale": row.rationale,
                "finish": row.source_finish_reason,
                "llm": row.source_llm_status,
                "proposed": row.proposed_as_of,
                "observed": row.observed_as_of,
                "by": row.proposed_by,
                "previous": row.previous_proposal_id,
            },
        )
    return row


#: 상태마다 «어느 칸에 날·사람·문장을 적나». 🔴 이 표 밖의 칸은 전이가 **안 건드린다** —
#:    `action_type` · `parameters_json` · `impact_json` 은 만들어진 뒤 불변이다 (§16).
_TRANSITION_COLUMNS: Mapping[str, tuple[str, str | None, str | None]] = {
    "APPROVED": ("approved_as_of", "approved_by", "approval_note"),
    "REJECTED": ("rejected_as_of", "rejected_by", "rejection_reason"),
    "EXPIRED": ("expired_as_of", None, None),
    "SUPERSEDED": ("superseded_as_of", None, None),
}


def transition_proposal(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    to_status: str,
    as_of: date,
    from_status: str = "PROPOSED",
    actor: str | None = None,
    note: str | None = None,
    require_exception_status: str | None = None,
) -> int:
    """상태를 한 칸 옮긴다. :returns: **바뀐 행 수** (0 이면 조건이 이미 안 맞는다).

    🔴 **`WHERE ... AND status = {from_status}` 가 이 함수의 심장이다** (§37). 두 사람이
       동시에 승인을 눌러도 UPDATE 는 한 번만 먹고, 늦은 쪽은 `0` 을 받는다. 읽고 나서
       쓰는 사이에 남이 끼어드는 lost update 를 응용 코드로는 못 막는다.

    :param require_exception_status: 주면 **그 Exception 이 지금 그 상태일 때만** 옮긴다.
        🔴 승인이 이것을 쓴다 — 이미 닫힌 문제의 제안을 승인하는 것을 막는 조건이
        읽기 쪽에만 있으면, 읽고 쓰는 사이에 재탐지가 문제를 닫아도 승인이 그대로
        들어간다. 조건이 **같은 문장 안**에 있어야 그 틈이 없다.
        ⚠️ 거절·만료·대체는 이 조건을 **안 건다** — 이미 닫힌 문제에 남은 제안을 사람이
        정리하는 것은 정상이다.

    ⚠️ `0` 을 «실패» 로 읽지 않는다 — *"내가 보고 온 상태가 이미 아니다"* 라는 사실이다.
       그것을 어떻게 다룰지(멱등인가 충돌인가 stale 인가)는 서비스가 정한다.
    """
    if to_status not in _TRANSITION_COLUMNS:
        raise ValueError(f"Commit 5 가 여는 전이가 아니다: {to_status}")
    date_column, actor_column, note_column = _TRANSITION_COLUMNS[to_status]

    assignments = [
        sql.SQL("status = %(to_status)s"),
        sql.SQL("{column} = %(as_of)s").format(column=sql.Identifier(date_column)),
        sql.SQL("updated_at = now()"),
    ]
    params: dict[str, Any] = {
        "to_status": to_status,
        "as_of": as_of,
        "from_status": from_status,
        "sim": sim_run_id,
        "proposal_id": proposal_id,
    }
    if actor_column is not None:
        assignments.append(
            sql.SQL("{column} = %(actor)s").format(column=sql.Identifier(actor_column))
        )
        params["actor"] = actor
    if note_column is not None:
        assignments.append(
            sql.SQL("{column} = %(note)s").format(column=sql.Identifier(note_column))
        )
        params["note"] = note

    gate = sql.SQL("")
    if require_exception_status is not None:
        # 🔴 **같은 문장 안**에서 건다 — 읽고 쓰는 사이의 틈을 없애는 것이 목적이다.
        gate = sql.SQL(
            """
               AND EXISTS (
                   SELECT 1
                     FROM {schema}.logistics_exceptions AS problem
                    WHERE problem.exception_id = proposal.exception_id
                      AND problem.sim_run_id = proposal.sim_run_id
                      AND problem.status = %(exception_status)s
               )
            """
        ).format(schema=_schema())
        params["exception_status"] = require_exception_status

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.logistics_action_proposals AS proposal
                   SET {assignments}
                 WHERE proposal.sim_run_id = %(sim)s
                   AND proposal.proposal_id = %(proposal_id)s
                   AND proposal.status = %(from_status)s
                   {gate}
                """
            ).format(
                schema=_schema(),
                assignments=sql.SQL(", ").join(assignments),
                gate=gate,
            ),
            params,
        )
        return cursor.rowcount


def record_execution_outcome(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    as_of: date,
    executed_by: str,
    executed: bool,
    result: Mapping[str, Any] | None = None,
    failure_code: str | None = None,
    failure_reason: str | None = None,
) -> int:
    """실행 결과를 적는다. :returns: **바뀐 행 수** (0 이면 이미 `APPROVED` 가 아니다).

    🔴 **`WHERE status = 'APPROVED'` 가 이 함수의 심장이다.** 두 worker 가 동시에 같은
       제안을 실행해도 이 `UPDATE` 는 한 번만 먹는다 — 늦은 쪽은 `0` 을 받고, 그때
       무엇을 할지는 서비스가 정한다 (§28 · §55).

    ⚠️ **여기서 업무 표를 안 건드린다.** 실제 실행은 도메인 정본 함수가 이미 했고, 이
       함수는 *"그 결과를 제안에 적는" 일*만 한다.
    """
    if executed and not (executed_by or "").strip():
        raise ValueError("executed_by 가 비었다 — 누가 돌렸는지 못 대는 실행은 안 적는다")
    if not executed and not (failure_code or "").strip():
        raise ValueError("failure_code 가 비었다 — 사유 없는 실패는 «모른다» 와 구별되지 않는다")

    assignments = sql.SQL(
        """
        status = 'EXECUTED',
        executed_as_of = %(as_of)s,
        executed_by = %(actor)s,
        execution_result_json = %(result)s::jsonb
        """
    ) if executed else sql.SQL(
        """
        status = 'FAILED',
        failed_as_of = %(as_of)s,
        executed_by = %(actor)s,
        failure_code = %(failure_code)s,
        failure_reason = %(failure_reason)s
        """
    )
    params: dict[str, Any] = {
        "as_of": as_of,
        "actor": executed_by,
        "sim": sim_run_id,
        "proposal_id": proposal_id,
    }
    if executed:
        params["result"] = _dumps(dict(result or {}))
    else:
        params["failure_code"] = failure_code
        params["failure_reason"] = failure_reason

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.logistics_action_proposals
                   SET {assignments}, updated_at = now()
                 WHERE sim_run_id = %(sim)s
                   AND proposal_id = %(proposal_id)s
                   AND status = 'APPROVED'
                """
            ).format(schema=_schema(), assignments=assignments),
            params,
        )
        return cursor.rowcount


def accept_exception_risk(
    conn: Any, *, sim_run_id: str, exception_id: str, as_of: date
) -> int:
    """위험 수용을 Exception 에 적는다. :returns: 바뀐 행 수.

    🔴 **닫지 않는다.** `status` 를 안 건드린다 — 위험을 안고 가기로 한 것이지 조건이
       사라진 것이 아니다 (상세설계 §7.1 F). 재탐지는 계속 돈다.

    🔴 **처음 수용한 날을 지킨다** (`COALESCE`). 두 번째 실행이 첫 날을 덮으면
       *"언제부터 이 위험을 안고 갔나"* 가 사라진다 — `proposed_as_of` 와 같은 규율이고,
       같은 제안을 두 번 실행해도 결과가 같게 만든다.

    ⚠️ 이미 닫힌(`RESOLVED` · `DISMISSED`) 문제에는 안 적는다 — 그 사실을 `0` 으로 낸다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.logistics_exceptions
                   SET risk_accepted_as_of = COALESCE(risk_accepted_as_of, %(as_of)s),
                       updated_at = now()
                 WHERE sim_run_id = %(sim)s
                   AND exception_id = %(exception)s
                   AND status IN ('OPEN', 'PROPOSED')
                """
            ).format(schema=_schema()),
            {"as_of": as_of, "sim": sim_run_id, "exception": exception_id},
        )
        return cursor.rowcount


def exception_risk_accepted_as_of(
    conn: Any, *, sim_run_id: str, exception_id: str
) -> date | None:
    """**되읽기용.** 위험 수용이 실제로 표에 적혔나 (Verify · §38 · §47)."""
    rows = _rows(
        conn,
        sql.SQL(
            "SELECT risk_accepted_as_of FROM {schema}.logistics_exceptions"
            " WHERE sim_run_id = %(sim)s AND exception_id = %(exception)s"
        ).format(schema=_schema()),
        {"sim": sim_run_id, "exception": exception_id},
    )
    return rows[0]["risk_accepted_as_of"] if rows else None


def mark_exception_proposed(
    conn: Any, *, sim_run_id: str, exception_id: str, as_of: date
) -> int:
    """Exception 을 «대응안 대기» 로. :returns: 바뀐 행 수.

    🔴 **`proposed_as_of` 는 `COALESCE` 로 «처음» 날을 지킨다.** 두 번째 제안이 첫
       제안이 선 날을 덮으면 *"이 문제에 대응안이 언제 처음 섰나"* 가 사라진다 —
       `opened_as_of` 를 `touch_exception` 이 안 건드리는 것과 같은 이유다.

    ⚠️ **`OPEN` 과 `PROPOSED` 둘 다에서 먹는다.** 거절 뒤 다시 제안하는 흐름이 정상이고,
       `RESOLVED` · `DISMISSED` 는 조건에서 빠져 **한 행도 안 바뀐다** (그 사실을 `0`
       으로 돌려준다).
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.logistics_exceptions
                   SET status = 'PROPOSED',
                       proposed_as_of = COALESCE(proposed_as_of, %(as_of)s),
                       updated_at = now()
                 WHERE sim_run_id = %(sim)s
                   AND exception_id = %(exception)s
                   AND status IN ('OPEN', 'PROPOSED')
                """
            ).format(schema=_schema()),
            {"as_of": as_of, "sim": sim_run_id, "exception": exception_id},
        )
        return cursor.rowcount


def reopen_exception(conn: Any, *, sim_run_id: str, exception_id: str) -> int:
    """살아 있는 대응안이 하나도 안 남았다 → 다시 «대응안 없는 문제». :returns: 행 수.

    🔴 **`proposed_as_of` 를 안 지운다.** 그날 제안이 섰다는 것은 일어난 사실이고,
       상태가 되돌아갔다고 사실을 지울 이유가 없다 (§8 · DDL §2 주석).

    ⚠️ **`RESOLVED` · `DISMISSED` 를 되살리지 않는다.** 재탐지가 이미 닫은 문제를 제안
       거절이 다시 열면, 조건이 사라진 문제가 장부에 남는다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.logistics_exceptions
                   SET status = 'OPEN',
                       updated_at = now()
                 WHERE sim_run_id = %(sim)s
                   AND exception_id = %(exception)s
                   AND status = 'PROPOSED'
                """
            ).format(schema=_schema()),
            {"sim": sim_run_id, "exception": exception_id},
        )
        return cursor.rowcount


def _dumps(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False)
