"""전역 에이전트 레지스트리가 **테스트 밖으로 안 샌다.**

2026-09-03 · `split_date` 검사를 돌리다 재무 테스트가 순서에 따라 갈리는 것을 봤다.

```text
pytest tests/master tests/finance/test_finance_api.py   → 1 failed
pytest tests/finance/test_finance_api.py tests/master   → 통과
pytest (전체 · 기본 순서)                                → 통과
```

🔴 **`wiring.reset()` 은 되돌릴 수 없다.** 등록은 `app/main.py` 가 **import 시점에
한 번** 하는데, 비운 뒤에는 그 모듈이 이미 import 돼 있어 **다시 등록되지 않는다.**

⚠️ **전체 스위트가 알파벳순이라 안 걸렸다.** 재무가 마스터보다 먼저 돌아서 우연히
통과했고 **부분 실행에서만 깨졌다.** 각 파트가 자기 스위트만 돌리면 평생 안 보인다.

★ 막은 자리는 `tests/conftest.py` 의 autouse fixture 다. **부르는 쪽 다섯을 안
  고쳤다** — 고쳐도 여섯 번째가 생기면 같은 일이 난다.
"""

from __future__ import annotations

import app.main  # noqa: F401  — import 시점에 세 파트를 등록한다. 이 검사의 전제다
from app.master import wiring


def _fake_port(request):  # pragma: no cover - 부르지 않는다
    raise AssertionError("등록만 하고 부르지 않는다")


# ── ① snapshot · restore 단위 ──────────────────────────────────────────────


def test_reset_은_되돌릴_수_없다():
    """🔴 **이 파일이 존재하는 이유다.**

    `reset()` 자체는 정당하다 — 마스터 API 가 *"어댑터 미등록"* 경로를 재려면
    필요하다. 문제는 **비운 채로 나가는 것**이고, 그것을 conftest 가 막는다.
    """
    assert wiring.registry().has("finance"), "app.main 이 등록해야 이 검사가 성립한다"

    wiring.reset()

    assert wiring.registry().registered == (), "reset 이 안 비웠다"
    # ⚠️ 여기서 끝나도 다음 테스트는 등록된 상태를 본다 — conftest 가 되돌린다


def test_앞_테스트의_reset_이_안_샜다():
    """🔴 **바로 위 테스트가 비운 채로 끝났다.** 그런데 여기서는 살아 있어야 한다.

    ⚠️ **순서 의존 검사다.** 그것이 이 문제의 성질이라 그대로 잰다 — pytest 는
      파일 안에서 정의 순서대로 돌고, 이 저장소에는 순서를 섞는 플러그인이 없다
      (실측 2026-09-03).
    """
    assert wiring.registry().has("finance"), (
        "앞 테스트의 reset 이 새어 나왔다 — tests/conftest.py 의 autouse fixture 를 확인한다"
    )
    assert wiring.registry().has("inventory")
    assert wiring.registry().has("purchase")


def test_등록을_더해도_안_샌다():
    """비우는 것만이 아니라 **더하는 것**도 되돌린다."""
    wiring.register("finance", _fake_port)  # type: ignore[arg-type]

    assert wiring.registry().get("finance") is _fake_port


def test_앞_테스트가_더한_것도_안_샜다():
    """바로 위가 남긴 가짜 포트가 여기서는 없어야 한다."""
    from app.finance.adapter import finance_port

    assert wiring.registry().get("finance") is finance_port, (
        "앞 테스트가 등록한 가짜 포트가 새어 나왔다"
    )


# ── ② 복원이 합치지 않는다 ─────────────────────────────────────────────────


def test_restore_는_합치지_않고_되돌린다():
    """★ **합치면 되돌린 것이 아니다.**

    테스트가 남긴 등록이 섞여 나가면 그 뒤 테스트가 *"누가 등록했는지"* 를 모른다.
    """
    saved = wiring.snapshot()
    wiring.register("finance", _fake_port)  # type: ignore[arg-type]

    wiring.restore(saved)

    assert wiring.registry().get("finance") is saved["finance"]
    assert wiring.registry().registered == tuple(sorted(saved)), (
        "restore 가 지금 등록된 것을 남겼다 — 되돌린 것이 아니라 합친 것이다"
    )


def test_snapshot_은_그때의_사본이다():
    """뜬 뒤에 바뀌어도 사본은 안 바뀐다 — 아니면 되돌릴 대상이 흔들린다."""
    saved = wiring.snapshot()
    before = dict(saved)

    wiring.reset()
    wiring.register("finance", _fake_port)  # type: ignore[arg-type]

    assert saved == before, "snapshot 이 살아 있는 레지스트리를 가리킨다"
