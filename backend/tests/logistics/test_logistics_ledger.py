"""원장 기록의 **규율** 검사. DB 를 부르지 않는다.

가짜 커넥션·커서로 잰다. 여기서 재는 것은 값이 DB 에 들어갔는지가 아니라
(그건 `test_logistics_ledger_db.py` 가 실 DB 로 잰다) 물류가 소유한 규율이다.

```text
커밋·커넥션을 쥐지 않나      트랜잭션은 마스터 것이다
Lot 을 잠그고 시작하나       동시 출고가 잔량을 음수로 못 만든다
검사가 쓰기보다 앞이나       실패해도 Move 가 안 남는다
같은 move_id 를 두 번 받나   잔량이 두 번 변하면 안 된다
다른 사실이면 멈추나         어느 쪽도 고르지 않는다
상태를 건드리지 않나         DEPLETED/ACTIVE 판단은 입고·출고 단계 소유다
```
"""

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import ledger
from app.logistics.ledger import (
    InvalidMoveQuantity,
    LotNotFound,
    MoveIdConflict,
    MoveLine,
    MoveLineTotalMismatch,
    OriginalQuantityExceeded,
    RemainingQuantityInsufficient,
    UnsupportedMoveType,
    record_inventory_move,
)

MOVED_AT = date(2026, 1, 7)
SIM_RUN_ID = "SIM-BURNIN-202512"
LOT_ID = "LOT-KIMCHI-015-BAECHU"


@pytest.fixture(autouse=True)
def 스키마이름을_고정한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_db_schema()` 는 `DB_SCHEMA` 환경변수를 읽는다 — 여기서 끊는다.

    🔴 **환경변수에 기대면 단독 실행에서 깨진다.** `backend/.env` 는 없고 값은
       저장소 루트 `.env` 에 있어, 다른 테스트가 먼저 읽어 준 프로세스 환경에
       얹혀 가게 된다. `pytest tests/logistics` 만 돌리면 그 순간 빨간불이다
       (기존 `test_logistics_transition.py` · `test_logistics_day_open.py` 가
       실제로 그 상태다 — 이 검사는 그 함정을 따라가지 않는다).

    ★ 이름은 아무거나 좋다. 여기서 재는 것은 SQL 이 **어느 스키마를 가리키는가**가
      아니라 규율이고, DB 를 부르지도 않는다.
    """
    monkeypatch.setattr(ledger, "get_db_schema", lambda: "haetdeul")


#: `_existing_move` 가 읽는 칸 순서와 같아야 한다 — 가짜 커서가 튜플로 답하기 때문이다.
_MOVE_COLUMNS = (
    "sim_run_id",
    "lot_id",
    "sale_item_id",
    "move_type",
    "quantity_kg",
    "moved_at",
    "reason_code",
    "note",
)


class 가짜커서:
    """실행된 SQL 과 파라미터를 기록한다. 읽어 줄 값은 밖에서 정한다.

    ★ **어느 표를 물었는지로 답한다** — 순번으로 답하지 않는다. 실행 순서 자체가
      검사 대상이라(`test_원장_잠금이_기존_Move_조회보다_먼저다`), 대역이 순번을
      전제하면 **순서를 바꾼 코드가 대역까지 같이 바꿔야 통과**하게 되어 검사가
      무뎌진다.

    ⚠️ `inventory_moves` 와 `inventory_move_lines` 는 서로의 부분문자열이 아니다
       (`..._move_lines` 에는 `inventory_moves` 가 없다). Line 은 `fetchall` 로 나간다.
    """

    def __init__(
        self,
        lot_row: object,
        move_row: object,
        line_rows: list[tuple[Any, ...]],
        rowcount: int = 1,
    ) -> None:
        self.queries: list[str] = []
        self.params: list[Any] = []
        self.rowcount = rowcount
        self._lot_row = lot_row
        self._move_row = move_row
        self._line_rows = line_rows
        self._마지막 = ""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        self._마지막 = str(query)
        self.queries.append(self._마지막)
        self.params.append(params)

    def fetchone(self) -> object:
        if "inventory_lots" in self._마지막:
            return self._lot_row
        if "inventory_moves" in self._마지막:
            return self._move_row
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._line_rows)


class 가짜커넥션:
    """commit·rollback 이 **몇 번** 불렸나를 센다 — 둘 다 0 이어야 한다."""

    def __init__(
        self,
        *,
        remaining: str = "60",
        original: str = "100",
        lot_exists: bool = True,
        existing_move: dict[str, Any] | None = None,
        existing_lines: list[tuple[Any, ...]] | None = None,
        rowcount: int = 1,
    ) -> None:
        self.commits = 0
        self.rollbacks = 0
        lot_row = (Decimal(remaining), Decimal(original)) if lot_exists else None
        move_row = (
            None if existing_move is None else tuple(existing_move[name] for name in _MOVE_COLUMNS)
        )
        self.커서 = 가짜커서(lot_row, move_row, existing_lines or [], rowcount)

    def cursor(self) -> 가짜커서:
        return self.커서

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _기록(conn: 가짜커넥션, **바꿀것: Any):
    인자: dict[str, Any] = {
        "move_id": "MOVE-TEST-1",
        "sim_run_id": SIM_RUN_ID,
        "lot_id": LOT_ID,
        "move_type": "OUT",
        "quantity_kg": Decimal(20),
        "moved_at": MOVED_AT,
        "reason_code": "SALE_FULFILLMENT",
    }
    인자.update(바꿀것)
    return record_inventory_move(conn, **인자)


def _사실(**바꿀것: Any) -> dict[str, Any]:
    사실: dict[str, Any] = {
        "sim_run_id": SIM_RUN_ID,
        "lot_id": LOT_ID,
        "sale_item_id": None,
        "move_type": "OUT",
        "quantity_kg": Decimal("20.000000"),
        "moved_at": MOVED_AT,
        "reason_code": "SALE_FULFILLMENT",
        "note": None,
    }
    사실.update(바꿀것)
    return 사실


def _is_write(query: str) -> bool:
    """쓰기 질의인가.

    ⚠️ `"UPDATE" in query` 로 재면 안 된다 — Lot **잠금**이 `SELECT ... FOR UPDATE` 라
       읽기가 쓰기로 잡힌다. 잠금 문구를 걷어내고 본다.
    """
    return "INSERT" in query or "UPDATE" in query.replace("FOR UPDATE", "")


def _쓰기(conn: 가짜커넥션) -> list[str]:
    return [q for q in conn.커서.queries if _is_write(q)]


# ── 트랜잭션 경계 ────────────────────────────────────────────────────────


def test_원장_기록은_커밋도_롤백도_하지_않는다():
    """🔴 커밋하면 재고만 먼저 확정된 반쪽 장부가 남는다.

    마스터가 재무 write 와 **한 번에** 커밋할 수 있어야 한다.
    """
    conn = 가짜커넥션()

    _기록(conn)

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_모듈이_자기_커넥션을_열지_않는다():
    """★ `db.get_connection` · `fetch_all` · `execute_returning_one` 을 쓰지 않는다.

    그것들은 자기 커넥션을 연다 — 마스터가 쥔 트랜잭션 밖에서 쓰게 되어
    `persist_inventory` 가 피한 반쪽 상태가 다시 생긴다.

    ⚠️ docstring 은 빼고 본다. 왜 안 쓰는지를 **설명하는 문장**에 그 이름들이 나오고,
       설명과 코드는 다른 것이다.
    """
    source = Path(ledger.__file__).read_text(encoding="utf-8")
    module_docstring = ast.get_docstring(ast.parse(source), clean=False)
    assert module_docstring is not None, "모듈 docstring 이 없다 — 왜 이 규율인지가 안 적혀 있다."
    code = source.replace(module_docstring, "", 1)

    assert "get_connection" not in code
    assert "fetch_all" not in code
    assert "fetch_one" not in code
    assert "execute_returning_one" not in code


# ── 잠금과 순서 ──────────────────────────────────────────────────────────


def _순번(conn: 가짜커넥션, 조각: str) -> int:
    """그 조각을 담은 첫 질의의 순번. 없으면 검사를 세운다."""
    for index, query in enumerate(conn.커서.queries):
        if 조각 in query:
            return index
    raise AssertionError(f"{조각!r} 를 담은 질의가 없다: {conn.커서.queries}")


def test_Lot_을_FOR_UPDATE_로_잠근다():
    """🔴 없으면 두 출고가 같은 잔량을 읽고 각자 빼서 합계가 음수가 된다."""
    conn = 가짜커넥션()

    _기록(conn)

    잠금 = conn.커서.queries[_순번(conn, "FOR UPDATE")]
    assert "inventory_lots" in 잠금
    assert conn.커서.params[_순번(conn, "FOR UPDATE")] == (LOT_ID, SIM_RUN_ID)


def test_읽기가_쓰기보다_먼저다():
    """원장 잠금 · 기존 Move 조회 · Lot 잠금 → 그다음이 쓰기다."""
    conn = 가짜커넥션()

    _기록(conn)

    앞의셋 = conn.커서.queries[:3]
    assert all("SELECT" in q for q in 앞의셋), conn.커서.queries
    assert not any(_is_write(q) for q in 앞의셋), conn.커서.queries
    assert any("INSERT" in q for q in conn.커서.queries[3:]), "그다음이 쓰기다"


def test_원장_잠금이_기존_Move_조회보다_먼저다():
    """🔴 이 순서가 무너지면 **같은 move_id + 다른 Lot** 경합이 다시 열린다.

    Lot row lock 은 서로 다른 행을 잠그므로 둘을 못 세운다 — 둘 다 *"기존 Move 없음"*
    으로 판단하고 같은 PK 를 INSERT 하고, 뒤엣것이 `MoveIdConflict` 가 아니라 raw
    `UniqueViolation` 으로 터져 **바깥 트랜잭션이 aborted** 가 된다.

    ★ 잠금이 **가장 먼저**여야 하는 것도 계약이다. 기다리는 쪽이 아직 아무 자원도 안
      쥐고 있어야 순환이 생길 자리가 없다.
    """
    conn = 가짜커넥션()

    _기록(conn)

    assert _순번(conn, "pg_advisory_xact_lock") == 0, "가장 먼저다"
    assert _순번(conn, "pg_advisory_xact_lock") < _순번(conn, "inventory_moves")
    assert _순번(conn, "inventory_moves") < _순번(conn, "FOR UPDATE")


def test_원장_잠금은_트랜잭션_수명이고_해제하지_않는다():
    """★ session-level 을 쓰면 풀어 줄 주인이 없어 커넥션에 잠금이 눌어붙는다.

    바깥 트랜잭션의 커밋/롤백과 함께 자동으로 풀려야 하므로 `_xact_` 판이어야 하고,
    이 모듈은 unlock 을 부르지 않는다.
    """
    conn = 가짜커넥션()

    _기록(conn)

    assert "pg_advisory_xact_lock" in conn.커서.queries[0]
    code = _코드만()
    assert "pg_advisory_lock(" not in code, "session-level 잠금을 쓰지 않는다"
    assert "pg_advisory_unlock" not in code, "직접 풀지 않는다"


def test_원장_잠금은_move_id_와_무관한_하나의_전역_잠금이다():
    """🔴 `move_id` 별 잠금은 **교착을 못 막았다.**

    ```text
    T1  record(MOVE-1, LOT-A)   adv(MOVE-1) · LOT-A row lock 획득
    T2  record(MOVE-2, LOT-A)   adv(MOVE-2) 획득 · LOT-A 를 기다린다
    T1  record(MOVE-2, LOT-A)   adv(MOVE-2) 를 기다린다   → 교착
    ```

    T2 는 `MOVE-1` 을 **요청한 적이 없어** 정렬로 전순서를 만들 수 없다. 잠금을 하나로
    합치면 그 순환이 성립하지 않는다.
    """
    첫번째 = 가짜커넥션()
    두번째 = 가짜커넥션()

    _기록(첫번째, move_id="MOVE-AAA")
    _기록(두번째, move_id="MOVE-ZZZ")

    assert 첫번째.커서.params[0] == 두번째.커서.params[0], (
        "move_id 가 달라도 같은 잠금을 잡아야 한다 — 그래야 전순서가 선다"
    )
    assert 첫번째.커서.params[0] == (ledger._LEDGER_LOCK_CLASSID, ledger._LEDGER_LOCK_OBJID)


def test_잠금_구현_상세를_공개_API_로_내보내지_않는다():
    """★ 잠금은 기술적 동시성 장치지 물류 업무 API 가 아니다."""
    assert "move_lock_key" not in ledger.__all__
    assert not hasattr(ledger, "move_lock_key")
    assert ledger.__all__ == sorted(ledger.__all__), "정렬을 유지한다"
    공개 = {이름 for 이름 in ledger.__all__}
    assert "record_inventory_move" in 공개
    assert {"MoveLine", "LedgerResult"} <= 공개
    assert not any(이름.startswith("_") for 이름 in 공개)
    assert not any("lock" in 이름.lower() for 이름 in 공개), "잠금 이름이 새지 않는다"


def test_기존_Move_가_없는_Lot_을_가리키면_LotNotFound_가_아니라_Conflict_다():
    """🔴 *"같은 move_id 인데 없는 Lot"* 은 **부재가 아니라 멱등 키 충돌**이다.

    Lot 을 먼저 잠그면 `LotNotFound` 가 나가고, 부르는 쪽은 *"Lot 을 만들어야 하나"* 로
    읽는다. 실제로는 그 `move_id` 가 이미 다른 Lot 에 쓰였다는 뜻이다.
    """
    conn = 가짜커넥션(lot_exists=False, existing_move=_사실())

    with pytest.raises(MoveIdConflict) as 오류:
        _기록(conn, lot_id="LOT-NOT-EXIST")

    assert "lot_id" in str(오류.value)
    assert _쓰기(conn) == []


def test_기존_Move_가_다른_존재하는_Lot_을_가리켜도_Conflict_다():
    conn = 가짜커넥션(existing_move=_사실())

    with pytest.raises(MoveIdConflict) as 오류:
        _기록(conn, lot_id="LOT-OTHER")

    assert "lot_id" in str(오류.value)
    assert _쓰기(conn) == []


def test_멱등_재시도는_Lot_에_행_잠금을_걸지_않는다():
    """★ 바꿀 것이 없는데 쓰기 잠금을 잡으면 교착이 날 면적만 넓어진다."""
    conn = 가짜커넥션(remaining="40", existing_move=_사실())

    result = _기록(conn)

    assert result.applied is False
    assert result.remaining_qty_kg == Decimal(40), "현재 잔량은 읽어서 돌려준다"
    assert not any("FOR UPDATE" in q for q in conn.커서.queries)


def _코드만() -> str:
    """`ledger.py` 에서 docstring 과 주석을 걷어낸 **실행되는 부분**.

    ⚠️ 이 모듈은 *"왜 그것을 안 쓰는지"* 를 설명하는 문장이 많아, 원문을 그대로 뒤지면
       **설명이 코드로 잡힌다.** 설명과 코드는 다른 것이고, 잠가야 할 것은 후자다.
    """
    source = Path(ledger.__file__).read_text(encoding="utf-8")
    죽일줄: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            죽일줄.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return "\n".join(
        줄.split("#", 1)[0]
        for 번호, 줄 in enumerate(source.splitlines(), start=1)
        if 번호 not in 죽일줄
    )


def test_UniqueViolation_을_제어_흐름으로_쓰지_않는다():
    """🔴 "일단 INSERT 하고 PK 위반을 잡는" 방식은 트랜잭션을 aborted 로 만든다.

    Savepoint 없이 그 방식을 쓰면 바깥 마스터 트랜잭션이 통째로 죽는다 — 그래서
    **먼저 직렬화하고 조회해서 판단**한다.
    """
    code = _코드만()

    assert "ON CONFLICT" not in code
    assert "UniqueViolation" not in code
    assert "except" not in code, "예외를 잡아 흐름으로 쓰지 않는다"
    assert "SAVEPOINT" not in code.upper(), "경계를 쥐는 것은 마스터다"


def test_Lot_이_없으면_만들지_않고_멈춘다():
    """Lot 생성은 입고 단계 소유다 — 원장이 지어내지 않는다."""
    conn = 가짜커넥션(lot_exists=False)

    with pytest.raises(LotNotFound):
        _기록(conn)

    assert _쓰기(conn) == []


# ── 수량 규칙: 검사가 쓰기보다 앞이다 ─────────────────────────────────────


def test_OUT_은_잔량을_뺀_값을_돌려준다():
    conn = 가짜커넥션(remaining="60")

    result = _기록(conn, quantity_kg=Decimal(20))

    assert result.applied is True
    assert result.remaining_qty_kg == Decimal(40)


def test_IN_은_잔량을_더한_값을_돌려준다():
    conn = 가짜커넥션(remaining="0", original="100")

    result = _기록(conn, move_type="IN", quantity_kg=Decimal(60), reason_code="PURCHASE_RECEIPT")

    assert result.applied is True
    assert result.remaining_qty_kg == Decimal(60)


def test_초과_OUT_은_Move_를_남기지_않는다():
    """🔴 검사가 INSERT 뒤로 가면 "Move 는 남고 잔량은 그대로" 가 만들어진다."""
    conn = 가짜커넥션(remaining="40")

    with pytest.raises(RemainingQuantityInsufficient):
        _기록(conn, quantity_kg=Decimal(41))

    assert _쓰기(conn) == []


def test_최초수량을_넘는_IN_은_Move_를_남기지_않는다():
    """DB CHECK `remaining <= original` 을 우회하지 않고 그 앞에서 막는다."""
    conn = 가짜커넥션(remaining="60", original="100")

    with pytest.raises(OriginalQuantityExceeded):
        _기록(conn, move_type="IN", quantity_kg=Decimal(50))

    assert _쓰기(conn) == []


@pytest.mark.parametrize("수량", [Decimal(0), Decimal(-1)])
def test_양수가_아닌_수량은_거절한다(수량: Decimal):
    conn = 가짜커넥션()

    with pytest.raises(InvalidMoveQuantity):
        _기록(conn, quantity_kg=수량)

    assert conn.커서.queries == [], "읽기조차 하기 전에 멈춘다"


def test_float_수량은_거절한다():
    """🔴 `Decimal(float)` 은 이진 오차를 원장에 영원히 남긴다."""
    conn = 가짜커넥션()

    with pytest.raises(InvalidMoveQuantity):
        _기록(conn, quantity_kg=0.1)


def test_int_수량은_정상_변환된다():
    """정확한 값이라 받는다 — 이 동작을 고정해 둔다 (float 과 가르는 자리다)."""
    conn = 가짜커넥션(remaining="60")

    result = _기록(conn, quantity_kg=20)

    assert result.remaining_qty_kg == Decimal(40)


@pytest.mark.parametrize("문자", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_유한하지_않은_수량은_거절한다(문자: str):
    """🔴 우리 예외로 막는다 — `decimal.InvalidOperation` 도 DB 오류도 밖으로 새면 안 된다.

    `Decimal("NaN") <= 0` 은 신호를 내고 `sNaN` 은 더해 보기만 해도 낸다. 그래서
    유한성 검사가 부호 검사보다 **먼저** 있어야 한다.
    """
    conn = 가짜커넥션()

    with pytest.raises(InvalidMoveQuantity):
        _기록(conn, quantity_kg=Decimal(문자))

    assert conn.커서.queries == [], "읽기조차 하기 전에 멈춘다"


@pytest.mark.parametrize("문자", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_Line_수량도_유한해야_한다(문자: str):
    conn = 가짜커넥션()

    with pytest.raises(InvalidMoveQuantity):
        _기록(conn, lines=[MoveLine(quantity_kg=Decimal(문자))])

    assert conn.커서.queries == []


def test_실행하지_않는_Move_Type_은_거절한다():
    """DISPOSE · ADJUST 는 DB 어휘로만 남아 있고 업무 규칙이 정해지지 않았다."""
    conn = 가짜커넥션()

    with pytest.raises(UnsupportedMoveType):
        _기록(conn, move_type="DISPOSE")

    assert conn.커서.queries == []


# ── 멱등: 같은 move_id ───────────────────────────────────────────────────


def test_같은_사실의_재실행은_잔량을_다시_바꾸지_않는다():
    """★ 실패가 아니라 **이미 반영됨**이다. 둘을 못 가리면 재시도가 잔량을 두 번 바꾼다."""
    conn = 가짜커넥션(remaining="40", existing_move=_사실())

    result = _기록(conn)

    assert result.applied is False
    assert result.remaining_qty_kg == Decimal(40)
    assert _쓰기(conn) == []


def test_이미_반영된_OUT_의_재실행이_초과판정으로_뒤집히지_않는다():
    """🔴 중복 검사가 수량 검사보다 **먼저**여야 성립한다.

    OUT 60 이 반영돼 잔량이 0 인 상태에서 같은 Move 를 다시 보내면, 순서가 뒤면
    "60 > 0" 으로 정상 재시도가 오류가 된다.
    """
    conn = 가짜커넥션(
        remaining="0", original="60", existing_move=_사실(quantity_kg=Decimal("60.000000"))
    )

    result = _기록(conn, quantity_kg=Decimal(60))

    assert result.applied is False
    assert result.remaining_qty_kg == Decimal(0)


@pytest.mark.parametrize(
    ("칸", "다른값"),
    [
        ("quantity_kg", Decimal(21)),
        ("move_type", "IN"),
        ("lot_id", "LOT-OTHER"),
        ("moved_at", date(2026, 1, 8)),
        ("reason_code", "SAFETY_STOCK_ROTATION"),
        ("sale_item_id", "SI-1"),
        ("note", "다른 설명"),
        ("sim_run_id", "SIM-OTHER"),
    ],
)
def test_같은_id_에_다른_사실이면_멈춘다(칸: str, 다른값: Any):
    """어느 쪽이 진짜인지 여기서 고르지 않는다 — 덮어쓰면 이전 사실이 조용히 사라진다."""
    conn = 가짜커넥션(existing_move=_사실(**{칸: 다른값}))

    with pytest.raises(MoveIdConflict) as 오류:
        _기록(conn)

    assert 칸 in str(오류.value), "무엇이 다른지 이름이 보여야 한다"
    assert _쓰기(conn) == []


# ── 멱등: Move Line 도 사실이다 ──────────────────────────────────────────
#
# 🔴 Header 만 대조하면 *"같은 20kg 인데 다른 Pallet 에서 나갔다"* 가 에러 없이 삼켜지고,
#    두 번째 요청의 Line 은 장부 어디에도 안 남는다.

#: Header 20kg 를 두 판으로 나눈 정상 묶음. Line 대조 검사들의 공통 출발점이다.
_두판 = [
    MoveLine(quantity_kg=Decimal(12), pallet_id="P-001", location_id="LOC-A", note="첫 판"),
    MoveLine(quantity_kg=Decimal(8), pallet_id="P-002", location_id="LOC-B"),
]
#: 위와 같은 사실을 DB 가 돌려주는 모양 — numeric(18,6) 왕복까지 흉내 낸다.
_두판_기존 = [
    ("P-001", "LOC-A", Decimal("12.000000"), "첫 판"),
    ("P-002", "LOC-B", Decimal("8.000000"), None),
]


def test_Header_와_Line_이_모두_같으면_정상_재시도다():
    """`Decimal("12")` ↔ `Decimal("12.000000")` 도 같은 수량이다 (DB 왕복)."""
    conn = 가짜커넥션(remaining="40", existing_move=_사실(), existing_lines=_두판_기존)

    result = _기록(conn, lines=_두판)

    assert result.applied is False
    assert result.remaining_qty_kg == Decimal(40)
    assert _쓰기(conn) == []


def test_Line_순서만_다른_것은_같은_재시도다():
    """★ 순서는 업무 사실이 아니다 — 같은 물건을 다른 차례로 적었을 뿐이다."""
    conn = 가짜커넥션(remaining="40", existing_move=_사실(), existing_lines=_두판_기존)

    result = _기록(conn, lines=list(reversed(_두판)))

    assert result.applied is False
    assert _쓰기(conn) == []


@pytest.mark.parametrize(
    ("무엇", "요청Line"),
    [
        (
            "pallet_id",
            [
                MoveLine(
                    quantity_kg=Decimal(12), pallet_id="P-999", location_id="LOC-A", note="첫 판"
                ),
                MoveLine(quantity_kg=Decimal(8), pallet_id="P-002", location_id="LOC-B"),
            ],
        ),
        (
            "location_id",
            [
                MoveLine(
                    quantity_kg=Decimal(12), pallet_id="P-001", location_id="LOC-Z", note="첫 판"
                ),
                MoveLine(quantity_kg=Decimal(8), pallet_id="P-002", location_id="LOC-B"),
            ],
        ),
        (
            "quantity_kg",
            [
                MoveLine(
                    quantity_kg=Decimal(15), pallet_id="P-001", location_id="LOC-A", note="첫 판"
                ),
                MoveLine(quantity_kg=Decimal(5), pallet_id="P-002", location_id="LOC-B"),
            ],
        ),
        (
            "note",
            [
                MoveLine(
                    quantity_kg=Decimal(12),
                    pallet_id="P-001",
                    location_id="LOC-A",
                    note="다른 설명",
                ),
                MoveLine(quantity_kg=Decimal(8), pallet_id="P-002", location_id="LOC-B"),
            ],
        ),
    ],
)
def test_Line_사실이_다르면_멈춘다(무엇: str, 요청Line: list[MoveLine]):
    conn = 가짜커넥션(remaining="40", existing_move=_사실(), existing_lines=_두판_기존)

    with pytest.raises(MoveIdConflict) as 오류:
        _기록(conn, lines=요청Line)

    assert "Move Line" in str(오류.value), f"{무엇} 차이가 Line 대조로 잡혀야 한다"
    assert _쓰기(conn) == []


def test_기존_Line_0건인데_요청에_Line_이_있으면_멈춘다():
    conn = 가짜커넥션(remaining="40", existing_move=_사실(), existing_lines=[])

    with pytest.raises(MoveIdConflict):
        _기록(conn, lines=_두판)

    assert _쓰기(conn) == []


def test_기존_Line_이_있는데_요청이_0건이면_멈춘다():
    conn = 가짜커넥션(remaining="40", existing_move=_사실(), existing_lines=_두판_기존)

    with pytest.raises(MoveIdConflict):
        _기록(conn)

    assert _쓰기(conn) == []


def test_같은_Line_의_중복_개수가_다르면_멈춘다():
    """★ 집합으로 보면 뭉개진다 — `[P1 10, P1 10]` 과 `[P1 10]` 은 다른 사실이다."""
    conn = 가짜커넥션(
        remaining="40",
        existing_move=_사실(quantity_kg=Decimal("20.000000")),
        existing_lines=[
            ("P-001", "LOC-A", Decimal("10.000000"), None),
            ("P-001", "LOC-A", Decimal("10.000000"), None),
        ],
    )

    with pytest.raises(MoveIdConflict) as 오류:
        _기록(
            conn,
            quantity_kg=Decimal(20),
            lines=[MoveLine(quantity_kg=Decimal(20), pallet_id="P-001", location_id="LOC-A")],
        )

    assert "Line 수: 기존=2 요청=1" in str(오류.value)
    assert _쓰기(conn) == []


def test_Line_0건끼리는_같은_재시도다():
    """Pallet 확정 전 입고가 그 상태다 — 양쪽 다 0건이면 정상 멱등이다."""
    conn = 가짜커넥션(remaining="40", existing_move=_사실(), existing_lines=[])

    result = _기록(conn)

    assert result.applied is False
    assert _쓰기(conn) == []


def test_중복_판정에_새_컬럼을_쓰지_않는다():
    """★ `inventory_moves_pkey` 가 이미 `move_id` 다 — 마이그레이션이 필요 없다."""
    source = Path(ledger.__file__).read_text(encoding="utf-8")

    assert "idempotency" not in source.lower()
    assert "ALTER TABLE" not in source
    assert "CREATE TABLE" not in source


# ── Move Line ────────────────────────────────────────────────────────────


def test_Line_없이도_Move_는_기록된다():
    """Pallet 확정 전 입고가 그 상태다 — Line 0 건은 정상이다."""
    conn = 가짜커넥션()

    result = _기록(conn)

    assert result.line_count == 0
    assert not any("inventory_move_lines" in q for q in conn.커서.queries)


def test_Line_이_있으면_Header_와_같은_Lot_으로_기록한다():
    conn = 가짜커넥션(remaining="60")

    result = _기록(
        conn,
        quantity_kg=Decimal(20),
        lines=[MoveLine(quantity_kg=Decimal(20), pallet_id="PLT-1")],
    )

    assert result.line_count == 1
    line_params = next(
        params
        for query, params in zip(conn.커서.queries, conn.커서.params, strict=True)
        if "inventory_move_lines" in query
    )
    assert line_params[1] == LOT_ID, "Line 의 Lot 은 Header 것을 그대로 쓴다"
    assert line_params[2] == "PLT-1"


def test_Line_합계가_Header_와_다르면_멈춘다():
    """`v_move_line_integrity` 가 사후에 잡을 상태를 애초에 못 만들게 한다."""
    conn = 가짜커넥션(remaining="60")

    with pytest.raises(MoveLineTotalMismatch):
        _기록(
            conn,
            quantity_kg=Decimal(20),
            lines=[MoveLine(quantity_kg=Decimal(12)), MoveLine(quantity_kg=Decimal(5))],
        )

    assert conn.커서.queries == [], "읽기조차 하기 전에 멈춘다"


def test_Pallet_이나_Location_을_지어내지_않는다():
    """없는 id 는 FK 가 막는다 — 코드가 만들어 채우지 않는다."""
    source = Path(ledger.__file__).read_text(encoding="utf-8")

    assert "INSERT INTO {}.pallets" not in source
    assert "storage_locations" not in source


# ── 상태 ─────────────────────────────────────────────────────────────────


def test_Lot_상태를_건드리지_않는다():
    """★ 잔량 0 이어도 DEPLETED 로 바꾸지 않고, IN 이 와도 ACTIVE 로 되돌리지 않는다.

    🔴 상태 결정은 입고·출고 단계 소유다. 여기서 한쪽만 정하면 그 임의 정책이
       사실로 굳는다 (미결 사항으로 보고했다).
    """
    conn = 가짜커넥션(remaining="20")

    result = _기록(conn, quantity_kg=Decimal(20))

    assert result.remaining_qty_kg == Decimal(0)
    update = next(q for q in conn.커서.queries if _is_write(q) and "inventory_lots" in q)
    assert "status" not in update
    assert "DEPLETED" not in update


def test_잔량과_원장을_같은_커서에서_쓴다():
    """둘 중 하나만 바뀌는 경로를 만들지 않는 것이 이 모듈의 존재 이유다."""
    conn = 가짜커넥션()

    _기록(conn)

    쓰기 = _쓰기(conn)
    assert any("INSERT INTO" in q and "inventory_moves" in q for q in 쓰기)
    assert any("INSERT" not in q and "inventory_lots" in q for q in 쓰기)


def test_잠근_행과_쓰는_행이_갈리면_멈춘다():
    """rowcount 0 은 잠금 조건과 쓰기 조건이 갈렸다는 뜻이다 — 조용히 지나가지 않는다."""
    conn = 가짜커넥션(rowcount=0)

    with pytest.raises(LotNotFound):
        _기록(conn)
