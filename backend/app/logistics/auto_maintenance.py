"""auto_maintenance.py — **규칙만으로 확실한 것만** 자동 정리한다 (사람 개입 없음).

```text
① 폐기대기 Lot 중 **전량이 안전하게 폐기 가능한 것만** 전량 폐기
② 그래서 잔량이 0 이 된 Lot 의 Pallet 자리 반환
③ 과거에 이미 잔량 0 이 됐는데 자리만 남은 Lot 도 ②와 같은 방식으로 정리
```

🔴 **이 파일은 조립만 한다. 규칙의 새 정본이 아니다.** 폐기 규칙도 비우기 규칙도 여기
   없다 — `turnover.load_lot_turnover` 로 후보를 묻고, `disposal.confirm_disposal` 로
   없애고, `warehouse.empty_pallet` 으로 자리를 돌려준다.

```text
UPDATE inventory_lots · INSERT inventory_moves · UPDATE pallets
   🔴 이 파일에 **한 줄도 없다.** 쓰기는 전부 도메인 함수를 통한다.
```

   ★ 그래서 세 도메인 모듈은 서로를 여전히 모른다. `turnover` 는 `disposal` 을 안
     부르고, `disposal` 은 `empty_pallet` 을 안 부른다 — 그 단방향을 못박은 검사
     (`test_18` · `test_55`)가 그대로 살아 있고, 이 파일이 그 **위에** 선다.

---

## 🔴 자동화의 경계 — 확실한 것만 한다

되돌릴 경로가 없다(`ADJUST_IN` 없음 · 실사 제외). 그래서 자동 실행은 사람의 판단이
필요 없을 만큼 **명백한 경우 하나**로만 좁힌다.

```text
자동으로 한다   폐기대기이고 · 살아있는 할당이 없고 · 잔량 전체가 폐기 가능하다
사람에게 남긴다  한 kg 이라도 잡혀 있으면 → SKIPPED_HELD_ALLOCATION (Lot 을 그대로 둔다)
```

🔴 **자동 부분 폐기가 없다.**

```text
remaining 100 · 살아있는 할당 30 · 폐기가능 70
   → 70 을 자동으로 버리지 **않는다.** 통째로 건너뛴다.
```

   ★ 이유: 70 을 버리면 그 Lot 은 *"할당 30 만 남은 Lot"* 이 되는데, 그 30 이 나가고
     나면 아무도 그 Lot 을 다시 안 본다. 남은 일을 사람이 보게 하려면 **손대지 않은
     채로** 남겨야 한다. 자동화는 *"덜 하는 쪽"* 으로 틀린다.

---

## 이 파일이 하지 않는 것

```text
stale inbound 자동 삭제 · 도착 참조 자동 정리   🔴 별도 explicit reconciliation 영역이다
purchase reference 추측                       발주 ID 의 주인은 물류가 아니다
cap_by_date 직접 수정                          용량은 Lot·Move 가 바뀐 **결과**다
등급 문자열 변환 ('상품' → '상/중')              해석을 여기서 얹지 않는다
부분 폐기 · 살아있는 할당 무시                   위 경계가 막는다
Lot 직접 삭제 · Move 직접 삭제                  없애는 경로는 원장 하나뿐이다
```

⚠️ `inbound_reconciliation` 을 **임포트조차 하지 않는다.** `ETA 지남 + purchase_id
   없음 + Receipt 없음` 은 강한 신호이지만 *"발주 참조 전달이 늦은 정상 입고"* 와
   구별되지 않는다 — 그것은 사람이 지목하는 경로다.

---

## 순서와 잠금

```text
① 출고/재고확보 전역 잠금        ← 배치 시작에서 한 번 (아래 이유)
② 폐기대기 후보 조회             turnover.load_lot_turnover  (remaining > 0 만 나온다)
③ Lot 마다 폐기 가능량 확인       disposal._lot_disposable_qty
④ 전량 가능할 때만 confirm_disposal
⑤ 잔량 0 확인                    ④ 의 결과가 말한다
⑥ 그 Lot 의 ACTIVE·HOLD Pallet   warehouse.get_lot_position
⑦ empty_pallet 하나씩
⑧ 커밋은 호출자가 한 번          🔴 이 파일에 commit 도 rollback 도 없다
```

🔴 **①을 배치 시작에서 잡는 이유는 교착 때문이다.**

```text
폐기 경로   출고 전역(…,3) → 원장 전역(…,1) → Lot 행 → 배치 전역(…,4)
자리만 정리  배치 전역(…,4) 하나
```

   ⚠️ 한 실행 안에서 *"자리만 정리할 Lot"* 을 먼저 처리하면 `(…,4) → (…,3)` 순서가
      생기고, 다른 실행의 `(…,3) → (…,4)` 와 **역전**된다. 시작에서 `(…,3)` 을 먼저
      잡아 두면 이 배치의 잠금 순서가 언제나 `3 → 1 → 4` 하나로 고정된다.

---

## 실패는 Lot 단위로 남고, **한 것을 잃지 않는다**

```text
도메인 거절 (DisposalBlocked · PalletNotEmptyable …)   그 Lot 만 SKIPPED/FAILED
그 밖의 예외                                            그대로 올린다
```

🔴 **savepoint 를 쓰지 않고 도메인 예외만 잡는다.** 물류 도메인 함수는 검증을 전부
   **DML 전에** 걸어서(*"DML 전에 막는다"*), 거절이 나도 트랜잭션이 안 더러워진다.
   반면 DB 오류가 났다면 그 트랜잭션은 이미 못 쓰는 상태라 이어 도는 것이 거짓말이다.

🔴 **한 것을 결과에서 잃지 않는다.** Pallet 두 장 중 첫 장을 비운 뒤 둘째에서 막혀도
   비운 장은 `emptied_pallet_ids` 에 그대로 남고, 막힌 사유는 `pallet_cleanup_error`
   에 따로 실린다 — DB 에는 `EMPTIED` 가 적혔는데 결과는 *"아무것도 안 비웠다"* 로
   말하는 어긋남이 자동화에서는 특히 위험하다.

   ★ 폐기까지 됐으면 `outcome` 은 `DISPOSED` 로 남는다. 자리를 못 돌려줬다고
     `FAILED` 로 적으면 **없어진 재고가 결과에서 사라진다.** 대신 `failures` 가
     `pallet_cleanup_error` 있는 줄까지 함께 잡아 아무도 안 보는 실패를 막는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from psycopg import sql

from app.logistics.db import get_db_schema

# 🔴 **폐기 규칙을 다시 적지 않고 가져다 쓴다.** `_lot_disposable_qty` 는 *"이 Lot 에서
#    없애도 되는 양"* 의 정본이라, 여기서 같은 뜻을 다시 적으면 두 곳이 갈린다
#    (같은 패키지의 밑줄 이름을 쓰는 것은 이미 있는 방식이다 —
#    `console_service` ← `outbound._ASSIGNED_ALLOCATION`).
from app.logistics.disposal import DisposalError, _lot_disposable_qty, confirm_disposal
from app.logistics.outbound import lock_outbound_writes
from app.logistics.turnover import load_lot_turnover
from app.logistics.warehouse import WarehouseError, empty_pallet, get_lot_position

__all__ = [
    "AutoMaintenanceResult",
    "InvalidAutoMaintenanceRequest",
    "LotMaintenanceOutcome",
    "MaintenanceOutcome",
    "auto_disposal_id_for",
    "run_logistics_auto_maintenance",
]


#: 자리를 아직 잡고 있는 Pallet 상태. 🔴 `warehouse._OCCUPYING_PALLET` 과 같은 뜻이다 —
#: 이 목록은 *"어느 Lot 을 물어볼까"* 를 고르는 데만 쓰고, 비워도 되는지는
#: `empty_pallet` 이 잠금 안에서 다시 판정한다.
_OCCUPYING_PALLET: tuple[str, ...] = ("ACTIVE", "HOLD")

#: Lot 하나가 이번 사이클에서 어떻게 됐나. 🔴 **DB 어휘가 아니다** — 파이썬 결과값이고
#: 어느 표에도 안 적힌다. 상태 표를 새로 만들지 않으려고 여기 둔다.
MaintenanceOutcome = Literal[
    #: 전량 폐기했다. 잔량 0 · Lot status DISPOSED.
    "DISPOSED",
    #: 살아있는 할당이 있어 **손대지 않았다.** 자동 부분 폐기를 하지 않는다.
    "SKIPPED_HELD_ALLOCATION",
    #: 이미 잔량 0 이라 폐기할 것이 없고, 남은 Pallet 자리만 돌려줬다.
    "PALLETS_EMPTIED",
    #: 도메인이 거절했고 이번 사이클이 이 Lot 에서 아무것도 못 했다. 사람이 봐야 한다.
    "FAILED",
]


class InvalidAutoMaintenanceRequest(ValueError):
    """요청 자체가 성립하지 않는다. **DB 에 묻기 전에 막는다.**

    🔴 `reason_code` · `recorded_by` · `occurred_at` 을 물류가 지어내지 않는다.
       첫째는 `confirm_disposal` 이 *"호출자가 준다"* 로 못박은 칸이고
       (`inventory_moves.reason_code` 에 CHECK 이 없다), 둘째는
       `pallet_events.recorded_by` 가 NOT NULL 인데 **자동화 주체를 코드가 지어내면
       그 이름이 장부에 사실로 남는다.** 셋째는 시뮬레이션 시간축의 주인이 호출자라서다
       (`fefo_allocation.decided_at` 과 같은 규율).
    """


@dataclass(frozen=True)
class LotMaintenanceOutcome:
    """Lot 하나의 결과. **터진 것도 값으로 남는다.**"""

    lot_id: str
    outcome: MaintenanceOutcome
    #: 이번 사이클이 실제로 없앤 양. 안 없앴으면 0.
    disposed_qty_kg: Decimal
    #: 이 Lot 을 처리한 뒤의 잔량.
    remaining_qty_kg: Decimal
    #: 이번 사이클이 **실제로 비운** Pallet. 순서를 지킨다.
    emptied_pallet_ids: tuple[str, ...] = ()
    #: 자리 반환이 도중에 막혔으면 그 사유. 안 막혔으면 `None`.
    #:
    #: 🔴 **`outcome` 과 따로 둔다.** 폐기는 됐는데 자리만 못 돌려준 날이 있고, 그때
    #:    Lot 전체를 `FAILED` 로 적으면 **없어진 재고가 결과에서 사라진다.** 두 사실을
    #:    한 칸에 접지 않는다 — `outcome` 은 재고를, 이 칸은 자리를 말한다.
    pallet_cleanup_error: str | None = None
    #: 왜 건너뛰었나 · 무엇이 터졌나. 사람이 읽는 줄이다.
    reason: str = ""


@dataclass(frozen=True)
class AutoMaintenanceResult:
    """자동 유지보수 1회의 결과.

    ★ **본 것과 한 것을 함께 싣는다.** `examined_lots` 가 없으면 *"0건 처리"* 가
      *"아무것도 안 봤다"* 인지 *"볼 것이 없었다"* 인지 구별되지 않는다.
    """

    as_of: date
    sim_run_id: str
    #: 회전 조회가 훑은 Lot 수 (잔량 > 0 인 것). 후보가 아닌 Lot 은 `lots` 에 안 실린다.
    examined_lots: int
    #: 손댔거나 **일부러 건너뛴** Lot 만. 정상 Lot 은 여기 없다.
    lots: tuple[LotMaintenanceOutcome, ...]

    @property
    def disposed_lot_ids(self) -> tuple[str, ...]:
        return tuple(one.lot_id for one in self.lots if one.outcome == "DISPOSED")

    @property
    def disposed_qty_kg(self) -> Decimal:
        return sum((one.disposed_qty_kg for one in self.lots), start=Decimal(0))

    @property
    def emptied_pallet_ids(self) -> tuple[str, ...]:
        return tuple(pid for one in self.lots for pid in one.emptied_pallet_ids)

    @property
    def skipped(self) -> tuple[LotMaintenanceOutcome, ...]:
        return tuple(one for one in self.lots if one.outcome == "SKIPPED_HELD_ALLOCATION")

    @property
    def failures(self) -> tuple[LotMaintenanceOutcome, ...]:
        """사람이 봐야 하는 줄. **`FAILED` 만 보지 않는다.**

        🔴 폐기는 됐는데 자리 반환이 막힌 Lot 은 `outcome` 이 `DISPOSED` 인 채로
           남는다 (폐기 사실을 숨기지 않으려고). 그 줄을 여기서 빠뜨리면 **아무도
           안 보는 실패**가 된다.
        """
        return tuple(
            one
            for one in self.lots
            if one.outcome == "FAILED" or one.pallet_cleanup_error is not None
        )

    @property
    def pallet_cleanup_failures(self) -> tuple[LotMaintenanceOutcome, ...]:
        """자리 반환만 막힌 줄. 재고 쪽은 성공했을 수 있다."""
        return tuple(one for one in self.lots if one.pallet_cleanup_error is not None)


def auto_disposal_id_for(*, sim_run_id: str, lot_id: str, as_of: date) -> str:
    """자동 폐기 건의 정체성. **순수 계산이고 결정론이다.**

    ```text
    AUTO-{sim_run_id}-{lot_id}-{YYYYMMDD}
    → disposal_move_id_for 가 MOVE-DISPOSE-… 를 얹는다
    ```

    🔴 난수도 벽시계도 시퀀스도 쓰지 않는다. 같은 실행·같은 Lot·같은 날이면 언제
       불러도 같은 값이다.

    ★ **`as_of` 를 넣는다.** `confirm_disposal` 의 멱등 판정은 `moved_at` 까지 대조하고
      이 사이클은 `moved_at = as_of` 로 적는다 — 날짜를 ID 에서 빼면 **ID 는 같은데
      사실이 다른** 조합이 만들어질 수 있고, 그때 재실행이 `DisposalIntegrityError`
      로 영구히 막힌다. ID 와 사실이 같은 축으로 움직이게 둔다.

    ⚠️ **멱등의 본체는 이 ID 가 아니라 잔량이다.** 전량 폐기가 커밋되면 그 Lot 은
       `load_lot_turnover`(잔량 > 0 만 본다)에서 사라져 다음 사이클이 아예 안 집는다.
       이 ID 는 **같은 트랜잭션 안에서 두 번 불렸을 때**를 닫는다.
    """
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(lot_id, 칸="lot_id")
    return f"AUTO-{sim_run_id}-{lot_id}-{as_of:%Y%m%d}"


def _require_text(값: Any, *, 칸: str) -> str:
    if not isinstance(값, str) or not 값.strip():
        raise InvalidAutoMaintenanceRequest(f"자동 유지보수에 쓸 수 없는 {칸} 다: {값!r}")
    return 값


def _lots_needing_pallet_cleanup(
    conn: Any, schema: sql.Identifier, *, sim_run_id: str
) -> list[str]:
    """잔량이 **이미 0** 인데 자리를 아직 잡고 있는 Lot 들.

    ★ `load_lot_turnover` 는 `remaining_qty_kg > 0` 만 돌려주므로 이 축이 그 조회에
      안 잡힌다 — 과거에 출고·폐기로 0 이 됐는데 Pallet 만 남은 경우다
      (수량 정리와 물리 자리는 원래 다른 사실이라 갈릴 수 있다).

    🔴 **여기서 *비워도 되나* 를 판정하지 않는다.** 이 조회는 *"어느 Lot 을 물어볼까"*
       까지만 고른다 — 실제 판정(`remaining == 0` · 상태 · 자리)은 `empty_pallet` 이
       잠금 안에서 다시 한다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT DISTINCT l.lot_id
                FROM {}.inventory_lots AS l
                JOIN {}.pallets AS p ON p.lot_id = l.lot_id
                WHERE l.sim_run_id = %s
                  AND l.remaining_qty_kg = 0
                  AND p.status = ANY(%s)
                ORDER BY l.lot_id
                """
            ).format(schema, schema),
            (sim_run_id, list(_OCCUPYING_PALLET)),
        )
        행들 = cursor.fetchall()
    # ★ 커넥션의 `row_factory` 가 호출자마다 다르다 — dict 든 tuple 이든 같게 읽는다.
    return [str(행["lot_id"] if isinstance(행, dict) else 행[0]) for 행 in 행들]


def _empty_lot_pallets(
    conn: Any,
    *,
    sim_run_id: str,
    lot_id: str,
    occurred_at: datetime,
    recorded_by: str,
    note: str | None,
) -> tuple[tuple[str, ...], str | None]:
    """이 Lot 의 **자리를 잡고 있는** Pallet 을 하나씩 비운다.

    ```text
    돌려주는 것   (실제로 비운 pallet_id 들, 도중에 막혔으면 그 사유)
    ```

    🔴 **예외로 나가지 않는다.** 두 장 중 첫 장을 비운 뒤 둘째에서 막히면, 예외를
       올리는 순간 **이미 비운 첫 장이 결과에서 사라진다** — DB 에는 `EMPTIED` 로
       적혀 있는데 결과는 *"아무것도 안 비웠다"* 로 말하게 된다. 그 어긋남을 막으려고
       성공분과 실패를 **함께** 돌려준다.

    ★ **막히면 그 Lot 의 나머지 장은 이번에 안 건드린다.** 자리 하나가 계약을 어겼다는
      것은 그 Lot 의 배치 사실이 흔들린다는 뜻이라, 이어서 더 비우기보다 사람이 보게
      남긴다. 안 건드린 장은 여전히 `ACTIVE`·`HOLD` 라 **다음 사이클이 다시 집는다.**

    ★ `get_lot_position` 이 `ACTIVE` · `HOLD` 만 돌려준다 — 이미 `EMPTIED` 이거나
      `DISPOSED` 인 Pallet 은 애초에 목록에 없다. 그래서 *"이미 비운 것을 또 비우려"*
      가지 않고, 혹시 가더라도 `empty_pallet` 이 `applied=False` 로 받는다.

    🔴 **`pallets` 를 직접 UPDATE 하지 않는다.** 자리 반환의 정본은 `empty_pallet` 이고
       `pallet_events` 도 그쪽이 적는다.
    """
    비운것: list[str] = []
    for 자리 in get_lot_position(conn, sim_run_id=sim_run_id, lot_id=lot_id):
        try:
            결과 = empty_pallet(
                conn,
                pallet_id=자리.pallet_id,
                occurred_at=occurred_at,
                recorded_by=recorded_by,
                note=note,
            )
        except WarehouseError as exc:
            return tuple(비운것), f"{자리.pallet_id}: {type(exc).__name__}: {exc}"
        if 결과.applied:
            비운것.append(결과.pallet_id)
    return tuple(비운것), None


def run_logistics_auto_maintenance(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    reason_code: str,
    recorded_by: str,
    occurred_at: datetime,
    note: str | None = None,
) -> AutoMaintenanceResult:
    """그날의 자동 유지보수를 돌린다. **사람 개입 없이 도는 유일한 쓰기 경로다.**

    ```text
    폐기대기 + 잔량 전체가 안 잡혀 있음   → 전량 폐기 → 그 Lot Pallet 자리 반환
    폐기대기 + 살아있는 할당 있음         → 손대지 않는다 (SKIPPED_HELD_ALLOCATION)
    폐기대기 아님                         → 손대지 않는다 (결과에도 안 싣는다)
    잔량 이미 0 + Pallet 남음             → Pallet 자리만 반환 (폐기 Move 없음)
    ```

    🔴 **부분 폐기를 하지 않는다.** 없애는 양은 언제나 `remaining_qty_kg` 전량이고,
       전량을 못 없애면 한 kg 도 안 없앤다.

    🔴 **`reason_code` · `recorded_by` · `occurred_at` 을 물류가 지어내지 않는다.**
       기본값을 두면 묻지도 않은 사유와 행위자가 장부에 사실로 선다.

    🔴 **커밋도 롤백도 하지 않는다.** 전역 잠금도 행 잠금도 트랜잭션 수명이라 호출자의
       커밋/롤백과 함께 풀린다. Lot 마다 커밋하지 않는다 — 한 사이클은 한 사실이다.

    :param conn: 호출자가 소유한 커넥션. 이 함수는 수명을 관리하지 않는다.
    :param as_of: 폐기대기 판정 기준일이자 `moved_at`. **호출자의 달력값이다.**
    :param occurred_at: Pallet 사건 시각. 🔴 벽시계를 여기서 읽지 않는다 —
        시간축의 주인은 호출자다 (`fefo_allocation.decided_at` 과 같은 규율).
    :raises InvalidAutoMaintenanceRequest: 축이나 어휘가 비었을 때. **DML 전에 막는다.**
    """
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(reason_code, 칸="reason_code")
    _require_text(recorded_by, 칸="recorded_by")
    if not isinstance(occurred_at, datetime):
        raise InvalidAutoMaintenanceRequest(f"occurred_at 은 datetime 이어야 한다: {occurred_at!r}")

    schema = sql.Identifier(get_db_schema())

    # ── ① 배치 전체의 잠금 순서를 여기서 고정한다 ─────────────────────
    #    ⚠️ 자리만 정리할 Lot 이 먼저 오면 (…,4) → (…,3) 순서가 생겨 다른 실행과
    #       역전된다. 시작에서 (…,3) 을 잡아 두면 이 배치는 늘 3 → 1 → 4 다.
    with conn.cursor() as cursor:
        lock_outbound_writes(cursor)

    결과들: list[LotMaintenanceOutcome] = []
    다룬Lot: set[str] = set()

    # ── ② 폐기대기 후보 ───────────────────────────────────────────────
    회전 = load_lot_turnover(conn, sim_run_id=sim_run_id, as_of=as_of)
    for lot in 회전:
        if not lot.disposal_candidate:
            # ★ 정상 Lot 이다. 결과에 싣지 않는다 — 매일 전 재고가 목록에 뜨면
            #   정작 손댄 것이 안 보인다. 본 개수는 `examined_lots` 가 말한다.
            continue
        다룬Lot.add(lot.lot_id)
        결과들.append(
            _maintain_one_lot(
                conn,
                schema,
                sim_run_id=sim_run_id,
                lot_id=lot.lot_id,
                remaining_qty_kg=lot.remaining_qty_kg,
                as_of=as_of,
                reason_code=reason_code,
                recorded_by=recorded_by,
                occurred_at=occurred_at,
                note=note,
            )
        )

    # ── ③ 잔량이 이미 0 인데 자리를 잡고 있는 Lot ─────────────────────
    for lot_id in _lots_needing_pallet_cleanup(conn, schema, sim_run_id=sim_run_id):
        if lot_id in 다룬Lot:
            # ★ 방금 전량 폐기한 Lot 이다 — 자리는 그 흐름에서 이미 돌려줬다.
            continue
        비운것, 막힌것 = _empty_lot_pallets(
            conn,
            sim_run_id=sim_run_id,
            lot_id=lot_id,
            occurred_at=occurred_at,
            recorded_by=recorded_by,
            note=note,
        )
        if not 비운것 and 막힌것 is None:
            continue
        결과들.append(
            LotMaintenanceOutcome(
                lot_id=lot_id,
                # ★ **한 장이라도 비웠으면 그것이 사실이다.** 뒤에서 막혔다고
                #   `FAILED` 로 적으면 이미 돌려준 자리가 결과에서 사라진다.
                outcome="PALLETS_EMPTIED" if 비운것 else "FAILED",
                disposed_qty_kg=Decimal(0),
                remaining_qty_kg=Decimal(0),
                emptied_pallet_ids=비운것,
                pallet_cleanup_error=막힌것,
                reason="잔량이 이미 0 이라 폐기 없이 자리만 돌려줬다" if 비운것 else "",
            )
        )

    return AutoMaintenanceResult(
        as_of=as_of,
        sim_run_id=sim_run_id,
        examined_lots=len(회전),
        lots=tuple(결과들),
    )


def _maintain_one_lot(
    conn: Any,
    schema: sql.Identifier,
    *,
    sim_run_id: str,
    lot_id: str,
    remaining_qty_kg: Decimal,
    as_of: date,
    reason_code: str,
    recorded_by: str,
    occurred_at: datetime,
    note: str | None,
) -> LotMaintenanceOutcome:
    """폐기대기 Lot 하나. **③ 확인 → ④ 폐기 → ⑤ 잔량 → ⑥⑦ 자리** 순서를 지킨다.

    🔴 **폐기보다 Pallet 을 먼저 비우지 않는다.** `empty_pallet` 은 잔량 0 을 요구하고,
       잔량을 0 으로 만드는 것은 폐기다 — 순서를 뒤집으면 아무것도 못 비운다.
    """
    # ── ③ 전량을 안전하게 없앨 수 있나 ────────────────────────────────
    #    ★ 판정 자체는 `disposal` 것을 그대로 쓴다. `confirm_disposal` 도 잠금 안에서
    #      같은 검사를 다시 하므로, 여기 통과가 곧 최종 허가는 아니다 (이중 방어).
    폐기가능, _ = _lot_disposable_qty(conn, schema, sim_run_id=sim_run_id, lot_id=lot_id)
    if 폐기가능 != remaining_qty_kg:
        return LotMaintenanceOutcome(
            lot_id=lot_id,
            outcome="SKIPPED_HELD_ALLOCATION",
            disposed_qty_kg=Decimal(0),
            remaining_qty_kg=remaining_qty_kg,
            reason=(
                f"살아있는 할당이 있어 전량 폐기가 아니다 (잔량 {remaining_qty_kg}"
                f" · 폐기가능 {폐기가능}). 자동 부분 폐기를 하지 않는다 —"
                " 남은 판단은 사람 몫이다."
            ),
        )

    # ── ④ 없애는 것은 폐기 함수뿐이다 ─────────────────────────────────
    try:
        폐기 = confirm_disposal(
            conn,
            disposal_id=auto_disposal_id_for(sim_run_id=sim_run_id, lot_id=lot_id, as_of=as_of),
            sim_run_id=sim_run_id,
            lot_id=lot_id,
            # 🔴 **언제나 전량이다.** 부분 폐기 경로를 자동화가 쓰지 않는다.
            quantity_kg=remaining_qty_kg,
            disposed_at=as_of,
            reason_code=reason_code,
            as_of=as_of,
            note=note,
        )
    except DisposalError as exc:
        return LotMaintenanceOutcome(
            lot_id=lot_id,
            outcome="FAILED",
            disposed_qty_kg=Decimal(0),
            remaining_qty_kg=remaining_qty_kg,
            reason=f"{type(exc).__name__}: {exc}",
        )

    # ── ⑤ 잔량이 0 이어야 자리를 돌려준다 ─────────────────────────────
    if 폐기.remaining_qty_kg != 0:
        # ★ 여기 오면 안 되지만(전량을 넘겼다) 잔량을 다시 묻는 것이 규율이다 —
        #   0 이 아닌데 자리를 비우면 **물건이 실린 Pallet 을 내주는 것**이다.
        return LotMaintenanceOutcome(
            lot_id=lot_id,
            outcome="DISPOSED",
            disposed_qty_kg=폐기.disposed_qty_kg,
            remaining_qty_kg=폐기.remaining_qty_kg,
            reason="잔량이 0 이 아니라 자리는 그대로 둔다",
        )

    # ── ⑥ · ⑦ 자리 반환 ──────────────────────────────────────────────
    비운것, 막힌것 = _empty_lot_pallets(
        conn,
        sim_run_id=sim_run_id,
        lot_id=lot_id,
        occurred_at=occurred_at,
        recorded_by=recorded_by,
        note=note,
    )
    # ⚠️ **폐기를 되돌리지 않고 숨기지도 않는다.** 물건은 없어졌고, 자리를 못 돌려준
    #    것은 다른 사실이다 — `outcome` 은 앞엣것을, `pallet_cleanup_error` 는 뒤엣것을
    #    말한다. 비운 장이 있으면 그 목록도 그대로 싣는다.
    return LotMaintenanceOutcome(
        lot_id=lot_id,
        outcome="DISPOSED",
        disposed_qty_kg=폐기.disposed_qty_kg,
        remaining_qty_kg=폐기.remaining_qty_kg,
        emptied_pallet_ids=비운것,
        pallet_cleanup_error=막힌것,
    )
