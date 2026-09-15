"""조사를 **한 번 돌리고, 그 실행을 감사 기록으로 남긴다.**

```text
run_and_persist_investigation
   ① investigation_id 를 짓고
   ② run_investigation(...)          ← Commit 4 의 Runtime 그대로. DB 에 안 쓴다
   ③ 결정론 snapshot                  ← Tool 을 다시 안 부르고 LLM 에게 안 묻는다
   ④ INSERT
   ⑤ commit
   → PersistedInvestigation
```

🔴 **`run_investigation` 은 여전히 DB-write-free 다** (§16). 그래프에 `persist` 노드도
   INSERT 도 commit 도 넣지 않았다 — 그 계약을 지키는 자리가 이 파일이다. 조사를 그냥
   부르면 아무것도 안 남고, **이 wrapper 를 불러야만** 감사 행이 선다.

🔴 **저장이 새 업무 숫자를 만들지 않는다.** 재고·용량·신선도·영향을 다시 계산하는 줄이
   한 줄도 없다. Runtime 이 이미 낸 결과를 **결정론으로 직렬화**할 뿐이다.

🔴 **«조사를 적었다» 가 «무엇을 했다» 로 번지지 않는다.** `LLM_FAILED` · `TIMEOUT` ·
   `TOOL_FAILED` 도 감사 행으로 남지만, 그것은 audit INSERT 하나일 뿐이다 —
   Exception 상태도, 제안도, 재고도 그 때문에 움직이지 않는다 (Commit 4 의 계약 그대로).

🔴 **조사 저장과 제안 생성은 다른 트랜잭션이다** (§32 · §33).

```text
조사 실행 → 조사 저장 → commit → (그 다음에) 제안 생성
```

   제안이 `LIVE_PROPOSAL_EXISTS` · `NO_RECOMMENDED_OPTION` 으로 안 섰다고 **조사가
   없었던 일이 되면 안 된다** — 조사는 실제로 돌았다. 반대로 조사 저장이 실패하면
   제안을 만들지 않는다: 가리킬 조사가 없는데 `investigation_id` 만 적을 수는 없다.

⚠️ **이 함수가 커넥션의 트랜잭션 주인이다.** 부르기 전에 커밋하지 않은 업무 쓰기가
   남아 있으면 안 된다 — 조사를 돌린 뒤 감사 INSERT 앞에서 한 번 되감기 때문이다
   (그 이유는 `run_and_persist_investigation` 안에 적혀 있다).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.logistics.agent import investigation_repository as repository
from app.logistics.agent.graph import run_investigation
from app.logistics.agent.investigation import (
    InvestigationBudget,
    InvestigationResult,
    jsonable,
)
from app.logistics.agent.investigation_repository import InvestigationRow

__all__ = [
    "INVESTIGATION_LOST_AFTER_RACE",
    "INVESTIGATION_PAYLOAD_CONFLICT",
    "TRACE_FIELDS",
    "InvestigationStateConflict",
    "PersistedInvestigation",
    "investigation_row_for",
    "run_and_persist_investigation",
    "same_investigation_audit",
    "save_investigation",
    "snapshot_investigation_result",
    "snapshot_tool_trace",
]


#: 🔴 같은 `investigation_id` 로 **다른 내용**이 들어왔다. 저장 재시도가 아니다.
INVESTIGATION_PAYLOAD_CONFLICT = "INVESTIGATION_PAYLOAD_CONFLICT"
#: 🔴 부딪혔는데 **무엇과 부딪혔는지 못 읽었다.** 추측해서 «저장됐다» 고 답하지 않는다.
INVESTIGATION_LOST_AFTER_RACE = "INVESTIGATION_LOST_AFTER_RACE"


class InvestigationStateConflict(RuntimeError):
    """조사 기록을 그렇게 남길 수 없다. **부르는 쪽의 실수이거나 진짜 충돌이다.**"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class PersistedInvestigation:
    """조사 한 번과 **그 실행이 남긴 행.**

    ```text
    SAVED    이 호출이 행을 세웠다
    REUSED   같은 ID 로 **같은 내용**이 이미 있었다 — 저장 재시도다 (§36)
    ```

    ⚠️ `REUSED` 는 «같은 조사를 다시 돌렸다» 가 아니다. 다시 조사한 것은 **새 실행**이고
       새 ID 를 받는다 (§35) — 이 값이 뜻하는 것은 *"같은 저장을 두 번 시도했다"* 뿐이다.
    """

    investigation_id: str
    result: InvestigationResult
    row: InvestigationRow
    status: Literal["SAVED", "REUSED"] = "SAVED"


# ══════════════════════════════════════════════════════════════════════════
#  snapshot — 🔴 **결정론이다.** Tool 도 LLM 도 여기서 안 부른다
# ══════════════════════════════════════════════════════════════════════════

#: 조사 호출 하나를 기록하는 칸들. 🔴 **`answer` 가 없다.**
TRACE_FIELDS: tuple[str, ...] = (
    "sequence",
    "tool_name",
    "arguments",
    "status",
    "reason",
    "detail",
    "observed_as_of",
    "uncertainties",
)


def snapshot_tool_trace(result: InvestigationResult) -> tuple[Mapping[str, Any], ...]:
    """*"무엇을 어떤 순서로 물었고 그 호출이 어떻게 끝났나"* — **답은 안 담는다.**

    ```text
    담는다    sequence · tool_name · arguments · status · reason · detail
              observed_as_of · uncertainties
    안 담는다  answer                                  ← Tool 이 낸 값 전체
    ```

    🔴 **`jsonable(record)` 를 쓰지 않는다.** 그 함수는 dataclass 의 칸을 전부 싣기
       때문에 `ToolCallRecord.answer` 가 **통째로 딸려 들어온다** — 용량 문맥의 18일
       창, 품목 Lot 목록, 예약 객체가 조사마다 복사된다. 칸을 손으로 고르는 이유가
       이것이다: 계약이 넓어져도 이 표는 안 넓어진다.

    ⚠️ **실패하고 거부된 호출도 남긴다.** *"물어봤는데 못 봤다"* 와 *"아예 안 물었다"* 는
       다른 사실이고, 실패를 지우면 조사 기록이 성공한 호출만 모은 이야기가 된다.
    """
    return tuple(
        {
            "sequence": record.sequence,
            "tool_name": record.tool_name,
            "arguments": jsonable(dict(record.arguments)),
            "status": record.status.value,
            "reason": record.reason,
            "detail": record.detail,
            "observed_as_of": jsonable(record.observed_as_of),
            "uncertainties": list(record.uncertainties),
        }
        for record in result.tool_calls
    )


def snapshot_investigation_result(result: InvestigationResult) -> Mapping[str, Any]:
    """조사의 **최종 판단**만. 🔴 `jsonable(result)` 를 통째로 쓰지 않는다.

    통째로 낮추면 `tool_calls[].answer` 가 **결과 칸으로도** 딸려 들어와, `answer` 를
    빼려고 trace 를 손으로 고른 일이 헛수고가 된다. 그래서 여기도 칸을 고른다.

    ```text
    담는다    finish_reason · llm_status · llm_error_kind
              summary · findings · missing_or_uncertain
              options[] (action · parameters · decision_owner · impact · rejected_reason)
              recommended_index · observed_as_of · uncertainties · counts
    안 담는다  Tool 답 전체 · raw LLM prompt · raw LLM response · system prompt ·
              공급자 request/response · LangGraph 내부 상태
    ```

    ★ `options[].impact` 는 **보존한다** — 그것이 Commit 3 의 결정론 계산기가 낸
      최종 판단이고, 조사 결과의 일부다. 🔴 저장하며 다시 셈하지는 않는다.

    ⚠️ `finish_reason` · `llm_status` · `llm_error_kind` 는 표의 칸에도 있다. 같은 값을
       두 곳에 적는 것이 아니라 **같은 하나의 출처**(`result`)에서 각각 옮기는 것이라
       어긋날 수 없고, 결과 snapshot 하나만 떠도 자족적으로 읽힌다.
    """
    return {
        "sim_run_id": result.sim_run_id,
        "exception_id": result.exception_id,
        "as_of": jsonable(result.as_of),
        "finish_reason": result.finish_reason.value,
        "llm_status": result.llm_status,
        "llm_error_kind": result.llm_error_kind,
        "summary": result.summary,
        "findings": list(result.findings),
        "missing_or_uncertain": list(result.missing_or_uncertain),
        # ★ `EvaluatedOption` 에는 Tool 답 칸이 없다 — 있는 것은 결정론이 검증한 뒤의
        #   후보와 그 영향뿐이라 통째로 낮춰도 조사 로그가 새지 않는다.
        "options": [jsonable(option) for option in result.options],
        "recommended_index": result.recommended_index,
        "observed_as_of": jsonable(result.observed_as_of),
        "uncertainties": list(result.uncertainties),
        # 🔴 예산을 얼마나 썼나 — **새로 세지 않는다.** Runtime 이 이미 센 값이다.
        "counts": {
            "tool_calls": result.tool_call_count,
            "planned_tool_calls": result.planned_tool_call_count,
            "replans": result.replan_count,
            "llm_calls": result.llm_call_count,
        },
    }


def investigation_row_for(
    result: InvestigationResult, *, investigation_id: str
) -> InvestigationRow:
    """결과 하나를 행 하나로. 🔴 **여기서 값을 만들지 않는다** — 전부 옮기기만 한다.

    ★ **공개 helper 다** — 저장하는 쪽(`save_investigation`)과, 저장된 조사가 정말 이
      결과의 저장본인지 **확인하는 쪽**(`proposal_service`)이 **같은 함수**를 지나야
      한다. 확인하는 쪽이 자기 직렬화를 따로 적으면 두 규칙이 조용히 갈라지고, 그러면
      «다르다» 가 실제 차이인지 직렬화 차이인지 아무도 못 댄다.

    🔴 **순수 함수다** — DB 도 Tool 도 LLM 도 안 부른다.
    """
    return InvestigationRow(
        investigation_id=investigation_id,
        sim_run_id=result.sim_run_id,
        exception_id=result.exception_id,
        # 🔴 업무 날짜는 Runtime 이 받은 `as_of` 다 — `date.today()` 가 아니다 (§38).
        as_of=result.as_of,
        finish_reason=result.finish_reason.value,
        llm_status=result.llm_status,
        llm_error_kind=result.llm_error_kind,
        # 🔴 `None` 이면 `None` 이다 — `as_of` 로도 `created_at` 으로도 안 메운다 (§39).
        observed_as_of=result.observed_as_of,
        tool_trace=snapshot_tool_trace(result),
        result=snapshot_investigation_result(result),
    )


# ══════════════════════════════════════════════════════════════════════════
#  저장 — 🔴 트랜잭션의 주인이 여기다
# ══════════════════════════════════════════════════════════════════════════


def save_investigation(
    conn: Any, *, result: InvestigationResult, investigation_id: str | None = None
) -> PersistedInvestigation:
    """이미 끝난 조사 하나를 감사 행으로 남긴다. **조사를 다시 돌리지 않는다.**

    :param investigation_id: 저장을 **재시도**할 때만 준다. 안 주면 새 ID 를 짓는다 —
        🔴 같은 조사를 다시 돌린 것은 새 실행이라 새 ID 여야 하고(§35), 이 인자는
        *"아까 그 저장이 들어갔는지 모르겠다"* 를 위한 것이다.
    """
    identifier = investigation_id or repository.new_investigation_id()
    row = investigation_row_for(result, investigation_id=identifier)
    try:
        repository.insert_investigation(conn, row=row)
        conn.commit()
    except Exception as error:
        conn.rollback()
        if repository.is_unique_violation(error):
            return _settle_after_race(conn, result=result, row=row)
        raise
    return PersistedInvestigation(
        investigation_id=identifier, result=result, row=row, status="SAVED"
    )


def _settle_after_race(
    conn: Any, *, result: InvestigationResult, row: InvestigationRow
) -> PersistedInvestigation:
    """부딪힌 뒤 **다시 읽어** 무슨 일이었는지로 답을 정한다.

    ```text
    같은 ID · 같은 내용   →  REUSED                          저장 재시도였다
    같은 ID · 다른 내용   →  🔴 INVESTIGATION_PAYLOAD_CONFLICT  남의 조사를 내 것이라
                                                             고 답하지 않는다
    행이 없다            →  🔴 답하지 않는다                   무엇과 부딪혔는지 못 댄다
    ```

    🔴 **유일 제약 위반을 곧바로 «이미 저장됐다» 로 옮기지 않는다** (`proposals` 의
       `_settle_after_race` 와 같은 규율). 그렇게 하면 다른 내용의 충돌이 조용히
       «성공» 으로 둔갑하고, 부르는 쪽은 자기가 만든 기록이 저장됐다고 믿는다.

    ⚠️ PostgreSQL 은 제약 위반 뒤 트랜잭션이 abort 상태라 **롤백 없이는 다시 못 읽는다.**
       부르는 쪽이 이미 롤백한 뒤에 들어온다.
    """
    stored = repository.select_investigation(
        conn, sim_run_id=row.sim_run_id, investigation_id=row.investigation_id
    )
    if stored is None:
        raise InvestigationStateConflict(
            INVESTIGATION_LOST_AFTER_RACE,
            f"{row.investigation_id} 저장이 부딪혔는데 그 행을 다시 못 읽었다"
            " — 무엇과 부딪혔는지 못 대는 채로 «저장됐다» 고 답하지 않는다",
        )
    if not same_investigation_audit(stored, row):
        raise InvestigationStateConflict(
            INVESTIGATION_PAYLOAD_CONFLICT,
            f"{row.investigation_id} 로 **다른 내용의** 조사 기록이 이미 있다"
            " — 저장 재시도가 아니다",
        )
    # ★ 이번 호출의 `result` 를 그대로 싣는다 — 위에서 **감사 내용이 같다는 것을
    #   확인한 뒤**라 저장된 행과 뜻이 어긋날 수 없다. 정본은 언제나 `row`(저장된 값)다.
    return PersistedInvestigation(
        investigation_id=row.investigation_id,
        result=result,
        row=stored,
        status="REUSED",
    )


def same_investigation_audit(stored: InvestigationRow, fresh: InvestigationRow) -> bool:
    """두 감사 기록이 **같은 조사 실행**인가. 🔴 실린 칸이 전부 같아야 한다.

    ```text
    저장 재시도 판정   같은 ID 로 들어온 두 번째 저장이 같은 내용인가 (§36)
    조사 연결 검증     제안이 가리키는 조사가 **이 결과의 저장본**인가
    ```

    ★ **두 질문이 같은 함수를 지난다.** 직렬화(`investigation_row_for`)와 비교를 한 벌로
      두어야 «다르다» 가 실제 차이라고 말할 수 있다.

    ⚠️ DB 에서 돌아온 JSONB 와 파이썬 snapshot 을 그대로 비교하면 `tuple` ↔ `list` ·
       `date` ↔ 문자열 때문에 **거짓 «다름»** 이 난다. 그래서 비교 직전에 `jsonable` 로
       한 번 더 낮춘다 — 값을 바꾸는 것이 아니라 **같은 모양으로 세우는** 것이다.

    ★ `created_at` 은 비교에 안 들어간다 — 애초에 행에 안 싣는다(벽시계다).
    """
    return (
        stored.sim_run_id == fresh.sim_run_id
        and stored.exception_id == fresh.exception_id
        and stored.as_of == fresh.as_of
        and stored.finish_reason == fresh.finish_reason
        and stored.llm_status == fresh.llm_status
        and stored.llm_error_kind == fresh.llm_error_kind
        and stored.observed_as_of == fresh.observed_as_of
        and list(stored.tool_trace) == jsonable(list(fresh.tool_trace))
        and dict(stored.result) == jsonable(dict(fresh.result))
    )


def run_and_persist_investigation(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    exception_id: str,
    investigation_id: str | None = None,
    plan_fn: Any = None,
    finalize_fn: Any = None,
    budget: InvestigationBudget | None = None,
    clock: Any = None,
) -> PersistedInvestigation:
    """조사 한 번을 돌리고 **그 실행을 기억한다.**

    ```text
    ① 조사 실행    run_investigation — 🔴 DB 에 아무것도 안 쓴다 (그대로다)
    ② 되감기       아래 이유
    ③ snapshot     결정론. Tool 0 · LLM 0
    ④ INSERT + commit
    ```

    🔴 **② 를 왜 하나.** 조사는 DB 를 **읽기만** 하는데, 그 읽기가 DB 오류로 터지면
       (`TOOL_FAILED`) PostgreSQL 트랜잭션이 abort 상태로 남는다 — 그 상태에서는 감사
       INSERT 도 못 한다. 그러면 *"조사가 실패했다는 사실"* 만 통째로 사라진다.
       조사는 쓴 것이 없으므로 여기서 되감아도 **잃는 것이 하나도 없고**, 그 대신
       실패한 조사도 기록으로 남는다.
       ⚠️ 그래서 이 함수는 **커넥션의 트랜잭션 주인**이다 — 부르기 전에 커밋하지 않은
          업무 쓰기를 남겨 두면 안 된다.

    🔴 **결과를 못 얻었으면 가짜 행을 안 만든다** (§21). 코드 버그 · 직렬화 실패 ·
       커넥션 붕괴로 `InvestigationResult` 자체가 없으면 `finish_reason='ERROR'` 같은
       것을 지어내 적지 않고 **예외를 그대로 올린다** — 없는 조사 결과를 기록에 남기는
       것보다 부르는 쪽이 터지는 편이 낫다.

    ⚠️ **제안을 여기서 안 만든다** (§32). 제안 생성은 이 커밋이 끝난 **뒤** 부르는 쪽이
       `create_proposal(result=…, investigation_id=…)` 로 한다 — 제안이 안 섰다고
       조사가 없었던 일이 되면 안 되고, 조사가 안 남았으면 제안도 서면 안 된다.
    """
    identifier = investigation_id or repository.new_investigation_id()
    try:
        result = run_investigation(
            conn,
            sim_run_id=sim_run_id,
            as_of=as_of,
            exception_id=exception_id,
            plan_fn=plan_fn,
            finalize_fn=finalize_fn,
            budget=budget,
            clock=clock,
        )
    except Exception:
        conn.rollback()
        raise
    conn.rollback()
    return save_investigation(conn, result=result, investigation_id=identifier)
