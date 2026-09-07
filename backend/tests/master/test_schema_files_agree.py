"""같은 정의가 두 파일에 있다. **갈리면 여기가 운다.**

뷰 본문(`_VIEW_PAIRS`) · 표의 어휘 CHECK(`_CHECK_PAIRS`) · 제약 **이름** 셋을 본다.

`database/README.md` §2 가 규약을 적어 뒀다.

> **같은 변경이 두 곳에 있습니다** — 본 DDL(신규 구축용)과 ALTER 판(이관용).
> 어느 하나만 고치면 갈립니다. **둘 다 고칩니다.**

🔴 **적어 두는 것만으로는 안 지켜진다.** `04` 문서 §6.1 의 *"선언은 있는데 강제가
없다"* 가 이 저장소의 반복 패턴이고, 여기가 그 자리다 — 새 DB 는 본 DDL 로 서고
운영 DB 는 ALTER 판으로 가는데, 둘이 갈리면 **어느 쪽도 에러를 안 낸다.**
서로 다른 스키마가 조용히 생긴다.

⚠️ **이 파일은 SQL 텍스트만 본다.** 살아 있는 DB 를 안 읽는다 — DB 없이도 돌아야
`-m db` 없이 기본 스위트에 들어간다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.master

_REPO = Path(app.master.__file__).parent.parent.parent.parent
_DB = _REPO / "database"

#: (뷰 이름, 본 DDL 파일, 이관 판 파일)
_VIEW_PAIRS = [
    ("v_ml_price_forecast", "10_domain_schema.sql", "ml_forecast_view_gate_reason.sql"),
]


def _view_body(path: Path, view: str) -> str:
    """`CREATE [OR REPLACE] VIEW <이름> AS ... ;` 의 본문. 공백은 뭉갠다."""
    text = path.read_text(encoding="utf-8")
    pattern = rf"CREATE (?:OR REPLACE )?VIEW haetdeul\.{re.escape(view)} AS(.*?);\s*$"
    found = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
    assert len(found) == 1, f"{path.name} 에 {view} 정의가 {len(found)} 곳이다"
    return re.sub(r"\s+", " ", found[0]).strip()


@pytest.mark.parametrize(("view", "base", "migration"), _VIEW_PAIRS)
def test_본_DDL_과_이관판이_같은_뷰를_만든다(view: str, base: str, migration: str):
    body_base = _view_body(_DB / base, view)
    body_migration = _view_body(_DB / migration, view)

    assert body_base == body_migration, (
        f"{base} 와 {migration} 의 {view} 정의가 갈렸다. "
        f"새 DB 와 운영 DB 가 서로 다른 스키마로 서게 된다 — 둘 다 고쳐라"
    )


@pytest.mark.parametrize(("view", "base", "migration"), _VIEW_PAIRS)
def test_대조가_공허하지_않다(view: str, base: str, migration: str):
    """⚠️ **먼저 뽑아내는지부터 단언한다.**

    정규식이 빈 문자열을 둘 내면 위 검사가 `"" == ""` 로 통과한다.
    두 본문이 실제로 내용을 갖는지 봐야 위 검사를 믿을 수 있다.
    """
    for name in (base, migration):
        body = _view_body(_DB / name, view)
        assert len(body) > 200, f"{name} 에서 뽑은 본문이 너무 짧다 ({len(body)}자)"
        assert "jsonb_build_object" in body, f"{name} 본문에 daily 조립이 없다"


def test_gate_reason_이_daily_에_실린다():
    """🔴 이 판의 주장이다 (2026-09-03 · 매입 `#212`).

    표에는 있는데 뷰가 `daily` 에 안 넣고 있었다. 받는 쪽은 *"이 행을 쓰지 말라"*
    (`is_gated`)는 알아도 **왜인지**를 못 봤다.

    ```text
    실측    is_gated 이면서 gate_reason 이 있는 행    326
            is_gated 인데 gate_reason 이 NULL 인 행     0

    AUC     사유가 lead_time 하나 - 지금은 추론이 된다
    WHSL    lead_time · quality · lead_time+quality - 추론이 안 된다
    ```
    """
    for _, base, migration in _VIEW_PAIRS:
        for name in (base, migration):
            body = _view_body(_DB / name, "v_ml_price_forecast")
            assert "'gate_reason', gate_reason" in body, (
                f"{name} 의 daily 에 gate_reason 이 없다 — is_gated 만 가면 "
                f"받는 쪽이 왜 게이팅됐는지 못 본다"
            )


def test_이관판이_CREATE_OR_REPLACE_다():
    """운영 DB 에 거는 것이라 `CREATE VIEW` 면 *"이미 있다"* 로 죽는다."""
    text = (_DB / "ml_forecast_view_gate_reason.sql").read_text(encoding="utf-8")

    assert "CREATE OR REPLACE VIEW" in text
    assert not re.search(r"CREATE VIEW\b", text), (
        "이관 판이 CREATE VIEW 다 — 운영 DB 에서 'already exists' 로 죽는다"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 표의 어휘 CHECK — 뷰와 같은 병이 표에도 있다
# ══════════════════════════════════════════════════════════════════════════════
#
# 🔴 뷰만 보던 사이에 표가 갈렸다. `master_decisions_decision_check` 가 본 DDL 에서는
#   3종인데 이관 판(`master/master_decision_cancel.sql`)은 CANCEL 을 더해 4종이었다.
#   운영 DB 는 CANCEL 을 받고 **새 DB 는 CANCEL 결정 저장이 전부 CHECK 로 막힌다.**
#   코드의 `Decision` 은 4종이다 (`app/master/decision.py`).

#: (제약 이름, 본 DDL 파일, 이관 판 파일)
#:
#: ⚠️ **어휘를 닫는 CHECK 만 담는다** — 이름이 `_check` 로 끝나는 것들이다.
#:   `_scenario_required` 같은 **관계** CHECK 는 여기 넣지 않는다. 아래 대조가
#:   문자열 리터럴 집합으로 재기 때문에 관계 CHECK 에는 뜻이 없다.
_CHECK_PAIRS = [
    (
        "master_decisions_decision_check",
        "master_decisions.sql",
        "master/master_decision_cancel.sql",
    ),
    (
        "master_decisions_revalidation_outcome_check",
        "master_decisions.sql",
        "master/master_decision_revalidation.sql",
    ),
]

#: 이관 판이 어휘 CHECK 를 거는 모양. ③ 의 훑기가 이것으로 잰다.
_ADDS_VOCAB_CHECK = re.compile(r"ADD\s+CONSTRAINT\s+(master_decisions_\w*_check)\s+CHECK\s*\(")


def _ddl_statements(text: str) -> str:
    """`--` 주석을 걷어낸 **실행되는 SQL** 만 남긴다.

    ⚠️ 원문을 그대로 뒤지면 **설명하는 주석**이 선언으로 잡힌다. 이 저장소의 이관
    판들은 되돌리기 절차와 검증 SQL 예시를 주석으로 달아 두는 관례라, 거기 적힌
    `ADD CONSTRAINT` 를 세면 **실행된 적 없는 제약이 있는 것으로 읽힌다.**
    `master_agent_runs_migration.sql` 의 되돌리기 블록이 그 자리다.
    """
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _master_decisions_sql() -> list[tuple[str, str]]:
    """`haetdeul.master_decisions` 를 건드리는 SQL 파일. (파일 이름, 실행문) 쌍이다.

    ⚠️ **범위를 좁힌 근거는 아래 훑기 검사의 docstring 에 있다.** 다른 표의 이관
    판은 그 파트 소유이고 그쪽 대조 표는 그쪽에 있어야 한다. 같은 목록을 두 벌
    만들면 한쪽만 넓어져서 **두 검사가 서로 다른 범위를 재게 된다.**
    """
    files = []
    for sql in sorted(_DB.rglob("*.sql")):
        text = _ddl_statements(sql.read_text(encoding="utf-8"))
        if "haetdeul.master_decisions" not in text:
            continue
        files.append((sql.relative_to(_DB).as_posix(), text))
    return files


def _check_body(path: Path, constraint: str) -> str:
    """`CONSTRAINT <이름> CHECK ( ... )` 의 괄호 안. 공백은 뭉갠다.

    🔴 **마지막 선언이 그 표의 최종 CHECK 다.** 이관 판은 `DROP CONSTRAINT` 뒤
    `ADD CONSTRAINT` 로 오고, 어휘를 여러 번 넓힌 이력이 있으면 앞선 선언은 이미
    지워진 것이다. 첫 번째를 잡으면 **낡은 어휘를 최종이라고 읽는다.**

    ⚠️ 괄호는 세면서 닫는다. `= ANY (ARRAY[...])` 는 괄호가 중첩돼 있어서
    `\\)` 로 끊으면 본문이 잘린다.
    """
    text = path.read_text(encoding="utf-8")
    opens = [
        m.end() for m in re.finditer(rf"CONSTRAINT\s+{re.escape(constraint)}\s+CHECK\s*\(", text)
    ]
    assert opens, f"{path.name} 에 {constraint} 의 CHECK 선언이 없다"

    start = opens[-1]
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    assert depth == 0, f"{path.name} 의 {constraint} 괄호가 안 닫힌다"

    return re.sub(r"\s+", " ", text[start : i - 1]).strip()


def _check_vocabulary(path: Path, constraint: str) -> set[str]:
    """CHECK 가 여는 **어휘 집합**.

    🔴 **문자열 그대로 비교하면 안 된다.** 두 파일이 같은 뜻을 다른 문법으로 적는다 —
    본 DDL 은 `decision IN ('A','B')`, 이관 판은 `decision = ANY (ARRAY['A','B'])`.
    문법이 아니라 **무엇이 허용되는가** 가 갈렸는지를 봐야 한다.
    """
    return set(re.findall(r"'([^']*)'", _check_body(path, constraint)))


@pytest.mark.parametrize(("constraint", "base", "migration"), _CHECK_PAIRS)
def test_본_DDL_과_이관판이_같은_어휘를_연다(constraint: str, base: str, migration: str):
    vocab_base = _check_vocabulary(_DB / base, constraint)
    vocab_migration = _check_vocabulary(_DB / migration, constraint)

    assert vocab_base == vocab_migration, (
        f"{base} 와 {migration} 의 {constraint} 어휘가 갈렸다. "
        f"본 DDL 만 {sorted(vocab_base - vocab_migration)} · "
        f"이관 판만 {sorted(vocab_migration - vocab_base)} — "
        f"새 DB 는 이관 판이 여는 값을 CHECK 로 막는다. 둘 다 고쳐라"
    )


@pytest.mark.parametrize(("constraint", "base", "migration"), _CHECK_PAIRS)
def test_어휘_대조가_공허하지_않다(constraint: str, base: str, migration: str):
    """⚠️ **먼저 뽑아내는지부터 단언한다.**

    정규식이 리터럴을 하나도 못 뽑으면 위 검사가 `set() == set()` 로 통과한다.
    뷰 쪽 `test_대조가_공허하지_않다` 와 같은 함정이고 같은 결로 막는다.
    """
    for name in (base, migration):
        body = _check_body(_DB / name, constraint)
        vocab = _check_vocabulary(_DB / name, constraint)

        assert len(body) > 20, f"{name} 에서 뽑은 {constraint} 본문이 너무 짧다 ({len(body)}자)"
        assert len(vocab) >= 2, (
            f"{name} 의 {constraint} 에서 뽑은 어휘가 {sorted(vocab)} 뿐이다 — "
            f"어휘를 닫는 CHECK 인데 값이 안 뽑혔으면 위 대조는 공허하다"
        )


def test_어휘_CHECK_를_건드리는_이관판이_대조표에_다_있다():
    """🔴 **쌍을 손으로 적는 표는 낡는다.** 낡으면 여기가 운다.

    이번에 갈린 것이 그래서다 — `master_decision_cancel.sql` 이 생겼는데 대조 표에
    아무도 안 넣었고, 검사는 조용히 초록이었다. 표가 훑기 결과와 다르면 빨개진다.
    양방향으로 잰다.

    · 훑기에만 있다  → 새 이관 판이 표에 안 들어왔다 (이번에 일어난 일)
    · 표에만 있다    → 정규식이 낡았거나 파일이 옮겨졌다 (공허한 초록 방지)

    ⚠️ **범위를 좁혔다.**

    1. `haetdeul.master_decisions` 를 건드리는 파일만 본다. 다른 표의 이관 판은
       그 파트 소유이고, 그쪽 대조 표는 그쪽에 있어야 한다.
    2. 이름이 `_check` 로 끝나는 **어휘** CHECK 만 본다. `_scenario_required` 같은
       관계 CHECK 는 어휘 집합으로 대조가 안 되므로 표에 담지 않고, 그래서 훑기에서도
       뺀다. 담을 수 없는 것을 재면 못 고칠 빨강이 남는다.
    """
    declared = {(constraint, migration) for constraint, _, migration in _CHECK_PAIRS}

    found = set()
    for name, text in _master_decisions_sql():
        for constraint in _ADDS_VOCAB_CHECK.findall(text):
            found.add((constraint, name))

    assert found == declared, (
        f"대조 표와 database/ 의 실제 이관 판이 다르다. "
        f"표에 없는 것 {sorted(found - declared)} · "
        f"훑기에 없는 것 {sorted(declared - found)} — "
        f"표에 없는 이관 판은 갈려도 아무도 안 본다"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 제약 **이름** — 어휘가 같아도 이름이 갈리면 제약이 둘 걸린다
# ══════════════════════════════════════════════════════════════════════════════
#
# 🔴 위 어휘 대조를 통과하면서 실제로 지나간 드리프트가 있다 (2026-09-07).
#
#     본 DDL      master_decisions_revalidation_pair
#     이관 판     master_decisions_revalidation_pairing
#
#   조건식은 논리적으로 같았다. **이름만 달랐다.** 어휘 대조는 문자열 리터럴 집합을
#   재는데 이 제약에는 리터럴이 'ERROR' 하나뿐이라 대조 표에도 못 들어간다 —
#   어느 검사에도 안 걸렸다.
#
#   이름이 갈리면 조용히 끝나지 않는다. 이관 판의 `IF NOT EXISTS` 는 **이름으로**
#   판정한다. 새 DB 를 본 DDL 로 세우고 그 위에 이관 판을 얹으면, 이관 판은 자기
#   이름이 없는 것을 보고 **같은 뜻의 제약을 하나 더 건다.** 표에 중복 CHECK 가
#   남고 위반 시 어느 이름이 뜰지도 갈린다.

#: 이관 판이 `master_decisions` 에 제약을 거는 모양.
#:
#: ⚠️ **`ADD` 만 본다.** `DROP CONSTRAINT IF EXISTS` 는 *"없앤다"* 이고 표가
#:   최종적으로 갖는 것은 `ADD` 가 말한다. 어휘를 넓히는 이관 판은 늘 DROP → ADD
#:   쌍으로 오므로, DROP 을 같이 세면 지워진 이름을 있는 것으로 읽는다.
#:
#: ⚠️ **표 이름까지 붙여서 본다.** `master_decisions_run_id.sql` 은 같은 파일에서
#:   `orchestrator_agent_runs` 에도 제약(`..._run_request_unique`)을 건다. 그것은
#:   이 표의 것이 아니라 본 DDL 에 없는 것이 맞다 — 표를 안 보면 바로 오탐이다.
_ADDS_CONSTRAINT = re.compile(
    r"ALTER\s+TABLE\s+(?:haetdeul\.)?master_decisions\s+ADD\s+CONSTRAINT\s+(\w+)"
)

#: 본 DDL 이 `CREATE TABLE` 안에서 제약을 선언하는 모양. 줄 머리의 `CONSTRAINT` 다.
#: `ADD CONSTRAINT` · `DROP CONSTRAINT` 는 줄 머리가 `CONSTRAINT` 가 아니라 안 걸린다.
_DECLARES_CONSTRAINT = re.compile(r"^\s*CONSTRAINT\s+(\w+)", re.MULTILINE)


def _base_constraint_names() -> set[str]:
    """본 DDL(`master_decisions.sql`) 이 선언하는 제약 이름."""
    text = _ddl_statements((_DB / "master_decisions.sql").read_text(encoding="utf-8"))
    return set(_DECLARES_CONSTRAINT.findall(text))


def _migration_constraint_names() -> set[tuple[str, str]]:
    """이관 판들이 `master_decisions` 에 거는 제약 이름. (이름, 파일) 쌍이다."""
    return {
        (constraint, name)
        for name, text in _master_decisions_sql()
        for constraint in _ADDS_CONSTRAINT.findall(text)
    }


def test_이관판이_거는_제약_이름이_본_DDL_에도_있다():
    """🔴 **한 방향으로만 잰다 — 이관 판 → 본 DDL.**

    반대로 걸면 **바로 오탐이 난다.** 본 DDL 에는 이관 판이 건드린 적 없는 제약이
    당연히 더 있다 — `master_decisions_seq_positive` · `master_decisions_unique_seq`
    처럼 2026-08-27 처음 세울 때부터 있던 것들이다. 이관 판은 **바뀐 것만** 적는
    파일이라 그것들을 다시 적을 이유가 없다.

    잡아야 할 것은 한쪽 방향뿐이다. **이관 판이 거는 이름은 본 DDL 이 이미 갖고
    있어야 한다** — 안 그러면 새 DB 에는 그 제약이 없거나(본 DDL 이 안 적었다),
    이름만 다른 같은 뜻의 제약이 둘 걸린다(이번에 일어난 일).
    """
    base = _base_constraint_names()
    missing = sorted((c, f) for c, f in _migration_constraint_names() if c not in base)

    assert not missing, (
        f"이관 판이 거는 제약이 본 DDL 에 그 이름으로 없다: {missing}. "
        f"본 DDL 에 있는 이름 {sorted(base)} — "
        f"이관 판의 IF NOT EXISTS 는 이름으로 판정하므로, 새 DB 를 본 DDL 로 세운 뒤 "
        f"이관 판을 얹으면 같은 뜻의 제약이 둘 걸린다. 이름을 맞춰라"
    )


def test_제약_이름_대조가_공허하지_않다():
    """⚠️ **먼저 뽑아내는지부터 단언한다.**

    양쪽 정규식 중 어느 하나라도 아무것도 못 뽑으면 위 검사가 빈 `missing` 으로
    통과한다. 앞의 두 공허 방지 검사와 같은 함정이고 같은 결로 막는다.
    """
    base = _base_constraint_names()
    found = _migration_constraint_names()

    assert len(base) >= 5, f"본 DDL 에서 뽑은 제약 이름이 {sorted(base)} 뿐이다"
    assert "master_decisions_seq_positive" in base, (
        "본 DDL 뽑기가 인라인 CONSTRAINT 선언을 놓치고 있다"
    )

    assert len(found) >= 2, f"이관 판에서 뽑은 제약 이름이 {sorted(found)} 뿐이다"
    assert "master_decisions_revalidation_pairing" in {c for c, _ in found}, (
        "이관 판 뽑기가 DO $$ 안의 ADD CONSTRAINT 를 놓치고 있다 — 이번 드리프트가 바로 그 자리다"
    )
