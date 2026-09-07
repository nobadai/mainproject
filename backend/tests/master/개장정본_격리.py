"""개장 정본(`master_day_openings`) 격리에 쓰는 것들. **`conftest` 와 검사가 같이 쓴다.**

🔴 **왜 `conftest.py` 에 두면 안 되나** (실측 2026-09-07).

`tests/master/test_db_isolation.py` 가 `from conftest import ...` 로 가져가고 있었는데,
저장소에 `conftest.py` 가 **넷**이다.

```text
tests/finance/conftest.py
tests/logistics/conftest.py
tests/master/conftest.py
tests/test_purchase_agent/conftest.py
```

`tests/master` 만 돌리면 맞는 것을 잡지만, `tests/master tests/logistics` 를 **같이**
돌리면 top-level `conftest` 가 물류 것으로 풀려 `ImportError` 로 **수집 자체가
멈춘다** — 그 폴더에 `__init__.py` 가 없어 `conftest` 가 최상위 이름이기 때문이다.

★ **`#352` 가 머지되고 25분 만에 `dev` 가 그 상태였다.** 파트 하나만 돌리면 초록이고
  합쳐 돌려야 드러난다 — *"안 터지는 이유를 확인하지 않으면 그 이유가 사라질 때
  조용히 틀린다"* 의 수집 단계 판이다.

⚠️ **이름이 겹치지 않는 모듈로 뺀다.** `conftest` 는 pytest 가 특별 취급하는 이름이라
  네 폴더에 다 있어도 정상이고, 겹치는 것을 없앨 수 없다. 겹치지 않는 이름을 쓰는 것이
  고칠 자리다.
"""

from __future__ import annotations

import sys
from types import ModuleType

from app.master import day_opening_repository as _개장_정본_저장소

#: 개장 정본 저장소에서 **실 DB 를 치는 함수들.** 이름을 가져간 자리를 전부 막아야 한다.
개장_정본_실_DB_함수 = ("read_day_opening", "record_day_opening")

#: 막기 전에 잡아 둔 **진짜 함수.** 이 모듈은 fixture 보다 먼저 import 되므로 여기
#: 담기는 것은 언제나 원본이다. `test_db_isolation.py` 가 이것과 비교해 격리를 잰다.
진짜_개장_정본_함수 = {이름: getattr(_개장_정본_저장소, 이름) for 이름 in 개장_정본_실_DB_함수}


def 개장_정본_이름을_가져간_모듈들(이름: str) -> list[ModuleType]:
    """`이름` 을 자기 네임스페이스에 들고 있는 `app.master.*` 모듈을 **전부** 준다.

    🔴 **원본 모듈만 막으면 샌다.** `from X import f` 는 import 시점에 `f` 를 부르는
       쪽 네임스페이스로 **복사한다.** 뒤에 `X.f` 를 바꿔도 이미 복사된 이름은 그대로라
       그 모듈은 **진짜 함수를 계속 부르고, 실 DB 를 친다** — `day_gate` 가 그랬다.

    ⚠️ **손으로 한 줄씩 적지 않는다.** 다음에 다른 모듈이 같은 이름을 가져가면 그때 또
       조용히 새고, 증상이 *"DB 상태에 따라 검사가 갈린다"* 라 **재현이 안 되는
       빨간불**이 된다.

    ★ **fixture 뒤에 import 되는 모듈은 걱정하지 않아도 된다.** 원본 모듈의 속성이
      이미 가짜로 바뀌어 있으므로, 늦게 `from X import f` 하는 쪽은 가짜를 가져간다.
    """
    return [
        모듈
        for 모듈이름, 모듈 in list(sys.modules.items())
        if 모듈이름.startswith("app.master") and 모듈 is not None and hasattr(모듈, 이름)
    ]
