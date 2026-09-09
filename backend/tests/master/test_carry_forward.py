"""승인이 **이미 열린 다음 날들**에도 실린다.

🔴 **실측 2026-09-07** — `#343` 뒤에 새 승인을 냈는데 도착일에 입고가 안 됐다.

```text
2026-01-14   in_transit 2건   ← 전이가 여기 들어갔다 (target_state_date = 승인일 + 1)
2026-01-15   in_transit 1건   ← **도착일인데 새 것이 없다**
```

`in_transit` 은 **승인 ~ 도착 ~ 검수까지 여러 날에 걸쳐 유지되는 상태**이고(물류
`day_open.py`), 물류는 그것을 **하루 넘김의 carry-forward** 로 유지한다. 그런데 그
방식은 *"다음 날을 만들 때 전날에서 물려받는다"* 라서, **다음 날이 이미 열려 있으면
그 행은 이 승인을 모른 채 굳는다.**

★ **정방향에서는 안 생긴다** — 내일은 아직 없으니까. 다만 *"내일을 미리 열어 두고
  오늘 승인"* 은 있을 수 있는 순서이고, 아티팩트로 보고 덮으면 **그 순서가 실제로
  오는 날 도착분이 조용히 사라진다.**

---

🔴 **마스터가 물류 표를 읽지 않는다** (정의서 §3.2.5).

어느 날이 열렸는지는 `master_day_openings` 가 아는 **마스터 사실**이라 마스터가
답할 수 있다. 물류 fixture 를 뒤지지 않는다.

⚠️ **물류 코드도 안 고친다.** `InventoryTransition` 이 날짜를 행마다 들고 있고
  어댑터 `persist` 가 행마다 `persist_inventory` 를 부른다 — **묶음을 여러 개 주면
  되는 계약**이다. 그리고 `in_transit` 은 덮어쓰기가 아니라 **병합**이라 같은
  `inbound_id` 를 여러 날에 실어도 안전하다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Self

import pytest

from app.master import transition
from app.master.commitment import ApprovedCommitment, ArrivalLeg

AS_OF = date(2026, 1, 13)
다음날 = AS_OF + timedelta(days=1)


class _가짜커서:
    """`persist_purchases` 가 쓰는 최소 표면. **아무것도 안 한다.**"""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return

    def __init__(self) -> None:
        self._row: dict[str, str] | None = None
        self.rowcount = 1

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        # `items` 조회만 답한다 — `test_transition_boundary.py` 의 대역과 같은 모양이다.
        self._row = {"item_id": "ITEM-BAECHU"} if "FROM" in text and "items" in text else None

    def executemany(self, *_: Any, **__: Any) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> dict[str, str] | None:
        return self._row


class _가짜커넥션:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> _가짜커서:
        return _가짜커서()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class _전이:
    """`build` 가 받은 날짜와 **그 날 실린 회차**를 그대로 기록한다."""

    def __init__(self) -> None:
        self.dates: list[date] = []
        self.persisted: list[Any] = []
        #: 날짜 → 그 날 `build` 가 받은 회차 번호. `#381` 이 보는 자리다.
        self.실린회차: dict[date, tuple[int, ...]] = {}
        self.실린총량: dict[date, float] = {}
        self.실린총액: dict[date, float] = {}

    def build(self, commitment: Any, *, target_state_date: date, **_: Any) -> tuple[Any, ...]:
        self.dates.append(target_state_date)
        self.실린회차[target_state_date] = tuple(
            leg.seq for leg in commitment.arrival_schedule
        )
        self.실린총량[target_state_date] = commitment.total_qty_kg
        self.실린총액[target_state_date] = commitment.total_amount_krw
        return (f"row@{target_state_date}",)

    def persist(self, conn: Any, rows: Any) -> None:
        self.persisted.extend(rows if isinstance(rows, (list, tuple)) else [rows])


class _재무전이(_전이):
    def build(self, commitment: Any, *, target_state_date: date, **_: Any) -> Any:
        self.dates.append(target_state_date)
        return f"finance@{target_state_date}"

    def persist(self, conn: Any, row: Any) -> None:
        self.persisted.append(row)


def _commitment(*, 도착=2) -> ApprovedCommitment:
    """회차 하나짜리 약정. `도착` 은 승인일로부터 며칠 뒤 도착인지다."""
    return ApprovedCommitment(
        approval_id="H1-REQ-CARRY-1",
        request_id="REQ-CARRY",
        as_of=AS_OF,
        item="배추",
        scenario_label="기본",
        total_qty_kg=100.0,
        total_amount_krw=100000.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=100.0,
                arrival_date=AS_OF + timedelta(days=도착),
                purchase_date=AS_OF,
                seq=1,
                payment_due_date=AS_OF,
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    before = dict(transition.registered())
    transition.reset()
    yield
    transition.reset()
    for part, impl in before.items():
        transition.register_transition(part, impl)


@pytest.fixture
def _배선() -> tuple[_재무전이, _전이]:
    재무, 물류 = _재무전이(), _전이()
    transition.register_transition("finance", 재무)
    transition.register_transition("logistics", 물류)
    return 재무, 물류


def _열린날(*days: date):
    def _fake(*, after: date, sim_run_id: str, connect: Any = None) -> tuple[date, ...]:
        return tuple(d for d in days if d > after)

    return _fake


# ---------------------------------------------------------------------------
# 1. 앞질러 열린 날이 없으면 — **아무것도 안 달라진다**
# ---------------------------------------------------------------------------


def test_정방향이면_다음날_하나뿐이다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """★ **비어 있는 것이 정상이다.** 내일은 아직 없다."""
    _, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날())

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert out.status == "APPLIED"
    assert 물류.dates == [다음날]
    assert out.carried_forward == []
    assert out.carried_forward_status == "OK", "읽었는데 못 읽은 것으로 나갔다"


# ---------------------------------------------------------------------------
# 2. 앞질러 열려 있으면 — **그 날들에도 싣는다**
# ---------------------------------------------------------------------------


def test_이미_열린_날들에도_같은_사실을_싣는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **이것이 없으면 도착일 행이 승인을 모른 채 굳는다.**

    ★ 도착일이 **열린 날들보다 뒤**라 다섯 날 모두 「앞으로 올 도착분」이다 —
      `#381` 이 좁히는 자리와 겹치지 않는다.
    """
    _, 물류 = _배선
    나중 = [다음날 + timedelta(days=n) for n in (1, 2, 5)]
    monkeypatch.setattr(transition, "opened_days_after", _열린날(*나중))

    out = transition.apply_approval(_commitment(도착=9), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert 물류.dates == [다음날, *나중], "이미 열린 날에 안 실었다"
    assert out.carried_forward == 나중


def test_따라잡은_날을_결과에_적는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """★ **왜 하루가 여러 번 바뀌었는지**가 화면까지 가야 한다."""
    monkeypatch.setattr(transition, "opened_days_after", _열린날(다음날 + timedelta(days=1)))

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert out.carried_forward == [다음날 + timedelta(days=1)]


def test_묶음을_여러_개_주지_물류를_고치지_않는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """⚠️ 물류 `persist` 는 **행마다** `persist_inventory` 를 부른다 — 계약 그대로다."""
    _, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날(다음날 + timedelta(days=1)))

    transition.apply_approval(_commitment(), connect=_가짜커넥션)

    assert 물류.persisted == [f"row@{다음날}", f"row@{다음날 + timedelta(days=1)}"]


# ---------------------------------------------------------------------------
# 3. 재무는 안 건드린다
# ---------------------------------------------------------------------------


def test_재무는_다음날_하나만_받는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **재무 상태의 다일 의미는 재무가 정한다.**

    `in_transit` 이 여러 날에 걸쳐 유지된다는 것은 **물류가 자기 파일에 적은 사실**
    이다. 재무 상태행도 같은 성격인지는 재무 몫이라, 마스터가 대신 넓히지 않는다 —
    `inspection_provider` 를 마스터가 안 고른 것과 같은 자리다.
    """
    재무, _ = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날(다음날 + timedelta(days=1)))

    transition.apply_approval(_commitment(), connect=_가짜커넥션)

    assert 재무.dates == [다음날], "마스터가 재무 다일 의미를 대신 정했다"


# ---------------------------------------------------------------------------
# 4. 못 읽어도 승인은 선다
# ---------------------------------------------------------------------------


def test_정본을_못_읽어도_승인을_멈추지_않는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """⚠️ `record_day_opening` 이 절대 raise 하지 않는 것과 같은 규율.

    ★ 다만 그때는 낡은 행이 남고, 그 사실은 `carried_forward` 가 **비어 있는 것**으로
      드러난다 — 조용히 성공한 것처럼 보이지 않는다.
    """

    def _못읽음(*, after: date, sim_run_id: str, connect: Any = None) -> None:
        return None

    _, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _못읽음)

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert out.status == "APPLIED"
    assert 물류.dates == [다음날]
    assert out.carried_forward == []
    assert out.carried_forward_status == "UNREADABLE", (
        "못 읽은 것이 '앞질러 열린 날이 없었다' 와 같은 문장으로 나갔다"
    )


# ---------------------------------------------------------------------------
# 5. 🔴 **도착일이 지난 날에는 안 싣는다** (`#381`)
#
#    `confirmed_inbound` 의 뜻은 *"D 시점에 아직 안 온, 앞으로 올 도착분"* 이다.
#    도착일이 `D` 보다 이르면 이미 왔거나(로트가 됐거나) 안 온 사고이고, 둘 다
#    「앞으로 올 도착분」이 아니다.
#
#    ⚠️ DB 실측 2026-09-08 — 도착일 `01-22`, carry-forward 가 연 날
#      `{01-21, 01-22, 01-23, 01-27, 01-28}`. 입고 처리는 `01-22` **한 행만** 걷고
#      (`_clear_schedule` 의 `WHERE ... as_of=%s`), 나머지 셋은 아무도 안 걷는
#      **유령 확정입고**로 남아 `cap_by_date` 를 0 으로 만들었다.
# ---------------------------------------------------------------------------


def test_도착일이_지난_날에는_그_회차를_안_싣는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **실측 모양 그대로다.** 도착일 하루 뒤·닷새 뒤·엿새 뒤가 열려 있다."""
    _, 물류 = _배선
    도착일 = 다음날 + timedelta(days=1)
    지난뒤 = [도착일 + timedelta(days=n) for n in (1, 5, 6)]
    monkeypatch.setattr(transition, "opened_days_after", _열린날(도착일, *지난뒤))

    out = transition.apply_approval(_commitment(도착=2), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert 물류.dates == [다음날, 도착일], f"도착일이 지난 날에도 실었다: {물류.dates}"
    assert all(d not in 물류.실린회차 for d in 지난뒤), "유령 확정입고가 또 실렸다"


def test_실을_회차가_없으면_그_날은_build_를_안_부른다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """★ 빈 묶음을 넘겨 물류가 *"오늘 도착 예정 0"* 을 쓰게 하는 것과 다르다."""
    _, 물류 = _배선
    도착일 = 다음날 + timedelta(days=1)
    monkeypatch.setattr(
        transition,
        "opened_days_after",
        _열린날(*[도착일 + timedelta(days=n) for n in (1, 2, 3)]),
    )

    out = transition.apply_approval(_commitment(도착=2), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert 물류.dates == [다음날], f"실을 회차가 없는 날에 build 를 불렀다: {물류.dates}"


def test_carried_forward_는_열린_날이_아니라_실제로_쓴_날이다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 화면이 *"따라잡았다"* 고 말하는 날과 행이 실제로 선 날이 갈리면 안 된다."""
    도착일 = 다음날 + timedelta(days=1)
    지난뒤 = [도착일 + timedelta(days=n) for n in (1, 5, 6)]
    monkeypatch.setattr(transition, "opened_days_after", _열린날(도착일, *지난뒤))

    out = transition.apply_approval(_commitment(도착=2), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert out.carried_forward == [도착일], (
        f"안 쓴 날이 따라잡은 날로 나갔다: {out.carried_forward}"
    )
    assert out.carried_forward_status == "OK"


# ---------------------------------------------------------------------------
# 6. 🔴 **다회차는 날마다 상한이 다르다** (`#397` 로 다회차가 열렸다)
# ---------------------------------------------------------------------------


def _두회차() -> ApprovedCommitment:
    """1회차 `AS_OF+2` 도착 40kg · 2회차 `AS_OF+5` 도착 60kg."""
    return ApprovedCommitment(
        approval_id="H1-REQ-CARRY-1",
        request_id="REQ-CARRY",
        as_of=AS_OF,
        item="배추",
        scenario_label="분할",
        total_qty_kg=100.0,
        total_amount_krw=100000.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=40.0,
                arrival_date=AS_OF + timedelta(days=2),
                purchase_date=AS_OF,
                seq=1,
                amount_krw=40000.0,
                payment_due_date=AS_OF,
            ),
            ArrivalLeg(
                item="배추",
                qty_kg=60.0,
                arrival_date=AS_OF + timedelta(days=5),
                purchase_date=AS_OF,
                seq=2,
                amount_krw=60000.0,
                payment_due_date=AS_OF,
            ),
        ),
    )


def test_1회차_도착_뒤_2회차_도착_전_날에는_2회차만_실린다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **날마다 상한이 다르다.** 1회차는 이미 왔고 2회차만 앞으로 올 도착분이다."""
    _, 물류 = _배선
    첫도착, 둘도착 = AS_OF + timedelta(days=2), AS_OF + timedelta(days=5)
    사이 = AS_OF + timedelta(days=3)
    둘_지난뒤 = AS_OF + timedelta(days=7)
    monkeypatch.setattr(
        transition, "opened_days_after", _열린날(첫도착, 사이, 둘도착, 둘_지난뒤)
    )

    out = transition.apply_approval(_두회차(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert 물류.실린회차[다음날] == (1, 2), "승인 다음 날에는 두 회차가 다 앞으로 올 도착분이다"
    assert 물류.실린회차[첫도착] == (1, 2), "도착일 당일은 아직 안 온 것으로 센다"
    assert 물류.실린회차[사이] == (2,), f"1회차가 도착 뒤에도 실렸다: {물류.실린회차[사이]}"
    assert 물류.실린회차[둘도착] == (2,)
    assert 둘_지난뒤 not in 물류.실린회차, "두 회차가 다 지난 날에도 실었다"
    assert out.carried_forward == [첫도착, 사이, 둘도착]


def test_좁힌_사본은_남긴_회차의_합으로_선다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """★ 사본도 `__post_init__` 검증을 지나야 한다 — 총량·총액이 남긴 회차와 맞는다."""
    _, 물류 = _배선
    사이 = AS_OF + timedelta(days=3)
    monkeypatch.setattr(transition, "opened_days_after", _열린날(사이))

    out = transition.apply_approval(_두회차(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert 물류.실린총량[다음날] == 100.0
    assert 물류.실린총량[사이] == 60.0, "좁혔는데 총량은 원래 값 그대로였다"
    assert 물류.실린총액[사이] == 60000.0, "좁혔는데 총액은 원래 값 그대로였다"


def test_회차_금액이_비면_총액을_지어내지_않는다() -> None:
    """🔴 하나라도 `None` 이면 **검증이 금액을 안 본다** — 그때 총액을 건드리면 창작이다.

    ⚠️ 이 모양은 `apply_approval` 로는 안 온다 — 회차가 둘 이상인데 금액이 비면
      `ledger_block_reason` 이 먼저 `NOT_APPLIED` 로 막는다. 그래서 좁히는 함수를
      **직접** 부른다. 막는 조건이 언젠가 느슨해져도 여기서 총액을 지어내지 않는다.
    """
    사이 = AS_OF + timedelta(days=3)
    금액없음 = ApprovedCommitment(
        approval_id="H1-REQ-CARRY-1",
        request_id="REQ-CARRY",
        as_of=AS_OF,
        item="배추",
        scenario_label="분할",
        total_qty_kg=100.0,
        total_amount_krw=100000.0,
        arrival_schedule=tuple(
            ArrivalLeg(
                item="배추",
                qty_kg=qty,
                arrival_date=AS_OF + timedelta(days=days),
                purchase_date=AS_OF,
                seq=seq,
                payment_due_date=AS_OF,
            )
            for seq, qty, days in ((1, 40.0, 2), (2, 60.0, 5))
        ),
    )

    좁힌것 = transition._still_incoming_on(금액없음, 사이)

    assert 좁힌것 is not None
    assert tuple(leg.seq for leg in 좁힌것.arrival_schedule) == (2,)
    assert 좁힌것.total_qty_kg == 60.0
    assert 좁힌것.total_amount_krw == 100000.0, "금액이 없는데 총액을 다시 만들었다"


# ---------------------------------------------------------------------------
# 7. 🔴 **리드타임 0 은 미정 상태다 — 조용히 지나가지 않는다**
#
#    `inbound_lead_days` 는 계약상 `ge=0` 이라 **0 이 허용되는 값**이다
#    (`app/logistics/schemas.py:283`). 그런데 0 이면 `arrival_date == as_of` 이고
#    목표 상태일은 그 **다음 날**이라 `_still_incoming_on` 이 `None` 을 돌려준다 —
#    물류 `build` 를 한 번도 안 부르고 `persist(conn, ())` 로 아무것도 안 쓴다.
#
#    ⚠️ **그런데 `purchases` 와 재무 행은 써지고 `APPLIED` 가 나갔다.** 물류만
#      조용히 빠진다. 이 변경 전에도 조용했다 — 그때는 유령이 될 행을 조용히 **썼고**
#      지금은 조용히 **안 쓴다. 둘 다 조용한 것이 문제다.**
#
#    ★ 그래서 *"틀렸다"* 로 단정하지 않고 **아무도 안 정했다는 사실을 드러낸다.**
# ---------------------------------------------------------------------------


def _리드타임0() -> ApprovedCommitment:
    """리드타임 0 — 도착일이 승인일 당일이다. 계약상 허용되는 값이다."""
    return ApprovedCommitment(
        approval_id="H1-REQ-CARRY-1",
        request_id="REQ-CARRY",
        as_of=AS_OF,
        item="배추",
        scenario_label="기본",
        total_qty_kg=100.0,
        total_amount_krw=100000.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=100.0,
                arrival_date=AS_OF,
                purchase_date=AS_OF,
                seq=1,
                payment_due_date=AS_OF,
            ),
        ),
        inbound_lead_days=0.0,
    )


def test_리드타임0이면_NOT_APPLIED_이고_커넥션을_안_연다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **`purchases` 도 재무 행도 안 써야 한다** — `_ledger_blocked` 와 같은 자리다."""
    재무, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날())
    열린횟수: list[int] = []

    def _connect() -> _가짜커넥션:
        열린횟수.append(1)
        return _가짜커넥션()

    out = transition.apply_approval(_리드타임0(), connect=_connect)

    assert out.status == "NOT_APPLIED", f"물류만 빠진 채 {out.status} 가 나갔다"
    assert 열린횟수 == [], "쓸 수 없는데 커넥션을 열었다"
    assert 재무.persisted == [] and 물류.persisted == [], "물류만 빠진 채 다른 파트를 썼다"
    assert 물류.dates == [] and 재무.dates == [], "막았는데 build 를 불렀다"


def test_사유가_도착일과_목표_상태일을_숫자로_적는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """★ *"도착일이 목표 상태일보다 이르다"* 를 사람이 바로 알아보게 적는다."""
    monkeypatch.setattr(transition, "opened_days_after", _열린날())

    out = transition.apply_approval(_리드타임0(), connect=_가짜커넥션)

    assert out.status == "NOT_APPLIED"
    assert AS_OF.isoformat() in out.reason, "회차 도착일이 사유에 없다"
    assert 다음날.isoformat() in out.reason, "목표 상태일이 사유에 없다"
    assert "1회차" in out.reason, "어느 회차인지 이름을 안 불렀다"
    # 🔴 *"틀렸다"* 가 아니라 **아무도 안 정했다** 는 사실을 적어야 한다.
    assert "정해진 적이 없다" in out.reason
    assert "물류·매입" in out.reason, "정할 자리를 안 가리켰다"


def test_도착일이_목표_상태일과_같으면_지나간다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **회귀 방어.** 리드타임 1 이상은 지금 그대로다 — 새 가드는 거기 안 건다.

    ★ 경계는 `arrival_date == target_state_date` 다. 도착일 당일은 아직 안 온 것으로
      세므로(`_still_incoming_on` 의 `>=`) 여기서 막으면 정상 승인이 다 막힌다.
    """
    _, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날())

    out = transition.apply_approval(_commitment(도착=1), connect=_가짜커넥션)

    assert out.status == "APPLIED", out.reason
    assert 물류.dates == [다음날], f"경계 승인이 안 실렸다: {물류.dates}"
    assert 물류.실린회차[다음날] == (1,)
