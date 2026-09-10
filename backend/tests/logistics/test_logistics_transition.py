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
from app.logistics.inbound_schedules import (
    InboundScheduleView,
    ScheduleAlreadyCancelled,
    ScheduleConflict,
    ScheduleReferenceMissing,
)
from app.logistics.schemas import (
    InTransitItem,
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
    """실행된 SQL 과 파라미터를 기록한다. **질의마다 다른 표를 낸다.**

    🔴 **승인 전이가 만지는 표가 셋이 됐다 (W3-3).** 업무 사실의 정본이
       `logistics_runtime_fixture` 의 JSON 두 칸에서 `inbound_schedules` 로 옮겨
       가면서, 한 벌만 돌려주는 대역은 뒤엣것이 앞엣것의 행 모양을 받아 엉뚱한
       데서 죽는다.

    ```text
    logistics_runtime_fixture   그날 행을 잠그고(SELECT fixture_id … FOR UPDATE)
                                status 둘과 source_ref 를 세운다
    purchase_items              purchase_item_id 를 얻는다 (fetch_purchase_detail)
    inbound_schedules           업무 사실이 여기 적힌다 (record_schedule)
    ```

    ★ `rowcount=0` 은 그날 fixture 행이 없다는 뜻이라 읽기도 빈손이어야 한다.
    """

    def __init__(self, rowcount: int, 일정: list[dict[str, Any]], 매입줄: Any) -> None:
        self.rowcount = rowcount
        self.queries: list[object] = []
        self.params: list[object] = []
        self._fixture행 = None if rowcount == 0 else ("LOG-FIXTURE-1",)
        #: `inbound_schedules` 에 **이미 있는** 행 (앞선 승인이 적어 둔 것).
        self._일정 = {행["inbound_id"]: 행 for 행 in 일정}
        self._매입줄 = 매입줄
        self._out: list[Any] = []
        #: 이번 실행이 **실제로 INSERT 한** 일정.
        self.적힌_일정: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        text = str(query)
        self.queries.append(query)
        self.params.append(params)
        if "inbound_schedules" in text and "INSERT" in text:
            칸 = (
                "inbound_id",
                "sim_run_id",
                "purchase_item_id",
                "quantity_kg",
                "expected_arrival_date",
                "created_as_of",
                "source_ref",
                "note",
            )
            행 = dict(zip(칸, params, strict=True))
            행["cancelled_as_of"] = None
            self.적힌_일정.append(행)
            self._일정[행["inbound_id"]] = 행
            self._out = []
        elif "inbound_schedules" in text:
            찾은것 = self._일정.get(params[1])
            self._out = [찾은것] if 찾은것 is not None else []
        elif "purchase_items" in text:
            self._out = list(self._매입줄)
        else:
            self._out = [] if self._fixture행 is None else [self._fixture행]

    def fetchone(self) -> object:
        return self._out[0] if self._out else None

    def fetchall(self) -> list[Any]:
        return list(self._out)


#: 기본값과 **명시적 `None`** 을 가르는 표식. `None` 은 *"아직 확인한 적 없다"*
#: (`UNRESOLVED`) 라는 사실이라 기본값으로 뭉개면 안 된다.
_기본 = object()

#: 대역이 내는 매입 줄 한 행. 칸 순서가 `purchase_detail._DETAIL_COLUMNS` 와 짝이다.
매입줄_한행 = ("PI-REQ-1-D1-S1", "ITEM-BAECHU", "특", Decimal(500), Decimal(1200))


class 가짜커넥션:
    """commit 이 **몇 번** 불렸나를 센다 — 0 이어야 한다."""

    def __init__(
        self,
        rowcount: int = 1,
        일정: list[dict[str, Any]] | None = None,
        매입줄: Any = _기본,
    ) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.커서 = 가짜커서(
            rowcount,
            [] if 일정 is None else 일정,
            [매입줄_한행] if 매입줄 is _기본 else 매입줄,
        )

    def cursor(self) -> 가짜커서:
        return self.커서

    @property
    def 적힌_일정(self) -> list[dict[str, Any]]:
        return self.커서.적힌_일정

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _일정행(
    inbound_id: str,
    *,
    purchase_item_id: str = "PI-REQ-1-D1-S1",
    quantity_kg: str = "300",
    eta: date = date(2026, 1, 2),
    created_as_of: date | None = None,
    cancelled_as_of: date | None = None,
) -> dict[str, Any]:
    """`inbound_schedules` 에 **이미 있는** 행 하나."""
    return {
        "inbound_id": inbound_id,
        "sim_run_id": SIM_RUN_ID,
        "purchase_item_id": purchase_item_id,
        "quantity_kg": Decimal(quantity_kg),
        "expected_arrival_date": eta,
        "created_as_of": AS_OF if created_as_of is None else created_as_of,
        "cancelled_as_of": cancelled_as_of,
        "source_ref": "APPROVAL:OTHER",
        "note": None,
    }


def _is_write(query: str) -> bool:
    """쓰기 질의인가.

    ⚠️ `"UPDATE" in query` 로 재면 안 된다 — fixture 행 **잠금**이
       `SELECT … FOR UPDATE` 라 읽기가 쓰기로 잡힌다. 잠금 문구를 걷어내고 본다.
    """
    return "INSERT" in query or "UPDATE" in query.replace("FOR UPDATE", "")


def _fixture_질의(conn: 가짜커넥션) -> list[str]:
    """fixture 행을 만진 질의만. **일정 표 질의가 그 뒤에 따라온다.**"""
    return [str(q) for q in conn.커서.queries if "logistics_runtime_fixture" in str(q)]


def _update_params(conn: 가짜커넥션) -> tuple[Any, ...]:
    """fixture 행 UPDATE 로 넘어간 파라미터.

    🔴 **`params[-1]` 로 집지 않는다 (W3-3).** UPDATE 뒤에 `inbound_schedules`
       질의가 더 붙어서, 마지막 것을 집으면 일정 INSERT 를 보게 된다.
    """
    쓰기 = [
        params
        for query, params in zip(conn.커서.queries, conn.커서.params, strict=True)
        if "logistics_runtime_fixture" in str(query) and _is_write(str(query))
    ]
    assert len(쓰기) == 1, "그날 행 UPDATE 는 한 번이다"
    params = 쓰기[0]
    assert isinstance(params, tuple)
    return params


def _written_in_transit_status(conn: 가짜커넥션) -> str:
    """🔴 **칸 순서가 바뀌었다 (W3-3).** JSON 두 칸이 빠져 status 가 맨 앞이다."""
    return _update_params(conn)[0]


def _written_confirmed_status(conn: 가짜커넥션) -> str:
    return _update_params(conn)[1]


def _적힌_일정(conn: 가짜커넥션) -> list[dict[str, Any]]:
    """이번 승인이 `inbound_schedules` 에 실제로 **새로 적은** 행.

    🔴 **업무 사실의 정본이 여기다 (W3-3).** 종전에는 fixture 행의 JSON 두 칸을
       파이썬에서 병합해 되돌려 적었고, 이 헬퍼가 그 병합 결과를 읽었다. 지금은
       날짜에 안 묶인 표에 한 행씩 적히고 **덮지도 병합하지도 않는다** —
       같은 사실이면 no-op, 다른 사실이면 `ScheduleConflict` 다.
    """
    return conn.적힌_일정


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


# ── build_next_inventory · 매입 참조 ────────────────────────────────────
#
# 🟢 **마스터가 넘긴다** (`#311` · 2026-09-06 · `master/transition.py` 의
#    `purchase_ids = {leg.seq: purchase_id_for(...)}`). 종전 이 자리에는
#    *"마스터는 아직 안 넘긴다"* 고 적혀 있었고, 그 전제로 아래 픽스처들이
#    `purchase_ids` 없이 승인분을 만들었다.
#
#    ⚠️ W3-2 가 `purchase_item_id` 를 필수로 만들면서 그 낡은 전제가 검사 20건을
#       세웠다 (마스터 실측 2026-09-10). **여기서 재는 것은 여전히 둘이다** —
#       기본값 없이도 현행 호출이 도는가, 그리고 받은 매핑을 그대로 흘리는가.

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
    원문 = Path(transition.__file__).read_text(encoding="utf-8")
    코드 = _코드만(원문)

    # 🔴 **호출 형태로 잰다 — 이름 언급이 아니다.** W3-2 가 `ScheduleReferenceMissing`
    #    의 오류 문구에 *"이 값은 마스터가 만든다 (… purchase_id_for)"* 를 적었는데,
    #    그것은 고칠 사람을 매입·마스터 쪽으로 보내는 안내지 규칙 복사가 아니다.
    #    문자열까지 금지하면 **도움말을 지워야 검사가 통과**하게 된다.
    assert "purchase_id_for(" not in 코드, "물류가 마스터 ID 함수를 부르고 있다"
    # ★ 이름을 들여오는 것도 막는다 — 위 괄호 검사만으로는 import 가 빠진다.
    가져온것 = {
        별칭.name
        for node in ast.walk(ast.parse(원문))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for 별칭 in node.names
    }
    assert "purchase_id_for" not in 가져온것, "물류가 마스터 ID 함수를 들여오고 있다"
    assert "PUR-" not in 코드, "물류가 매입 ID 문자열을 조립하고 있다"


# ── persist_inventory ───────────────────────────────────────────────────


def test_persist_does_not_commit():
    """🔴 커밋은 재무 write 와 함께 마스터가 한 번 한다."""
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

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
        persist_inventory(
            conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조))
        )

    message = str(excinfo.value)
    assert SIM_RUN_ID in message
    assert str(AS_OF) in message


@pytest.mark.parametrize("legs가_있나", [False, True])
def test_persist_status_marks_the_axis_as_checked(legs가_있나):
    """🔴 **길이를 여기서 세지 않는다 (W3-3).** 두 status 는 *"이 승인으로 그 축을
       확인했다"* 하나만 말하고, 몇 건인가는 Reader 가 `inbound_schedules` 에서 낸다.

    ⚠️ 종전에는 여기서 `CONFIRMED_ZERO` 를 적었다 — fixture JSON 이 목록의 정본이던
       시절엔 쓰는 쪽이 길이를 알고 있었기 때문이다. 지금 그 값을 적으면 **일정 표를
       안 보고 0 건이라고 단정**하는 셈이 된다 (`CONFIRMED_ZERO` 어휘 자체는
       `day_open` · `arrival` 에 그대로 살아 있다).
    """
    conn = 가짜커넥션()
    rows = build_next_inventory(_두회차(), purchase_ids=매입참조) if legs가_있나 else []

    persist_inventory(conn, **_fixture_인자(rows))

    assert _written_in_transit_status(conn) == "CONFIRMED"
    assert _written_confirmed_status(conn) == "CONFIRMED"


def test_persist_update_does_not_touch_other_status_columns():
    """⑤ `confirmed_outbound_*` · `evidence_grade` · `approved_by` 는 남의 칸이다.

    ⚠️ `confirmed_inbound_*` 는 **2026-09-04 부터 이 UPDATE 가 겸한다** (임시). 덮지
       않고 병합하는지는 아래 `test_persist_merges_...` 가 잰다 — 여기서는 그 둘
       말고는 아무 칸도 늘지 않았음을 잠근다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

    statement = _fixture_질의(conn)[-1]
    assert "confirmed_outbound_status" not in statement
    assert "confirmed_outbound_json" not in statement
    assert "evidence_grade" not in statement
    assert "approved_by" not in statement
    assert "lot_priority" not in statement
    assert "zone_capacity" not in statement
    # 🔴 **JSON 두 칸도 더 이상 안 쓴다 (W3-3).** 업무 사실의 정본이 옮겨 갔다.
    assert "in_transit_json" not in statement
    assert "confirmed_inbound_json" not in statement


# ── persist_inventory · confirmed_inbound 병합 (임시 조치) ───────────────
#
# ⚠️ **이 절 전체가 임시다.** 승인과 발주 확정은 다른 사실인데 지금은 발주 확정
#    단계에 코드가 없어 승인을 그것으로 대신 본다. 물류가 그 단계를 만들면 병합도
#    이 검사들도 함께 걷어낸다 (`transition.py` 모듈 docstring 참조).
#
# 🔴 걷어내기 전까지 지켜야 하는 것은 하나다 — **덮지 않고 더한다.**


def test_persist_does_not_touch_someone_elses_schedule():
    """① 앞선 승인이 적어 둔 일정을 **건드리지 않는다.**

    🔴 종전에는 목록 하나를 통째로 읽어 병합해 되돌려 적었고, 그 병합이 틀리면 남의
       입고가 에러 없이 사라졌다. 지금은 **자기 `inbound_id` 한 행씩만** 적는다 —
       남의 행을 읽지도 쓰지도 않으므로 사라질 자리가 구조적으로 없다.
    """
    남의것 = _일정행("INB-OTHER-9")
    conn = 가짜커넥션(일정=[남의것])

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

    적힌것 = _적힌_일정(conn)
    assert [행["inbound_id"] for 행 in 적힌것] == [
        "INB-H1-REQ-1-1-1",
        "INB-H1-REQ-1-1-2",
    ]
    assert conn.커서._일정["INB-OTHER-9"] == 남의것, "남의 일정을 그대로 둔다"
    assert _written_confirmed_status(conn) == "CONFIRMED"


def test_persist_is_idempotent_for_the_same_approval():
    """② 같은 승인을 두 번 반영해도 목록이 안 부푼다.

    ★ 첫 반영이 쓴 목록을 그대로 두 번째 반영의 기존 목록으로 물린다 — 실제로 같은
      날 같은 승인이 두 번 흐를 때 일어나는 일이다.

    ⚠️ 중복이 생기면 B-1 이 `CONFIRMED_INBOUND_ID_DUPLICATED` 로 잡지만, **여기서 안
       만드는 것이 먼저다.**
    """
    rows = build_next_inventory(_두회차(), purchase_ids=매입참조)
    첫번 = 가짜커넥션()
    persist_inventory(첫번, **_fixture_인자(rows))

    두번 = 가짜커넥션(일정=_적힌_일정(첫번))
    persist_inventory(두번, **_fixture_인자(rows))

    assert len(_적힌_일정(첫번)) == 2
    assert _적힌_일정(두번) == [], "같은 사실이라 새로 적을 것이 없다"


def test_persist_writes_values_that_pass_the_b1_gap_rule(complete_logistics_snapshot):
    """③ 🔴 **이 PR 의 핵심이다.** 쓴 두 칸이 B-1 을 실제로 통과한다.

    ★ **값 비교를 재구현하지 않는다.** `find_in_transit_schedule_gap` 을 그대로 불러
      `None` 이 나오는지 본다 — 재구현하면 물류가 규칙을 바꾼 날 검사만 통과한다.

    ★ 두 payload 를 **JSON 을 지나 스키마로 되돌려** 넘긴다. `quantity_kg` 가 `!=` 로
      비교되므로 직렬화·역직렬화 왕복 뒤에도 같은 `Decimal` 이어야 한다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

    snapshot = complete_logistics_snapshot.model_copy(update=_두_축(conn))

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

    persist_inventory(
        conn, **_fixture_인자(build_next_inventory(commitment, purchase_ids=매입참조))
    )

    적힌것 = _적힌_일정(conn)[0]
    assert 적힌것["quantity_kg"] == Decimal("0.1"), "float 을 거치면 값이 흔들린다"
    assert isinstance(적힌것["quantity_kg"], Decimal)
    assert 적힌것["expected_arrival_date"] == date(2026, 1, 2)


def test_persist_empty_approval_writes_no_schedule():
    """④ 빈 승인은 일정 표에 **아무것도 안 적는다** — 지우지도 않는다.

    ★ *"더할 것이 없다"* 와 *"기존 것을 지워라"* 는 다르다. 앞선 승인이 적어 둔 행은
      그대로 남고, 이번 승인은 자기 몫이 없으므로 INSERT 도 없다.
    """
    남의것 = _일정행("INB-OTHER-9")
    conn = 가짜커넥션(일정=[남의것])

    persist_inventory(conn, **_fixture_인자([]))

    assert _적힌_일정(conn) == [], "적을 것이 없다"
    assert conn.커서._일정["INB-OTHER-9"] == 남의것, "남의 일정을 안 지운다"
    assert _written_confirmed_status(conn) == "CONFIRMED"


def test_persist_reads_before_writing_on_the_given_connection():
    """★ 병합은 **읽기 없이는 못 한다.** 그 읽기가 같은 커넥션·같은 커서다.

    🔴 자기 커넥션을 새로 열면 마스터가 쥔 트랜잭션 밖에서 읽게 되어, 같은 커밋 안의
       앞선 write 를 못 보고 그것을 덮는다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

    읽기, 쓰기 = _fixture_질의(conn)
    # ★ **목록을 더 이상 여기서 안 읽는다 (W3-3).** 행이 있는지 보고 잠그기만 한다 —
    #   업무 사실의 정본이 `inbound_schedules` 로 옮겨 갔기 때문이다.
    assert "in_transit_json" not in 읽기
    assert "confirmed_inbound_json" not in 읽기
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

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

    읽기, 쓰기 = _fixture_질의(conn)
    assert "FOR UPDATE" in 읽기, "쓰기 전에 그 fixture 행을 잠근다"
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
        persist_inventory(
            conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조))
        )

    assert len(conn.커서.queries) == 1
    assert not _is_write(str(conn.커서.queries[0]))
    assert _적힌_일정(conn) == [], "일정도 안 적는다"


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


def test_adapter_empty_commitment_still_marks_the_day():
    """③ 빈 약정도 **그날 행에 «확인했다» 를 적는다.**

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
    assert _written_in_transit_status(conn) == "CONFIRMED"
    assert _적힌_일정(conn) == [], "적을 회차가 없다"


def test_adapter_persist_passes_sim_run_id_and_the_target_state_date():
    """④ 🔴 **하루 어긋난 행에 쓰는 것을 잡는다.**

    `as_of` 로 넘어가는 값은 승인일(`commitment.as_of`)이 아니라 마스터가 준
    `target_state_date` 다. 재무가 같은 날짜로 `finance_states` 를 세우므로, 여기서
    하루 앞 행에 쓰면 두 장부가 다른 날에 앉는다 — 에러 없이 갈린다.
    """
    adapter = _adapter()
    conn = 가짜커넥션()

    adapter.persist(conn, adapter.build(
            _두회차(), target_state_date=TARGET_STATE_DATE, purchase_ids=매입참조
        ))

    params = _update_params(conn)
    assert SIM_RUN_ID in params, "sim_run_id 를 그대로 넘겨야 WHERE 가 그 행을 찾는다"
    assert TARGET_STATE_DATE in params
    assert AS_OF not in params, "승인일 행에 썼다 — 재무 상태와 하루 어긋난다"


def test_adapter_persist_does_not_commit_or_rollback():
    """⑤ 커밋은 재무 write 와 함께 **마스터가 한 번** 한다."""
    adapter = _adapter()
    conn = 가짜커넥션()

    adapter.persist(conn, adapter.build(
            _두회차(), target_state_date=TARGET_STATE_DATE, purchase_ids=매입참조
        ))

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_adapter_passes_the_missing_fixture_error_through():
    """★ 행이 없으면 어댑터가 삼키지 않는다 — 마스터가 `FAILED` 로 사유를 남긴다."""
    adapter = _adapter()
    conn = 가짜커넥션(rowcount=0)

    with pytest.raises(LogisticsFixtureMissing):
        adapter.persist(conn, adapter.build(
            _두회차(), target_state_date=TARGET_STATE_DATE, purchase_ids=매입참조
        ))


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


def _뷰(행: dict[str, Any]) -> InboundScheduleView:
    """적힌 일정 한 행을 Reader 가 내는 모양으로. **두 축이 여기서 갈라진다.**"""
    return InboundScheduleView(
        inbound_id=행["inbound_id"],
        sim_run_id=행["sim_run_id"],
        purchase_item_id=행["purchase_item_id"],
        purchase_id="PUR-REQ-1-D1-S1",
        item_id="ITEM-BAECHU",
        item_name="배추",
        quantity_kg=행["quantity_kg"],
        expected_arrival_date=행["expected_arrival_date"],
        created_as_of=행["created_as_of"],
        has_receipt=False,
        stock_applied=False,
    )


def _두_축(conn: 가짜커넥션) -> dict[str, Any]:
    """적힌 일정 한 벌에서 **두 축을 함께** 낸다.

    🔴 **B-1 이 재던 어긋남이 구조적으로 없어졌다 (W3-2).** 종전에는 fixture 행의
       JSON 두 칸이 각자 자라서 한쪽만 빠지는 일이 있었고, `find_in_transit_schedule_gap`
       이 그것을 잡았다. 지금은 **같은 표 같은 행**에서 두 축이 나온다
       (`InboundScheduleView.as_in_transit` · `as_scheduled_quantity`) —
       이 검사는 그 사실을 잠근다.
    """
    뷰들 = [_뷰(행) for 행 in _적힌_일정(conn)]
    return {
        "in_transit": [v.as_in_transit() for v in 뷰들],
        "confirmed_inbound_schedule": [v.as_scheduled_quantity() for v in 뷰들],
    }


def test_A_일정이_없으면_새로_적는다():
    """★ 승인분마다 `inbound_schedules` 에 한 행씩 선다."""
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차(), purchase_ids=매입참조)))

    적힌것 = _적힌_일정(conn)
    assert [행["inbound_id"] for 행 in 적힌것] == ["INB-H1-REQ-1-1-1"]
    assert 적힌것[0]["purchase_item_id"] == "PI-REQ-1-D1-S1"
    assert 적힌것[0]["created_as_of"] == AS_OF, "그날이 곧 이 일정이 장부에 선 날이다"
    assert 적힌것[0]["source_ref"] == "APPROVAL:H1-REQ-1-1"


def test_B_같은_사실이면_다시_안_적는다():
    """멱등 재반영 — 같은 `inbound_id` · 같은 사실이면 no-op 다.

    ⚠️ 종전에는 목록을 병합해 되돌려 적었고 «안 부푼다» 가 그 결과였다. 지금은
       **INSERT 자체가 안 일어난다** — `record_schedule` 이 `False` 를 낸다.
    """
    rows = build_next_inventory(_한회차(), purchase_ids=매입참조)
    이미있음 = _일정행("INB-H1-REQ-1-1-1")
    conn = 가짜커넥션(일정=[이미있음])

    persist_inventory(conn, **_fixture_인자(rows))

    assert _적힌_일정(conn) == []
    assert conn.커서._일정["INB-H1-REQ-1-1-1"] == 이미있음, "갈아 끼우지도 않는다"


def test_C_직렬화_자릿수가_달라도_같은_수량이면_멱등이다():
    """🔴 문자열로 비교하면 `"300"` 과 `"300.00"` 이 갈려 정상 재반영이 터진다.

    ★ `record_schedule` 이 `Decimal(str(...))` 로 되돌려 비교한다.
    """
    rows = build_next_inventory(_한회차(qty_kg=300.0), purchase_ids=매입참조)
    conn = 가짜커넥션(일정=[_일정행("INB-H1-REQ-1-1-1", quantity_kg="300.00")])

    persist_inventory(conn, **_fixture_인자(rows))

    assert _적힌_일정(conn) == [], "자릿수만 다른 것은 같은 사실이다"


def test_D_다른_승인은_각자_자기_행에_선다():
    """🔴 이 단계가 고치는 자리다. 종전에는 목록을 덮어 `[B]` 만 남았다."""
    남의것 = _일정행("INB-OTHER-9")
    conn = 가짜커넥션(일정=[남의것])

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차(), purchase_ids=매입참조)))

    assert [행["inbound_id"] for 행 in _적힌_일정(conn)] == ["INB-H1-REQ-1-1-1"]
    assert set(conn.커서._일정) == {"INB-OTHER-9", "INB-H1-REQ-1-1-1"}
    assert conn.커서._일정["INB-OTHER-9"] == 남의것


def test_E_같은_id_에_다른_수량이면_멈춘다():
    """★ 어느 쪽이 진짜인지 여기서 고르지 않는다 — 덮지도 버리지도 않는다."""
    conn = 가짜커넥션(일정=[_일정행("INB-H1-REQ-1-1-1", quantity_kg="300")])

    with pytest.raises(ScheduleConflict) as 오류:
        persist_inventory(
            conn,
            **_fixture_인자(
                build_next_inventory(_한회차(qty_kg=999.0), purchase_ids=매입참조)
            ),
        )

    assert "INB-H1-REQ-1-1-1" in str(오류.value)
    assert _적힌_일정(conn) == [], "충돌하면 아무것도 안 적는다"


def test_F_취소된_일정은_되살리지_않는다():
    """🔴 **취소 여부를 대조 넷보다 먼저 본다.** 값이 같아도 그 행은 이미
       *"그날부터 없다"* 고 적힌 행이다 — 취소를 무르는 업무 계약이 저장소에 없다.

    ⚠️ 종전 이 자리는 *"기존 목록에 중복 id 가 있으면 멈춘다"* 였다. 그 상태는
       **표에서는 성립하지 않는다** — `(sim_run_id, inbound_id)` 가 PK 다. 대신
       표가 새로 만든 위험이 이것이다.
    """
    conn = 가짜커넥션(일정=[_일정행("INB-H1-REQ-1-1-1", cancelled_as_of=date(2026, 1, 6))])

    with pytest.raises(ScheduleAlreadyCancelled) as 오류:
        persist_inventory(
            conn, **_fixture_인자(build_next_inventory(_한회차(), purchase_ids=매입참조))
        )

    assert "INB-H1-REQ-1-1-1" in str(오류.value)
    assert _적힌_일정(conn) == []


def test_G_승인분_안에_충돌하는_중복_id_가_있으면_멈춘다():
    conn = 가짜커넥션()
    같은_id_다른_수량 = [
        InTransitItem(
            inbound_id="INB-DUP-1",
            purchase_id="PUR-REQ-1-D1-S1",
            item="배추",
            quantity_kg=Decimal(10),
            expected_arrival_date=date(2026, 1, 2),
        ),
        InTransitItem(
            inbound_id="INB-DUP-1",
            purchase_id="PUR-REQ-1-D1-S1",
            item="배추",
            quantity_kg=Decimal(20),
            expected_arrival_date=date(2026, 1, 2),
        ),
    ]

    with pytest.raises(ScheduleConflict):
        persist_inventory(conn, **_fixture_인자(같은_id_다른_수량))


def test_H_승인분_안의_동일한_중복은_한_행만_남는다():
    conn = 가짜커넥션()
    똑같은_두_행 = build_next_inventory(_한회차(), purchase_ids=매입참조) * 2

    persist_inventory(conn, **_fixture_인자(똑같은_두_행))

    assert len(_적힌_일정(conn)) == 1


def test_I_매입_참조가_없으면_적기_전에_멈춘다():
    """🔴 **비워 두고 넘어가지 않는다.** 이 표가 정본이라 빠진 행은
       *"승인은 났는데 도착 조회에 안 잡히는 입고"* 가 된다 — FIRSTINB 사고의 모양이다.

    ⚠️ 종전 이 자리는 *"참조 없던 행에 참조가 붙는 것도 다른 사실이다"* 였다.
       참조 없는 행 자체가 이제 못 서므로 그 상태가 성립하지 않는다.
    """
    conn = 가짜커넥션()

    with pytest.raises(ScheduleReferenceMissing) as 오류:
        persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차())))

    assert "INB-H1-REQ-1-1-1" in str(오류.value)
    assert _적힌_일정(conn) == []


def test_J_같은_id_에_다른_매입_참조면_멈춘다():
    """🔴 **`purchase_item_id` 만 대조에서 빼는 예외를 두지 않는다.**

    같은 `inbound_id` 인데 매입 출처가 다르면 같은 건이 아니다. 조용히 한쪽을
    남기면 도착 뒤 **틀린 매입 줄에서 등급·단가를 읽는다.**
    """
    conn = 가짜커넥션(일정=[_일정행("INB-H1-REQ-1-1-1", purchase_item_id="PI-OTHER")])

    with pytest.raises(ScheduleConflict) as 오류:
        persist_inventory(
            conn, **_fixture_인자(build_next_inventory(_한회차(), purchase_ids=매입참조))
        )

    assert "INB-H1-REQ-1-1-1" in str(오류.value)


def test_K_confirmed_inbound_모양은_그대로다():
    """🔴 **일정·수량 사실에는 출처를 얹지 않는다.**

    `ScheduledQuantity` 는 outbound 등 다른 일정에도 재사용된다. 어느 매입에서
    왔는지는 **운송 중인 물건의 속성**이지 일정의 속성이 아니다.
    """
    conn = 가짜커넥션()

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_두회차(), purchase_ids=매입참조)))

    두축 = _두_축(conn)
    적힌_일정 = [row.model_dump(mode="json") for row in 두축["confirmed_inbound_schedule"]]
    assert 적힌_일정, "확정 일정이 비면 이 검사가 아무것도 안 잰다"
    assert all(set(row) == {"date", "quantity_kg", "item", "inbound_id"} for row in 적힌_일정)
    assert all(row.purchase_id for row in 두축["in_transit"]), (
        "운송 중 쪽에는 반대로 반드시 있어야 한다"
    )


def test_L_두_축이_한_표에서_나와_B1_이_선다(complete_logistics_snapshot):
    """★ 두 축이 같은 행에서 갈라지므로 짝이 안 맞을 자리가 없다."""
    conn = 가짜커넥션(일정=[_일정행("INB-OTHER-9")])

    persist_inventory(conn, **_fixture_인자(build_next_inventory(_한회차(), purchase_ids=매입참조)))

    스냅샷 = complete_logistics_snapshot.model_copy(update=_두_축(conn))

    assert find_in_transit_schedule_gap(스냅샷) is None



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
