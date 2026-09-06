"""승인 약정 → `in_transit` · `confirmed_inbound` 반영의 build·persist 검사.

★ **DB 를 부르지 않는다.** 가짜 커넥션·커서로 잰다. 여기서 재는 것은 값이 DB 에
  들어갔는지가 아니라 **물류가 소유한 규율 다섯**이다.

  ```text
  회차 값을 그대로 옮기나           도착일을 다시 계산하지 않는다
  같은 승인이 같은 id 를 내나       두 번 반영해도 부풀지 않는다
  커밋·커넥션을 쥐지 않나           트랜잭션은 마스터 것이다
  없는 행을 지어내지 않나           evidence_grade 는 물류 판단이다
  남의 칸을 덮지 않나               confirmed_inbound 는 병합이지 덮어쓰기가 아니다
  ```
"""

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import transition
from app.logistics.schemas import (
    InTransitItem,
    InventoryLogisticsSnapshot,
    ScheduledQuantity,
)
from app.logistics.tools import find_in_transit_schedule_gap
from app.logistics.transition import (
    InventoryTransition,
    LogisticsFixtureMissing,
    LogisticsTransitionAdapter,
    build_next_inventory,
    persist_inventory,
)
from app.master.commitment import ApprovedCommitment, ArrivalLeg

AS_OF = date(2025, 12, 31)
SIM_RUN_ID = "LOG-RUNTIME-SIM-BURNIN-202512-DAY30"
#: 승인 다음 달력일. 마스터가 정해 `build` 로 준다 — 물류가 세지 않는다.
TARGET_STATE_DATE = AS_OF + timedelta(days=1)

#: 물류가 이미 확정해 둔 입고 한 건. **이번 승인과 무관한 남의 사실이다** — 승인
#: 반영 뒤에도 그대로 있어야 한다.
남의_확정입고 = {
    "inbound_id": "INB-OTHER-9",
    "item": "무",
    "quantity_kg": "120.5",
    "date": "2026-01-04",
}


class 가짜커서:
    """실행된 SQL 과 파라미터를 기록한다. `rowcount` · 읽어 줄 값은 밖에서 정한다.

    ★ `persist_inventory` 는 **읽고 나서 쓴다** — `fetchone` 이 그 읽기다.
      `rowcount=0` 은 그날 fixture 행이 없다는 뜻이라 읽기도 빈손이어야 한다.
    """

    def __init__(self, rowcount: int, in_transit: object, confirmed_inbound: object) -> None:
        self.rowcount = rowcount
        self.queries: list[object] = []
        self.params: list[object] = []
        # ★ 칸 순서가 `persist_inventory` 의 SELECT 와 짝이다 —
        #   `SELECT in_transit_json, confirmed_inbound_json`.
        self._행 = None if rowcount == 0 else (in_transit, confirmed_inbound)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        self.queries.append(query)
        self.params.append(params)

    def fetchone(self) -> object:
        return self._행


#: 기본값과 **명시적 `None`** 을 가르는 표식. `None` 은 *"아직 확인한 적 없다"*
#: (`UNRESOLVED`) 라는 사실이라 기본값으로 뭉개면 안 된다.
_기본 = object()


class 가짜커넥션:
    """commit 이 **몇 번** 불렸나를 센다 — 0 이어야 한다."""

    def __init__(
        self,
        rowcount: int = 1,
        confirmed_inbound: object = _기본,
        in_transit: object = _기본,
    ) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.커서 = 가짜커서(
            rowcount,
            [] if in_transit is _기본 else in_transit,
            [] if confirmed_inbound is _기본 else confirmed_inbound,
        )

    def cursor(self) -> 가짜커서:
        return self.커서

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _is_write(query: str) -> bool:
    """쓰기 질의인가.

    ⚠️ `"UPDATE" in query` 로 재면 안 된다 — fixture 행 **잠금**이
       `SELECT … FOR UPDATE` 라 읽기가 쓰기로 잡힌다. 잠금 문구를 걷어내고 본다.
    """
    return "INSERT" in query or "UPDATE" in query.replace("FOR UPDATE", "")


def _update_params(conn: 가짜커넥션) -> tuple[Any, ...]:
    """UPDATE 로 넘어간 파라미터. **SELECT 가 앞에 하나 더 있다.**"""
    assert len(conn.커서.params) == 2, "읽기 한 번 · 쓰기 한 번이다"
    params = conn.커서.params[-1]
    assert isinstance(params, tuple)
    return params


def _written_in_transit(conn: 가짜커넥션) -> list[dict[str, Any]] | None:
    written = _update_params(conn)[0]
    return None if written is None else written.obj


def _written_in_transit_status(conn: 가짜커넥션) -> str:
    return _update_params(conn)[1]


def _written_confirmed_inbound(conn: 가짜커넥션) -> list[dict[str, Any]] | None:
    written = _update_params(conn)[2]
    return None if written is None else written.obj


def _written_confirmed_status(conn: 가짜커넥션) -> str:
    return _update_params(conn)[3]


def _commitment(
    *,
    legs: tuple[ArrivalLeg, ...],
    total_qty_kg: float,
    approval_id: str = "H1-REQ-1-1",
) -> ApprovedCommitment:
    return ApprovedCommitment(
        approval_id=approval_id,
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario_label="보수",
        total_qty_kg=total_qty_kg,
        total_amount_krw=1_000_000.0,
        arrival_schedule=legs,
        inbound_lead_days=2.0,
    )


def _두회차() -> ApprovedCommitment:
    return _commitment(
        legs=(
            ArrivalLeg(
                item="배추",
                qty_kg=300.0,
                arrival_date=date(2026, 1, 2),
                purchase_date=AS_OF,
                seq=1,
            ),
            ArrivalLeg(
                item="배추",
                qty_kg=200.0,
                arrival_date=date(2026, 1, 5),
                purchase_date=date(2026, 1, 3),
                seq=2,
            ),
        ),
        total_qty_kg=500.0,
    )


def _fixture_인자(rows) -> dict[str, object]:
    return {
        "sim_run_id": SIM_RUN_ID,
        "as_of": AS_OF,
        "rows": rows,
        "source_ref": "APPROVAL:H1-REQ-1-1",
    }


# ── build_next_inventory ────────────────────────────────────────────────


def test_build_carries_each_arrival_leg_as_in_transit_item():
    """회차 둘 → `InTransitItem` 둘. 품목·수량·도착일이 약정 그대로다."""
    rows = build_next_inventory(_두회차())

    assert len(rows) == 2
    assert [row.item for row in rows] == ["배추", "배추"]
    assert [row.quantity_kg for row in rows] == [Decimal("300.0"), Decimal("200.0")]
    assert [row.expected_arrival_date for row in rows] == [date(2026, 1, 2), date(2026, 1, 5)]


def test_build_makes_inbound_id_from_approval_and_seq():
    """`INB-{approval_id}-{seq}` 다."""
    rows = build_next_inventory(_두회차())

    assert [row.inbound_id for row in rows] == ["INB-H1-REQ-1-1-1", "INB-H1-REQ-1-1-2"]


def test_build_is_idempotent_for_the_same_commitment():
    """같은 약정을 두 번 부르면 같은 id 가 나온다.

    ★ 순번 카운터나 난수를 쓰면 두 번째 반영이 같은 물건을 다른 건으로 만들어
      `in_transit` 이 부푼다 — 갱신이 멱등하지 않게 되는 자리가 정확히 여기다.
    """
    첫번 = build_next_inventory(_두회차())
    두번 = build_next_inventory(_두회차())

    assert [row.inbound_id for row in 첫번] == [row.inbound_id for row in 두번]


def test_build_does_not_recompute_arrival_date_from_purchase_date():
    """🔴 **도착일을 다시 계산하지 않는다.**

    약정에 `purchase_date + N` 과 어긋나는 도착일을 일부러 넣고, 그 값이 **그대로**
    실리는지 본다. 여기서 다시 더하면 같은 사실의 주인이 둘이 된다.
    """
    어긋난_도착일 = date(2026, 2, 20)  # purchase_date(2025-12-31) + N 과 무관한 값
    commitment = _commitment(
        legs=(
            ArrivalLeg(
                item="배추",
                qty_kg=100.0,
                arrival_date=어긋난_도착일,
                purchase_date=AS_OF,
                seq=1,
            ),
        ),
        total_qty_kg=100.0,
    )

    rows = build_next_inventory(commitment)

    assert rows[0].expected_arrival_date == 어긋난_도착일
    assert rows[0].expected_arrival_date != AS_OF


def test_build_returns_empty_list_for_empty_arrival_schedule():
    """빈 일정은 예외가 아니다 — 반영할 입고 예정이 **없다**는 정상 상태다."""
    commitment = _commitment(legs=(), total_qty_kg=500.0)

    assert build_next_inventory(commitment) == []


def test_build_quantity_is_decimal_without_binary_drift():
    """`Decimal(float)` 이면 0.1 이 이진 오차를 달고 들어온다."""
    commitment = _commitment(
        legs=(
            ArrivalLeg(
                item="배추",
                qty_kg=0.1,
                arrival_date=date(2026, 1, 2),
                purchase_date=AS_OF,
                seq=1,
            ),
        ),
        total_qty_kg=0.1,
    )

    quantity = build_next_inventory(commitment)[0].quantity_kg

    assert isinstance(quantity, Decimal)
    assert quantity == Decimal("0.1")
    assert str(quantity) == "0.1"


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"마스터 ID 함수를 부르지 않는다"* 고 **설명하는 문장**이
       호출로 잡힌다. 설명과 실행문은 다른 것이고, 잠가야 할 것은 후자다.

    ★ **문자열 리터럴은 남긴다.** `f"PUR-{…}"` 로 ID 를 조립하는 것이 바로 잡으려는
      위반이라, 문자열까지 걷어내면 검사가 아무것도 안 잰다.
    """
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                코드 = 코드.replace(docstring, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


# ── build_next_inventory · 매입 참조 (아직 안 켜진 계약) ────────────────
#
# 🟡 **마스터는 아직 `purchase_ids` 를 안 넘긴다.** 그 규약은 마스터 소유 파일에
#    있어 물류가 고칠 자리가 아니다 — 여기서 재는 것은 **물류 쪽 준비가 끝났는가**와
#    **현행 마스터가 그대로 도는가** 둘이다.

#: 마스터가 계약을 켜는 날 넘겨 줄 매핑의 모양. 🔴 **물류가 만드는 값이 아니라서**
#: 여기서도 `purchase_id_for()` 를 부르지 않고 마스터가 준 모양 그대로 적는다 —
#: 테스트가 그 함수를 부르면 물류가 그래도 된다는 신호를 남기게 된다.
매입참조 = {1: "PUR-REQ-1-D1-S1", 2: "PUR-REQ-1-D1-S2"}


def test_build_without_purchase_ids_keeps_working_for_the_current_master():
    """🔴 **현행 마스터 호출이 그대로 돈다.** 인자를 필수로 만들면 여기서 터진다.

    `apply_approval` 은 아직 `logistics.build(commitment, target_state_date=…)` 로
    부른다 — 물류가 남의 규약을 혼자 강제할 수 없다.
    """
    rows = build_next_inventory(_두회차())

    assert len(rows) == 2
    assert [row.purchase_id for row in rows] == [None, None]
    assert [row.inbound_id for row in rows] == ["INB-H1-REQ-1-1-1", "INB-H1-REQ-1-1-2"]


def test_build_with_purchase_ids_carries_the_master_owned_value_per_leg():
    """★ 계약이 켜지면 회차마다 **그 회차의** 참조가 실린다."""
    rows = build_next_inventory(_두회차(), purchase_ids=매입참조)

    assert [row.purchase_id for row in rows] == ["PUR-REQ-1-D1-S1", "PUR-REQ-1-D1-S2"]
    assert all(row.purchase_id == 매입참조[seq] for seq, row in zip((1, 2), rows, strict=True))


def test_build_does_not_reconstruct_the_purchase_id_itself():
    """🔴 **받은 값을 그대로 쓴다 — 모양을 보고 짐작하지 않는다.**

    마스터 형식(`PUR-…`)과 전혀 다른 문자열을 넘겨도 그대로 실려야 한다. 여기서
    `approval_id` 를 뜯어 재조립하거나 `purchase_id_for()` 를 부르면 이 검사가 깨진다.
    """
    rows = build_next_inventory(_한회차(), purchase_ids={1: "완전히-다른-문자열"})

    assert rows[0].purchase_id == "완전히-다른-문자열"


def test_build_stops_when_a_supplied_mapping_misses_this_leg():
    """🔴 **계약을 받고서 회차가 빠진 것은 무결성 문제다.**

    ★ `purchase_ids=None`(계약이 안 켜졌다)과 **다른 사실**이다. 여기서 `None` 을
      넣고 넘어가면 둘이 같은 값으로 뭉개져, 나중에 도착 처리가 구별하지 못한다.
    """
    with pytest.raises(transition.PurchaseReferenceMissing) as 오류:
        build_next_inventory(_두회차(), purchase_ids={1: "PUR-REQ-1-D1-S1"})

    assert "seq=2" in str(오류.value)


def test_build_does_not_borrow_another_legs_purchase_id():
    """🔴 **매핑에 값이 하나뿐이어도 그것을 집지 않는다.**

    `next(iter(purchase_ids.values()))` 같은 대체가 들어가면 이 물건이 **남의 매입
    줄**에 달리고, 도착 뒤 그 줄에서 등급·단가를 읽어 **틀린 원가의 로트**로 굳는다.
    """
    seq2만_있는_약정 = _commitment(
        legs=(
            ArrivalLeg(
                item="배추",
                qty_kg=200.0,
                arrival_date=date(2026, 1, 5),
                purchase_date=AS_OF,
                seq=2,
            ),
        ),
        total_qty_kg=200.0,
    )

    with pytest.raises(transition.PurchaseReferenceMissing):
        build_next_inventory(seq2만_있는_약정, purchase_ids={1: "PUR-REQ-1-D1-S1"})


def test_build_rejects_an_empty_purchase_reference():
    """★ 빈 문자열도 참조가 아니다 — 있는 척하는 값을 통과시키지 않는다."""
    with pytest.raises(transition.PurchaseReferenceMissing):
        build_next_inventory(_한회차(), purchase_ids={1: ""})


def test_build_with_an_empty_mapping_and_no_legs_is_fine():
    """★ 회차가 없으면 대조할 것도 없다 — 빈 매핑은 예외가 아니다."""
    assert build_next_inventory(_commitment(legs=(), total_qty_kg=500.0), purchase_ids={}) == []


def test_build_keeps_inbound_id_and_purchase_id_as_separate_identities():
    """🔴 **`purchase_id` 가 `inbound_id` 를 대신하지 않는다.**

    `inbound_id` 는 *"물류가 셈하는 입고 건"*, `purchase_id` 는 *"매입 원장의 어느
    행에서 왔나"* 다. B-1 대조의 열쇠는 여전히 `inbound_id` 다.
    """
    row = build_next_inventory(_한회차(), purchase_ids=매입참조)[0]

    assert row.inbound_id == "INB-H1-REQ-1-1-1"
    assert row.purchase_id == "PUR-REQ-1-D1-S1"
    assert row.inbound_id != row.purchase_id


def test_build_does_not_call_the_master_id_factory():
    """🔴 **원문을 읽어 잠근다.** 물류가 마스터 ID 규칙을 복사하면 같은 사실의 주인이
    둘이 되고, 마스터가 형식을 바꾸는 날 두 곳이 어긋난 채로 조용히 돈다.

    ⚠️ 주석·docstring 은 걷어내고 본다 — *"부르지 않는다"* 고 **설명하는 문장**이
       호출로 잡히면 안 된다.
    """
    코드 = _코드만(Path(transition.__file__).read_text(encoding="utf-8"))

    assert "purchase_id_for" not in 코드, "물류가 마스터 ID 함수를 부르고 있다"
    assert "PUR-" not in 코드, "물류가 매입 ID 문자열을 조립하고 있다"


# ── persist_inventory ───────────────────────────────────────────────────


def test_persist_does_not_commit():
    """🔴 커밋은 재무 write 와 함께 마스터가 한 번 한다."""
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_persist_does_not_open_its_own_connection():
    """원문에 `get_connection` 이 없다 — 마스터가 쥔 트랜잭션 밖에서 쓰면 안 된다."""
    source = Path(transition.__file__).read_text(encoding="utf-8")

    assert "get_connection" not in source


def test_persist_raises_when_the_fixture_row_is_missing():
    """행이 없으면 만들지 않고 예외를 낸다. 메시지에 무엇이 없는지 적힌다."""
    conn = 가짜커넥션(rowcount=0)

    with pytest.raises(LogisticsFixtureMissing) as excinfo:
        persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    message = str(excinfo.value)
    assert SIM_RUN_ID in message
    assert str(AS_OF) in message


@pytest.mark.parametrize(
    ("legs가_있나", "기대_status"),
    [(False, "CONFIRMED_ZERO"), (True, "CONFIRMED")],
)
def test_persist_status_follows_whether_rows_exist(legs가_있나, 기대_status):
    """비면 `CONFIRMED_ZERO`, 있으면 `CONFIRMED` 다."""
    conn = 가짜커넥션()
    rows = build_next_inventory(_두회차()) if legs가_있나 else []

    persist_inventory(conn, **_fixture_인자(rows))

    assert 기대_status in _update_params(conn)


def test_persist_update_does_not_touch_other_status_columns():
    """⑤ `confirmed_outbound_*` · `evidence_grade` · `approved_by` 는 남의 칸이다.

    ⚠️ `confirmed_inbound_*` 는 **2026-09-04 부터 이 UPDATE 가 겸한다** (임시). 덮지
       않고 병합하는지는 아래 `test_persist_merges_...` 가 잰다 — 여기서는 그 둘
       말고는 아무 칸도 늘지 않았음을 잠근다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    statement = str(conn.커서.queries[-1])
    assert "confirmed_outbound_status" not in statement
    assert "confirmed_outbound_json" not in statement
    assert "evidence_grade" not in statement
    assert "approved_by" not in statement
    assert "lot_priority" not in statement
    assert "zone_capacity" not in statement


# ── persist_inventory · confirmed_inbound 병합 (임시 조치) ───────────────
#
# ⚠️ **이 절 전체가 임시다.** 승인과 발주 확정은 다른 사실인데 지금은 발주 확정
#    단계에 코드가 없어 승인을 그것으로 대신 본다. 물류가 그 단계를 만들면 병합도
#    이 검사들도 함께 걷어낸다 (`transition.py` 모듈 docstring 참조).
#
# 🔴 걷어내기 전까지 지켜야 하는 것은 하나다 — **덮지 않고 더한다.**


def test_persist_merges_into_the_existing_confirmed_inbound_instead_of_overwriting():
    """① 기존 확정 입고가 남고 이번 승인분이 **더해진다.**

    🔴 덮어쓰면 그날 이미 확정돼 있던 남의 입고가 에러 없이 사라진다. 사라진 뒤에는
       처음부터 없었던 것과 구별되지 않는다.
    """
    conn = 가짜커넥션(confirmed_inbound=[남의_확정입고])

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    written = _written_confirmed_inbound(conn)
    assert written is not None
    assert written[0] == 남의_확정입고, "남의 확정 입고를 그대로 둔다 (모양도 안 바꾼다)"
    assert [row["inbound_id"] for row in written] == [
        "INB-OTHER-9",
        "INB-H1-REQ-1-1-1",
        "INB-H1-REQ-1-1-2",
    ]
    assert _written_confirmed_status(conn) == "CONFIRMED"


def test_persist_is_idempotent_for_the_same_approval():
    """② 같은 승인을 두 번 반영해도 목록이 안 부푼다.

    ★ 첫 반영이 쓴 목록을 그대로 두 번째 반영의 기존 목록으로 물린다 — 실제로 같은
      날 같은 승인이 두 번 흐를 때 일어나는 일이다.

    ⚠️ 중복이 생기면 B-1 이 `CONFIRMED_INBOUND_ID_DUPLICATED` 로 잡지만, **여기서 안
       만드는 것이 먼저다.**
    """
    rows = build_next_inventory(_두회차())
    첫번 = 가짜커넥션()
    persist_inventory(첫번, **_fixture_인자(rows))

    두번 = 가짜커넥션(confirmed_inbound=_written_confirmed_inbound(첫번))
    persist_inventory(두번, **_fixture_인자(rows))

    assert _written_confirmed_inbound(두번) == _written_confirmed_inbound(첫번)


def test_persist_writes_values_that_pass_the_b1_gap_rule(complete_logistics_snapshot):
    """③ 🔴 **이 PR 의 핵심이다.** 쓴 두 칸이 B-1 을 실제로 통과한다.

    ★ **값 비교를 재구현하지 않는다.** `find_in_transit_schedule_gap` 을 그대로 불러
      `None` 이 나오는지 본다 — 재구현하면 물류가 규칙을 바꾼 날 검사만 통과한다.

    ★ 두 payload 를 **JSON 을 지나 스키마로 되돌려** 넘긴다. `quantity_kg` 가 `!=` 로
      비교되므로 직렬화·역직렬화 왕복 뒤에도 같은 `Decimal` 이어야 한다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "in_transit": [InTransitItem.model_validate(row) for row in _written_in_transit(conn)],
            "confirmed_inbound_schedule": [
                ScheduledQuantity.model_validate(row)
                for row in _written_confirmed_inbound(conn) or []
            ],
        }
    )

    assert find_in_transit_schedule_gap(snapshot) is None


def test_persist_quantity_survives_the_json_round_trip(complete_logistics_snapshot):
    """③-보강: 소수 수량이 왕복 뒤에도 같은 `Decimal` 로 돌아온다.

    🔴 `Decimal` 을 문자열로 뭉개거나 `float` 을 거치면 값은 같아 보이는데 B-1 이
       `IN_TRANSIT_CONFIRMED_SCHEDULE_MISMATCH` 를 낸다.
    """
    commitment = _commitment(
        legs=(
            ArrivalLeg(
                item="배추",
                qty_kg=0.1,
                arrival_date=date(2026, 1, 2),
                purchase_date=AS_OF,
                seq=1,
            ),
        ),
        total_qty_kg=0.1,
    )
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(commitment)))

    확정 = ScheduledQuantity.model_validate((_written_confirmed_inbound(conn) or [])[0])
    전이 = InTransitItem.model_validate(_written_in_transit(conn)[0])
    assert 확정.quantity_kg == Decimal("0.1")
    assert 확정.quantity_kg == 전이.quantity_kg
    assert 확정.date == 전이.expected_arrival_date == date(2026, 1, 2)
    assert 확정.item == 전이.item


def test_persist_empty_approval_keeps_the_existing_confirmed_inbound():
    """④ 빈 승인은 `confirmed_inbound` 를 **비우지 않는다.**

    ★ *"더할 것이 없다"* 와 *"기존 것을 지워라"* 는 다르다. 두 칸 모두 기존 목록을
      지키고, 더할 것이 없으면 그대로 둔다 — 여기 기존 `in_transit` 이 비어 있어
      결과가 `CONFIRMED_ZERO` 인 것이지 승인이 덮은 것이 아니다
      (기존 행이 남는 경우는 `test_G_승인분이_없으면_기존_운송중을_지키다`).
    """
    conn = 가짜커넥션(confirmed_inbound=[남의_확정입고])

    persist_inventory(conn, **_fixture_인자([]))

    assert _written_in_transit(conn) == [], "in_transit 은 승인분이 없다고 적는다"
    assert "CONFIRMED_ZERO" in _update_params(conn)
    assert _written_confirmed_inbound(conn) == [남의_확정입고]
    assert _written_confirmed_status(conn) == "CONFIRMED"


def test_persist_empty_approval_does_not_turn_unresolved_into_confirmed_zero():
    """④-보강: 더할 것이 없는데 기존이 `None` 이면 `None` 그대로 둔다.

    🔴 `[]` 로 적으면 *"확인했고 0 건"* 이라는, **우리가 하지 않은 확인**이 장부에
       남는다. 0 과 null 은 다르다.
    """
    conn = 가짜커넥션(confirmed_inbound=None)

    persist_inventory(conn, **_fixture_인자([]))

    assert _written_confirmed_inbound(conn) is None
    assert _written_confirmed_status(conn) == "UNRESOLVED"


def test_persist_reads_before_writing_on_the_given_connection():
    """★ 병합은 **읽기 없이는 못 한다.** 그 읽기가 같은 커넥션·같은 커서다.

    🔴 자기 커넥션을 새로 열면 마스터가 쥔 트랜잭션 밖에서 읽게 되어, 같은 커밋 안의
       앞선 write 를 못 보고 그것을 덮는다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    읽기, 쓰기 = (str(q) for q in conn.커서.queries)
    # ★ **두 칸을 함께 읽는다.** 둘 다 병합 대상이 됐다 (2026-09-05) — 한 칸만 읽으면
    #   나머지 한 칸은 병합할 기존값을 모른 채 덮게 된다.
    assert "SELECT in_transit_json, confirmed_inbound_json" in 읽기
    assert not _is_write(읽기)
    assert _is_write(쓰기) and "SELECT" not in 쓰기
    # ★ 읽은 행과 쓴 행이 갈리면 남의 목록에 이번 승인분을 얹는다.
    assert conn.커서.params[0] == (SIM_RUN_ID, AS_OF, transition.USAGE_SCOPE)
    assert _update_params(conn)[-3:] == (SIM_RUN_ID, AS_OF, transition.USAGE_SCOPE)


def test_persist_locks_the_fixture_row_before_merging():
    """🔴 **A: 잠금 없이 병합하면 마지막 쓴 쪽이 이긴다 (lost update).**

    ```text
    초기            in_transit = [A]
    T1 승인 B        SELECT → [A]   병합 → [A, B]
    T2 승인 C        SELECT → [A]   병합 → [A, C]   ← 같은 옛 목록을 읽었다
    T1 UPDATE·COMMIT               [A, B]
    T2 UPDATE·COMMIT               [A, C]           🔴 승인 B 가 사라진다
    ```

    ★ 3-B1 의 병합만으로는 못 막는다 — 병합은 *"한 트랜잭션이 본 목록"* 위에서만
      정확하고, 둘이 같은 옛 목록을 보는 것 자체를 막지 못한다. B-1 도 못 잡는다:
      사라진 쪽이 두 칸에서 **함께** 빠져 대조가 성립한다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    읽기, 쓰기 = (str(q) for q in conn.커서.queries)
    assert "FOR UPDATE" in 읽기, "병합 전에 그 fixture 행을 잠근다"
    assert "FOR UPDATE" not in 쓰기
    # ★ 잠근 행과 쓰는 행이 같아야 한다 — 갈리면 잠금이 아무것도 안 지킨다.
    assert conn.커서.params[0] == _update_params(conn)[-3:]


def test_persist_does_not_create_an_advisory_lock():
    """★ 바꾸는 것이 **이미 알고 있는 행 하나**라 행 잠금으로 충분하다.

    `ledger.py` 가 전역 advisory lock 을 쓰는 이유는 거기가 여러 행·여러 표를
    오가기 때문이라 사정이 다르다.
    """
    source = Path(transition.__file__).read_text(encoding="utf-8")

    assert "pg_advisory" not in source


def test_persist_does_not_send_an_update_when_the_row_is_missing():
    """⑥-보강 / E: 읽을 행이 없으면 **UPDATE 를 보내기 전에** 멈춘다."""
    conn = 가짜커넥션(rowcount=0)

    with pytest.raises(LogisticsFixtureMissing):
        persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    assert len(conn.커서.queries) == 1
    assert not _is_write(str(conn.커서.queries[0]))


def test_transition_module_does_not_write_inventory_lots():
    """🔴 승인 시점의 입고 예정은 `inventory_lots` 에 넣을 수 없다 (네 가지 장벽).

    ★ 원문을 읽어 검사한다 — SQL 문자열은 실행되기 전까지 흔적이 없다.

    ★ **모듈 docstring 은 떼고 본다.** 그 docstring 이 *왜* 그 표를 못 쓰는지를 네
      가지 장벽과 함께 적고 있어 표 이름이 거기 나온다 — 설명하는 문장과 그 표에
      쓰는 코드는 다른 것이고, 잠가야 할 것은 후자다.
    """
    source = Path(transition.__file__).read_text(encoding="utf-8")
    module_docstring = ast.get_docstring(ast.parse(source), clean=False)
    assert module_docstring is not None, "모듈 docstring 이 없다 — 왜 이 표인지가 안 적혀 있다."
    code = source.replace(module_docstring, "", 1)

    assert "inventory_lots" not in code


# ── LogisticsTransitionAdapter ──────────────────────────────────────────
#
# ★ 어댑터는 **얇다.** 여기서 재는 것은 계산도 SQL 도 아니고 *"인자가 제대로
#   옮겨지는가"* 하나다 — 그 자리가 틀리면 하루 어긋난 행에 조용히 쓰인다.


def _adapter() -> LogisticsTransitionAdapter:
    return LogisticsTransitionAdapter(sim_run_id=SIM_RUN_ID)


def test_adapter_build_makes_one_bundle_carrying_the_date_and_source_ref():
    """① `build` 는 회차 낱개가 아니라 **묶음 하나**를 낸다.

    🔴 회차에는 `target_state_date` 가 없다 — 낱개로 내면 `persist` 가 어느 날 행에
       쓸지 모른다.
    """
    bundles = _adapter().build(_두회차(), target_state_date=TARGET_STATE_DATE)

    assert len(bundles) == 1, "승인 하나가 바꾸는 fixture 행은 하나다"
    bundle = bundles[0]
    assert isinstance(bundle, InventoryTransition)
    assert bundle.target_state_date == TARGET_STATE_DATE
    assert [row.inbound_id for row in bundle.items] == [
        "INB-H1-REQ-1-1-1",
        "INB-H1-REQ-1-1-2",
    ], "묶음 안의 회차는 build_next_inventory 가 낸 그대로다"


def test_adapter_source_ref_names_the_master_approval():
    """② `MASTER-APPROVAL:{approval_id}` 다 — 어느 승인이 이 행을 바꿨는지 남는다."""
    bundle = _adapter().build(_두회차(), target_state_date=TARGET_STATE_DATE)[0]

    assert bundle.source_ref == "MASTER-APPROVAL:H1-REQ-1-1"


def test_adapter_empty_commitment_still_writes_confirmed_zero():
    """③ 빈 약정도 **그날 행을 `CONFIRMED_ZERO` 로 적는다.**

    ★ *"쓸 것이 없다"* 와 *"어느 행인지 모른다"* 는 다른 사실이다. 묶음이 회차 낱개면
      빈 약정에서 시퀀스가 비어 `persist` 가 아무 일도 안 하고, 그러면 승인분이
      없다는 우리가 아는 사실이 장부에 안 남는다.
    """
    adapter = _adapter()
    conn = 가짜커넥션()

    bundles = adapter.build(
        _commitment(legs=(), total_qty_kg=500.0), target_state_date=TARGET_STATE_DATE
    )
    adapter.persist(conn, bundles)

    assert len(bundles) == 1 and bundles[0].items == ()
    assert "CONFIRMED_ZERO" in _update_params(conn)


def test_adapter_persist_passes_sim_run_id_and_the_target_state_date():
    """④ 🔴 **하루 어긋난 행에 쓰는 것을 잡는다.**

    `as_of` 로 넘어가는 값은 승인일(`commitment.as_of`)이 아니라 마스터가 준
    `target_state_date` 다. 재무가 같은 날짜로 `finance_states` 를 세우므로, 여기서
    하루 앞 행에 쓰면 두 장부가 다른 날에 앉는다 — 에러 없이 갈린다.
    """
    adapter = _adapter()
    conn = 가짜커넥션()

    adapter.persist(conn, adapter.build(_두회차(), target_state_date=TARGET_STATE_DATE))

    params = _update_params(conn)
    assert SIM_RUN_ID in params, "sim_run_id 를 그대로 넘겨야 WHERE 가 그 행을 찾는다"
    assert TARGET_STATE_DATE in params
    assert AS_OF not in params, "승인일 행에 썼다 — 재무 상태와 하루 어긋난다"


def test_adapter_persist_does_not_commit_or_rollback():
    """⑤ 커밋은 재무 write 와 함께 **마스터가 한 번** 한다."""
    adapter = _adapter()
    conn = 가짜커넥션()

    adapter.persist(conn, adapter.build(_두회차(), target_state_date=TARGET_STATE_DATE))

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_adapter_passes_the_missing_fixture_error_through():
    """★ 행이 없으면 어댑터가 삼키지 않는다 — 마스터가 `FAILED` 로 사유를 남긴다."""
    adapter = _adapter()
    conn = 가짜커넥션(rowcount=0)

    with pytest.raises(LogisticsFixtureMissing):
        adapter.persist(conn, adapter.build(_두회차(), target_state_date=TARGET_STATE_DATE))


# ── persist_inventory · in_transit 누적 (3-B1) ──────────────────────────
#
# 🔴 **종전에는 `in_transit` 을 덮어썼다.** 같은 fixture 행을 겨냥한 승인이 둘이면
#    뒤엣것이 앞엣것을 **에러 없이 지웠다.**
#
#    ```text
#    승인 A   in_transit=[A]  confirmed=[A]
#    승인 B   in_transit=[B]  confirmed=[A, B]   ← A 의 운송 중 물량이 사라진다
#    ```
#
#    B-1 은 *"in_transit 의 행마다 confirmed 에 짝이 있나"* 를 보므로 이 손실을
#    **못 잡는다** — 없어진 쪽이 in_transit 이라 검사할 대상 자체가 사라진다.
#    그래서 읽는 쪽(B-1)이 아니라 **쓰는 쪽**을 고쳤다.


#: 앞선 승인이 이미 반영해 둔 운송 중 한 건. **이번 승인과 무관한 남의 사실이다.**
남의_운송중 = {
    "inbound_id": "INB-OTHER-9",
    "item": "무",
    "quantity_kg": "120.5",
    "expected_arrival_date": "2026-01-04",
}


def _한회차(*, approval_id: str = "H1-REQ-1-1", qty_kg: float = 300.0) -> Any:
    return _commitment(
        legs=(
            ArrivalLeg(
                item="배추",
                qty_kg=qty_kg,
                arrival_date=date(2026, 1, 2),
                purchase_date=AS_OF,
                seq=1,
            ),
        ),
        total_qty_kg=qty_kg,
        approval_id=approval_id,
    )


def test_A_기존이_None_이고_승인분이_없으면_None_을_지킨다():
    """★ *"아직 확인한 적 없다"* 를 *"확인했고 0 건"* 으로 바꾸지 않는다."""
    conn = 가짜커넥션(in_transit=None, confirmed_inbound=None)

    persist_inventory(conn, **_fixture_인자([]))

    assert _written_in_transit(conn) is None
    assert _written_in_transit_status(conn) == "UNRESOLVED"


def test_B_기존이_빈목록이고_승인분이_없으면_CONFIRMED_ZERO_다():
    conn = 가짜커넥션(in_transit=[])

    persist_inventory(conn, **_fixture_인자([]))

    assert _written_in_transit(conn) == []
    assert _written_in_transit_status(conn) == "CONFIRMED_ZERO"


def test_C_기존이_빈목록이고_승인분이_있으면_CONFIRMED_다():
    conn = 가짜커넥션(in_transit=[])
    rows = build_next_inventory(_한회차())

    persist_inventory(conn, **_fixture_인자(rows))

    assert len(_written_in_transit(conn)) == 1
    assert _written_in_transit_status(conn) == "CONFIRMED"


def test_D_다른_승인은_기존_운송중에_더해진다():
    """🔴 이 단계가 고치는 자리다. 종전에는 `[B]` 만 남았다."""
    conn = 가짜커넥션(in_transit=[남의_운송중])

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차())))

    적힌것 = _written_in_transit(conn)
    assert len(적힌것) == 2, "앞선 승인분이 사라지면 안 된다"
    assert 남의_운송중 in 적힌것
    assert {row["inbound_id"] for row in 적힌것} == {"INB-OTHER-9", "INB-H1-REQ-1-1-1"}


def test_E_같은_승인을_두_번_반영해도_목록이_안_부푼다():
    """멱등 재반영 — 같은 `inbound_id` · 같은 사실이면 더하지 않는다."""
    rows = build_next_inventory(_한회차())
    첫번 = 가짜커넥션(in_transit=[])
    persist_inventory(첫번, **_fixture_인자(rows))

    두번 = 가짜커넥션(in_transit=_written_in_transit(첫번))
    persist_inventory(두번, **_fixture_인자(rows))

    assert _written_in_transit(두번) == _written_in_transit(첫번)


def test_E2_직렬화_자릿수가_달라도_같은_수량이면_멱등이다():
    """🔴 문자열로 비교하면 `"300.0"` 과 `"300.00"` 이 갈려 정상 재반영이 터진다."""
    rows = build_next_inventory(_한회차())
    자릿수만_다른_기존 = [
        {**rows[0].model_dump(mode="json"), "quantity_kg": "300.00"},
    ]
    conn = 가짜커넥션(in_transit=자릿수만_다른_기존)

    persist_inventory(conn, **_fixture_인자(rows))

    assert _written_in_transit(conn) == 자릿수만_다른_기존, "더하지도 갈아 끼우지도 않는다"


def test_F_같은_id_에_다른_수량이면_멈춘다():
    """★ 어느 쪽이 진짜인지 여기서 고르지 않는다 — 덮지도 버리지도 않는다."""
    기존 = build_next_inventory(_한회차(qty_kg=300.0))[0].model_dump(mode="json")
    conn = 가짜커넥션(in_transit=[기존])

    with pytest.raises(transition.InboundScheduleConflict) as 오류:
        persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차(qty_kg=999.0))))

    assert "INB-H1-REQ-1-1-1" in str(오류.value)
    assert len(conn.커서.queries) == 1, "읽기만 하고 UPDATE 를 보내지 않는다"


def test_G_승인분이_없으면_기존_운송중을_지키다():
    conn = 가짜커넥션(in_transit=[남의_운송중])

    persist_inventory(conn, **_fixture_인자([]))

    assert _written_in_transit(conn) == [남의_운송중]
    assert _written_in_transit_status(conn) == "CONFIRMED"


def test_H_기존에_중복된_id_가_있으면_멈춘다():
    """🔴 깨진 목록 위에 병합하지 않는다."""
    conn = 가짜커넥션(in_transit=[남의_운송중, dict(남의_운송중)])

    with pytest.raises(transition.InboundScheduleConflict) as 오류:
        persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차())))

    assert "INB-OTHER-9" in str(오류.value)
    assert len(conn.커서.queries) == 1


def test_I_승인분_안에_충돌하는_중복_id_가_있으면_멈춘다():
    conn = 가짜커넥션(in_transit=[])
    같은_id_다른_수량 = [
        InTransitItem(
            inbound_id="INB-DUP-1",
            item="배추",
            quantity_kg=Decimal(10),
            expected_arrival_date=date(2026, 1, 2),
        ),
        InTransitItem(
            inbound_id="INB-DUP-1",
            item="배추",
            quantity_kg=Decimal(20),
            expected_arrival_date=date(2026, 1, 2),
        ),
    ]

    with pytest.raises(transition.InboundScheduleConflict):
        persist_inventory(conn, **_fixture_인자(같은_id_다른_수량))


def test_I2_승인분_안의_동일한_중복은_한_행만_남는다():
    conn = 가짜커넥션(in_transit=[])
    똑같은_두_행 = build_next_inventory(_한회차()) * 2

    persist_inventory(conn, **_fixture_인자(똑같은_두_행))

    assert len(_written_in_transit(conn)) == 1


def test_J_누적_뒤에도_B1_이_선다():
    """★ 두 칸이 같은 규칙으로 자라므로 in_transit 의 행마다 confirmed 에 짝이 있다."""
    conn = 가짜커넥션(
        in_transit=[남의_운송중],
        confirmed_inbound=[남의_확정입고],
    )

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차())))

    스냅샷 = InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=AS_OF,
        on_hand_by_lot=[],
        in_transit=[InTransitItem.model_validate(row) for row in _written_in_transit(conn)],
        confirmed_inbound_schedule=[
            ScheduledQuantity.model_validate(row) for row in _written_confirmed_inbound(conn)
        ],
        confirmed_outbound_schedule=[],
        outbound_commitments=[],
        used_capacity_kg=Decimal(0),
        guaranteed_capacity_by_zone_kg=None,
        evidence_refs=[],
    )

    assert find_in_transit_schedule_gap(스냅샷) is None
    assert len(_written_in_transit(conn)) == 2
    assert len(_written_confirmed_inbound(conn)) == 2


# ── persist_inventory · 매입 참조의 멱등과 충돌 ────────────────────────
#
# ★ 여기서 재는 것은 **참조가 실린 뒤에도 3-B1 의 병합 규율이 그대로인가**다.
#   `_merge_schedule()` 을 약하게 만들지 않았는지 확인하는 자리다.


def test_K_같은_매입_참조로_재반영하면_목록이_안_부푼다():
    """★ `purchase_id` 가 실려도 **멱등 재반영은 그대로다.**"""
    rows = build_next_inventory(_한회차(), purchase_ids=매입참조)
    첫번 = 가짜커넥션(in_transit=[])
    persist_inventory(첫번, **_fixture_인자(rows))

    두번 = 가짜커넥션(in_transit=_written_in_transit(첫번))
    persist_inventory(두번, **_fixture_인자(rows))

    적힌것 = _written_in_transit(두번)
    assert 적힌것 == _written_in_transit(첫번)
    assert len(적힌것) == 1
    assert 적힌것[0]["purchase_id"] == "PUR-REQ-1-D1-S1"


def test_L_같은_inbound_id_에_다른_매입_참조면_멈춘다():
    """🔴 **`purchase_id` 만 대조에서 빼는 예외를 두지 않는다.**

    같은 `inbound_id` 인데 매입 출처가 다르면 같은 건이 아니다. 조용히 한쪽을
    남기면 도착 뒤 **틀린 매입 줄에서 등급·단가를 읽는다.**

    ★ UPDATE 전에 오른다 — 마스터가 승인 전이 전체를 롤백할 수 있다.
    """
    기존 = build_next_inventory(_한회차(), purchase_ids={1: "PUR-REQ-1-D1-S1"})[0]
    conn = 가짜커넥션(in_transit=[기존.model_dump(mode="json")])
    다른_매입 = build_next_inventory(_한회차(), purchase_ids={1: "PUR-OTHER-D1-S1"})

    with pytest.raises(transition.InboundScheduleConflict) as 오류:
        persist_inventory(conn, **_fixture_인자(다른_매입))

    assert "INB-H1-REQ-1-1-1" in str(오류.value)
    assert len(conn.커서.queries) == 1, "읽기만 하고 UPDATE 를 보내지 않는다"


def test_M_참조_없던_행에_참조가_붙는_것도_다른_사실이다():
    """⚠️ 계약이 켜지는 날 한 번은 여기서 부딪힌다. **그것이 맞다.**

    ★ 조용한 되메우기(backfill)를 이 자리에서 하지 않는다 — 그것은 별도 결정이고,
      여기서 하면 *"언제 무엇이 채워졌나"* 가 아무 데도 안 남는다.
    """
    참조없는_옛_행 = build_next_inventory(_한회차())[0].model_dump(mode="json")
    conn = 가짜커넥션(in_transit=[참조없는_옛_행])

    assert 참조없는_옛_행["purchase_id"] is None
    with pytest.raises(transition.InboundScheduleConflict):
        persist_inventory(
            conn, **_fixture_인자(build_next_inventory(_한회차(), purchase_ids=매입참조))
        )


def test_N_confirmed_inbound_모양은_그대로다():
    """🔴 **일정·수량 사실에는 출처를 얹지 않는다.**

    `ScheduledQuantity` 는 outbound 등 다른 일정에도 재사용된다. 어느 매입에서
    왔는지는 **운송 중인 물건의 속성**이지 일정의 속성이 아니다.
    """
    conn = 가짜커넥션(in_transit=[], confirmed_inbound=[])

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

    적힌_일정 = _written_confirmed_inbound(conn)
    assert 적힌_일정, "확정 일정이 비면 이 검사가 아무것도 안 잰다"
    assert all(set(row) == {"date", "quantity_kg", "item", "inbound_id"} for row in 적힌_일정)
    assert all("purchase_id" in row for row in _written_in_transit(conn)), (
        "운송 중 쪽에는 반대로 반드시 있어야 한다"
    )


def test_O_매입_참조가_실려도_B1_이_그대로_선다():
    """★ B-1 이 대조하는 값은 여전히 넷이다 — 한쪽에만 필드가 늘어도 짝이 맞는다.

    ★ 참조가 **있는 행**과 **없는 옛 행**이 한 목록에 섞여도 성립한다.
    """
    conn = 가짜커넥션(in_transit=[남의_운송중], confirmed_inbound=[남의_확정입고])

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차(), purchase_ids=매입참조)))

    스냅샷 = InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=AS_OF,
        on_hand_by_lot=[],
        in_transit=[InTransitItem.model_validate(row) for row in _written_in_transit(conn)],
        confirmed_inbound_schedule=[
            ScheduledQuantity.model_validate(row) for row in _written_confirmed_inbound(conn)
        ],
        confirmed_outbound_schedule=[],
        outbound_commitments=[],
        used_capacity_kg=Decimal(0),
        guaranteed_capacity_by_zone_kg=None,
        evidence_refs=[],
    )

    assert find_in_transit_schedule_gap(스냅샷) is None
    참조 = {row["inbound_id"]: row.get("purchase_id") for row in _written_in_transit(conn)}
    assert 참조 == {"INB-OTHER-9": None, "INB-H1-REQ-1-1-1": "PUR-REQ-1-D1-S1"}


# ── LogisticsTransitionAdapter · 매입 참조 통과 ────────────────────────


def test_adapter_build_works_without_purchase_ids():
    """🔴 **현행 마스터 호출이 그대로 돈다.** 기본값이 없으면 여기서 `TypeError` 다."""
    bundle = _adapter().build(_두회차(), target_state_date=TARGET_STATE_DATE)[0]

    assert [row.purchase_id for row in bundle.items] == [None, None]


def test_adapter_build_forwards_purchase_ids_untouched():
    """★ 어댑터에는 업무가 없다 — 받은 매핑을 그대로 흘려보낸다."""
    bundle = _adapter().build(
        _두회차(), target_state_date=TARGET_STATE_DATE, purchase_ids=매입참조
    )[0]

    assert [row.purchase_id for row in bundle.items] == [
        "PUR-REQ-1-D1-S1",
        "PUR-REQ-1-D1-S2",
    ]


def test_adapter_build_passes_the_missing_reference_error_through():
    """★ 어댑터가 삼키지 않는다 — 마스터가 `FAILED` 로 사유를 남긴다."""
    with pytest.raises(transition.PurchaseReferenceMissing):
        _adapter().build(
            _두회차(), target_state_date=TARGET_STATE_DATE, purchase_ids={1: "PUR-REQ-1-D1-S1"}
        )
