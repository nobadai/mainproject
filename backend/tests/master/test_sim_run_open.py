"""**새 실행을 여는 절차** (2026-09-10 · `#531` · `#539` 의 후속).

```text
① 새 실행 한 행          create_sim_run           이미 있다 (#531)
② 시작 재무 상태          seed_opening_finance_state   ← 이 판
③ 다시 열 때 장부 비우기   reset_sim_run_ledger         ← 이 판
```

🔴 **DB 를 안 탄다.** 커넥션도 카탈로그도 전부 대역이다 — 이 판은 절차를 세우는
   것까지고, 실제로 걷거나 행을 쓰거나 지우는 것은 이 판이 하지 않는다.

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가
  안 같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

from app.finance.db import get_db_schema
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.sim_run_open import (
    BaselineLineage,
    reset_sim_run_ledger,
    seed_opening_finance_state,
)

_MASTER = Path(__file__).resolve().parents[2] / "app" / "master"
_스키마 = get_db_schema()

#: 이 검사가 여는 새 실행. 🔴 **번인과 다르다** — 같으면 번인 가드를 걷은 뮤턴트가
#:   전부 살아남는다.
새실행 = "SIM-WALK-202601"
새조달 = "LOAN_BASELINE"

출발실행 = "SIM-BURNIN-202512"
출발상태 = "FIN-DAY30-LOAN"
계보 = BaselineLineage(from_sim_run_id=출발실행, finance_state_id=출발상태)


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _벗긴_원문(path: Path) -> str:
    """주석과 docstring 을 걷어낸 코드.

    🔴 **왜 걷어내나.** 이 파일은 근거를 길게 적는다 — 금지어가 **설명 문장 안에**
      있어서 원문 잠금이 늘 실패하면, 그 검사는 코드가 아니라 문장을 재는 것이 된다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


# ── 대역 ───────────────────────────────────────────────────────────────

#: `finance_states` 의 칸 — 실제 DDL 을 그대로 본뜬다.
#:   `financial_limit_krw` 는 `GENERATED ALWAYS` 라 **실을 수 없다.**
_칸목록: tuple[tuple[str, str], ...] = (
    ("finance_state_id", "NEVER"),
    ("sim_run_id", "NEVER"),
    ("state_date", "NEVER"),
    ("state_type", "NEVER"),
    ("financing_mode", "NEVER"),
    ("current_cash_krw", "NEVER"),
    ("minimum_operating_cash_krw", "NEVER"),
    ("committed_outflows_krw", "NEVER"),
    ("unsettled_purchase_payables_krw", "NEVER"),
    ("receivables_krw", "NEVER"),
    ("inventory_book_value_krw", "NEVER"),
    ("operational_inventory_value_krw", "NEVER"),
    ("current_debt_krw", "NEVER"),
    ("recommended_loan_amount_krw", "NEVER"),
    ("financial_limit_krw", "ALWAYS"),
    ("note", "NEVER"),
)

#: `NOT NULL` 인 칸 — **열셋**이다. 일부만 나르면 나머지를 어디서 채울지가 또 생긴다.
_NOT_NULL_칸 = (
    "finance_state_id",
    "sim_run_id",
    "state_date",
    "state_type",
    "financing_mode",
    "current_cash_krw",
    "minimum_operating_cash_krw",
    "committed_outflows_krw",
    "unsettled_purchase_payables_krw",
    "receivables_krw",
    "inventory_book_value_krw",
    "operational_inventory_value_krw",
    "current_debt_krw",
)

#: 출발점 한 행. **identity 넷은 새 실행이 물려받으면 안 되는 값**이다.
_출발행: dict[str, Any] = {
    "finance_state_id": 출발상태,
    "sim_run_id": 출발실행,
    "state_date": date(2025, 12, 31),
    "state_type": "DAY30",
    "financing_mode": 새조달,
    "current_cash_krw": Decimal("-13280000.000000"),
    "minimum_operating_cash_krw": Decimal("5000000.000000"),
    "committed_outflows_krw": Decimal("1200000.000000"),
    "unsettled_purchase_payables_krw": Decimal("3400000.000000"),
    "receivables_krw": Decimal("73050000.000000"),
    "inventory_book_value_krw": Decimal("8800000.000000"),
    "operational_inventory_value_krw": Decimal("8100000.000000"),
    "current_debt_krw": Decimal("30000000.000000"),
    "recommended_loan_amount_krw": Decimal("20000000.000000"),
    "note": "번인 30일 끝",
}

#: 대역 스키마의 표 — 축(`sim_run_id`)을 가진 것만.
_표들 = (
    "deliveries",
    "expenses",
    "finance_states",
    "payables",
    "purchases",
    "sales",
    "sim_runs",
)

#: 대역 스키마의 `(자식, 부모)`.
_FK = (
    ("expenses", "deliveries"),
    ("deliveries", "sales"),
    ("payables", "purchases"),
    ("sales", "sim_runs"),
    ("purchases", "sim_runs"),
    ("finance_states", "sim_runs"),
    # 대상 밖의 부모 — 지우는 순서에 안 낀다.
    ("sales", "items"),
    # 자기 참조 — 순서를 못 정하는 근거가 아니다.
    ("purchases", "purchases"),
)

#: 표마다 몇 행이 있는가. `expenses` 는 **0** 이다 — 0 도 세어서 나와야 한다.
_행수 = {
    "deliveries": 12,
    "expenses": 0,
    "finance_states": 1,
    "payables": 7,
    "purchases": 5,
    "sales": 9,
}


class _대역커서:
    """카탈로그 질의에만 답하고, 던진 문장을 전부 모은다."""

    def __init__(self, 대장: _대역커넥션) -> None:
        self.대장 = 대장
        self.rowcount = -1
        self._rows: list[dict[str, Any]] = []
        self._one: dict[str, Any] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        문장 = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.대장.log.append((문장, list(params or [])))
        self._rows, self._one = [], None
        if "pg_constraint" in 문장:
            self._rows = [{"child_table": c, "parent_table": p} for c, p in self.대장.fk]
        elif "is_generated" in 문장:
            self._rows = [
                {"column_name": name, "is_generated": gen, "identity_generation": None}
                for name, gen in self.대장.칸목록
            ]
        elif "col.table_name" in 문장:
            self._rows = [{"table_name": one} for one in self.대장.표들]
        elif "DELETE FROM" in 문장:
            self.rowcount = self.대장.행수.get(_표이름(문장), 0)
        elif "finance_state_id = %s" in 문장 and "SELECT" in 문장:
            self._one = self.대장.출발행

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._one


class _대역커넥션:
    def __init__(self, **over: Any) -> None:
        self.log: list[tuple[str, list[Any]]] = []
        self.commits = 0
        self.칸목록 = over.pop("칸목록", _칸목록)
        self.표들 = over.pop("표들", _표들)
        self.fk = over.pop("fk", _FK)
        self.행수 = over.pop("행수", _행수)
        출발행 = over.pop("출발행", _출발행)
        self.출발행 = dict(출발행) if 출발행 is not None else None
        assert not over, f"안 쓰는 인자: {sorted(over)}"

    def cursor(self) -> _대역커서:
        return _대역커서(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _표이름(문장: str) -> str:
    """대역이 `DELETE FROM "스키마"."표"` 에서 표 이름을 집는다.

    ★ **검사 쪽 편의다.** 운영 코드는 이름을 파싱하지 않는다 — 그 사실은
      `test_이름을_안_파싱한다` 가 따로 잰다.
    """
    찾은 = re.search(r'DELETE FROM "[^"]+"\."([^"]+)"', 문장)
    assert 찾은, f"표 이름을 못 집었다: {문장}"
    return 찾은.group(1)


def _문장들(conn: _대역커넥션, 머리: str) -> list[tuple[str, list[Any]]]:
    return [(문장, params) for 문장, params in conn.log if 머리 in 문장]


# ── ② 시작 재무 상태 ───────────────────────────────────────────────────


def _심는다(**over: Any) -> tuple[_대역커넥션, str]:
    conn = _대역커넥션(**{k: over.pop(k) for k in ("출발행",) if k in over})
    인자: dict[str, Any] = {
        "sim_run_id": 새실행,
        "financing_mode": 새조달,
        "baseline": 계보,
        "finance_state_id": "FIN-WALK-202601-OPEN",
        "state_date": date(2026, 1, 1),
        "state_type": "OPENING",
    }
    인자.update(over)
    return conn, seed_opening_finance_state(conn, **인자)


def _실린다(conn: _대역커넥션) -> dict[str, Any]:
    """INSERT 한 문장에서 `칸 이름 → 실린 값` 을 되짚는다."""
    실은것 = _문장들(conn, "INSERT INTO")
    assert len(실은것) == 1, f"한 행이 아니다: {실은것}"
    문장, params = 실은것[0]
    칸들 = re.findall(r'"([^"]+)"', 문장.split("VALUES")[0])
    칸들 = [one for one in 칸들 if one not in (_스키마, "finance_states")]
    assert len(칸들) == len(params), f"칸 수와 값 수가 다르다: {칸들} / {params}"
    return dict(zip(칸들, params, strict=True))


def test_시작_상태의_identity_가_새것이다() -> None:
    """🔴 **출발점 행을 그대로 복제하지 않는다.**

    ★★ 복제하면 새 실행의 첫 행에 **남의 실행 id 가 들어오고**, 그 뒤로 이 행이
      누구 것인지 되가를 방법이 없다.
    """
    conn, 이름 = _심는다()

    실린것 = _실린다(conn)
    assert 이름 == "FIN-WALK-202601-OPEN"
    assert 실린것["finance_state_id"] == "FIN-WALK-202601-OPEN"
    assert 실린것["sim_run_id"] == 새실행
    assert 실린것["state_date"] == date(2026, 1, 1)
    assert 실린것["state_type"] == "OPENING"
    for 칸 in ("finance_state_id", "sim_run_id", "state_date", "state_type"):
        assert 실린것[칸] != _출발행[칸], f"출발점의 {칸} 을 그대로 물려받았다"


def test_재무_값은_출발점에서_이관된다() -> None:
    """🔴 **지어내지 않는다.** `0` 으로 보정하면 *"출발점을 못 찾았다"* 가
    *"빈손에서 시작했다"* 로 둔갑한다.
    """
    conn, _ = _심는다()

    실린것 = _실린다(conn)
    for 칸, 값 in _출발행.items():
        if 칸 in ("finance_state_id", "sim_run_id", "state_date", "state_type"):
            continue
        assert 실린것[칸] == 값, f"{칸} 이 출발점에서 안 왔다: {실린것[칸]!r} != {값!r}"


def test_NOT_NULL_칸을_하나도_안_빠뜨린다() -> None:
    """★ `finance_states` 의 `NOT NULL` 은 **열셋**이다 — 일부만 고르면 나머지를
    어디서 채울지가 또 생긴다. 칸 목록의 주인은 `information_schema` 다.
    """
    conn, _ = _심는다()

    실린것 = _실린다(conn)
    assert len(_NOT_NULL_칸) == 13
    빠진것 = [칸 for 칸 in _NOT_NULL_칸 if 칸 not in 실린것]
    assert not 빠진것, f"NOT NULL 칸을 빠뜨렸다: {빠진것}"


def test_생성_칸은_안_싣는다() -> None:
    """★ `financial_limit_krw` 는 `GENERATED ALWAYS` 다 — 실으면 DB 가 막는다."""
    conn, _ = _심는다()

    assert "financial_limit_krw" not in _실린다(conn)


def test_조달_방식은_새_실행의_것을_싣는다() -> None:
    """★ 정합성 셋 ③ 이 *같음* 을 이미 쟀다 — 그래도 **새 실행 것을 적는다**.
    출발점에서 나르면 이 칸의 주인이 출발점이 되고, 셋 ③ 을 걷는 날 말없이 갈린다.
    """
    conn, _ = _심는다()

    assert _실린다(conn)["financing_mode"] == 새조달


def test_출발점이_없으면_터진다() -> None:
    """🔴 **정합성 셋 ①.** 다른 baseline 을 추측하지 않는다."""
    with pytest.raises(LookupError) as err:
        _심는다(출발행=None)

    assert _NFC(출발상태) in _NFC(str(err.value))


def test_출발점이_없으면_아무것도_안_싣는다() -> None:
    """★ 터지기만 하고 **행이 남으면** 안 된다."""
    conn = _대역커넥션(출발행=None)
    with pytest.raises(LookupError):
        seed_opening_finance_state(
            conn,
            sim_run_id=새실행,
            financing_mode=새조달,
            baseline=계보,
            finance_state_id="FIN-WALK-202601-OPEN",
            state_date=date(2026, 1, 1),
            state_type="OPENING",
        )

    assert not _문장들(conn, "INSERT INTO")


def test_출발점의_실행_축이_lineage_와_다르면_터진다() -> None:
    """🔴 **정합성 셋 ②.** `finance_state_id` 는 맞는데 **다른 실행의 행**이면,
    그 값은 우리가 이어받겠다고 말한 그 곡선의 끝이 아니다.
    """
    다른행 = {**_출발행, "sim_run_id": "SIM-WALK-202512"}
    with pytest.raises(ValueError) as err:
        _심는다(출발행=다른행)

    말 = _NFC(str(err.value))
    assert _NFC("SIM-WALK-202512") in 말 and _NFC(출발실행) in 말
    conn = _대역커넥션(출발행=다른행)
    with pytest.raises(ValueError):
        seed_opening_finance_state(
            conn,
            sim_run_id=새실행,
            financing_mode=새조달,
            baseline=계보,
            finance_state_id="FIN-WALK-202601-OPEN",
            state_date=date(2026, 1, 1),
            state_type="OPENING",
        )
    assert not _문장들(conn, "INSERT INTO")


def test_출발점의_조달_방식이_다르면_터진다() -> None:
    """🔴 **정합성 셋 ③.** 무차입 끝 상태 위에서 차입 실행을 열면 부채도 한도도
    처음부터 어긋난 채로 179일이 걸린다.

    ★ 재무가 마감에서 같은 셋을 다시 확인한다 — **양쪽이 같은 것을 재는 것이 의도**다.
    """
    다른행 = {**_출발행, "financing_mode": "BASE_NO_LOAN"}
    with pytest.raises(ValueError) as err:
        _심는다(출발행=다른행)

    말 = _NFC(str(err.value))
    assert _NFC("BASE_NO_LOAN") in 말 and _NFC(새조달) in 말
    conn = _대역커넥션(출발행=다른행)
    with pytest.raises(ValueError):
        seed_opening_finance_state(
            conn,
            sim_run_id=새실행,
            financing_mode=새조달,
            baseline=계보,
            finance_state_id="FIN-WALK-202601-OPEN",
            state_date=date(2026, 1, 1),
            state_type="OPENING",
        )
    assert not _문장들(conn, "INSERT INTO")


@pytest.mark.parametrize("빈값", ["", "   "])
@pytest.mark.parametrize("칸", ["sim_run_id", "financing_mode", "finance_state_id", "state_type"])
def test_빈_값으로_시작_상태를_못_만든다(칸: str, 빈값: str) -> None:
    """🔴 **없는 값을 메우지 않는다.**"""
    with pytest.raises(ValueError) as err:
        _심는다(**{칸: 빈값})

    assert _NFC(칸) in _NFC(str(err.value))


def test_시작_상태를_심는_자리가_커밋하지_않는다() -> None:
    """🔴 **커밋은 부르는 쪽이 한다** — `create_sim_run` 과 같다."""
    conn, _ = _심는다()

    assert conn.commits == 0


# ── ③ 장부를 지운다 ────────────────────────────────────────────────────


def test_번인_실행의_장부는_안_지운다() -> None:
    """🔴 **번인에 기초 상태가 있다.** 지우면 모든 실행의 출발점이 사라지고,
    그것을 되살릴 곳이 저장소에 없다.
    """
    conn = _대역커넥션()
    with pytest.raises(ValueError) as err:
        reset_sim_run_ledger(conn, sim_run_id=BURN_IN_SIM_RUN_ID)

    assert _NFC(BURN_IN_SIM_RUN_ID) in _NFC(str(err.value))
    assert not _문장들(conn, "DELETE FROM"), "번인에 DELETE 를 던졌다"


@pytest.mark.parametrize("빈값", ["", "   "])
def test_축이_비면_지우기가_막힌다(빈값: str) -> None:
    """★ 어느 실행인지를 여기서 지어내지 않는다."""
    conn = _대역커넥션()
    with pytest.raises(ValueError):
        reset_sim_run_ledger(conn, sim_run_id=빈값)

    assert not _문장들(conn, "DELETE FROM")


def test_지우는_순서가_자식_먼저다() -> None:
    """🔴 **자식부터 안 지우면 FK 가 막는다.** 부모를 먼저 지우려 하면
    `ForeignKeyViolation` 이 나고, 그때는 **일부만 지워진 장부**가 남는다.
    """
    conn = _대역커넥션()
    결과 = reset_sim_run_ledger(conn, sim_run_id=새실행)

    던진순서 = [_표이름(문장) for 문장, _ in _문장들(conn, "DELETE FROM")]
    assert 던진순서 == list(결과.order), "돌려준 순서와 실제로 던진 순서가 다르다"
    자리 = {표: i for i, 표 in enumerate(던진순서)}
    for 자식, 부모 in _FK:
        if 자식 == 부모 or 자식 not in 자리 or 부모 not in 자리:
            continue
        assert 자리[자식] < 자리[부모], f"부모({부모})를 자식({자식})보다 먼저 지운다"


def test_같은_스키마면_같은_순서다() -> None:
    """★ 같은 층에서는 이름순으로 고정한다 — 안 그러면 순서를 재는 검사가 흔들린다."""
    첫번째 = reset_sim_run_ledger(_대역커넥션(), sim_run_id=새실행).order
    두번째 = reset_sim_run_ledger(_대역커넥션(), sim_run_id=새실행).order

    assert 첫번째 == 두번째


def test_지운_행_수를_표마다_돌려준다() -> None:
    """🔴 **안 세면** *"지웠다"* 와 *"지울 것이 없었다"* 가 같아진다.

    ★ `expenses` 는 0 이다 — **0 도 담긴다.**
    """
    결과 = reset_sim_run_ledger(_대역커넥션(), sim_run_id=새실행)

    assert dict(결과.deleted) == _행수
    assert 결과.deleted["expenses"] == 0
    assert 결과.total_deleted == sum(_행수.values())
    assert 결과.sim_run_id == 새실행


def test_실행_행_자체는_안_지운다() -> None:
    """🔴 **다시 여는 것이지 없애는 것이 아니다.** `sim_runs` 행까지 지우면
    설정도 기간도 사라지고, 그 실행이 무엇이었는지 아무 데도 안 남는다.
    """
    conn = _대역커넥션()
    결과 = reset_sim_run_ledger(conn, sim_run_id=새실행)

    assert "sim_runs" not in 결과.order
    assert "sim_runs" not in [_표이름(문장) for 문장, _ in _문장들(conn, "DELETE FROM")]


def test_모든_DELETE_가_축으로만_좁힌다() -> None:
    """★ 조건 없이 지우면 **남의 실행까지** 날아간다."""
    conn = _대역커넥션()
    reset_sim_run_ledger(conn, sim_run_id=새실행)

    문장들 = _문장들(conn, "DELETE FROM")
    assert 문장들
    for 문장, params in 문장들:
        assert '"sim_run_id" = %s' in 문장, f"축으로 안 좁혔다: {문장}"
        assert params == [새실행]


def test_축을_가진_표를_읽어서_정한다() -> None:
    """★★ **손으로 안 적는다.** 축을 가진 표는 마이그레이션이 늘리고 줄인다 —
    손으로 적은 목록은 표가 하나 늘어난 날 조용히 그 표만 안 지운다.
    """
    conn = _대역커넥션(
        표들=("sim_runs", "sales", "새로_생긴_표"),
        fk=(("sales", "sim_runs"), ("새로_생긴_표", "sales")),
        행수={"sales": 3, "새로_생긴_표": 2},
    )
    결과 = reset_sim_run_ledger(conn, sim_run_id=새실행)

    assert 결과.order == ("새로_생긴_표", "sales")
    assert dict(결과.deleted) == {"새로_생긴_표": 2, "sales": 3}


def test_뷰는_대상이_아니다() -> None:
    """★ 뷰에는 DELETE 를 던지지 않는다 — 목록 질의가 `BASE TABLE` 로 좁힌다."""
    conn = _대역커넥션()
    reset_sim_run_ledger(conn, sim_run_id=새실행)

    목록질의 = [문장 for 문장, _ in conn.log if "col.table_name" in 문장]
    assert len(목록질의) == 1
    assert "'BASE TABLE'" in 목록질의[0]


def test_축을_가진_표가_없으면_다_지웠다고_안_한다() -> None:
    """🔴 **빈 목록으로** *"다 지웠다"* **고 답하지 않는다.**"""
    with pytest.raises(LookupError):
        reset_sim_run_ledger(_대역커넥션(표들=("sim_runs",)), sim_run_id=새실행)


def test_고리가_있으면_아무_순서로나_안_던진다() -> None:
    """★ 순서를 못 세우면 터진다 — 반쯤 지워진 장부보다 낫다."""
    conn = _대역커넥션(
        표들=("sim_runs", "가", "나"),
        fk=(("가", "나"), ("나", "가")),
        행수={"가": 1, "나": 1},
    )
    with pytest.raises(RuntimeError):
        reset_sim_run_ledger(conn, sim_run_id=새실행)


def test_지우는_자리가_커밋하지_않는다() -> None:
    """🔴 여기서 커밋하면 장부만 먼저 지워지고, 뒤이어 시작 상태 적재가 터졌을 때
    **출발점 없는 빈 실행**이 남는다.
    """
    conn = _대역커넥션()
    reset_sim_run_ledger(conn, sim_run_id=새실행)

    assert conn.commits == 0


# ── config_json 은 lineage 만 ──────────────────────────────────────────


def test_config_json_에_숫자가_안_들어간다() -> None:
    """🔴 **가리키기만 한다.** 잔액을 설정에 복사하면 원본 행과 복사본이
    **두 진실**이 되고, 원본이 고쳐지는 날 갈린다.
    """
    설정 = 계보.as_config()

    assert 설정 == {"baseline": {"from_sim_run_id": 출발실행, "finance_state_id": 출발상태}}
    for 값 in 설정["baseline"].values():
        assert isinstance(값, str), f"설정에 숫자가 들어왔다: {값!r}"


def test_출발점을_안_가리키면_계보를_못_만든다() -> None:
    """🔴 **지어내지 않는다.**"""
    for 인자 in ({"from_sim_run_id": "  "}, {"finance_state_id": ""}):
        with pytest.raises(ValueError):
            BaselineLineage(**{"from_sim_run_id": 출발실행, "finance_state_id": 출발상태, **인자})


# ── 원문 잠금 ──────────────────────────────────────────────────────────


def test_이름을_안_파싱한다() -> None:
    """🔴 **이름은 사람이 읽는 것**이다. `LOAN` / `BASE` 를 읽어 판정하는 순간
    이름이 사실의 주인이 되고, 그 뒤로 이름을 못 바꾼다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run_open.py")

    for 금지 in (".split(", ".rsplit(", ".partition(", ".startswith(", ".endswith(", "re."):
        assert 금지 not in 원문, f"이름을 되읽는다: {금지}"


def test_조달_방식_이름을_원문이_안_든다() -> None:
    """🔴 **한 값을 보고 다른 값을 추측하지 않는다.** 호출자가 `financing_mode` 와
    `baseline` 을 **함께** 명시한다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run_open.py")

    for 금지 in ("LOAN_BASELINE", "BASE_NO_LOAN", "FIN-DAY30", "DAY30"):
        assert 금지 not in 원문, f"값을 원문에 박았다: {금지}"


def test_표_목록을_원문이_안_든다() -> None:
    """★★ 표 목록의 주인은 `information_schema` 다.

    ⚠️ `sim_runs` 와 `finance_states` 는 예외다 — 각각 **안 지우는 표**와
      **시작 상태를 심는 표**로, 이 절차가 이름으로 지목해야 하는 자리다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run_open.py")

    for 금지 in ("deliveries", "payables", "purchases", "inventory_lots", "receivables"):
        assert 금지 not in 원문, f"표 목록을 손으로 적었다: {금지}"


def test_커넥션을_스스로_안_연다() -> None:
    """🔴 **커넥션은 인자다.** 안에서 열면 이 검사들이 실 DB 로 나가고, 이 판이
    *"DB 에 한 행도 안 쓰고 안 지운다"* 를 못 지킨다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run_open.py")

    for 금지 in ("get_connection", "execute_query", "fetch_one", "fetch_all"):
        assert 금지 not in 원문, f"여는 절차가 커넥션을 스스로 연다: {금지}"


def test_시계를_안_읽는다() -> None:
    """★ `state_date` 도 인자다 — 여기서 시계를 읽으면 같은 실행을 두 번 열 때
    출발 날짜가 갈리고, 그 사실이 어디에도 안 남는다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run_open.py")

    for 금지 in ("now(", "utcnow(", "today(", "seoul_now"):
        assert 금지 not in 원문, f"여는 절차가 시계를 읽는다: {금지}"
