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
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import receipts
from app.logistics.arrival import DueInbound
from app.logistics.purchase_detail import PurchaseDetail
from app.logistics.receipts import (
    InvalidInboundIdentity,
    ReceiptExistence,
    ReceiptFactsMissing,
    ReceiptIntegrityError,
    ReceiptRowUnreadable,
    check_receipt_state,
    create_arrived_receipt,
    receipt_id_for,
)
from app.logistics.schemas import InTransitItem

SIM_RUN_ID = "SIM-BURNIN-202512"
INBOUND_ID = "INB-H1-THRU-20260105-BAECHU-1-1"

#: `check_receipt_state` 의 SELECT 칸 순서와 **같아야** 한다 —
#: 가짜 커서가 튜플로도 답하기 때문이다.
_칸순서 = ("receipt_id", "receipt_status")


def _insert_칸이름(text: str) -> list[str]:
    """INSERT 문의 칸 목록을 그대로 읽는다 — 가짜 표에 꽂아 넣으려는 것이다.

    ★ 이름을 정규식으로 **짐작하지 않는다.** 괄호 안을 그대로 쪼개야 칸이 늘거나
      줄었을 때 검사가 조용히 통과하지 않는다.
    """
    본문 = re.search(r"inbound_receipts\s*\((.*?)\)", text, re.DOTALL)
    assert 본문 is not None, text
    # ⚠️ `str(Composed)` 는 줄바꿈을 **글자 두 개**(\n)로 적는다 — 그냥 strip 하면
    #    첫 칸 이름에 그 두 글자가 붙어 남는다.
    칸들 = 본문.group(1).replace(chr(92) + "n", " ")
    return [칸.strip() for 칸 in 칸들.split(",") if 칸.strip()]


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
        text = str(query)
        self.log.append((text, params))
        assert isinstance(params, tuple), "파라미터를 튜플로 넘겨야 한다"
        self._rows = []

        if "pg_advisory" in text:
            # ★ 잠금은 값을 안 돌려준다 — 여기서는 걸렸다는 사실만 기록한다.
            return

        if "INSERT" in text:
            # ★ **실제로 표에 넣는다.** 같은 트랜잭션 안에서 두 번 부르면 두 번째가
            #   ALREADY_EXISTS 로 갈라지는지까지 재려는 것이다.
            self._table.append(dict(zip(_insert_칸이름(text), params, strict=True)))
            return

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
            {name: row[name] for name in _칸순서}
            if self._매핑행
            else tuple(row[name] for name in _칸순서)
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


def _행(
    receipt_id: str,
    *,
    sim_run_id: str = SIM_RUN_ID,
    inbound_id: str = INBOUND_ID,
    receipt_status: str = "ARRIVED",
) -> dict:
    return {
        "receipt_id": receipt_id,
        "sim_run_id": sim_run_id,
        "inbound_id": inbound_id,
        "receipt_status": receipt_status,
    }


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

    assert 결과 == ReceiptExistence(status="NEW", receipt_id=None, receipt_status=None)


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

    assert 결과 == ReceiptExistence(
        status="ALREADY_EXISTS", receipt_id="RCP-1", receipt_status="ARRIVED"
    )


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


# ── receipt_status 를 그대로 내보낸다 (3-B4-F) ─────────────────────────
#
# 🔴 **`ALREADY_EXISTS` 만으로는 "이 입고를 더 볼 필요가 없다" 와 구별이 안 된다.**
#
#    ```text
#    Receipt 가 ARRIVED 로 있다 · 검수 없음 · Lot 없음 · 원장 IN 없음
#    ⇒ 행은 있지만 처리는 끝나지 않았다
#    ```
#
#    건너뛰면 Receipt 만 남고 재고가 안 들어온 채 영구 고착된다.


@pytest.mark.parametrize("상태", ["ARRIVED", "INSPECTING", "INSPECTED", "PUTAWAY_DONE", "CLOSED"])
def test_F1_DDL_어휘의_상태를_그대로_돌려준다(상태: str):
    """★ DB CHECK 어휘 다섯을 **손대지 않고** 나른다.

    ```sql
    CHECK (receipt_status IN ('ARRIVED','INSPECTING','INSPECTED','PUTAWAY_DONE','CLOSED'))
    ```
    """
    conn = 가짜커넥션([_행("RCP-1", receipt_status=상태)])

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과.status == "ALREADY_EXISTS"
    assert 결과.receipt_status == 상태


def test_F2_NEW_는_상태를_지어내지_않는다():
    """🔴 `NEW` 에 `"ARRIVED"` 를 얹으면 *"아직 없다"* 와 *"막 도착했다"* 가 같은 값이 된다."""
    conn = 가짜커넥션([])

    결과 = check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 결과.receipt_status is None
    assert 결과.receipt_id is None


@pytest.mark.parametrize(
    "이상한값",
    ["RECEIVED", "arrived", "", None, "ARRIVED ", "COMPLETED"],
    ids=["없는어휘", "소문자", "빈문자열", "NULL", "공백꼬리", "지어낸값"],
)
def test_F3_계약_밖_상태는_무결성_오류다(이상한값):
    """🔴 **`ARRIVED` 로 대신 읽지 않는다.**

    모르는 상태를 아는 값으로 바꾸면 **검수·Lot 이 이미 있는 행을 처음부터 다시
    돌게 된다** — 그것이 이 검사가 막는 자리다.

    ⚠️ `ck_inbound_receipts_status` 가 있으면 원래 못 생기지만, 제약이 아직 안 적용된
       DB 도 있을 수 있어 검사한다 (`ReceiptIntegrityError` 와 같은 이유).
    """
    conn = 가짜커넥션([_행("RCP-1", receipt_status=이상한값)])

    with pytest.raises(ReceiptRowUnreadable) as 오류:
        check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    메시지 = str(오류.value)
    assert "RCP-1" in 메시지, "어느 행인지 보여야 조사할 수 있다"
    assert "ARRIVED" in 메시지, "허용 어휘를 보여 준다"


@pytest.mark.parametrize("빈값", ["", "   ", None], ids=["빈문자열", "공백", "NULL"])
def test_F4_receipt_id_가_비면_무결성_오류다(빈값):
    """★ `receipt_id` 는 PK · NOT NULL 이다 — 비어 오면 읽은 것이 그 행이 아니다."""
    conn = 가짜커넥션([_행(빈값)])

    with pytest.raises(ReceiptRowUnreadable):
        check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)


def test_F5_상태를_읽어도_두_건_판정이_먼저다():
    """★ 손상 판정이 값 읽기보다 앞이다 — 깨진 상태에서 값을 꺼내 쓰지 않는다."""
    conn = 가짜커넥션(
        [_행("RCP-A", receipt_status="CLOSED"), _행("RCP-B", receipt_status="RECEIVED")]
    )

    with pytest.raises(ReceiptIntegrityError):
        check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)


def test_F6_상태로_진행_여부를_판단하지_않는다():
    """⚠️ *"어느 상태에서 무엇으로 이어갈지"* 는 별도 상태기계 감사가 정한다.

    🔴 지금 이 모듈이 그 분기를 들고 있으면, 스키마가 증명 못 하는 규칙이
       코드에 먼저 굳는다 (3-B4-D 감사 K 항목).
    """
    코드 = _코드만(_원문())

    for 금지 in ("INSPECTING", "INSPECTED", "PUTAWAY_DONE", "CLOSED"):
        assert 코드.count(금지) <= 1, f"{금지} 가 어휘 선언 밖에서 쓰이고 있다"
    for 금지 in ("resume", "next_step", "완료", "is_complete", "should_"):
        assert 금지 not in 코드, f"{금지} — 진행 판단이 들어와 있다"


def test_F7_질의가_두_칸을_읽는다():
    conn = 가짜커넥션([])

    check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    질의 = conn.log[0][0]
    assert "SELECT receipt_id, receipt_status" in 질의


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

    ⚠️ **조회 질의만 본다.** 3-B4-G 부터 이 모듈이 INSERT 도 하고 그 INSERT 는
       `purchase_item_id` · `item_id` · `arrived_at` 칸을 **쓴다** — 원문 전체를
       뒤지면 그 쓰기가 조회로 오해된다. 잠가야 할 것은 *"무엇으로 찾는가"* 다.
    """
    conn = 가짜커넥션([])

    check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    질의, 파라미터 = conn.log[0]
    assert 파라미터 == (SIM_RUN_ID, INBOUND_ID)
    for 금지 in ("purchase_id", "approval_id", "item_id", "expected_arrival_date", "arrived_at"):
        assert 금지 not in 질의, f"{금지} 로 Receipt 를 찾고 있다"


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


def test_9_10_11_쓰기는_ARRIVED_INSERT_하나뿐이다():
    """🔴 **UPDATE · DELETE 가 없다.** 이 판의 쓰기는 `ARRIVED` 행 INSERT 하나뿐이다.

    ★ 기존 Receipt 를 고치는 경로를 만들지 않는다 — 상태 진행도 보강도 이 단계의
      일이 아니고, 그 경로가 생기면 *"언제 무엇이 바뀌었나"* 를 아무도 못 본다.
    """
    코드 = _코드만(_원문())

    for 금지 in ("UPDATE", "DELETE", "TRUNCATE", "MERGE"):
        assert 금지 not in 코드, f"{금지} 가 있다 — 이 단계는 고치거나 지우지 않는다"
    assert 코드.count("INSERT") == 1, "INSERT 는 한 자리뿐이어야 한다"
    assert "INSERT INTO {}.inbound_receipts" in 코드, "그 INSERT 는 Receipt 표로만 간다"


def test_9b_조회_함수는_여전히_아무것도_안_쓴다():
    """★ 쓰기가 생겼다고 **조회까지 쓰기가 되면 안 된다.**"""
    conn = 가짜커넥션([_행("RCP-1")])

    check_receipt_state(conn, sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    질의들 = [str(q) for q, _ in conn.log]
    assert len(질의들) == 1
    for 금지 in ("INSERT", "UPDATE", "DELETE", "pg_advisory"):
        assert 금지 not in 질의들[0], f"조회가 {금지} 를 보내고 있다"


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
        # ★ 물류 형제 모듈이다 — 앞 단계가 검증한 사실을 **타입으로** 받는다.
        "app.logistics.arrival",
        "app.logistics.purchase_detail",
    }, 모듈


def test_16b_매입_상세를_조회하지_않는다():
    """⚠️ `purchase_items` 조회 · `item_id` · 원가 · 등급 유도는 **뒤 단계**다.

    이 함수가 답하는 질문은 하나다 — *"이 입고 건에 Receipt 가 이미 있나"*.
    """
    코드 = _코드만(_원문())

    for 금지 in ("purchase_items", "inventory_lots", "inventory_moves", "unit_price", "grade"):
        assert 금지 not in 코드, f"{금지} 를 건드리고 있다"
    assert "items" not in 코드.replace("purchase_item", ""), "items 표를 뒤지고 있다"


# ── 아직 답하지 않는 것 ─────────────────────────────────────────────────


def test_일정을_건드리지_않는다():
    """⚠️ 이 판은 **일정을 바꾸지 않는다** — 그래서 fixture 행을 잡지도 않는다.

    🔴 `in_transit` · `confirmed_inbound` 정리는 뒤 단계이고, 여기서 미리 손대면
       Receipt 만 쓰고 일정이 사라진 반쪽 상태가 생긴다.
    """
    코드 = _코드만(_원문())

    assert "FOR UPDATE" not in 코드, "일정 행을 잠글 이유가 아직 없다"
    assert "logistics_runtime_fixture" not in 코드
    assert "in_transit" not in 코드
    assert "confirmed_inbound" not in 코드


# ══════════════════════════════════════════════════════════════════════════
# 3-B4-G — ARRIVED Receipt 생성
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 **첫 쓰기 단계다.** 여기서 재는 것은 값이 DB 에 들어갔는지가 아니라
#    **물류가 소유한 쓰기 규율 다섯**이다.
#
#    ```text
#    id 가 결정론인가        같은 (sim_run_id, inbound_id) 는 늘 같은 행이다
#    잠금이 먼저인가         잠금 밖 조회를 믿으면 둘 다 INSERT 로 간다
#    없는 값을 안 만드나     수용량·위치·팔레트를 지어내지 않는다
#    예정일을 안 옮기나      연체분을 오늘 도착으로 적지 않는다
#    이미 있으면 안 쓰나     상태를 진행시키지도, 고치지도 않는다
#    ```


def _due(
    *,
    inbound_id: str = INBOUND_ID,
    purchase_id: str = "PUR-THRU-20260105-BAECHU-D1-S1",
    eta: date = date(2026, 1, 7),
    overdue: bool = False,
) -> DueInbound:
    return DueInbound(
        item=InTransitItem(
            inbound_id=inbound_id,
            purchase_id=purchase_id,
            item="배추",
            quantity_kg=Decimal("3587.0"),
            expected_arrival_date=eta,
        ),
        inbound_id=inbound_id,
        purchase_id=purchase_id,
        expected_arrival_date=eta,
        overdue=overdue,
    )


def _detail(
    *,
    purchase_item_id: str = "PITEM-THRU-20260105-BAECHU-D1-S1-BAECHU",
    item_id: str = "ITEM-BAECHU",
    quantity_kg: Decimal = Decimal("3587.000000"),
) -> PurchaseDetail:
    return PurchaseDetail(
        purchase_item_id=purchase_item_id,
        item_id=item_id,
        grade=None,
        quantity_kg=quantity_kg,
        unit_price_krw_per_kg=Decimal("854.000000"),
    )


def _쓴다(conn: 가짜커넥션, **kwargs) -> Any:
    return create_arrived_receipt(
        conn,
        sim_run_id=kwargs.pop("sim_run_id", SIM_RUN_ID),
        inbound=kwargs.pop("inbound", _due()),
        purchase_detail=kwargs.pop("purchase_detail", _detail()),
    )


def _쓰기질의(conn: 가짜커넥션) -> list[Any]:
    return [(q, p) for q, p in conn.log if "INSERT" in str(q)]


def _insert_params(conn: 가짜커넥션) -> tuple[Any, ...]:
    """INSERT 로 넘어간 파라미터. **앞에 잠금과 조회가 있다.**"""
    쓰기 = _쓰기질의(conn)
    assert len(쓰기) == 1, "INSERT 는 한 번이어야 한다"
    params = 쓰기[0][1]
    assert isinstance(params, tuple)
    return params


def _insert_columns(conn: 가짜커넥션) -> str:
    return str(_쓰기질의(conn)[0][0])


# ── 1~5. receipt_id 는 결정론이다 ──────────────────────────────────────


def test_G1_receipt_id_규칙():
    """★ `RCPT-{sim_run_id}-{inbound_id}` 다."""
    받은 = receipt_id_for(sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 받은 == "RCPT-" + SIM_RUN_ID + "-" + INBOUND_ID


def test_G2_같은_짝이면_같은_id_다():
    """🔴 난수·시계·시퀀스를 쓰면 재시도가 **같은 물건을 다른 행으로** 만든다."""
    첫번 = receipt_id_for(sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)
    두번 = receipt_id_for(sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert 첫번 == 두번


def test_G3_다른_실행이면_다른_id_다():
    """🔴 **PK 는 `receipt_id` 단독인데 유일성 축은 `(sim_run_id, inbound_id)` 다.**

    스키마가 *"같은 `inbound_id` 가 다른 실행에 있는 것은 합법"* 이라고 선언하므로,
    `inbound_id` 만으로 지으면 두 실행의 같은 입고가 **PK 에서 충돌한다.**
    """
    가 = receipt_id_for(sim_run_id="SIM-A", inbound_id=INBOUND_ID)
    나 = receipt_id_for(sim_run_id="SIM-B", inbound_id=INBOUND_ID)

    assert 가 != 나


def test_G4_다른_입고면_다른_id_다():
    가 = receipt_id_for(sim_run_id=SIM_RUN_ID, inbound_id="INB-A-1")
    나 = receipt_id_for(sim_run_id=SIM_RUN_ID, inbound_id="INB-B-1")

    assert 가 != 나


@pytest.mark.parametrize("빈값", ["", "   ", "\t", None], ids=["빈문자열", "공백", "탭", "None"])
def test_G5_빈_식별자로는_id_를_못_짓는다(빈값):
    with pytest.raises(InvalidInboundIdentity):
        receipt_id_for(sim_run_id=SIM_RUN_ID, inbound_id=빈값)
    with pytest.raises(InvalidInboundIdentity):
        receipt_id_for(sim_run_id=빈값, inbound_id=INBOUND_ID)


def test_G5b_매입_ID_에서_유도하지_않는다():
    """🔴 `purchase_id` 의 주인은 마스터다 — 그 모양에 기대면 마스터가 형식을 바꾸는
    날 조용히 어긋난다.
    """
    받은id = receipt_id_for(sim_run_id=SIM_RUN_ID, inbound_id=INBOUND_ID)

    assert "PUR-" not in 받은id
    assert "PITEM-" not in 받은id


# ── 6~17. 새 Receipt 는 무엇을 적나 ────────────────────────────────────


def test_G6_새_건이면_INSERT_가_한_번_나간다():
    conn = 가짜커넥션([])

    _쓴다(conn)

    assert len(_쓰기질의(conn)) == 1
    assert "inbound_receipts" in _insert_columns(conn)


def test_G7_G8_새_건은_applied_True_에_ARRIVED_다():
    conn = 가짜커넥션([])

    결과 = _쓴다(conn)

    assert 결과.applied is True
    assert 결과.receipt_status == "ARRIVED"
    assert 결과.receipt_id == "RCPT-" + SIM_RUN_ID + "-" + INBOUND_ID


def test_G9_fact_source_는_SCENARIO_SIMULATED_다():
    """🔴 창고에서 사람이 확인한 것이 아니다 — `HUMAN_RECORDED` 는 거짓이다."""
    conn = 가짜커넥션([])

    _쓴다(conn)

    assert "SCENARIO_SIMULATED" in _insert_params(conn)
    assert "HUMAN_RECORDED" not in _insert_params(conn)


def test_G10_arrived_at_은_예정일_그대로다():
    conn = 가짜커넥션([])

    _쓴다(conn, inbound=_due(eta=date(2026, 1, 7)))

    assert date(2026, 1, 7) in _insert_params(conn)


def test_G11_연체분도_원래_예정일을_지킨다():
    """🔴 **`as_of` 로 옮기지 않는다.**

    ```text
    expected_arrival_date = 2026-01-05
    처리 as_of             = 2026-01-07
    arrived_at            = 2026-01-05
    ```

    옮기면 그 로트가 이틀 더 신선한 것처럼 보이고, 신선도는 폐기·판매 판단으로
    흘러간다 (`repository` 가 `as_of - received_at` 으로 잔여일을 센다).
    """
    conn = 가짜커넥션([])

    _쓴다(conn, inbound=_due(eta=date(2026, 1, 5), overdue=True))

    params = _insert_params(conn)
    assert date(2026, 1, 5) in params
    assert date(2026, 1, 7) not in params, "처리일을 도착일로 적었다"


def test_G11b_시계를_읽지_않는다():
    """★ 날짜의 출처는 **인자로 받은 예정일 하나뿐**이다."""
    코드 = _코드만(_원문())

    for 금지 in ("today(", "now(", "utcnow", "CURRENT_DATE", "as_of"):
        assert 금지 not in 코드, "도착일을 지어내고 있다: " + 금지


def test_G12_주문수량은_매입_상세에서_온다():
    """★ 일정 수량(`3587.0`)이 아니라 **권위 있는 매입 사실**(`3587.000000`)이다.

    ⚠️ 두 값의 대조는 이 단계에서 하지 않는다 — 매입은 6자리로 quantize 하고 물류는
       원값을 유지해서, 소수 6자리를 넘으면 **정당하게 달라진다** (3-B4-E 결론).
    """
    conn = 가짜커넥션([])

    _쓴다(conn, purchase_detail=_detail(quantity_kg=Decimal("3587.000000")))

    수량 = [v for v in _insert_params(conn) if isinstance(v, Decimal)]
    assert 수량 == [Decimal("3587.000000")], "수량 칸은 주문수량 하나뿐이다"
    assert str(수량[0]) == "3587.000000", "자릿수를 손대지 않는다"


@pytest.mark.parametrize(
    "안적는칸",
    [
        "accepted_qty_kg",
        "hold_qty_kg",
        "rejected_qty_kg",
        "receiving_location_id",
        "estimated_pallet_count",
        "actual_pallet_count",
        "received_by",
        "note",
    ],
)
def test_G13_17_모르는_칸은_아예_안_적는다(안적는칸: str):
    """🔴 **값을 지어내는 대신 DB 기본값(NULL)에 맡긴다.**

    ★ `ck_inbound_receipts_qty` 가 `COALESCE(...,0)` 이라 NULL 이 허용되고, DDL 주석이
      *"미입력(NULL)은 0 으로 보지 않는다"* 라고 못박고 있다 — 0 으로 채우면
      *"검수했고 수용 0kg"* 이 된다.

    ★ 위치도 팔레트도 지어내지 않는다. 첫 Location 을 고르는 것도 고르는 것이다.
    """
    conn = 가짜커넥션([])

    _쓴다(conn)

    assert 안적는칸 not in _insert_columns(conn), "지어내고 있다: " + 안적는칸


def test_G17b_적는_칸은_아홉_개뿐이다():
    conn = 가짜커넥션([])

    _쓴다(conn)

    칸들 = _insert_columns(conn)
    for 필수 in (
        "receipt_id",
        "sim_run_id",
        "inbound_id",
        "purchase_item_id",
        "item_id",
        "arrived_at",
        "ordered_qty_kg",
        "receipt_status",
        "fact_source",
    ):
        assert 필수 in 칸들
    assert len(_insert_params(conn)) == 9


def test_G17c_created_at_은_DB_기본값에_맡긴다():
    conn = 가짜커넥션([])

    _쓴다(conn)

    칸들 = _insert_columns(conn)
    assert "created_at" not in 칸들
    assert "updated_at" not in 칸들


def test_G17d_item_id_를_따로_번역하지_않는다():
    """🔴 `inbound_receipts` 는 `purchase_item_id` 와 `item_id` 를 **따로** 드는데
    둘이 같은 품목인지 검사하는 복합 FK 도 CHECK 도 없다 — 독립 번역하면 두 칸이
    다른 품목을 가리켜도 DB 가 통과시킨다.
    """
    conn = 가짜커넥션([])

    _쓴다(conn, purchase_detail=_detail(item_id="ITEM-MU"))

    assert "ITEM-MU" in _insert_params(conn), "매입 상세의 item_id 를 그대로 써야 한다"
    assert "배추" not in str(conn.log), "한글 품목명이 새어 들어갔다"


# ── 14. 매입 사실이 없으면 안 쓴다 ─────────────────────────────────────


@pytest.mark.parametrize("빈값", ["", "   "], ids=["빈문자열", "공백"])
def test_G_매입_상세가_비면_INSERT_하지_않는다(빈값: str):
    """🔴 `purchase_item_id=NULL` 로 만들면 다음 실행이 `ALREADY_EXISTS` 를 보고
    **이 입고를 영영 건너뛴다** — 보강할 경로가 저장소에 없다.
    """
    conn = 가짜커넥션([])

    with pytest.raises(ReceiptFactsMissing):
        _쓴다(conn, purchase_detail=_detail(purchase_item_id=빈값))
    with pytest.raises(ReceiptFactsMissing):
        _쓴다(conn, purchase_detail=_detail(item_id=빈값))

    assert conn.log == [], "질의를 보내기 전에 멈춰야 한다"


# ── 18~23. 이미 있으면 쓰지 않는다 ─────────────────────────────────────


@pytest.mark.parametrize(
    "기존상태", ["ARRIVED", "INSPECTING", "INSPECTED", "PUTAWAY_DONE", "CLOSED"]
)
def test_G18_22_이미_있으면_INSERT_없이_상태를_보존한다(기존상태: str):
    """🔴 **`applied=False` 는 "입고 처리가 끝났다" 가 아니다.**

    뜻은 하나뿐이다 — *"행이 이미 있어 새로 만들지 않았다."* 상태를 진행시키지도,
    고치지도 않는다.
    """
    conn = 가짜커넥션([_행("RCP-EXISTING", receipt_status=기존상태)])

    결과 = _쓴다(conn)

    assert 결과.applied is False
    assert 결과.receipt_status == 기존상태, "상태를 진행시키지 않는다"
    assert _쓰기질의(conn) == [], "INSERT 를 보냈다"


def test_G23_기존_receipt_id_는_DB_값이_권위다():
    """★ 우리가 지은 값이 아니라 **DB 에 적힌 값**을 돌려준다 — 이 작명 규칙이
    생기기 전에 만들어진 행이라도 그 행이 진짜다.
    """
    conn = 가짜커넥션([_행("RCP-LEGACY-0001")])

    결과 = _쓴다(conn)

    assert 결과.receipt_id == "RCP-LEGACY-0001"
    assert 결과.receipt_id != "RCPT-" + SIM_RUN_ID + "-" + INBOUND_ID


def test_G_두_건이면_쓰기_전에_멈춘다():
    """★ 깨진 상태 위에 새 행을 얹지 않는다."""
    conn = 가짜커넥션([_행("RCP-A"), _행("RCP-B")])

    with pytest.raises(ReceiptIntegrityError):
        _쓴다(conn)

    assert _쓰기질의(conn) == []


# ── 24~26. 잠금이 먼저이고, 그 안에서 다시 묻는다 ──────────────────────


def test_G24_25_잠금_다음_재조회_다음_INSERT_순이다():
    """🔴 **③ 이 ② 뒤인 것이 이 함수의 동시성 계약이다.**

    호출자가 앞서 조회했더라도 그 답은 잠금 **밖**의 사실이라 이미 낡았을 수 있다.
    다시 묻지 않으면 두 트랜잭션이 함께 *"새 건"* 을 보고 둘 다 INSERT 로 간다.
    """
    conn = 가짜커넥션([])

    _쓴다(conn)

    순서 = [str(q) for q, _ in conn.log]
    assert len(순서) == 3, "잠금 · 조회 · INSERT 셋이어야 한다"
    assert "pg_advisory_xact_lock" in 순서[0], "잠금이 가장 먼저다"
    assert "SELECT receipt_id, receipt_status" in 순서[1], "잠금 안에서 다시 묻는다"
    assert "INSERT" in 순서[2]


def test_G24b_잠금_키가_원장과_안_겹친다():
    """★ `ledger` 는 `(20260905, 1)` 을 쓴다 (실측). 도착은 `objid=2` 다."""
    conn = 가짜커넥션([])

    _쓴다(conn)

    잠금 = next(p for q, p in conn.log if "pg_advisory" in str(q))
    assert 잠금 == (20260905, 2)
    assert 잠금 != (20260905, 1), "원장 잠금과 같은 키를 쓰면 안 된다"


def test_G24c_이미_있는_경우에도_잠금이_먼저다():
    """★ 재실행 경로에서도 잠금 밖 조회를 믿지 않는다."""
    conn = 가짜커넥션([_행("RCP-1")])

    _쓴다(conn)

    순서 = [str(q) for q, _ in conn.log]
    assert "pg_advisory_xact_lock" in 순서[0]
    assert len(순서) == 2, "잠금 · 조회뿐이다"


def test_G24d_입고별_잠금을_쓰지_않는다():
    """🔴 입고별 키는 `ledger` 가 겪은 교착을 그대로 재현한다 — 두 트랜잭션이
    요청하는 잠금 **집합 자체가 달라** 전순서를 매길 수 없다.
    """
    가 = 가짜커넥션([])
    나 = 가짜커넥션([])

    _쓴다(가, inbound=_due(inbound_id="INB-A-1"))
    _쓴다(나, inbound=_due(inbound_id="INB-B-1"))

    잠금가 = next(p for q, p in 가.log if "pg_advisory" in str(q))
    잠금나 = next(p for q, p in 나.log if "pg_advisory" in str(q))
    assert 잠금가 == 잠금나, "입고가 달라도 같은 전역 키여야 한다"


def test_G26_UniqueViolation_을_흐름으로_쓰지_않는다():
    """🔴 잠금이 있는데도 그물이 터지면 그것은 **버그**다 — 삼키지 않고 올린다."""
    코드 = _코드만(_원문())

    assert "UniqueViolation" not in 코드
    assert "psycopg.errors" not in 코드
    assert "except" not in 코드, "예외를 잡아 흐름으로 쓰지 않는다"
    assert "ON CONFLICT" not in 코드, "DB 에게 멱등을 떠넘기지 않는다"


# ── 27~33. 경계는 그대로다 ─────────────────────────────────────────────


def test_G27_28_커밋도_롤백도_하지_않는다():
    conn = 가짜커넥션([])

    _쓴다(conn)

    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_G29_33_받은_커넥션만_쓰고_닫지_않는다():
    conn = 가짜커넥션([])

    _쓴다(conn)

    assert conn.closed == 0
    assert len(conn.커서들) == 3, "잠금 · 조회 · INSERT 각각 커서 하나"


def test_G30_다른_파트를_임포트하지_않는다():
    """★ 매입·마스터 코드를 끌어오지 않는다 — 앞 단계가 검증한 **타입만** 받는다."""
    코드 = _코드만(_원문())

    for 금지 in ("app.master", "app.purchase_agent", "app.finance", "app.sales"):
        assert 금지 not in 코드, "끌어오고 있다: " + 금지


def test_G31_매입_계산을_하지_않는다():
    """★ 단가·금액·품목 번역은 전부 앞 단계가 이미 했다."""
    코드 = _코드만(_원문())

    for 금지 in ("unit_price", "line_amount", "item_name", "purchase_id_for"):
        assert 금지 not in 코드, "남의 계산을 복제하고 있다: " + 금지
