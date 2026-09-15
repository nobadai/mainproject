"""스위트 전체 공통 — **테스트가 전역 상태를 원래대로 두고 나가게 한다.**

2026-09-03 · `tests/master` 가 재무 테스트를 깨뜨리는 것을 실측하다 나왔다.

🔴 **전역 레지스트리가 파트 밖으로 샜다.**

```text
pytest tests/master tests/finance/test_finance_api.py   → 1 failed
pytest tests/finance/test_finance_api.py tests/master   → 통과
pytest (전체 · 기본 순서)                                → 통과
```

`app/master/wiring._REGISTRY` 는 프로세스 전역이고, 등록은 `app/main.py` 가
**import 시점에 한 번** 한다. `wiring.reset()` 이 그것을 비우면 그 모듈은 이미
import 돼 있어 **다시 등록되지 않는다.**

⚠️ **전체 스위트가 알파벳순이라 안 걸렸다.** 재무가 마스터보다 먼저 돌아서
우연히 통과했고, **부분 실행에서만 깨졌다.** 각 파트가 자기 스위트만 돌리면
평생 안 보인다.

★ **부르는 쪽 다섯을 고치지 않는다.**

```text
tests/finance/test_finance_boundary_history.py:478
tests/master/test_ask.py:142 · :144
tests/master/test_master_api.py:24 · :26
```

  다섯을 고쳐도 **여섯 번째가 생기면 같은 일이 난다.** 여기서 한 번 막으면
  누가 어디서 부르든 그 테스트 밖으로 안 샌다.

★ **`reset()` 을 금지하지 않는다.** 등록을 비우는 것은 마스터 API 가 *"어댑터
  미등록"* 경로를 재려면 반드시 필요하다 — 막을 것은 **비우는 것**이 아니라
  **비운 채로 나가는 것**이다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.master import day_open, transition, wiring


@pytest.fixture(autouse=True)
def 전역_에이전트_레지스트리를_되돌린다() -> Iterator[None]:
    """테스트가 등록을 어떻게 바꾸든 **끝나면 원래대로**.

    ★ 테스트 앞에서 비우지 않는다. 그러면 *"등록된 상태를 전제하는"* 테스트가
      전부 깨진다 — 되돌리는 것이지 초기화하는 것이 아니다.
    """
    saved = wiring.snapshot()
    try:
        yield
    finally:
        wiring.restore(saved)


@pytest.fixture(autouse=True)
def 전역_전이_등록소를_되돌린다() -> Iterator[None]:
    """전이 등록소도 같은 자리에서 샌다 — **끝나면 원래대로**.

    🔴 **위 레지스트리와 똑같은 함정이다.** `app/master/transition._TRANSITIONS` 도
       프로세스 전역이고 등록은 `app/main.py` 가 **import 시점에 한 번** 한다.
       `transition.reset()` 을 부른 테스트가 그대로 나가면 그 프로세스에서 **영영
       빈 채로** 남는다 — 이미 import 된 `app.main` 은 다시 등록되지 않는다.

    ⚠️ 전이 등록이 0건이던 동안에는 비워도 비운 티가 안 났다. 등록이 서는 순간
       (`#272`) 부터 이 새는 자리가 실제로 다른 파트 검사에 닿는다.

    ★ `wiring` 과 달리 `snapshot`/`restore` 가 없어 여기서 `registered()` 로 뜨고
      `reset()` + `register_transition()` 으로 되돌린다 — 마스터 모듈에 검사 전용
      함수를 더하지 않는다.
    """
    saved = dict(transition.registered())
    try:
        yield
    finally:
        transition.reset()
        for part, impl in saved.items():
            transition.register_transition(part, impl)


@pytest.fixture(autouse=True)
def 전역_하루넘김_등록소를_되돌린다() -> Iterator[None]:
    """하루 넘김 등록소도 프로세스 전역이다 — **끝나면 원래대로**.

    ⚠️ **오늘은 함정이 아직 없다.** `app/master/day_open._OPENINGS` 는 전역이지만
       `app/main.py` 에 등록 줄이 없다 (재무 미회신 · 물류 파트 소유). 그래서
       `reset()` 을 부른 테스트가 그대로 나가도 지워질 등록 자체가 없다.

    ★ **그래도 지금 세워 둔다.** 전이 등록소가 정확히 그 순서로 물렸다 — 0건이던
      동안에는 비워도 티가 안 나다가, 등록이 서는 날(`#272`) 갑자기 다른 파트
      검사에 닿았다. 여기서 새는 방향은 반대다: 대역을 등록한 테스트가 그대로
      나가면 **가짜 구현이 다음 테스트로 흘러든다.**

    ★ 위 전이 fixture 와 같은 방식이다 — `registered()` 로 뜨고 `reset()` +
      `register_day_opening()` 으로 되돌린다.
    """
    saved = dict(day_open.registered())
    try:
        yield
    finally:
        day_open.reset()
        for part, impl in saved.items():
            day_open.register_day_opening(part, impl)
