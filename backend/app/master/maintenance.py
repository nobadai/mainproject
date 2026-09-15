"""maintenance.py — **그날 자리를 비우는 단계를 마스터가 부른다** (2026-09-11).

```text
개장 → **물류 유지보수** → 미적용 전이 재시도 → 입고 → 채권 → 수금 → [장부 관문] → …
```

★★ **왜 이 단계가 생겼나 — 나갈 길 셋이 다 막혀 있었다.**

  ```text
  실측 2026-09-11 · 보수안 1~3월 걷기 71일
    매입   15건 · 2026-01-05 ~ 01-09 **닷새뿐**
    보류   156건 · 사유 "하드 제약(창고)으로 수량이 0까지 축소되어 제안 불가"
    그날 물류 제약   guaranteed_capacity_kg 8,000 · used_capacity_kg 8,338
                     warehouse_free_kg 0 · rental_cap_kg 0 · 이후 cap_by_date 전부 0.0
  ```

  ```text
  판매로 나간다   🟢 신선도가 지난 Lot 을 물류가 판매 가능 수량에서 뺀다 (규칙대로다)
  폐기로 나간다   🔴 **걷기가 안 불렀다** — 걷기 실행 넷의 Lot 이 전부 ACTIVE 였다
  ```

  ★★ **그래서 고칠 자리는 물류가 아니라 「부르는 자리 하나」다.** 폐기도 자리 반환도
    `logistics.auto_maintenance.run_logistics_auto_maintenance` 에 이미 있었고,
    `app/master/` 어디에도 그것을 부르는 줄이 없었다 — 판매 판단이 0건이던 때와
    매입 승인이 0건이던 때와 **같은 모양**이다.

---

🔴 **물류의 안전 조건을 여기서 다시 적지 않는다.**

  무엇을 자동으로 버리고 무엇을 사람에게 남기는지는 저쪽이 정했고
  (*"한 kg 이라도 잡혀 있으면 통째로 건너뛴다 · 자동 부분 폐기 없음"*), 이 파일은
  **트랜잭션과 어휘만** 진다. 후보를 고르는 줄도 폐기량을 정하는 줄도 여기 없다.

🔴 **트랜잭션의 주인이 이 파일이다.** `run_logistics_auto_maintenance` 는 커밋도
   롤백도 안 한다고 못박았다 — 그 약속의 반대편이 여기다.

  ★ **한 사이클이 한 커밋이다.** Lot 마다 커밋하지 않는다. 저쪽이 배치 시작에서
    출고 전역 잠금을 잡아 잠금 순서를 `3 → 1 → 4` 하나로 고정해 두는데, 중간에
    커밋하면 그 잠금이 풀려 다른 실행과 역전될 자리가 생긴다.

🔴 **터져도 하루는 계속 간다.** 예외를 값으로 옮긴다 (`_stage` · `_retry_pending`
   과 같은 태도). 자리를 못 비운 것과 하루를 못 산 것은 다른 사실이다.

---

## 사유 · 행위자 · 시각 — **마스터가 정해서 넘긴다**

물류가 셋 다 기본값을 두지 않았다. *"물류가 지어내지 않는다. 기본값을 두면 묻지도
않은 사유와 행위자가 장부에 사실로 선다"* — 그 빈칸을 채우는 것이 부르는 쪽의 일이다.

```text
reason_code   FRESHNESS_EXPIRED    왜 버리나
recorded_by   AUTO-MAINTENANCE     누가 했나 · 🔴 사람 이름을 안 쓴다
occurred_at   걷기의 시간축         🔴 벽시계를 여기서 안 읽는다
```
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from app.finance.db import get_connection
from app.logistics.auto_maintenance import (
    AutoMaintenanceResult,
    run_logistics_auto_maintenance,
)

__all__ = [
    "AUTO_MAINTENANCE",
    "FRESHNESS_EXPIRED",
    "MaintenanceOut",
    "run_auto_maintenance",
]


FRESHNESS_EXPIRED = "FRESHNESS_EXPIRED"
"""자동 폐기의 `reason_code`. **왜 버리는가.**

🔴 **새 판정 기준을 만든 이름이 아니다.** 물류가 후보로 올리는 조건이 딱 하나이고
  (`turnover.is_disposal_candidate` — `remaining_freshness_days <= 0`), 이 낱말은
  **그 조건을 그대로 부르는 말**이다. 사유에 다른 뜻을 적으면 장부가 *"왜 버렸나"*
  에 후보 조건과 다른 답을 하게 된다.

★ **축 이름을 새로 안 짓는다.** `contracts/core.py` 의 `BindingConstraint` 가
  이미 `FRESHNESS` 를 쓰고 물류 신호도 `INVENTORY_FRESHNESS_PRESSURE` 다 — 같은
  축의 말이라 읽는 사람이 어느 사실을 가리키는지 바로 안다.

⚠️ **뜻을 좁게 읽어야 한다.** *"과학적으로 부패했다"* 가 아니라 `turnover.py` 가
  적어 둔 그대로 *"현재 계약상 판매 대상에서 빠져 폐기 검토가 필요하다"* 다.

🔴 **`MVP_DEMO_FIXTURE_CORRECTION` 을 쓰지 않는다.** 씨앗 보정용이라고 물류가
  적어 뒀다 — 업무 폐기를 그 사유로 적으면 나중에 둘을 못 가른다.
"""

AUTO_MAINTENANCE = "AUTO-MAINTENANCE"
"""자동 유지보수가 적는 `recorded_by`. **`pallet_events` 에 남는 행위자다.**

🔴 **사람 이름을 안 쓴다.** `AUTO-BACKFILL` 과 같은 결이고 같은 이유다 — 사람이 안
  눌렀는데 눌렀다고 기록되면 그 행은 되돌릴 수 없다. 폐기는 승인보다 더하다:
  물류가 *"되돌릴 경로가 없다(`ADJUST_IN` 없음 · 실사 제외)"* 고 못박았다.

★ **`AUTO-BACKFILL` 을 재사용하지 않는다.** 그 낱말의 뜻은 *"승인을 자동으로
  채웠다"* 이고, 이쪽은 *"창고를 자동으로 정리했다"* 다 — 한 낱말로 적으면
  `pallet_events` 만 보고는 어느 자동화가 한 일인지 못 가른다.
"""


@dataclass(frozen=True)
class MaintenanceOut:
    """유지보수 단계 1회의 결과. **예외 대신 이것을 돌려준다.**

    ```text
    RAN           손댔거나 일부러 건너뛴 Lot 이 있었다 — **전부 성공했다는 뜻이 아니다**
    NOTHING_DUE   확인했고 할 것이 없었다 — 🟢 정상이다
    FAILED        하려다 터졌다 — 아무것도 안 바뀌었다
    ```

    ★ **어휘를 새로 만들지 않았다.** 셋 다 하루 순서가 이미 쓰는 말이다
      (`RetryStatus` · `InboundOut` · `OutboundOut`), 단계를 안 탄 날의
      `NOT_ATTEMPTED` 도 `DayRunOutcome` 이 이미 쓴다.

    🔴 **`NOTHING_DUE` 와 `FAILED` 를 접지 않는다.** *"버릴 것이 없었다"* 와
       *"버릴 것이 있었는지조차 못 물어봤다"* 는 다르고, 접으면 조회가 죽은 날
       이 단계가 조용해진다.
    """

    as_of: date
    status: Literal["RAN", "NOTHING_DUE", "FAILED"]
    reason: str = ""
    #: 물류가 낸 값 **그대로**. 🔴 **접지 않는다** — 몇 Lot 을 봤고 무엇을 버렸고
    #: 무엇을 사람에게 남겼는지의 주인은 `AutoMaintenanceResult` 하나다.
    result: AutoMaintenanceResult | None = None

    @property
    def outcomes(self) -> Mapping[str, int]:
        """Lot 별 결과 분포. **`MaintenanceOutcome` 넷을 그대로 센다.**

        ★ `RetryOut.outcomes` · `BackfillOut.outcomes` 와 같은 모양이다 — 요약이
          이 값을 그대로 싣는다.

        🔴 **여기서 새 이름을 안 붙인다.** 넷의 주인은 `auto_maintenance.py` 다.
        """
        counted: dict[str, int] = {}
        if self.result is None:
            return counted
        for one in self.result.lots:
            counted[one.outcome] = counted.get(one.outcome, 0) + 1
        return counted


def run_auto_maintenance(
    as_of: date,
    *,
    sim_run_id: str,
    occurred_at: datetime,
    connect: Any = None,
    maintain_fn: Callable[..., AutoMaintenanceResult] = run_logistics_auto_maintenance,
) -> MaintenanceOut:
    """그날 자리를 비운다. 🔴 **예외를 밖으로 내지 않는다.**

    ★ **`receive_arrivals` · `ship_due_sales` 와 같은 모양이다** — `as_of` 를 받고,
      상태를 값으로 돌려주고, 하루의 진행을 자기 성공에 걸지 않는다.

    🔴 **무엇을 버릴지 여기서 안 고른다.** 후보도 안전 조건도 `maintain_fn` 의
       것이다 — 이 함수가 지는 것은 트랜잭션과 어휘 셋뿐이다.

    🔴 **커밋이 여기 있다.** 물류가 안 하겠다고 못박은 그 일이다. 터지면 롤백하고
       `FAILED` 로 돌아선다 — 반쯤 비운 창고를 장부에 남기지 않는다.

    :param sim_run_id: 어느 실행의 창고인가. 🔴 **이번 실행 축으로만 돈다.**
    :param occurred_at: `pallet_events` 에 남을 시각. 🔴 **인자다 — 이 함수는
        시계를 안 읽는다.** 걷기의 시간축을 부르는 쪽이 나른다
        (`fefo_allocation.decided_at` 과 같은 규율).
    :param maintain_fn: 물류 경계. 🔴 **기본값이 실제 함수 자체다** — `None` 을
        안 받는다 (`clock.py` · `verifier.py` 와 같은 규율).
    """
    open_connection = get_connection if connect is None else connect
    try:
        conn = open_connection()
    except Exception as exc:  # noqa: BLE001 - 연결 실패가 그날을 통째로 세우면 안 된다.
        return MaintenanceOut(as_of=as_of, status="FAILED", reason=f"연결 실패: {exc}")

    try:
        try:
            result = maintain_fn(
                conn,
                sim_run_id=sim_run_id,
                as_of=as_of,
                # 🔴 **셋 다 마스터가 정해 넘긴다.** 물류 기본값에 안 기댄다 —
                #    기본값이 아예 없다 (`InvalidAutoMaintenanceRequest`).
                reason_code=FRESHNESS_EXPIRED,
                recorded_by=AUTO_MAINTENANCE,
                occurred_at=occurred_at,
            )
        except Exception as exc:  # noqa: BLE001 - 유지보수가 터져도 하루는 계속 간다.
            conn.rollback()
            return MaintenanceOut(
                as_of=as_of,
                status="FAILED",
                reason=f"유지보수가 터졌다: {type(exc).__name__}: {exc}",
            )

        # 🔴 **한 사이클이 한 커밋이다.** Lot 마다 나눠 적지 않는다 — 저쪽이 배치
        #    시작에서 잡아 둔 잠금 순서가 중간 커밋에 풀리면 다른 실행과 역전된다.
        conn.commit()

        if not result.lots:
            # 🟢 **정상이다.** 폐기대기도 없고 자리만 남은 Lot 도 없었다.
            return MaintenanceOut(
                as_of=as_of,
                status="NOTHING_DUE",
                reason=f"손댈 Lot 이 없었다 (본 Lot {result.examined_lots})",
                result=result,
            )
        return MaintenanceOut(
            as_of=as_of,
            status="RAN",
            reason=(
                f"{len(result.lots)}개 Lot 을 다뤘다 (본 Lot {result.examined_lots}"
                f" · 폐기 {result.disposed_qty_kg}kg · 자리 {len(result.emptied_pallet_ids)}장)"
            ),
            result=result,
        )
    finally:
        conn.close()
