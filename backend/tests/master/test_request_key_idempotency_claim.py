"""업무 키에 대한 **주석의 주장을 실제로 잰다** (2026-09-11 · 매입 지적).

🔴 **적어 놓고 한 번도 안 쟀다.** 세 자리가 `master_agent_runs_run_request_unique` 를
   *"두 번째를 막는다"* 의 근거로 적고 있었는데, **거짓이었다.**

```text
master_agent_runs_pkey                 UNIQUE (run_id)          ← run_id 가 PK 다
master_agent_runs_run_request_unique   UNIQUE (run_id, request_id)
```

★★ **PK 를 품은 복합 유니크는 아무것도 더 막지 않는다.** 앞 칸이 이미 유일하므로
  뒤에 무엇을 붙여도 그 인덱스는 **언제나 통과**한다.

⚠️ 오늘 잠금을 세우며 *"재는 줄은 있는데 재는 대상이 없는 검사가 가장 위험하다"* 고
  적었는데, **주석이 그 모양이었다.** 그래서 이 검사를 둔다 — 주석이 무엇을 주장하면
  그 주장이 참인지 **코드가 재야** 한다.

🟢 **인덱스를 바꾸지 않는다.** 같은 업무 키에 행이 여럿 서는 것은 **의도**다 — 판단은
   걸을 때마다 다시 서고, 그래야 부서가 고친 것을 다시 잴 수 있다.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from app.master import run_repository, scheduler

#: 🔴 이 이름이 **멱등의 근거로** 인용되면 안 된다.
_인덱스 = "master_agent_runs_run_request_unique"

def _원문(모듈) -> str:
    return unicodedata.normalize("NFC", Path(모듈.__file__).read_text(encoding="utf-8"))


def test_인덱스_이름이_설명하는_한_자리에만_있다() -> None:
    """🔴 **세 자리가 그 이름을 「멱등의 근거」로 적고 있었다.**

    ★★ 「가까이 '막는다' 가 오면 잡는다」로 쟀다가 **변이가 안 물었다.** 제가 둔
      예외(*"'거짓' 이 근처면 봐준다"*)가 변이를 삼켰고, 바꾼 줄에는 인덱스 전체
      이름이 없어 그물에도 안 걸렸다. 오늘 제가 경고한 바로 그 모양이다.

    🟢 그래서 **자리 수**를 잠근다. 왜 거짓인지는 **한 자리**에만 있고, 나머지는 그
      자리를 가리킨다 — 같은 설명이 세 벌이면 한쪽만 고쳐지는 날이 온다.
    """
    설명한자리 = _원문(run_repository).count(_인덱스)
    나머지 = _원문(scheduler).count(_인덱스)

    assert 설명한자리 == 1, (
        f"설명이 한 자리가 아니다 (run_repository 에 {설명한자리}번)."
        " 여러 벌이면 한쪽만 고쳐지는 날이 온다"
    )
    assert 나머지 == 0, (
        f"scheduler 가 인덱스 이름을 {나머지}번 든다 —"
        " 이유는 `run_repository.build_request_id` 한 자리에만 두고 가리킨다"
    )


def test_인덱스를_안_바꾼다는_판단이_적혀_있다() -> None:
    """🟢 **안 바꾸는 것이 결론이다.** 같은 업무 키에 행이 여럿인 것은 **의도**다.

    ⚠️ 이 검사가 없으면 *"거짓이니 인덱스를 고치자"* 로 가는 날 아무도 안 막는다 —
      고치면 **재실행이 막히고**, 그러면 부서가 고친 것을 다시 잴 수 없다.
    """
    assert "인덱스를 안 바꾼다" in _원문(run_repository), (
        "'인덱스를 안 바꾼다' 는 판단이 사라졌다 — 왜 그대로 두는지가 없으면"
        " 다음 사람이 인덱스를 고치고 재실행을 막는다"
    )


def test_그_주장이_거짓인_이유가_한_자리에_적혀_있다() -> None:
    """★ **없애기만 하면 다음 사람이 같은 주장을 다시 쓴다.**

    🔴 왜 거짓인지가 **한 자리**에 있어야 하고, 나머지는 그 자리를 가리켜야 한다 —
       같은 설명이 세 벌이면 한쪽만 고쳐지는 날이 온다.
    """
    원문 = _원문(run_repository)

    assert "PK" in 원문 or "run_id 가 PK" in 원문, "왜 안 막는지가 안 적혀 있다"
    assert "언제나 통과" in 원문, "'언제나 통과한다' 는 사실이 안 적혀 있다"
    assert "의도" in 원문, "같은 업무 키에 행이 여럿인 것이 **의도**라는 말이 없다"


def test_다른_두_자리가_그_한_자리를_가리킨다() -> None:
    """⚠️ 설명을 베껴 두면 갈린다 — **가리키게** 한다."""
    원문 = _원문(scheduler)

    assert "build_request_id" in 원문, (
        "scheduler 가 이유를 적은 자리(`run_repository.build_request_id`)를 안 가리킨다"
    )


def test_세는_축을_밝히라는_말이_있다() -> None:
    """🔴 **재실행이 있는 실행에서 행으로 세면 부풀려진다.**

    ★★ 매입이 행으로 세다 30 이 더 나왔고, 제가 축을 안 밝힌 것이 원인이었다.
      실측: `SIM-WALK-2026-V4` 는 창을 두 번 걸어 **행 854 · 업무 키 428** 이다.
    """
    원문 = _원문(run_repository)

    assert re.search(r"행으로\s*세면", 원문), "세는 축에 대한 경고가 없다"
    assert "as_of" in 원문 and "cycle" in 원문, "무엇으로 세라는 말이 없다"
