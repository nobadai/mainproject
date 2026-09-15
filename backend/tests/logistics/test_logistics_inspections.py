"""검수 기록과 Receipt 마감의 **규율** 검사 (3-B4-H). DB 를 부르지 않는다.

```text
결과를 지어내지 않나     판정·수량은 호출자가 준다
DB 계약을 먼저 거나      항등식·판정 배반을 DML 앞에서 막는다
재실행이 멱등한가        같은 사실이면 안 쓰고, 다른 사실이면 멈춘다
마감을 되돌리지 않나     INSPECTED 이상의 상태를 앞당기지도 되돌리지도 않는다
안 한 관찰을 안 적나     inbound_inspection_checks 를 만들지 않는다
```
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import inspections
from app.logistics.inspections import (
    InspectionConflict,
    InspectionIntegrityError,
    InspectionOutcome,
    InspectionWriteResult,
    InvalidInspectionOutcome,
    find_inspection,
    inspection_id_for,
    record_inspection,
    validate_outcome,
)

RECEIPT_ID = "RCPT-SIM-BURNIN-202512-INB-H1-THRU-20260105-BAECHU-1-1"
INSPECTION_ID = "INSP-" + RECEIPT_ID
INSPECTED_AT = datetime(2026, 1, 7, 9, 30, tzinfo=UTC)
INSPECTOR = "WH-INSPECTOR-01"

_검수칸 = (
    "inspection_id",
    "verdict",
    "inspected_qty_kg",
    "accepted_qty_kg",
    "hold_qty_kg",
    "reject_qty_kg",
)
_영수칸 = ("receipt_status", "accepted_qty_kg", "hold_qty_kg", "rejected_qty_kg")


@pytest.fixture(autouse=True)
def 스키마이름을_고정한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_db_schema()` 는 환경변수를 읽는다 — 여기서 끊는다."""
    monkeypatch.setattr(inspections, "get_db_schema", lambda: "haetdeul")


def _결과(
    verdict: str = "PASS",
    *,
    inspected: str = "3587.000000",
    accepted: str | None = None,
    hold: str = "0",
    reject: str = "0",
) -> InspectionOutcome:
    return InspectionOutcome(
        verdict=verdict,  # type: ignore[arg-type]
        inspected_qty_kg=Decimal(inspected),
        accepted_qty_kg=Decimal(inspected if accepted is None else accepted),
        hold_qty_kg=Decimal(hold),
        reject_qty_kg=Decimal(reject),
    )


class 가짜커서:
    """작은 `inbound_receipts` · `inbound_inspections` 표를 들고 실제로 거른다."""

    def __init__(self, 영수: list[dict], 검수: list[dict], log: list[Any]) -> None:
        self._영수 = 영수
        self._검수 = 검수
        self.log = log
        self._rows: list[Any] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        text = str(query)
        self.log.append((text, params))
        assert isinstance(params, tuple)
        self._rows = []

        if "pg_advisory" in text:
            return

        if "INSERT" in text:
            칸 = _insert_칸이름(text)
            self._검수.append(dict(zip(칸, params, strict=True)))
            return

        if "UPDATE" in text:
            accepted, hold, rejected, status, receipt_id = params
            for row in self._영수:
                if row["receipt_id"] == receipt_id:
                    row.update(
                        accepted_qty_kg=accepted,
                        hold_qty_kg=hold,
                        rejected_qty_kg=rejected,
                        receipt_status=status,
                    )
            return

        (receipt_id,) = params
        if "inbound_inspections" in text:
            찾은 = sorted(
                (r for r in self._검수 if r["receipt_id"] == receipt_id),
                key=lambda r: r["inspection_id"],
            )
            한계 = re.search(r"Literal\((\d+)\)", text)
            if 한계:
                찾은 = 찾은[: int(한계.group(1))]
            self._rows = [tuple(r[name] for name in _검수칸) for r in 찾은]
            return

        찾은영수 = [r for r in self._영수 if r["receipt_id"] == receipt_id]
        self._rows = [tuple(r[name] for name in _영수칸) for r in 찾은영수]

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        raise AssertionError("fetchone 을 쓰면 2행 이상이 조용히 첫 행으로 나간다")


def _insert_칸이름(text: str) -> list[str]:
    """INSERT 문의 칸 목록을 그대로 읽는다 — 짐작하지 않는다."""
    본문 = re.search(r"inbound_inspections\s*\((.*?)\)", text, re.DOTALL)
    assert 본문 is not None, text
    # ⚠️ `str(Composed)` 는 줄바꿈을 **글자 두 개**(\n)로 적는다 — 그냥 strip 하면
    #    첫 칸 이름에 그 두 글자가 붙어 남는다.
    칸들 = 본문.group(1).replace(chr(92) + "n", " ")
    return [칸.strip() for 칸 in 칸들.split(",") if 칸.strip()]


class 가짜커넥션:
    def __init__(self, *, 영수: list[dict] | None = None, 검수: list[dict] | None = None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.log: list[Any] = []
        self.커서들: list[가짜커서] = []
        self.영수 = 영수 if 영수 is not None else [_영수행()]
        self.검수 = 검수 or []

    def cursor(self) -> 가짜커서:
        cur = 가짜커서(self.영수, self.검수, self.log)
        self.커서들.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def _영수행(*, status: str = "ARRIVED", receipt_id: str = RECEIPT_ID, **수량) -> dict:
    행 = {
        "receipt_id": receipt_id,
        "receipt_status": status,
        "accepted_qty_kg": None,
        "hold_qty_kg": None,
        "rejected_qty_kg": None,
    }
    행.update(수량)
    return 행


def _검수행(outcome: InspectionOutcome, *, inspection_id: str = INSPECTION_ID) -> dict:
    return {
        "inspection_id": inspection_id,
        "receipt_id": RECEIPT_ID,
        "verdict": outcome.verdict,
        "inspected_qty_kg": outcome.inspected_qty_kg,
        "accepted_qty_kg": outcome.accepted_qty_kg,
        "hold_qty_kg": outcome.hold_qty_kg,
        "reject_qty_kg": outcome.reject_qty_kg,
    }


def _적는다(conn: 가짜커넥션, outcome: InspectionOutcome | None = None) -> InspectionWriteResult:
    return record_inspection(
        conn,
        receipt_id=RECEIPT_ID,
        inspected_at=INSPECTED_AT,
        inspector=INSPECTOR,
        outcome=outcome or _결과(),
    )


def _쓰기(conn: 가짜커넥션, 종류: str) -> list[Any]:
    return [(q, p) for q, p in conn.log if 종류 in str(q)]


def _코드만(source: str) -> str:
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                코드 = 코드.replace(doc, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _원문() -> str:
    return Path(inspections.__file__).read_text(encoding="utf-8")


# ── 1~10. DB 계약을 쓰기 전에 건다 ─────────────────────────────────────


def test_1_PASS_는_보류도_거부도_없다():
    validate_outcome(_결과("PASS"))


def test_2_HOLD_는_일부만_수용할_수_있다():
    validate_outcome(_결과("HOLD", inspected="100", accepted="70", hold="30", reject="0"))


def test_3_REJECT_는_수용이_0_이다():
    validate_outcome(_결과("REJECT", inspected="100", accepted="0", hold="0", reject="100"))


def test_4_합이_안_맞으면_거부한다():
    """🔴 **값을 고쳐 맞추지 않는다** — 어느 쪽이 맞는지 우리가 모른다."""
    with pytest.raises(InvalidInspectionOutcome, match="항등식"):
        validate_outcome(_결과("HOLD", inspected="100", accepted="70", hold="20", reject="0"))


def test_5_PASS_인데_거부가_있으면_거부한다():
    with pytest.raises(InvalidInspectionOutcome):
        validate_outcome(_결과("PASS", inspected="100", accepted="90", hold="0", reject="10"))


def test_6_HOLD_인데_보류가_0_이면_거부한다():
    with pytest.raises(InvalidInspectionOutcome):
        validate_outcome(_결과("HOLD", inspected="100", accepted="100", hold="0", reject="0"))


def test_7_REJECT_인데_수용이_있으면_거부한다():
    with pytest.raises(InvalidInspectionOutcome):
        validate_outcome(_결과("REJECT", inspected="100", accepted="10", hold="0", reject="90"))


@pytest.mark.parametrize("검수량", ["0", "-1"], ids=["0", "음수"])
def test_8_검수량이_0_이하면_거부한다(검수량: str):
    with pytest.raises(InvalidInspectionOutcome):
        validate_outcome(_결과("PASS", inspected=검수량, accepted=검수량, hold="0", reject="0"))


def test_9_음수_수량을_거부한다():
    with pytest.raises(InvalidInspectionOutcome):
        validate_outcome(_결과("HOLD", inspected="100", accepted="110", hold="10", reject="-20"))


@pytest.mark.parametrize("나쁜값", ["NaN", "Infinity", "-Infinity"])
def test_10_비유한값을_거부한다(나쁜값: str):
    """🔴 `NaN` 은 DB CHECK 의 부등식을 **조용히 통과**해 항등식이 깨진 행을 남긴다."""
    with pytest.raises(InvalidInspectionOutcome, match="유한"):
        validate_outcome(
            InspectionOutcome(
                verdict="PASS",
                inspected_qty_kg=Decimal(나쁜값),
                accepted_qty_kg=Decimal(나쁜값),
                hold_qty_kg=Decimal(0),
                reject_qty_kg=Decimal(0),
            )
        )


def test_10b_float_을_거부한다():
    with pytest.raises(InvalidInspectionOutcome, match="Decimal"):
        validate_outcome(
            InspectionOutcome(
                verdict="PASS",
                inspected_qty_kg=100.0,  # type: ignore[arg-type]
                accepted_qty_kg=Decimal(100),
                hold_qty_kg=Decimal(0),
                reject_qty_kg=Decimal(0),
            )
        )


def test_10c_계약_밖_판정을_거부한다():
    with pytest.raises(InvalidInspectionOutcome, match="어휘"):
        validate_outcome(_결과("PARTIAL"))


# ── 11~15. 새 검수를 적는다 ────────────────────────────────────────────


def test_11_ARRIVED_면_검수를_INSERT_한다():
    conn = 가짜커넥션()

    결과 = _적는다(conn)

    assert 결과.applied is True
    assert len(_쓰기(conn, "INSERT")) == 1
    assert len(conn.검수) == 1


def test_12_Receipt_가_INSPECTED_로_넘어간다():
    conn = 가짜커넥션()

    결과 = _적는다(conn)

    assert 결과.receipt_status == "INSPECTED"
    assert conn.영수[0]["receipt_status"] == "INSPECTED"


def test_13_수량이_Receipt_로_옮겨진다():
    """⚠️ 칸 이름이 다르다 — 검수 `reject_qty_kg` → Receipt `rejected_qty_kg`."""
    conn = 가짜커넥션()

    _적는다(conn, _결과("HOLD", inspected="100", accepted="70", hold="30", reject="0"))

    행 = conn.영수[0]
    assert 행["accepted_qty_kg"] == Decimal(70)
    assert 행["hold_qty_kg"] == Decimal(30)
    assert 행["rejected_qty_kg"] == Decimal(0)


def test_14_fact_source_는_SCENARIO_SIMULATED_다():
    conn = 가짜커넥션()

    _적는다(conn)

    assert "SCENARIO_SIMULATED" in _쓰기(conn, "INSERT")[0][1]
    assert "HUMAN_RECORDED" not in _쓰기(conn, "INSERT")[0][1]


def test_14b_검수자와_검수시각은_호출자가_준_값_그대로다():
    """🔴 **지어내지 않는다** — 저장소에 시스템 행위자 규약도 시각 규약도 없다."""
    conn = 가짜커넥션()

    _적는다(conn)

    params = _쓰기(conn, "INSERT")[0][1]
    assert INSPECTOR in params
    assert INSPECTED_AT in params


def test_14c_시계를_읽지_않는다():
    """★ `now()` 는 Receipt `updated_at` 하나뿐 — **DB 기록 시각**이지 업무 사실이 아니다."""
    코드 = _코드만(_원문())

    for 금지 in ("datetime.now", "utcnow", "today("):
        assert 금지 not in 코드, f"{금지} — 검수 시각을 지어내고 있다"
    assert 코드.count("now()") == 1, "now() 는 updated_at 한 자리뿐이다"


def test_14d_시간대_없는_시각을_거부한다():
    """🔴 naive 를 `TIMESTAMPTZ` 에 넣으면 세션 TimeZone 에 따라 뜻이 달라진다."""
    conn = 가짜커넥션()

    with pytest.raises(InvalidInspectionOutcome, match="시간대"):
        record_inspection(
            conn,
            receipt_id=RECEIPT_ID,
            inspected_at=datetime(2026, 1, 7, 9, 30),  # noqa: DTZ001 - 일부러 naive
            inspector=INSPECTOR,
            outcome=_결과(),
        )
    assert conn.log == []


@pytest.mark.parametrize("빈값", ["", "   "], ids=["빈문자열", "공백"])
def test_14e_검수자가_비면_멈춘다(빈값: str):
    conn = 가짜커넥션()

    with pytest.raises(InvalidInspectionOutcome, match="inspector"):
        record_inspection(
            conn,
            receipt_id=RECEIPT_ID,
            inspected_at=INSPECTED_AT,
            inspector=빈값,
            outcome=_결과(),
        )
    assert conn.log == []


def test_15_inspection_id_가_결정론이다():
    assert inspection_id_for(receipt_id=RECEIPT_ID) == INSPECTION_ID
    assert inspection_id_for(receipt_id=RECEIPT_ID) == inspection_id_for(receipt_id=RECEIPT_ID)
    assert inspection_id_for(receipt_id="RCPT-A") != inspection_id_for(receipt_id="RCPT-B")


def test_15b_빈_receipt_id_로는_id_를_못_짓는다():
    with pytest.raises(InvalidInspectionOutcome):
        inspection_id_for(receipt_id="   ")


# ── 16~21. 재실행과 무결성 ─────────────────────────────────────────────


def test_16_같은_사실로_다시_부르면_안_쓴다():
    conn = 가짜커넥션()
    첫번 = _적는다(conn)

    두번 = _적는다(conn)

    assert 첫번.applied is True
    assert 두번.applied is False
    assert len(conn.검수) == 1
    assert len(_쓰기(conn, "INSERT")) == 1
    assert 두번.outcome == 첫번.outcome


def test_16b_자릿수만_달라도_같은_사실이다():
    """🔴 문자열로 비교하면 `100` 과 `100.000000` 이 갈려 **정상 재실행이 터진다.**"""
    conn = 가짜커넥션(
        영수=[
            _영수행(
                status="INSPECTED",
                accepted_qty_kg=Decimal("100.000000"),
                hold_qty_kg=Decimal("0.000000"),
                rejected_qty_kg=Decimal("0.000000"),
            )
        ],
        검수=[_검수행(_결과("PASS", inspected="100.000000"))],
    )

    결과 = _적는다(conn, _결과("PASS", inspected="100"))

    assert 결과.applied is False


def test_17_다른_사실이면_충돌이다():
    """🔴 덮지도 버리지도 않는다 — 어느 쪽이 진짜인지 고를 근거가 없다."""
    conn = 가짜커넥션(검수=[_검수행(_결과("PASS", inspected="100"))])

    with pytest.raises(InspectionConflict):
        _적는다(conn, _결과("REJECT", inspected="100", accepted="0", hold="0", reject="100"))

    assert len(_쓰기(conn, "INSERT")) == 0
    assert len(_쓰기(conn, "UPDATE")) == 0


@pytest.mark.parametrize("상태", ["INSPECTED", "PUTAWAY_DONE", "CLOSED"])
def test_18_19_마감된_Receipt_는_중복_생성하지_않는다(상태: str):
    outcome = _결과("PASS", inspected="100")
    conn = 가짜커넥션(
        영수=[
            _영수행(
                status=상태,
                accepted_qty_kg=Decimal(100),
                hold_qty_kg=Decimal(0),
                rejected_qty_kg=Decimal(0),
            )
        ],
        검수=[_검수행(outcome)],
    )

    결과 = _적는다(conn, outcome)

    assert 결과.applied is False
    assert 결과.receipt_status == 상태, "상태를 앞당기지도 되돌리지도 않는다"
    assert len(_쓰기(conn, "INSERT")) == 0
    assert len(_쓰기(conn, "UPDATE")) == 0


@pytest.mark.parametrize("상태", ["INSPECTED", "PUTAWAY_DONE", "CLOSED"])
def test_20_상태는_마감인데_검수가_없으면_무결성_오류다(상태: str):
    """🔴 *"검수를 이미 했다"* 는 주장인데 사실이 없다 — 새로 적으면 **사라진 결과가
    있었다는 것조차 안 남는다.** 복구 경로는 스키마에도 코드에도 없다.
    """
    conn = 가짜커넥션(영수=[_영수행(status=상태)], 검수=[])

    with pytest.raises(InspectionIntegrityError, match="검수 행이 없다"):
        _적는다(conn)

    assert len(_쓰기(conn, "INSERT")) == 0


def test_20b_마감된_Receipt_의_수량이_검수와_다르면_무결성_오류다():
    """⚠️ 뒤 단계가 이미 그 값으로 움직였을 수 있어 덮어쓰지 않는다."""
    outcome = _결과("PASS", inspected="100")
    conn = 가짜커넥션(
        영수=[
            _영수행(
                status="PUTAWAY_DONE",
                accepted_qty_kg=Decimal(80),
                hold_qty_kg=Decimal(0),
                rejected_qty_kg=Decimal(0),
            )
        ],
        검수=[_검수행(outcome)],
    )

    with pytest.raises(InspectionIntegrityError, match="수량이 검수와 다르다"):
        _적는다(conn, outcome)


def test_21_검수가_둘이면_무결성_오류다():
    """★ DB 에 `receipt_id` UNIQUE 가 없다 — 여기가 유일한 방어선이다."""
    outcome = _결과("PASS", inspected="100")
    conn = 가짜커넥션(
        검수=[
            _검수행(outcome, inspection_id="INSP-B"),
            _검수행(outcome, inspection_id="INSP-A"),
        ]
    )

    with pytest.raises(InspectionIntegrityError, match="둘 이상"):
        _적는다(conn, outcome)

    assert len(_쓰기(conn, "INSERT")) == 0


def test_21b_find_inspection_도_첫_행을_고르지_않는다():
    outcome = _결과("PASS", inspected="100")
    conn = 가짜커넥션(
        검수=[_검수행(outcome, inspection_id="INSP-B"), _검수행(outcome, inspection_id="INSP-A")]
    )

    with pytest.raises(InspectionIntegrityError):
        find_inspection(conn, receipt_id=RECEIPT_ID)


def test_21c_ARRIVED_인데_검수가_이미_있으면_Receipt_만_맞춘다():
    """★ **반쪽 상태 복구다.** 쓰는 값이 정상 경로가 썼을 값과 글자 그대로 같아서
    새 정보를 만들지 않는다 — 그래서 안전하다.
    """
    outcome = _결과("PASS", inspected="100")
    conn = 가짜커넥션(영수=[_영수행(status="ARRIVED")], 검수=[_검수행(outcome)])

    결과 = _적는다(conn, outcome)

    assert 결과.applied is False, "검수를 새로 만들지는 않는다"
    assert 결과.receipt_status == "INSPECTED"
    assert conn.영수[0]["accepted_qty_kg"] == Decimal(100)
    assert len(_쓰기(conn, "INSERT")) == 0
    assert len(_쓰기(conn, "UPDATE")) == 1


def test_21d_Receipt_가_없으면_검수만_적지_않는다():
    conn = 가짜커넥션(영수=[])

    with pytest.raises(InspectionIntegrityError, match="Receipt 가 없다"):
        _적는다(conn)

    assert len(_쓰기(conn, "INSERT")) == 0


# ── 22~26. 경계와 범위 ─────────────────────────────────────────────────


def test_22_검수_항목_행을_만들지_않는다():
    """🔴 `MOLD=false` 를 채우면 **하지 않은 관찰을 했다고 적는 것**이 된다.

    ★ 그 표의 주석이 *"사람이 웹 Form 으로 넣는다"* 이고, 필수라는 정책이 없다.
    """
    코드 = _코드만(_원문())

    assert "inbound_inspection_checks" not in 코드
    for 금지 in ("MOLD", "ROT", "ODOR", "CONTAMINATION", "PACKAGING_DAMAGE", "severity"):
        assert 금지 not in 코드, f"{금지} — 안 한 관찰을 적고 있다"


def test_23_24_25_커밋도_롤백도_새_커넥션도_없다():
    conn = 가짜커넥션()

    _적는다(conn)

    assert conn.commits == 0
    assert conn.rollbacks == 0
    assert conn.closed == 0
    코드 = _코드만(_원문())
    assert "get_connection" not in 코드
    assert "commit" not in 코드
    assert "rollback" not in 코드


def test_26_다른_파트를_임포트하지_않는다():
    tree = ast.parse(_원문())
    모듈: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            모듈.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            모듈.add(node.module)

    assert not [
        m for m in 모듈 if m.startswith(("app.master", "app.purchase", "app.finance", "app.sales"))
    ]
    assert 모듈 <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "typing",
        "psycopg",
        "app.logistics.db",
        "app.logistics.receipts",
    }, 모듈


def test_잠금이_먼저이고_세_번째_잠금을_만들지_않는다():
    """★ 도착 쓰기와 **같은 전역 키**를 쓴다 — 세 번째 잠금을 만들지 않는다."""
    conn = 가짜커넥션()

    _적는다(conn)

    순서 = [str(q) for q, _ in conn.log]
    assert "pg_advisory_xact_lock" in 순서[0], "잠금이 가장 먼저다"
    잠금 = next(p for q, p in conn.log if "pg_advisory" in str(q))
    assert 잠금 == (20260905, 2), "도착 쓰기 잠금과 같은 키여야 한다"
    assert len([q for q, _ in conn.log if "pg_advisory" in str(q)]) == 1


def test_아직_Lot_도_원장도_일정도_건드리지_않는다():
    코드 = _코드만(_원문())

    for 금지 in (
        "inventory_lots",
        "inventory_moves",
        "logistics_runtime_fixture",
        "in_transit",
        "PUTAWAY_DONE'",
        "receiving_location_id",
        "pallet",
    ):
        assert 금지 not in 코드, f"{금지} — 이 단계 범위가 아니다"
