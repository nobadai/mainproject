"""
transition.py — 승인 → 상태전이의 **트랜잭션 경계** (C 형태 ⑦)

사람이 매입안을 승인하면 `commitment.py` 가 확정 입고 약정을 만든다. 그 약정이
재무 현금 장부와 물류 재고 장부를 **실제로 바꾸는** 자리가 여기다.

```text
승인 → ApprovedCommitment → [ 재무 write · 물류 write ] → 한 커밋
                             └────────── 이 파일이 감싼다 ──────────┘
```

★ **마스터는 무슨 값을 어느 칸에 쓸지 모른다.** 그것은 재무·물류가 소유한다.
  각 파트가 `build`(순수 계산)와 `persist(conn, ...)`(주어진 커넥션으로 write)를
  내고, 마스터는 **언제 부를지와 언제 커밋할지**만 정한다.

  ```text
  무슨 값을 어느 칸에 어떤 SQL 로   재무 · 물류 소유
  언제 · 한 트랜잭션으로 커밋        마스터 소유 ← 이 파일
  ```

🔴 **이 모듈에 SQL 이 있으면 그 분담이 무너진다.** 여기에 `INSERT` 를 한 줄이라도
   적는 순간 마스터가 `finance_states` 의 칸 이름을 알게 되고, 재무가 칸을 바꿀 때
   조용히 어긋난다. `test_transition_boundary.py` 가 원문을 읽어 그것을 막는다.

★ **두 파트를 한 커밋으로 묶는 이유.** 재무만 커밋되고 물류가 터지면, 현금은
  나갔는데 입고 예정은 없는 장부가 된다 — 그 상태를 **아무도 틀렸다고 말해 주지
  않는다.** 둘 다 되거나 둘 다 안 되어야 한다.

★ **어댑터 미등록은 오류가 아니라 상태다** (`wiring.py` · `service.py` 와 같은 태도).
  재무·물류 구현이 아직 없는 지금 이 경로가 500 을 내면, 승인 자체가 막힌다.
  그건 *"그 부서가 오늘 돌지 않는다"* 이지 승인이 실패한 것이 아니다.

🔴 **`with conn:` 을 쓰지 않는다.** psycopg3 의 커넥션 컨텍스트 매니저는 블록이
   정상 종료하면 **자동으로 commit** 한다. 그러면 "커밋은 마스터가 한 번만 한다"는
   이 파일의 유일한 일이 문법에 숨어 버리고, 변이 검사(커밋 지우기)도 안 걸린다.
   commit · rollback · close 를 눈에 보이게 적는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.commitment import ApprovedCommitment

__all__ = [
    "PARTS",
    "FinanceTransition",
    "LogisticsTransition",
    "TransitionOut",
    "TransitionPart",
    "apply_approval",
    "missing",
    "register_transition",
    "registered",
    "reset",
]

TransitionPart = Literal["finance", "logistics"]

#: 전이에 참여하는 파트와 **호출 순서**. 재무가 먼저다 — 현금이 모자라 재무가 터지면
#: 재고 쪽은 손도 대지 않은 채 롤백된다.
PARTS: tuple[TransitionPart, ...] = ("finance", "logistics")


class FinanceTransition(Protocol):
    """승인분이 현금 장부를 바꾸는 방식. **재무가 소유한다.**

    ★ `build` 는 순수 계산이고 `persist` 는 write 다. 나누는 이유는 하나다 —
      계산이 실패하면 **DB 를 열지도 않은 채** 멈출 수 있어야 한다.

    ★ `persist` 는 **commit 하지 않는다.** 커밋은 두 파트가 모두 끝난 뒤
      `apply_approval` 이 한 번 한다.
    """

    def build(self, commitment: ApprovedCommitment, as_of: date) -> object: ...
    def persist(self, conn: Any, row: object) -> None: ...


class LogisticsTransition(Protocol):
    """승인분이 재고 장부를 바꾸는 방식. **물류가 소유한다.**

    ★ 재무와 달리 여러 행이 나온다 — 회차별 입고가 각각 로트/이동이 된다.
      몇 행인지도 물류가 정한다.
    """

    def build(self, commitment: ApprovedCommitment) -> Sequence[object]: ...
    def persist(self, conn: Any, rows: Sequence[object]) -> None: ...


class TransitionOut(BaseModel):
    """승인 1건의 상태전이 결과.

    🔴 **세 값을 섞지 않는다.**

      ```text
      APPLIED       두 장부가 바뀌었다
      NOT_APPLIED   아직 그 부서가 안 돈다 — 바꿀 것이 없었다
      FAILED        바꾸려다 실패했다 — 아무것도 안 바뀌었다
      ```

      `NOT_APPLIED` 를 `FAILED` 로 접으면 미구현이 장애로 읽히고, 반대로 접으면
      **실패한 전이가 "안 돌았다"로 조용히 묻힌다.**
    """

    status: Literal["APPLIED", "NOT_APPLIED", "FAILED"]
    reason: str = ""
    #: 실제로 write 를 낸 파트. `APPLIED` 가 아니면 비어 있다.
    parts: list[str] = Field(default_factory=list)
    #: 아직 어댑터가 없는 파트.
    missing: list[str] = Field(default_factory=list)


# ── 등록소 ──────────────────────────────────────────────────────────────
#
# `wiring.py` 의 에이전트 레지스트리와 같은 결이다. 다른 점은 하나 — 저쪽은
# **부를 대상**을 담고 여기는 **장부를 바꿀 방법**을 담는다. 한 사전에 섞으면
# 어댑터가 없는 것과 전이가 없는 것이 같은 문장으로 나가고, 둘은 다른 사실이다.

_TRANSITIONS: dict[TransitionPart, Any] = {}


def register_transition(part: TransitionPart, impl: Any) -> None:
    """전이 구현을 등록한다. 재무·물류 모듈이 임포트 시점에 부른다."""
    if part not in PARTS:
        raise ValueError(f"전이 파트가 아니다: {part!r}. 가능: {', '.join(PARTS)}")
    _TRANSITIONS[part] = impl


def registered() -> Mapping[TransitionPart, Any]:
    """지금 등록된 전이. **읽기용 사본**이다 — 밖에서 넣지 못하게 한다."""
    return dict(_TRANSITIONS)


def missing() -> tuple[str, ...]:
    """아직 전이 구현이 없는 파트. **`PARTS` 순서를 지킨다.**

    ★ 순서를 지키는 이유는 사유 문장 때문이다. 집합 순서로 적으면 같은 상황이
      실행마다 다른 문장으로 나가 로그를 비교할 수 없다.
    """
    return tuple(part for part in PARTS if part not in _TRANSITIONS)


def reset() -> None:
    """테스트 전용 — 등록을 비운다."""
    _TRANSITIONS.clear()


# ── 트랜잭션 경계 ───────────────────────────────────────────────────────


def apply_approval(
    commitment: ApprovedCommitment,
    *,
    connect: Callable[[], Any] | None = None,
) -> TransitionOut:
    """승인분을 재무·물류 장부에 **한 트랜잭션으로** 반영한다.

    순서가 이 함수의 전부다.

    ```text
    1. 미등록 확인   → 커넥션을 열지 않는다
    2. build 두 번   → 커넥션 밖에서 (실패해도 DB 를 안 건드린다)
    3. persist 두 번 → 한 커넥션으로
    4. commit 한 번  → 실패하면 rollback
    ```

    🔴 **예외를 밖으로 던지지 않는다.** 이 함수가 불릴 때 결정은 **이미 적재됐다.**
       전이 실패가 예외로 올라가면 라우터가 500 을 내고, 사람이 보기에는 승인이
       실패한 것이 된다 — 실제로는 승인은 남았고 장부만 안 바뀐 것이다.
       `decision.py` 가 *"약정 조립 실패가 결정을 지우면 안 된다"* 고 정한 것과
       같은 규율이고, 여기서는 그것이 한 단계 더 뒤에 걸린다.

    ★ **다만 삼키되 사유는 반드시 남긴다.** 조용히 `FAILED` 만 돌려주면 무엇이
      터졌는지 아무 데도 안 남는다.

    :param connect: 커넥션 팩토리. 안 주면 `app.finance.db.get_connection` 을 쓴다 —
                    재무·물류가 같은 DB(같은 `DB_*`)를 쓰므로 커넥션도 하나면 된다.
    """
    absent = missing()
    if absent:
        # ★ 여기서 돌아선다 — **커넥션을 열지 않는다.** 열고 나서 아무 일도 안 하면
        #   빈 트랜잭션이 매 승인마다 열렸다 닫힌다.
        return TransitionOut(
            status="NOT_APPLIED",
            reason=f"상태전이 미등록: {', '.join(absent)}",
            missing=list(absent),
        )

    finance = _TRANSITIONS["finance"]
    logistics = _TRANSITIONS["logistics"]

    try:
        # 🔴 **커넥션 밖에서 계산한다.** 순수 계산이 터지는 것은 흔한 일인데
        #   (약정 모양이 예상과 다르다 등), 커넥션을 연 뒤에 터지면 열린 트랜잭션이
        #   남는다. 계산 실패는 DB 를 만나기 전에 끝나야 한다.
        finance_row = finance.build(commitment, commitment.as_of)
        logistics_rows = logistics.build(commitment)
    except Exception as exc:  # noqa: BLE001 - 전이 실패가 적재된 결정을 지우면 안 된다.
        return TransitionOut(status="FAILED", reason=f"전이 계산 실패: {exc}")

    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        finance.persist(conn, finance_row)
        logistics.persist(conn, logistics_rows)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 전이 실패가 적재된 결정을 지우면 안 된다.
        conn.rollback()
        return TransitionOut(status="FAILED", reason=f"전이 적재 실패: {exc}")
    finally:
        conn.close()
    return TransitionOut(status="APPLIED", parts=list(PARTS))
