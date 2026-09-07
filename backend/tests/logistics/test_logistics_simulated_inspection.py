"""MVP 시뮬레이션 검수 provider 의 규율 검사 (`#333`). DB 를 부르지 않는다.

```text
무엇을 만드나        도착 수량 그대로 PASS — 손실률도 비율도 없다
결정론인가           같은 입력 두 번 → 글자 그대로 같은 InspectionFact
Core 계약과 짝인가    validate_outcome 통과 · tz 있는 시각 · 빈 inspector 아님
근거 없는 축을 쓰나   등급·단가가 판정을 바꾸면 없는 규칙을 만든 것이다
조립에 꽂히나        provider 를 바꿔 끼우기만 하면 RECEIVED 까지 간다
```

⚠️ **검사 이름은 한글, 헬퍼·대역 이름은 영어다** — `test_logistics_inbound_execution.py`
   와 같은 규율이다.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import inbound_execution, simulated_inspection
from app.logistics.arrival import DueInbound
from app.logistics.inbound_execution import InspectionFact, LogisticsInboundExecution
from app.logistics.inbound_stock import InboundStockResult
from app.logistics.inspections import InspectionWriteResult, validate_outcome
from app.logistics.purchase_detail import PurchaseDetail
from app.logistics.receipts import ReceiptExistence, ReceiptWriteResult
from app.logistics.schemas import InTransitItem
from app.logistics.simulated_inspection import ScenarioSimulatedInspectionProvider

SIM_RUN_ID = "SIM-BURNIN-202512"
AS_OF = date(2026, 1, 7)
INBOUND_A = "INB-H1-THRU-20260105-BAECHU-1-1"
PURCHASE_A = "PUR-THRU-20260105-BAECHU-D1-S1"
QTY = Decimal("3587.000000")


# ── 자잘한 만들기 ───────────────────────────────────────────────────────


def _in_transit_row(*, qty: str = "3587.000000", eta: date = AS_OF) -> InTransitItem:
    return InTransitItem(
        inbound_id=INBOUND_A,
        purchase_id=PURCHASE_A,
        item="배추",
        quantity_kg=Decimal(qty),
        expected_arrival_date=eta,
    )


def _due(*, qty: str = "3587.000000", eta: date = AS_OF) -> DueInbound:
    item = _in_transit_row(qty=qty, eta=eta)
    return DueInbound(
        item=item,
        inbound_id=INBOUND_A,
        purchase_id=PURCHASE_A,
        expected_arrival_date=eta,
        overdue=eta < AS_OF,
    )


def _purchase_detail(
    *, grade: str | None = None, unit_price: str = "933.000000", qty: str = "3587.000000"
) -> PurchaseDetail:
    return PurchaseDetail(
        purchase_item_id="PI-A",
        item_id="ITEM-BAECHU",
        grade=grade,
        quantity_kg=Decimal(qty),
        unit_price_krw_per_kg=Decimal(unit_price),
    )


def _provide(
    *,
    as_of: date = AS_OF,
    inbound: DueInbound | None = None,
    detail: PurchaseDetail | None = None,
) -> InspectionFact:
    return ScenarioSimulatedInspectionProvider().provide(
        as_of=as_of,
        inbound=inbound or _due(),
        purchase_detail=detail or _purchase_detail(),
    )


def _code_only(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"난수를 쓰지 않는다"* 고 **설명하는 문장**이 위반으로
       잡힌다 — 설명과 실행문은 다른 것이다.
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
    return Path(simulated_inspection.__file__).read_text(encoding="utf-8")


# ── 1~5. 무엇을 만드나 ──────────────────────────────────────────────────


def test_1_판정은_PASS_다():
    assert _provide().outcome.verdict == "PASS"


def test_2_검수량은_도착_수량_그대로다():
    """🔴 매입 줄 수량으로 다시 세지 않는다 — 둘은 다른 축이다."""
    inbound = _due(qty="1234.500000")

    outcome = _provide(inbound=inbound, detail=_purchase_detail(qty="9999.000000")).outcome

    assert outcome.inspected_qty_kg == inbound.item.quantity_kg
    assert outcome.inspected_qty_kg == Decimal("1234.500000")


def test_3_수용량이_검수량과_같다():
    outcome = _provide().outcome

    assert outcome.accepted_qty_kg == outcome.inspected_qty_kg == QTY


def test_4_보류도_거부도_0_이다():
    """🔴 손실률이 아니라 *"이번 MVP 가 그 축을 아직 안 쓴다"* 는 뜻이다."""
    outcome = _provide().outcome

    assert outcome.hold_qty_kg == Decimal(0)
    assert outcome.reject_qty_kg == Decimal(0)


def test_5_네_수량이_모두_Decimal_이다():
    """🔴 `validate_outcome` 이 `float` · `int` · `bool` 을 전부 거부한다."""
    outcome = _provide().outcome

    for 칸 in ("inspected_qty_kg", "accepted_qty_kg", "hold_qty_kg", "reject_qty_kg"):
        값 = getattr(outcome, 칸)
        assert type(값) is Decimal, f"{칸} 이 {type(값).__name__} 다"


# ── 6~8. Core 계약과 짝인가 ─────────────────────────────────────────────


def test_6_Core_검증을_통과한다():
    """★ 항등식·판정↔수량 규칙의 주인은 `inspections` 다 — 여기서 다시 적지 않고
    **진짜 검증기를 돌려** 짝인지 잰다."""
    validate_outcome(_provide().outcome)


def test_7_검수시각에_시간대가_있다():
    """🔴 naive 를 `TIMESTAMPTZ` 에 넣으면 세션 TimeZone 에 따라 뜻이 달라져
    `record_inspection` 이 거부한다."""
    inspected_at = _provide().inspected_at

    assert inspected_at.tzinfo is not None
    assert inspected_at.utcoffset() is not None


def test_8_검수자가_비지_않는다():
    """🔴 `inbound_inspections.inspector` 는 NOT NULL 이고 `record_inspection` 이
    공백뿐인 값도 막는다."""
    inspector = _provide().inspector

    assert inspector.strip()
    assert inspector == "SCENARIO_SIMULATED"


# ── 9~12. 시각 규칙 ─────────────────────────────────────────────────────


def test_9_검수일은_처리일이다():
    outcome_date = _provide(as_of=AS_OF).inspected_at.date()

    assert outcome_date == AS_OF


def test_10_연체분도_도착예정일이_아니라_처리일을_쓴다():
    """★ 도착일의 주인은 `receipts.create_arrived_receipt`(`arrived_at`) 이고, 검수는
    **처리한 날** 한 일이다 — 같은 사실을 두 칸에 적지 않는다."""
    eta = AS_OF - timedelta(days=30)

    fact = _provide(as_of=AS_OF, inbound=_due(eta=eta))

    assert fact.inspected_at.date() == AS_OF
    assert fact.inspected_at.date() != eta


def test_11_자정_UTC_다():
    """⚠️ `00:00` 은 업무 시각 주장이 아니다 — 우리가 아는 시간 사실은 **날짜 하나**다.

    ★ UTC 인 이유: `00:00 UTC` 는 KST 로 **같은 날 09:00** 이라 어느 쪽으로 읽어도
      날짜가 안 밀린다. `00:00 KST` 는 UTC 로 전날 15:00 이다.
    """
    assert _provide().inspected_at == datetime(2026, 1, 7, 0, 0, tzinfo=UTC)


def test_12_시계를_읽지_않는다():
    """🔴 같은 시뮬레이션을 다시 돌리면 같은 값이 나와야 한다."""
    코드 = _code_only(_source())

    for 금지 in ("datetime.now", "utcnow", "today(", ".now("):
        assert 금지 not in 코드, f"{금지} — 검수 시각을 지어내고 있다"


# ── 13~16. 결정론과 근거 없는 축 ────────────────────────────────────────


def test_13_같은_입력이면_같은_사실이다():
    """🔴 두 번 부른 값이 다르면 정상 재실행이 `InspectionConflict` 로 뒤집힌다."""
    첫번 = _provide()
    두번 = _provide()

    assert 첫번 == 두번
    assert 첫번.outcome == 두번.outcome


def test_14_난수를_쓰지_않는다():
    """🔴 저장소에 품질손실 Scenario 근거가 없다 — 시드도 비율도 만들지 않는다."""
    코드 = _code_only(_source())

    for 금지 in ("random", "seed", "shuffle", "uniform", "choice"):
        assert 금지 not in 코드, f"{금지} — 품질손실을 지어내고 있다"


def test_15_등급이_판정을_바꾸지_않는다():
    """🔴 *"등급이 수용률을 정한다"* 는 규칙이 저장소에 없다."""
    기준 = _provide(detail=_purchase_detail(grade=None)).outcome

    for grade in ("특", "상", "중", "하"):
        assert _provide(detail=_purchase_detail(grade=grade)).outcome == 기준, grade


def test_16_단가가_판정을_바꾸지_않는다():
    기준 = _provide(detail=_purchase_detail(unit_price="933.000000")).outcome

    assert _provide(detail=_purchase_detail(unit_price="12345.000000")).outcome == 기준


def test_17_정상_경로는_None_을_내지_않는다():
    """★ Protocol 의 `None` 은 *"이 입고의 검수 사실을 모른다"* 이고, 이 provider 는
    모르는 입고가 없다. 그 계약 자체는 다른 provider 를 위해 그대로 남는다."""
    assert _provide() is not None
    assert _provide(inbound=_due(qty="0.000001")) is not None


def test_18_상태도_생성_인자도_없다():
    """★ 합격률·시드를 인자로 열면 그 순간 *"누군가 그 값을 정해야 한다"* 가 된다."""
    assert ScenarioSimulatedInspectionProvider().provide is not None
    assert vars(ScenarioSimulatedInspectionProvider()) == {}


def test_19_DB_도_외부_호출도_하지_않는다():
    """🔴 provider 는 **두 잠금을 쥔 채** 불린다 — 여기서 느리면 도착 처리 전체가 선다."""
    코드 = _code_only(_source())

    for 금지 in (
        "SELECT",
        "INSERT",
        "UPDATE",
        "cursor",
        "conn",
        "commit",
        "rollback",
        "sql.SQL",
        "get_db_schema",
        "requests",
        "httpx",
        "open(",
    ):
        assert 금지 not in 코드, f"{금지} — 순수 계산이 아니다"


def test_20_다른_파트를_임포트하지_않는다():
    tree = ast.parse(_source())
    모듈: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            모듈.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            모듈.add(node.module)

    assert not [
        m for m in 모듈 if m.startswith(("app.master", "app.purchase", "app.finance", "app.sales"))
    ]


def test_21_운영_코드에_한글_identifier_가_없다():
    """★ 저장소 규율 — `app/**` 의 identifier 는 영어다. 주석·docstring 은 한글이다."""
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


# ── 조립 — provider 만 바꿔 끼운다 ──────────────────────────────────────
#
# ★ **큰 E2E fixture 를 새로 만들지 않는다.** `test_logistics_inbound_execution.py` 가
#   조립층 규율을 이미 전부 재고 있으므로, 여기서는 *"진짜 provider 를 꽂았을 때
#   RECEIVED 까지 가고 PASS 사실이 그대로 흘러가나"* 하나만 잰다.


def _receipt_id(inbound_id: str) -> str:
    """`receipts.receipt_id_for` 와 **같은 규칙**이다."""
    return f"RCPT-{SIM_RUN_ID}-{inbound_id}"


class FakeCursor:
    def __init__(self) -> None:
        self._rows: list[Any] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        self._rows = []

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class FakeConnection:
    """경계만 센다 — 조립층은 커넥션으로 아무것도 하지 않아야 한다."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class Wiring:
    """DB 를 만지는 이웃 함수 여섯을 **기록하는 대역**으로 바꾼다.

    🔴 `select_due_inbound` 는 바꾸지 않는다 — 진짜 도착 판정이 돌아야 한다.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, in_transit: Any) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self._in_transit = in_transit

        monkeypatch.setattr(inbound_execution, "load_in_transit_for_receiving", self._load)
        monkeypatch.setattr(inbound_execution, "fetch_purchase_detail", self._fetch_detail)
        monkeypatch.setattr(inbound_execution, "check_receipt_state", self._check_state)
        monkeypatch.setattr(inbound_execution, "create_arrived_receipt", self._create_receipt)
        monkeypatch.setattr(inbound_execution, "record_inspection", self._record_inspection)
        monkeypatch.setattr(
            inbound_execution, "materialize_inspected_inbound", self._materialize_call
        )

    def _load(self, conn: Any, **kwargs: Any) -> Any:
        self.log.append(("load_in_transit", kwargs))
        return self._in_transit

    def _fetch_detail(self, conn: Any, **kwargs: Any) -> PurchaseDetail:
        self.log.append(("fetch_purchase_detail", kwargs))
        return _purchase_detail()

    def _check_state(self, conn: Any, **kwargs: Any) -> ReceiptExistence:
        self.log.append(("check_receipt_state", kwargs))
        return ReceiptExistence(status="NEW", receipt_id=None, receipt_status=None)

    def _create_receipt(self, conn: Any, **kwargs: Any) -> ReceiptWriteResult:
        self.log.append(("create_arrived_receipt", kwargs))
        return ReceiptWriteResult(
            applied=True,
            receipt_id=_receipt_id(kwargs["inbound"].inbound_id),
            receipt_status="ARRIVED",
        )

    def _record_inspection(self, conn: Any, **kwargs: Any) -> InspectionWriteResult:
        self.log.append(("record_inspection", kwargs))
        return InspectionWriteResult(
            applied=True,
            inspection_id="INSP-" + kwargs["receipt_id"],
            receipt_status="INSPECTED",
            outcome=kwargs["outcome"],
        )

    def _materialize_call(self, conn: Any, **kwargs: Any) -> InboundStockResult:
        self.log.append(("materialize", kwargs))
        return InboundStockResult(
            applied=True,
            receipt_status="PUTAWAY_DONE",
            lot_id="LOT-" + kwargs["receipt_id"],
            move_id="MOVE-IN-LOT-" + kwargs["receipt_id"],
            accepted_qty_kg=QTY,
            schedule_cleared=True,
        )

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.log]

    def call_args(self, name: str) -> dict[str, Any]:
        return next(kwargs for called, kwargs in self.log if called == name)


def test_22_조립에_꽂으면_RECEIVED_까지_간다(monkeypatch: pytest.MonkeyPatch):
    """```text
    Due inbound → Receipt → ScenarioSimulatedInspectionProvider → PASS → materialize → RECEIVED
    ```"""
    wiring = Wiring(monkeypatch, in_transit=[_in_transit_row()])
    conn = FakeConnection()

    result = LogisticsInboundExecution(
        sim_run_id=SIM_RUN_ID,
        inspection_provider=ScenarioSimulatedInspectionProvider(),
    ).receive(conn, as_of=AS_OF)

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
    # ★ 조립층이 provider 의 사실을 **가공 없이** 넘겼나.
    적힌것 = wiring.call_args("record_inspection")
    assert 적힌것["inspector"] == "SCENARIO_SIMULATED"
    assert 적힌것["inspected_at"] == datetime(2026, 1, 7, 0, 0, tzinfo=UTC)
    assert 적힌것["outcome"].verdict == "PASS"
    assert 적힌것["outcome"].accepted_qty_kg == QTY
    assert 적힌것["outcome"].hold_qty_kg == Decimal(0)
    assert 적힌것["outcome"].reject_qty_kg == Decimal(0)
    # 🔴 커밋은 마스터 것이다.
    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 0, 0)


def test_23_연체분도_조립에서_처리일로_적힌다(monkeypatch: pytest.MonkeyPatch):
    """★ `arrived_at`(예정일) 과 `inspected_at`(처리일) 이 갈리는 자리를 실제 경로에서 잰다."""
    eta = AS_OF - timedelta(days=2)
    wiring = Wiring(monkeypatch, in_transit=[_in_transit_row(eta=eta)])

    result = LogisticsInboundExecution(
        sim_run_id=SIM_RUN_ID,
        inspection_provider=ScenarioSimulatedInspectionProvider(),
    ).receive(FakeConnection(), as_of=AS_OF)

    assert result.status == "RECEIVED"
    assert wiring.call_args("create_arrived_receipt")["inbound"].expected_arrival_date == eta
    assert wiring.call_args("record_inspection")["inspected_at"].date() == AS_OF
