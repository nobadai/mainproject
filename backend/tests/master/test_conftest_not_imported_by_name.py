"""검사 모듈이 **`conftest` 를 이름으로 가져가지 않는다.**

🔴 **실측 2026-09-07 — `#352` 가 머지되고 25분 만에 `dev` 가 깨져 있었다.**

```text
tests/master/test_db_isolation.py:14
    from conftest import 개장_정본_이름을_가져간_모듈들, 진짜_개장_정본_함수
```

저장소에 `conftest.py` 가 **넷**이고(`finance` · `logistics` · `master` ·
`test_purchase_agent`) 그 폴더들에 `__init__.py` 가 없어 `conftest` 는 **최상위
이름**이다. 그래서:

```text
pytest tests/master                    🟢 초록 — master 것을 잡는다
pytest tests/master tests/logistics    🔴 ImportError — **수집 자체가 멈춘다**
```

★★ **파트 하나만 돌리면 안 보인다.** 합쳐 돌려야 드러나고, CI 가 파트별로 돌면
  영영 안 보인다 — *"안 터지는 이유를 확인하지 않으면 그 이유가 사라질 때 조용히
  틀린다"* 의 **수집 단계** 판이다.

⚠️ 고칠 자리는 `conftest.py` 가 아니다. 네 폴더에 그 이름이 있는 것은 pytest 규약상
  정상이고 없앨 수 없다. **겹치지 않는 이름의 모듈로 빼는 것**이 고치는 것이고,
  `개장정본_격리.py` 가 그 자리다.

★ 이 검사는 **다음 사람이 같은 편의를 다시 쓰는 것**을 막는다. fixture 아닌 것을
  `conftest` 에서 가져오고 싶은 유혹은 계속 온다.
"""

from __future__ import annotations

import ast
from pathlib import Path

_검사_폴더 = Path(__file__).parent


def _conftest_를_이름으로_가져가는_파일() -> list[str]:
    걸린_것: list[str] = []
    for path in sorted(_검사_폴더.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "conftest":
                걸린_것.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                걸린_것.extend(
                    f"{path.name}:{node.lineno}" for alias in node.names if alias.name == "conftest"
                )
    return 걸린_것


def test_conftest_를_이름으로_import_하지_않는다() -> None:
    """🔴 **합쳐 돌리는 날 수집이 멈춘다.** 파트별로만 돌리면 안 보인다."""
    걸린_것 = _conftest_를_이름으로_가져가는_파일()
    assert not 걸린_것, (
        f"conftest 를 이름으로 가져가는 자리: {걸린_것}. "
        "저장소에 conftest.py 가 넷이라 tests/master 와 tests/logistics 를 같이 돌리면 "
        "다른 것이 잡혀 수집이 멈춘다 — 겹치지 않는 이름의 모듈로 빼십시오"
    )


def test_스캐너가_파일을_실제로_읽는다() -> None:
    """★ **재는 대상이 0건이면 위 검사가 공짜로 초록이다** (`#320` 교훈)."""
    파일들 = list(_검사_폴더.rglob("*.py"))
    assert len(파일들) > 20, f"검사 파일을 {len(파일들)}개밖에 못 찾았다 — 스캐너가 고장 났다"
