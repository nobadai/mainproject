"""transition.py — 승인 약정을 **물류 재고 상태로 옮기는** build·persist (C 형태 ⑦).

마스터가 `app/master/transition.py` 에서 트랜잭션 경계를 쥐고, **무슨 값을 어느 칸에
어떤 SQL 로 쓸지는 물류가 소유한다.** 이 파일이 그 물류 몫이다.

```text
승인 → ApprovedCommitment → build_next_inventory  (순수 계산 · DB 를 안 부른다)
                          → persist_inventory     (주어진 conn 으로 write · commit 안 함)
```

🔴 **왜 `inventory_lots` 가 아니라 runtime fixture 인가.**
   승인 시점의 입고 예정을 `inventory_lots` 에 넣을 수 없다 (2026-09-03 실측).
   네 가지가 막는다.

   ```text
   status CHECK       ACTIVE · DEPLETED · DISPOSED · HOLD   IN_TRANSIT 이 없다
   move_type CHECK    IN · OUT · DISPOSE · ADJUST           입고 예정 값이 없다
   purchase_item_id   NOT NULL + FK → purchase_items        승인만으론 그 행이 없다
   unit_cost · zone   NOT NULL                              승인 시점에 없거나 추정이다
   ```

   ★ 네 가지 중 어느 하나도 **코드로 우회할 수 없다.** 상태값을 지어내면 CHECK 가
     막고, 막지 않게 스키마를 열면 *"아직 안 온 물건"* 이 실재 로트와 같은 칸에 앉는다.
     `unit_cost` 를 추정으로 채우면 그 추정이 원가가 되어 재무로 흘러간다.

🟢 **대신 `logistics_runtime_fixture.in_transit_json` 은 이미 그 자리다.**
   계약은 `schemas.py` 의 `InTransitItem` 이고, `in_transit_status` 는
   `CONFIRMED · CONFIRMED_ZERO · UNRESOLVED` 셋 중 하나다. 스키마를 바꾸지 않는다.

🔴 **`in_transit` → 실제 입고(`inventory_lots`) 전환 시점은 이 판에서 안 정했다.**
   물건이 실제로 도착해 로트가 되는 순간을 무엇으로 볼지(도착일 경과 · 검수 완료 ·
   `purchase_items` 행 생성)는 **물류 판단이고 아직 미결이다.** 여기서 정하지 않는다 —
   지금 임의로 정하면 그 규칙이 근거 없이 굳는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from app.logistics.db import get_db_schema
from app.logistics.schemas import InTransitItem
from app.master.commitment import ApprovedCommitment

__all__ = [
    "InventoryTransition",
    "LogisticsFixtureMissing",
    "LogisticsTransitionAdapter",
    "build_next_inventory",
    "persist_inventory",
]

#: 조회·갱신 대상 범위. `repository.py` 의 `LOGISTICS_POLICY_USAGE_SCOPE` 와 같은 값이다.
USAGE_SCOPE = "AGENT_MVP_DEMO"


class LogisticsFixtureMissing(LookupError):
    """갱신할 runtime fixture 행이 없다.

    🔴 **없으면 새로 만들지 않는다.** 새 행에는 `evidence_grade` · `approved_by` ·
       나머지 두 status(`confirmed_inbound_status` · `confirmed_outbound_status`)를
       정해 넣어야 하는데 그것은 **물류가 근거를 갖고 내리는 판단**이다.
       없는 판단을 기본값으로 지어내면, 지어낸 값이 그날의 사실로 남는다.
    """


def build_next_inventory(commitment: ApprovedCommitment) -> list[InTransitItem]:
    """승인 약정의 회차별 입고를 `InTransitItem` 목록으로 옮긴다. **순수 계산이다.**

    ★ DB 를 부르지 않는다 — 계산이 실패하면 커넥션을 열기도 전에 멈춰야 한다
      (마스터 `transition.py` 가 build 를 커넥션 밖에서 부르는 이유다).

    🔴 **도착일을 다시 계산하지 않는다.** `leg.arrival_date` 를 그대로 쓴다.
       마스터가 물류 `inbound_lead_days`(N4)로 이미 계산해 약정에 실었고, 여기서
       `purchase_date + N` 을 다시 더하면 **같은 사실의 주인이 둘이 된다.**
       두 곳이 각자 계산하면 어느 날 어긋나고, 어긋난 쪽이 틀렸다고 아무도 말해 주지
       않는다. 약정이 실은 값이 그 사실의 유일한 원본이다.

    ★ 빈 `arrival_schedule` 은 예외가 아니다. 회차 일정을 못 만든 약정도 승인은 살아
      있고(마스터 `commitment.py` 의 `notes` 가 왜 못 만들었는지 적는다), 그때 물류가
      반영할 입고 예정이 **없다**는 것은 정상 상태다.
    """
    rows: list[InTransitItem] = []
    for leg in commitment.arrival_schedule:
        rows.append(
            InTransitItem(
                # ★ **승인 id + 회차 seq 로 만든다.** 같은 승인을 두 번 반영해도 같은
                #   id 가 나와야 갱신이 멱등해진다 — 순번 카운터나 난수를 쓰면 두 번째
                #   반영이 같은 물건을 다른 건으로 만들어 `in_transit` 이 부풀고,
                #   `confirmed_inbound_schedule` 과 대조할 열쇠(B-1)도 사라진다.
                inbound_id=f"INB-{commitment.approval_id}-{leg.seq}",
                item=leg.item,
                # ★ `Decimal(str(x))` 를 쓴다. `Decimal(float)` 은 0.1 이 갖고 있는
                #   이진 오차를 그대로 들여와 수량에 안 보이는 꼬리를 남긴다.
                quantity_kg=Decimal(str(leg.qty_kg)),
                expected_arrival_date=leg.arrival_date,
            )
        )
    return rows


def persist_inventory(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    rows: Sequence[InTransitItem],
    source_ref: str,
) -> None:
    """계산된 입고 예정을 runtime fixture 의 `in_transit` 칸에 쓴다.

    🔴 **commit 하지 않는다.** 커밋은 재무 write 와 함께 마스터가 한 번 한다.
       여기서 커밋하면 물류만 먼저 확정되고, 뒤이어 재무가 터졌을 때 **현금은 안
       나갔는데 입고 예정만 있는 장부**가 남는다.

    🔴 **자기 커넥션을 새로 열지 않는다.** 여기서 커넥션을 만들면 마스터가 쥔
       트랜잭션 밖에서 쓰게 되어 위와 같은 반쪽 상태가 다시 생긴다. 인자로 받은
       `conn` 만 쓴다 — 이 모듈이 `repository.py` 의 `fetch_all` 을 안 쓰는 이유다.

    ★ 건드리는 칸은 넷뿐이다 — `in_transit_json` · `in_transit_status` · `source_ref` ·
      `updated_at`. `confirmed_inbound_*` · `confirmed_outbound_*` · `evidence_grade` ·
      `approved_by` 는 **다른 사실이고 다른 근거를 갖는다.** 같은 UPDATE 에 얹으면
      승인 반영이 조용히 남의 칸을 덮는다.

    :raises LogisticsFixtureMissing: 그날의 fixture 행이 없을 때. **만들지 않는다.**
    """
    # ★ 비어 있음을 "확인된 0" 으로 적는다. `UNRESOLVED` 로 적으면 *"아직 모른다"* 가
    #   되어 소비자가 판정을 미룬다 — 승인분이 없다는 것은 우리가 아는 사실이다.
    status = "CONFIRMED" if rows else "CONFIRMED_ZERO"
    payload = [row.model_dump(mode="json") for row in rows]

    query = sql.SQL(
        """
        UPDATE {}.logistics_runtime_fixture
        SET in_transit_json = %s,
            in_transit_status = %s,
            source_ref = %s,
            updated_at = NOW()
        WHERE sim_run_id = %s
          AND as_of = %s
          AND usage_scope = %s
        """
    ).format(sql.Identifier(get_db_schema()))

    with conn.cursor() as cursor:
        cursor.execute(
            query,
            (Jsonb(payload), status, source_ref, sim_run_id, as_of, USAGE_SCOPE),
        )
        if cursor.rowcount != 1:
            # ★ 무엇이 없는지 보이게 적는다. "행이 없다" 만으로는 sim_run_id 가 틀린
            #   것인지 그날 fixture 가 아직 안 만들어진 것인지 가릴 수 없다.
            raise LogisticsFixtureMissing(
                "갱신할 물류 runtime fixture 행이 없다"
                f" (sim_run_id={sim_run_id}, as_of={as_of}, usage_scope={USAGE_SCOPE})."
                " 새 행을 만들지 않는다 — evidence_grade · approved_by · 나머지 두"
                " status 는 물류 판단이다."
            )


# ── 마스터 전이 Protocol 어댑터 ─────────────────────────────────────────
#
# ★ **위의 두 함수를 감싸기만 한다.** 계산은 `build_next_inventory` 가, 쓰기는
#   `persist_inventory` 가 그대로 한다 — 여기 있는 것은 마스터가 부르는 호출 모양에
#   이름과 인자를 맞춰 주는 배선뿐이다.
#
# ⚠️ **걷어내기 쉽게 얹었다.** 물류가 자기 어댑터를 올리면 이 절만 통째로 지우면
#    되고, 위의 두 함수는 손댄 자리가 없다.
#
# 🔴 **물류가 예고한 모양과 다른 곳이 하나 있다 — 생성 인자 `sim_run_id` 다.**
#    물류 계약에는 *"생성 인자가 없습니다"* 로 적혀 있었다. 그런데
#    `persist_inventory` 의 WHERE 절이 `sim_run_id` 를 쓰고(위 `query`), 그 값은
#    **어느 실행의 장부인가**라는 실행 정체성이라 물류가 아니라 마스터가 정한다.
#    모듈 상수로 박으면 실행이 둘이 되는 날 물류 코드를 고쳐야 하므로, 배선 자리
#    (`app/main.py`)에서 눈에 보이게 주입받는다.


@dataclass(frozen=True)
class InventoryTransition:
    """승인 한 건이 만드는 재고 변화 **한 묶음**.

    🔴 **회차 낱개가 아니라 묶음인 이유가 둘이다.**

    ```text
    회차에는 target_state_date 가 없다   persist 가 어느 날 행에 쓸지 모른다
    arrival_schedule 이 비면 빈 목록이다  "쓸 것이 없다" 와 "어느 행인지 모른다" 가
                                          같아진다
    ```

    ★ **빈 승인도 그날 행을 `CONFIRMED_ZERO` 로 적어야 한다.** 회차를 낱개로 내면
      빈 약정에서 시퀀스 자체가 비어 `persist` 가 아무 일도 안 하게 되고, 그러면
      *"승인분이 없다"* 는 우리가 아는 사실이 장부에 안 남는다.
    """

    #: 이 변화가 설 날. **마스터가 준다** — 물류가 세지 않는다.
    target_state_date: date
    #: 이 변화의 출처. `persist_inventory` 가 fixture 행의 `source_ref` 에 그대로 적는다.
    source_ref: str
    #: `build_next_inventory` 가 낸 회차별 입고 예정. **비어 있을 수 있다.**
    items: tuple[InTransitItem, ...]


class LogisticsTransitionAdapter:
    """마스터 전이 Protocol(`app.master.transition.LogisticsTransition`)의 물류 입구.

    ★ **여기에는 업무가 없다.** 재무 `FinanceTransitionAdapter` 와 같은 결이다 —
      얇게 두어야 계약이 바뀔 때 고칠 자리가 한 곳으로 남는다.

    🔴 **commit 도 rollback 도 하지 않고 커넥션을 새로 열지도 않는다.**
       `persist_inventory` 가 이미 그 규율을 지킨다 — 어댑터는 인자만 옮긴다.

    :param sim_run_id: 이 반영이 앉을 시뮬레이션 실행. **마스터가 소유한 값**이고
        `app/master/ledger_repository.BURN_IN_SIM_RUN_ID` 가 그 주인이다.
    """

    def __init__(self, *, sim_run_id: str) -> None:
        self._sim_run_id = sim_run_id

    def build(
        self,
        commitment: ApprovedCommitment,
        *,
        target_state_date: date,
    ) -> Sequence[InventoryTransition]:
        """묶음 **하나를 담은 시퀀스**를 낸다.

        ★ Protocol 이 `Sequence[object]` 라 하나만 담아도 어기지 않는다. 승인 하나가
          바꾸는 fixture 행이 하나뿐이라 묶음도 하나다.
        """
        return (
            InventoryTransition(
                target_state_date=target_state_date,
                # ★ 마스터 승인에서 왔다는 것을 행에 남긴다. 접두사를 붙이는 이유는
                #   같은 칸에 다른 출처(번인 적재 등)가 앉을 수 있어서다.
                source_ref=f"MASTER-APPROVAL:{commitment.approval_id}",
                items=tuple(build_next_inventory(commitment)),
            ),
        )

    def persist(self, conn: Any, rows: Sequence[InventoryTransition]) -> None:
        """묶음마다 `persist_inventory` 를 부른다. **인자를 옮기는 것이 전부다.**

        🔴 **`as_of` 에 `target_state_date` 를 넘긴다.** 승인일(`commitment.as_of`)이
           아니다 — 승인이 바꾸는 것은 **다음 날 상태**이고, 재무가 같은 날짜로
           `finance_states` 를 세운다. 여기서 하루 앞 행에 쓰면 두 장부가 다른 날에
           앉아 그날의 사실이 갈린다.
        """
        for bundle in rows:
            persist_inventory(
                conn,
                sim_run_id=self._sim_run_id,
                as_of=bundle.target_state_date,
                rows=bundle.items,
                source_ref=bundle.source_ref,
            )
