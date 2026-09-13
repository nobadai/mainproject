"""승인된 대응안을 **실제로 돌리고, 돌아갔는지 되읽는다** (#628 Commit 6).

```text
APPROVED ──▶ 실행 직전 재검증 ──▶ Act (도메인 정본 함수) ──▶ Verify (되읽기)
                  │                      │                        │
              STALE_ACTION            거부/실패                  불일치
                  ▼                      ▼                        ▼
                FAILED                 FAILED                   FAILED
                                                                  │ 일치
                                                                  ▼
                                                              EXECUTED
```

🔴 **«요청을 접수시켰다» 는 «실행됐다» 가 아니다.** 이 파일이 `EXECUTED` 를 적는 것은
   도메인 정본 함수가 실제로 장부를 바꾸고 **그 결과를 되읽어 확인했을 때**뿐이다.

🔴 **실행 경계를 지어내지 않는다.** 타 부서에 «이 요청을 받아라» 는 공식 진입점이
   없으면 그 행동은 **실행하지 않고** 제안을 `APPROVED` 로 남긴다 — 없는 계약을 만들어
   `EXECUTED` 라고 적는 것보다 승인 상태로 멈추는 편이 정직하다.

```text
ACCEPT_RISK        ✅ logistics_exceptions.risk_accepted_as_of      (물류 소유)
DISPOSAL_REQUEST   ✅ disposal.confirm_disposal                      (물류 소유 · 원장 정본)
SALES_PRIORITY_…   ⬜ 판매에 «우선판매 요청» 을 받는 진입점이 없다
PURCHASE_ADJUST_…  ⬜ 예정 입고 수량을 조정하는 writer 가 없다
```

🔴 **LLM 이 여기 없다.** Act 도 Verify 도 결정론이다 — *"실행이 잘 된 것 같나요"* 를
   모델에게 묻지 않는다.

🔴 **새 계산기를 만들지 않는다.** 폐기 가능량도 신선도도 잔량도 전부 기존 도메인 정본
   (`disposal.confirm_disposal` · `agent.tools.get_lot`)이 답한다. 이 파일에 `UPDATE
   inventory_lots` 도 `INSERT inventory_moves` 도 한 줄 없다.

⚠️ **Exception 을 여기서 닫지 않는다.** 실행됐다고 문제가 사라진 것이 아니다 — 조건이
   실제로 사라졌는지는 기존 탐지기(하루의 물류 점검 칸)가 다음 번에 판정한다 (§58).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.logistics.agent import proposals as repository
from app.logistics.agent.investigation import jsonable
from app.logistics.agent.proposal_service import (
    ProposalNotFound,
    ProposalStateConflict,
)
from app.logistics.agent.proposals import ProposalRow
from app.logistics.agent.schemas import LIVE_STATUSES as LIVE_EXCEPTION_STATUSES
from app.logistics.agent.tools import get_lot
from app.logistics.disposal import (
    DisposalBlocked,
    DisposalError,
    DisposalIntegrityError,
    InvalidDisposalRequest,
    confirm_disposal,
)

__all__ = [
    "ACTION_NOT_EXECUTABLE",
    "ACTION_PARAMETERS_INCOMPLETE",
    "ACT_OWNER_REJECTED",
    "DISPOSAL_REASON_CODE",
    "STALE_ACTION",
    "VERIFY_RESULT_MISMATCH",
    "ExecutionOutcome",
    "ExecutionRefused",
    "ExecutionUnsupported",
    "execute_approved_proposal",
]


# ══════════════════════════════════════════════════════════════════════════
#  실패 어휘 — 🔴 «모른다» 를 «실패» 로 적지 않는다
# ══════════════════════════════════════════════════════════════════════════

#: 승인 뒤 사실이 바뀌어 **그 행동이 더는 성립하지 않는다.** 업무 표 변경 0.
STALE_ACTION = "STALE_ACTION"
#: 도메인 정본 함수가 **명시적으로 거부**했다 (폐기 근거 없음 · 한도 초과 …).
ACT_OWNER_REJECTED = "ACT_OWNER_REJECTED"
#: 제안 인자에 실행에 필요한 값이 없다. 🔴 없는 값을 지어내 채우지 않는다.
ACTION_PARAMETERS_INCOMPLETE = "ACTION_PARAMETERS_INCOMPLETE"
#: Act 는 끝났는데 **되읽은 사실이 다르다.** 🔴 그때 `EXECUTED` 를 적지 않는다.
VERIFY_RESULT_MISMATCH = "VERIFY_RESULT_MISMATCH"
#: 🔴 이 행동을 실제로 돌릴 **공식 경계가 없다.** 실패가 아니라 **아직 못 하는 것**이라
#:    제안을 `APPROVED` 로 남긴다 — 없는 계약을 만들어 «실행됨» 이라고 적지 않는다.
ACTION_NOT_EXECUTABLE = "ACTION_NOT_EXECUTABLE"

#: 🔴 **물류가 업무 어휘를 짓지 않는다** — 그런데 `inventory_moves.reason_code` 에는
#:    CHECK 이 없고 기존 값은 씨앗 보정용뿐이다. 그래서 *"이 폐기가 어디서 왔나"* 를
#:    가리키는 최소한의 출처 문자열만 쓴다. 사람이 고른 사유가 생기면 그것이 이긴다.
DISPOSAL_REASON_CODE = "LOGISTICS_AGENT_DISPOSAL_PROPOSAL"


class ExecutionRefused(RuntimeError):
    """실행이 **확정적으로** 안 된다. 제안은 `FAILED` 가 된다.

    🔴 *"됐는지 모르겠다"* 에는 쓰지 않는다 — 모르는 것을 실패로 적으면 재시도가
       이중 실행을 낳는다 (§29 · §54).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExecutionUnsupported(RuntimeError):
    """이 행동을 돌릴 **공식 경계가 아직 없다.** 제안은 `APPROVED` 로 남는다.

    ★ `ExecutionRefused` 와 다른 사실이다 — 저쪽은 *"이 제안은 안 된다"* 이고 이쪽은
      *"우리가 아직 못 한다"* 다. `FAILED` 로 적으면 제안이 terminal 이 되어, 나중에
      계약이 생겨도 그 제안은 영영 못 돌린다.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


OutcomeStatus = Literal["EXECUTED", "FAILED", "ALREADY_EXECUTED", "BLOCKED"]


@dataclass(frozen=True)
class ExecutionOutcome:
    """실행을 시도한 결과. **예외 대신 이것을 돌려준다.**

    ```text
    EXECUTED          실제로 돌았고 되읽어 확인했다
    ALREADY_EXECUTED  같은 요청이 이미 돌았다 — 부작용 0 (재시도다)
    FAILED            확정적으로 안 됐다. 업무 표 변경 0
    BLOCKED           돌릴 경계가 없다. 🔴 제안은 `APPROVED` 그대로다
    ```
    """

    status: OutcomeStatus
    proposal: ProposalRow | None = None
    failure_code: str = ""
    failure_reason: str = ""
    result: Mapping[str, Any] = field(default_factory=dict)

    @property
    def executed(self) -> bool:
        return self.status in ("EXECUTED", "ALREADY_EXECUTED")


# ══════════════════════════════════════════════════════════════════════════
#  행동별 실행 — 🔴 명시적 표. eval · reflection · 동적 import 없음
# ══════════════════════════════════════════════════════════════════════════


def _accept_risk(
    conn: Any, *, proposal: ProposalRow, as_of: date, executed_by: str
) -> Mapping[str, Any]:
    """위험을 안고 가기로 한 사실을 **Exception 에 적는다.**

    🔴 **Exception 을 `RESOLVED` 로 만들지 않는다** (§39). 위험을 수용한 것이지 위험이
       사라진 것이 아니다 — 재탐지는 계속 돈다 (상세설계 §7.1 F).

    ★ Verify 는 되읽기다: `risk_accepted_as_of` 가 실제로 표에 섰는지 확인한다.
    """
    changed = repository.accept_exception_risk(
        conn,
        sim_run_id=proposal.sim_run_id,
        exception_id=proposal.exception_id,
        as_of=as_of,
    )
    if changed != 1:
        raise ExecutionRefused(
            STALE_ACTION,
            f"{proposal.exception_id} 가 살아 있는 문제가 아니게 됐다 — 위험 수용을 적지 않는다",
        )

    # ── Verify — 함수가 안 터졌다가 아니라 **표를 다시 읽는다** ──────────
    accepted = repository.exception_risk_accepted_as_of(
        conn, sim_run_id=proposal.sim_run_id, exception_id=proposal.exception_id
    )
    if accepted is None:
        raise ExecutionRefused(
            VERIFY_RESULT_MISMATCH,
            f"{proposal.exception_id} 를 되읽었는데 risk_accepted_as_of 가 비어 있다",
        )
    return {
        "action": "ACCEPT_RISK",
        "owner": "LOGISTICS",
        "reference_id": proposal.exception_id,
        "result": "RISK_ACCEPTED",
        # ⚠️ **처음 수용한 날**이다 — 재실행이 날짜를 덮지 않는다(`COALESCE`).
        "risk_accepted_as_of": accepted.isoformat(),
    }


def _dispose(
    conn: Any, *, proposal: ProposalRow, as_of: date, executed_by: str
) -> Mapping[str, Any]:
    """폐기를 **기존 정본 함수로** 실행한다.

    🔴 **`disposal.confirm_disposal` 이 재고를 없애는 유일한 경로다.** 이 파일에 잔량을
       고치는 SQL 이 한 줄도 없고, 폐기 가능량 계산기도 새로 만들지 않는다 —
       *"지금 이 Lot 을 이만큼 폐기할 수 있나"* 는 그 함수가 쓰기 전에 전부 판정한다
       (폐기대기 근거 · 살아 있는 할당 보호 · 한도). 그 판정이 곧 **실행 직전 재검증**이다.

    🔴 **멱등 키가 `proposal_id` 다** (§26 · §45). 같은 제안을 두 번 실행해도 Move 는
       하나다 — 그 함수가 `MOVE-DISPOSE-{proposal_id}` 로 이미 적힌 폐기를 알아본다.

    ★ Verify 는 **Commit 3 의 읽기 Tool 로 되읽는다** (`get_lot`). 폐기 함수가 돌려준
      잔량과 원장이 말하는 잔량이 같아야 `EXECUTED` 다.
    """
    lot_id = proposal.parameters.get("lot_id")
    if not isinstance(lot_id, str) or not lot_id.strip():
        raise ExecutionRefused(
            ACTION_PARAMETERS_INCOMPLETE,
            f"{proposal.proposal_id} 에 폐기 대상 lot_id 가 없다 — 대상을 지어내지 않는다",
        )
    quantity = _quantity(proposal.parameters.get("qty_kg"))
    if quantity is None:
        raise ExecutionRefused(
            ACTION_PARAMETERS_INCOMPLETE,
            f"{proposal.proposal_id} 에 폐기 수량이 없다 — 수량을 지어내지 않는다",
        )

    try:
        disposed = confirm_disposal(
            conn,
            # 🔴 제안 하나가 폐기 하나다 — 재실행이 두 번째 폐기가 되지 않는다.
            disposal_id=proposal.proposal_id,
            sim_run_id=proposal.sim_run_id,
            lot_id=lot_id,
            quantity_kg=quantity,
            disposed_at=as_of,
            reason_code=DISPOSAL_REASON_CODE,
            as_of=as_of,
            note=f"{proposal.exception_id} / {proposal.proposal_id}",
        )
    except (DisposalBlocked, InvalidDisposalRequest) as error:
        # ⚠️ 쓰기 **전에** 막힌다 — 업무 표는 한 줄도 안 바뀌었다.
        raise ExecutionRefused(
            STALE_ACTION,
            f"승인 뒤 사실이 바뀌어 폐기가 성립하지 않는다: {error}",
        ) from error
    except DisposalIntegrityError as error:
        raise ExecutionRefused(ACT_OWNER_REJECTED, str(error)) from error
    except DisposalError as error:  # pragma: no cover - 위 셋이 이 모듈의 전부다.
        raise ExecutionRefused(ACT_OWNER_REJECTED, str(error)) from error

    # ── Verify — 원장이 실제로 그렇게 말하나 ──────────────────────────
    view = get_lot(conn, sim_run_id=proposal.sim_run_id, as_of=as_of, lot_id=lot_id)
    if view.lot is None:
        raise ExecutionRefused(
            VERIFY_RESULT_MISMATCH,
            f"폐기 뒤 {lot_id} 를 원장에서 되읽지 못했다",
        )
    if view.lot.remaining_qty_kg != disposed.remaining_qty_kg:
        raise ExecutionRefused(
            VERIFY_RESULT_MISMATCH,
            f"폐기 함수는 잔량 {disposed.remaining_qty_kg} 라는데 원장은"
            f" {view.lot.remaining_qty_kg} 다 — 숫자가 갈리면 실행됨으로 적지 않는다",
        )
    return {
        "action": "DISPOSAL_REQUEST",
        "owner": "LOGISTICS",
        # 🔴 정본 행을 가리킨다 — 이 값으로 원장에서 그 폐기를 찾을 수 있다.
        "reference_id": disposed.move_id,
        "result": "DISPOSED" if disposed.applied else "ALREADY_DISPOSED",
        "lot_id": lot_id,
        # ⚠️ 전부 정본이 낸 값이다. 여기서 셈한 수는 하나도 없다.
        "disposed_qty_kg": str(disposed.disposed_qty_kg),
        "remaining_qty_kg": str(view.lot.remaining_qty_kg),
        "lot_status": disposed.lot_status,
    }


def _sales_priority(
    conn: Any, *, proposal: ProposalRow, as_of: date, executed_by: str
) -> Mapping[str, Any]:
    """⬜ **판매에 요청을 넘기는 공식 경계가 없다.**

    조사 결과 (2026-09-13 · 현재 HEAD):

    ```text
    판매 쓰기 정본     sales.persistence.confirm_sale
    그 유일한 호출자   master.sales_approval.confirm_approved_sale
    그 함수의 입력     request_id · run_id · **재검증을 통과한 시나리오** ·
                      재무 기여이익 요약
    ```

    즉 판매 원장에 닿는 길은 *"사람이 돌린 판매 판단 실행이 승인까지 갔을 때"* 뿐이고,
    «이 Lot 을 우선 팔아 달라» 는 **요청을 받는 진입점도, 그 요청을 적어 둘 표도 없다**
    (`logistics_tasks` 류는 Core 에 없다 — 상세설계 §11).

    🔴 물류가 그 길을 흉내 내려면 판매 시나리오·가격·거래처·수량을 **직접 지어내야**
       한다 — 그것은 이 Agent 가 처음부터 하지 않기로 한 일이다 (§30 · 역할 경계 §9.1).

    ★ 그래서 실행하지 않고 제안을 `APPROVED` 로 남긴다. 필요한 최소 계약은 보고서에
      적었다 — 만들기 전에 판매 소유 팀이 정할 일이다.
    """
    raise ExecutionUnsupported(
        ACTION_NOT_EXECUTABLE,
        "판매에 «우선판매 요청» 을 넘기는 공식 진입점이 없다"
        " (판매 쓰기는 master.confirm_approved_sale 이 승인된 판매 실행의 꼬리에서만 연다)."
        " 요청을 적어 둘 표도 없다 — 계약을 지어내지 않고 승인 상태로 멈춘다.",
    )


def _purchase_adjust(
    conn: Any, *, proposal: ProposalRow, as_of: date, executed_by: str
) -> Mapping[str, Any]:
    """⬜ **예정 입고 수량을 조정하는 writer 가 없다.**

    조사 결과 (2026-09-13 · 현재 HEAD):

    ```text
    inbound_schedules.record_schedule   승인된 매입 확약에서 **새로 적는다**   (transition.py)
    inbound_schedules.cancel_schedule   그 일정을 **통째로 취소한다**          (cancellation.py)
    수량을 줄이는 writer                 **없다**
    ```

    제안의 `qty_delta_kg` 는 *"이만큼 줄여 달라"* 인데, 그 뜻을 받는 함수가 없다.
    마스터의 `REQUEST_CHANGE` · `CANCEL` 은 **매입 실행 요청(`request_id`)** 을 축으로
    돌고, 제안은 그 축을 갖고 있지 않다.

    🔴 물류가 `UPDATE inbound_schedules SET quantity_kg = …` 를 새로 만들면 그 순간
       **분할 회차 · 발주 수량 · 실제 매입일을 물류가 정하는 것**이 된다 (§33 금지).
       `cancel_schedule` 로 통째로 지우는 것도 «조정» 이 아니다 — 그 판단은 매입 것이다.

    ★ 그래서 실행하지 않고 제안을 `APPROVED` 로 남긴다.
    """
    raise ExecutionUnsupported(
        ACTION_NOT_EXECUTABLE,
        "예정 입고 수량을 조정하는 공식 writer 가 없다"
        " (record_schedule=신규 · cancel_schedule=전체 취소뿐이고 수량 조정은 없다)."
        " 마스터 REQUEST_CHANGE/CANCEL 은 매입 실행 요청 축이라 제안이 가리킬 수 없다 —"
        " 계약을 지어내지 않고 승인 상태로 멈춘다.",
    )


#: 🔴 **명시적 표다.** `eval` · reflection · 동적 import · LLM routing · 문자열 SQL 없음.
ACTION_EXECUTORS: Mapping[str, Any] = {
    "ACCEPT_RISK": _accept_risk,
    "DISPOSAL_REQUEST": _dispose,
    "SALES_PRIORITY_REQUEST": _sales_priority,
    "PURCHASE_ADJUST_REQUEST": _purchase_adjust,
}


# ══════════════════════════════════════════════════════════════════════════
#  진입점 — 🔴 트랜잭션의 주인이다
# ══════════════════════════════════════════════════════════════════════════


def execute_approved_proposal(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    as_of: date,
    executed_by: str,
) -> ExecutionOutcome:
    """승인된 대응안 하나를 실행한다. **Logistics 가 소유하는 파이썬 진입점**이다.

    ```text
    ① 제안 조회            없으면 ProposalNotFound
    ② 실행 가능 상태        APPROVED 만. 이미 EXECUTED 면 그 결과를 그대로 돌려준다
    ③ 실행 직전 재검증      🔴 승인 때 본 사실이 아직 참인가
    ④ action dispatch      명시적 표
    ⑤ Act + Verify         도메인 정본 함수 → 되읽기
    ⑥ 결과 기록            WHERE status='APPROVED' 조건부 UPDATE
    ```

    🔴 **Scheduler 가 이것을 자동으로 부르지 않는다** (§64). 붙이는 것은 후속 Commit 이고,
       Master `status_flow.py` 도 이번에 손대지 않는다.

    🔴 **자동 재시도가 없다** (§10). `FAILED` 가 된 제안을 이 함수가 다시 돌리지 않는다 —
       «실행됐는지 모르는 상태» 를 실패로 적고 재시도하면 이중 실행이 난다.

    :param as_of: 시뮬레이션 영업일. 🔴 `date.today()` 를 안 쓴다 (§73).
    :param executed_by: 실제로 돌린 주체. ⚠️ `decision_owner` 와 다른 축이다.
    :returns: 실행했나 · 이미 돌았나 · 확정 실패인가 · 돌릴 경계가 없나.
    """
    if not executed_by.strip():
        raise ValueError("executed_by 가 비었다 — 누가 돌렸는지 못 대는 실행은 안 적는다")

    row = repository.select_proposal(conn, sim_run_id=sim_run_id, proposal_id=proposal_id)
    if row is None:
        raise ProposalNotFound(f"{sim_run_id} 에 {proposal_id} 가 없다")
    if row.status == "EXECUTED":
        # ★ 같은 요청이 한 번 더 왔다 — 부작용 0 으로 그때 결과를 그대로 돌려준다 (§27).
        return ExecutionOutcome(
            status="ALREADY_EXECUTED", proposal=row, result=row.execution_result
        )
    if row.status != "APPROVED":
        raise ProposalStateConflict(
            "STATE_CONFLICT",
            f"{proposal_id} 는 {row.status} 라 실행할 수 없다 — 실행은 APPROVED 에서만이다",
        )
    if row.approved_as_of is not None and as_of < row.approved_as_of:
        raise ValueError(
            f"as_of({as_of}) 가 승인일({row.approved_as_of}) 보다 앞선다 —"
            " 승인되기 전에 실행할 수 없다"
        )

    handler = ACTION_EXECUTORS.get(row.action_type)
    if handler is None:  # pragma: no cover - 카탈로그는 DB CHECK 이 이미 닫았다.
        return ExecutionOutcome(
            status="BLOCKED",
            proposal=row,
            failure_code=ACTION_NOT_EXECUTABLE,
            failure_reason=f"{row.action_type} 을 돌리는 자리가 없다",
        )

    try:
        _refuse_if_stale(conn, row=row)
        result = handler(conn, proposal=row, as_of=as_of, executed_by=executed_by)
    except ExecutionUnsupported as blocked:
        # 🔴 **아무것도 안 쓴다.** 제안은 `APPROVED` 로 남아 계약이 생기면 그때 돈다.
        conn.rollback()
        return ExecutionOutcome(
            status="BLOCKED",
            proposal=row,
            failure_code=blocked.code,
            failure_reason=str(blocked),
        )
    except ExecutionRefused as refused:
        # 🔴 **업무 mutation 을 먼저 되돌린다.** 확정 실패는 제안에만 남는다.
        conn.rollback()
        return _record(
            conn,
            row=row,
            as_of=as_of,
            executed_by=executed_by,
            executed=False,
            failure_code=refused.code,
            failure_reason=str(refused),
        )
    except Exception:
        conn.rollback()
        raise

    return _record(
        conn, row=row, as_of=as_of, executed_by=executed_by, executed=True, result=result
    )


def _refuse_if_stale(conn: Any, *, row: ProposalRow) -> None:
    """승인 때 본 사실이 **아직 참인가** (§11).

    ```text
    Exception 이 있나            없으면 가리킬 문제가 사라졌다
    같은 실행 축인가              복합 FK 가 이미 막지만 한 번 더 본다
    아직 살아 있나 (OPEN·PROPOSED) 이미 닫힌 문제에 대응을 실행하지 않는다
    ```

    ⚠️ 행동별 재검증은 **도메인 정본 함수가 한다** — 폐기 가능량도 신선도도 그쪽이 이미
       쓰기 전에 판정한다. 여기서 같은 계산을 두 벌로 두지 않는다.
    """
    status = repository.exception_status(
        conn, sim_run_id=row.sim_run_id, exception_id=row.exception_id
    )
    if status is None:
        raise ExecutionRefused(
            STALE_ACTION, f"{row.exception_id} 가 {row.sim_run_id} 에 없다"
        )
    if status not in LIVE_EXCEPTION_STATUSES:
        raise ExecutionRefused(
            STALE_ACTION,
            f"{row.exception_id} 가 {status} 다 — 이미 닫힌 문제에 대응을 실행하지 않는다",
        )


def _record(
    conn: Any,
    *,
    row: ProposalRow,
    as_of: date,
    executed_by: str,
    executed: bool,
    result: Mapping[str, Any] | None = None,
    failure_code: str = "",
    failure_reason: str = "",
) -> ExecutionOutcome:
    """결과를 제안에 적고 커밋한다. 🔴 **`WHERE status='APPROVED'` 가 경합을 막는다.**

    ⚠️ `0` 행이면 남이 먼저 같은 제안을 실행했다. 그때는 **되돌리고 다시 읽어** 뜻을
       정한다 — 이미 `EXECUTED` 면 그것이 답이다(부작용은 정본 함수의 멱등이 막았다).
    """
    try:
        changed = repository.record_execution_outcome(
            conn,
            sim_run_id=row.sim_run_id,
            proposal_id=row.proposal_id,
            as_of=as_of,
            executed_by=executed_by,
            executed=executed,
            result=jsonable(dict(result or {})),
            failure_code=failure_code or None,
            failure_reason=failure_reason or None,
        )
        if changed == 0:
            conn.rollback()
            return _settle_after_race(conn, row=row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    decided = repository.select_proposal(
        conn, sim_run_id=row.sim_run_id, proposal_id=row.proposal_id
    )
    if decided is None:  # pragma: no cover - 방금 1 행을 바꾸고 커밋했다.
        raise ProposalNotFound(f"{row.sim_run_id} 에 {row.proposal_id} 가 없다")
    return ExecutionOutcome(
        status="EXECUTED" if executed else "FAILED",
        proposal=decided,
        failure_code=failure_code,
        failure_reason=failure_reason,
        result=decided.execution_result,
    )


def _settle_after_race(conn: Any, *, row: ProposalRow) -> ExecutionOutcome:
    """남이 먼저 옮겼다 — 다시 읽어 뜻을 정한다.

    🔴 **부작용이 둘이 되지는 않았다.** 도메인 정본 함수가 멱등이라(폐기는 Move ID,
       위험 수용은 `COALESCE`) 같은 제안을 두 worker 가 돌려도 장부에는 한 번만 적힌다.
    """
    fresh = repository.select_proposal(
        conn, sim_run_id=row.sim_run_id, proposal_id=row.proposal_id
    )
    if fresh is None:
        raise ProposalNotFound(f"{row.sim_run_id} 에 {row.proposal_id} 가 없다")
    if fresh.status == "EXECUTED":
        return ExecutionOutcome(
            status="ALREADY_EXECUTED", proposal=fresh, result=fresh.execution_result
        )
    raise ProposalStateConflict(
        "STATE_CONFLICT",
        f"{row.proposal_id} 를 실행하는 사이에 남이 {fresh.status} 로 옮겼다",
    )


def _quantity(value: Any) -> Decimal | None:
    """제안이 적어 둔 수량을 `Decimal` 로. 🔴 **값을 만들지 않는다** — 없으면 `None`."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | str):
        try:
            return Decimal(str(value))
        except ArithmeticError:
            return None
    return None
