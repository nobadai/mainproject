"""하루 넘김이 물류 fixture 행을 전날에서 물려받는지 재는 검사.

★ **DB 를 부르지 않는다.** 가짜 커넥션·커서로 잰다. 그런데 여기서 재야 하는 것은
  **SQL 이 옮기는 값**이라 `persist_inventory` 검사처럼 파라미터만 봐서는 안 된다 —
  `INSERT ... SELECT` 는 값을 DB 안에서 옮기므로 파이썬 쪽으로 지나가지 않는다.

🔴 **그래서 INSERT 문을 읽어 "어느 칸이 어디서 오는가" 를 표로 만든다.**

```text
칸 목록      INSERT INTO ... ( ... ) 안의 이름들
값 목록      SELECT ... FROM 사이의 식들
표           칸 -> 식        base.x 면 물려받는 것이고 리터럴이면 새로 두는 것이다
```

  그 표로 **물려받은 행을 흉내 내어** 실제 `find_in_transit_schedule_gap` 에 넣는다.
  값 비교를 재구현하지 않는다 — 재구현하면 물류가 규칙을 바꾼 날 검사만 통과한다.

⚠️ 표를 만드는 것이지 SQL 을 실행하는 것이 아니다. 이 검사가 잠그는 것은 **어느 칸을
   물려받기로 했는가**이고, 그 SQL 이 PostgreSQL 에서 도는지는 실 DB 검증 몫이다.
"""

import ast
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from app.logistics import day_open
from app.logistics.day_open import LogisticsDayOpening
from app.logistics.schemas import InTransitItem, ScheduledQuantity
from app.logistics.tools import find_in_transit_schedule_gap
from app.logistics.transition import USAGE_SCOPE, LogisticsFixtureMissing
from app.master import day_open as master_day_open

CARRY_FROM = date(2026, 1, 6)
AS_OF = CARRY_FROM + timedelta(days=1)

#: 전날 행. **어제 떠 있던 입고 한 건**이 `in_transit` 과 `confirmed_inbound` 양쪽에
#: 짝으로 들어 있다 — 승인 전이(`persist_inventory`)가 두 칸을 함께 쓴 뒤의 모양이다.
_떠있는_입고 = {
    "inbound_id": "INB-H1-REQ-9-1",
    "item": "배추",
    "quantity_kg": "300.0",
    "expected_arrival_date": "2026-01-09",
}
_그_입고의_확정일정 = {
    "inbound_id": "INB-H1-REQ-9-1",
    "item": "배추",
    "quantity_kg": "300.0",
    "date": "2026-01-09",
}

기준행: dict[str, Any] = {
    "fixture_id": "LOG-RUNTIME-SIM-BURNIN-202512-DAY30-20260106",
    "sim_run_id": "SIM-BURNIN-202512-DAY30",
    "as_of": CARRY_FROM,
    "in_transit_status": "CONFIRMED",
    "in_transit_json": [_떠있는_입고],
    "confirmed_inbound_status": "CONFIRMED",
    "confirmed_inbound_json": [_그_입고의_확정일정],
    "confirmed_outbound_status": "CONFIRMED_ZERO",
    "confirmed_outbound_json": [],
    "lot_priority_status": "CONFIRMED",
    "lot_priority_json": [{"lot_id": "LOT-001", "priority": 1}],
    "zone_capacity_status": "CONFIRMED",
    "guaranteed_capacity_by_zone_json": {"COLD_HUMID": 8000},
    "usage_scope": USAGE_SCOPE,
    "evidence_grade": "A",
    "approved_by": "logistics-lead",
    "source_ref": "MASTER-DAY-OPEN:2026-01-06",
    "is_active": True,
    "note": "전날 행",
}


# ── 가짜 커넥션 ─────────────────────────────────────────────────────────


class 가짜커서:
    """실행된 SQL 과 파라미터를 기록한다. **어느 날이 열려 있는지는 밖에서 정한다.**"""

    def __init__(self, 열린_날: set[date]) -> None:
        self.열린_날 = 열린_날
        self.queries: list[str] = []
        self.params: list[Any] = []
        self._행: tuple[int, ...] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: object = None) -> None:
        statement = str(query)
        self.queries.append(statement)
        self.params.append(params)
        if "SELECT 1" not in statement:
            self._행 = None
            return
        assert isinstance(params, dict)
        self._행 = (1,) if params["as_of"] in self.열린_날 else None

    def fetchone(self) -> object:
        return self._행


class 가짜커넥션:
    """commit · rollback 이 **몇 번** 불렸나를 센다 — 물류 쪽에서는 0 이어야 한다."""

    def __init__(self, 열린_날: set[date] | None = None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.커서 = 가짜커서(set() if 열린_날 is None else 열린_날)

    def cursor(self) -> 가짜커서:
        return self.커서

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


def _insert_문(conn: 가짜커넥션) -> str:
    문장 = [query for query in conn.커서.queries if "INSERT INTO" in query]
    assert len(문장) == 1, f"INSERT 는 한 번이다 (실제 {len(문장)}번)"
    return 문장[0]


def _insert_파라미터(conn: 가짜커넥션) -> dict[str, Any]:
    params = [
        param
        for query, param in zip(conn.커서.queries, conn.커서.params, strict=True)
        if "INSERT INTO" in query
    ]
    assert len(params) == 1
    assert isinstance(params[0], dict)
    return params[0]


# ── INSERT 문을 표로 만든다 ─────────────────────────────────────────────


_원문 = Path(day_open.__file__).read_text(encoding="utf-8")


def _최상위_쉼표로_나눈다(구절: str) -> list[str]:
    """괄호와 홑따옴표 안의 쉼표는 건드리지 않는다.

    ⚠️ `to_char(%(as_of)s::date, 'YYYYMMDD')` 안의 쉼표로 자르면 표가 통째로 어긋난다.
    """
    조각: list[str] = []
    깊이 = 0
    따옴표 = False
    현재 = ""
    for 글자 in 구절:
        if 따옴표:
            현재 += 글자
            if 글자 == "'":
                따옴표 = False
            continue
        if 글자 == "'":
            따옴표 = True
        elif 글자 == "(":
            깊이 += 1
        elif 글자 == ")":
            깊이 -= 1
        elif 글자 == "," and 깊이 == 0:
            조각.append(현재.strip())
            현재 = ""
            continue
        현재 += 글자
    if 현재.strip():
        조각.append(현재.strip())
    return 조각


def _칸과_식() -> dict[str, str]:
    """`칸 이름 -> 그 칸에 들어갈 식`. INSERT 원문을 읽어 만든다."""
    시작 = _원문.index("INSERT INTO")
    끝 = _원문.index("ON CONFLICT", 시작)
    구문 = _원문[시작:끝]

    여는_괄호 = 구문.index("(")
    깊이 = 0
    for 위치, 글자 in enumerate(구문[여는_괄호:], start=여는_괄호):
        if 글자 == "(":
            깊이 += 1
        elif 글자 == ")":
            깊이 -= 1
            if 깊이 == 0:
                닫는_괄호 = 위치
                break
    칸 = _최상위_쉼표로_나눈다(구문[여는_괄호 + 1 : 닫는_괄호])

    select = 구문.index("SELECT", 닫는_괄호)
    frm = 구문.index("FROM", select)
    식 = _최상위_쉼표로_나눈다(구문[select + len("SELECT") : frm])

    assert len(칸) == len(식), f"칸 {len(칸)}개 · 식 {len(식)}개 — 짝이 안 맞는다"
    return dict(zip(칸, 식, strict=True))


_모른다 = object()


def _값(식: str, base: dict[str, Any], params: dict[str, Any]) -> Any:
    """식 하나를 값으로 바꾼다. 계산할 수 없는 식은 `_모른다` 다."""
    벗긴 = 식
    jsonb = False
    for 캐스트 in ("::date", "::JSONB", "::jsonb"):
        if 벗긴.endswith(캐스트):
            jsonb = jsonb or 캐스트.lower() == "::jsonb"
            벗긴 = 벗긴[: -len(캐스트)]
    if 벗긴.startswith("base."):
        return base[벗긴[len("base.") :]]
    if 벗긴.startswith("%(") and 벗긴.endswith(")s"):
        return params[벗긴[2:-2]]
    if 벗긴 == "TRUE":
        return True
    # ⚠️ `NULL` 은 값이다 — *"아직 확인한 적 없다"*(`UNRESOLVED`)의 짝이라 `_모른다` 로
    #    뭉개면 그 자리를 물려받지 않게 바꾼 변이가 TypeError 로 터진다.
    if 벗긴 == "NULL":
        return None
    if 벗긴.startswith("'") and 벗긴.endswith("'"):
        리터럴 = 벗긴[1:-1]
        return json.loads(리터럴) if jsonb else 리터럴
    return _모른다


def _물려받은_행(params: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """INSERT 가 세울 행을 흉내 낸다. **DB 가 하는 일을 표로 따라간 것이다.**"""
    기준 = 기준행 if base is None else base
    return {칸: _값(식, 기준, params) for 칸, 식 in _칸과_식().items()}


def _스냅샷(행: dict[str, Any], 바탕):
    """물려받은 행을 B-1 이 보는 모양으로 되돌린다."""
    떠있는 = 행["in_transit_json"]
    확정된 = 행["confirmed_inbound_json"]
    return 바탕.model_copy(
        update={
            "in_transit": (
                None if 떠있는 is None else [InTransitItem.model_validate(r) for r in 떠있는]
            ),
            "confirmed_inbound_schedule": (
                None if 확정된 is None else [ScheduledQuantity.model_validate(r) for r in 확정된]
            ),
        }
    )


def _연다(conn: 가짜커넥션 | None = None) -> 가짜커넥션:
    사용할 = 가짜커넥션({CARRY_FROM}) if conn is None else conn
    LogisticsDayOpening().open_day(사용할, as_of=AS_OF, carry_from=CARRY_FROM)
    return 사용할


# ── ① is_open ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("물어본_날", "기대"),
    [(CARRY_FROM, True), (AS_OF, False)],
)
def test_is_open_tells_whether_that_days_row_exists(물어본_날, 기대):
    """① 있는 날 True · 없는 날 False."""
    conn = 가짜커넥션({CARRY_FROM})

    assert LogisticsDayOpening().is_open(conn, as_of=물어본_날) is 기대


def test_is_open_asks_by_usage_scope_and_as_of_only():
    """① `sim_run_id` 를 조건에 넣지 않는다.

    ⚠️ `is_open` 은 인자가 `as_of` 뿐이라 `sim_run_id` 를 **받을 자리가 없다.** 모듈
       상수로 박으면 실행이 둘이 되는 날 물류 코드를 고쳐야 한다. 읽는 쪽
       (`repository.get_active_logistics_runtime_fixture`)도 `usage_scope + as_of` 로
       고르므로 같은 눈으로 본다.
    """
    conn = 가짜커넥션({CARRY_FROM})

    LogisticsDayOpening().is_open(conn, as_of=CARRY_FROM)

    문장 = conn.커서.queries[0]
    assert "sim_run_id" not in 문장
    assert conn.커서.params[0] == {"as_of": CARRY_FROM, "usage_scope": USAGE_SCOPE}


# ── ② open_day 가 물려받아 행을 만든다 ──────────────────────────────────


def test_open_day_inserts_by_selecting_from_the_carry_from_row():
    """② 전날 행을 골라 그 값으로 새 행을 만든다."""
    conn = _연다()

    문장 = _insert_문(conn)
    assert "logistics_runtime_fixture base" in 문장
    assert "base.as_of = %(carry_from)s" in 문장
    params = _insert_파라미터(conn)
    assert params["carry_from"] == CARRY_FROM
    assert params["as_of"] == AS_OF
    assert params["usage_scope"] == USAGE_SCOPE


def test_open_day_carries_the_sim_run_id_from_the_base_row():
    """② `sim_run_id` 는 물려받는 것이지 정하는 것이 아니다.

    🔴 마스터 상수(`BURN_IN_SIM_RUN_ID`)를 가져다 쓰면 실행이 여럿이 되는 날 이 코드만
       옛 값을 들고 남는다.
    """
    assert _칸과_식()["sim_run_id"] == "base.sim_run_id"
    assert "BURN_IN_SIM_RUN_ID" not in _원문
    assert _물려받은_행(_insert_파라미터(_연다()))["sim_run_id"] == 기준행["sim_run_id"]


@pytest.mark.parametrize(
    "칸",
    [
        "confirmed_outbound_status",
        "confirmed_outbound_json",
        "zone_capacity_status",
        "guaranteed_capacity_by_zone_json",
        "usage_scope",
        "evidence_grade",
        "approved_by",
    ],
)
def test_open_day_carries_the_settled_columns(칸):
    """② 어제 정해져 있던 설정값은 그대로 온다 — 창고 구조도 근거 등급도 안 바뀌었다."""
    assert _칸과_식()[칸] == f"base.{칸}"


# ── ③ in_transit 과 confirmed_inbound 는 짝이다 ─────────────────────────


@pytest.mark.parametrize(
    "칸",
    [
        "in_transit_status",
        "in_transit_json",
        "confirmed_inbound_status",
        "confirmed_inbound_json",
    ],
)
def test_open_day_carries_in_transit_and_confirmed_inbound_together(칸):
    """③ 🔴 **네 칸 모두 물려받는다.**

    `in_transit` 은 매입 승인 ~ 창고 도착 ~ 검수 완료까지 여러 날에 걸쳐 유지되는
    상태다. 하루가 넘어갔다고 어제 떠 있던 물건이 사라지지 않는다.

    🔴 그리고 `confirmed_inbound` 는 그 짝이다. 한쪽만 물려받으면 B-1 이 다음 날을
       세운다 (아래 ④).
    """
    assert _칸과_식()[칸] == f"base.{칸}"


def test_open_day_does_not_seed_in_transit_like_the_fixture_sql():
    """③ 씨앗 SQL 과 다른 자리는 여기 하나다.

    `database/27_logistics_runtime_fixture_20260105_20260106.sql` 은 `in_transit` 을
    리터럴로 새로 뒀다. 그것은 관통 Day1/Day2 를 세우려던 파일이라 그랬고, 하루
    넘김은 물려받는다.
    """
    행 = _물려받은_행(_insert_파라미터(_연다()))

    assert 행["in_transit_json"] == 기준행["in_transit_json"]
    assert 행["in_transit_status"] == "CONFIRMED"
    assert 행["confirmed_inbound_json"] == 기준행["confirmed_inbound_json"]


# ── ④ 물려받은 행이 B-1 을 통과한다 ────────────────────────────────────


def test_carried_row_passes_the_b1_gap_rule(complete_logistics_snapshot):
    """④ 🔴 **이 PR 의 핵심이다.** 물려받은 행을 B-1 에 실제로 넣는다.

    ★ **값 비교를 재구현하지 않는다.** `find_in_transit_schedule_gap` 을 그대로 불러
      `None` 이 나오는지 본다.

    ★ 실측으로 겪은 자리다 (2026-09-04). 승인 전이가 `in_transit` 만 채웠더니 다음 날
      물류가 `IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE` 로 경계를 못 냈다 (`#275`).
    """
    행 = _물려받은_행(_insert_파라미터(_연다()))

    스냅샷 = _스냅샷(행, complete_logistics_snapshot)

    assert 스냅샷.in_transit, "전날에 떠 있던 입고가 실제로 실려 있어야 재는 뜻이 있다"
    assert find_in_transit_schedule_gap(스냅샷) is None


def test_carrying_only_in_transit_would_break_the_b1_gap_rule(complete_logistics_snapshot):
    """④-보강: **한쪽만 물려받으면 실제로 다음 날이 선다.**

    ⚠️ 이 검사는 구현을 재지 않는다 — 짝을 깨면 무슨 일이 나는지를 B-1 에게 직접 물어
       위 검사가 무엇을 막고 있는지 눈에 보이게 남긴다.
    """
    한쪽만 = dict(기준행, confirmed_inbound_status="CONFIRMED_ZERO", confirmed_inbound_json=[])
    행 = _물려받은_행(_insert_파라미터(_연다()), base=한쪽만)

    스냅샷 = _스냅샷(행, complete_logistics_snapshot)

    assert find_in_transit_schedule_gap(스냅샷) == "IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE"


# ── ⑤ lot_priority 는 물려받지 않는다 ───────────────────────────────────


def test_open_day_does_not_carry_lot_priority():
    """⑤ 🔴 **판단이라 물려받지 않는다.**

    씨앗 SQL 이 그렇게 적었고(`database/27_...sql` 124행) 그대로 옮겼다. 어제 어느
    로트를 먼저 내보내기로 했는지는 어제의 판단이지 오늘의 사실이 아니다.

    ⚠️ `NULL` 이 아니라 `CONFIRMED_ZERO` · `[]` 다 — 물류가 정한 값이라 바꾸지 않는다.
    """
    표 = _칸과_식()
    assert 표["lot_priority_status"] != "base.lot_priority_status"
    assert 표["lot_priority_json"] != "base.lot_priority_json"

    행 = _물려받은_행(_insert_파라미터(_연다()))
    assert 행["lot_priority_status"] == "CONFIRMED_ZERO"
    assert 행["lot_priority_json"] == []


# ── ⑥ 새로 두는 칸 ─────────────────────────────────────────────────────


def test_open_day_sets_a_new_as_of_and_fixture_id():
    """⑥ `as_of` 와 `fixture_id` 는 새로 둔다 — 물려받으면 같은 행이 두 번 선다."""
    표 = _칸과_식()
    assert 표["as_of"] == "%(as_of)s::date"
    assert 표["fixture_id"] != "base.fixture_id"
    assert "base.sim_run_id" in 표["fixture_id"], "id 는 물려받은 실행 id 로 만든다"
    assert "%(as_of)s" in 표["fixture_id"], "그날 날짜가 id 에 들어간다"

    assert _물려받은_행(_insert_파라미터(_연다()))["as_of"] == AS_OF


def test_open_day_source_ref_does_not_look_like_an_approval():
    """⑥ 🔴 **승인이 만든 것처럼 보이면 안 된다.**

    01-02 씨앗이 `MASTER-APPROVAL:RT-1` 이라 **없는 승인을 가리키는** 문제가 있었다
    (2026-09-04 실측). 이 행을 만든 것은 승인이 아니라 하루 넘김이다.
    """
    행 = _물려받은_행(_insert_파라미터(_연다()))

    assert 행["source_ref"] == f"MASTER-DAY-OPEN:{AS_OF}"
    assert "APPROVAL" not in 행["source_ref"]
    assert 행["note"], "왜 이 행이 생겼는지 한 문장이 남는다"
    assert str(CARRY_FROM) in 행["note"], "어느 날에서 물려받았는지 적는다"


def test_open_day_does_not_insert_twice_for_the_same_day():
    """⑥ 마스터가 `is_open` 으로 거르지만 `ON CONFLICT` 로 한 겹 더 둔다.

    ⚠️ 막을 것이 둘이다 — PK `fixture_id` 와 UNIQUE
       `uq_log_runtime_fixture (sim_run_id, as_of, usage_scope)`.
    """
    assert "ON CONFLICT DO NOTHING" in _insert_문(_연다())


# ── ⑦ 물려받을 행이 없으면 ─────────────────────────────────────────────


def test_open_day_raises_when_the_carry_from_row_is_missing():
    """⑦ 🔴 **만들지 않고 예외를 던진다.**

    물려받을 곳이 없는데 행을 세우면 `evidence_grade` · `approved_by` · status 들을
    기본값으로 지어내게 되고, **지어낸 값이 그날의 사실로 남는다.**
    """
    conn = 가짜커넥션(열린_날=set())

    with pytest.raises(LogisticsFixtureMissing) as excinfo:
        LogisticsDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    메시지 = str(excinfo.value)
    assert str(CARRY_FROM) in 메시지
    assert USAGE_SCOPE in 메시지
    assert not [query for query in conn.커서.queries if "INSERT INTO" in query], "행을 안 만든다"


# ── ⑧ 트랜잭션은 마스터 것이다 ─────────────────────────────────────────


def test_open_day_does_not_commit_or_roll_back():
    """⑧ 🔴 커밋은 파트가 모두 끝난 뒤 마스터가 한 번 한다."""
    conn = _연다()

    assert conn.commits == 0
    assert conn.rollbacks == 0
    assert conn.closes == 0


def test_open_day_does_not_open_its_own_connection():
    """⑧ 원문에 `get_connection` 이 없다 — 마스터가 쥔 트랜잭션 밖에서 쓰면 안 된다."""
    assert "get_connection" not in _원문


def test_open_day_does_not_reuse_the_approval_write_path():
    """⑧ 승인 쓰기 경로를 **가져다 쓰지 않는다** — 다른 시점의 다른 쓰기다.

    ★ `transition.py` 에서 가져오는 것은 값 둘뿐이다. `persist_inventory` 를 부르면
      승인이 소유한 UPDATE 가 하루 넘김 경로에 얹혀 두 사실의 주인이 섞인다.
    """
    가져온_이름 = {
        alias.name
        for node in ast.walk(ast.parse(_원문))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "persist_inventory" not in 가져온_이름
    assert "build_next_inventory" not in 가져온_이름
    assert {"USAGE_SCOPE", "LogisticsFixtureMissing"} <= 가져온_이름


# ── ⑨ 등록 뒤 마스터가 실제로 부른다 ───────────────────────────────────


def test_master_open_day_walks_logistics_after_registration():
    """⑨ 등록하면 마스터 경계가 물류를 실제로 부른다.

    ★ 마스터가 `is_open` 으로 뒤로 걸어 전날을 찾고, 그 다음 날부터 `open_day` 를
      부른다. 커밋은 **마스터가** 한 번 한다 — 물류는 0 번이다 (⑧).
    """
    # 🔴 **물류만 남기고 잰다 (2026-09-05).** `app/main.py` 가 이제 재무도 등록하므로
    #    비우지 않으면 이 가짜 커넥션이 재무 질의까지 받게 되고, 그러면 이 검사가
    #    재는 것이 "물류를 부르는가" 가 아니라 "두 파트가 다 도는가" 로 바뀐다.
    master_day_open.reset()
    master_day_open.register_day_opening("logistics", LogisticsDayOpening())
    conn = 가짜커넥션({CARRY_FROM})

    결과 = master_day_open.open_day(AS_OF, connect=lambda: conn)

    assert 결과.status == "OPENED"
    assert [part.part for part in 결과.parts] == ["logistics"]
    assert 결과.parts[0].opened == [AS_OF]
    assert 결과.missing == ["finance"], "이 검사는 물류만 등록해 물류 경로만 잰다"
    assert "INSERT INTO" in _insert_문(conn)
    assert conn.commits == 1, "커밋은 마스터가 한 번 한다"


def test_master_open_day_is_idempotent_for_an_already_open_day():
    """⑨ 이미 열려 있으면 아무것도 안 한다 — INSERT 자체가 없다."""
    master_day_open.reset()  # 위와 같은 이유 — 물류만 남기고 잰다
    master_day_open.register_day_opening("logistics", LogisticsDayOpening())
    conn = 가짜커넥션({CARRY_FROM, AS_OF})

    결과 = master_day_open.open_day(AS_OF, connect=lambda: conn)

    assert 결과.status == "NOT_OPENED"
    assert 결과.parts[0].opened == []
    assert not [query for query in conn.커서.queries if "INSERT INTO" in query]
