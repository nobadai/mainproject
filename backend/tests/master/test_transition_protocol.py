"""전이 Protocol 이 **실제 구현과 같은 모양인가** (C 형태 ⑦).

`test_transition_boundary.py` 는 *"언제 커넥션을 열고 언제 커밋하는가"* 를 잰다.
이 파일이 재는 것은 그 앞 — **무엇을 넘겨주는가**다.

🔴 **두 Protocol 이 서로 달랐던 것은 마스터 잘못이다.** `#238` 에서 재무는
   `build(commitment, as_of)`, 물류는 `build(commitment)` 로 근거 없이 다르게 적혔고,
   둘 다 실제 구현과도 어긋나 있었다. 날짜를 `build` 가 못 받으니 물류에서는 그 값이
   `persist` 로 밀려났다 (물류 지적).

★ **DB 를 부르지 않는다.** 가짜 재무·물류 전이로 인자만 잰다 — 지금 등록된 실제
  구현이 0건이라, 여기서 안 재면 규약이 다시 어긋나도 아무도 말해 주지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from app.master import transition
from app.master.commitment import ApprovedCommitment, ArrivalLeg

#: 🔴 **금요일이다.** 달력 다음 날은 토요일이고, 실행일 달력이라면 월요일이다.
#:   이 하나가 두 규칙을 갈라 준다.
FRIDAY = date(2026, 1, 2)


@pytest.fixture(autouse=True)
def 전이_등록소를_비운다() -> Iterator[None]:
    """등록소는 프로세스 전역이다 — **앞뒤로 비운다.**"""
    transition.reset()
    try:
        yield
    finally:
        transition.reset()


def _commitment(
    *,
    as_of: date = FRIDAY,
    approval_id: str = "H1-REQ-7-2",
    request_id: str = "REQ-7",
    legs: tuple[ArrivalLeg, ...] | None = None,
) -> ApprovedCommitment:
    if legs is None:
        legs = (
            ArrivalLeg(
                item="배추",
                qty_kg=44.0,
                arrival_date=as_of + timedelta(days=2),
                purchase_date=as_of,
                seq=1,
                # ★ 지급일이 없으면 원장을 못 써 전이가 `NOT_APPLIED` 로 돌아선다 —
                #   그 판정은 `test_purchase_ledger.py` 가 잰다. 여기서는 규약 모양을
                #   재는 것이 목적이라 **쓸 수 있는 약정**을 기본으로 둔다.
                payment_due_date=as_of,
            ),
        )
    return ApprovedCommitment(
        approval_id=approval_id,
        request_id=request_id,
        as_of=as_of,
        item="배추",
        scenario_label="보수",
        total_qty_kg=sum(leg.qty_kg for leg in legs) if legs else 44.0,
        total_amount_krw=228800.0,
        arrival_schedule=legs,
        inbound_lead_days=2.0,
    )


def _두회차(as_of: date = FRIDAY) -> ApprovedCommitment:
    return _commitment(
        as_of=as_of,
        legs=(
            ArrivalLeg(
                item="배추",
                qty_kg=20.0,
                arrival_date=as_of + timedelta(days=2),
                purchase_date=as_of,
                seq=1,
                payment_due_date=as_of,
            ),
            ArrivalLeg(
                item="배추",
                qty_kg=24.0,
                arrival_date=as_of + timedelta(days=5),
                purchase_date=as_of + timedelta(days=3),
                seq=2,
                payment_due_date=as_of + timedelta(days=3),
            ),
        ),
    )


class 가짜커서:
    """`items` 조회만 답하고 나머지 SQL 은 센다. **진짜 DB 를 부르지 않는다.**"""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.rowcount = 1
        self._row: dict[str, str] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self.executed.append(text)
        self._row = {"item_id": "ITEM-BAECHU"} if "FROM" in text and "items" in text else None

    def fetchone(self) -> dict[str, str] | None:
        return self._row


class 가짜커넥션:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.cursors: list[가짜커서] = []

    def cursor(self) -> 가짜커서:
        cur = 가짜커서()
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class 가짜재무:
    """재무 자리 대역. **인자를 키워드로만 받는다.**

    ★ 기본값을 두지 않는다 — 마스터가 안 넘기면 `TypeError` 로 즉시 걸려야 한다.
      기본값을 두면 규약이 다시 어긋나도 초록불이 나온다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[date, Mapping[int, str]]] = []

    def build(
        self,
        commitment: ApprovedCommitment,
        *,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
    ) -> Any:
        self.calls.append((target_state_date, dict(purchase_ids)))
        return "finance-row"

    def persist(self, conn: Any, row: Any) -> None:
        return None


class 가짜물류:
    """물류 자리 대역. **`purchase_ids` 를 안 받는다.**

    ★ 안 받는 것도 규약이다. 마스터가 물류에도 넘기면 여기서 `TypeError` 가 난다.
    """

    def __init__(self) -> None:
        self.calls: list[date] = []

    def build(
        self,
        commitment: ApprovedCommitment,
        *,
        target_state_date: date,
    ) -> list[Any]:
        self.calls.append(target_state_date)
        return ["logistics-row"]

    def persist(self, conn: Any, rows: Any) -> None:
        return None


def _등록한다() -> tuple[가짜재무, 가짜물류]:
    finance, logistics = 가짜재무(), 가짜물류()
    transition.register_transition("finance", finance)
    transition.register_transition("logistics", logistics)
    return finance, logistics


# ── a. 두 파트가 같은 모양으로 불린다 ───────────────────────────────────


def test_재무_build_는_두_값을_키워드로_받는다() -> None:
    """★ `target_state_date` 와 `purchase_ids` 둘 다 **키워드**다."""
    finance, _ = _등록한다()

    out = transition.apply_approval(_commitment(), connect=lambda: 가짜커넥션())

    assert out.status == "APPLIED"
    assert len(finance.calls) == 1
    target, purchase_ids = finance.calls[0]
    assert isinstance(target, date)
    assert isinstance(purchase_ids, Mapping)


def test_물류_build_는_날짜를_키워드로_받는다() -> None:
    """🔴 전에는 물류 `build` 가 날짜를 **아예 못 받았다** — 그래서 `persist` 가 대신
    받았고, 그 자리에서 규약이 실제와 갈렸다."""
    _, logistics = _등록한다()

    transition.apply_approval(_commitment(), connect=lambda: 가짜커넥션())

    assert len(logistics.calls) == 1
    assert isinstance(logistics.calls[0], date)


def test_두_파트가_같은_날짜를_받는다() -> None:
    """★ 같은 승인분인데 재무와 물류가 다른 날을 딛으면 두 장부가 갈린다."""
    finance, logistics = _등록한다()

    transition.apply_approval(_commitment(), connect=lambda: 가짜커넥션())

    assert finance.calls[0][0] == logistics.calls[0]


# ── b. 달력 다음 날이다 — 주말을 건너뛰지 않는다 ────────────────────────


def test_상태가_설_날은_승인_다음_달력일이다() -> None:
    """🔴 **금요일 승인이면 토요일이다.** 실행일 달력이면 월요일이 나온다.

    ★ 주말에도 판매 시나리오로 물류·재무가 움직인다 — 장부는 날마다 흐른다.
      `#240` 의 *"실행일은 평일만, 경과일수는 달력일"* 과 같은 결이다.
    """
    finance, logistics = _등록한다()
    assert FRIDAY.weekday() == 4, "고정값이 금요일이 아니면 이 검사가 아무것도 안 잰다"

    transition.apply_approval(_commitment(as_of=FRIDAY), connect=lambda: 가짜커넥션())

    토요일 = date(2026, 1, 3)
    assert 토요일.weekday() == 5
    assert finance.calls[0][0] == 토요일, "주말을 건너뛰었다 — 실행일 달력을 쓴 것이다"
    assert logistics.calls[0] == 토요일


def test_평일_승인도_그냥_다음_날이다() -> None:
    """★ 주말만 특별하게 다루는 것이 아니다 — 언제나 달력 +1 일이다."""
    finance, _ = _등록한다()
    수요일 = date(2025, 12, 31)

    transition.apply_approval(_commitment(as_of=수요일), connect=lambda: 가짜커넥션())

    assert finance.calls[0][0] == date(2026, 1, 1)


def test_전이_모듈이_실행일_달력을_부르지_않는다() -> None:
    """🔴 **원문을 읽어 잠근다.** 달력 다음 날 규칙은 값 하나만 보면 우연히 맞을 수
    있다 — 실행일 달력을 부르는 코드가 파일에 아예 없어야 한다.

    ★ import 로는 안 잡힌다. 부르는 자리가 한 줄 들어와도 다른 검사는 조용하다.
    """
    source = Path(transition.__file__).read_text(encoding="utf-8")

    assert "next_execution_day" not in source, (
        "마스터 전이가 실행일 달력을 쓰고 있다 — 상태가 설 날은 달력 다음 날이다"
    )


# ── c. purchase_id 는 결정론이다 ────────────────────────────────────────


def test_purchase_id_형식() -> None:
    """```text
    PUR-{request_id}-D{decision_seq}-S{seq}
    ```"""
    commitment = _commitment(approval_id="H1-REQ-7-2", request_id="REQ-7")

    assert transition.purchase_id_for(commitment, 1) == "PUR-REQ-7-D2-S1"
    assert transition.purchase_id_for(commitment, 3) == "PUR-REQ-7-D2-S3"


def test_같은_약정이면_두_번_불러도_같은_id_다() -> None:
    """★ ★ **결정론이다.** 난수나 순번 카운터를 쓰면 두 번째 반영이 UPSERT 로
    겹쳐 쓰이지 않고 **행을 하나 더 만든다.** 물류 `inbound_id` 가
    `INB-{approval_id}-{seq}` 인 것이 같은 이유다.
    """
    첫번 = [transition.purchase_id_for(_commitment(), seq) for seq in (1, 2)]
    두번 = [transition.purchase_id_for(_commitment(), seq) for seq in (1, 2)]

    assert 첫번 == 두번
    assert len(set(첫번)) == 2, "회차가 다르면 id 도 달라야 한다"


def test_approval_id_형식이_어긋나면_예외다() -> None:
    """🔴 **조용히 넘기지 않는다.** `decision_seq` 를 0 이나 1 로 대신 채우면 서로
    다른 결정이 같은 `purchase_id` 를 갖고, UPSERT 가 앞선 매입을 덮어쓴다.
    """
    깨진 = _commitment(approval_id="REQ-7-2", request_id="REQ-7")  # H1- 접두사 없음

    with pytest.raises(ValueError, match="approval_id"):
        transition.purchase_id_for(깨진, 1)


def test_회차가_숫자가_아니면_예외다() -> None:
    """★ 접두사만 맞고 뒤가 숫자가 아닌 경우도 같은 자리다."""
    깨진 = _commitment(approval_id="H1-REQ-7-final", request_id="REQ-7")

    with pytest.raises(ValueError, match="approval_id"):
        transition.purchase_id_for(깨진, 1)


def test_purchase_item_id_형식() -> None:
    """★ `PUR-` 를 떼고 `PITEM-` 을 붙인다 — 접두사가 겹치지 않는다."""
    purchase_id = transition.purchase_id_for(_commitment(), 1)

    item_id = transition.purchase_item_id_for(purchase_id, "배추")

    assert item_id == "PITEM-REQ-7-D2-S1-배추"
    assert item_id.startswith("PITEM-")
    assert "PUR-" not in item_id, "접두사가 둘 겹치면 번인 모양과 갈린다"


# ── d. 회차마다 purchases 한 행 ─────────────────────────────────────────


def test_회차가_둘이면_purchase_id_도_둘이다() -> None:
    """★ 회차마다 `purchases` 한 행이다. `purchases.purchase_date` 가 header 에
    하나뿐이라 매입일이 다른 회차를 한 header 에 담을 수 없다.

    ⚠️ 전에는 `apply_approval` 을 돌려 재무가 받은 매핑으로 쟀다. 이제 회차가 둘이면
      **매입 원장을 쓸 수 없어 전이가 앞에서 돌아서므로**(`test_purchase_ledger.py`)
      재무를 부르지 않는다. 재는 사실은 그대로고, 재는 자리만 ID 함수로 옮겼다.
    """
    commitment = _두회차()

    purchase_ids = {
        leg.seq: transition.purchase_id_for(commitment, leg.seq)
        for leg in commitment.arrival_schedule
    }

    assert set(purchase_ids) == {1, 2}, "seq 로 갈려 있어야 한다"
    assert purchase_ids[1] == "PUR-REQ-7-D2-S1"
    assert purchase_ids[2] == "PUR-REQ-7-D2-S2"


def test_회차가_없으면_빈_매핑이고_예외가_아니다() -> None:
    """★ 회차 일정을 못 만든 약정도 승인은 살아 있다 — 반영할 매입이 **없다**는 것은
    정상 상태다 (물류 `build_next_inventory` 가 빈 목록을 정상으로 보는 것과 같다).
    """
    finance, _ = _등록한다()

    out = transition.apply_approval(_commitment(legs=()), connect=lambda: 가짜커넥션())

    assert out.status == "APPLIED"
    assert finance.calls[0][1] == {}


# ── e. 기존 규율은 그대로다 ─────────────────────────────────────────────


def test_미등록이면_여전히_NOT_APPLIED_이고_커넥션을_안_연다() -> None:
    """★ 규약을 바꾼다고 *"아직 안 돈다"* 가 장애로 바뀌면 안 된다."""
    calls: list[int] = []

    def _connect() -> 가짜커넥션:
        calls.append(1)
        return 가짜커넥션()

    out = transition.apply_approval(_commitment(), connect=_connect)

    assert out.status == "NOT_APPLIED"
    assert out.missing == ["finance", "logistics"]
    assert calls == [], "미등록인데 커넥션을 열었다"
