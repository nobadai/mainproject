"""하루를 여는 진입점 — **그날 상태 행을 보장한다**.

재무·물류의 `DayOpening` 구현은 아직 없다. 그래서 여기서 재는 것은 *"어떤 값을
물려받는가"* 가 아니라 **마스터가 소유한 것 하나** — 어느 날부터 어느 날까지 걷고,
구멍을 남기지 않고, 어떤 달력을 쓰고, 언제 커넥션을 열고 한 번 커밋하는가다.

🔴 **구현이 없다고 검사를 미루면 그 자리가 영영 안 잠긴다.** 재무·물류가 들어온 뒤에
   "01-03 이 비었다"를 발견하면, 그때는 이미 그날을 조회하는 경로가 막혀 있다.
   가짜 커넥션은 그 달력을 **오늘** 잰다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import day_open
from app.master.router import router

AS_OF = date(2026, 1, 5)


@pytest.fixture(autouse=True)
def 하루넘김_등록소를_비운다() -> Iterator[None]:
    """등록소는 프로세스 전역이다 — **앞뒤로 비운다.**

    ★ 끝나고만 비우면 앞 테스트가 남긴 등록이 이 파일로 흘러든다
      (`test_transition_boundary.py` 와 같은 규율).
    """
    day_open.reset()
    try:
        yield
    finally:
        day_open.reset()


class 가짜커넥션:
    """세는 것만 한다 — commit · rollback · close 가 **몇 번** 불렸나."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class 가짜하루열기:
    """파트 자리에 들어가는 대역. **물어본 날과 만든 날을 기록한다.**

    ★ 진짜 표 대신 열린 날 집합을 들고 있다 — `open_day` 를 부르면 그날이 열린다.
      DB 없이도 *"만든 날을 다시 물어보면 열려 있다"* 가 성립해야 멱등을 잴 수 있다.
    """

    def __init__(
        self,
        name: str,
        open_days: set[date],
        *,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self._open = set(open_days)
        self._raises = raises
        #: `is_open` 이 물어본 날. 뒤로 몇 날까지 걸었는지가 여기 남는다.
        self.asked: list[date] = []
        #: 만든 날 — `(as_of, carry_from)` 짝이다.
        self.opened: list[tuple[date, date]] = []
        #: 받은 커넥션. 파트끼리 같은 것을 써야 한다.
        self.conns: list[Any] = []

    def is_open(self, conn: Any, *, as_of: date) -> bool:
        self.asked.append(as_of)
        self.conns.append(conn)
        return as_of in self._open

    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:
        self.conns.append(conn)
        if self._raises is not None:
            raise self._raises
        self.opened.append((as_of, carry_from))
        self._open.add(as_of)


def _connect_spy(conn: 가짜커넥션, calls: list[int]):
    def _connect() -> 가짜커넥션:
        calls.append(1)
        return conn

    return _connect


def _both(open_days: set[date]) -> tuple[가짜하루열기, 가짜하루열기]:
    """두 파트를 같은 상태로 등록한다."""
    finance = 가짜하루열기("finance", open_days)
    logistics = 가짜하루열기("logistics", open_days)
    day_open.register_day_opening("finance", finance)
    day_open.register_day_opening("logistics", logistics)
    return finance, logistics


# ── ① 미등록은 오류가 아니라 상태다 ────────────────────────────────────


def test_등록이_0건이면_커넥션을_열지_않는다() -> None:
    """🔴 열고 나서 아무 일도 안 하면 **빈 트랜잭션**이 하루 넘김마다 열렸다 닫힌다."""
    calls: list[int] = []

    out = day_open.open_day(AS_OF, connect=_connect_spy(가짜커넥션(), calls))

    assert out.status == "NOT_OPENED", "미등록은 '못 했다' 다"
    assert out.missing == ["finance", "logistics"]
    assert "finance" in out.reason and "logistics" in out.reason
    assert out.parts == []
    assert calls == [], "미등록인데 커넥션을 열었다"


# ── ② 멱등 ──────────────────────────────────────────────────────────────


def test_이미_열려_있으면_아무것도_안_하고_빈_목록을_낸다() -> None:
    """★ 같은 날을 두 번 열면 두 번째는 할 일이 없다.

    🔴 **`ALREADY_OPENED` 다. `NOT_OPENED` 가 아니다** (계약 §어휘 · 2026-09-06 정정).
       앞은 *"할 일이 없었다"* 이고 뒤는 *"못 했다"* 다 — 접으면 **매일 도는 정상
       상태가 실패로 보인다.**
    """
    finance, logistics = _both({AS_OF})
    conn = 가짜커넥션()

    out = day_open.open_day(AS_OF, connect=lambda: conn)

    assert out.status == "ALREADY_OPENED"
    assert "이미 열려" in out.reason
    assert [part.opened for part in out.parts] == [[], []]
    assert finance.opened == [] and logistics.opened == []
    assert conn.commits == 1, "행을 안 만들어도 트랜잭션은 정상 종료한다"


def test_두_번_열어도_두_번째는_아무것도_안_만든다() -> None:
    """★ 멱등을 **연속 호출로** 잰다 — 첫 호출이 만든 행이 둘째 호출의 전제가 된다."""
    finance, _ = _both({AS_OF - timedelta(days=2)})

    첫째 = day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())
    둘째 = day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    assert 첫째.status == "OPENED"
    assert 둘째.status == "ALREADY_OPENED", "멱등 no-op 은 실패가 아니다"
    assert [part.opened for part in 둘째.parts] == [[], []]
    assert len(finance.opened) == 2, "두 번째 호출이 행을 더 만들었다"


# ── ③ 구멍을 남기지 않는다 ──────────────────────────────────────────────


def test_사흘이_비면_세_날을_순서대로_채운다() -> None:
    """🔴 **마지막 행 12-31 · as_of 01-05 → 다섯 행이다.**

    01-03 을 건너뛰면 나중에 그날을 조회할 때 또 막힌다. 마지막 날만 만드는 것으로는
    다음 하루 넘김이 다시 같은 자리에서 선다.
    """
    finance, logistics = _both({date(2025, 12, 31)})

    out = day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    만든날 = [as_of for as_of, _ in finance.opened]
    assert 만든날 == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
    ], "구멍이 있거나 순서가 틀렸다"
    assert 만든날 == [as_of for as_of, _ in logistics.opened]
    assert out.status == "OPENED"
    assert [part.opened for part in out.parts] == [만든날, 만든날]


# ── ④ carry_from 은 바로 전날이다 ───────────────────────────────────────


def test_carry_from_이_바로_전날이다() -> None:
    """🔴 건너뛴 날에서 물려받으면 그 사이 하루치 사실이 **장부에 없는 채로** 선다."""
    finance, _ = _both({date(2025, 12, 31)})

    day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    for as_of, carry_from in finance.opened:
        assert carry_from == as_of - timedelta(days=1), (
            f"{as_of} 를 {carry_from} 에서 물려받았다 — 건너뛴 날이 있다"
        )


# ── ⑤ 달력은 날마다다 — 실행일이 아니다 ────────────────────────────────


def test_주말도_채운다() -> None:
    """🔴 **판단은 평일만이고 장부는 날마다다.**

    2026-01-03 은 토요일, 01-04 는 일요일이다. `execution_day.next_execution_day` 로
    걸으면 이 두 날이 사라지고, 토·일 이틀치 사실이 없는 채로 월요일 행이 선다.
    토요일에도 재고는 늙고 지급일은 온다.
    """
    finance, _ = _both({date(2026, 1, 2)})  # 금요일

    day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())  # 01-05 는 월요일

    만든날 = [as_of for as_of, _ in finance.opened]
    assert date(2026, 1, 3) in 만든날, "토요일을 걸렀다 — 실행일 달력을 썼다"
    assert date(2026, 1, 4) in 만든날, "일요일을 걸렀다 — 실행일 달력을 썼다"
    assert 만든날 == [date(2026, 1, 3), date(2026, 1, 4), date(2026, 1, 5)]


def test_공휴일도_채운다() -> None:
    """★ 설날(2026-02-17)도 그냥 하루다 — 달력에 특별 취급이 없다.

    ⚠️ 실행일 달력은 공휴일을 **애초에 모른다** (`execution_day` 의 한계). 여기서
      재는 것은 그 한계와 무관하다 — 하루 넘김은 어느 달력도 안 쓰고 날마다 건는다.
    """
    설날 = date(2026, 2, 17)
    finance, _ = _both({설날 - timedelta(days=1)})

    day_open.open_day(설날, connect=lambda: 가짜커넥션())

    assert [as_of for as_of, _ in finance.opened] == [설날]


def test_하루넘김_모듈이_실행일_달력을_임포트하지_않는다() -> None:
    """🔴 **원문으로 잠근다.** 언젠가 누가 *"주말엔 안 도니까"* 로 갈아 끼울 수 있다.

    ★ import 로는 안 잡힌다 — 쓰지 않는 import 는 아무 흔적이 없고, 쓰는 순간에는
      이미 하루가 사라져 있다.
    """
    source = Path(day_open.__file__).read_text(encoding="utf-8")

    for 금지 in ("import execution_day", "next_execution_day(", "is_execution_day("):
        assert 금지 not in source, f"하루 넘김이 실행일 달력을 썼다: {금지}"


# ── ⑥ 상한 31일 ────────────────────────────────────────────────────────


def test_31일을_넘으면_막고_행을_안_만든다() -> None:
    """⚠️ **실수로 먼 날을 열면 수백 행이 조용히 생긴다.**

    🔴 **`REJECTED_GAP` 이다. `NOT_OPENED` 가 아니다** (계약 §5 · 2026-09-06 정정).
       이것만 *"관리자 강제 개장"* 이라는 **다른 다음 걸음**을 갖는다 — 접으면 화면이
       재시도를 권하고, 재시도로는 안 풀린다.

    마지막 열린 날이 32일 전이면 뒤로 31일까지 걸어도 못 찾는다 — 막는다.
    """
    먼날 = AS_OF - timedelta(days=32)
    finance, logistics = _both({먼날})
    conn = 가짜커넥션()

    out = day_open.open_day(AS_OF, connect=lambda: conn)

    assert out.status == "REJECTED_GAP"
    assert [part.status for part in out.parts] == ["PART_FAILED", "PART_FAILED"]
    assert "31" in out.parts[0].reason, "상한을 사유에 안 적으면 왜 막혔는지 모른다"
    assert finance.opened == [] and logistics.opened == [], "막혔는데 행을 만들었다"
    assert conn.commits == 1 and conn.rollbacks == 0, "막힘은 실패가 아니다"


def test_딱_31일이면_막지_않고_31행을_만든다() -> None:
    """★ 경계는 **포함**이다. 30일 번인 바로 다음 칸까지는 연다."""
    finance, _ = _both({AS_OF - timedelta(days=31)})

    out = day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    assert out.status == "OPENED"
    assert len(finance.opened) == 31
    assert finance.opened[-1][0] == AS_OF


def test_상한을_넘으면_뒤로_더_걷지_않는다() -> None:
    """🔴 막는 것만으로는 부족하다 — **찾는 걸음 자체가 상한 안에서 끝나야 한다.**"""
    finance, _ = _both(set())

    day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    assert finance.asked[0] == AS_OF, "as_of 부터 물어봐야 한다"
    assert finance.asked[-1] == AS_OF - timedelta(days=day_open.MAX_CARRY_DAYS)
    assert len(finance.asked) == day_open.MAX_CARRY_DAYS + 1


# ── ⑦ 파트마다 따로 걷는다 ──────────────────────────────────────────────


def test_파트마다_자기_마지막_날에서_걷는다() -> None:
    """🔴 **재무와 물류가 서로 다른 날까지 열려 있을 수 있다.**

    한 파트의 진도를 다른 파트에 맞춰 세면, 앞선 쪽은 있는 행을 다시 만들고 뒤처진
    쪽은 구멍이 남는다.
    """
    finance = 가짜하루열기("finance", {date(2026, 1, 4)})
    logistics = 가짜하루열기("logistics", {date(2026, 1, 1)})
    day_open.register_day_opening("finance", finance)
    day_open.register_day_opening("logistics", logistics)

    out = day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    assert [as_of for as_of, _ in finance.opened] == [date(2026, 1, 5)]
    assert [as_of for as_of, _ in logistics.opened] == [
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
    ]
    assert out.status == "OPENED"


def test_한쪽이_막혀도_다른_쪽을_되돌리지_않는다() -> None:
    """★ 뒤처진 파트가 있다고 앞선 파트의 그날 행을 없애지 않는다.

    ⚠️ **`FAILED` 와 다른 자리다.** 막힘은 우리가 아는 사실이고, 실패는 되돌려야 하는
      사건이다. 둘을 접으면 재무가 한 달 밀린 날 물류까지 못 연다.
    """
    finance = 가짜하루열기("finance", {AS_OF - timedelta(days=40)})
    logistics = 가짜하루열기("logistics", {AS_OF - timedelta(days=1)})
    day_open.register_day_opening("finance", finance)
    day_open.register_day_opening("logistics", logistics)
    conn = 가짜커넥션()

    out = day_open.open_day(AS_OF, connect=lambda: conn)

    # 🔴 **전체는 REJECTED_GAP 이다** (계약 §5 · 2026-09-06 정정).
    #    재무가 상한을 넘겨 막혔으면 그 날은 온전히 열리지 않았고, 다음 걸음이
    #    "관리자 강제 개장" 이다. 물류가 열었다는 이유로 OPENED 를 내면 화면이
    #    **그 날을 정상으로 보고 지나간다.**
    assert out.status == "REJECTED_GAP"
    상태 = {part.part: part.status for part in out.parts}
    assert 상태 == {"finance": "PART_FAILED", "logistics": "PART_OPENED"}
    assert [as_of for as_of, _ in logistics.opened] == [AS_OF]
    assert conn.commits == 1 and conn.rollbacks == 0


def test_한쪽만_등록되면_등록된_쪽만_걷는다() -> None:
    """★ `apply_approval` 과 **다른 자리다.**

    승인 전이는 반쪽 반영이 *"현금은 나갔는데 입고 예정이 없는 장부"* 를 만들어서
    한쪽만 등록되면 아무것도 안 한다. 하루 넘김은 두 파트가 서로 다른 표의 서로 다른
    행을 만들고 엮이지 않는다 — 물류가 먼저 붙은 날 재무를 기다릴 이유가 없다.
    """
    logistics = 가짜하루열기("logistics", {AS_OF - timedelta(days=1)})
    day_open.register_day_opening("logistics", logistics)

    out = day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    assert out.status == "OPENED"
    assert out.missing == ["finance"]
    assert [part.part for part in out.parts] == ["logistics"]
    assert [as_of for as_of, _ in logistics.opened] == [AS_OF]


# ── ⑧ 한 커넥션 · 한 커밋 · 실패하면 rollback ───────────────────────────


def test_한_커넥션으로_한_번_커밋한다() -> None:
    finance, logistics = _both({AS_OF - timedelta(days=1)})
    conn = 가짜커넥션()
    calls: list[int] = []

    out = day_open.open_day(AS_OF, connect=_connect_spy(conn, calls))

    assert out.status == "OPENED"
    assert calls == [1], "커넥션은 하나만 연다"
    assert conn.commits == 1, "커밋은 두 파트가 끝난 뒤 한 번이다"
    assert conn.rollbacks == 0
    assert conn.closed == 1
    assert set(finance.conns) == {conn}
    assert set(logistics.conns) == {conn}, "두 파트가 같은 커넥션을 써야 한다"


def test_적재가_터지면_전부_되돌린다() -> None:
    """🔴 반쯤 채운 달력이 커밋되면 **어디까지 진짜인지 아무도 말해 주지 않는다.**"""
    finance = 가짜하루열기("finance", {AS_OF - timedelta(days=3)})
    logistics = 가짜하루열기(
        "logistics", {AS_OF - timedelta(days=3)}, raises=RuntimeError("전날 행이 없다")
    )
    day_open.register_day_opening("finance", finance)
    day_open.register_day_opening("logistics", logistics)
    conn = 가짜커넥션()

    out = day_open.open_day(AS_OF, connect=lambda: conn)

    assert out.status == "NOT_OPENED"
    assert "전날 행이 없다" in out.reason, "사유를 안 남기면 무엇이 터졌는지 모른다"
    assert out.parts == [], "되돌렸는데 만든 날 목록이 나가면 안 된다"
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn.closed == 1


def test_적재_실패가_예외로_올라가지_않는다() -> None:
    """★ 500 이 되면 사람이 보기에 다음 날로 못 가는 것이 된다 — 실제로는 어제 그대로다."""
    _both({AS_OF - timedelta(days=1)})
    day_open.register_day_opening(
        "logistics",
        가짜하루열기("logistics", {AS_OF - timedelta(days=1)}, raises=OSError("끊겼다")),
    )

    out = day_open.open_day(AS_OF, connect=lambda: 가짜커넥션())

    assert out.status == "NOT_OPENED"


def test_with_conn_을_쓰지_않는다() -> None:
    """🔴 psycopg3 는 블록이 정상 종료하면 **자동으로 커밋한다.**

    그러면 "커밋은 마스터가 한 번만 한다"는 규율이 문법에 숨고, 커밋 줄을 지우는 변이
    검사도 안 걸린다 (`transition.py` 가 같은 이유로 같은 것을 금한다).
    """
    source = Path(day_open.__file__).read_text(encoding="utf-8")

    assert "with conn" not in source.replace("`with conn:`", "")


# ── ⑨ 분담이 문자로 잠긴다 ─────────────────────────────────────────────


def test_하루넘김_모듈에_SQL_이_없다() -> None:
    """🔴 여기에 `INSERT` 가 한 줄이라도 들어오면 마스터가 **남의 칸 이름**을 알게 된다.

    ★ 무엇을 물려받을지는 파트가 안다. 마스터는 **언제 · 어느 날까지 · 한 트랜잭션**만
      정한다 — `test_transition_boundary.py` 의 같은 검사와 짝이다.
    """
    source = Path(day_open.__file__).read_text(encoding="utf-8")

    for 금지 in ("INSERT INTO", "UPDATE ", "DELETE ", "SELECT "):
        assert 금지 not in source, f"마스터 하루 넘김 경계에 SQL 이 있다: {금지}"


# ── ⑩ 트리거는 명시적 호출이다 ─────────────────────────────────────────


def test_매입_실행이_하루를_열지_않는다() -> None:
    """🔴 **판단 한 번이 장부를 바꾸면 재현성이 깨진다.**

    조회하려고 돌린 실행이 상태를 만들면 *"같은 as_of 로 백번 돌려도 같은 답"* 이
    성립하지 않는다. 하루가 넘어가는 것은 사건이지 부작용이 아니다.
    """
    from app.master import flow, service

    for 모듈 in (service, flow):
        source = Path(모듈.__file__).read_text(encoding="utf-8")
        assert "day_open" not in source, (
            f"{모듈.__name__} 이 하루 넘김을 부른다 — 실행이 상태를 만들면 안 된다"
        )


def test_라우터에_명시적_자리가_있다() -> None:
    """★ 부를 자리가 없으면 결국 누군가 실행 경로에 얹는다."""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    응답 = client.post("/master/days/2026-01-05/open")

    assert 응답.status_code == 200, 응답.text
    본문 = 응답.json()
    assert 본문["as_of"] == "2026-01-05"
    # ★ 오늘은 등록이 0건이라 `NOT_OPENED` 다 — 그것도 **말해 주어야 하는 사실**이다.
    assert 본문["status"] == "NOT_OPENED"
    assert 본문["missing"] == ["finance", "logistics"]
