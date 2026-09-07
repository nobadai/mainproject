"""최종 승인 시점 재검증의 **기록 자리** — M-3.

2026-09-04 에 판매와 확정했다. 최종 승인 클릭 시점에 **그때 선택된 1안을 다시
검증하고**, 그 결과를 결정 행에 적는다.

```text
decision                 APPROVE · REJECT_ALL · REQUEST_CHANGE · CANCEL   사용자가 무엇을 눌렀나
revalidation_request_id  재검증이 도는 **새** request_id                   ← 신설
revalidation_outcome     PASSED · CONDITIONAL · FAILED · ERROR             ← 신설
```

⚠️ **이 조각은 저장 자리까지다.** 재검증을 실제로 도는 것은 M-4 다. 그래서 지금
  두 칸은 어느 결정에서든 `None` 이고, **그 `None` 은 "재검증을 하지 않았다"** 이지
  실패가 아니다. 되먹임 · `approved_commitments` 때도 칸을 먼저 열고 채우는 쪽을
  뒤에 붙였다 — 같은 순서다.

★ **DB 를 치지 않는다.** 어휘는 타입에서, 적재는 `execute_returning_one` 을 가로채
  읽고, DDL 은 SQL 텍스트로 본다. `tests/master/conftest.py` 의 격리와 같은 결이다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest

import app.master
from app.master import decision_repository
from app.master.decision import Decision, DecisionOut, RevalidationOutcome

_DDL = (
    Path(app.master.__file__).parent.parent.parent.parent
    / "database"
    / "master"
    / "master_decision_revalidation.sql"
)

#: 판매에 통보하고 판매가 그대로 받은 네 값 (2026-09-04).
_OUTCOMES = frozenset({"PASSED", "CONDITIONAL", "FAILED", "ERROR"})


# ── ① 어휘 ─────────────────────────────────────────────────────────────────


def test_재검증_결과_어휘는_넷이다():
    """★ 코드가 어휘의 주인이다 — DDL 의 CHECK 는 같은 넷을 되풀이한다.

    🔴 **`CONDITIONAL` 이 빠지는 것이 이 검사의 표적이다.** 빠지면 *"통과했으나 새
      조건이 붙었다"* 를 적을 값이 없어져 `PASSED` 로 접히고, 그 순간 **사용자가 본
      적 없는 조건이 사용자 승인으로 기록된다.**
    """
    assert set(get_args(RevalidationOutcome)) == _OUTCOMES


def test_CONDITIONAL_은_PASSED_와_다른_값이다():
    """⚠️ 위 검사만으로는 부족하다 — 넷이 다 있어도 뜻이 같으면 소용이 없다.

    `CONDITIONAL` 은 **통과가 아니다.** 사용자가 승인한 대상은 그때 화면에 있던 그
    안이고, 새 조건이 붙으면 그것은 다른 안이다. 둘이 **따로 선다**는 것을 잠근다.
    """
    values = get_args(RevalidationOutcome)
    assert "CONDITIONAL" in values
    assert "PASSED" in values
    assert values.index("CONDITIONAL") != values.index("PASSED")


def test_결정_어휘에_재검증_결과가_섞이지_않는다():
    """🔴 **이 파일에서 가장 중요한 검사다.**

    `decision` 은 **사용자가 무엇을 눌렀나**이고 `revalidation_outcome` 은 **그 뒤
    재검증이 어떻게 됐나**다. `decision` 에 `REVALIDATION_FAILED` 를 더하면

    ```text
    사용자가 APPROVE 를 눌렀는데 재검증에서 막혔다   ← 이것이
    사용자가 승인하지 않았다                          ← 이것으로 뭉개진다
    ```

    앞엣것이 이력에서 사라진다. 게다가 번복 규칙(`next_seq` · `mark_current`)이
    전부 `decision` 값으로 판단하므로, 사람이 안 누른 값이 *"현재 유효한 결정"* 이
    될 수 있다.
    """
    decisions = set(get_args(Decision))

    assert decisions == {"APPROVE", "REJECT_ALL", "REQUEST_CHANGE", "CANCEL"}, (
        f"결정 어휘가 늘었다: {sorted(decisions)} — 재검증 결과는 decision 이 아니라 "
        f"revalidation_outcome 에 적는다"
    )
    assert not decisions & _OUTCOMES, "재검증 결과 값이 결정 어휘에 섞여 들어왔다"


# ── ② 응답 스키마 ──────────────────────────────────────────────────────────


def _decision_out(**overrides: Any) -> DecisionOut:
    base: dict[str, Any] = {
        "decision_id": uuid4(),
        "request_id": "REQ-20260907-0001",
        "decision_seq": 1,
        "decision": "APPROVE",
        "scenario_label": "기본",
        "decided_by": "이현서",
        "end_code_at_decision": "E1_APPROVED",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return DecisionOut(**base)


def test_두_칸이_응답에_실린다():
    """`DecisionOut` 이 `/decision` · `/decisions` · `/ask` 응답의 정본이다."""
    row = _decision_out(
        revalidation_request_id="REQ-20260907-0002",
        revalidation_outcome="CONDITIONAL",
    )

    assert row.revalidation_request_id == "REQ-20260907-0002"
    assert row.revalidation_outcome == "CONDITIONAL"
    assert row.model_dump()["revalidation_outcome"] == "CONDITIONAL"


def test_안_주면_None_이고_그것은_실패가_아니다():
    """★ **비어 있는 칸이 "안 왔다" 로 보이는 것은 지금은 맞다** — 배선이 M-4 다.

    `None` 이 기본이라 기존 호출자(그리고 M-4 전의 모든 승인)가 그대로 돈다.
    """
    row = _decision_out()

    assert row.revalidation_request_id is None
    assert row.revalidation_outcome is None


def test_어휘_밖의_결과는_스키마가_막는다():
    """DDL 의 CHECK 와 같은 규칙을 입구에서도 건다 — 뒤에서 터지면 500 이 된다."""
    with pytest.raises(ValueError):
        _decision_out(revalidation_request_id="REQ-20260907-0002", revalidation_outcome="PASS")


# ── ③ 적재 ─────────────────────────────────────────────────────────────────

#: `INSERT INTO ... ( <여기> ) VALUES ( <%s ...> )` 를 원문에서 뽑는다.
_INSERT = re.compile(
    r"INSERT INTO \{\}\.\{\} \((?P<cols>[^)]*)\)\s*VALUES \((?P<vals>[^)]*)\)",
    re.DOTALL,
)


def _insert_columns() -> list[str]:
    """`save_decision` 이 실제로 적는 컬럼 순서.

    ★ `_COLUMNS`(SELECT 용)를 보지 않는다. **둘은 갈릴 수 있다** — SELECT 에만 넣고
      INSERT 에서 빠뜨리면 조회는 컬럼을 돌려주는데 값이 늘 NULL 이다. 이 검사가
      막으려는 것이 정확히 그것이라, INSERT 문 자체를 읽는다.
    """
    import inspect

    source = inspect.getsource(decision_repository.save_decision)
    found = _INSERT.search(source)
    assert found is not None, "save_decision 에서 INSERT 문을 못 찾았다"

    columns = [c.strip() for c in found.group("cols").split(",") if c.strip()]
    placeholders = [v.strip() for v in found.group("vals").split(",") if v.strip()]
    assert len(columns) == len(placeholders), (
        f"컬럼 {len(columns)}개에 자리표시자 {len(placeholders)}개다 — 어긋나면 "
        f"값이 옆 칸으로 밀린다"
    )
    return columns


@pytest.fixture
def 적재_인자(monkeypatch: pytest.MonkeyPatch):
    """`execute_returning_one` 을 가로채 **무엇을 어느 칸에 넣으려 했는지** 준다.

    🔴 **DB 를 세우지 않는다.** 이 검사가 보는 것은 "무엇을 넣으려 했는가" 이지 DB
      동작이 아니다 — `test_master_run_repository.py` 가 쓰는 방식과 같다.
    """
    columns = _insert_columns()
    잡힌_값: dict[str, Any] = {}

    def _capture(query: Any, params: tuple[Any, ...]) -> dict[str, Any]:
        assert len(params) == len(columns), (
            f"INSERT 컬럼 {len(columns)}개에 값 {len(params)}개를 넘긴다"
        )
        잡힌_값.update(dict(zip(columns, params, strict=True)))
        # RETURNING 이 돌려줄 행. 넣은 값을 그대로 돌려준다 — `_row_to_out` 이 두 칸을
        # 읽어 내는지까지 같이 본다.
        return {**잡힌_값, "run_id": UUID(잡힌_값["run_id"]), "created_at": datetime.now(UTC)}

    monkeypatch.setattr(decision_repository, "execute_returning_one", _capture)
    monkeypatch.setattr(decision_repository, "get_db_schema", lambda: "haetdeul")
    return 잡힌_값


RUN_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _save(**overrides: Any) -> DecisionOut:
    payload: dict[str, Any] = {
        "request_id": "REQ-20260907-0001",
        "decision_seq": 1,
        "decision": "APPROVE",
        "decided_by": "이현서",
        "end_code_at_decision": "E1_APPROVED",
        "scenario_label": "기본",
        "history_run_id": RUN_UUID,
    }
    payload.update(overrides)
    return decision_repository.save_decision(**payload)


def test_두_칸이_INSERT_에_실린다(적재_인자):
    """🔴 **칸을 만들어 두고 INSERT 에서 빠뜨리면 늘 NULL 이다.**

    아무 에러도 안 나고 응답도 멀쩡하다. *"이 승인이 재검증을 거쳤나"* 를 못 보게
    되는 것만 조용히 남는다 — `master_agent_runs` 의 `item`·`end_code` 가 같은
    함정이었다 (2026-09-02).
    """
    saved = _save(
        revalidation_request_id="REQ-20260907-0002",
        revalidation_outcome="PASSED",
    )

    assert 적재_인자["revalidation_request_id"] == "REQ-20260907-0002"
    assert 적재_인자["revalidation_outcome"] == "PASSED"
    # `_row_to_out` 이 되읽는 것까지 본다 — 넣기만 하고 못 읽으면 반쪽이다.
    assert saved.revalidation_request_id == "REQ-20260907-0002"
    assert saved.revalidation_outcome == "PASSED"


def test_재검증_키가_follow_up_칸으로_새지_않는다(적재_인자):
    """🔴 **두 칸은 다른 사건이다.**

    ```text
    follow_up_request_id     REQUEST_CHANGE 가 낳은 재실행  — 결정 **뒤**, 사람이 돌린 것
    revalidation_request_id  승인 직전 재검증이 낳은 재실행  — 결정 **전**, 서버가 돌린 것
    ```

    재검증은 **새 `as_of` · 새 `request_id`** 로 도는데 승인은 원 실행에 달려 있다.
    한 칸에 담으면 그 값이 어느 쪽인지 DB 도 코드도 모른다.

    ★ 서로 다른 값을 넣어 **바뀌거나 겹치는 것**을 잡는다. 같은 값을 넣으면
      뒤바뀌어도 검사가 통과한다.
    """
    saved = _save(
        follow_up_request_id="REQ-FOLLOWUP-0001",
        revalidation_request_id="REQ-REVALIDATION-0002",
        revalidation_outcome="FAILED",
    )

    assert 적재_인자["follow_up_request_id"] == "REQ-FOLLOWUP-0001"
    assert 적재_인자["revalidation_request_id"] == "REQ-REVALIDATION-0002"
    assert saved.follow_up_request_id == "REQ-FOLLOWUP-0001"
    assert saved.revalidation_request_id == "REQ-REVALIDATION-0002"


def test_안_주면_두_칸이_NULL_로_간다(적재_인자):
    """★ 기본이 `None` 이라 M-4 전의 호출자가 그대로 돈다.

    **없는 재검증을 'PASSED' 로 지어 넣지 않는다** — 그러면 추측이 사실로 둔갑한다.
    """
    _save()

    assert 적재_인자["revalidation_request_id"] is None
    assert 적재_인자["revalidation_outcome"] is None


def test_조회_컬럼에도_두_칸이_있다():
    """SELECT 에 없으면 적재는 되는데 **되읽을 수가 없다.**"""
    assert "revalidation_request_id" in decision_repository._COLUMNS
    assert "revalidation_outcome" in decision_repository._COLUMNS


# ── ④ DDL ──────────────────────────────────────────────────────────────────


def _ddl_text() -> str:
    return _DDL.read_text(encoding="utf-8")


def _ddl_statements() -> str:
    """주석을 걷어 낸 **실행되는 문장만.**

    🔴 **이 구분이 없으면 오탐·누락이 둘 다 난다.** 이 파일의 헤더는 *"왜
      `follow_up_request_id` 를 재사용하지 않는가"* 를 설명하느라 그 이름을 적고
      있고, 되돌리기 절차는 `DROP COLUMN` 을 주석으로 적어 두고 있다. 문서를 코드로
      세면 둘 다 잘못 읽힌다 — `test_master_run_repository._value_strings` 가 같은
      함정을 두 번 밟고 세운 규칙이다.
    """
    lines = [line for line in _ddl_text().splitlines() if not line.lstrip().startswith("--")]
    return "\n".join(lines)


def test_본_DDL_과_ALTER_판이_같은_칸을_든다():
    """🔴 **같은 변경이 두 곳에 있다. 둘 다 고친다** (`database/README.md` §2).

    본 DDL 은 신규 구축용(`CREATE TABLE`)이고 ALTER 판은 이관용이다. 어느 하나만
    고치면 **새로 세운 DB 와 옮긴 DB 의 스키마가 갈리는데, 어느 쪽도 에러를 안 내고
    조용히 갈린다.** `run_id` 때도 둘 다 고쳤다 (본 DDL 개정 이력 2026-08-30).

    ⚠️ 이 검사는 칸 이름만 본다. CHECK 문구까지 대조하지는 않는다 —
      `test_schema_files_agree.py` 가 뷰 본문에 대해 하는 일의 표 판이 아직 없다.
    """
    assert _DDL.exists(), f"{_DDL.name} 이 없다"

    본_DDL = (_DDL.parent.parent / "master_decisions.sql").read_text(encoding="utf-8")
    for 칸 in ("revalidation_request_id", "revalidation_outcome"):
        assert 칸 in 본_DDL, f"본 DDL 에 {칸} 이 없다 — 새로 세운 DB 에는 그 칸이 안 생긴다"


def test_두_칸을_NULL_허용으로_연다():
    """🔴 **기존 30행을 채우지 않는다.**

    그때는 재검증이 없었다. 없던 검사를 'PASSED' 로 채우면 추측이 사실로 둔갑하고,
    나중에 *"재검증을 통과한 승인"* 을 세면 아무도 검증하지 않은 26건이 딸려 나온다.
    """
    statements = _ddl_statements()

    for column in ("revalidation_request_id", "revalidation_outcome"):
        pattern = rf"ADD COLUMN IF NOT EXISTS {column} TEXT NULL"
        assert re.search(pattern, statements), f"{column} 을 NULL 허용으로 여는 문장이 없다"

    # ⚠️ `NOT NULL` 문자열만 세면 안 된다 — 짝 CHECK 의 `IS NOT NULL` 이 걸린다.
    #   칸을 여는 문장과 뒤에서 조이는 문장만 본다.
    assert not re.search(r"ADD COLUMN[^\n;]*NOT NULL", statements), (
        "재검증 칸을 NOT NULL 로 연다 — 기존 30행이 전부 막힌다"
    )
    assert not re.search(r"ALTER COLUMN[^\n;]*SET NOT NULL", statements), (
        "뒤에서 NOT NULL 로 조인다 — 같은 문제다"
    )


def test_백필이_없다():
    """★ 없는 사건에 결과를 적는 문장은 추측이 아니라 **창작**이다."""
    statements = _ddl_statements()

    assert not re.search(r"\bUPDATE\b", statements, re.IGNORECASE), (
        "실행되는 UPDATE 가 있다 — 기존 행을 채우면 안 한 재검증이 한 것으로 남는다"
    )


def test_결과_어휘를_CHECK_로_닫는다():
    """`decision` 이 CHECK 로 어휘를 닫아 둔 것과 같은 결이다.

    🔴 **`CONDITIONAL` 이 빠지는 것이 표적이다** — 빠지면 DB 가 그 값을 거부하고,
      M-4 는 *"통과했으나 조건이 붙었다"* 를 `PASSED` 나 `FAILED` 로 접어야 한다.
    """
    statements = _ddl_statements()
    found = re.search(r"CHECK \(revalidation_outcome IN \((?P<values>[^)]*)\)\)", statements)
    assert found is not None, "revalidation_outcome 에 CHECK 가 없다"

    values = {v.strip().strip("'") for v in found.group("values").split(",")}
    assert values == _OUTCOMES, f"DDL 어휘가 코드와 갈렸다: {sorted(values)}"


def test_코드와_DDL_이_같은_어휘를_쓴다():
    """🔴 **어휘가 두 곳에 있다. 갈리면 조용히 갈린다.**

    코드가 `CONDITIONAL` 을 보내는데 DDL 이 안 받으면 승인 적재가 통째로 터지고,
    반대면 DB 에는 있는 값을 코드가 못 읽는다. 둘을 대조한다.
    """
    found = re.search(r"CHECK \(revalidation_outcome IN \((?P<values>[^)]*)\)\)", _ddl_statements())
    assert found is not None
    ddl_values = {v.strip().strip("'") for v in found.group("values").split(",")}

    assert ddl_values == set(get_args(RevalidationOutcome))


def test_결과만_있고_실행이_없는_행을_막는다():
    """🔴 *"재검증했는데 어느 실행인지 모른다"* 가 되는 자리다.

    `run_id` 가 없던 시절 승인이 겪은 병과 같다. 이 표는 이미
    `decision`↔`scenario_label` 관계를 CHECK 로 잠그고 있으므로 같은 근거가 선다.

    ⚠️ **`ERROR` 만 예외다** — *"재검증 자체를 못 돌렸다"* 라 가리킬 실행이 아예
      없을 수 있다. 예외가 없으면 M-4 는 가짜 업무 키를 지어 넣어야 한다.
    """
    statements = _ddl_statements()

    assert "master_decisions_revalidation_pairing" in statements, (
        "두 칸의 관계를 잠그는 CHECK 가 없다"
    )
    assert re.search(
        r"revalidation_request_id IS\s+NULL AND revalidation_outcome = 'ERROR'", statements
    ), "ERROR 예외가 없다 — 못 돌린 재검증에 가짜 실행 키를 지어 넣게 된다"


def test_두_번_돌려도_안전하다():
    """운영 DB 에 거는 판이라 재실행이 *"이미 있다"* 로 죽으면 안 된다."""
    statements = _ddl_statements()

    assert statements.count("ADD COLUMN IF NOT EXISTS") == 2
    assert statements.count("CREATE INDEX IF NOT EXISTS") == 1
    # 제약 둘은 `IF NOT EXISTS` 가 없으므로 `pg_constraint` 조회로 감싼다.
    assert statements.count("FROM pg_constraint") == 2


def test_follow_up_칸을_건드리지_않는다():
    """★ `follow_up_request_id` 는 `RERUN_WITH_CONDITION` 의미 그대로 둔다.

    ⚠️ 주석은 뺀 뒤에 본다 — 이 파일의 헤더가 *"왜 재사용하지 않는가"* 를 설명하며
      그 이름을 적고 있다.
    """
    assert "follow_up_request_id" not in _ddl_statements(), (
        "실행되는 문장이 follow_up_request_id 를 건드린다 — 두 의미가 섞인다"
    )
