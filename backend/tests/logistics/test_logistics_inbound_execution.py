"""도착 실행 **조립층**의 규율 검사 (3-B4-J). DB 를 부르지 않는다.

```text
남의 로직을 다시 짜지 않나   SQL 도 수량 계산도 도착일 규칙도 이 파일에 없다
어디서부터 이어가나          receipt_status 로 갈라 마지막 성공 단계 다음부터
검수를 지어내지 않나          provider 가 없으면 BLOCKED — PASS 를 만들지 않는다
막힘과 실패를 가르나          부재는 값으로, 무결성은 예외로
경계를 안 넘나               commit · rollback · close · 새 커넥션이 없다
```

★ **`select_due_inbound` 만은 진짜를 쓴다.** 순수 계산이고, *"도착 판정을 다시
  구현하지 않았다"* 가 이 파일이 재야 할 사실이라 대역으로 바꾸면 그 검사가 사라진다.

⚠️ **검사 이름은 한글, 헬퍼·대역·fixture 이름은 영어다.** 앞은 무엇을 재는지 읽히게
   하려는 것이고, 뒤는 저장소의 identifier 규율을 따른 것이다.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self, get_args

import pytest

from app.logistics import inbound_execution, inbound_stock, inspections
from app.logistics.arrival import ArrivalBlockReason, ArrivalUnresolvedReason
from app.logistics.inbound_execution import (
    InboundBlockReason,
    InspectionFact,
    LogisticsInboundExecution,
    UnknownReceiptStage,
)
from app.logistics.inbound_stock import (
    InboundStockResult,
    InvalidReceivingAxis,
    LotIntegrityError,
    ScheduleIntegrityError,
    load_in_transit_for_receiving,
)
from app.logistics.inspections import (
    InspectionConflict,
    InspectionOutcome,
    InspectionWriteResult,
)
from app.logistics.purchase_detail import (
    PurchaseDetail,
    PurchaseDetailAmbiguous,
    PurchaseDetailMissing,
)
from app.logistics.receipts import ReceiptExistence, ReceiptStatus, ReceiptWriteResult
from app.logistics.schemas import InTransitItem
from app.logistics.transition import USAGE_SCOPE
from app.master.inbound import PARTS, InboundPartOut

SIM_RUN_ID = "SIM-BURNIN-202512"
AS_OF = date(2026, 1, 7)
INSPECTED_AT = datetime(2026, 1, 7, 9, 30, tzinfo=UTC)
INSPECTOR = "WH-INSPECTOR-01"

INBOUND_A = "INB-H1-THRU-20260105-BAECHU-1-1"
PURCHASE_A = "PUR-THRU-20260105-BAECHU-D1-S1"
INBOUND_B = "INB-H1-THRU-20260105-MU-1-1"
PURCHASE_B = "PUR-THRU-20260105-MU-D1-S1"

QTY = Decimal("3587.000000")


# ── 자잘한 만들기 ───────────────────────────────────────────────────────


def _in_transit_row(
    *,
    inbound_id: str | None = INBOUND_A,
    purchase_id: str | None = PURCHASE_A,
    item: str = "배추",
    qty: str = "3587.000000",
    eta: date | None = AS_OF,
) -> InTransitItem:
    return InTransitItem(
        inbound_id=inbound_id,
        purchase_id=purchase_id,
        item=item,
        quantity_kg=Decimal(qty),
        expected_arrival_date=eta,
    )


def _purchase_detail(
    *, purchase_item_id: str = "PI-A", item_id: str = "ITEM-BAECHU"
) -> PurchaseDetail:
    return PurchaseDetail(
        purchase_item_id=purchase_item_id,
        item_id=item_id,
        grade=None,
        quantity_kg=QTY,
        unit_price_krw_per_kg=Decimal("933.000000"),
    )


def _make_outcome(
    *, accepted: str | None = None, hold: str = "0", reject: str = "0"
) -> InspectionOutcome:
    """시험용 **고정값**이다. 운영 검수 정책이 아니다.

    🔴 여기 적힌 수치에서 production 규칙을 역으로 만들지 않는다 — 저장소에 그 규칙이
       없다는 것이 지금의 사실이고, 이 값은 *"provider 가 준 사실을 그대로 넘기나"* 를
       재기 위한 상수일 뿐이다.
    """
    inspected = QTY
    return InspectionOutcome(
        verdict="PASS" if accepted is None else "HOLD",
        inspected_qty_kg=inspected,
        accepted_qty_kg=inspected if accepted is None else Decimal(accepted),
        hold_qty_kg=Decimal(hold),
        reject_qty_kg=Decimal(reject),
    )


def _make_fact(outcome: InspectionOutcome | None = None) -> InspectionFact:
    return InspectionFact(
        inspected_at=INSPECTED_AT, inspector=INSPECTOR, outcome=outcome or _make_outcome()
    )


def _receipt_id(inbound_id: str) -> str:
    """`receipts.receipt_id_for` 와 **같은 규칙**이다 — 대역이 진짜와 다른 id 를 내면
    상태 재개 검사가 아무것도 안 잰다."""
    return f"RCPT-{SIM_RUN_ID}-{inbound_id}"


def _existing_receipt(status: ReceiptStatus, *, inbound_id: str = INBOUND_A) -> ReceiptExistence:
    return ReceiptExistence(
        status="ALREADY_EXISTS", receipt_id=_receipt_id(inbound_id), receipt_status=status
    )


class FakeInspectionProvider:
    """결정론 대역 provider. **자동 검수 정책이 아니다.**

    ★ `absent=True` 는 *"이 입고의 검수 사실을 모른다"* 를 뜻한다 — 실패가 아니라 부재다.
    """

    def __init__(self, fact: InspectionFact | None = None, *, absent: bool = False) -> None:
        self.calls: list[tuple[date, str, PurchaseDetail]] = []
        self._fact = None if absent else (fact or _make_fact())

    def provide(
        self, *, as_of: date, inbound: Any, purchase_detail: PurchaseDetail
    ) -> InspectionFact | None:
        self.calls.append((as_of, inbound.inbound_id, purchase_detail))
        return self._fact


class FakeCursor:
    def __init__(self, log: list[tuple[str, Any]], rows: list[Any]) -> None:
        self.log = log
        self._rows = rows
        self._out: list[Any] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        text = str(query)
        self.log.append((text, params))
        self._out = [] if "pg_advisory" in text else list(self._rows)

    def fetchall(self) -> list[Any]:
        return list(self._out)

    def fetchone(self) -> Any:
        raise AssertionError("fetchone 을 쓰면 2행 이상이 조용히 첫 행으로 나간다")


class FakeConnection:
    """경계만 센다 — 이 조립층은 커넥션으로 아무것도 하지 않아야 한다."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.log: list[tuple[str, Any]] = []
        self._rows = rows if rows is not None else []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.log, self._rows)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def _unwrap(value: Any) -> Any:
    """대역의 반환값. `Exception` 이면 **던진다** — 실패 경로를 같은 표로 적기 위해서다."""
    if isinstance(value, Exception):
        raise value
    return value


class Wiring:
    """DB 를 만지는 이웃 함수 여섯을 **기록하는 대역**으로 바꾼다.

    🔴 `select_due_inbound` 는 바꾸지 않는다 — 진짜 도착 판정이 돌아야 한다.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        in_transit: Any,
        detail: Any = None,
        receipt_state: dict[str, ReceiptExistence] | None = None,
        materialize: Any = None,
        inspection_write: Any = None,
    ) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self._in_transit = in_transit
        self._detail = _purchase_detail() if detail is None else detail
        self._receipt_state = receipt_state or {}
        self._materialize = materialize
        self._inspection_write = inspection_write

        monkeypatch.setattr(inbound_execution, "load_in_transit_for_receiving", self._load)
        monkeypatch.setattr(inbound_execution, "fetch_purchase_detail", self._fetch_detail)
        monkeypatch.setattr(inbound_execution, "check_receipt_state", self._check_state)
        monkeypatch.setattr(inbound_execution, "create_arrived_receipt", self._create_receipt)
        monkeypatch.setattr(inbound_execution, "record_inspection", self._record_inspection)
        monkeypatch.setattr(
            inbound_execution, "materialize_inspected_inbound", self._materialize_call
        )

    # ── 대역들 ─────────────────────────────────────────────────────────

    def _load(self, conn: Any, **kwargs: Any) -> Any:
        self.log.append(("load_in_transit", kwargs))
        return _unwrap(self._in_transit)

    def _fetch_detail(self, conn: Any, **kwargs: Any) -> PurchaseDetail:
        self.log.append(("fetch_purchase_detail", kwargs))
        by_purchase = self._detail
        if isinstance(by_purchase, dict):
            return _unwrap(by_purchase[kwargs["purchase_id"]])
        return _unwrap(by_purchase)

    def _check_state(self, conn: Any, **kwargs: Any) -> ReceiptExistence:
        self.log.append(("check_receipt_state", kwargs))
        fresh = ReceiptExistence(status="NEW", receipt_id=None, receipt_status=None)
        return self._receipt_state.get(kwargs["inbound_id"], fresh)

    def _create_receipt(self, conn: Any, **kwargs: Any) -> ReceiptWriteResult:
        self.log.append(("create_arrived_receipt", kwargs))
        return ReceiptWriteResult(
            applied=True,
            receipt_id=_receipt_id(kwargs["inbound"].inbound_id),
            receipt_status="ARRIVED",
        )

    def _record_inspection(self, conn: Any, **kwargs: Any) -> InspectionWriteResult:
        self.log.append(("record_inspection", kwargs))
        _unwrap(self._inspection_write)
        return InspectionWriteResult(
            applied=True,
            inspection_id="INSP-" + kwargs["receipt_id"],
            receipt_status="INSPECTED",
            outcome=kwargs["outcome"],
        )

    def _materialize_call(self, conn: Any, **kwargs: Any) -> InboundStockResult:
        self.log.append(("materialize", kwargs))
        _unwrap(self._materialize)
        return InboundStockResult(
            applied=True,
            receipt_status="PUTAWAY_DONE",
            lot_id="LOT-" + kwargs["receipt_id"],
            move_id="MOVE-IN-LOT-" + kwargs["receipt_id"],
            accepted_qty_kg=QTY,
            schedule_cleared=True,
        )

    # ── 읽기 편의 ──────────────────────────────────────────────────────

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.log]

    def call_args(self, name: str) -> dict[str, Any]:
        return next(kwargs for called, kwargs in self.log if called == name)

    def call_count(self, name: str) -> int:
        return sum(1 for called, _ in self.log if called == name)


def _make_execution(
    provider: Any = None, *, sim_run_id: str = SIM_RUN_ID
) -> LogisticsInboundExecution:
    return LogisticsInboundExecution(
        sim_run_id=sim_run_id, inspection_provider=provider or FakeInspectionProvider()
    )


def _receive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    in_transit: Any,
    provider: Any = None,
    as_of: date = AS_OF,
    **wiring_kwargs: Any,
) -> tuple[InboundPartOut, Wiring, Any]:
    used = FakeInspectionProvider() if provider is None else provider
    wiring = Wiring(monkeypatch, in_transit=in_transit, **wiring_kwargs)
    result = _make_execution(used).receive(FakeConnection(), as_of=as_of)
    return result, wiring, used


def _code_only(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"자동 PASS 를 만들지 않는다"* 고 **설명하는 문장**이
       위반으로 잡힌다. 설명과 실행문은 다른 것이다.
    """
    tree = ast.parse(source)
    code = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                code = code.replace(docstring, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in code.splitlines())


def _source() -> str:
    return Path(inbound_execution.__file__).read_text(encoding="utf-8")


# ── 1~6. 도착 대상 고르기 ───────────────────────────────────────────────


def test_1_due_inbound_은_끝까지_처리된다(monkeypatch: pytest.MonkeyPatch):
    result, wiring, _ = _receive(monkeypatch, in_transit=[_in_transit_row()])

    assert result.status == "RECEIVED"
    assert result.received == [INBOUND_A]
    assert wiring.names == [
        "load_in_transit",
        "fetch_purchase_detail",
        "check_receipt_state",
        "create_arrived_receipt",
        "record_inspection",
        "materialize",
    ]


def test_2_연체분도_처리한다(monkeypatch: pytest.MonkeyPatch):
    """🔴 당일만 보면 **그 하루가 영원히 안 오는 물건으로 남는다.**

    ★ 실측 자리다 — 도착 예정 2026-01-07 이 02-06 까지 `in_transit` 에 남아 있었다.
    """
    eta = AS_OF - timedelta(days=30)
    result, wiring, _ = _receive(monkeypatch, in_transit=[_in_transit_row(eta=eta)])

    assert result.status == "RECEIVED"
    assert result.received == [INBOUND_A]
    # ★ 예정일을 `as_of` 로 옮기지 않는다 — 그 날짜는 로트의 `received_at` 이 된다.
    assert wiring.call_args("create_arrived_receipt")["inbound"].expected_arrival_date == eta


def test_3_미래_도착만_있으면_받을_것이_없다(monkeypatch: pytest.MonkeyPatch):
    future = _in_transit_row(eta=AS_OF + timedelta(days=1))
    result, wiring, _ = _receive(monkeypatch, in_transit=[future])

    assert result.status == "NOTHING_DUE"
    assert result.received == []
    assert wiring.names == ["load_in_transit"], "도착 전 물건에 DB 를 더 묻지 않는다"


def test_3b_미래_행은_매입_참조가_없어도_막힘이_아니다(monkeypatch: pytest.MonkeyPatch):
    """⚠️ 아직 오지도 않은 물건을 *"막혔다"* 고 적으면 협의 중인 정상 상태가 매일
    장애로 보고된다."""
    future = _in_transit_row(eta=AS_OF + timedelta(days=3), purchase_id=None)
    result, _, _ = _receive(monkeypatch, in_transit=[future])

    assert result.status == "NOTHING_DUE"


def test_4_빈_목록은_받을_것이_없다(monkeypatch: pytest.MonkeyPatch):
    result, _, _ = _receive(monkeypatch, in_transit=[])

    assert result.status == "NOTHING_DUE"
    assert result.reason == ""


def test_5_운송_목록을_모르면_BLOCKED_다(monkeypatch: pytest.MonkeyPatch):
    """🔴 `None` 과 `[]` 는 **다른 사실이다.**

    *"오늘 받을 것이 없다"* 와 *"오늘 뭐가 도착할지 모른다"* 를 뭉치면, 후자를 전자로
    읽는 순간 모르는 것을 아는 것처럼 다루게 된다.
    """
    result, _, _ = _receive(monkeypatch, in_transit=None)

    assert result.status == "BLOCKED"
    assert result.reason == "IN_TRANSIT_UNRESOLVED"
    assert result.received == []


def test_6_매입_참조가_없는_due_는_BLOCKED_다(monkeypatch: pytest.MonkeyPatch):
    """⚠️ 지금 실데이터가 정확히 이 상태다 — 도착일은 지났는데 `purchase_id` 가 없다."""
    result, wiring, _ = _receive(monkeypatch, in_transit=[_in_transit_row(purchase_id=None)])

    assert result.status == "BLOCKED"
    assert result.reason == f"{INBOUND_A}: ARRIVAL_PURCHASE_REFERENCE_MISSING"
    assert wiring.names == ["load_in_transit"], "참조 없이 매입을 조회하지 않는다"


def test_6b_도착일을_모르는_행도_BLOCKED_다(monkeypatch: pytest.MonkeyPatch):
    result, _, _ = _receive(monkeypatch, in_transit=[_in_transit_row(eta=None)])

    assert result.status == "BLOCKED"
    assert result.reason == f"{INBOUND_A}: ARRIVAL_DATE_UNRESOLVED"


def test_6c_사유가_둘이면_둘_다_남는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 하나만 남기면 그것을 고친 뒤 **또 막힌다.**"""
    broken = _in_transit_row(inbound_id=None, purchase_id=None)
    result, _, _ = _receive(monkeypatch, in_transit=[broken])

    assert result.status == "BLOCKED"
    assert result.reason == (
        "ARRIVAL_INBOUND_ID_MISSING; ARRIVAL_PURCHASE_REFERENCE_MISSING"
    ), "inbound_id 가 없는 행도 사유 목록에서 사라지면 안 된다"


def test_6d_도착일_규칙을_다시_구현하지_않았다():
    """★ `<=` · `>` · `as_of` 비교가 이 파일에 **없어야** 한다 — 판정의 주인은 `arrival` 이다."""
    code = _code_only(_source())

    assert "select_due_inbound" in code
    for banned in ("expected_arrival_date <", "expected_arrival_date >", "timedelta", "overdue"):
        assert banned not in code, f"{banned} — 도착 판정을 여기서 다시 하고 있다"


# ── 7~8. 매입 참조 ──────────────────────────────────────────────────────


def test_7_매입_줄이_없으면_BLOCKED_다(monkeypatch: pytest.MonkeyPatch):
    """🔴 시세·평균원가·품목명 유추로 **대신 채우지 않는다.**"""
    result, wiring, provider = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        detail=PurchaseDetailMissing("매입 줄이 없다"),
    )

    assert result.status == "BLOCKED"
    assert result.reason == f"{INBOUND_A}: PURCHASE_DETAIL_MISSING"
    assert wiring.names == ["load_in_transit", "fetch_purchase_detail"]
    assert provider.calls == [], "매입 줄도 없는데 검수를 묻지 않는다"


def test_8_매입_줄이_둘이면_BLOCKED_다(monkeypatch: pytest.MonkeyPatch):
    """🔴 첫 행도 최신도 **고르지 않는다** — 고른 단가가 로트 원가로 굳는다."""
    result, wiring, _ = _receive(
        monkeypatch, in_transit=[_in_transit_row()], detail=PurchaseDetailAmbiguous("둘 이상")
    )

    assert result.status == "BLOCKED"
    assert result.reason == f"{INBOUND_A}: PURCHASE_DETAIL_AMBIGUOUS"
    assert "create_arrived_receipt" not in wiring.names


def test_8b_매입_열쇠는_purchase_id_하나다(monkeypatch: pytest.MonkeyPatch):
    """🔴 identity 를 `(sim_run_id, purchase_id)` 로 바꾸지 않는다."""
    _, wiring, _ = _receive(monkeypatch, in_transit=[_in_transit_row()])

    assert wiring.call_args("fetch_purchase_detail") == {"purchase_id": PURCHASE_A}


# ── 9~14. 상태별 재개 ───────────────────────────────────────────────────


def test_9_NEW_는_Receipt_부터_만든다(monkeypatch: pytest.MonkeyPatch):
    result, wiring, provider = _receive(monkeypatch, in_transit=[_in_transit_row()])

    assert wiring.names[3:] == ["create_arrived_receipt", "record_inspection", "materialize"]
    assert [inbound_id for _, inbound_id, _ in provider.calls] == [INBOUND_A]
    assert result.received == [INBOUND_A]


def test_10_ARRIVED_는_Receipt_를_다시_만들지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 `ALREADY_EXISTS` 를 *"다 됐다"* 로 읽지도, 처음부터 다시 돌지도 않는다."""
    result, wiring, provider = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        receipt_state={INBOUND_A: _existing_receipt("ARRIVED")},
    )

    assert wiring.names[3:] == ["record_inspection", "materialize"]
    assert wiring.call_count("create_arrived_receipt") == 0
    assert len(provider.calls) == 1
    assert result.status == "RECEIVED"


def test_11_INSPECTING_도_검수부터_이어간다(monkeypatch: pytest.MonkeyPatch):
    result, wiring, provider = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        receipt_state={INBOUND_A: _existing_receipt("INSPECTING")},
    )

    assert wiring.names[3:] == ["record_inspection", "materialize"]
    assert len(provider.calls) == 1
    assert result.status == "RECEIVED"


def test_12_INSPECTED_는_재고화만_한다(monkeypatch: pytest.MonkeyPatch):
    """🔴 이미 적힌 검수 사실을 이번 판정으로 **덮으려 들지 않는다.**"""
    result, wiring, provider = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        receipt_state={INBOUND_A: _existing_receipt("INSPECTED")},
    )

    assert wiring.names[3:] == ["materialize"]
    assert provider.calls == [], "검수가 끝난 건에 provider 를 부르면 안 된다"
    assert result.status == "RECEIVED"


def test_13_PUTAWAY_DONE_은_재고화를_다시_부른다(monkeypatch: pytest.MonkeyPatch):
    """★ 재고를 다시 만들지 않는다 — 기존 Lot · Move 를 **읽어서 검증**하고 남은
    일정을 걷는 것이 `materialize_inspected_inbound` 의 일이다."""
    result, wiring, provider = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        receipt_state={INBOUND_A: _existing_receipt("PUTAWAY_DONE")},
    )

    assert wiring.names[3:] == ["materialize"]
    assert provider.calls == []
    assert result.status == "RECEIVED"
    assert result.received == [INBOUND_A]


def test_14_CLOSED_도_재고화를_다시_부른다(monkeypatch: pytest.MonkeyPatch):
    result, wiring, provider = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        receipt_state={INBOUND_A: _existing_receipt("CLOSED")},
    )

    assert wiring.names[3:] == ["materialize"]
    assert provider.calls == []
    assert result.status == "RECEIVED"


def test_14b_재고화는_어느_상태에서도_불린다(monkeypatch: pytest.MonkeyPatch):
    """⚠️ `PUTAWAY_DONE` 인데 일정이 안 걷힌 **반쪽 상태**가 있을 수 있다."""
    for status in get_args(ReceiptStatus):
        _, wiring, _ = _receive(
            monkeypatch,
            in_transit=[_in_transit_row()],
            receipt_state={INBOUND_A: _existing_receipt(status)},
        )
        assert wiring.call_count("materialize") == 1, status


def test_14c_재고화에_넘기는_인자가_계약대로다(monkeypatch: pytest.MonkeyPatch):
    _, wiring, _ = _receive(monkeypatch, in_transit=[_in_transit_row()])

    assert wiring.call_args("materialize") == {
        "as_of": AS_OF,
        "receipt_id": _receipt_id(INBOUND_A),
        "purchase_detail": _purchase_detail(),
        "usage_scope": USAGE_SCOPE,
    }


def test_14d_검수_단계_구분이_inspections_와_같다():
    """🔴 갈리면 검수를 못 적는 상태에서 provider 를 부르거나 부를 자리를 건너뛴다."""
    assert inbound_execution._NEEDS_INSPECTION == inspections._BEFORE_INSPECTION
    assert inbound_execution._INSPECTION_SETTLED == inspections._INSPECTION_DONE
    # ★ 둘이 Receipt 상태 어휘를 남김없이 덮는다 — 그래서 UnknownReceiptStage 가 뜰
    #   유일한 경우가 "어휘가 늘었다" 뿐이다.
    assert inbound_execution._NEEDS_INSPECTION | inbound_execution._INSPECTION_SETTLED == set(
        get_args(ReceiptStatus)
    )


def test_14e_모르는_단계는_아는_단계로_접지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 검수 전으로 보면 적힌 검수를 덮고, 검수 후로 보면 검수를 건너뛴 채 재고를 만든다."""
    monkeypatch.setattr(inbound_execution, "_INSPECTION_SETTLED", frozenset({"CLOSED"}))
    wiring = Wiring(
        monkeypatch,
        in_transit=[_in_transit_row()],
        receipt_state={INBOUND_A: _existing_receipt("PUTAWAY_DONE")},
    )

    with pytest.raises(UnknownReceiptStage, match="PUTAWAY_DONE"):
        _make_execution().receive(FakeConnection(), as_of=AS_OF)

    assert wiring.call_count("materialize") == 0, "모르는 단계에서 재고화로 나아가면 안 된다"


# ── 15~17. 멱등 · 여러 건 · 실행 격리 ───────────────────────────────────


def test_15_이미_걷힌_일정은_다시_처리하지_않는다(monkeypatch: pytest.MonkeyPatch):
    """★ 첫 실행이 `_clear_schedule` 로 그 건을 걷었으므로 다음 실행은 아예 안 본다 —
    중복 Receipt · Lot · Move 를 막는 것은 각 모듈의 멱등 장치이고, 이 층은 **다시
    부르지도 않는다.**"""
    result, wiring, _ = _receive(monkeypatch, in_transit=[])

    assert result.status == "NOTHING_DUE"
    assert wiring.names == ["load_in_transit"]


def test_15b_같은_건을_다시_받아도_새로_만들지_않는다(monkeypatch: pytest.MonkeyPatch):
    """⚠️ 일정이 아직 안 걷힌 재실행이다 — 기존 경로로 들어가고 새 Receipt 를 안 만든다."""
    result, wiring, provider = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        receipt_state={INBOUND_A: _existing_receipt("PUTAWAY_DONE")},
    )

    assert wiring.call_count("create_arrived_receipt") == 0
    assert wiring.call_count("record_inspection") == 0
    assert provider.calls == []
    assert result.received == [INBOUND_A]


def test_16_정상_A_와_막힌_B_가_함께_있으면(monkeypatch: pytest.MonkeyPatch):
    """🔴 **한 건이 막혀도 나머지는 계속 간다.** 그리고 받은 것은 지우지 않는다.

    ★ `BLOCKED` 가 `RECEIVED` 를 이긴다 — *"받을 게 있었는데 못 받았다"* 가 먼저
      알려야 하는 사실이다.
    """
    result, wiring, _ = _receive(
        monkeypatch,
        in_transit=[
            _in_transit_row(),
            _in_transit_row(inbound_id=INBOUND_B, purchase_id=PURCHASE_B, item="무"),
        ],
        detail={PURCHASE_A: _purchase_detail(), PURCHASE_B: PurchaseDetailMissing("없다")},
    )

    assert result.status == "BLOCKED"
    assert result.received == [INBOUND_A]
    assert result.reason == f"{INBOUND_B}: PURCHASE_DETAIL_MISSING"
    assert wiring.call_count("materialize") == 1


def test_16b_막힌_건이_뒤에_있어도_앞의_정상건이_처리된다(monkeypatch: pytest.MonkeyPatch):
    """★ `due` 는 `(도착일, inbound_id)` 순이다 — 순서가 뒤바뀌어도 결과가 같아야 한다."""
    rows = [
        _in_transit_row(inbound_id=INBOUND_B, purchase_id=PURCHASE_B, item="무"),
        _in_transit_row(),
    ]
    detail = {PURCHASE_A: _purchase_detail(), PURCHASE_B: PurchaseDetailMissing("없다")}
    result, _, _ = _receive(monkeypatch, in_transit=rows, detail=detail)
    reversed_result, _, _ = _receive(monkeypatch, in_transit=list(reversed(rows)), detail=detail)

    assert result == reversed_result
    assert result.received == [INBOUND_A]


def test_17_모든_조회가_내_실행으로_좁혀진다(monkeypatch: pytest.MonkeyPatch):
    """🔴 남의 실행 장부에 Receipt · Lot · 원장을 적지 않는다."""
    _, wiring, _ = _receive(monkeypatch, in_transit=[_in_transit_row()])

    assert wiring.call_args("load_in_transit") == {
        "sim_run_id": SIM_RUN_ID,
        "as_of": AS_OF,
        "usage_scope": USAGE_SCOPE,
    }
    assert wiring.call_args("check_receipt_state")["sim_run_id"] == SIM_RUN_ID
    assert wiring.call_args("create_arrived_receipt")["sim_run_id"] == SIM_RUN_ID


def test_17b_빈_sim_run_id_로는_만들_수_없다():
    """⚠️ 빈 문자열은 **주입은 했는데 값이 안 실린 것**이다 — 조용히 넘기면 조회가
    0건이 되고 그 0건은 *"그날 행이 없다"* 로 읽힌다."""
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="sim_run_id"):
            LogisticsInboundExecution(
                sim_run_id=blank, inspection_provider=FakeInspectionProvider()
            )


def test_17c_실행을_None_으로_접지_않는다():
    """🔴 `day_open` 은 조회 경로라 `None` 을 받는 길이 있었지만 이쪽은 쓰기 경로다."""
    with pytest.raises((ValueError, TypeError)):
        LogisticsInboundExecution(
            sim_run_id=None,  # type: ignore[arg-type]
            inspection_provider=FakeInspectionProvider(),
        )


# ── 18~19. 막힘과 실패의 경계 ───────────────────────────────────────────


def test_18_검수_충돌은_BLOCKED_로_삼키지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 무결성 위반을 `BLOCKED` 로 접으면 *"데이터를 주세요"* 로 나가고, 깨진 장부
    위에서 다음 날이 계속 걷는다."""
    Wiring(
        monkeypatch,
        in_transit=[_in_transit_row()],
        inspection_write=InspectionConflict("다른 사실의 검수가 있다"),
    )

    with pytest.raises(InspectionConflict):
        _make_execution().receive(FakeConnection(), as_of=AS_OF)


def test_19_재고화_실패는_그대로_올라간다(monkeypatch: pytest.MonkeyPatch):
    """★ 마스터가 통째로 롤백하고 `FAILED` 로 만든다 — 물류가 `FAILED` 를 짓지 않는다."""
    Wiring(
        monkeypatch, in_transit=[_in_transit_row()], materialize=LotIntegrityError("Lot 이 없다")
    )

    with pytest.raises(LotIntegrityError):
        _make_execution().receive(FakeConnection(), as_of=AS_OF)


def test_19b_일정_행이_없으면_그대로_올라간다(monkeypatch: pytest.MonkeyPatch):
    """⚠️ 그날 행이 없다는 것은 하루가 안 열렸다는 뜻이다 — 부재가 아니라 순서 위반이다."""
    Wiring(monkeypatch, in_transit=ScheduleIntegrityError("그날 fixture 행이 없다"))

    with pytest.raises(ScheduleIntegrityError):
        _make_execution().receive(FakeConnection(), as_of=AS_OF)


def test_19c_broad_catch_가_없다():
    """🔴 `except Exception` 하나면 위 세 검사가 전부 조용히 무력해진다."""
    code = _code_only(_source())

    assert "except Exception" not in code
    assert "except BaseException" not in code
    caught = {
        node.type.id
        for node in ast.walk(ast.parse(_source()))
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name)
    }
    assert caught == {"PurchaseDetailMissing", "PurchaseDetailAmbiguous"}, caught


def test_19d_물류가_FAILED_어휘를_만들지_않는다():
    code = _code_only(_source())

    assert "FAILED" not in code
    assert set(get_args(InboundPartOut.model_fields["status"].annotation)) == {
        "RECEIVED",
        "NOTHING_DUE",
        "BLOCKED",
    }


# ── 20. 경계 소유권 ─────────────────────────────────────────────────────


def test_20_커밋도_롤백도_닫기도_새_커넥션도_없다(monkeypatch: pytest.MonkeyPatch):
    wiring = Wiring(monkeypatch, in_transit=[_in_transit_row()])
    conn = FakeConnection()

    _make_execution().receive(conn, as_of=AS_OF)

    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 0, 0)
    assert wiring.call_count("materialize") == 1
    code = _code_only(_source())
    for banned in ("commit", "rollback", ".close(", "get_connection", "psycopg"):
        assert banned not in code, f"{banned} — 트랜잭션 경계는 마스터 것이다"


def test_20b_SQL_도_스키마도_이_파일에_없다():
    """★ 이 층은 **조립만 한다.** SQL 이 한 줄이라도 있으면 남의 소유를 다시 짜는 것이다."""
    code = _code_only(_source())

    for banned in (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "sql.SQL",
        "cursor",
        "get_db_schema",
        "inbound_receipts",
        "inbound_inspections",
        "inventory_lots",
        "inventory_moves",
        "logistics_runtime_fixture",
        "pg_advisory",
        "FOR UPDATE",
    ):
        assert banned not in code, f"{banned} — WMS Core 를 다시 구현하고 있다"


def test_20c_id_규칙을_다시_짓지_않는다():
    """🔴 `receipt_id` · `lot_id` · `move_id` 의 주인은 각 모듈이다."""
    code = _code_only(_source())

    for banned in (
        "RCPT-",
        "LOT-",
        "MOVE-",
        "INSP-",
        "receipt_id_for",
        "lot_id_for",
        "move_id_for",
    ):
        assert banned not in code, f"{banned} — id 를 여기서 짓고 있다"


def test_20d_다른_파트를_임포트하지_않는다():
    """⚠️ `app.master.inbound` 만은 예외다 — 결과 계약(`InboundPartOut`)의 주인이다."""
    tree = ast.parse(_source())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert not [m for m in modules if m.startswith(("app.purchase", "app.finance", "app.sales"))]
    assert {m for m in modules if m.startswith("app.master")} == {"app.master.inbound"}


def test_20e_파트_이름이_마스터_등록소와_같다():
    assert inbound_execution._PART in PARTS


def test_20f_운영_코드에_한글_identifier_가_없다():
    """★ 저장소 규율 — `app/**` 의 identifier 는 영어다. 주석·docstring·메시지는 한글이다."""
    tree = ast.parse(_source())
    korean: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names = [node.name]
        elif isinstance(node, ast.keyword) and node.arg:
            names = [node.arg]
        korean += [n for n in names if any("가" <= ch <= "힣" for ch in n)]

    assert korean == [], korean


# ── 21~23. 검수 provider ────────────────────────────────────────────────


def test_21_검수_전_상태에서만_provider_를_부른다(monkeypatch: pytest.MonkeyPatch):
    for status in ("ARRIVED", "INSPECTING"):
        _, _, provider = _receive(
            monkeypatch,
            in_transit=[_in_transit_row()],
            receipt_state={INBOUND_A: _existing_receipt(status)},  # type: ignore[arg-type]
        )
        assert len(provider.calls) == 1, status


def test_21b_NEW_도_provider_를_부른다(monkeypatch: pytest.MonkeyPatch):
    _, _, provider = _receive(monkeypatch, in_transit=[_in_transit_row()])

    as_of, inbound_id, detail = provider.calls[0]
    assert (as_of, inbound_id) == (AS_OF, INBOUND_A)
    assert detail == _purchase_detail(), "provider 는 매입 원장의 권위값을 함께 본다"


def test_22_검수가_끝난_상태에서는_provider_를_부르지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 부르면 DB 에 적힌 검수 사실을 이번 판정으로 덮으려 들고, `record_inspection`
    이 `InspectionConflict` 로 **정상 재실행을 실패로 뒤집는다.**"""
    for status in ("INSPECTED", "PUTAWAY_DONE", "CLOSED"):
        _, wiring, provider = _receive(
            monkeypatch,
            in_transit=[_in_transit_row()],
            receipt_state={INBOUND_A: _existing_receipt(status)},  # type: ignore[arg-type]
        )
        assert provider.calls == [], status
        assert wiring.call_count("record_inspection") == 0, status


def test_23_provider_가_준_사실을_그대로_넘긴다(monkeypatch: pytest.MonkeyPatch):
    """🔴 시각도 검수자도 판정도 **가공하지 않는다.** 가공하면 provider 가 준 사실과
    DB 에 적힌 사실이 갈린다."""
    outcome = _make_outcome(accepted="3000.000000", hold="587.000000")
    fact = InspectionFact(
        inspected_at=datetime(2026, 1, 7, 14, 5, tzinfo=UTC),
        inspector="WH-INSPECTOR-99",
        outcome=outcome,
    )
    _, wiring, _ = _receive(
        monkeypatch, in_transit=[_in_transit_row()], provider=FakeInspectionProvider(fact)
    )

    passed = wiring.call_args("record_inspection")
    assert passed["inspected_at"] is fact.inspected_at
    assert passed["inspector"] is fact.inspector
    assert passed["outcome"] is outcome
    assert passed["receipt_id"] == _receipt_id(INBOUND_A)


def test_23b_provider_가_사실을_모르면_BLOCKED_다(monkeypatch: pytest.MonkeyPatch):
    """🔴 대신 만들지 않는다 — 여기서 PASS 를 지어내면 그 수량이 그대로 가용재고가
    되고, **아무도 그 판정을 한 적이 없다.**"""
    result, wiring, _ = _receive(
        monkeypatch,
        in_transit=[_in_transit_row()],
        provider=FakeInspectionProvider(absent=True),
    )

    assert result.status == "BLOCKED"
    assert result.reason == f"{INBOUND_A}: INSPECTION_FACT_UNAVAILABLE"
    assert result.received == []
    assert wiring.call_count("record_inspection") == 0
    assert wiring.call_count("materialize") == 0, "검수 사실 없이 재고를 만들지 않는다"


def test_23c_provider_없이는_만들_수_없다():
    """🔴 기본값을 두는 순간 그 기본값이 **검수 정책**이 된다."""
    with pytest.raises(TypeError):
        LogisticsInboundExecution(sim_run_id=SIM_RUN_ID)  # type: ignore[call-arg]


def test_23c2_provider_None_은_생성_시점에_막힌다():
    """🔴 **배선 오류는 배선 시점에 터져야 한다.**

    타입상 필수여도 런타임에서는 `None` 이 들어온다. 그대로 두면 객체는 멀쩡히 서고
    **실제로 도착할 물건이 생긴 날** `provide` 에서 늦게 터지는데, 그때는 마스터가
    그 예외를 `FAILED` 로 바꿔 그날 입고를 통째로 롤백한다 — 배선 실수가 운영 장애의
    모습으로 나타난다.
    """
    with pytest.raises(ValueError, match="inspection_provider"):
        LogisticsInboundExecution(
            sim_run_id=SIM_RUN_ID,
            inspection_provider=None,  # type: ignore[arg-type]
        )


def test_23c3_Protocol_준수까지_검사하지는_않는다():
    """⚠️ `isinstance` 로 서명까지 보려 들면 대역·부분구현이 정당한 자리에서 막힌다 —
    생성자가 막는 것은 *"주입을 안 했다"* 하나뿐이다."""

    class MinimalProvider:
        def provide(self, **kwargs: Any) -> None:
            return None

    assert LogisticsInboundExecution(
        sim_run_id=SIM_RUN_ID, inspection_provider=MinimalProvider()
    ) is not None
    code = _code_only(_source())
    assert "isinstance" not in code
    assert "runtime_checkable" not in code


def test_23d_자동_검수_정책이_코드에_없다():
    """🔴 저장소 어디에도 *"몇 %가 PASS 인가"* 를 정한 규칙이 없다. 여기서 만들지 않는다."""
    code = _code_only(_source())

    for banned in (
        "PASS",
        "HOLD",
        "REJECT",
        "SYSTEM",
        "accepted",
        "quantity_kg",
        "Decimal",
        "now()",
        ".now(",
        "today(",
        "utcnow",
    ):
        assert banned not in code, f"{banned} — 검수 사실을 여기서 만들고 있다"


def test_23e_기본_provider_구현이_저장소에_없다():
    """★ 이 파일이 내놓는 것은 **계약뿐**이다 — `provide` 를 구현한 운영 클래스가 없다."""
    tree = ast.parse(_source())
    impls = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(isinstance(b, ast.FunctionDef) and b.name == "provide" for b in node.body)
        and not any(isinstance(b, ast.Name) and b.id == "Protocol" for b in node.bases)
    ]
    assert impls == [], f"자동 검수 provider 를 만들었다: {impls}"


# ── 어휘 ────────────────────────────────────────────────────────────────


def test_막힘_어휘가_arrival_것을_그대로_쓴다():
    """★ 같은 사실에 두 어휘를 두지 않는다."""
    reasons = set(get_args(InboundBlockReason))

    assert set(get_args(ArrivalBlockReason)) <= reasons
    assert set(get_args(ArrivalUnresolvedReason)) <= reasons


# ── 17. 잠금 순서 (reader) ──────────────────────────────────────────────


@pytest.fixture
def pin_schema_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inbound_stock, "get_db_schema", lambda: "haetdeul")


def _fixture_rows(in_transit: Any, confirmed: Any = None) -> list[Any]:
    return [(in_transit, confirmed if confirmed is not None else [])]


def test_L1_도착_잠금이_fixture_잠금보다_먼저다(pin_schema_name: None):
    """🔴 순서가 뒤집히면 두 트랜잭션이 요청하는 잠금 집합에 **전순서가 없어져** 교착이
    생긴다 (`ledger._lock_ledger_writes` 가 겪은 자리)."""
    conn = FakeConnection(_fixture_rows([]))

    load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    queries = [query for query, _ in conn.log]
    assert "pg_advisory_xact_lock" in queries[0], "도착 전역 잠금이 가장 먼저다"
    assert "FOR UPDATE" in queries[1], "그다음이 fixture 행 잠금이다"
    assert "logistics_runtime_fixture" in queries[1]


def test_L2_도착_쓰기와_같은_전역_키를_쓴다(pin_schema_name: None):
    """★ 세 번째 잠금을 만들지 않는다 — `receipts` 의 그 키를 그대로 쓴다."""
    conn = FakeConnection(_fixture_rows([]))

    load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    locks = [params for query, params in conn.log if "pg_advisory" in query]
    assert locks == [(20260905, 2)]


def test_L3_조회_축이_유일성_축과_같다(pin_schema_name: None):
    conn = FakeConnection(_fixture_rows([]))

    load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    _, params = conn.log[1]
    assert params == (SIM_RUN_ID, AS_OF, USAGE_SCOPE)


def test_L4_None_과_빈목록을_가른다(pin_schema_name: None):
    unknown = load_in_transit_for_receiving(
        FakeConnection(_fixture_rows(None)), sim_run_id=SIM_RUN_ID, as_of=AS_OF
    )
    zero = load_in_transit_for_receiving(
        FakeConnection(_fixture_rows([])), sim_run_id=SIM_RUN_ID, as_of=AS_OF
    )

    assert unknown is None, "확인한 적 없다 — 0 건으로 바꾸지 않는다"
    assert zero == []


def test_L5_행을_InTransitItem_으로_좁힌다(pin_schema_name: None):
    stored = _in_transit_row().model_dump(mode="json")
    conn = FakeConnection(_fixture_rows([stored]))

    loaded = load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert loaded == [_in_transit_row()]


def test_L6_계약_밖_행은_조용히_걸러지지_않는다(pin_schema_name: None):
    """★ 걸러 내면 그 행이 사라진 줄 아무도 모른다 — `in_transit` 은 도착 대상 목록이다."""
    conn = FakeConnection(_fixture_rows([{"inbound_id": "INB-X", "모르는칸": 1}]))

    with pytest.raises(Exception, match="(?i)valid"):
        load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)


def test_L7_그날_행이_없으면_부재로_접지_않는다(pin_schema_name: None):
    conn = FakeConnection([])

    with pytest.raises(ScheduleIntegrityError):
        load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)


def test_L8_빈_실행축으로는_묻지_않는다(pin_schema_name: None):
    """🔴 없는 열쇠로 물으면 0건이 돌아오고 그것은 *"그날 행이 없다"* 로 읽힌다."""
    for blank in ("", "   "):
        conn = FakeConnection(_fixture_rows([]))
        with pytest.raises(InvalidReceivingAxis):
            load_in_transit_for_receiving(conn, sim_run_id=blank, as_of=AS_OF)
        assert conn.log == [], "DB 에 묻기도 전에 막는다"


def test_L8b_빈_usage_scope_도_묻기_전에_막힌다(pin_schema_name: None):
    """🔴 조회 축은 세 칸이다 — `sim_run_id` 만 막으면 나머지 한 칸으로 같은 구멍이 남는다.

    ★ 기본값이 있어 정상 경로에서는 안 비지만, 이 함수는 **public export** 라 기본값을
      안 쓰는 호출자가 언제든 생긴다.
    """
    for blank in ("", "   "):
        conn = FakeConnection(_fixture_rows([]))
        with pytest.raises(InvalidReceivingAxis, match="usage_scope"):
            load_in_transit_for_receiving(
                conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF, usage_scope=blank
            )
        assert conn.log == [], "잠금도 걸기 전에 막는다"


def test_L9_커밋도_롤백도_새_커넥션도_없다(pin_schema_name: None):
    conn = FakeConnection(_fixture_rows([]))

    load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 0, 0)


def test_L10_두_건_이상이면_첫_행을_고르지_않는다(pin_schema_name: None):
    conn = FakeConnection([([], []), ([], [])])

    with pytest.raises(LotIntegrityError):
        load_in_transit_for_receiving(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)
