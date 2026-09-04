"""승인 약정이 **매입 원장에 남는가** (`purchases` · `purchase_items`).

`git grep "INSERT INTO purchases"` 가 0건이었다 — 승인이 재무 채무와 물류 입고
예정으로는 흘러갔는데 정작 매입 원장에는 아무것도 안 남았다. 이 파일이 그 자리를
잰다.

🔴 **실 DB 를 부르지 않는다.** 가짜 커넥션으로 *"어떤 SQL 이 어떤 순서로 나갔나"* 만
   본다. 진짜 INSERT 는 머지 뒤에 사람이 돌린다.

★ 재는 것은 넷이다.

  ```text
  쓰는가      회차 하나면 header 한 행 · 품목 한 줄
  순서        원장이 재무보다 먼저 (payables.purchase_id 가 FK 다)
  안 쓰는가   회차가 둘이면 · 지급일이 없으면 — 커넥션도 안 연다
  어휘        item_id 는 items 표에서 조회한다 — 하드코딩 맵이 아니다
  ```
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.master import ledger, transition
from app.master.commitment import ApprovedCommitment, ArrivalLeg, build_commitment

AS_OF = date(2025, 12, 31)


@pytest.fixture(autouse=True)
def 전이_등록소를_비운다() -> Iterator[None]:
    transition.reset()
    try:
        yield
    finally:
        transition.reset()


def _leg(
    *,
    seq: int = 1,
    qty_kg: float = 3587.0,
    purchase_date: date = AS_OF,
    payment_due_date: date | None = AS_OF,
) -> ArrivalLeg:
    return ArrivalLeg(
        item="배추",
        qty_kg=qty_kg,
        arrival_date=purchase_date + timedelta(days=2),
        purchase_date=purchase_date,
        seq=seq,
        payment_due_date=payment_due_date,
    )


def _commitment(*, legs: tuple[ArrivalLeg, ...] | None = None) -> ApprovedCommitment:
    #: 🟢 실측 예다 — 3,587kg × 854원 = 3,063,298원. 자릿수가 정확히 떨어진다.
    if legs is None:
        legs = (_leg(),)
    return ApprovedCommitment(
        approval_id="H1-REQ-1-1",
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario_label="보수",
        total_qty_kg=sum(leg.qty_kg for leg in legs) if legs else 3587.0,
        total_amount_krw=3063298.0,
        arrival_schedule=legs,
        inbound_lead_days=2.0,
    )


class 가짜커서:
    """나간 SQL 과 파라미터를 그대로 들고 있는다.

    ★ `items` 조회에만 답한다. `item_id` 를 못 찾는 날을 재려면 `item_row=None` 을 준다.
    """

    def __init__(self, log: list[tuple[str, Any]], *, item_row: dict[str, str] | None) -> None:
        self.log = log
        self.item_row = item_row
        self.rowcount = 1
        self._row: dict[str, str] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self.log.append((text, list(params or [])))
        self._row = self.item_row if ("FROM" in text and "items" in text) else None

    def fetchone(self) -> dict[str, str] | None:
        return self._row


#: 조회하면 나오는 행. 테스트가 `item_row=None` 을 **명시로** 줄 수 있게 기본값을 뗀다.
_FOUND = {"item_id": "ITEM-BAECHU"}


class 가짜커넥션:
    def __init__(self, *, item_row: dict[str, str] | None = _FOUND) -> None:
        self.log: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        #: `None` 을 명시로 주면 *"items 표에 없다"* 를 잰다 — 기본값과 갈라 둔다.
        self.item_row = item_row

    def cursor(self) -> 가짜커서:
        return 가짜커서(self.log, item_row=self.item_row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class 가짜전이:
    """재무·물류 자리 대역. **자기 이름을 로그에 남긴다** — 순서를 재려는 것이다."""

    def __init__(self, name: str, log: list[tuple[str, Any]]) -> None:
        self.name = name
        self.log = log

    def build(
        self,
        commitment: ApprovedCommitment,
        *,
        target_state_date: date,
        purchase_ids: Mapping[int, str] | None = None,
    ) -> Any:
        return f"{self.name}-row"

    def persist(self, conn: Any, rows: Any) -> None:
        self.log.append((f"{self.name}.persist", None))


def _params_of(conn: 가짜커넥션, 표: str) -> list[Any]:
    """그 표로 나간 `INSERT` 의 파라미터."""
    return next(params for text, params in conn.log if "INSERT INTO" in text and 표 in text)


def _rows_of(commitment: ApprovedCommitment) -> tuple[ledger.PurchaseWrite, ...]:
    purchase_ids = {
        leg.seq: transition.purchase_id_for(commitment, leg.seq)
        for leg in commitment.arrival_schedule
    }
    return ledger.build_purchase_rows(commitment, purchase_ids=purchase_ids)


# ── ① 회차 하나면 header 한 행 · 품목 한 줄 ─────────────────────────────


def test_회차_하나가_purchases_한_행과_purchase_items_한_줄이_된다() -> None:
    conn = 가짜커넥션()

    written = ledger.persist_purchases(conn, _rows_of(_commitment()))

    assert written == {"purchases": 1, "purchase_items": 1}
    나간_SQL = [text for text, _ in conn.log]
    assert sum("INSERT INTO" in t and "purchase_items" in t for t in 나간_SQL) == 1
    assert sum("INSERT INTO" in t and ".purchases" in t for t in 나간_SQL) == 1


def test_채우는_값이_설계대로다() -> None:
    """🔴 **지어낸 칸이 없다.** 공급처·시장·등급·근거는 승인이 모르는 사실이라 NULL 이다."""
    row = _rows_of(_commitment())[0]

    assert row.purchase_id == "PUR-REQ-1-D1-S1"
    assert row.purchase_date == AS_OF
    assert row.payment_due_date == AS_OF
    assert row.proposal_id == "PROP-REQ-1"
    assert row.scenario_id == "SCN-REQ-1-보수"
    assert row.sim_run_id == ledger.BURN_IN_SIM_RUN_ID
    # 🟢 3,587kg × 854원 = 3,063,298원 — 실측 예가 정확히 떨어진다.
    assert row.unit_price_krw_per_kg == Decimal("854.000000")
    assert row.line_amount_krw == Decimal("3063298.000000")

    conn = 가짜커넥션()
    ledger.persist_purchases(conn, (row,))
    header = _params_of(conn, ".purchases")
    assert ledger.MASTER_PURCHASE_TYPE in header, "purchase_type 이 MASTER_APPROVAL 이어야 한다"
    assert "OPEN" in header, "settlement_status 는 OPEN 이다"


def test_Line_금액이_DB_CHECK_를_지킨다() -> None:
    """⚠️ `|line_amount - quantity × unit_price| < 0.1` 이 DB CHECK 다.

    ★ 안 떨어지는 날에는 **맞춰 넣지 않고 멈춘다** — 총액을 곱셈 결과로 바꾸면
      원장 총액이 승인 총액과 갈린다.
    """
    row = _rows_of(_commitment())[0]

    drift = abs(row.line_amount_krw - row.quantity_kg * row.unit_price_krw_per_kg)
    assert drift < Decimal("0.1")


# ── ② 원장이 재무보다 먼저다 ────────────────────────────────────────────


def test_원장이_재무_persist_보다_먼저_불린다() -> None:
    """🔴 `payables.purchase_id` 가 `purchases` 를 참조하는 FK 다 — 부모가 먼저다."""
    log: list[tuple[str, Any]] = []
    transition.register_transition("finance", 가짜전이("finance", log))
    transition.register_transition("logistics", 가짜전이("logistics", log))
    conn = 가짜커넥션()

    def _connect() -> 가짜커넥션:
        conn.log = log  # 원장 SQL 과 부서 persist 를 **한 줄에** 세운다
        return conn

    out = transition.apply_approval(_commitment(), connect=_connect)

    assert out.status == "APPLIED"
    순서 = [
        "ledger" if isinstance(name, str) and "INSERT INTO" in name else name
        for name, _ in log
    ]
    assert 순서.index("ledger") < 순서.index("finance.persist"), (
        "매입 원장이 재무보다 뒤에 가면 payables 가 FK 에서 터진다"
    )
    assert conn.commits == 1, "커밋은 여전히 한 번이다"


# ── ③ 회차가 둘이면 쓰지 않는다 ─────────────────────────────────────────


def test_회차가_둘이면_NOT_APPLIED_이고_커넥션을_안_연다() -> None:
    """★ 재무 `_single_leg` 이 이미 같은 이유로 막는다. **같은 사실을 두 곳이 다르게
    판정하지 않게** 마스터가 앞에서 같은 사유로 멈춘다.
    """
    log: list[tuple[str, Any]] = []
    transition.register_transition("finance", 가짜전이("finance", log))
    transition.register_transition("logistics", 가짜전이("logistics", log))
    calls: list[int] = []

    두회차 = _commitment(
        legs=(
            _leg(seq=1, qty_kg=2000.0),
            _leg(seq=2, qty_kg=1587.0, purchase_date=AS_OF + timedelta(days=3)),
        )
    )

    def _connect() -> 가짜커넥션:
        calls.append(1)
        return 가짜커넥션()

    out = transition.apply_approval(두회차, connect=_connect)

    assert out.status == "NOT_APPLIED"
    assert "회차가 둘 이상" in out.reason
    assert calls == [], "쓸 수 없는데 커넥션을 열었다"
    assert log == [], "쓸 수 없는데 부서를 불렀다"


# ── ④ 지급일이 없으면 쓰지 않는다 ───────────────────────────────────────


def test_지급일이_없으면_NOT_APPLIED_다() -> None:
    """★ **없는 날짜를 지어내지 않는다.** `purchases.payment_due_date` 는 NOT NULL 이다."""
    transition.register_transition("finance", 가짜전이("finance", []))
    transition.register_transition("logistics", 가짜전이("logistics", []))
    calls: list[int] = []

    def _connect() -> 가짜커넥션:
        calls.append(1)
        return 가짜커넥션()

    out = transition.apply_approval(
        _commitment(legs=(_leg(payment_due_date=None),)), connect=_connect
    )

    assert out.status == "NOT_APPLIED"
    assert "purchase_payment_days" in out.reason
    assert calls == []


def test_지급일이_없으면_원장_계산_자체가_멈춘다() -> None:
    """★ 전이 앞단을 지나쳐 들어와도 원장이 다시 막는다 — 0 으로 대체하지 않는다."""
    with pytest.raises(ledger.PurchaseLedgerNotWritable, match="purchase_payment_days"):
        _rows_of(_commitment(legs=(_leg(payment_due_date=None),)))


# ── ⑤ item_id 는 items 표가 주인이다 ────────────────────────────────────


def test_item_id_를_items_표에서_조회한다() -> None:
    """🔴 **하드코딩 맵을 만들지 않는다.** 맵이 또 하나의 어휘가 되어 표와 갈린다."""
    conn = 가짜커넥션()

    ledger.persist_purchases(conn, _rows_of(_commitment()))

    조회 = [
        (text, params) for text, params in conn.log if "FROM" in text and "items" in text
    ]
    assert len(조회) == 1, "품목마다 items 표를 한 번 읽어야 한다"
    assert "item_name" in 조회[0][0], "한글 품목명으로 찾는다"
    assert 조회[0][1] == ["배추"]

    line = _params_of(conn, "purchase_items")
    assert line[0] == "PITEM-REQ-1-D1-S1-BAECHU", "접미사는 ITEM- 을 뗀 나머지다"
    assert line[2] == "ITEM-BAECHU", "한글이 아니라 items 표의 item_id 가 들어간다"


def test_품목을_못_찾으면_멈춘다() -> None:
    """★ 물류가 오늘 같은 자리를 고쳤다 — *"매칭 0건인데 에러가 안 납니다."*"""
    conn = 가짜커넥션(item_row=None)

    with pytest.raises(ledger.PurchaseLedgerNotWritable, match="배추"):
        ledger.persist_purchases(conn, _rows_of(_commitment()))


# ── ⑥ N5 가 지급일을 만든다 ─────────────────────────────────────────────


def _scenario() -> dict[str, Any]:
    return {
        "label": "보수",
        "total_qty_kg": 44.0,
        "total_amount_krw": 228800.0,
        "split_plan": [{"seq": 1, "date": "2025-12-31", "qty_kg": 44.0}],
    }


def test_N5_로_지급일을_만든다() -> None:
    """★ `arrival_date` 와 같은 모양이다 — 부서가 값을 주고 마스터가 옮긴다."""
    commitment = build_commitment(
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario=_scenario(),
        inbound_lead_days=2.0,
        decision_seq=1,
        purchase_payment_days=7,
    )

    leg = commitment.arrival_schedule[0]
    assert leg.payment_due_date == AS_OF + timedelta(days=7)
    assert leg.purchase_date == AS_OF, "기준은 매입일이지 승인일이 아니다"


def test_N5_가_없으면_지급일도_없다() -> None:
    """🔴 **0 으로 대체하지 않는다.** 0 이면 *"오늘 승인분이 오늘 지급"* 이 되어
    지급일이라는 사실이 사라진다 — N4 를 0 으로 못 쓰게 한 것과 같은 이유다.
    """
    commitment = build_commitment(
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario=_scenario(),
        inbound_lead_days=2.0,
        decision_seq=1,
    )

    assert commitment.arrival_schedule[0].payment_due_date is None
    assert commitment.arrival_schedule, "N5 가 없다고 입고 일정까지 버리지 않는다"


def test_N5_가_일수로_안_읽히면_일정을_만들지_않는다() -> None:
    """⚠️ `arrival_date` 와 같은 태도다 — 자르지 않고 사유를 남긴다."""
    commitment = build_commitment(
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario=_scenario(),
        inbound_lead_days=2.0,
        decision_seq=1,
        purchase_payment_days=-1,
    )

    assert commitment.arrival_schedule == ()
    assert any("purchase_payment_days" in note for note in commitment.notes)


# ── ⑦ 분담이 문자로 잠긴다 ──────────────────────────────────────────────


def test_SQL_은_ledger_에_있고_transition_에는_없다() -> None:
    """🔴 전이 경계에 `INSERT` 가 들어오면 마스터가 남의 칸 이름을 알게 된다.

    ★ `test_전이_모듈에_SQL_이_없다` 가 한쪽을 잠근다. 여기서는 **반대쪽**을 잰다 —
      원장에 SQL 이 없으면 분담을 지킨 것이 아니라 아무 데도 안 쓴 것이다.
    """
    전이 = Path(transition.__file__).read_text(encoding="utf-8")
    원장 = Path(ledger.__file__).read_text(encoding="utf-8")

    assert "INSERT INTO" not in 전이
    assert "INSERT INTO {}.purchases" in 원장
    assert "INSERT INTO {}.purchase_items" in 원장
