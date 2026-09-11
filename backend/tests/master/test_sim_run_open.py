"""**새 실행을 여는 절차** (2026-09-10 · `#531` · `#539` 의 후속).

```text
① 새 실행 한 행          create_sim_run           이미 있다 (#531)
② 시작 재무 상태          seed_opening_finance_state   ← 이 판
③ 다시 열 때 장부 비우기   reset_sim_run_ledger         ← 이 판
③' 다시 열 때 실행 행 삭제 delete_sim_run_row           ← 이 판
```

★★ **③ 과 ③' 은 다른 일이다.** ③ 은 *"다시 여는 것이지 없애는 것이 아니다"* 라
  실행 행에 손대지 않는다. ③' 은 `--reset` 이 정하는 일이고, `create_sim_run` 에
  `ON CONFLICT` 를 붙이지 않고 같은 이름으로 다시 열 수 있게 하는 유일한 길이다.

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
from types import SimpleNamespace
from typing import Any, Self

import pytest
from psycopg.errors import ForeignKeyViolation

from app.finance.db import get_db_schema
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.sim_run_open import (
    LOGISTICS_FIXTURE_TABLE,
    RUN_TABLE,
    BaselineLineage,
    delete_sim_run_row,
    reset_sim_run_ledger,
    seed_opening_finance_state,
    seed_opening_logistics_fixture,
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

#: 이 검사가 이관해 올 물류 사실의 `usage_scope`.
#:   🔴 **어휘의 주인은 물류다** — 검사가 값을 들되 운영 코드는 안 든다
#:   (`test_usage_scope_를_원문이_안_든다` 가 그것을 잰다).
쓰임 = "AGENT_MVP_DEMO"
물류씨앗 = "LOG-WALK-202601-OPEN"

#: `logistics_runtime_fixture` 의 칸 — 실제 DDL 을 그대로 본뜬다.
_물류칸목록: tuple[tuple[str, str], ...] = (
    ("fixture_id", "NEVER"),
    ("sim_run_id", "NEVER"),
    ("as_of", "NEVER"),
    ("in_transit_status", "NEVER"),
    ("in_transit_json", "NEVER"),
    ("confirmed_inbound_status", "NEVER"),
    ("confirmed_inbound_json", "NEVER"),
    ("confirmed_outbound_status", "NEVER"),
    ("confirmed_outbound_json", "NEVER"),
    ("usage_scope", "NEVER"),
    ("evidence_grade", "NEVER"),
    ("source_ref", "NEVER"),
    ("approved_by", "NEVER"),
    ("is_active", "NEVER"),
    ("note", "NEVER"),
    ("created_at", "NEVER"),
    ("updated_at", "NEVER"),
    ("lot_priority_status", "NEVER"),
    ("lot_priority_json", "NEVER"),
    ("zone_capacity_status", "NEVER"),
    ("guaranteed_capacity_by_zone_json", "NEVER"),
)

#: `NOT NULL` 인 칸. 일부만 나르면 나머지를 어디서 채울지가 또 생긴다.
_물류_NOT_NULL_칸 = (
    "fixture_id",
    "sim_run_id",
    "as_of",
    "in_transit_status",
    "confirmed_inbound_status",
    "confirmed_outbound_status",
    "usage_scope",
    "evidence_grade",
    "source_ref",
    "approved_by",
    "is_active",
)

#: 🔴 **이관하지 않는 칸.** `NOT NULL` 이지만 DB 기본값(`now()`)이 채운다.
#:
#: ★★ 축이 다르다 — `evidence_grade` 는 *"이 사실이 어디서 왔나"* 라 따라가는 것이
#:   맞고, 이 둘은 *"이 행이 언제 쓰였나"* 다. 이관본은 **지금** 쓰인 새 행이라
#:   source 시각을 나르면 두 달 전에 만들어졌다고 말하게 된다.
_행이_쓰인_시각_칸 = ("created_at", "updated_at")

#: 🔴 **물류 소유의 근거 셋**. 마스터가 새로 쓰지 않고 그대로 따라간다.
_근거셋 = ("evidence_grade", "source_ref", "approved_by")

#: 이관해 올 물류 fixture 한 행. 번인 2025-12-31 행을 그대로 본뜬다.
_물류출발행: dict[str, Any] = {
    "fixture_id": "LOG-BURNIN-DAY30",
    "sim_run_id": 출발실행,
    "as_of": date(2025, 12, 31),
    "in_transit_status": "CONFIRMED_ZERO",
    "in_transit_json": None,
    "confirmed_inbound_status": "CONFIRMED_ZERO",
    "confirmed_inbound_json": None,
    "confirmed_outbound_status": "CONFIRMED_ZERO",
    "confirmed_outbound_json": None,
    "usage_scope": 쓰임,
    "evidence_grade": "SIM_FIXED",
    "source_ref": "MVP-DECISION-20260825:LOG-RUNTIME-DAY30",
    "approved_by": "HUMAN",
    "is_active": True,
    "note": "번인 30일 물류 고정",
    "created_at": "2026-08-25T00:00:00+09:00",
    "updated_at": "2026-08-25T00:00:00+09:00",
    "lot_priority_status": "CONFIRMED_ZERO",
    "lot_priority_json": None,
    "zone_capacity_status": "UNRESOLVED",
    "guaranteed_capacity_by_zone_json": None,
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


#: 🔴 **예외 문장에 표 이름을 안 담는다.** 담으면 `diag` 를 안 읽고 예외 문장만
#:   베껴 붙인 뮤턴트가 살아남는다 — 진짜 psycopg 는 둘 다 들고 오지만, 여기서
#:   재는 것은 *"제약이 들고 온 것을 읽어서 적는가"* 다.
_FK사유 = "실행 행을 지울 수 없다"


class _FK위반(ForeignKeyViolation):
    """FK 가 막는 그 예외. **제약 이름과 남은 표를 `diag` 로 들고 온다.**"""

    def __init__(self, msg: str, *, table: str, constraint: str) -> None:
        super().__init__(msg)
        self._진단 = SimpleNamespace(table_name=table, constraint_name=constraint)

    @property
    def diag(self) -> SimpleNamespace:  # type: ignore[override]
        return self._진단


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
            # ★ 칸 목록은 **표마다 다르다** — 질의가 어느 표를 물었는지로 고른다.
            물은표 = (list(params or []) + [None, None])[1]
            목록 = (
                self.대장.물류칸목록
                if 물은표 == LOGISTICS_FIXTURE_TABLE
                else self.대장.칸목록
            )
            self._rows = [
                {"column_name": name, "is_generated": gen, "identity_generation": None}
                for name, gen in 목록
            ]
        elif "col.table_name" in 문장:
            self._rows = [{"table_name": one} for one in self.대장.표들]
        elif "DELETE FROM" in 문장:
            표 = _표이름(문장)
            # 🔴 **DB 가 막는 자리를 대역도 막는다.** 실행 행을 지우려는데 장부가
            #    남아 있으면 FK 가 터뜨린다 — 그 막힘이 자기 검사다.
            if 표 == RUN_TABLE and self.대장.FK막힘 is not None:
                남은표, 제약 = self.대장.FK막힘
                raise _FK위반(_FK사유, table=남은표, constraint=제약)
            self.rowcount = self.대장.행수.get(표, 0)
        elif "finance_state_id = %s" in 문장 and "SELECT" in 문장:
            self._one = self.대장.출발행
        elif "usage_scope = %s" in 문장 and "SELECT" in 문장:
            # ★ 대역이 **셋을 다 확인하고** 답한다 — 하나라도 안 좁히면 이 대역이
            #   *"못 찾았다"* 를 내고, 그래야 좁히기를 거른 뮤턴트가 안 산다.
            찾는것 = list(params or [])
            있는것 = self.대장.물류출발행
            self._one = (
                있는것
                if 있는것 is not None
                and 찾는것
                == [있는것["sim_run_id"], 있는것["as_of"], 있는것["usage_scope"]]
                else None
            )

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._one


class _대역커넥션:
    def __init__(self, **over: Any) -> None:
        self.log: list[tuple[str, list[Any]]] = []
        self.commits = 0
        self.칸목록 = over.pop("칸목록", _칸목록)
        self.물류칸목록 = over.pop("물류칸목록", _물류칸목록)
        self.표들 = over.pop("표들", _표들)
        self.fk = over.pop("fk", _FK)
        self.행수 = over.pop("행수", _행수)
        #: `(남은 표, 제약 이름)` 이면 실행 행 삭제가 FK 로 막힌다. 🟢 자기 검사.
        self.FK막힘: tuple[str, str] | None = over.pop("FK막힘", None)
        출발행 = over.pop("출발행", _출발행)
        self.출발행 = dict(출발행) if 출발행 is not None else None
        물류출발행 = over.pop("물류출발행", _물류출발행)
        self.물류출발행 = dict(물류출발행) if 물류출발행 is not None else None
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


def _실린다(conn: _대역커넥션, 표: str = "finance_states") -> dict[str, Any]:
    """INSERT 한 문장에서 `칸 이름 → 실린 값` 을 되짚는다."""
    실은것 = _문장들(conn, "INSERT INTO")
    assert len(실은것) == 1, f"한 행이 아니다: {실은것}"
    문장, params = 실은것[0]
    assert f'"{_스키마}"."{표}"' in 문장, f"{표} 에 안 실었다: {문장}"
    칸들 = re.findall(r'"([^"]+)"', 문장.split("VALUES")[0])
    칸들 = [one for one in 칸들 if one not in (_스키마, 표)]
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


def test_note_는_안_주면_출발점_것을_잇고_주면_덮는다() -> None:
    """★ `note` 는 identity 도 재무 값도 아니다 — **그래서 여기서 정한다.**

    ⚠️ 안 주면 출발점의 설명이 딸려온다. 새 실행의 첫 행에 *"번인 30일 끝"* 이
      붙는 것이 어색하면 **부르는 쪽이 덮는다** — 여기서 조용히 비우지 않는다.
      비우면 *"설명이 원래 없었다"* 와 *"우리가 지웠다"* 가 같아진다.
    """
    이어받은 = _실린다(_심는다()[0])
    덮은 = _실린다(_심는다(note="2026 걷기 시작")[0])

    assert 이어받은["note"] == _출발행["note"]
    assert 덮은["note"] == "2026 걷기 시작"


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


# ── ②' 시작 물류 fixture ───────────────────────────────────────────────


def _물류를_심는다(**over: Any) -> tuple[_대역커넥션, str]:
    conn = _대역커넥션(
        **{k: over.pop(k) for k in ("물류출발행", "물류칸목록") if k in over}
    )
    인자: dict[str, Any] = {
        "sim_run_id": 새실행,
        "baseline_run_id": 출발실행,
        "fixture_id": 물류씨앗,
        "as_of": date(2025, 12, 31),
        "usage_scope": 쓰임,
    }
    인자.update(over)
    return conn, seed_opening_logistics_fixture(conn, **인자)


def _물류조회(conn: _대역커넥션) -> tuple[str, list[Any]]:
    조회 = [
        (문장, params)
        for 문장, params in conn.log
        if "SELECT" in 문장 and "usage_scope = %s" in 문장
    ]
    assert len(조회) == 1, f"물류 source 를 한 번만 찾아야 한다: {조회}"
    return 조회[0]


def test_물류_source_를_셋으로_찾는다() -> None:
    """🔴 **`(baseline_run_id, as_of, usage_scope)` 셋이다.**

    ★★ 그 셋이 정확히 `uq_log_runtime_fixture` 이고, 물류 `is_open` 이 묻는 열쇠와
      **같다** — 그래서 최대 한 행이고, 그래서 여기서 놓은 행을 물류가 읽는다.
      하나라도 빼면 남의 실행/남의 날/남의 scope 행을 이관해 올 수 있다.
    """
    conn, _ = _물류를_심는다()

    문장, params = _물류조회(conn)
    assert '"sim_run_id" = %s' in 문장, f"실행으로 안 좁혔다: {문장}"
    assert "as_of = %s" in 문장, f"날짜로 안 좁혔다: {문장}"
    assert "usage_scope = %s" in 문장, f"scope 로 안 좁혔다: {문장}"
    assert params == [출발실행, date(2025, 12, 31), 쓰임]


def test_물류_source_를_새_실행이_아니라_baseline_에서_찾는다() -> None:
    """🔴 **새 실행에는 물류 행이 없다** — 그것이 이 씨앗이 서는 이유다.

    ⚠️ 좁히는 축에 새 실행 id 를 넣으면 늘 0건이 나오고, 그때 이 절차는 영원히
      *"이관할 것이 없다"* 만 답한다.
    """
    conn, _ = _물류를_심는다()

    _, params = _물류조회(conn)
    assert params[0] == 출발실행
    assert params[0] != 새실행, "새 실행에서 source 를 찾는다 — 거기엔 한 행도 없다"


def test_물류_씨앗의_identity_가_새것이다() -> None:
    """🔴 **fixture_id 와 sim_run_id 는 새로 정한다.**

    ★★ 안 덮으면 새 실행의 첫 물류 행에 **남의 실행 id 가 실리고**, 그러면 물류가
      제 실행에서 그 행을 못 읽는다 (`is_open` 이 `sim_run_id` 로 좁힌다).
    """
    conn, 이름 = _물류를_심는다()

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    assert 이름 == 물류씨앗
    assert 실린것["fixture_id"] == 물류씨앗
    assert 실린것["fixture_id"] != _물류출발행["fixture_id"]
    assert 실린것["sim_run_id"] == 새실행
    assert 실린것["sim_run_id"] != _물류출발행["sim_run_id"]


def test_물류_값은_source_에서_그대로_이관된다() -> None:
    """🔴 **지어내지 않는다.** `note` 와 identity 둘 말고는 전부 따라간다."""
    conn, _ = _물류를_심는다()

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    for 칸, 값 in _물류출발행.items():
        if 칸 in ("fixture_id", "sim_run_id", "note", *_행이_쓰인_시각_칸):
            continue
        assert 실린것[칸] == 값, f"{칸} 이 source 에서 안 왔다: {실린것[칸]!r} != {값!r}"


def test_행이_쓰인_시각을_이관하지_않는다() -> None:
    """🔴 **`created_at` · `updated_at` 은 이관 대상이 아니다.**

    ★★ 그 둘은 사실이 언제 생겼나가 아니라 **이 행이 언제 쓰였나**이고, 이관본은
      **지금** 쓰인 새 행이다. source 값을 나르면 새 행이 두 달 전에 만들어졌다고
      말하게 된다 — 없는 사실이고, **에러 없이 틀린 값**이다.

    ★ 안 실으면 DB 기본값(`now()`)이 선다. 마스터가 시각을 지어내지도 않는다.
    """
    conn, _ = _물류를_심는다()

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    실린칸 = set(실린것)
    따라온것 = [칸 for 칸 in _행이_쓰인_시각_칸 if 칸 in 실린칸]
    assert not 따라온것, f"이관본이 source 의 시각을 들고 왔다: {따라온것}"


def test_is_active_는_따라간다() -> None:
    """🔴 **`is_active` 는 기본값이 있어도 이관한다.**

    ★★ *"이 사실이 살아 있나"* 는 **물류의 판정**이다. source 가 죽어 있는데
      마스터가 살려 놓으면 물류가 내린 판정을 뒤집는 것이 된다 — 기본값이 있다는
      것만으로 `created_at` 과 같이 묶으면 그 뒤집기가 조용히 일어난다.
    """
    죽은행 = {**_물류출발행, "is_active": False}
    conn, _ = _물류를_심는다(물류출발행=죽은행)

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    assert 실린것["is_active"] is False, "마스터가 물류의 판정을 뒤집었다"


def test_근거_셋을_마스터가_새로_쓰지_않는다() -> None:
    """🔴 **`evidence_grade` · `source_ref` · `approved_by` 는 물류 소유다.**

    ★★ 그 셋은 *"이 사실이 어디서 왔고 누가 승인했나"* 다. 마스터가 새로 쓰면
      물류가 승인한 적 없는 근거가 물류 표에 앉는다 — 같은 사실이니 그대로 따라가고,
      **되짚을 수 있게** `note` 가 출처를 단다.
    """
    conn, _ = _물류를_심는다()

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    for 칸 in _근거셋:
        assert 실린것[칸] == _물류출발행[칸], f"마스터가 {칸} 을 새로 썼다"


def test_as_of_를_새로_정하지_않는다() -> None:
    """★★ **source 를 찾은 그 날짜 그대로다.** 같은 날의 같은 사실을 다른 실행 축에
    앉히는 것이라 날짜가 바뀔 이유가 없다.
    """
    conn, _ = _물류를_심는다()

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    assert 실린것["as_of"] == date(2025, 12, 31)
    assert 실린것["as_of"] == _물류출발행["as_of"]


def test_note_에_source_의_fixture_id_가_남는다() -> None:
    """🔴 **되짚을 수 있어야 한다.** 근거 셋을 그대로 따라가므로, *"어느 행에서
    왔나"* 를 여기 안 적으면 출처가 어디에도 안 남는다.
    """
    conn, _ = _물류를_심는다()

    적힌것 = _NFC(str(_실린다(conn, LOGISTICS_FIXTURE_TABLE)["note"]))
    assert _NFC(_물류출발행["fixture_id"]) in 적힌것, f"출처를 안 적었다: {적힌것}"
    assert _NFC(출발실행) in 적힌것
    assert 적힌것 != _NFC(str(_물류출발행["note"])), "source 의 메모를 그대로 이었다"


def test_물류_source_가_없으면_터진다() -> None:
    """🔴 **막는다.** 다른 날짜로 물러서지도 다른 scope 를 뒤지지도 않는다."""
    with pytest.raises(LookupError) as err:
        _물류를_심는다(물류출발행=None)

    말 = _NFC(str(err.value))
    assert _NFC(출발실행) in 말 and _NFC(쓰임) in 말 and "2025-12-31" in 말


def test_물류_source_가_없으면_빈_행을_안_지어낸다() -> None:
    """🔴 **지어내면 그 하루가 「물류가 확인한 날」로 둔갑한다.**

    ★★ 물류는 이 표를 보고 *"내가 그날을 열었다"* 를 판정한다 — 빈 행을 놓으면
      물류가 확인한 적 없는 날이 열린 것으로 읽히고, 그 위로 179일이 걸린다.
    """
    conn = _대역커넥션(물류출발행=None)
    with pytest.raises(LookupError):
        seed_opening_logistics_fixture(
            conn,
            sim_run_id=새실행,
            baseline_run_id=출발실행,
            fixture_id=물류씨앗,
            as_of=date(2025, 12, 31),
            usage_scope=쓰임,
        )

    assert not _문장들(conn, "INSERT INTO"), "source 가 없는데 행을 실었다"


def test_다른_날짜로_물러서지_않는다() -> None:
    """🔴 **한 번만 찾고 만다.** 못 찾았다고 다른 날을 뒤지면, 이관해 온 사실이
    우리가 말한 그날의 사실이 아니게 된다.
    """
    conn = _대역커넥션(물류출발행=None)
    with pytest.raises(LookupError):
        seed_opening_logistics_fixture(
            conn,
            sim_run_id=새실행,
            baseline_run_id=출발실행,
            fixture_id=물류씨앗,
            as_of=date(2025, 12, 31),
            usage_scope=쓰임,
        )

    조회 = [문장 for 문장, _ in conn.log if "SELECT" in 문장 and "usage_scope = %s" in 문장]
    assert len(조회) == 1, f"source 를 {len(조회)} 번 찾았다 — 물러섰다"


def test_물류_NOT_NULL_칸을_하나도_안_빠뜨린다() -> None:
    """★ 일부만 고르면 나머지를 어디서 채울지가 또 생긴다."""
    conn, _ = _물류를_심는다()

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    빠진것 = [칸 for 칸 in _물류_NOT_NULL_칸 if 칸 not in 실린것]
    assert not 빠진것, f"NOT NULL 칸을 빠뜨렸다: {빠진것}"


def test_물류_칸_목록이_information_schema_에서_온다() -> None:
    """⚠️ **손으로 적지 않는다.** 물류가 칸을 더하는 날 손으로 적은 목록은 조용히
    뒤처지고, 그때 새 칸은 `NOT NULL` 이면 INSERT 를 막고 아니면 말없이 빈다.
    """
    늘어난칸 = (*_물류칸목록, ("물류가_내일_더할_칸", "NEVER"))
    늘어난행 = {**_물류출발행, "물류가_내일_더할_칸": "따라와야 한다"}
    conn, _ = _물류를_심는다(물류칸목록=늘어난칸, 물류출발행=늘어난행)

    실린것 = _실린다(conn, LOGISTICS_FIXTURE_TABLE)
    assert 실린것["물류가_내일_더할_칸"] == "따라와야 한다"


def test_물류_칸을_information_schema_에_물을_때_그_표를_묻는다() -> None:
    """★ 재무 표의 칸으로 물류 행을 실으면 칸이 통째로 어긋난다."""
    conn, _ = _물류를_심는다()

    칸질의 = [params for 문장, params in conn.log if "is_generated" in 문장]
    assert len(칸질의) == 1
    assert 칸질의[0] == [_스키마, LOGISTICS_FIXTURE_TABLE]


@pytest.mark.parametrize("빈값", ["", "   "])
@pytest.mark.parametrize(
    "칸", ["sim_run_id", "baseline_run_id", "fixture_id", "usage_scope"]
)
def test_빈_값으로_물류_씨앗을_못_만든다(칸: str, 빈값: str) -> None:
    """🔴 **없는 값을 메우지 않는다.**"""
    with pytest.raises(ValueError) as err:
        _물류를_심는다(**{칸: 빈값})

    assert _NFC(칸) in _NFC(str(err.value))


def test_물류_씨앗을_심는_자리가_커밋하지_않는다() -> None:
    """🔴 **커밋은 부르는 쪽이 한다** — `seed_opening_finance_state` 와 같다.

    ⚠️ 여기서 커밋하면 물류 씨앗만 먼저 앉고, 뒤이어 무엇이 터져도 그 행은 남는다.
    """
    conn, _ = _물류를_심는다()

    assert conn.commits == 0


def test_usage_scope_를_원문이_안_든다() -> None:
    """🔴 **어휘의 주인은 물류다.**

    ★★ 마스터가 제 코드에 박으면 물류가 값을 바꾸는 날 말없이 갈린다 — 그때
      마스터가 놓은 씨앗을 물류가 못 읽고, 개장은 다시 거절한다. 물류 상수를
      import 해 오는 것도 같은 이유로 안 된다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run_open.py")

    for 금지 in (쓰임, "USAGE_SCOPE", "LOGISTICS_POLICY_USAGE_SCOPE", "app.logistics"):
        assert 금지 not in 원문, f"물류 어휘를 마스터에 박았다: {금지}"


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


# ── ③' 실행 행을 지운다 ────────────────────────────────────────────────
#
# ★★ **③ 과 다른 일이다.** 장부 비우기는 실행 행에 손대지 않고, 그 규율은 지금도
#    맞다. 실행 행을 지우는 것은 `--reset` 이 정한다.

#: 실행 행 한 행이 지워지는 대역. 장부 표들은 그대로 두고 `sim_runs` 만 더한다.
_실행행있음 = {**_행수, RUN_TABLE: 1}


def test_실행_행을_축으로_좁혀_지운다() -> None:
    """🔴 **그 실행 하나만 지운다.** 조건 없이 지우면 남의 실행까지 날아간다."""
    conn = _대역커넥션(행수=_실행행있음)
    지운수 = delete_sim_run_row(conn, sim_run_id=새실행)

    던진것 = _문장들(conn, "DELETE FROM")
    assert len(던진것) == 1, f"한 문장이 아니다: {던진것}"
    문장, params = 던진것[0]
    assert _표이름(문장) == RUN_TABLE, f"실행 행이 사는 표에 안 던졌다: {문장}"
    assert '"sim_run_id" = %s' in 문장, f"축으로 안 좁혔다: {문장}"
    assert params == [새실행]
    assert 지운수 == 1


def test_지울_실행_행이_없어도_막지_않는다() -> None:
    """★ **0 행도 정상이다.** 같은 이름으로 처음 여는 자리에는 지울 행이 없고,
    그때 터뜨리면 여는 것 자체가 막힌다. 몇 행이었는지는 돌려준다.
    """
    assert delete_sim_run_row(_대역커넥션(), sim_run_id=새실행) == 0


@pytest.mark.parametrize("빈값", ["", "   "])
def test_축이_비면_실행_행을_못_지운다(빈값: str) -> None:
    """★ 어느 실행인지를 여기서 지어내지 않는다."""
    conn = _대역커넥션(행수=_실행행있음)
    with pytest.raises(ValueError):
        delete_sim_run_row(conn, sim_run_id=빈값)

    assert not _문장들(conn, "DELETE FROM")


def test_장부가_남아_있으면_FK_가_막는다() -> None:
    """🟢 **그 막힘이 자기 검사다.**

    ★★ 지우기를 빠뜨린 표가 있으면 **시끄럽게** 드러난다 — 조용히 반쪽 실행이
      서는 것보다 낫다.
    """
    conn = _대역커넥션(행수=_실행행있음, FK막힘=("sales", "fk_sales_sim_run"))
    with pytest.raises(RuntimeError) as err:
        delete_sim_run_row(conn, sim_run_id=새실행)

    assert isinstance(err.value.__cause__, ForeignKeyViolation), "FK 가 막은 사실이 안 남았다"


def test_FK_에_막히면_어느_표가_남았는지_사유에_적는다() -> None:
    """⚠️ **제약이 들고 온 것을 읽어서 적는다.**

    🔴 버리고 *"못 지웠다"* 만 남기면 사람이 어느 표가 남았는지를 다시 찾아 헤맨다.
    """
    conn = _대역커넥션(행수=_실행행있음, FK막힘=("sales", "fk_sales_sim_run"))
    with pytest.raises(RuntimeError) as err:
        delete_sim_run_row(conn, sim_run_id=새실행)

    사유 = _NFC(str(err.value))
    assert "sales" in 사유, f"어느 표가 남았는지가 없다: {사유}"
    assert "fk_sales_sim_run" in 사유, f"제약 이름이 없다: {사유}"
    assert _NFC(새실행) in 사유


def test_실행_행_삭제가_커밋하지_않는다() -> None:
    """🔴 커밋은 부르는 쪽이 한다 — 여기서 커밋하면 실행 행만 먼저 사라지고,
    뒤이어 만들기가 터졌을 때 **설정도 기간도 없는 자리**가 남는다.
    """
    conn = _대역커넥션(행수=_실행행있음)
    delete_sim_run_row(conn, sim_run_id=새실행)

    assert conn.commits == 0


def test_장부_비우기는_실행_행에_손대지_않는다() -> None:
    """🟡 **③ 의 규율은 지금도 맞다.** 장부만 비우려는 자리에서 실행 행까지
    날아가면 *"다시 여는 것이지 없애는 것이 아니다"* 가 거짓이 된다.
    """
    conn = _대역커넥션(행수=_실행행있음)
    결과 = reset_sim_run_ledger(conn, sim_run_id=새실행)

    assert RUN_TABLE not in 결과.order
    assert RUN_TABLE not in [_표이름(문장) for 문장, _ in _문장들(conn, "DELETE FROM")]


def test_번인이면_장부_비우기에서_먼저_터진다() -> None:
    """🔴 **번인 가드를 실행 행 삭제가 다시 만들지 않는다.**

    ★★ `--reset` 은 ① 장부 비우기를 먼저 부르고 거기서 터진다 — 두 곳에서 막으면
      언젠가 한쪽만 고쳐지고, 그때 어느 쪽이 진짜 규칙인지 아무도 못 답한다.
      그 순서는 문(`sim_run_runner`)이 잰다.
    """
    conn = _대역커넥션(행수=_실행행있음)
    with pytest.raises(ValueError) as err:
        reset_sim_run_ledger(conn, sim_run_id=BURN_IN_SIM_RUN_ID)

    assert _NFC(BURN_IN_SIM_RUN_ID) in _NFC(str(err.value))
    assert not _문장들(conn, "DELETE FROM"), "번인에 DELETE 를 던졌다"


def test_CASCADE_로_풀지_않는다() -> None:
    """🔴 **`ON DELETE CASCADE` 를 달거나 FK 를 끄면 자기 검사가 사라진다.**

    ⚠️ 그러면 지우기를 빠뜨린 표가 있어도 아무 소리 없이 다 지워지고, 다시 연
      실행이 정말 빈 장부에서 출발했는지 아무도 못 답한다.

    ★ **DDL 로 가는 길을 잰다.** 사유 문장에 *"CASCADE 를 달지 않는다"* 라고 적는
      것은 막을 일이 아니다 — 조각으로 재면 그 문장이 잡혀 검사가 코드가 아니라
      설명을 재게 된다.
    """
    원문 = _NFC(_벗긴_원문(_MASTER / "sim_run_open.py"))

    for 금지 in (
        "ON DELETE CASCADE",
        "DROP CONSTRAINT",
        "ALTER TABLE",
        "DISABLE TRIGGER",
        "session_replication_role",
    ):
        assert 금지 not in 원문, f"자기 검사를 끄는 길로 갔다: {금지}"


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
