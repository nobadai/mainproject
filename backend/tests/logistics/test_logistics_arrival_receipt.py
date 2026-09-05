"""입고 Receipt **존재 조회**의 규율 검사 (3-B4-C). DB 를 부르지 않는다.

가짜 커넥션·커서로 잰다. 여기서 재는 것은 값이 DB 에 있는지가 아니라 **물류가
소유한 규율 다섯**이다.

```text
열쇠가 유일성 축과 같나      (sim_run_id, inbound_id) 로만 묻는다
0 · 1 · 2+ 를 가르나         첫 행을 조용히 고르지 않는다
1건이 실패가 아닌가          멱등 재실행은 정상이다
아무것도 안 쓰나             INSERT · UPDATE · DELETE · commit · rollback 이 없다
커넥션을 안 여나             바깥 트랜잭션에 얹혀 간다
```
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import receipts
from app.logistics.receipts import (
    InvalidInboundIdentity,
    ReceiptExistence,
    ReceiptIntegrityError,
    check_receipt_state,
)

SIM_RUN_ID = "SIM-BURNIN-202512"
INBOUND_ID = "INB-H1-THRU-20260105-BAECHU-1-1"


@pytest.fixture(autouse=True)
def 스키마이름을_고정한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_db_schema()` 는 `DB_SCHEMA` 환경변수를 읽는다 — 여기서 끊는다.

    🔴 환경변수에 기대면 `pytest tests/logistics` 단독 실행에서 깨진다
       (`test_logistics_ledger.py` 가 같은 함정을 피하는 방식 그대로다).
    """
    monkeypatch.setattr(receipts, "get_db_schema", lambda: "haetdeul")


class 가짜커서:
    """작은 `inbound_receipts` 표를 들고 **파라미터로 실제로 거른다.**

    ★ SQL 문자열만 보는 검사는 *"열쇠가 맞나"* 를 못 잰다. 여기서는 넘어온 두 값으로
      진짜 필터를 걸어, 다른 실행의 같은 `inbound_id` 가 섞이지 않는지 **행동으로**
      확인한다.
    """

    def __init__(self, table: list[dict[str, str]], log: list[Any], *, 매핑행: bool) -> None:
        self._table = table
        self._매핑행 = 매핑행
        self.log = log
        self._rows: list[Any] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        self.log.append((str(query), params))
        assert isinstance(params, tuple), "열쇠 두 값을 튜플로 넘겨야 한다"
        sim_run_id, inbound_id = params
        matched = sorted(
            (
                row
                for row in self._table
                if row["sim_run_id"] == sim_run_id and row["inbound_id"] == inbound_id
            ),
            key=lambda row: row["receipt_id"],
        )
        self._rows = [
            dict(row) if self._매핑행 else (row["receipt_id"],)  # type: ignore[misc]
            for row in matched
        ]

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        raise AssertionError(
            "fetchone 을 쓰면 2건 이상이 조용히 첫 행으로 나간다 — 무결성 위반이"
            " 정상 응답이 되는 자리다"
        )


class 가짜커넥션:
    """커서를 **몇 번** 열었나, 커밋·롤백이 불렸나를 센다."""

    def __init__(self, table: list[dict[str, str]] | None = None, *, 매핑행: bool = False) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.log: list[Any] = []
        self.커서들: list[가짜커서] = []
        self._table = table or []
        self._매핑행 = 매핑행

    def cursor(self) -> 가짜커서:
        cur = 가짜커서(self._table, self.log, 매핑행=self._매핑행)
        self.커서들.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def _행(receipt_id: str, *, sim_run_id: str = SIM_RUN_ID, inbound_id: str = INBOUND_ID) -> dict:
    return {"receipt_id": receipt_id, "sim_run_id": sim_run_id, "inbound_id": inbound_id}


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"INSERT 를 하지 않는다"* 고 **설명하는 문장**이 쓰기로
       잡힌다. 설명과 실행문은 다른 것이고, 잠가야 할 것은 후자다.

    ★ 문자열 리터럴은 남긴다 — SQL 이 문자열 안에 있어서다.
    """
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                코드 = 코드.replace(docstring, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _원문() -> str:
    return Path(receipts.__file__).read_text(encoding="utf-8")


# ── 1~4. 0 · 1 · 2+ 를 가른다 ───────────────────────────────────────────


def test_1_행이_없으면_NEW_다():
    """★ *"아직 Receipt 가 없다"* — 뒤 단계가 만들 수 있다는 뜻이지 만들지는 않는다."""
    conn = 가짜커넥션([])

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과 == ReceiptExistence(status="NEW", receipt_id=None)


def test_2_한_건이면_ALREADY_EXISTS_다():
    """🔴 **실패가 아니다.** 멱등 재실행은 정상이고, 예외로 표현하면 바깥이 롤백한다.

    ★ `ledger.record_inventory_move` 가 같은 `move_id` 에 `applied=False` 를 돌려주는
      것과 같은 자리다.
    """
    conn = 가짜커넥션([_행("RCP-1")])

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과.status == "ALREADY_EXISTS"


def test_3_기존_receipt_id_를_그대로_돌려준다():
    """★ 권위 있는 값은 DB 에 있는 그것이다 — 새로 짓지 않는다."""
    conn = 가짜커넥션([_행("RCP-THRU-20260105-0001")])

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과.receipt_id == "RCP-THRU-20260105-0001"


@pytest.mark.parametrize("매핑행", [False, True], ids=["튜플행", "매핑행"])
def test_3b_row_factory_가_무엇이든_같은_답이다(매핑행: bool):
    """★ 커넥션을 만드는 곳은 배선 자리다 — 이 모듈이 row_factory 를 강요하지 않는다."""
    conn = 가짜커넥션([_행("RCP-1")], 매핑행=매핑행)

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과 == ReceiptExistence(status="ALREADY_EXISTS", receipt_id="RCP-1")


def test_4_두_건_이상이면_무결성_오류다():
    """🔴 **어느 것도 고르지 않는다.** 최신도 첫 행도 고르지 않고, 조용히 합치지도 않는다.

    ★ `repository` 가 활성 fixture 2건에, `transition._index_by_inbound_id` 가 중복
      `inbound_id` 에 하는 일과 같다 — 깨진 상태 위에서 계속 걷지 않는다.
    """
    conn = 가짜커넥션([_행("RCP-B"), _행("RCP-A")])

    with pytest.raises(ReceiptIntegrityError) as 오류:
        check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    메시지 = str(오류.value)
    assert INBOUND_ID in 메시지
    assert "RCP-A" in 메시지 and "RCP-B" in 메시지, "부딪힌 id 가 보여야 조사할 수 있다"


def test_4b_손상_메시지가_결정적이다():
    """★ 같은 손상 상태를 두 번 물으면 같은 문장이 나온다 (`ORDER BY receipt_id`)."""
    바로 = 가짜커넥션([_행("RCP-B"), _행("RCP-A")])
    거꾸로 = 가짜커넥션([_행("RCP-A"), _행("RCP-B")])

    메시지 = []
    for conn in (바로, 거꾸로):
        with pytest.raises(ReceiptIntegrityError) as 오류:
            check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)
        메시지.append(str(오류.value))

    assert 메시지[0] == 메시지[1]


def test_4c_조회를_두_건까지로_묶는다():
    """★ 셋을 가르는 데 그 이상이 필요 없다 — 어차피 어느 하나도 고르지 않는다."""
    conn = 가짜커넥션([])

    check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    질의 = conn.log[0][0]
    assert "LIMIT" in 질의 and "Literal(2)" in 질의


# ── 5~6. 열쇠는 유일성 축과 같다 ────────────────────────────────────────


def test_5_정확히_두_값으로_묻는다():
    """🔴 DB 의 `uq_inbound_receipts_inbound_id UNIQUE (sim_run_id, inbound_id)` 와
    **같은 축**이어야 한다 — 다르면 *"있다/없다"* 와 *"두 번 못 선다"* 가 다른 것을 뜻한다.
    """
    conn = 가짜커넥션([])

    check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    질의, 파라미터 = conn.log[0]
    assert 파라미터 == (SIM_RUN_ID, INBOUND_ID), "열쇠는 두 값뿐이다"
    assert "sim_run_id = %s" in 질의
    assert "inbound_id = %s" in 질의
    assert "inbound_receipts" in 질의


def test_5b_매입_참조나_품목으로_찾지_않는다():
    """🔴 그것들은 Receipt 의 정체성이 아니다.

    ★ 매입 참조로 찾으면 회차가 여럿인 매입 하나에 여러 입고가 달릴 때 **남의 건을
      자기 것으로** 본다.
    """
    코드 = _코드만(_원문())

    for 금지 in ("purchase_id", "approval_id", "item_id", "expected_arrival_date", "arrived_at"):
        assert 금지 not in 코드, f"{금지} 로 Receipt 를 찾고 있다"


def test_6_다른_실행의_같은_inbound_id_는_세지_않는다():
    """★ `sim_run_id` 로 범위를 잡는 이유다 — 다른 시뮬레이션이 같은 `inbound_id` 를
    갖는 것은 정상이다.
    """
    conn = 가짜커넥션([_행("RCP-OTHER", sim_run_id="SIM-DIFFERENT")])

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과.status == "NEW"


def test_6b_같은_실행의_다른_inbound_id_도_세지_않는다():
    conn = 가짜커넥션([_행("RCP-OTHER", inbound_id="INB-OTHER-9")])

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과.status == "NEW"


# ── 7~8. 없는 열쇠로 묻지 않는다 ────────────────────────────────────────


@pytest.mark.parametrize("빈값", ["", "   ", "\t", "\n"], ids=["빈문자열", "공백", "탭", "줄바꿈"])
def test_7_빈_inbound_id_는_DB_에_묻기도_전에_막힌다(빈값: str):
    """🔴 **없는 것과 물어보지 못한 것은 다른 사실이다.**

    없는 열쇠로 물으면 0건이 돌아오고 그 0건은 *"아직 Receipt 가 없다"* 로 읽힌다 —
    그러면 뒤 단계가 쓰레기 식별자로 행을 만들어 UNIQUE 축을 오염시킨다
    (`inbound_id` 가 nullable 이라 DB 도 그것을 안 막는다).
    """
    conn = 가짜커넥션([])

    with pytest.raises(InvalidInboundIdentity):
        check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=빈값)

    assert conn.log == [], "질의를 보내기 전에 멈춰야 한다"
    assert conn.커서들 == [], "커서도 열지 않는다"


@pytest.mark.parametrize("빈값", ["", "   "], ids=["빈문자열", "공백"])
def test_8_빈_sim_run_id_도_막힌다(빈값: str):
    """★ 열쇠는 두 값이 **함께**여야 유일성 축이 된다."""
    conn = 가짜커넥션([])

    with pytest.raises(InvalidInboundIdentity):
        check_receipt_state(conn, sim_run_id=빈값, inbound_id=INBOUND_ID)

    assert conn.log == []


def test_8b_도착_후보_선택과_같은_눈으로_본다():
    """★ `arrival` 이 공백뿐인 식별자를 `blocked` 로 거르고, 여기서도 막는다 —
    두 단계가 같은 것을 *"없다"* 로 봐야 한 쪽이 통과시킨 값이 다른 쪽에서 터지지 않는다.
    """
    from datetime import date

    from app.logistics.arrival import select_due_inbound
    from app.logistics.schemas import InTransitItem

    선택 = select_due_inbound(
        [
            InTransitItem(
                inbound_id="   ",
                purchase_id="PUR-REQ-1-D1-S1",
                item="배추",
                quantity_kg=1,
                expected_arrival_date=date(2026, 1, 7),
            )
        ],
        as_of=date(2026, 1, 7),
    )

    assert 선택.due == (), "공백뿐인 식별자를 due 로 올리면 여기서 터진다"
    assert 선택.blocked[0].reasons == ("ARRIVAL_INBOUND_ID_MISSING",)


# ── 9~15. 아무것도 쓰지 않고 커넥션을 열지 않는다 ───────────────────────


def test_9_10_11_쓰기_문장이_없다():
    """🔴 이 단계는 **읽기 전용**이다. Receipt 를 만드는 것은 다음 단계다."""
    코드 = _코드만(_원문())

    for 금지 in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "MERGE"):
        assert 금지 not in 코드, f"{금지} 가 있다 — 이 단계는 읽기만 한다"


def test_12_13_커밋도_롤백도_하지_않는다():
    """🔴 커밋은 나중에 **한 바깥 트랜잭션**이 한 번 한다 (`ledger.py` 와 같은 규율)."""
    코드 = _코드만(_원문())
    assert "commit" not in 코드
    assert "rollback" not in 코드

    conn = 가짜커넥션([_행("RCP-1")])
    check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_14_자기_커넥션을_열지_않는다():
    """🔴 커넥션을 새로 열면 바깥 트랜잭션 **밖에서** 읽게 되고, 그러면 같은 호출
    안에서 방금 쓴 것을 못 본다.

    ★ 그래서 `repository.fetch_all` 도 쓰지 않는다 — 그쪽이 자기 커넥션을 연다.
    """
    코드 = _코드만(_원문())

    assert "get_connection" not in 코드
    assert "fetch_all" not in 코드
    assert "execute_returning_one" not in 코드


def test_15_받은_커넥션만_쓰고_닫지도_않는다():
    """★ 커서 하나만 열고 끝난다 — 커넥션의 수명은 호출자 것이다."""
    conn = 가짜커넥션([_행("RCP-1")])

    check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert len(conn.커서들) == 1
    assert conn.closed == 0, "받은 커넥션을 닫지 않는다"


def test_15b_UniqueViolation_을_흐름으로_쓰지_않는다():
    """🔴 DB 무결성 예외는 트랜잭션을 aborted 로 만든다 — *"이미 있으니 넘어간다"* 를
    그것으로 표현하면 멀쩡한 재실행이 장애가 된다.
    """
    코드 = _코드만(_원문())

    assert "UniqueViolation" not in 코드
    assert "psycopg.errors" not in 코드, "DB 예외 종류를 흐름 분기로 쓰지 않는다"
    assert "except" not in 코드, "예외를 잡아 흐름으로 쓰지 않는다"


# ── 16. 남의 파트에 손대지 않는다 ───────────────────────────────────────


def test_16_다른_파트를_임포트하지_않는다():
    """★ 이 모듈은 물류 소유다. 매입·재무·판매·마스터 계약을 끌어오지 않는다."""
    tree = ast.parse(_원문())
    모듈: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            모듈.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            모듈.add(node.module)

    남의것 = [
        name
        for name in 모듈
        if name.startswith(("app.master", "app.purchase", "app.finance", "app.sales"))
    ]
    assert 남의것 == [], 남의것
    assert 모듈 <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "typing",
        "psycopg",
        "app.logistics.db",
    }, 모듈


def test_16b_매입_상세를_조회하지_않는다():
    """⚠️ `purchase_items` 조회 · `item_id` · 원가 · 등급 유도는 **뒤 단계**다.

    이 함수가 답하는 질문은 하나다 — *"이 입고 건에 Receipt 가 이미 있나"*.
    """
    코드 = _코드만(_원문())

    for 금지 in ("purchase_items", "inventory_lots", "inventory_moves", "unit_price", "grade"):
        assert 금지 not in 코드, f"{금지} 를 건드리고 있다"


# ── 아직 답하지 않는 것 ─────────────────────────────────────────────────


def test_동시_중복_생성을_풀지_않았다():
    """⚠️ **읽고-쓰기 사이는 여전히 비어 있다.**

    ```text
    T1 조회 → 0건      T2 조회 → 0건      둘 다 "새 건" 으로 본다
    ```

    🔴 여기서 미리 잠그지 않는다 — 쓰기 경로가 없어 무엇을 어떤 순서로 잠글지 정할
       근거가 없고, 근거 없는 잠금은 `ledger.py` 가 겪은 교착을 다시 부른다.
       막는 것은 쓰기 단계의 일이다.
    """
    코드 = _코드만(_원문())

    assert "pg_advisory" not in 코드
    assert "FOR UPDATE" not in 코드
