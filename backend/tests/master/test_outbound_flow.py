"""**하루의 출고 조립 — 무엇을 고르고, 무엇을 안 하는가.**

⚠️ **DB 를 안 탄다.** 연결 · 조회 · 물류 세 함수 · 판매 lifecycle 훅을 전부 대역으로
준다. 실물 예약이 아직 0행이라 DB 를 타 봐야 매일 `NOTHING_DUE` 밖에 못 본다.

🔴 **여기서 잠그는 것 여덟.**

```text
① 그날 sale_date 인 것만 나간다
② decided_at 이 phase_instant(as_of, "ALLOCATE") 다 — 벽시계가 아니다
③ 한 판매가 터져도 나머지가 돈다
④ 나갈 것이 없으면 NOTHING_DUE 다 — BLOCKED 도 FAILED 도 아니다
⑤ 한 판매의 **일부 품목만** 나갔으면 mark_sale_delivered 를 안 부른다
   그리고 **한 품목이 요구량보다 적게** 나가도 안 부른다 (PR #484 §5.2)
⑥ 장부 관문이 막은 날은 출고도 안 돈다
⑦ ship 이 터져도 allocate 를 안 되돌린다
⑧ 확보 0kg 은 SHORT 다 — FAILED 도 NOTHING_DUE 도 아니다 (PR #484 §5.1)
```
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.master import outbound_flow
from app.master.outbound_flow import (
    DueSaleItem,
    SaleItemOutcome,
    fully_shipped_sales,
    ship_due_sales,
)
from app.master.sim_time import phase_instant

AS_OF = date(2026, 9, 8)
OTHER_DAY = date(2026, 9, 9)


# ── 대역 ────────────────────────────────────────────────────────────────


class _Conn:
    """연결 대역. **commit · rollback 이 언제 났는지**를 순서째로 적어 둔다."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.closed = False

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.closed = True


#: 판매 한 줄이 요구하는 양. `_row` 가 이 값으로 판매 품목을 만든다.
REQUIRED = Decimal(100)


@dataclass
class _Shipped:
    """`ShipmentResult` 의 최소 모양. 이 파일이 보는 칸만 있다."""

    shipped_qty_kg: Decimal = Decimal(0)


@dataclass
class _Reserved:
    """`ReservationResult` 의 최소 모양. 이 파일이 보는 칸만 있다.

    🔴 **`reserved_qty_kg` 를 대역이 실제로 낸다.** 전에는 예약 대역도 `_Shipped` 를
      돌려줘서 이 칸이 아예 없었다 — 그러면 *"확보가 0이었다"* 를 검사할 수가 없다.
    """

    reserved_qty_kg: Decimal = REQUIRED


class _Spy:
    """물류·판매 함수 대역. 부른 인자를 그대로 모으고, 지정한 건에서만 터진다."""

    def __init__(
        self, name: str, conn: _Conn, *, boom_on: str | None = None, result: Any = None
    ) -> None:
        self.name = name
        self.conn = conn
        self.boom_on = boom_on
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: Any, *args: Any, **kwargs: Any) -> Any:
        payload = dict(kwargs)
        if args:
            payload["positional"] = args[0]
        self.calls.append(payload)
        self.conn.events.append(self.name)
        target = _target_of(payload)
        if self.boom_on is not None and self.boom_on in (target or ""):
            raise RuntimeError(f"{self.name} 이 터졌다: {target}")
        return self.result


def _target_of(payload: dict[str, Any]) -> str | None:
    """대역이 *"이번 건이 무엇인가"* 를 알아보는 자리. 예약 이름 또는 판매 id."""
    if "reservation_id" in payload:
        return str(payload["reservation_id"])
    if "sale_id" in payload:
        return str(payload["sale_id"])
    request = payload.get("positional")
    return str(getattr(request, "reservation_id", "")) or None


def _row(
    sale_id: str, seq: int, *, sale_date: date = AS_OF, item_id: str = "ITEM-배추"
) -> DueSaleItem:
    return DueSaleItem(
        sale_id=sale_id,
        sale_item_id=f"SI-{sale_id}-{seq}",
        item_id=item_id,
        sim_run_id="SIM-1",
        quantity_kg=REQUIRED,
        sale_date=sale_date,
    )


def _run(
    rows: list[DueSaleItem],
    *,
    conn: _Conn | None = None,
    reserve_boom: str | None = None,
    allocate_boom: str | None = None,
    ship_boom: str | None = None,
    deliver_boom: str | None = None,
    reserved: Decimal = REQUIRED,
    shipped: Decimal = REQUIRED,
) -> tuple[Any, dict[str, _Spy], _Conn]:
    conn = _Conn() if conn is None else conn
    spies = {
        "reserve": _Spy("reserve", conn, boom_on=reserve_boom, result=_Reserved(reserved)),
        "allocate": _Spy("allocate", conn, boom_on=allocate_boom),
        "ship": _Spy("ship", conn, boom_on=ship_boom, result=_Shipped(shipped)),
        "deliver": _Spy("deliver", conn, boom_on=deliver_boom),
    }
    out = ship_due_sales(
        AS_OF,
        connect=lambda: conn,
        due_fn=lambda _conn, *, as_of: tuple(rows),
        reserve_fn=spies["reserve"],
        allocate_fn=spies["allocate"],
        ship_fn=spies["ship"],
        deliver_fn=spies["deliver"],
    )
    return out, spies, conn


# ── ① 그날 sale_date 인 것만 나간다 ────────────────────────────────────


def test_다른_날_납품분은_안_나간다():
    """🔴 `sales.sale_date` 가 정본이다. 섞이면 앞뒤 날 재고가 전부 틀린다."""
    out, spies, _ = _run([_row("SALE-오늘", 1), _row("SALE-내일", 1, sale_date=OTHER_DAY)])

    assert [one.sale_id for one in out.items] == ["SALE-오늘"]
    나간것 = [call["reservation_id"] for call in spies["ship"].calls]
    assert 나간것 == ["RSV-SI-SALE-오늘-1"]


def test_전부_다른_날이면_NOTHING_DUE():
    out, spies, _ = _run([_row("SALE-내일", 1, sale_date=OTHER_DAY)])

    assert out.status == "NOTHING_DUE"
    assert spies["ship"].calls == []


# ── ② decided_at 은 파생값이다 ─────────────────────────────────────────


def test_decided_at_은_ALLOCATE_단계에서_파생한다():
    """🔴 벽시계면 같은 하루를 다시 돌릴 때 값이 달라진다."""
    out, spies, _ = _run([_row("SALE-A", 1)])

    assert out.status == "RAN"
    assert [call["decided_at"] for call in spies["allocate"].calls] == [
        phase_instant(AS_OF, "ALLOCATE")
    ]


def test_decided_at_은_같은_날을_두_번_돌려도_같다():
    _, first, _ = _run([_row("SALE-A", 1)])
    _, second, _ = _run([_row("SALE-A", 1)])

    assert first["allocate"].calls[0]["decided_at"] == second["allocate"].calls[0]["decided_at"]


def test_같은_하루의_할당은_모두_같은_시각이다():
    _, spies, _ = _run([_row("SALE-A", 1), _row("SALE-B", 1)])

    시각들 = {call["decided_at"] for call in spies["allocate"].calls}
    assert len(시각들) == 1


# ── ③ 한 판매가 터져도 나머지가 돈다 ───────────────────────────────────


def test_한_판매가_터져도_나머지는_계속_돈다():
    out, spies, _ = _run(
        [_row("SALE-A", 1), _row("SALE-B", 1), _row("SALE-C", 1)],
        ship_boom="RSV-SI-SALE-B-1",
    )

    assert out.status == "RAN"
    assert out.failed_items == ("SI-SALE-B-1",)
    나간것 = [call["reservation_id"] for call in spies["ship"].calls]
    assert 나간것 == ["RSV-SI-SALE-A-1", "RSV-SI-SALE-B-1", "RSV-SI-SALE-C-1"]
    assert out.delivered_sales == ("SALE-A", "SALE-C")


def test_할당이_터진_판매도_나머지를_안_세운다():
    out, spies, _ = _run([_row("SALE-A", 1), _row("SALE-B", 1)], allocate_boom="RSV-SI-SALE-A-1")

    assert out.failed_items == ("SI-SALE-A-1",)
    assert [call["reservation_id"] for call in spies["allocate"].calls] == [
        "RSV-SI-SALE-A-1",
        "RSV-SI-SALE-B-1",
    ]


# ── ④ 나갈 것이 없으면 NOTHING_DUE ─────────────────────────────────────


def test_나갈_것이_없으면_NOTHING_DUE_이고_BLOCKED_가_아니다():
    """🔴 예약이 아직 0행이라 이것이 지금의 매일이다. 실패로 접으면 매일이 빨갛다."""
    out, spies, conn = _run([])

    assert out.status == "NOTHING_DUE"
    assert out.status not in ("BLOCKED", "FAILED")
    assert spies["reserve"].calls == []
    assert conn.closed is True


def test_조회가_터지면_FAILED_이고_NOTHING_DUE_가_아니다():
    """⚠️ 못 읽은 것을 *"없다"* 로 만들지 않는다."""

    def _boom(_conn: Any, *, as_of: date) -> Any:
        raise RuntimeError("DB 가 죽었다")

    conn = _Conn()
    out = ship_due_sales(AS_OF, connect=lambda: conn, due_fn=_boom)

    assert out.status == "FAILED"
    assert "DB 가 죽었다" in out.reason


# ── ⑤ 일부만 나갔으면 DELIVERED 로 안 적는다 ───────────────────────────


def test_한_판매의_일부_품목만_나가면_DELIVERED_로_안_적는다():
    """🔴 일부만 나갔는데 `DELIVERED` 면 나머지 품목이 영원히 안 나간다."""
    out, spies, _ = _run(
        [_row("SALE-A", 1), _row("SALE-A", 2), _row("SALE-A", 3)],
        ship_boom="RSV-SI-SALE-A-2",
    )

    assert out.failed_items == ("SI-SALE-A-2",)
    assert out.delivered_sales == ()
    assert spies["deliver"].calls == []


def test_모든_품목이_나간_판매만_DELIVERED_로_적는다():
    out, spies, _ = _run(
        [_row("SALE-A", 1), _row("SALE-A", 2), _row("SALE-B", 1)],
        ship_boom="RSV-SI-SALE-A-2",
    )

    assert out.delivered_sales == ("SALE-B",)
    assert [call["sale_id"] for call in spies["deliver"].calls] == ["SALE-B"]


def test_품목이_하나뿐인_판매는_그것만_나가면_DELIVERED():
    out, spies, _ = _run([_row("SALE-A", 1)])

    assert out.delivered_sales == ("SALE-A",)
    assert [call["sale_id"] for call in spies["deliver"].calls] == ["SALE-A"]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("RAN", "RAN"), ("SALE-A",)),
        (("RAN", "FAILED"), ()),
        (("FAILED", "RAN"), ()),
        (("FAILED", "FAILED"), ()),
        (("RAN", "SHORT"), ()),
        (("SHORT", "SHORT"), ()),
    ],
)
def test_모두_RAN_일_때만_완주다(statuses: tuple[str, ...], expected: tuple[str, ...]):
    results = [
        SaleItemOutcome(
            sale_id="SALE-A",
            sale_item_id=f"SI-SALE-A-{i}",
            reservation_id=f"RSV-SI-SALE-A-{i}",
            status=status,  # type: ignore[arg-type]
            shipped_qty_kg=REQUIRED,
            required_qty_kg=REQUIRED,
        )
        for i, status in enumerate(statuses, start=1)
    ]

    assert fully_shipped_sales(results) == expected


def test_부분_출고는_DELIVERED_가_아니다():
    """🔴 **`RAN` 은 「단계를 탔다」이지 「완납했다」가 아니다** (물류 PR #484 §5.2).

    100kg 주문에 60kg 이 나가도 예약 → 할당 → 출고는 끝까지 돈다. 그것을 `DELIVERED`
    로 닫으면 나머지 40kg 이 영원히 안 나간다 — 다음 날 `order_status` 필터가 그
    판매를 아예 안 집기 때문이다.
    """
    out, spies, _ = _run([_row("SALE-A", 1)], reserved=Decimal(60), shipped=Decimal(60))

    assert out.items[0].status == "RAN", "단계는 탔다 — 상태까지 바꾸지 않는다"
    assert out.items[0].shipped_qty_kg == Decimal(60)
    assert out.items[0].required_qty_kg == REQUIRED
    assert out.delivered_sales == ()
    assert spies["deliver"].calls == []


def test_요구량만큼_나가면_DELIVERED_다():
    """★ 위 검사의 짝. 조건이 *"항상 거짓"* 이 아니라 **양을 본다**는 것을 잠근다."""
    out, spies, _ = _run([_row("SALE-A", 1)], shipped=REQUIRED)

    assert out.delivered_sales == ("SALE-A",)
    assert [call["sale_id"] for call in spies["deliver"].calls] == ["SALE-A"]


def test_요구량을_넘겨_나가도_완납이다():
    """⚠️ `>` 가 아니라 `>=` 다 — 딱 맞게 나간 날이 완납에서 빠지면 안 된다."""
    out, _, _ = _run([_row("SALE-A", 1)], shipped=Decimal(120))

    assert out.delivered_sales == ("SALE-A",)


# ── ⑧ 확보 0kg 은 shortage 다 — FAILED 가 아니다 ───────────────────────


def test_확보가_0kg_이면_할당을_안_부른다():
    """🔴 **없는 예약을 할당하지 않는다** (물류 PR #484 §5.1).

    예전에는 결과를 안 보고 그대로 `allocate` 로 갔고, 예약 행이 없어
    `OutboundIntegrityError` 가 났다 — **정상 사업 결과가 장애로 기록됐다.**
    """
    out, spies, _ = _run([_row("SALE-A", 1)], reserved=Decimal(0))

    assert spies["allocate"].calls == []
    assert spies["ship"].calls == []
    assert out.items[0].status == "SHORT"


def test_확보_0kg_은_FAILED_가_아니다():
    """`required=100, reserved=0` 은 **정상 사업 결과인 shortage** 다."""
    out, _, _ = _run([_row("SALE-A", 1)], reserved=Decimal(0))

    assert out.status == "RAN"
    assert out.failed_items == (), "터진 것이 아니다"
    assert out.short_items == ("SI-SALE-A-1",), "그렇다고 아무 일도 없던 것도 아니다"
    assert "확보 0kg" in out.reason


def test_확보_0kg_은_완납이_아니다():
    """★ 나간 것이 없으므로 `DELIVERED` 로 닫히면 안 된다."""
    out, spies, _ = _run([_row("SALE-A", 1)], reserved=Decimal(0))

    assert out.delivered_sales == ()
    assert spies["deliver"].calls == []


def test_확보_0kg_은_NOTHING_DUE_와_다른_사실이다():
    """🔴 **없는 것과 해 봤는데 0인 것은 다르다.**

    `NOTHING_DUE` 는 *"그날 나갈 판매가 없다"* 이고 `SHORT` 는 *"나갈 판매가
    있었는데 확보가 0이었다"* 다. 접으면 재고가 모자란 날과 주문이 없는 날이
    장부에서 같아 보인다.
    """
    부족, _, _ = _run([_row("SALE-A", 1)], reserved=Decimal(0))
    없는날, _, _ = _run([_row("SALE-A", 1, sale_date=OTHER_DAY)])

    assert 부족.status == "RAN"
    assert 없는날.status == "NOTHING_DUE"
    assert 부족.items[0].status not in ("NOTHING_DUE", "FAILED")


def test_한_품목이_부족해도_나머지는_나간다():
    """★ shortage 가 그날을 세우지 않는다 — 실패 규율과 같은 자리다."""
    out, spies, _ = _run([_row("SALE-A", 1), _row("SALE-B", 1)], reserved=Decimal(0))

    assert len(spies["reserve"].calls) == 2
    assert [one.status for one in out.items] == ["SHORT", "SHORT"]


def test_확보량을_못_읽으면_예전대로_할당까지_간다():
    """🔴 **칸이 없는 것은 0이 아니다.**

    *"확보가 0이었다"* 와 *"얼마나 확보됐는지 못 읽었다"* 는 다른 사실이다. 못 읽은
    것을 0으로 접으면 물류가 칸 이름을 바꾼 날 **모든 출고가 조용히 shortage** 가
    되고, 그러면 아무 소리 없이 아무것도 안 나간다.
    """
    conn = _Conn()
    spies = {
        "reserve": _Spy("reserve", conn, result=object()),
        "allocate": _Spy("allocate", conn),
        "ship": _Spy("ship", conn, result=_Shipped(REQUIRED)),
        "deliver": _Spy("deliver", conn),
    }
    out = ship_due_sales(
        AS_OF,
        connect=lambda: conn,
        due_fn=lambda _conn, *, as_of: (_row("SALE-A", 1),),
        reserve_fn=spies["reserve"],
        allocate_fn=spies["allocate"],
        ship_fn=spies["ship"],
        deliver_fn=spies["deliver"],
    )

    assert len(spies["allocate"].calls) == 1
    assert out.items[0].status == "RAN"
    assert out.short_items == ()


def test_DELIVERED_가_터져도_출고를_안_되돌린다():
    """⚠️ 물건은 이미 나갔다. 판매 상태가 못 따라온 것은 별개 사실이다."""
    out, _, _ = _run([_row("SALE-A", 1)], deliver_boom="SALE-A")

    assert out.items[0].status == "RAN"
    assert out.delivered_sales == ()
    assert any("DELIVERED" in note for note in out.notes)


# ── ⑦ ship 이 터져도 allocate 를 안 되돌린다 ───────────────────────────


def test_ship_이_터져도_allocate_는_커밋된_뒤다():
    """🔴 할당은 *"어느 Lot 에서 뺄지 정했다"*, 출고는 *"나갔다"* — 다른 사실이다."""
    _, _, conn = _run([_row("SALE-A", 1)], ship_boom="RSV-SI-SALE-A-1")

    assert conn.events == ["reserve", "commit", "allocate", "commit", "ship", "rollback"]


def test_되돌리는_함수를_임포트조차_안_한다():
    """★ `cancel_allocation` 이 이 모듈에 없다 — 실수로 부를 자리가 없다."""
    assert not hasattr(outbound_flow, "cancel_allocation")


# ── 순서 · 어휘 ────────────────────────────────────────────────────────


def test_예약_이름은_계산한다():
    """🔴 봉투에 안 적고 `sale_item_id` 에서 만든다 — 결정론이다."""
    _, spies, _ = _run([_row("SALE-A", 7)])

    request = spies["reserve"].calls[0]["positional"]
    assert request.reservation_id == "RSV-SI-SALE-A-7"
    assert request.quantity_kg == Decimal(100)


def test_출고일은_as_of_다():
    _, spies, _ = _run([_row("SALE-A", 1)])

    assert spies["ship"].calls[0]["shipped_at"] == AS_OF
    assert spies["ship"].calls[0]["sale_item_id"] == "SI-SALE-A-1"


def test_전량_예약_함수를_안_부른다():
    """🔴 시뮬레이션 경로는 `reserve_confirmed_sale_available` 이다."""
    assert outbound_flow.ship_due_sales.__defaults__ is None
    import inspect

    기본값 = inspect.signature(ship_due_sales).parameters["reserve_fn"].default
    assert 기본값.__name__ == "reserve_confirmed_sale_available"
