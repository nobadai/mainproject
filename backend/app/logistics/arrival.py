"""arrival.py — 운송 중인 물건 중 **오늘 도착 처리 대상**을 가려낸다 (3-B4-B).

```text
in_transit 목록 + as_of  →  select_due_inbound  →  due · blocked · unresolved · not_due
```

🔴 **순수 계산이다. DB 를 부르지 않는다.** 커넥션도 커서도 SQL 도 없다.
   이미 읽어 둔 사실을 받아 **분류만** 한다 — `transition.build_next_inventory` 와
   같은 규율이다 (계산이 실패하면 커넥션을 열기도 전에 멈춰야 한다).

★ **이 함수가 답하는 질문은 하나다.**

  ```text
  답한다      물류가 아는 사실만으로 이 행이 도착 처리 대상인가
  답하지 않는다  이 건이 이미 처리됐나   ← inbound_receipts 조회. 3-B4-C 다
  ```

🔴 **도착일 규칙은 `expected_arrival_date <= as_of` 다. `==` 가 아니다.**

   ```text
   eta = 2026-01-07   as_of = 2026-01-07   → 대상 (당일)
   eta = 2026-01-06   as_of = 2026-01-07   → 대상 (연체)
   eta = 2026-01-08   as_of = 2026-01-07   → 아직 아니다
   ```

   ★ `==` 로 잡으면 **그날 도착 처리가 안 돈 물건이 영영 갇힌다.** 근거가 실측에
     있다 — runtime fixture 에 2026-01-03 · 01-04 행이 **없다**(달력에 구멍이 있다).
     그리고 `day_open` 이 `in_transit` 을 날마다 물려받으므로 놓친 행은 사라지지도
     않고 매일 그대로 실려 온다. B-1 은 통과하고 점유 계산은 계속 그 물량을 세므로
     **아무도 틀렸다고 말해 주지 않는다.**

   ★ 저장소 선례도 `<=` 다 — `app/sales/tools.py` 가 도착분을
     `expected_arrival_date <= due_date` 로 센다.

🔴 **날짜를 지어내지 않는다.** `expected_arrival_date` 가 없으면 `as_of` 로 대신
   채우지 않는다. 그 날짜는 나중에 로트의 `received_at` 이 되어 신선도 계산으로
   흘러가는 값이다 — 지어낸 하루가 거기서 사실이 된다.

🔴 **`purchase_id` 를 지어내지 않는다.** `approval_id` · `inbound_id` 를 뜯어
   `PUR-…` 를 조립하지 않는다. 그 ID 의 주인은 마스터이고
   (`app/master/transition.py` 의 `purchase_id_for`), 물류는 **받아서 쓸 뿐**이다.
   같은 규칙이 두 곳에 있으면 마스터가 형식을 바꾸는 날 조용히 어긋난다.

⚠️ **참조가 아직 안 넘어온다 — 그래서 지금 실데이터는 `blocked` 로 나온다.**
   마스터 전이 규약(`LogisticsTransition.build`)에 `purchase_ids` 를 더하는 것은
   **후속 협의 안건**이고, 그 파일은 마스터 소유라 물류가 고칠 자리가 아니다.
   `blocked` 는 그 상태를 **보이게** 만드는 것이 일이다 — 감추지도, `due` 로
   올리지도 않는다.

🔴 **`arrived_at` 을 여기서 정하지 않는다.** `expected_arrival_date` 는 *예정일*이고
   Receipt 의 `arrived_at` 은 *도착 사실*이다. 둘을 같게 볼지는 Receipt 단계의
   결정이라 이 함수는 **아무 날짜도 고르지 않는다.**
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.logistics.schemas import InTransitItem, RuntimeSourceStatus

__all__ = [
    "ArrivalBlockReason",
    "ArrivalSelection",
    "ArrivalUnresolvedReason",
    "BlockedInbound",
    "DueInbound",
    "UnresolvedInbound",
    "select_due_inbound",
]


#: 도착일은 왔는데 **필요한 식별자가 없어** 진행할 수 없는 사유.
#:
#: ★ 튜플에 담기는 **나열 순서일 뿐 우선순위가 아니다.** 한 행에 둘 다 해당하면 둘
#:   다 남는다 — 하나를 다른 하나 뒤에 숨기면 하나를 고친 뒤 또 막힌다.
ArrivalBlockReason = Literal[
    "ARRIVAL_INBOUND_ID_MISSING",
    "ARRIVAL_PURCHASE_REFERENCE_MISSING",
]

#: 도착 여부를 **판정할 근거 자체가 없는** 사유.
#:
#: 🔴 `blocked` 와 다른 갈래다. `blocked` 는 *"날은 됐는데 못 간다"* 이고 이쪽은
#:    *"언제인지를 모른다"* 다. 뭉치면 `as_of` 로 날짜를 채우고 싶어지는데, 그것이
#:    바로 이 파일이 막으려는 일이다.
ArrivalUnresolvedReason = Literal["ARRIVAL_DATE_UNRESOLVED"]


@dataclass(frozen=True)
class DueInbound:
    """도착 처리로 넘어갈 수 있는 행. **세 값이 다 있다.**

    ★ `inbound_id` · `purchase_id` · `expected_arrival_date` 를 **좁혀서** 다시 싣는다.
      `item` 안에도 있지만 그쪽은 셋 다 `None` 이 될 수 있는 타입이라, 뒤 단계가
      매번 다시 확인하게 된다 — 여기서 한 번 좁히고 끝낸다.
    """

    #: 분류한 원본 행. **바꾸지 않는다** — 이 함수는 아무것도 변형하지 않는다.
    item: InTransitItem
    inbound_id: str
    purchase_id: str
    expected_arrival_date: date
    #: `expected_arrival_date < as_of` 인가. **당일(`==`)은 연체가 아니다.**
    overdue: bool


@dataclass(frozen=True)
class BlockedInbound:
    """도착일은 왔는데 식별자가 없어 멈춘 행. **버리지 않고 보이게 남긴다.**

    🔴 조용히 빼면 그 물건은 어느 보고에도 안 나온다 — 지금 실데이터의 유일한
       운송 중 행이 정확히 이 상태다(오늘 도착 예정 · 매입 참조 없음).
    """

    item: InTransitItem
    expected_arrival_date: date
    #: 막은 사유 **전부**. 하나만 남기지 않는다 (`ArrivalBlockReason` 주석 참조).
    reasons: tuple[ArrivalBlockReason, ...]
    #: 진단용. 막힌 행도 연체일 수 있고, 그 사실은 사유와 별개다.
    overdue: bool


@dataclass(frozen=True)
class UnresolvedInbound:
    """도착일이 없어 판정 자체가 불가능한 행.

    ⚠️ **지금 생산 경로로는 만들 수 없다.** `ArrivalLeg.arrival_date` 가 필수이고
       `ScheduledQuantity.date` 도 필수라, `persist_inventory` 가 그런 행을 쓰기 전에
       터진다. 손으로 심은 행에서만 나온다 — 그래도 조용히 넘기지 않는다.
    """

    item: InTransitItem
    reason: ArrivalUnresolvedReason


@dataclass(frozen=True)
class ArrivalSelection:
    """`select_due_inbound` 의 결과. **네 갈래를 섞지 않는다.**

    🔴 **`source_status` 가 `None` 과 `[]` 를 가른다.**

    ```text
    in_transit is None   UNRESOLVED       아직 확인한 적 없다
    in_transit == []     CONFIRMED_ZERO   확인했고 0 건이다
    in_transit == [행]    CONFIRMED        확인했고 이만큼이다
    ```

       ★ 둘 다 네 목록이 비지만 **같은 사실이 아니다.** 뭉치면 *"오늘 도착할 게
         없다"* 와 *"오늘 뭐가 도착할지 모른다"* 가 같은 값으로 나가고, 후자를
         전자로 읽는 순간 모르는 것을 아는 것처럼 다루게 된다.

    ★ **어휘를 새로 만들지 않았다.** `schemas.RuntimeSourceStatus` 가 이미 이 세
      상태를 뜻하고 fixture 의 `in_transit_status` 가 그 값을 쓴다 — 같은 사실에
      두 어휘를 두지 않는다.

    ⚠️ `source_status` 는 **목록의 상태**이지 행의 판정이 아니다. 모든 행이
       `blocked` 여도 목록 자체는 `CONFIRMED` 다 — 우리는 무엇이 떠 있는지 안다.
    """

    source_status: RuntimeSourceStatus
    #: 도착 처리 대상. `(expected_arrival_date, inbound_id)` 오름차순.
    due: tuple[DueInbound, ...]
    #: 날은 됐으나 식별자가 없어 멈춘 행.
    blocked: tuple[BlockedInbound, ...]
    #: 도착일을 모르는 행.
    unresolved: tuple[UnresolvedInbound, ...]
    #: 아직 도착일 전인 행. **원본 그대로** 담는다 — 손대지 않는다.
    not_due: tuple[InTransitItem, ...]
    #: `expected_arrival_date < as_of` 인 행 수 (`due` + `blocked`).
    #:
    #: ★ **진단 신호일 뿐이다.** 이 값으로 무엇을 바꾸지 않는다 — 날짜를 옮기지도,
    #:   순서를 뒤집지도 않는다. *"밀린 것이 있다"* 를 보이게 하는 것이 전부다.
    #:
    #: ⚠️ 당일 도착(`==`)은 연체가 아니다.
    overdue_count: int


def _있나(값: str | None) -> bool:
    """식별자가 실제로 있는가. **빈 문자열은 없는 것으로 본다.**

    ★ `transition.build_next_inventory` 가 매입 참조를 볼 때와 같은 눈이다 —
      있는 척하는 값을 통과시키면 뒤 단계가 그 값으로 조회를 나간다.
    """
    return bool(값)


def _지문(item: InTransitItem) -> str:
    """행 **자신의 사실만으로** 만든 전순서 보조 열쇠.

    🔴 **우선순위 점수가 아니다.** `(expected_arrival_date, inbound_id)` 는
       `inbound_id` 가 없는 행(`blocked`)에서 전순서가 아니라, 그때 남는 동률을
       입력 순서가 결정하게 된다. 그러면 **같은 입력을 다르게 담기만 해도 결과
       순서가 달라진다.**

    ★ 새 사실을 만들지 않는다 — 행이 이미 들고 있는 값을 정렬된 JSON 으로 적을 뿐이다.
    """
    return json.dumps(item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def select_due_inbound(
    in_transit: Sequence[InTransitItem] | None,
    *,
    as_of: date,
) -> ArrivalSelection:
    """운송 중인 행들을 `as_of` 기준으로 **네 갈래로 나눈다.**

    ```text
    unresolved  expected_arrival_date is None                    날짜를 모른다
    not_due     expected_arrival_date > as_of                    아직이다
    due         eta <= as_of · inbound_id 있음 · purchase_id 있음  진행 가능
    blocked     eta <= as_of 인데 둘 중 하나라도 없음               멈춘다 (보이게)
    ```

    🔴 **판정 순서가 계약이다.** 날짜를 **먼저** 본다.

    ```text
    eta = 2026-01-10   as_of = 2026-01-07   purchase_id 없음   → not_due
                                                                 ★ blocked 가 아니다
    ```

       아직 오지도 않은 물건을 *"막혔다"* 고 적으면, 협의가 진행 중인 정상 상태가
       매일 장애로 보고된다. 참조가 없다는 사실이 **업무를 막는 순간**은 그 물건이
       도착 처리 대상이 되는 날부터다.

    ⚠️ **날짜가 없는 것(`unresolved`)만은 `purchase_id` 와 무관하게 먼저 갈린다.**
       도착 자격을 따질 날 자체가 없어서다.

    🔴 **아무것도 변형하지 않는다.** 날짜를 채우지도, ID 를 짓지도, 원본 행을 고치지도
       않는다. DB 도 부르지 않는다.

    ⚠️ **한 행이 이미 처리됐는지는 답하지 않는다.** `inbound_receipts` 조회는
       3-B4-C 다 — 여기서 `due` 는 *"물류가 아는 사실만으로는 자격이 있다"* 는 뜻이다.

    :param in_transit: 이미 읽어 둔 운송 중 목록. `None` 은 *"확인한 적 없다"* 이고
        `[]` 는 *"확인했고 0 건"* 이라 **다른 사실이다.**
    :param as_of: 판정 기준일. 마스터가 정하는 달력값이고 물류가 세지 않는다.
    """
    if in_transit is None:
        # ★ 아는 척으로 바꾸지 않는다. 네 목록이 비는 것은 `[]` 와 같지만
        #   `source_status` 가 두 사실을 갈라 준다.
        return ArrivalSelection(
            source_status="UNRESOLVED",
            due=(),
            blocked=(),
            unresolved=(),
            not_due=(),
            overdue_count=0,
        )

    due: list[DueInbound] = []
    blocked: list[BlockedInbound] = []
    unresolved: list[UnresolvedInbound] = []
    not_due: list[InTransitItem] = []
    overdue_count = 0

    for item in in_transit:
        eta = item.expected_arrival_date
        if eta is None:
            # 🔴 `as_of` 로 채우지 않는다. 없는 날짜를 지어내면 그 하루가 뒤 단계에서
            #    사실이 된다 (로트의 `received_at` → 신선도).
            unresolved.append(UnresolvedInbound(item=item, reason="ARRIVAL_DATE_UNRESOLVED"))
            continue
        if eta > as_of:
            # ★ **날짜를 먼저 본다.** 아직 안 온 물건은 참조가 없어도 정상이다.
            not_due.append(item)
            continue

        overdue = eta < as_of
        if overdue:
            overdue_count += 1

        reasons: list[ArrivalBlockReason] = []
        if not _있나(item.inbound_id):
            reasons.append("ARRIVAL_INBOUND_ID_MISSING")
        if not _있나(item.purchase_id):
            # ⚠️ 지금 실데이터가 여기로 온다 — 마스터가 참조를 아직 안 넘긴다.
            #    **정상적으로 예상된 상태**이고, 물류가 값을 지어내 풀 일이 아니다.
            reasons.append("ARRIVAL_PURCHASE_REFERENCE_MISSING")

        if reasons:
            blocked.append(
                BlockedInbound(
                    item=item,
                    expected_arrival_date=eta,
                    reasons=tuple(reasons),
                    overdue=overdue,
                )
            )
            continue

        # ★ 여기 온 행은 셋이 다 있다 — 타입을 좁혀 다시 싣는다.
        assert item.inbound_id is not None
        assert item.purchase_id is not None
        due.append(
            DueInbound(
                item=item,
                inbound_id=item.inbound_id,
                purchase_id=item.purchase_id,
                expected_arrival_date=eta,
                overdue=overdue,
            )
        )

    # ★ **연체가 오래된 것부터, 같은 날은 `inbound_id` 로.** 둘 다 행이 이미 들고
    #   있는 사실이다 — 우선순위 점수를 만들지 않는다. `_지문` 은 `inbound_id` 가
    #   없는 행에서 동률을 없애는 보조 열쇠일 뿐이다 (입력 순서가 결과를 바꾸지
    #   못하게 한다).
    due.sort(key=lambda row: (row.expected_arrival_date, row.inbound_id, _지문(row.item)))
    blocked.sort(
        key=lambda row: (row.expected_arrival_date, row.item.inbound_id or "", _지문(row.item))
    )
    not_due.sort(key=lambda row: (row.expected_arrival_date, row.inbound_id or "", _지문(row)))
    # ★ 도착일이 없는 갈래는 날짜로 못 세운다 — 남은 사실로만 세운다.
    unresolved.sort(key=lambda row: (row.item.inbound_id or "", _지문(row.item)))

    return ArrivalSelection(
        # ⚠️ 목록의 상태다. 모든 행이 `blocked` 여도 **무엇이 떠 있는지는 안다.**
        source_status="CONFIRMED" if in_transit else "CONFIRMED_ZERO",
        due=tuple(due),
        blocked=tuple(blocked),
        unresolved=tuple(unresolved),
        not_due=tuple(not_due),
        overdue_count=overdue_count,
    )
