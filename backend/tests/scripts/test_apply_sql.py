"""`.sql` 파일 하나를 있는 그대로 적용하는 실행기 (2026-09-11).

🔴 **`psql` 이 이 환경에 없다.** 그렇다고 DDL 을 손으로 옮겨 적으면 **파일과 실제로
   돈 것이 갈리고**, 그때 어느 쪽이 맞는지 답할 방법이 없다.

★★ **왜 이 검사가 필요한가.** 오늘(`2026-09-11`) 걷기가 판매 검증에서 멈췄고 원인이
  **저장소에 있는 마이그레이션이 DB 에 안 들어간 것**이었다. `#353` 때 *"DB 적용 →
  코드 머지"* 로 정한 순서를 어긴 결과이고, 그때는 승인 32건이 죽었다.

🔴 **이 실행기의 안전 속성은 하나다 — `--apply` 가 없으면 DB 에 안 붙는다.**
   그 속성이 깨지면 *"무엇이 도는지 보려고" 부른 명령이 실제로 돈다.*
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import apply_sql


@pytest.fixture
def 붙으면_터지는_psycopg(monkeypatch):
    """🔴 DB 에 붙는 순간 터뜨린다. *"안 붙었다"* 를 값으로 잰다."""

    def 절대_안_됨(*_args, **_kwargs):
        raise AssertionError("DB 에 붙었다 — --apply 없이 붙으면 안 된다")

    monkeypatch.setattr(apply_sql.psycopg, "connect", 절대_안_됨)


def _sql파일(tmp_path: Path) -> Path:
    경로 = tmp_path / "probe.sql"
    경로.write_text("SELECT 1;\n", encoding="utf-8")
    return 경로


def test_apply_없이는_DB_에_안_붙는다(tmp_path, monkeypatch, capsys, 붙으면_터지는_psycopg) -> None:
    """🔴 **이 실행기의 전부다.**"""
    monkeypatch.setattr(sys, "argv", ["apply_sql.py", str(_sql파일(tmp_path))])

    assert apply_sql.main() == 0

    나온것 = unicodedata.normalize("NFC", capsys.readouterr().out)
    assert "SELECT 1;" in 나온것, "무엇이 돌지 안 보여 준다"
    assert "안 돌렸다" in 나온것, "안 돌렸다는 사실을 안 말한다"


def test_dry_run_도_같다(tmp_path, monkeypatch, 붙으면_터지는_psycopg) -> None:
    """⚠️ `--dry-run` 은 **기본과 같은 것**이지 다른 길이 아니다."""
    monkeypatch.setattr(sys, "argv", ["apply_sql.py", str(_sql파일(tmp_path)), "--dry-run"])

    assert apply_sql.main() == 0


def test_없는_파일이면_막는다(tmp_path, monkeypatch, 붙으면_터지는_psycopg) -> None:
    """★ 없는 파일을 빈 문자열로 읽어 **아무것도 안 하고 성공**하면 안 된다."""
    monkeypatch.setattr(sys, "argv", ["apply_sql.py", str(tmp_path / "없다.sql")])

    with pytest.raises(SystemExit) as 터진것:
        apply_sql.main()

    assert "파일이 없다" in unicodedata.normalize("NFC", str(터진것.value))


def test_파일을_한_글자도_안_바꾼다(tmp_path, monkeypatch, capsys, 붙으면_터지는_psycopg) -> None:
    """🔴 **SQL 을 만들지 않는다.** 읽어서 그대로 보낸다.

    ★★ 손으로 옮겨 적거나 문장을 쪼개면 파일과 실제로 돈 것이 갈린다 — 그것이
      이 실행기를 만든 이유 자체를 없앤다.
    """
    본문 = "-- 주석\nBEGIN;\n  ALTER TABLE x ADD CONSTRAINT y CHECK (z IN ('a', 'b'));\nCOMMIT;\n"
    경로 = tmp_path / "ddl.sql"
    경로.write_text(본문, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["apply_sql.py", str(경로)])

    apply_sql.main()

    나온것 = unicodedata.normalize("NFC", capsys.readouterr().out)
    assert 본문.strip() in 나온것, "보여 준 것이 파일과 다르다"
