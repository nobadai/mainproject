"""
cancellation.py — **취소된 승인의 입고 예정을 걷는다.** 물류 fixture 쪽 취소 자리.

⚠️ **마스터가 임시로 얹은 모듈이다** (`day_open.py` 와 같은 방식 · `#280` 전례).
  물류가 자기 구현을 올리면 **이 파일을 통째로 지우면 되고**, 기존 코드는 한 줄도
  안 건드렸다.

★ **셋을 마스터가 정해 통보했다** (2026-09-05 · 물류 이견 없음).

```text
① confirmed_inbound   취소된 승인의 inbound_id 를 목록에서 **제거**한다
                      항목마다 상태 칸을 두지 않는다 (ScheduledQuantity 를 안 넓힌다)
                      목록을 통째로 새로 쓰지 않는다 (남의 승인분이 사라진다)
② in_transit          같은 규칙으로 걷는다. 남으면 CONFIRMED · 비면 CONFIRMED_ZERO
③ 넣는 쪽과 빼는 쪽    같은 모듈에 둔다 — 두 규칙이 갈리면 한쪽만 고쳐지는 날이 온다
```

🔴 **`UNRESOLVED` 가 아니라 `CONFIRMED_ZERO` 다.**

  취소는 *"확인했고 이제 없다"* 이지 *"모른다"* 가 아니다. `01-06` 씨앗에 마스터가
  요구한 기준과 같다.

🔴 **과거 행을 안 고친다.**

```text
승인 01-05  →  target_state_date 01-06 행에 in_transit 을 적었다
취소 01-07  →  target_state_date 01-08 행에서 걷는다

01-06 · 01-07 은 그대로 둔다 — **그때는 실제로 오는 중이었다.**
```

  ★ 재무 역분개와 **같은 규율**이다 (`#302` — *"과거 state rewrite 금지"*).

🔴 **`FOR UPDATE` 로 그 행 하나를 잠그고 읽고-고치고-쓴다.**

  `persist_inventory` 와 같은 이유다 — 병합(여기서는 제거)을 파이썬에서 하는 이상
  읽기와 쓰기 사이가 비어 있고, 그 틈을 닫는 것은 행 잠금뿐이다.

⚠️ **입고된 뒤에는 이 함수로 못 물린다.** 물건이 창고에 있으면 취소가 아니라
  반품이다. 다만 지금은 입고 실행 진입점 자체가 없어(`Arrival → … → IN`) 그 판정을
  할 자리도 없다 — **입고 실행이 서면 여기에 그 방어를 더해야 한다.**

⚠️ **`confirmed_inbound` 를 건드리는 것은 `#275` 와 같은 임시 자리다.** 승인과 발주
  확정은 다른 사실이고, 물류가 발주 확정 단계를 만들면 넣는 쪽과 함께 이쪽도 그
  단계로 옮겨야 한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.logistics.inbound_schedules import (
    assert_cancellable,
    cancel_schedule,
)
from app.master.commitment import ApprovedCommitment

__all__ = ["LogisticsCancellationAdapter", "inbound_ids_of", "withdraw_inventory"]


def inbound_ids_of(commitment: ApprovedCommitment) -> tuple[str, ...]:
    """이 승인이 만든 입고 건 ID. **`build_next_inventory` 와 같은 규칙으로 조립한다.**

    ★ **표를 읽지 않는다.** 규칙이 결정론이라 취소 시점에 다시 만들어도 같은 값이
      나온다 — 읽으면 *"fixture 가 말하는 것"* 과 *"약정이 말하는 것"* 이 갈릴 자리가
      하나 더 생긴다 (마스터 `purchase_ids_of` 와 같은 판단).

    ⚠️ **`transition.py:252` 와 같은 문자열이어야 한다.** 한 글자라도 다르면 아무것도
      못 걷고, 그런데도 조용히 성공한다 — 그래서 검사가 두 자리를 대조한다.
    """
    return tuple(f"INB-{commitment.approval_id}-{leg.seq}" for leg in commitment.arrival_schedule)


def withdraw_inventory(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    inbound_ids: Sequence[str],
    source_ref: str,
) -> int:
    """이 입고 건들을 **`as_of` 그날부터** 취소한다.

    🔴 **정본은 `inbound_schedules.cancelled_as_of` 하나다 (W3-3).** 종전에는 그날
       fixture 행의 두 JSON 칸에서도 항목을 빼야 했는데, Reader 가 더 이상 그 칸을
       읽지 않으므로 걷을 이유가 없어졌다.

    ```text
    as_of <  cancelled_as_of   그날 이 일정은 여전히 존재한다
    as_of >= cancelled_as_of   그날부터 취소다
    ```

       ⚠️ 받는 `as_of` 는 이미 `cancelled_on + 1`(목표 상태일)이다
          (`LogisticsCancellationAdapter.cancel`). 취소일 자체를 적으면 **이미
          지나간 하루의 사실이 바뀐다.**

    🔴 **commit 하지 않는다.** 커밋은 재무 취소·매입 원장과 함께 마스터가 한 번 한다.

    🔴 **자기 커넥션을 새로 열지 않는다.** `persist_inventory` 와 같은 이유다.

    🔴 **도착 Receipt 가 있으면 거절한다.** 물건이 도착했으면 취소가 아니라
       반품·폐기·실사이고, 그 판단을 물류가 대신 내리지 않는다
       (`inbound_reconciliation` 이 긋는 그 선과 같다).

      ⚠️ **판정을 쓰기보다 먼저 한다.** 하나씩 취소하다 중간에 막히면 앞의 것은 이미
         닫힌 뒤다 — 같은 트랜잭션이라 롤백은 되지만, 판정이 쓰기와 섞이면
         *"무엇이 왜 막혔나"* 가 흐려진다.

    ★ **없는 것을 걷어도 오류가 아니다** — 이미 취소된 뒤의 재시도가 그렇다. 그때
      돌려주는 값이 `0` 이라 마스터가 *"이번에 실제로 걷은 것"* 을 말할 수 있다
      (재무 `#302` 의 *"retry no-op"* 과 같은 모양).

    :returns: 이번 호출로 **실제로 취소된 일정 수.**
    :raises ScheduleReceiptExists: 도착 Receipt 가 있는 입고를 취소하려 할 때.
    :raises ScheduleCancelConflict: 이미 **다른 날짜로** 취소된 일정일 때.
    """
    drop = sorted({i for i in inbound_ids if i})
    if not drop:
        # ★ 회차 일정이 없던 약정도 승인은 살아 있다 — 걷을 입고가 **없다**는 것은
        #   정상 상태다 (마스터 `cancel_purchases` 와 같은 태도).
        return 0

    # ── 판정이 먼저다. 하나라도 도착했으면 아무것도 안 걷는다 ──────────
    assert_cancellable(conn, sim_run_id=sim_run_id, inbound_ids=drop)

    return sum(
        1
        for inbound_id in drop
        if cancel_schedule(
            conn, sim_run_id=sim_run_id, inbound_id=inbound_id, cancelled_as_of=as_of
        )
    )


class LogisticsCancellationAdapter:
    """마스터 `ApprovalCancellation` 을 물류 함수에 잇는 얇은 배선.

    ★ **위 함수를 감싸기만 한다.** 걷는 규칙은 `withdraw_inventory` 가 그대로 한다.

    🔴 **`sim_run_id` 는 생성 인자다.** *"어느 실행의 장부인가"* 는 실행 정체성이라
      물류가 아니라 마스터가 정한다 — `LogisticsTransitionAdapter` 와 같은 판단이고,
      배선 자리(`app/main.py`)에서 눈에 보이게 주입한다.
    """

    def __init__(self, *, sim_run_id: str) -> None:
        self._sim_run_id = sim_run_id

    def cancel(
        self,
        conn: Any,
        *,
        commitment: ApprovedCommitment,
        cancelled_on: date,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
        financing_mode: str,
    ) -> None:
        """🔴 **`target_state_date` 행에서 걷는다. 승인이 쓴 행이 아니다.**

        승인은 `commitment.as_of + 1` 행에 적었고 취소는 `cancelled_on + 1` 행에서
        걷는다. 둘이 다르면 그 사이 날들은 **그대로 둔다** — 그때는 실제로 오는
        중이었다.

        ⚠️ `purchase_ids` 와 `financing_mode` 는 **안 쓴다.** 물류가 걷는 열쇠는
          `inbound_id` 이고 물류 축은 `(sim_run_id, as_of, usage_scope)` 라 재무 축이
          안 걸린다.

        ★ **받되 안 쓰는 것이 규약이 반쪽인 것보다 낫다** — 두 파트가 같은 인자를
          받아야 호출부가 하나로 선다. `purchase_ids` 를 재무에만 줬다가 물류 Arrival 이
          막힌 자리가 그 교훈이다 (`#313`).
        """
        withdraw_inventory(
            conn,
            sim_run_id=self._sim_run_id,
            as_of=target_state_date,
            inbound_ids=inbound_ids_of(commitment),
            source_ref=f"MASTER-CANCEL:{commitment.approval_id}@{cancelled_on.isoformat()}",
        )
