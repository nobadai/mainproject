"""
cancellation.py — **승인을 물린다.** `apply_approval` 과 대칭인 트랜잭션 경계.

🔴 **번복하면 앞 승인의 장부가 남는다** (2026-09-05 발견).

```text
승인 A(기본)  decision_seq 1 → purchases D1 · payables AP-…-1 · unsettled += A
번복 B(보수)  decision_seq 2 → purchases D2 · payables AP-…-2 · unsettled += B

decision_seq 가 달라 purchase_id 도 달라 ON CONFLICT 가 안 걸린다 — **둘 다 남는다.**
```

```text
사용자가 한 것       기본을 취소하고 보수를 골랐다
화면이 보여 주는 것   보수 하나          (current_commitment · is_current)
장부에 있는 것        기본 + 보수 둘      🔴
```

★ **`#290` 이 승인 위 승인을 막는 것은 반쪽이다.** `REJECT_ALL` · `REQUEST_CHANGE` 를
  끼우면 뚫린다 (매입 실측 2026-09-06). 진짜 답은 **되돌리는 경로**다.

🔴 **지우지 않고 적는다.**

```text
지우면   그 승인이 있었다는 사실이 사라진다
적으면   "승인했고 취소했다" 가 이력에 남는다
```

★ `master_decisions` 가 append-only 인 이유가 그것이고 **원장도 같다.** 이 모듈은
  어디서도 DELETE 를 내지 않는다 — 상태 칸을 `CANCELLED` 로 **적을** 뿐이다.

★ **다섯 자리가 한 트랜잭션이다.**

  ```text
  ① master_decisions   CANCEL 결정                      마스터 (호출자가 적재)
  ② purchases          settlement_status = CANCELLED    마스터 (`ledger.cancel_purchases`)
  ③ payables           CANCELLED + 취소금액              재무 (`#302`)
  ④ finance_states     unsettled 역분개                  재무 (`#302`)
  ⑤ 물류 fixture       in_transit · confirmed_inbound    물류
  ```

  ⚠️ **다섯이 다 되거나 다 안 되어야 한다.** 재무만 물리고 물류가 남으면 *"돈은 안
    내는데 물건은 오는"* 장부가 된다 — `apply_approval` 이 막는 것과 같은 종류다.

🔴 **취소일과 상태 날짜는 다른 값이고 둘 다 싣는다** (재무 회신 2026-09-06 · `§4`).

  ```text
  commitment.as_of   **원 승인일**    — 취소일로 쓰면 안 된다
  cancelled_on       **취소 사건일**
  target_state_date  cancelled_on + 1 calendar day
  ```

  ★ **승인과 같은 규칙이다** — *"사건이 일어난 날 + 1일"*. 승인도 취소도 사건이고,
    사건은 다음 날 상태에 나타난다.

  ⚠️ **`target_state_date - 1` 로 역산하지 않는다.** 지금은 그 뺄셈이 맞지만 규칙이
    바뀌는 날 취소일이 **조용히 따라 틀린다.** 같은 사실을 두 곳에서 만들지 않는다.

  ★ **과거 상태를 고치지 않는다.** 승인 01-05 · 취소 01-07 이면 01-06 · 01-07 의
    미지급은 그대로 두고 01-08 부터 뺀다 — **그때는 실제로 미지급이 있었다.**

⚠️ **입고된 뒤에는 못 물린다.** 물건이 창고에 있으면 취소가 아니라 반품이다. 재무의
  *"`SETTLED` 는 fail-closed"* 와 같은 성격이고, 판정은 각 파트가 한다 — 이 모듈은
  파트가 거절하면 **통째로 롤백**한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.commitment import ApprovedCommitment
from app.master.ledger import cancel_purchases
from app.master.transition import PARTS, TransitionPart, purchase_id_for

__all__ = [
    "PARTS",
    "ApprovalCancellation",
    "CancellationOut",
    "cancellation_missing",
    "register_cancellation",
    "registered_cancellations",
    "financing_mode_of",
    "undo_approval",
]


class ApprovalCancellation(Protocol):
    """승인분을 자기 장부에서 물리는 방식. **각 부서가 소유한다.**

    ★ **`build` 를 순수하게 나누지 않는다.** `apply_approval` 과 다른 점이다 —
      *"무엇을 물릴 수 있나"* 는 **적힌 사실**이고 부서가 DB 를 읽어야 안다
      (재무는 `payables.status` 를 잠그고 읽는다). `DayOpening` 이 `conn` 을 받는
      것과 같은 이유다.

    ★ **`conn` 은 받기만 한다.** commit·rollback·close 를 하지 않는다 — 트랜잭션
      경계는 마스터가 쥔다.

    🔴 **`financing_mode` 를 마스터가 싣는다** (재무 요청 2026-09-06).

      `finance_states` 정본이 `(sim_run_id, financing_mode, state_date)` 인데 취소
      조회는 `sim_run_id + state_date` 만 보고 있었고, **같은 날짜에 mode 가 둘이면
      ambiguous 로 막힙니다.** 실측으로 `2025-12-31` 하루가 이미 그렇다.

      ★ **재무가 고르지 않겠다고 했고 그것이 맞다.** 고르는 순간 그 선택이 조용히
        굳고, 나중에 누구도 왜 그 축이었는지 못 찾는다 — `purchase_ids` 를 재무가
        지어내면 안 되는 것과 같은 자리다.

      ⚠️ **물류도 같은 인자를 받는다. 안 쓰더라도.** 두 파트가 같은 모양이어야
        호출부가 하나로 서고, `purchase_ids` 를 반쪽으로 뒀다가 물류 Arrival 이
        막힌 자리가 그 교훈이다.

    :param commitment: 무엇을 승인했었나. `as_of` 는 **승인일**이다.
    :param cancelled_on: **취소 사건일.** `commitment.as_of` 와 다를 수 있다.
    :param target_state_date: `cancelled_on + 1일`. 부서가 다시 계산하지 않는다.
    :param purchase_ids: 회차(seq) → purchase_id. 승인 때와 **같은 매핑**이다.
    :param financing_mode: 이 실행의 재무 축. **마스터가 실어 준다** (재무 요청
        2026-09-06) — 부서가 임의로 고르거나 최신 상태를 추론하지 않는다.
    """

    def cancel(
        self,
        conn: Any,
        *,
        commitment: ApprovedCommitment,
        cancelled_on: date,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
        financing_mode: str,
    ) -> None: ...


class CancellationOut(BaseModel):
    """취소 한 번의 결과. **`TransitionOut` 과 같은 세 갈래다.**

    ```text
    CANCELLED     다섯 자리가 다 물렸다
    NOT_APPLIED   아직 그 부서가 안 돈다 — 물릴 방법이 없었다
    FAILED        물리려다 실패했다 — **아무것도 안 바뀌었다**
    ```

    ⚠️ `NOT_APPLIED` 를 `FAILED` 로 접으면 미구현이 장애로 읽히고, 반대로 접으면
      실패한 취소가 *"안 돌았다"* 로 조용히 묻힌다.
    """

    status: Literal["CANCELLED", "NOT_APPLIED", "FAILED"]
    reason: str = ""
    #: 실제로 write 를 낸 파트. `CANCELLED` 가 아니면 비어 있다.
    parts: list[str] = Field(default_factory=list)
    #: 아직 취소 구현이 없는 파트.
    missing: list[str] = Field(default_factory=list)
    #: 물린 매입 원장 header 수. 이미 취소된 것을 다시 물면 **0** 이다 (멱등).
    cancelled_purchases: int = 0


# ── 등록소 ──────────────────────────────────────────────────────────────
#
# ★ **전이 등록소와 따로 둔다.** 같은 사전에 넣으면 *"전이는 되는데 취소는 안 되는"*
#   상태를 표현할 수 없다. 실제로 지금이 그 상태다 — 전이는 둘 다 섰고 취소는 재무만
#   섰다.

_CANCELLATIONS: dict[TransitionPart, Any] = {}


def register_cancellation(part: TransitionPart, impl: Any) -> None:
    """취소 구현을 등록한다. 재무·물류 모듈이 임포트 시점에 부른다."""
    if part not in PARTS:
        raise ValueError(f"취소 파트가 아니다: {part!r}. 가능: {', '.join(PARTS)}")
    _CANCELLATIONS[part] = impl


def registered_cancellations() -> Mapping[TransitionPart, Any]:
    """지금 등록된 취소 구현. **읽기용 사본**이다."""
    return dict(_CANCELLATIONS)


def cancellation_missing() -> tuple[str, ...]:
    """아직 취소 구현이 없는 파트. **`PARTS` 순서를 지킨다.**"""
    return tuple(part for part in PARTS if part not in _CANCELLATIONS)


def reset() -> None:
    """등록을 비운다. 검사용이다."""
    _CANCELLATIONS.clear()


# ── 경계 ────────────────────────────────────────────────────────────────


def purchase_ids_of(commitment: ApprovedCommitment) -> dict[int, str]:
    """이 승인이 만든 회차별 매입 header ID. **승인 때와 같은 매핑이다.**

    ★ **새로 조회하지 않는다.** `purchase_id_for` 가 결정론이라 취소 시점에 다시
      조립해도 같은 값이 나온다 — 표를 읽으면 *"원장이 말하는 것"* 과 *"약정이
      말하는 것"* 이 갈릴 자리가 하나 더 생긴다.

    ★ 재무가 요구한 계약이다 (`#302 §3`) — *"payable_id 문자열 파싱도, approval_id
      에서의 추론도, 첫 회차 ID 임의 선택도 하지 않겠습니다."*
    """
    return {leg.seq: purchase_id_for(commitment, leg.seq) for leg in commitment.arrival_schedule}


def financing_mode_of(commitment: ApprovedCommitment) -> str:
    """이 승인이 속한 실행의 재무 축.

    ★ **마스터가 이미 읽고 나르는 값이다.** `sim_runs.financing_mode` 를
      `ledger_repository` 가 읽고 `service.py` 가 응답에 싣는다 — 지어내는 값이
      아니라서 재무가 요청한 *"호출자가 축을 명시"* 가 성립한다.

    ⚠️ `sim_run_id_for` 와 같은 자리에서 온다. 그 함수가 *"마스터가 이미 소유한
      하나뿐인 포인터"* 를 쓰므로 여기도 같은 실행을 가리킨다.

    :raises LookupError: 그 실행을 못 찾을 때. **지어내지 않는다.**
    """
    from app.master.ledger import sim_run_id_for
    from app.master.ledger_repository import get_burn_in

    run = get_burn_in(sim_run_id_for(commitment))
    mode = run.get("financing_mode")
    if not isinstance(mode, str) or not mode.strip():
        raise LookupError(
            f"sim_runs.financing_mode 를 읽을 수 없다 ({sim_run_id_for(commitment)}) —"
            " 축을 지어내지 않는다"
        )
    return mode


def undo_approval(
    commitment: ApprovedCommitment,
    *,
    cancelled_on: date,
    connect: Any = None,
) -> CancellationOut:
    """승인 하나를 다섯 자리에서 **한 트랜잭션으로** 물린다.

    ★ **`apply_approval` 과 대칭이다** — 예외를 밖으로 내지 않고 값으로 돌려준다.
      취소 실패가 적재된 결정을 지우면, 사람이 취소를 눌렀다는 사실이 사라진다.

    ⚠️ **`cancelled_on` 이 승인일보다 앞설 수 없다.** 앞서면 *"승인하기 전에
      취소했다"* 가 장부에 남는다 — 그건 날짜를 잘못 넘긴 것이지 사건이 아니다.

    :param cancelled_on: **취소 사건일.** `commitment.as_of`(승인일)와 다를 수 있다.
    """
    if cancelled_on < commitment.as_of:
        return CancellationOut(
            status="FAILED",
            reason=(
                f"취소일({cancelled_on.isoformat()})이 승인일"
                f"({commitment.as_of.isoformat()})보다 앞선다 — 날짜가 잘못 왔다"
            ),
        )

    absent = cancellation_missing()
    if absent:
        # ★ **어댑터 미등록은 오류가 아니다.** 그 부서가 아직 취소를 안 한다는 뜻이고,
        #   그때 마스터가 자기 원장만 물리면 **반쪽 취소**가 된다.
        return CancellationOut(
            status="NOT_APPLIED",
            reason=f"취소 어댑터 미등록: {', '.join(absent)}",
            missing=list(absent),
        )

    adapters = registered_cancellations()
    try:
        # ★ **커넥션을 열기 전에 읽는다.** 축을 못 읽으면 트랜잭션을 시작하지도 않는다 —
        #   `apply_approval` 이 build 를 커넥션 밖에서 부르는 것과 같은 규율이다.
        financing_mode = financing_mode_of(commitment)
    except Exception as exc:  # noqa: BLE001 - 축을 못 읽은 것도 값으로 돌려준다.
        return CancellationOut(status="FAILED", reason=f"재무 축을 못 읽었다: {exc}")
    # 🔴 **취소일 + 1일이다.** 승인과 **같은 규칙**이고, 부서가 다시 계산하지 않게
    #    마스터가 실어 준다 (재무 요청 2026-09-06).
    target_state_date = cancelled_on + timedelta(days=1)
    purchase_ids = purchase_ids_of(commitment)

    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        # ★ **매입 원장을 먼저 물린다.** 승인이 부모를 먼저 세운 것과 같은 순서다 —
        #   읽는 사람이 두 경로를 나란히 볼 수 있어야 한다.
        cancelled = cancel_purchases(conn, purchase_ids.values())
        for part in PARTS:
            adapters[part].cancel(
                conn,
                commitment=commitment,
                cancelled_on=cancelled_on,
                target_state_date=target_state_date,
                purchase_ids=purchase_ids,
                financing_mode=financing_mode,
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 취소 실패가 적재된 결정을 지우면 안 된다.
        conn.rollback()
        return CancellationOut(status="FAILED", reason=f"취소 적재 실패: {exc}")
    finally:
        conn.close()
    return CancellationOut(
        status="CANCELLED", parts=list(PARTS), cancelled_purchases=cancelled
    )
