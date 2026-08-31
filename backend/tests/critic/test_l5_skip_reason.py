"""L5 가 **왜** 판정하지 못했는지 — 셋을 한 문구로 내지 않는다.

전에는 미가동·장애·호출 불필요를 전부 이렇게 냈다.

```text
L5: LLM 판정 미수행 — 논리 일관성 검증 생략
```

**셋은 해야 할 일이 다르다.** 앞 둘은 고칠 것이 있고 뒤는 없다 — 한 줄로 내면
사람이 **없는 문제를 찾는다** (매입 8/31 지적과 같은 종류).

🔴 이 Flow 의 기본 상태는 `SKIPPED_TEMPLATE` 다 — **판정할 문장이 없다.**
   L5 는 *"클리핑 후에 쓰인 결정 근거"* 를 보는데, 1차 Flow 에는 그 문장을 쓰는
   단계가 없다(오케 selector 가 하던 일). **장애가 아니라 아직 없는 단계다.**
"""

from __future__ import annotations

import pytest

from app.critic.critic_v0_4 import _judge_ran, _l5_skip_reason


class _Judge:
    """`JudgeRunner` 대역 — `result.llm_status` 만 흉내 낸다."""

    def __init__(self, status: str | None) -> None:
        self.result = None if status is None else type("R", (), {"llm_status": status})()
        self.ran = status == "SUCCESS"

    def __call__(self, payload):  # pragma: no cover - 호출되지 않는다
        raise AssertionError("이 검사는 judge 를 부르지 않는다")


def test_판정할_문장이_없으면_그렇게_적는다():
    """장애로 못 한 것이 아니다 — **아직 없는 단계**다."""
    말 = _l5_skip_reason(_Judge("SKIPPED_TEMPLATE"))

    assert "판정할 결정 근거가 없다" in 말
    assert "미수행" not in 말, "'미수행' 만 적으면 읽는 사람이 서버를 뒤진다"


def test_불렀는데_실패한_것과_구분한다():
    assert "쓸 수 있는 판정을 못 받았다" in _l5_skip_reason(_Judge("FALLBACK"))


def test_설정이_꺼진_것과도_구분한다():
    assert "설정이 꺼져 있다" in _l5_skip_reason(_Judge("DISABLED"))


@pytest.mark.parametrize("status", ["SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"])
def test_셋이_서로_다른_말을_받는다(status: str):
    """🔴 같은 말이 나오면 가른 의미가 없다."""
    others = {s for s in ("SKIPPED_TEMPLATE", "FALLBACK", "DISABLED") if s != status}
    말 = _l5_skip_reason(_Judge(status))

    assert all(말 != _l5_skip_reason(_Judge(o)) for o in others)


def test_모르는_상태는_종전_문구로_간다():
    """새 상태가 생겨도 **추측해서 번역하지 않는다.**"""
    assert _l5_skip_reason(_Judge(None)) == "LLM 판정 미수행"


def test_돌았는지_판단은_안_바뀐다():
    """문구만 갈랐다 — coverage 를 0 으로 두는 판단은 그대로다."""
    assert _judge_ran(_Judge("SUCCESS")) is True
    assert _judge_ran(_Judge("SKIPPED_TEMPLATE")) is False
    assert _judge_ran(_Judge("FALLBACK")) is False
