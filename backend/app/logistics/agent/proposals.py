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
from dataclasses import dataclass, replace
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
    "exception_status",
    "insert_proposal",
    "is_unique_violation",
    "latest_terminal_as_of",
    "live_proposals_for",
    "mark_exception_proposed",
    "next_proposal_id",
    "project_proposal_at",
    "proposal_key_for",
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

#: 끝난 날 칸 → 그 날이 뜻하는 상태. **과거 재현의 사전이다** (§41).
TERMINAL_DATE_STATUSES: Mapping[str, str] = {
    "approved_as_of": "APPROVED",
    "rejected_as_of": "REJECTED",
    "expired_as_of": "EXPIRED",
    "superseded_as_of": "SUPERSEDED",
}

#: 상태마다 «그 결정이 남긴 칸». 🔴 **그날 안 일어난 결정의 칸은 비워서 낸다** (§42) —
#:    D10 의 거절 사유가 D6 조회에 보이면 그날 없던 사실이 과거에 생긴다.
DECISION_DETAIL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "APPROVED": ("approved_as_of", "approved_by", "approval_note"),
    "REJECTED": ("rejected_as_of", "rejected_by", "rejection_reason"),
    "EXPIRED": ("expired_as_of",),
    "SUPERSEDED": ("superseded_as_of",),
}


class ProposalInvariantViolation(RuntimeError):
    """그날 상태를 **못 고른다.** 🔴 하나를 골라 답하지 않고 멈춘다 (fail-closed).

    ```text
    끝난 날이 둘      DB CHECK 가 막지만, 손으로 고친 행이 있을 수 있다
    EXECUTED/FAILED   그 상태가 «언제» 됐는지 적는 칸이 아직 없다 (Commit 6)
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
    #: Commit 4 의 조사는 DB 에 안 남는다 — 지금은 늘 `None` 이다 (§44).
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

    ⚠️ **조사 식별자를 지문에 안 넣는다.** Commit 4 의 조사는 DB 에 안 남아서 실제로
       존재하지 않는 값이다 — 없는 것을 키에 섞으면 지문이 늘 달라져 아무것도 못 막는다.

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
    approved_as_of <= as_of     APPROVED
    rejected_as_of <= as_of     REJECTED
    expired_as_of  <= as_of     EXPIRED
    superseded_as_of <= as_of   SUPERSEDED
    그 밖                        PROPOSED
    ```

    🔴 **지금 값을 과거로 쓰지 않는다.** `row.status` 를 그대로 내면 D10 에 거절된 제안이
       D6 조회에서도 거절로 보인다 — 그날 아직 사람이 안 본 제안이다.

    🔴 **미래 detail 을 안 흘린다** (§42). 그날 안 일어난 결정의 칸(`rejected_by` ·
       `rejection_reason` · `approval_note` …)은 **비워서** 낸다. Commit 3 의 Exception
       과거 조회가 세운 원칙 그대로다.

    ⚠️ 못 고르면 **답하지 않는다** — `ProposalInvariantViolation` 을 올린다.
    """
    if row.proposed_as_of > as_of:
        return None
    if row.status in {"EXECUTED", "FAILED"}:
        # 🔴 그 상태가 «언제» 됐는지 적는 칸이 아직 없다 (Commit 6). 날짜 없이 상태만
        #    옮기면 실행 전날 조회에도 «실행됨» 이 뜬다.
        raise ProposalInvariantViolation(
            f"{row.proposal_id} 의 상태 {row.status} 는 전이 날짜 칸이 없어 과거로 못 접는다"
            " — 실행 상태는 Commit 6 의 것이다"
        )

    landed = [
        (status, when)
        for column, status in TERMINAL_DATE_STATUSES.items()
        if (when := getattr(row, column)) is not None and when <= as_of
    ]
    if len(landed) > 1:
        raise ProposalInvariantViolation(
            f"{row.proposal_id} 에 {as_of} 까지 끝난 날이 {len(landed)} 개다"
            f" ({sorted(status for status, _ in landed)}) — 그날 상태를 고를 수 없다"
        )

    status = landed[0][0] if landed else "PROPOSED"
    blanked: dict[str, Any] = {}
    for decided, columns in DECISION_DETAIL_COLUMNS.items():
        if decided == status:
            continue
        blanked.update(dict.fromkeys(columns))
    return replace(row, status=status, **blanked)


def latest_terminal_as_of(
    rows: Sequence[ProposalRow],
) -> tuple[date | None, tuple[str, ...]]:
    """이 문제의 기존 제안들이 **마지막으로 끝난 날**. 없으면 `None`.

    ```text
    P1  D5 제안 · D6 거절      →  (D6, ())
    P1  아직 살아 있다          →  (None, ())     ← 끝난 날이 없다
    P1  EXECUTED (Commit 6)    →  (None, ('P1',)) ← 끝난 날을 못 댄다
    ```

    🔴 **새 제안이 그날보다 앞서면 «그날 살아 있던 제안» 이 둘이 된다.** 부분 유일
       인덱스는 «지금» 상태만 보므로 이 겹침을 못 막는다 — 과거로 접었을 때만 보인다.

    ⚠️ **`EXECUTED` · `FAILED` 는 끝난 날 칸이 없다** (Commit 6). 그런 행이 섞이면 순서를
       못 세우므로 **이름을 돌려주고 부르는 쪽이 멈춘다** — 추측해서 통과시키지 않는다.

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
