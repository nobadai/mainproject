"""같은 뷰 정의가 두 파일에 있다. **갈리면 여기가 운다.**

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
