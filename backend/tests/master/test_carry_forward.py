"""승인은 **목표 상태일 하루에만** 실린다. 이미 열린 다음 날들로 전파하지 않는다.

🔴 **이 파일의 뜻이 두 번 바뀌었다. 이력을 먼저 읽어라.**

```text
① 2026-09-07  "이미 열린 날들에도 싣는다"    물류가 입고 예정을 날짜별 fixture JSON
   (`#343`)                                   에 복제하던 구조였다. 다음 날이 이미 열려
                                              있으면 그 행이 승인을 모른 채 굳어서,
                                              마스터가 열린 날마다 다시 실어야 했다.

② 2026-09-10  "안 싣는다"                    inbound_schedules 1행이 정본이 되면서
   (물류 `#484` · W3-3)                       (`created_as_of <= as_of` 로 질의)
                                              복제가 없어졌다. 전파할 것이 없고,
                                              하면 같은 inbound_id 에 다른
                                              created_as_of 가 되어 물류가
                                              `ScheduleConflict` 로 멈춘다.
```

★ **파일 이름을 그대로 둔다.** carry-forward 를 **안 한다**는 것이 이제 이 파일이
  지키는 사실이다 — 누가 그 루프를 되살리려고 이 이름을 찾아왔을 때 답이 여기 있어야
  한다. 이름을 바꾸면 그 사람이 빈손으로 돌아간다.

---

🔴 **전파를 안 하는 것도 지켜야 할 사실이다.** 조용히 없어진 기능이 아니다.

```text
전파를 되살린다  →  같은 inbound_id 를 02-09 · 02-10 두 날에 싣는다
                 →  물류가 created_as_of 를 둘 가질 수 없다고 거절한다
                 →  그 승인이 FAILED 로 선다 (물류 실측 2026-09-09)
```

⚠️ **`app/master/transition.py` 의 로직은 안 되돌린다.** 이 파일은 **검사**다.

---

🔴 **회차 좁히기(`_still_incoming_on`)는 살아 있다.**

전방 전파가 없어지면서 *"날마다 그 날에 유효한 회차만 싣는다"* 를 `apply_approval`
바깥에서 볼 수 없게 됐다. 그래서 그 검사들은 **함수를 직접 부르는 문**으로 옮겼다 —
재는 사실은 그대로다. 그 함수는 `_arrival_blocked` 가 *"목표 상태일에 앞으로 올
도착분이 있나"* 를 묻는 자리에서 여전히 쓰인다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Self

import pytest

from app.master import transition
from app.master.commitment import ApprovedCommitment, ArrivalLeg

AS_OF = date(2026, 1, 13)

#: 이 검사가 쓰는 실행 축. 🔴 **운영값(`BURN_IN_SIM_RUN_ID`)을 안 쓴다** — 축을
#:   상수에서 다시 읽는 뮤턴트가 살아남는다.
실행축 = "SIM-TEST-AXIS"
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
        self.실린회차[target_state_date] = tuple(leg.seq for leg in commitment.arrival_schedule)
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


class _개장정본_감시:
    """`opened_days_after` 를 **부르면 기록한다.** 부르는 것 자체가 회귀다.

    ★ 실제로 앞질러 열린 날이 있는 것처럼 답한다 — 마스터가 이 답을 쓰기 시작하면
      곧바로 전파가 되살아나 `물류.dates` 가 늘어난다. **미끼다.**
    """

    def __init__(self, *days: date) -> None:
        self.days = days
        self.호출: list[date] = []

    def __call__(self, *, after: date, sim_run_id: str, connect: Any = None) -> tuple[date, ...]:
        self.호출.append(after)
        return tuple(d for d in self.days if d > after)


# ---------------------------------------------------------------------------
# 1. 🔴 **목표 상태일 하루에만 싣는다** — 앞질러 열린 날이 있어도 그렇다
# ---------------------------------------------------------------------------


def test_정방향이면_다음날_하나뿐이다(_배선: tuple[_재무전이, _전이]) -> None:
    """★ **하나인 것이 정상이다.**

    ② 2026-09-10 — 뜻이 바뀌었다. 전에는 *"내일이 아직 없어서"* 하나였고, 지금은
      **전파를 아예 안 해서** 하나다. 재는 값은 그대로다.
    """
    _, 물류 = _배선

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션, sim_run_id=실행축)
    assert out.status == "APPLIED", out.reason

    assert out.status == "APPLIED"
    assert 물류.dates == [다음날]
    assert out.carried_forward == []
    assert out.carried_forward_status == "OK", "안 읽는데 못 읽은 것으로 나갔다"


# ---------------------------------------------------------------------------
# 2. 🔴 **도착일이 멀어도 안 늘어난다**
# ---------------------------------------------------------------------------


def test_이미_열린_날들에는_안_싣는다(_배선: tuple[_재무전이, _전이]) -> None:
    """🔴 **② 뒤집힌 검사다** (물류 `#484` · 2026-09-10).

    ```text
    ① 2026-09-07  "이미 열린 날들에도 싣는다"   fixture JSON 복제 구조라 필요했다
    ② 2026-09-10  "안 싣는다"                  inbound_schedules 1행이 정본이 되어
                                               전파가 ScheduleConflict 를 만든다
    ```

    ★ 도착일을 **아흐레 뒤**로 둔다 — 옛 코드였다면 그 사이 열린 날마다 같은 회차가
      다 유효해 네 날에 실렸을 모양이다. 여기서 하나로 서야 회귀가 잡힌다.
    """
    _, 물류 = _배선

    out = transition.apply_approval(_commitment(도착=9), connect=_가짜커넥션, sim_run_id=실행축)
    assert out.status == "APPLIED", out.reason

    assert 물류.dates == [다음날], f"전방 전파가 되살아났다: {물류.dates}"
    assert out.carried_forward == []


def test_따라잡은_날이_없다고_결과에_적는다(_배선: tuple[_재무전이, _전이]) -> None:
    """★ **회신 칸은 남긴다.** 읽는 쪽이 있고, 칸을 없애는 것은 이번 판의 일이 아니다.

    ② 2026-09-10 — 전에는 *"따라잡은 날을 적는다"* 였다. 지금은 따라잡을 것이
      없으므로 **비어 있는 것이 정직한 답**이고, `UNREADABLE` 이 아니다.
    """
    out = transition.apply_approval(_commitment(), connect=_가짜커넥션, sim_run_id=실행축)
    assert out.status == "APPLIED", out.reason

    assert out.carried_forward == []
    assert out.carried_forward_status == "OK"


def test_물류에_한_묶음만_준다(_배선: tuple[_재무전이, _전이]) -> None:
    """⚠️ 물류 `persist` 는 **행마다** `persist_inventory` 를 부른다 — 계약은 그대로다.

    ② 2026-09-10 — 전에는 *"묶음을 여러 개 준다"* 였다. 계약이 바뀐 것이 아니라
      **마스터가 넘길 날이 하나뿐**이라 묶음도 하나다.
    """
    _, 물류 = _배선

    transition.apply_approval(_commitment(), connect=_가짜커넥션, sim_run_id=실행축)

    assert 물류.persisted == [f"row@{다음날}"], f"묶음이 여럿 나갔다: {물류.persisted}"


# ---------------------------------------------------------------------------
# 3. 재무는 안 건드린다
# ---------------------------------------------------------------------------


def test_재무는_다음날_하나만_받는다(_배선: tuple[_재무전이, _전이]) -> None:
    """🔴 **재무 상태의 다일 의미는 재무가 정한다.**

    `in_transit` 이 여러 날에 걸쳐 유지된다는 것은 **물류가 자기 파일에 적은 사실**
    이다. 재무 상태행도 같은 성격인지는 재무 몫이라, 마스터가 대신 넓히지 않는다 —
    `inspection_provider` 를 마스터가 안 고른 것과 같은 자리다.

    ★ ① 재는 사실이 그대로다. 지금은 물류도 하루만 받지만, **넓힐 자리가 생기면 물류
      쪽부터 넓어진다** — 그때 재무가 딸려 들어가는 것을 여기가 막는다.
    """
    재무, _ = _배선

    transition.apply_approval(_commitment(), connect=_가짜커넥션, sim_run_id=실행축)

    assert 재무.dates == [다음날], "마스터가 재무 다일 의미를 대신 정했다"


# ---------------------------------------------------------------------------
# 4. 🔴 **개장 정본을 아예 안 읽는다**
# ---------------------------------------------------------------------------


def test_개장_정본을_아예_안_읽는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **② 뒤집힌 검사다** (물류 `#484` · 2026-09-10).

    ```text
    ① 2026-09-07  "못 읽으면 UNREADABLE 로 적고 승인은 세운다"
                  마스터가 `master_day_openings` 를 읽어 전파할 날을 정하던 시절.
                  «없는 것»과 «못 읽은 것»을 가르는 것이 그 검사의 전부였다.

    ② 2026-09-10  "안 읽는다"
                  전파가 없어져 읽을 이유가 없다. **못 읽을 일이 없으므로**
                  `UNREADABLE` 도 안 나온다.
    ```

    ⚠️ 「못 읽은 것과 없는 것은 다르다」는 규율 자체는 안 잃었다 —
      `collection_events` 가 자기 자리에서 같은 규율을 지키고
      `test_collection_events.py` 가 그것을 잰다.

    ★ **미끼를 놓는다.** 앞질러 열린 날이 있는 것처럼 답하는 대역을 걸어 두고, 그것을
      **한 번도 안 불렀는지**를 잰다. 부르기 시작하면 전파도 곧 되살아난다.
    """
    _, 물류 = _배선
    감시 = _개장정본_감시(다음날 + timedelta(days=1), 다음날 + timedelta(days=2))
    monkeypatch.setattr("app.master.day_opening_repository.opened_days_after", 감시)

    out = transition.apply_approval(_commitment(도착=9), connect=_가짜커넥션, sim_run_id=실행축)
    assert out.status == "APPLIED", out.reason

    assert 감시.호출 == [], f"승인 전이가 개장 정본을 다시 읽었다: {감시.호출}"
    assert 물류.dates == [다음날]
    assert out.carried_forward == []
    assert out.carried_forward_status == "OK", "안 읽는데 UNREADABLE 이 나갔다"


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
#
# ★ **재는 문이 바뀌었다** (2026-09-10 · 물류 `#484`). 전방 전파가 없어져
#   `apply_approval` 로는 «다른 날» 을 만들 수 없다. 그래서 좁히는 함수를 **직접**
#   부른다 — 그 함수는 `_arrival_blocked` 가 쓰고 있어 살아 있고, 재는 사실도 그대로다.
# ---------------------------------------------------------------------------


def test_도착일이_지난_날에는_그_회차를_안_싣는다() -> None:
    """🔴 **실측 모양 그대로다.** 도착일 하루 뒤·닷새 뒤·엿새 뒤를 묻는다."""
    도착일 = 다음날 + timedelta(days=1)
    지난뒤 = [도착일 + timedelta(days=n) for n in (1, 5, 6)]
    약정 = _commitment(도착=2)

    assert transition._still_incoming_on(약정, 도착일) is not None, (
        "도착일 당일은 아직 안 온 것으로 센다"
    )
    for 날 in 지난뒤:
        assert transition._still_incoming_on(약정, 날) is None, (
            f"도착일이 지난 {날} 에 그 회차가 남았다 — 유령 확정입고다"
        )


def test_실을_회차가_없으면_빈_묶음이_아니라_None_이다() -> None:
    """★ 빈 사본을 돌려주면 물류가 *"오늘 도착 예정 0"* 을 새로 쓰게 된다.

    ★ ① 재는 사실이 그대로다 — 전에는 *"그 날은 `build` 를 안 부른다"* 로 **결과**를
      쟀고, 지금은 그 결정을 만드는 **답의 종류**를 잰다. `None` 이 아니라 회차 0개짜리
      사본이 나오면 `_arrival_blocked` 도 못 막고 빈 묶음이 물류까지 간다.
    """
    도착일 = 다음날 + timedelta(days=1)
    약정 = _commitment(도착=2)

    좁힌것 = transition._still_incoming_on(약정, 도착일 + timedelta(days=1))

    assert 좁힌것 is None, f"실을 회차가 없는데 사본을 만들었다: {좁힌것}"


def test_carried_forward_는_열린_날이_아니라_실제로_쓴_날이다(
    _배선: tuple[_재무전이, _전이],
) -> None:
    """🔴 화면이 *"따라잡았다"* 고 말하는 날과 행이 실제로 선 날이 갈리면 안 된다.

    ★ ① 재는 사실이 그대로다. 리터럴 대신 **불변식**으로 적었다 — 지금은 양쪽이 다
      비어 있지만, 누가 전파를 되살리면서 `carried_forward` 를 안 채우면 그 순간
      여기가 운다.
    """
    _, 물류 = _배선

    out = transition.apply_approval(_commitment(도착=2), connect=_가짜커넥션, sim_run_id=실행축)
    assert out.status == "APPLIED", out.reason

    assert out.carried_forward == [d for d in 물류.dates if d != 다음날], (
        f"물류가 쓴 날 {물류.dates} 과 따라잡았다고 적은 날 {out.carried_forward} 이 갈렸다"
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


def _실린회차(약정: ApprovedCommitment, 날: date) -> tuple[int, ...] | None:
    """그 날 「앞으로 올 도착분」으로 남는 회차 번호들. 하나도 없으면 `None`."""
    좁힌것 = transition._still_incoming_on(약정, 날)
    return None if 좁힌것 is None else tuple(leg.seq for leg in 좁힌것.arrival_schedule)


def test_1회차_도착_뒤_2회차_도착_전_날에는_2회차만_실린다() -> None:
    """🔴 **날마다 상한이 다르다.** 1회차는 이미 왔고 2회차만 앞으로 올 도착분이다."""
    첫도착, 둘도착 = AS_OF + timedelta(days=2), AS_OF + timedelta(days=5)
    사이 = AS_OF + timedelta(days=3)
    둘_지난뒤 = AS_OF + timedelta(days=7)
    약정 = _두회차()

    assert _실린회차(약정, 다음날) == (1, 2), "승인 다음 날에는 두 회차가 다 앞으로 올 도착분이다"
    assert _실린회차(약정, 첫도착) == (1, 2), "도착일 당일은 아직 안 온 것으로 센다"
    assert _실린회차(약정, 사이) == (2,), f"1회차가 도착 뒤에도 남았다: {_실린회차(약정, 사이)}"
    assert _실린회차(약정, 둘도착) == (2,)
    assert _실린회차(약정, 둘_지난뒤) is None, "두 회차가 다 지난 날에도 남았다"


def test_좁힌_사본은_남긴_회차의_합으로_선다() -> None:
    """★ 사본도 `__post_init__` 검증을 지나야 한다 — 총량·총액이 남긴 회차와 맞는다."""
    사이 = AS_OF + timedelta(days=3)
    약정 = _두회차()

    안좁힌것 = transition._still_incoming_on(약정, 다음날)
    좁힌것 = transition._still_incoming_on(약정, 사이)

    assert 안좁힌것 is not None and 안좁힌것.total_qty_kg == 100.0
    assert 좁힌것 is not None
    assert 좁힌것.total_qty_kg == 60.0, "좁혔는데 총량은 원래 값 그대로였다"
    assert 좁힌것.total_amount_krw == 60000.0, "좁혔는데 총액은 원래 값 그대로였다"


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
    _배선: tuple[_재무전이, _전이],
) -> None:
    """🔴 **`purchases` 도 재무 행도 안 써야 한다** — `_ledger_blocked` 와 같은 자리다."""
    재무, 물류 = _배선
    열린횟수: list[int] = []

    def _connect() -> _가짜커넥션:
        열린횟수.append(1)
        return _가짜커넥션()

    out = transition.apply_approval(_리드타임0(), connect=_connect, sim_run_id=실행축)

    assert out.status == "NOT_APPLIED", f"물류만 빠진 채 {out.status} 가 나갔다"
    assert 열린횟수 == [], "쓸 수 없는데 커넥션을 열었다"
    assert 재무.persisted == [] and 물류.persisted == [], "물류만 빠진 채 다른 파트를 썼다"
    assert 물류.dates == [] and 재무.dates == [], "막았는데 build 를 불렀다"


def test_사유가_도착일과_목표_상태일을_숫자로_적는다(
    _배선: tuple[_재무전이, _전이],
) -> None:
    """★ *"도착일이 목표 상태일보다 이르다"* 를 사람이 바로 알아보게 적는다."""
    out = transition.apply_approval(_리드타임0(), connect=_가짜커넥션, sim_run_id=실행축)

    assert out.status == "NOT_APPLIED"
    assert AS_OF.isoformat() in out.reason, "회차 도착일이 사유에 없다"
    assert 다음날.isoformat() in out.reason, "목표 상태일이 사유에 없다"
    assert "1회차" in out.reason, "어느 회차인지 이름을 안 불렀다"
    # 🔴 *"틀렸다"* 가 아니라 **아무도 안 정했다** 는 사실을 적어야 한다.
    assert "정해진 적이 없다" in out.reason
    assert "물류·매입" in out.reason, "정할 자리를 안 가리켰다"


def test_도착일이_목표_상태일과_같으면_지나간다(
    _배선: tuple[_재무전이, _전이],
) -> None:
    """🔴 **회귀 방어.** 리드타임 1 이상은 지금 그대로다 — 새 가드는 거기 안 건다.

    ★ 경계는 `arrival_date == target_state_date` 다. 도착일 당일은 아직 안 온 것으로
      세므로(`_still_incoming_on` 의 `>=`) 여기서 막으면 정상 승인이 다 막힌다.
    """
    _, 물류 = _배선

    out = transition.apply_approval(_commitment(도착=1), connect=_가짜커넥션, sim_run_id=실행축)

    assert out.status == "APPLIED", out.reason
    assert 물류.dates == [다음날], f"경계 승인이 안 실렸다: {물류.dates}"
    assert 물류.실린회차[다음날] == (1,)
