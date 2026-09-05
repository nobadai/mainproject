"""시나리오 붕괴 — **같은 안이 둘이면 선택지가 아니다** (`#301` · 2026-09-05).

🔴 매입이 실측으로 짚어 준 자리다.

```text
보수  ((배추 2571), ((offset 0, 배추 2571),))
기본  ((배추 2571), ((offset 0, 배추 2571),))   ← 보수와 **글자 그대로 같다**
공격  ((배추 2571), ((offset 0, 1286), (offset 6, 1285)))

서로 다른 지문 = 2  →  `>= 2` 를 통과 → PASS · 지적 0
```

★ 이 파일이 지키는 것은 넷이다.

  ```text
  ① 같은 안이 둘이면 붕괴다              사용자에게 같은 것을 두 번 보여준다
  ② 2안 설계 근거는 그대로다              총량이 같아도 분할이 다르면 다른 선택지다
  ③ spread_min 이 실제로 결과를 바꾼다     전에는 죽어 있었다
  ④ Critic 까지 닿는다                    룰을 두 벌 짜지 않는다 (§6.4)
  ```
"""

from __future__ import annotations

import pytest

from app.contracts.core import VARIANT_SPREAD_MIN, ClipResult, SplitLeg
from app.orchestrator.band import detect_collapse_type, detect_variant_collapse


def _r(scenario_id: str, qty: float, legs: list[tuple[int, float]]) -> ClipResult:
    return ClipResult(
        scenario_id=scenario_id,
        qty_kg={"배추": qty},
        clipped_qty_kg={"배추": float(qty)},
        clipped_split_plan=tuple(
            SplitLeg(offset_days=o, qty_kg={"배추": float(q)}) for o, q in legs
        ),
    )


def _one(scenario_id: str, qty: float) -> ClipResult:
    return _r(scenario_id, qty, [(0, qty)])


def _split(scenario_id: str, qty: float) -> ClipResult:
    half = round(qty / 2)
    return _r(scenario_id, qty, [(0, half), (6, qty - half)])


# ── ① 매입이 낸 세 경우 ───────────────────────────────────────────────────


def test_셋이_같은_수량인데_분할만_다르면_붕괴다():
    """🔴 **`#301` 의 본문이다.** `cap=4,243,163` 실측 — 보수와 기본이 완전히 같다.

    ★ 서로 다른 지문이 2라 옛 `>= 2` 를 통과했다. 사용자 화면에는 세 안이 뜨는데
      **둘이 같은 안**이다.
    """
    results = [_one("보수", 2571), _one("기본", 2571), _split("공격", 2571)]

    assert len({r.signature() for r in results}) == 2, "지문이 2인 것이 이 검사의 전제다"
    assert detect_variant_collapse(results) is True


def test_총량이_크게_벌어져_있으면_붕괴가_아니다():
    """`cap=9,000,000` 실측 — 보수 2,571 vs 기본·공격 5,454. 스프레드 0.529.

    ⚠️ **기본과 공격은 총량이 같고 분할만 다르다.** `:690` 이 그것을 *"현금흐름과 로트
      나이가 다른 서로 다른 선택지"* 라 정했고, 이 판이 그 결정을 안 건드린다.
    """
    results = [_one("보수", 2571), _one("기본", 5454), _split("공격", 5454)]

    assert len({r.signature() for r in results}) == 3
    assert detect_variant_collapse(results) is False


def test_총량이_붙어_있어도_지문이_다_다르면_통과한다():
    """`cap=4,500,000` 실측 — 2,571 / 2,727 / 2,727(분할). 스프레드 0.057.

    🟡 **이 판이 바꾸지 않는 자리다.** 총량이 임계 안으로 붙었는데 분할이 달라 통과한다 —
      *"총량이 붙었는데 분할만 다른 안을 선택지로 볼 것인가"* 는 `:690` 이 이미 한쪽으로
      정한 도메인 판단이고, 뒤집으려면 매입·팀 결정이 필요하다.
    """
    results = [_one("보수", 2571), _one("기본", 2727), _split("공격", 2727)]

    assert detect_variant_collapse(results) is False


# ── ② 2안 설계 근거는 그대로다 ────────────────────────────────────────────


def test_2안_총량이_같고_분할만_달라도_붕괴가_아니다():
    """★ `:690` 이 적은 그 예시다 — 5,449kg 을 오늘 전량 vs 오늘 2,725 / D+3 2,724.

    🔴 **바뀌는 것은 "몇 개가 달라야 하는가" 뿐이고 2안에서는 답이 같다** (`2 == 2`).
    """
    results = [_one("a", 5449), _r("b", 5449, [(0, 2725), (3, 2724)])]

    assert detect_variant_collapse(results) is False


def test_2안이_완전히_같으면_붕괴다():
    results = [_one("a", 2571), _one("b", 2571)]

    assert detect_variant_collapse(results) is True


def test_살아남은_안이_하나뿐이면_붕괴다():
    results = [_one("a", 2571)]

    assert detect_variant_collapse(results) is True


# ── ③ spread_min 이 실제로 결과를 바꾼다 (전에는 죽어 있었다) ─────────────


def test_같은_안이_섞여도_남은_폭이_넓으면_붕괴가_아니다():
    """★ `②` 의 원래 취지 — *"총량 스프레드가 임계 이상이면 붕괴 아님 (방어적)"*.

    기본·공격이 완전히 같지만 보수가 크게 떨어져 있어 **실질 선택지가 둘은 된다.**
    """
    results = [_one("보수", 1000), _one("기본", 5000), _one("공격", 5000)]

    spread = (5000 - 1000) / 5000
    assert spread >= VARIANT_SPREAD_MIN, "스프레드가 임계 위인 것이 전제다"
    assert detect_variant_collapse(results) is False


def test_임계가_결과를_바꾼다():
    """🔴 **전에는 `spread_min` 이 결과를 한 번도 안 바꿨다.**

    ```text
    옛 ② 에 닿으려면 지문이 **전부 같아야** 했다
       → 수량 벡터도 전부 같다 → 스프레드가 항상 0 → 항상 붕괴
    ```

    ★ 매입이 물은 `constraints.yaml:208 collapsed_threshold`(읽는 코드 0곳)와 짝이었다 —
      양쪽 다 죽어 있었다. 이제 같은 입력이 임계에 따라 갈린다.
    """
    results = [_one("보수", 1000), _one("기본", 5000), _one("공격", 5000)]

    assert detect_variant_collapse(results, spread_min=0.15) is False
    assert detect_variant_collapse(results, spread_min=0.95) is True


def test_총량이_0이면_붕괴다():
    results = [_one("a", 0), _one("b", 0)]

    assert detect_variant_collapse(results) is True


# ── ④ Critic 까지 닿는다 ──────────────────────────────────────────────────


def test_붕괴_종류를_축으로_가른다():
    """`detect_collapse_type` 이 `AXIS` / `QUANTITY` 를 가른다 — 대응이 다르기 때문이다."""
    results = [_one("보수", 2571), _one("기본", 2571), _split("공격", 2571)]

    assert detect_collapse_type(results, ["timing", "timing", "timing"]) == "AXIS"
    assert detect_collapse_type(results, ["quantity", "timing", "timing"]) == "QUANTITY"


def test_Critic_의_classify_collapse_까지_닿는다():
    """🔴 **판정 자리가 아니라 소비 자리를 잰다.**

    ★ `critic_v0_4.classify_collapse` 가 `detect_collapse_type` 에 위임한다 (§6.4 —
      룰을 두 벌 짜지 않는다). **통로가 끊기면 T3 는 잡는데 Critic 은 못 잡는다.**
    """
    from app.critic.critic_v0_4 import classify_collapse

    results = [_one("보수", 2571), _one("기본", 2571), _split("공격", 2571)]

    class _안:
        strategy_type = "timing"

    collapsed, ctype = classify_collapse(results, {r.scenario_id: _안() for r in results})

    assert collapsed is True, "Critic 이 붕괴를 못 봤다"
    assert ctype == "AXIS"


@pytest.mark.parametrize(
    ("kind", "results", "expected"),
    [
        ("셋 다 같음", ["one:2571", "one:2571", "one:2571"], True),
        ("둘만 같음", ["one:2571", "one:2571", "split:2571"], True),
        ("셋 다 다름", ["one:2571", "one:2727", "split:2727"], False),
    ],
)
def test_같은_안의_개수로_갈린다(kind: str, results: list[str], expected: bool):
    """★ 규칙 한 줄로 말하면 — **모든 안이 서로 달라야 붕괴가 아니다.**"""
    clips = [
        (_one if spec.split(":")[0] == "one" else _split)(f"s{i}", float(spec.split(":")[1]))
        for i, spec in enumerate(results)
    ]

    assert detect_variant_collapse(clips) is expected, kind
