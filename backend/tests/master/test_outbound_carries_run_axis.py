"""출고가 **실행 축으로 거른다** — 2026-09-11.

`outbound_flow.due_sale_items` 는 그날 나갈 판매를 `sale_date` 와 `order_status`
로만 골랐다. 축이 없으니 **남의 실행**이 아니라 **모든 실행**의 그 날짜 판매를
봤다.

```text
전  WHERE s.sale_date = %s AND s.order_status IN ('CONFIRMED','READY')
후  WHERE s.sim_run_id = %s AND s.sale_date = %s AND s.order_status IN (...)
```

⚠️ **지금까지 안 걸린 것은 번인 판매가 전부 `DELIVERED` 였기 때문이다.** 상태
  필터가 `DELIVERED` 를 빼서 아무것도 안 잡혔을 뿐이다. **판매 확정이 서는 순간**
  같은 날짜의 남의 실행 판매가 내 창고에서 나간다 — 나가고 나면 되돌릴 경로가
  없고, 두 실행의 재고가 동시에 틀린다.

🔴 **채권은 `2026-09-09` 에 이미 같은 판단을 받았다**
  (`finance_receivable.read_confirmed_sales` 가 `sim_run_id` 로 거른다). 이웃을
  고치고 출고 하나가 남아 있었다.

🔴 **이 파일이 잠그는 넷.**

```text
① 두 실행의 판매가 같은 날짜에 있어도 **자기 실행 것만** 나간다
② 축은 **기본값 없는 키워드**다 — 안 넘기면 TypeError
③ 스케줄러의 「출고」 단계 호출이 축을 넘긴다 (AST · 0건을 세면 그것도 막는다)
④ **조회가 거른다** — 파이썬이 아니라 SQL 이 거른다
```

★★ ④ 가 ① 과 다른 사실이다. 파이썬에서 걸러도 답은 맞지만 **DB 가 남의 실행 행을
  다 읽어 온다** — 커지면 느려지고, 무엇보다 *"조회가 정본"* 이 아니게 된다. 그래서
  이 파일의 목 커서는 **SQL 이 적은 조건만큼만** 걸러서 돌려준다. 조건이 빠지면 남의
  실행 행이 그대로 올라오고, 그것이 ④ 를 빨갛게 만든다.

★ **DB 를 안 탄다.** 연결 · 커서 · 물류 세 함수 · 판매 lifecycle 훅을 전부 대역으로
  준다. `DB_SCHEMA` 만 환경에서 끊어 준다.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Self

import pytest

from app.master import outbound_flow, scheduler
from app.master.clock import SEOUL
from app.master.outbound_flow import due_sale_items, ship_due_sales
from app.master.pending_transition import RetryOut
from app.master.scheduler import ScheduledAction, run_scheduled_day

#: 두 실행이 **같은 날짜**로 부딪히는 날. 날짜를 고정해야 축만 재게 된다.
고른_날 = date(2026, 9, 8)

#: 🔴 **둘 다 번인이 아니다.** 하나라도 번인이면 번인으로 떨어지는 길을 못 본다.
실행_가 = "SIM-WALK-2026-V4"
실행_나 = "SIM-SALESCHAIN-20260911"

#: 판매 한 줄이 요구하는 양.
요구량 = Decimal(100)

#: 검사 대상 파일. 🔴 **`__file__` 에서 얻는다** — 경로를 손으로 적으면 파일이
#:   옮겨간 날 조용한 빈 통과가 될 길이 생긴다.
_스케줄러 = pathlib.Path(scheduler.__file__)


@pytest.fixture(autouse=True)
def 스키마_이름(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_db_schema()` 는 `DB_SCHEMA` 를 읽는다 — 여기서 끊는다. **연결은 안 연다.**"""
    monkeypatch.setenv("DB_SCHEMA", "haetdeul")


# ── 목 커서 — **SQL 이 적은 조건만큼만 거른다** ─────────────────────────

#: `컬럼 = %s` 를 **적힌 차례대로** 집는다. JOIN 의 `si.sale_id = s.sale_id` 는
#: `%s` 가 없어서 안 걸리고, WHERE 앞은 아예 안 본다.
_등호 = re.compile(r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?)\s*=\s*%s")

#: `컬럼 IN ('A', 'B')` — 값이 SQL 안에 박혀 있는 조건.
_목록 = re.compile(r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?)\s+IN\s+\(([^)]*)\)")


def _칸(이름: str) -> str:
    """`s.sim_run_id` → `sim_run_id`. 별칭을 떼고 칸 이름만 남긴다."""
    return 이름.split(".")[-1]


def _WHERE(문장: str) -> str:
    """`WHERE` 뒤 · `ORDER BY` 앞. **거르는 자리만** 본다."""
    뒤 = 문장.split("WHERE", 1)[1] if "WHERE" in 문장 else ""
    return 뒤.split("ORDER BY", 1)[0]


def _걸러낸다(표: list[dict[str, Any]], 문장: str, 값들: list[Any]) -> list[dict[str, Any]]:
    """**진짜 DB 가 하듯이** SQL 에 적힌 조건만큼만 거른다.

    🔴 **여기가 이 파일의 심장이다.** SQL 에서 `sim_run_id` 조건이 빠지면 이 함수는
       남의 실행 행을 그대로 돌려준다 — 파이썬 쪽에서 나중에 걸러 봐야 *"DB 가
       읽어 왔다"* 는 사실은 안 지워진다.
    """
    where = _WHERE(문장)
    쌍 = list(zip([_칸(m.group(1)) for m in _등호.finditer(where)], 값들, strict=False))
    목록 = [
        (_칸(m.group(1)), tuple(v.strip().strip("'") for v in m.group(2).split(",")))
        for m in _목록.finditer(where)
    ]
    남은: list[dict[str, Any]] = []
    for 행 in 표:
        if any(행[칸] != 값 for 칸, 값 in 쌍):
            continue
        if any(행[칸] not in 값 for 칸, 값 in 목록):
            continue
        남은.append(행)
    return 남은


class _커서:
    """커서 대역. **무엇을 물었고 무엇이 올라왔는지**를 남긴다."""

    def __init__(self, 표: list[dict[str, Any]]) -> None:
        self.표 = 표
        self.문장 = ""
        self.넘긴값: list[Any] = []
        self.낸_행: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, query: Any, params: Any = None) -> None:
        self.문장 = query.as_string() if hasattr(query, "as_string") else str(query)
        self.넘긴값 = list(params or [])
        self.낸_행 = _걸러낸다(self.표, self.문장, self.넘긴값)

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.낸_행)


class _연결:
    """연결 대역. 커서 하나를 계속 쓰므로 **마지막 조회가 그대로 남는다.**"""

    def __init__(self, 표: list[dict[str, Any]]) -> None:
        self.커서 = _커서(표)
        self.events: list[str] = []
        self.closed = False

    def cursor(self) -> _커서:
        return self.커서

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.closed = True


def _행(
    sale_id: str,
    sim_run_id: str,
    *,
    seq: int = 1,
    sale_date: date = 고른_날,
    order_status: str = "CONFIRMED",
) -> dict[str, Any]:
    """`sales JOIN sale_items` 한 줄. **조회가 읽는 칸만** 있다."""
    return {
        "sale_id": sale_id,
        "sim_run_id": sim_run_id,
        "sale_date": sale_date,
        "sale_item_id": f"SI-{sale_id}-{seq}",
        "item_id": "ITEM-배추",
        "quantity_kg": 요구량,
        "order_status": order_status,
    }


#: 🔴 **두 실행의 판매가 같은 날짜에 있다.** 이것이 지금 고치는 상황 그대로다.
부딪히는_표 = [
    _행("SALE-내것-1", 실행_가),
    _행("SALE-남의것-1", 실행_나),
    _행("SALE-남의것-2", 실행_나),
    # ⚠️ 번인 축까지 같은 날에 둔다 — 상수로 떨어지는 길도 막는다.
    _행("SALE-번인", "BURN-IN-2026"),
    # ★ 상태 필터가 여전히 살아 있는지 같이 잰다.
    _행("SALE-내것-이미나감", 실행_가, seq=9, order_status="DELIVERED"),
    # ★ 날짜 필터도 같이 잰다.
    _행("SALE-내것-내일", 실행_가, seq=8, sale_date=date(2026, 9, 9)),
]


# ── 물류 · 판매 대역 ────────────────────────────────────────────────────


@dataclass
class _예약결과:
    reserved_qty_kg: Decimal = 요구량


@dataclass
class _출고결과:
    shipped_qty_kg: Decimal = 요구량


class _대역:
    """물류 · 판매 함수 대역. 부른 인자를 그대로 모은다."""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, _conn: Any, *args: Any, **kwargs: Any) -> Any:
        payload = dict(kwargs)
        if args:
            payload["positional"] = args[0]
        self.calls.append(payload)
        return self.result


def _내보낸다(sim_run_id: str, 표: list[dict[str, Any]] | None = None) -> tuple[Any, Any, _연결]:
    """진짜 `ship_due_sales` 를 **진짜 `due_sale_items` 와 함께** 돌린다.

    🔴 **`due_fn` 을 대역으로 안 끼운다.** 여기서 끼우면 이 파일이 재려는 것
       (*"조회가 축으로 거르는가"*) 이 통째로 빠진다.
    """
    conn = _연결(부딪히는_표 if 표 is None else 표)
    ship = _대역(_출고결과())
    out = ship_due_sales(
        고른_날,
        sim_run_id=sim_run_id,
        connect=lambda: conn,
        reserve_fn=_대역(_예약결과()),
        allocate_fn=_대역(),
        ship_fn=ship,
        deliver_fn=_대역(),
    )
    return out, ship, conn


# ---------------------------------------------------------------------------
# ① 자기 실행 것만 나간다
# ---------------------------------------------------------------------------


def test_같은_날짜에_두_실행이_있어도_자기_실행_것만_나간다():
    """🔴 **이것이 버그의 본체다.**

    ★ **두 번 부른다.** 한 번만 부르면 *"넘긴 값으로 걸렀다"* 와 *"마침 그것만
      있었다"* 를 못 가른다.
    """
    가_out, 가_ship, _ = _내보낸다(실행_가)
    나_out, 나_ship, _ = _내보낸다(실행_나)

    assert [one.sale_id for one in 가_out.items] == ["SALE-내것-1"]
    assert [one.sale_id for one in 나_out.items] == ["SALE-남의것-1", "SALE-남의것-2"]
    assert [call["sale_item_id"] for call in 가_ship.calls] == ["SI-SALE-내것-1-1"]
    assert [call["sale_item_id"] for call in 나_ship.calls] == [
        "SI-SALE-남의것-1-1",
        "SI-SALE-남의것-2-1",
    ]


def test_조회가_남의_실행_판매를_아예_안_돌려준다():
    """★ `ship_due_sales` 를 안 거치고 조회만 직접 잰다 — 자리를 못 박는다."""
    conn = _연결(부딪히는_표)

    나온것 = due_sale_items(conn, as_of=고른_날, sim_run_id=실행_가)

    assert [one.sale_id for one in 나온것] == ["SALE-내것-1"]
    assert {one.sim_run_id for one in 나온것} == {실행_가}


def test_자기_실행_것이_없으면_NOTHING_DUE_다():
    """⚠️ 남의 실행 판매가 있어도 *"내 것이 없다"* 는 `NOTHING_DUE` 다."""
    out, ship, _ = _내보낸다("SIM-아무것도-안판-실행")

    assert out.status == "NOTHING_DUE"
    assert ship.calls == []


# ---------------------------------------------------------------------------
# ② 축은 기본값 없는 키워드다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "함수",
    [ship_due_sales, due_sale_items],
    ids=["ship_due_sales", "due_sale_items"],
)
def test_축은_기본값_없는_키워드다(함수):
    """★ `revalidate_scenario` 와 **같은 규율이다** — 기본값은 곧 업무 규칙이 된다.

    🔴 기본값이 생기면 안 넘긴 자리가 조용히 그 값(번인)으로 돌고, 아무 데도 안
      적힌다. 안 넘기면 **터져야** 그 자리를 그날 안다.
    """
    param = inspect.signature(함수).parameters["sim_run_id"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"sim_run_id 가 키워드 전용이 아니다: {param}"
    )
    assert param.default is inspect.Parameter.empty, (
        f"sim_run_id 에 기본값이 생겼다({param.default!r}) — 기본값은 곧 업무 규칙이다"
    )


def test_축_없이_부르면_터진다():
    """★ 위 검사의 짝 — 서명뿐 아니라 **실제로 안 도는지**까지 본다."""
    conn = _연결(부딪히는_표)

    with pytest.raises(TypeError):
        ship_due_sales(고른_날, connect=lambda: conn)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        due_sale_items(conn, as_of=고른_날)  # type: ignore[call-arg]

    assert conn.커서.문장 == "", "축 없이도 조회가 나갔다"


# ---------------------------------------------------------------------------
# ③ 스케줄러가 축을 넘긴다 — AST · 그리고 실제로 돌려 본다
# ---------------------------------------------------------------------------


def _파싱() -> ast.Module:
    """대상 파일의 AST. 🔴 **못 읽으면 그 자리에서 터진다** — 빈 통과를 안 만든다."""
    return ast.parse(_스케줄러.read_text(encoding="utf-8"))


def test_스케줄러의_출고_호출이_축을_넘긴다():
    """🔴 **이 자리가 채권·수금 옆에서 혼자 축을 안 넘기던 곳이다.**

    ★★ **0건을 세면 그것도 막는다.** 호출을 하나도 못 찾으면 아래 `not 샌것` 은
      공짜로 참이다 — 이름이 바뀐 날 이 검사가 아무것도 안 재면서 초록이 된다.
    """
    센_호출 = 0
    샌것: list[int] = []
    for 마디 in ast.walk(_파싱()):
        if not isinstance(마디, ast.Call):
            continue
        if getattr(마디.func, "id", None) != "outbound_fn":
            continue
        센_호출 += 1
        if not any(kw.arg == "sim_run_id" for kw in 마디.keywords):
            샌것.append(마디.lineno)

    assert 센_호출 >= 1, (
        f"{_스케줄러.name} 에서 outbound_fn 호출을 한 건도 못 찾았다 — 검사가 헛돌고 있다"
    )
    assert not 샌것, f"출고를 부르면서 축을 안 넘기는 자리가 있다 (줄 {샌것})"


def test_출고_단계가_채권_수금과_같은_모양이다():
    """★ **옆 두 줄과 눈으로 같아 보여야 한다.**

    ★★ 채권·수금을 같이 센다 — 셋 다 못 찾으면 그때도 실패한다.
    """
    잰_단계: dict[str, list[set[str | None]]] = {}
    for 마디 in ast.walk(_파싱()):
        if not isinstance(마디, ast.Call) or getattr(마디.func, "id", None) != "_stage":
            continue
        if not 마디.args or not isinstance(마디.args[0], ast.Constant):
            continue
        이름 = 마디.args[0].value
        안쪽 = [n for n in ast.walk(마디) if isinstance(n, ast.Call) and n is not 마디]
        잰_단계.setdefault(이름, []).append({kw.arg for c in 안쪽 for kw in c.keywords})

    for 단계 in ("채권", "수금", "출고"):
        assert 단계 in 잰_단계, f'`_stage("{단계}", …)` 를 못 찾았다 — 검사가 헛돌고 있다'
        assert all("sim_run_id" in 키워드 for 키워드 in 잰_단계[단계]), (
            f"{단계} 단계가 축을 안 넘긴다: {잰_단계[단계]}"
        )


# ── 그리고 실제로 하루를 돌려 본다 ─────────────────────────────────────


@dataclass
class _단계결과:
    status: str
    reason: str = ""


class _단계대역:
    """입고 · 채권 · 수금 · 마감 대역. `as_of` 하나만 받아도 되는 자리들."""

    def __init__(self, status: str) -> None:
        self.status = status

    def __call__(self, _as_of: date, **_kwargs: Any) -> _단계결과:
        return _단계결과(self.status)


class _출고대역:
    """🔴 **진짜 `ship_due_sales` 와 같은 서명이다** — 축을 안 받으면 터진다.

    ★ 그래서 스케줄러가 축을 빼먹으면 `_stage` 가 그것을 `FAILED` 로 옮기고,
      아래 검사가 그 자리에서 빨개진다.
    """

    def __init__(self) -> None:
        self.받은축: list[tuple[date, str]] = []

    def __call__(self, as_of: date, *, sim_run_id: str, **_kwargs: Any) -> _단계결과:
        self.받은축.append((as_of, sim_run_id))
        return _단계결과("NOTHING_DUE")


def test_하루를_돌리면_출고가_그_실행의_축을_받는다():
    """★ AST 의 짝 — 서명이 아니라 **실제로 흘러가는 값**을 잰다."""
    출고 = _출고대역()
    action = ScheduledAction(
        as_of=고른_날,
        now=datetime(2026, 9, 8, 9, 30, tzinfo=SEOUL),
        action="RUN_NOW",
        reason="검사",
        deadline=datetime(2026, 9, 8, 10, 30, tzinfo=SEOUL),
        ready_items=(),
        retry_after=None,
    )

    out = run_scheduled_day(
        action,
        sim_run_id=실행_나,
        open_day_fn=_단계대역("OPENED"),
        retry_fn=lambda _as_of, **_k: RetryOut(status="NOTHING_DUE", reason="없다"),
        receive_fn=_단계대역("RECEIVED"),
        issue_fn=_단계대역("ISSUED"),
        collect_fn=_단계대역("COLLECTED"),
        outbound_fn=출고,
        close_fn=_단계대역("CLOSED"),
        items=(),
    )

    assert 출고.받은축 == [(고른_날, 실행_나)], f"출고가 받은 축이 다르다: {출고.받은축}"
    assert out.outbound_status == "NOTHING_DUE", (
        f"출고 단계가 안 돌았다: {out.outbound_status} · {out.notes}"
    )


# ---------------------------------------------------------------------------
# ④ 조회가 거른다 — 파이썬이 아니다
# ---------------------------------------------------------------------------


def test_DB_가_남의_실행_행을_아예_안_읽어_온다():
    """★★ **① 과 다른 사실이다.**

    파이썬에서 걸러도 나가는 것은 같지만, 그러면 DB 가 남의 실행 행을 전부 읽어
    온다 — 커지면 느려지고, 무엇보다 *"조회가 정본"* 이 아니게 된다.
    """
    _, _, conn = _내보낸다(실행_가)

    올라온_축 = {행["sim_run_id"] for 행 in conn.커서.낸_행}

    assert conn.커서.낸_행, "조회가 한 행도 안 냈다 — 이 검사가 아무것도 안 재고 있다"
    assert 올라온_축 == {실행_가}, f"DB 가 남의 실행 행까지 읽어 왔다: {sorted(올라온_축)}"


def test_조회가_축을_조건으로_넘긴다():
    """★ 문장에 조건이 있고 **그 자리에 넘긴 값이 실린다.**"""
    conn = _연결(부딪히는_표)

    due_sale_items(conn, as_of=고른_날, sim_run_id=실행_가)

    where = _WHERE(conn.커서.문장)
    칸들 = [_칸(m.group(1)) for m in _등호.finditer(where)]

    assert "sim_run_id" in 칸들, f"WHERE 에 sim_run_id 조건이 없다: {where}"
    assert 실행_가 in conn.커서.넘긴값, f"축을 params 로 안 넘겼다: {conn.커서.넘긴값}"
    assert dict(zip(칸들, conn.커서.넘긴값, strict=False))["sim_run_id"] == 실행_가, (
        f"조건과 값의 차례가 어긋났다: {칸들} / {conn.커서.넘긴값}"
    )


def test_축을_읽어_두는_칸과_거르는_자리를_안_섞는다():
    """⚠️ `DueSaleItem.sim_run_id` 는 **되짚기용**이지 거르는 자리가 아니다.

    🔴 `_due_today` 는 날짜만 본다 — 축을 거기서 다시 거르면 *"조회가 정본"* 이
       두 벌이 되고, 한쪽만 고치는 날 둘이 갈린다.
    """
    원문 = inspect.getsource(outbound_flow._due_today)

    assert "sim_run_id" not in 원문, (
        f"_due_today 가 축을 다시 거른다 — 거르는 자리는 조회 하나다:\n{원문}"
    )
