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
from datetime import date, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.commitment import ApprovedCommitment
from app.master.ledger import build_purchase_rows, persist_purchases

__all__ = [
    "PARTS",
    "FinanceTransition",
    "LogisticsTransition",
    "TransitionOut",
    "TransitionPart",
    "apply_approval",
    "missing",
    "purchase_id_for",
    "purchase_item_id_for",
    "register_transition",
    "registered",
    "reset",
]

TransitionPart = Literal["finance", "logistics"]

#: 전이에 참여하는 파트와 **호출 순서**. 재무가 먼저다 — 현금이 모자라 재무가 터지면
#: 재고 쪽은 손도 대지 않은 채 롤백된다.
PARTS: tuple[TransitionPart, ...] = ("finance", "logistics")


# ── 두 파트가 같은 모양을 갖는다 ────────────────────────────────────────
#
# 🔴 **두 Protocol 이 서로 달랐던 것은 마스터 잘못이다.** `#238` 에서 재무는
#    `build(commitment, as_of)`, 물류는 `build(commitment)` 로 적었는데 그 차이에
#    근거가 없었다. 마스터가 규약을 근거 없이 두 모양으로 적어 놓은 것이다.
#
# 🔴 그리고 두 규약 다 **실제 구현과도 어긋나 있었다.**
#
#    ```text
#    적혀 있던 규약                      실제 구현
#    finance   build(commitment, as_of)  build_finance_transition(
#                                            commitment, *, purchase_id, target_state_date)
#    logistics build(commitment)         build_next_inventory(commitment)
#                                        persist_inventory(conn, *, sim_run_id, as_of, ...)
#    ```
#
# ★ **물류가 짚어 주었다** — *"build 이 날짜를 못 받으니 persist 가 대신 받게 됐고,
#   그래서 규약과 어긋났습니다."* 정확한 지적이다. 날짜를 계산 자리에서 못 받으면
#   그 값은 write 자리로 밀려나고, 순수 계산과 write 의 경계가 날짜 때문에 흐려진다.
#
# ★ **공통은 `commitment` 와 `target_state_date` 다.** 두 파트가 같은 승인분을
#   같은 날짜 기준으로 옮긴다 — 다를 이유가 없다.
#
# ★ **`purchase_ids` 도 공통이다** (2026-09-06 · 물류 요청 · `#311`).
#
#   ```text
#   재무   payables.purchase_id 가 purchases 를 참조하는 NOT NULL 컬럼이다
#   물류   도착 시점에 purchase_items 의 권위값을 읽어야 한다
#          (purchase_item_id · item_id · grade · unit_price_krw_per_kg)
#   ```
#
#   **둘 다 매입 원장을 가리켜야 하고, 둘 다 그 ID 를 지어낼 수 없다.** 만들 자리는
#   승인을 쥔 마스터다 (`purchase_id_for`).
#
# 🔴 **전에는 "재무만 받는다" 였고 그것이 틀렸다.** 마스터가 *"물류에는 필요 없다"* 를
#    단정했는데, 필요를 판정할 자리는 그 값을 쓰는 부서다. `#256` 이 두 Protocol 을
#    같은 모양으로 맞췄는데 이 인자만 반쪽으로 남아 있었고, 그래서 물류 WMS 의
#    Arrival 이 도착일에 막혔다 (`InTransitItem.purchase_id = None`).


class FinanceTransition(Protocol):
    """승인분이 현금 장부를 바꾸는 방식. **재무가 소유한다.**

    ★ `build` 는 순수 계산이고 `persist` 는 write 다. 나누는 이유는 하나다 —
      계산이 실패하면 **DB 를 열지도 않은 채** 멈출 수 있어야 한다.

    ★ `persist` 는 인자가 **`(conn, …)` 두 개뿐**이고 **commit 하지 않는다.**
      커밋은 두 파트가 모두 끝난 뒤 `apply_approval` 이 한 번 한다.

    :param target_state_date: 승인 결과 상태가 설 날. **마스터가 준다** — 재무가
        실행일 달력을 소유하지 않는다.
    :param purchase_ids: **회차(seq) → purchase_id 매핑**이다. 단수가 아닌 이유는
        `purchases.purchase_date` 가 header 에 **하나뿐**이기 때문이다. 회차마다
        매입일이 다르므로 한 header 에 여러 회차를 담을 수 없고, 따라서 **회차마다
        `purchases` 한 행**이 선다.
    """

    def build(
        self,
        commitment: ApprovedCommitment,
        *,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
    ) -> object: ...
    def persist(self, conn: Any, row: object) -> None: ...


class LogisticsTransition(Protocol):
    """승인분이 재고 장부를 바꾸는 방식. **물류가 소유한다.**

    ★ 재무와 달리 여러 행이 나온다 — 회차별 입고가 각각 로트/이동이 된다.
      몇 행인지도 물류가 정한다.

    ★ `persist` 는 재무와 같이 인자가 **`(conn, …)` 두 개뿐**이고 **commit 하지
      않는다.**

    🔴 **`purchase_ids` 도 받는다** (2026-09-06 · 물류 요청 · `#311`).

      전에 이 자리에 이렇게 적혀 있었다 —

      > `purchase_ids` 를 받지 않는다. 물류가 쓰는 `in_transit` 은 `purchases` 를
      > 참조하지 않는다 — **필요 없는 값을 규약에 얹지 않는다.**

      ⚠️ **그 "필요 없다" 를 마스터가 단정한 것이 틀렸다.** 물류 WMS 가 도착 시점에
        `purchase_items` 의 권위값(`purchase_item_id` · `item_id` · `grade` ·
        `unit_price_krw_per_kg`)을 읽어야 하고, 그러려면 **매입 원장을 가리키는 ID**가
        필요하다. 그것을 만드는 곳은 마스터(`purchase_id_for`)이고 물류는 생성·파싱·
        재구성을 하지 않는다 — 그 원칙이 옳다.

      ```text
      전   Master 승인 → purchase_ids → Purchase · Finance 만 사용
                       → 물류에는 미전달 → InTransitItem.purchase_id = None
                       → 도착일에 Arrival 이 막힘 (E2E 끊김)
      후   재무와 **같은 값·같은 모양**을 물류도 받는다
      ```

    ★ **재무와 대칭이다.** `#238` 에서 두 Protocol 이 서로 달랐던 것을 `#256` 이
      맞췄는데, `purchase_ids` 만 재무 쪽에 남아 반쪽이었다. 이제 같아진다.
    """

    def build(
        self,
        commitment: ApprovedCommitment,
        *,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
    ) -> Sequence[object]: ...
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


# ── purchase_id 짓기 ────────────────────────────────────────────────────
#
# ★ **순수 함수다 — DB 를 부르지 않는다.** ID 를 시퀀스나 채번 표에서 받아 오면
#   같은 승인을 두 번 반영할 때 다른 ID 가 나오고, 그 순간 UPSERT 가 겹쳐 쓰는
#   대신 **행을 하나 더 만든다.**
#
# ★ ★ **결정론이어야 한다.** 같은 승인이면 언제 몇 번을 불러도 같은 ID 가 나온다.
#   난수도, 순번 카운터도, 시각도 쓰지 않는다. 물류 `inbound_id` 가
#   `INB-{approval_id}-{seq}` 인 것이 정확히 같은 이유다 (물류 `transition.py`:
#   *"순번 카운터나 난수를 쓰면 두 번째 반영이 같은 물건을 다른 건으로 만들어
#   `in_transit` 이 부풀고 … 대조할 열쇠(B-1)도 사라진다"*).
#
# ★ **접두사는 번인 데이터를 따른다.** 번인에 이미 `PUR-KIMCHI-001` 과
#   `PITEM-SAFETY-001-BAECHU` 가 있다 — 새 규칙을 짓지 않고 그 모양에 맞춘다.
#
#   ```text
#   purchase_id       PUR-{request_id}-D{decision_seq}-S{seq}
#   purchase_item_id  PITEM-{purchase_id 에서 앞의 "PUR-" 를 뗀 나머지}-{item_code}
#   ```


def _decision_seq_of(commitment: ApprovedCommitment) -> int:
    """`approval_id` 에서 결정 회차를 꺼낸다.

    🔴 **형식에 기대는 자리다.** `decision_seq` 는 `ApprovedCommitment` 에 직접
       없고, `commitment.py` 가 `approval_id = f"H1-{request_id}-{decision_seq}"`
       로 지어 넣은 것을 되읽는 수밖에 없다.

    ★ 기대는 이상 **어긋나면 조용히 넘기지 않는다.** 여기서 0 이나 1 로 대신
      채우면 서로 다른 결정이 같은 `purchase_id` 를 갖게 되고, UPSERT 가 앞선
      결정의 매입을 **덮어쓴다.** 틀린 ID 로 계속 가는 것보다 멈추는 편이 낫다.
    """
    prefix = f"H1-{commitment.request_id}-"
    approval_id = commitment.approval_id
    tail = approval_id[len(prefix) :] if approval_id.startswith(prefix) else ""
    if not (tail.isascii() and tail.isdigit()):
        raise ValueError(
            f"approval_id 가 'H1-{{request_id}}-{{decision_seq}}' 형식이 아니다:"
            f" {approval_id!r} (request_id={commitment.request_id!r})."
            " 결정 회차를 지어내지 않는다."
        )
    return int(tail)


def purchase_id_for(commitment: ApprovedCommitment, seq: int) -> str:
    """이 승인의 **회차 하나**가 만드는 매입 Header ID.

    ```text
    PUR-{request_id}-D{decision_seq}-S{seq}
    ```

    ★ 회차마다 하나인 이유는 `purchases.purchase_date` 가 header 에 **하나뿐**이기
      때문이다. 회차마다 매입일이 다른 분할 매입을 한 header 에 담으면 그중 하나의
      날짜만 남는다.
    """
    return f"PUR-{commitment.request_id}-D{_decision_seq_of(commitment)}-S{seq}"


def purchase_item_id_for(purchase_id: str, item_code: str) -> str:
    """매입 Header 아래 품목 한 줄의 ID.

    ★ `PUR-` 를 떼고 `PITEM-` 을 붙인다 — 접두사가 둘 겹치면
      `PITEM-PUR-…` 이 되어 번인의 `PITEM-SAFETY-001-BAECHU` 와 모양이 갈린다.
    """
    if not purchase_id.startswith("PUR-"):
        raise ValueError(f"purchase_id 가 'PUR-' 로 시작하지 않는다: {purchase_id!r}")
    return f"PITEM-{purchase_id[len('PUR-') :]}-{item_code}"


# ── 원장을 쓸 수 있는 상태인가 ──────────────────────────────────────────


def _ledger_blocked(commitment: ApprovedCommitment) -> str:
    """매입 원장을 쓸 수 없는 사유. 쓸 수 있으면 **빈 문자열**이다.

    ★ **`FAILED` 가 아니라 `NOT_APPLIED` 로 가는 자리다.** 둘 다 아직 못 쓴 상태지만,
      여기 걸리는 것은 *"바꾸려다 실패했다"* 가 아니라 *"쓸 값이 아직 없다"* 다.

    🔴 **회차가 둘 이상이면 쓰지 않는다.** 매입이 회차별 금액을 아직 안 보내
       어느 회차에 얼마가 걸리는지 말할 방법이 없다. 수량 비율로 쪼개면 회차마다
       단가가 다른 분할 매입에서 **조용히 틀린 원장**이 생긴다.

       ★ 재무 `_single_leg` 이 이미 같은 이유로 막고 있다. 같은 사실을 두 곳이 다르게
         판정하지 않게 마스터가 **앞에서 같은 사유로** 멈춘다 — 뒤에서 재무가 막으면
         같은 상태가 `FAILED` 로 나간다.

    🔴 **지급일이 없으면 쓰지 않는다.** `purchases.payment_due_date` 는 NOT NULL 이고
       그 값의 근거는 재무 N5 다. 없는 날짜를 지어내지 않는다.

    ★ 회차가 **하나도 없는** 경우는 여기서 가르지 않는다 — 그건 원장 이전에 재무가
      `commitment_arrival_schedule` 로 먼저 막는 상태이고, 그 사유를 여기서 다시
      쓰면 같은 사실이 두 문장으로 나간다.
    """
    legs = commitment.arrival_schedule
    if len(legs) > 1:
        return "회차가 둘 이상이라 원장을 쓸 수 없다 — 회차별 금액이 아직 없다"
    if legs and legs[0].payment_due_date is None:
        return "재무 purchase_payment_days(N5) 가 없어 지급일을 만들 수 없다"
    return ""


# ── 트랜잭션 경계 ───────────────────────────────────────────────────────


def apply_approval(
    commitment: ApprovedCommitment,
    *,
    connect: Callable[[], Any] | None = None,
) -> TransitionOut:
    """승인분을 재무·물류 장부에 **한 트랜잭션으로** 반영한다.

    순서가 이 함수의 전부다.

    ```text
    1. 미등록 확인    → 커넥션을 열지 않는다
    2. 회차·지급일 확인 → 쓸 수 없으면 NOT_APPLIED, 역시 커넥션을 열지 않는다
    3. build 세 번    → 커넥션 밖에서 (실패해도 DB 를 안 건드린다)
    4. persist 세 번  → 한 커넥션으로 · **매입 원장이 재무보다 먼저**
    5. commit 한 번   → 실패하면 rollback
    ```

    ★ **매입 원장이 재무보다 먼저인 이유는 FK 다.** `payables.purchase_id` 가
      `purchases` 를 참조한다 — 부모 행이 없으면 재무 write 가 FK 에서 터진다.

    🔴 **여기에 SQL 은 없다.** 무슨 값을 어느 칸에 쓸지는 `master/ledger.py` 가 알고,
       이 함수는 **언제 부를지**만 정한다 (`test_전이_모듈에_SQL_이_없다`).

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

    blocked = _ledger_blocked(commitment)
    if blocked:
        # ★ **`FAILED` 가 아니다.** 쓸 수 없다는 것은 우리가 아는 사실이지 실패가
        #   아니다. 그리고 여기서도 **커넥션을 열지 않는다.**
        return TransitionOut(status="NOT_APPLIED", reason=blocked)

    finance = _TRANSITIONS["finance"]
    logistics = _TRANSITIONS["logistics"]

    try:
        # 🔴 **커넥션 밖에서 계산한다.** 순수 계산이 터지는 것은 흔한 일인데
        #   (약정 모양이 예상과 다르다 등), 커넥션을 연 뒤에 터지면 열린 트랜잭션이
        #   남는다. 계산 실패는 DB 를 만나기 전에 끝나야 한다.
        # 🔴 **달력 다음 날이다. 실행일 달력(평일만 도는 그것)을 쓰지 않는다.**
        #   금요일 승인이면 토요일이다. 주말에도 판매 시나리오로 물류·재무가 움직여
        #   장부는 **날마다 흐른다** — 다음 평일까지 상태를 미루면 토·일 이틀치 사실이
        #   장부에 없는 채로 월요일 상태가 선다. `#240` 이 정한 *"실행일은 평일만,
        #   경과일수는 달력일"* 과 같은 결이다. 여기서 세는 것은 **상태가 설 날**이지
        #   *"다음에 언제 판단을 도는가"* 가 아니다.
        target_state_date = commitment.as_of + timedelta(days=1)
        # ★ 회차마다 `purchases` 한 행이므로 회차마다 ID 하나다. `arrival_schedule`
        #   이 비면 **빈 매핑**이고 그것은 예외가 아니다 — 회차 일정을 못 만든 약정도
        #   승인은 살아 있다 (`commitment.py` 의 `notes` 가 왜 못 만들었는지 적는다).
        purchase_ids = {
            leg.seq: purchase_id_for(commitment, leg.seq)
            for leg in commitment.arrival_schedule
        }
        # ★ 매입 원장도 **커넥션 밖에서** 계산한다 — 재무·물류와 같은 규율이다.
        #   `items` 조회만 커넥션이 필요하고 그것은 `persist_purchases` 안에 있다.
        ledger_rows = build_purchase_rows(commitment, purchase_ids=purchase_ids)
        finance_row = finance.build(
            commitment,
            target_state_date=target_state_date,
            purchase_ids=purchase_ids,
        )
        # 🔴 **재무와 같은 매핑을 준다** (`#311` · 물류 요청 2026-09-06). 물류가 도착일에
        #    `purchase_items` 의 권위값을 읽으려면 매입 원장을 가리키는 ID 가 필요하고,
        #    물류는 그 ID 를 만들지도 파싱하지도 않는다.
        #
        # ★ **`persist_purchases` 가 먼저 돈다** (아래). 물류가 저장하는 `purchase_id` 가
        #   가리키는 부모 행은 **같은 트랜잭션 안에서 이미 서 있다.**
        logistics_rows = logistics.build(
            commitment,
            target_state_date=target_state_date,
            purchase_ids=purchase_ids,
        )
    except Exception as exc:  # noqa: BLE001 - 전이 실패가 적재된 결정을 지우면 안 된다.
        return TransitionOut(status="FAILED", reason=f"전이 계산 실패: {exc}")

    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        # 🔴 **재무보다 먼저다.** `payables.purchase_id` 가 `purchases` 를 참조하는
        #    FK 라 부모 행이 먼저 서야 한다.
        persist_purchases(conn, ledger_rows)
        finance.persist(conn, finance_row)
        logistics.persist(conn, logistics_rows)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 전이 실패가 적재된 결정을 지우면 안 된다.
        conn.rollback()
        return TransitionOut(status="FAILED", reason=f"전이 적재 실패: {exc}")
    finally:
        conn.close()
    return TransitionOut(status="APPLIED", parts=list(PARTS))
