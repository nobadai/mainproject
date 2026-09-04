"""승인 약정 → `in_transit` 반영의 build·persist 검사.

★ **DB 를 부르지 않는다.** 가짜 커넥션·커서로 잰다. 여기서 재는 것은 값이 DB 에
  들어갔는지가 아니라 **물류가 소유한 규율 넷**이다.

  ```text
  회차 값을 그대로 옮기나           도착일을 다시 계산하지 않는다
  같은 승인이 같은 id 를 내나       두 번 반영해도 부풀지 않는다
  커밋·커넥션을 쥐지 않나           트랜잭션은 마스터 것이다
  없는 행을 지어내지 않나           evidence_grade 는 물류 판단이다
  ```
"""

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Self

import pytest

from app.logistics import transition
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


class 가짜커서:
    """실행된 SQL 과 파라미터를 기록한다. `rowcount` 는 밖에서 정한다."""

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.queries: list[object] = []
        self.params: list[object] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        self.queries.append(query)
        self.params.append(params)


class 가짜커넥션:
    """commit 이 **몇 번** 불렸나를 센다 — 0 이어야 한다."""

    def __init__(self, rowcount: int = 1) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.커서 = 가짜커서(rowcount)

    def cursor(self) -> 가짜커서:
        return self.커서

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


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

    assert 기대_status in conn.커서.params[0]


def test_persist_update_does_not_touch_other_status_columns():
    """`confirmed_inbound_*` · `confirmed_outbound_*` 는 다른 사실이고 다른 근거다."""
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차())))

    statement = str(conn.커서.queries[0])
    assert "confirmed_inbound_status" not in statement
    assert "confirmed_outbound_status" not in statement
    assert "evidence_grade" not in statement
    assert "approved_by" not in statement


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
    assert "CONFIRMED_ZERO" in conn.커서.params[0]


def test_adapter_persist_passes_sim_run_id_and_the_target_state_date():
    """④ 🔴 **하루 어긋난 행에 쓰는 것을 잡는다.**

    `as_of` 로 넘어가는 값은 승인일(`commitment.as_of`)이 아니라 마스터가 준
    `target_state_date` 다. 재무가 같은 날짜로 `finance_states` 를 세우므로, 여기서
    하루 앞 행에 쓰면 두 장부가 다른 날에 앉는다 — 에러 없이 갈린다.
    """
    adapter = _adapter()
    conn = 가짜커넥션()

    adapter.persist(conn, adapter.build(_두회차(), target_state_date=TARGET_STATE_DATE))

    params = conn.커서.params[0]
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
