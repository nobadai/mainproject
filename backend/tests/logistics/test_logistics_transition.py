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
from app.logistics.schemas import InTransitItem, ScheduledQuantity
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

    def __init__(self, rowcount: int, confirmed_inbound: object) -> None:
        self.rowcount = rowcount
        self.queries: list[object] = []
        self.params: list[object] = []
        self._행 = None if rowcount == 0 else (confirmed_inbound,)

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

    def __init__(self, rowcount: int = 1, confirmed_inbound: object = _기본) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.커서 = 가짜커서(rowcount, [] if confirmed_inbound is _기본 else confirmed_inbound)

    def cursor(self) -> 가짜커서:
        return self.커서

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _update_params(conn: 가짜커넥션) -> tuple[Any, ...]:
    """UPDATE 로 넘어간 파라미터. **SELECT 가 앞에 하나 더 있다.**"""
    assert len(conn.커서.params) == 2, "읽기 한 번 · 쓰기 한 번이다"
    params = conn.커서.params[-1]
    assert isinstance(params, tuple)
    return params


def _written_in_transit(conn: 가짜커넥션) -> list[dict[str, Any]]:
    return _update_params(conn)[0].obj


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

    ★ *"더할 것이 없다"* 와 *"기존 것을 지워라"* 는 다르다. `in_transit` 은 승인이
      유일한 주인이라 `CONFIRMED_ZERO` 로 덮지만, `confirmed_inbound` 는 아니다.
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
    assert "SELECT confirmed_inbound_json" in 읽기
    assert "UPDATE" not in 읽기
    assert "UPDATE" in 쓰기 and "SELECT" not in 쓰기
    # ★ 읽은 행과 쓴 행이 갈리면 남의 목록에 이번 승인분을 얹는다.
    assert conn.커서.params[0] == (SIM_RUN_ID, AS_OF, transition.USAGE_SCOPE)
    assert _update_params(conn)[-3:] == (SIM_RUN_ID, AS_OF, transition.USAGE_SCOPE)


def test_persist_does_not_send_an_update_when_the_row_is_missing():
    """⑥-보강: 읽을 행이 없으면 **UPDATE 를 보내기 전에** 멈춘다."""
    conn = 가짜커넥션(rowcount=0)

    with pytest.raises(LogisticsFixtureMissing):
        persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    assert len(conn.커서.queries) == 1
    assert "UPDATE" not in str(conn.커서.queries[0])


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
