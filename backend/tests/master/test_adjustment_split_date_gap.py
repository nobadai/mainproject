"""`timing` 조정안의 **대상 회차가 표준형에 안 실린다.** 채우면 여기서 알려 준다.

2026-09-03 · 재무 A-2 답을 대조하다 나왔다.

🔴 물류가 값을 손에 들고 쓰면서 **칸에만 안 넣는다.**

```python
# app/logistics/adapter.py:1118·1129
key = (adjustment.axis, adjustment.split_date, target)          # 중복 제거 키에 쓴다
reason = f"… {adjustment.split_date.isoformat()} 회차 — …"       # 문장에도 쓴다

SuggestedAdjustment(dept=…, axis=…, target_value=…, unit=…, reason=…, ref_ids=…)
                                                        # ← split_date= 가 없다
```

🔴 **마스터 화면이 그 값을 이미 읽는다** (`answer.py:297`). 늘 `None` 이라 *"N 회차"*
문장이 한 번도 안 나왔다.

⚠️ **`None` 이 두 뜻이다.**

```text
재무 amount    회차 개념이 없다      → None 이 정상
물류 timing    회차가 있는데 안 실었다 → None 이 누락
```

화면은 지금 둘을 같게 본다.

★ **강제는 아직 안 한다.** `__post_init__` 에서 *"timing 이면 필수"* 로 막으면
  **지금 물류가 던지는 것이 계약 위반**이 된다. 남의 파트를 깨뜨리지 않는다.

```text
지금     안 채워진 사실을 고정한다        2026-09-03 오전
채운 뒤   검사를 뒤집는다                 2026-09-03 저녁 · 물류 #214
그 뒤    __post_init__ 으로 강제한다      같은 판 — 아무도 안 깨졌다
```

🟢 **셋이 다 끝났다 (2026-09-03).** 물류가 `#214` 로 두 칸을 채웠고, 같은 판에서
강제를 걸었다. 이 파일은 이제 *"안 채운다"* 가 아니라 **"채운다"** 를 고정한다.

★ **강제를 미룬 것이 옳았다.** 오전에 걸었으면 물류가 던지는 것이 계약 위반이
  됐다. 저녁에 거니 전 스위트가 초록불이다 — `04` 문서 §6.1 의 *"선언은 있는데
  강제가 없다"* 를 **시점을 적어 두는 것**으로 끊은 첫 사례다.

⚠️ `04` 문서 §6.1 에 *"선언은 있는데 강제가 없다"* 를 반복 패턴으로 적어 뒀다.
**강제 시점을 미리 적어 두는 것**이 그 반복을 끊는 방법이다.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from datetime import date
from pathlib import Path

import pytest

import app.master
from app.contracts.core import ContractViolation, SuggestedAdjustment
from app.master.answer import _scope
from app.master.flow import _wire

_BACKEND = Path(app.master.__file__).parent.parent.parent
_LOGISTICS = _BACKEND / "app" / "logistics" / "adapter.py"
_FINANCE = _BACKEND / "app" / "finance" / "execution.py"


def _kwargs_of_construction(path: Path) -> tuple[str, ...]:
    """그 파일에서 `SuggestedAdjustment(...)` 를 세우며 넘기는 키워드.

    **호출이 하나가 아니면 터진다** — 여러 자리를 뭉쳐 세면 한 곳만 고쳐도 통과한다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SuggestedAdjustment"
    ]
    assert len(found) == 1, f"{path} 의 SuggestedAdjustment 생성이 {len(found)} 곳이다"
    return tuple(kw.arg for kw in found[0].keywords if kw.arg)


# ── ① 지금 상태 (고정) ─────────────────────────────────────────────────────


def test_물류가_split_date_를_싣는다():
    """🟢 **뒤집힌 검사다** (물류 `#214` · 2026-09-03).

    전에는 *"안 싣는다"* 를 고정했다. 이제는 **싣는 것**을 고정한다 —
    되돌아가면 여기가 운다.
    """
    passed = _kwargs_of_construction(_LOGISTICS)

    assert "split_date" in passed, (
        "물류가 split_date 를 다시 안 싣는다 — 화면의 'N 회차' 문장이 없어진다"
    )


def test_물류가_scenario_labels_도_싣는다():
    """🔴 **빈 칸이 하나가 아니라 둘이다** (물류 지적 2026-09-03).

    화면이 **두 칸을 읽는다** (`answer._scope`).

    ```python
    if adjustment.scenario_labels:  parts.append(f"{'·'.join(...)}안")
    if adjustment.split_date:       parts.append(f"{...} 회차")
    ```

    ⚠️ **이쪽은 한 줄이 아니다.** 중복 제거 루프가 라벨을 그 자리에서 버린다.

    ```python
    key = (adjustment.axis, adjustment.split_date, target)
    if key in seen_adjustments:
        continue          # ← 두 번째 시나리오의 라벨이 여기서 사라진다
    ```

      같은 조정이 세 시나리오에서 나와도 **첫 라벨 하나만** 손에 남는다.
      수집 → 조립 2단계로 바꿔야 채울 수 있다 (물류 미결 §0-5).

    ★ 그래서 둘을 **같이** 잠근다 — 물류가 한 판에서 만들면 한 번에 뒤집힌다.
    """
    passed = _kwargs_of_construction(_LOGISTICS)

    assert "scenario_labels" in passed, (
        "물류가 scenario_labels 를 다시 안 싣는다 — 같은 조정이 세 안에서 나와도 "
        "화면에 안이 하나만 보인다"
    )


def test_물류는_그_값을_손에_들고_있다():
    """전제 단언. **값이 없어서 못 싣는 것이 아니다** — 그러면 부탁이 성립하지 않는다.

    같은 함수가 중복 제거 키와 `reason` 문장에 그 값을 쓴다.
    """
    source = _LOGISTICS.read_text(encoding="utf-8")

    assert "adjustment.split_date" in source, (
        "물류가 split_date 를 아예 안 쓴다 — 그러면 '한 줄이면 된다' 가 틀린 말이 된다"
    )


def test_재무는_두_칸을_다_옮긴다():
    """대조군. **물류만의 문제**라는 것을 보인다.

    재무 `amount` 축은 회차 개념이 없어 `split_date` 가 `None` 인 것이 정상인데,
    **옮기는 코드는 있다.** 오는 날 그대로 흐른다.

    🟢 `scenario_labels` 는 실제로 채운다 (`#197`). 그 자리 주석이 왜 둘 다
    필요한지를 적어 뒀다.

    > 예전에는 여섯 칸만 옮겼다. 그래서 상류가 `scenario_labels` 를 채워도 이
    > 지점에서 잃었다.

    **재무는 생성부와 변환부를 둘 다 고쳤고 물류는 둘 다 안 했다.**
    """
    passed = _kwargs_of_construction(_FINANCE)

    for name in ("split_date", "scenario_labels"):
        assert name in passed, (
            f"재무도 {name} 을 안 옮긴다 — 그러면 이 문제는 물류 하나가 아니라 "
            f"계약 전달의 문제다"
        )


# ── ② 읽는 쪽은 이미 있다 ──────────────────────────────────────────────────


def test_화면이_그_값을_이미_읽는다():
    """🔴 **읽는 쪽이 있는데 채우는 쪽이 없다.**

    `answer._scope` 가 `split_date` 로 *"N 회차"* 를 만든다.
    늘 `None` 이라 그 문장이 한 번도 안 나왔다.
    """
    with_date = SuggestedAdjustment(
        dept="inventory",
        axis="timing",
        target_value=3.0,
        unit="d",
        reason="사유",
        ref_ids=("REF-1",),
        split_date=date(2026, 1, 5),
    )
    # ⚠️ **대조군은 재무 `amount` 다.** timing 은 이제 계약이 회차를 강제해서
    #   split_date 없이 세울 수 없다. 회차 개념이 없는 축이 그 자리를 대신한다 —
    #   재는 것은 그대로다: **값이 없으면 문장을 안 만든다.**
    without = SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=3.0,
        unit="krw",
        reason="사유",
        ref_ids=("REF-1",),
    )

    assert "2026-01-05 회차" in _scope(with_date)
    assert "회차" not in _scope(without), (
        "값이 없는데 회차 문장이 나온다 — 마스터가 없는 것을 지어내고 있다"
    )


def test_화면이_라벨도_이미_읽는다():
    """`scenario_labels` 도 같은 함수가 읽는다. **둘 다 늘 비어 있었다.**"""
    with_labels = SuggestedAdjustment(
        dept="inventory",
        axis="timing",
        target_value=3.0,
        unit="d",
        reason="사유",
        ref_ids=("REF-1",),
        scenario_labels=("보수", "기본"),
        split_date=date(2026, 1, 5),
    )

    assert "보수·기본안" in _scope(with_labels)


def test_채우면_전선에서_안_터진다():
    """`#175` 가 미리 막아 둔 자리. **지금 채우셔도 안전하다.**

    `asdict` 는 `date` 를 그대로 둬서 `json.dumps` 에서 죽는다.
    `_wire` 가 ISO 문자열로 편다 — 그때는 물류가 안 채워서 안 터지고 있었다.
    """
    wired = _wire(
        SuggestedAdjustment(
            dept="inventory",
            axis="timing",
            target_value=3.0,
            unit="d",
            reason="사유",
            ref_ids=("REF-1",),
            split_date=date(2026, 1, 5),
        )
    )

    assert wired["split_date"] == "2026-01-05", "date 객체가 그대로 나가면 json 에서 죽는다"
    assert isinstance(wired["ref_ids"], list), "튜플도 같이 펴야 한다"


# ── ③ 강제는 아직 안 한다 (시점을 못 박는다) ────────────────────────────────


def test_timing_에_split_date_를_강제한다():
    """🟢 **미뤄 뒀던 3단계다** (2026-09-03 저녁).

    물류가 `#214` 로 채운 뒤에 걸었더니 **아무도 안 깨졌다.**
    오전에 걸었으면 물류가 던지는 것이 계약 위반이 됐다.

    ★ **`None` 의 두 뜻이 하나가 된다.**

    ```text
    이제      timing 이면 회차가 반드시 있다
    그래서    화면의 None 은 "회차 개념이 없는 축" 하나만 뜻한다
    ```
    """
    with pytest.raises(ContractViolation, match="split_date"):
        SuggestedAdjustment(
            dept="inventory",
            axis="timing",
            target_value=3.0,
            unit="d",
            reason="사유",
            ref_ids=("REF-1",),
        )


def test_회차_개념이_없는_축은_그대로_None_이다():
    """⚠️ **강제가 재무를 안 문다.** `amount` 는 회차가 없는 것이 정상이다.

    여기가 빨간불이면 강제를 너무 넓게 건 것이다.
    """
    adjustment = SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=20_000_000.0,
        unit="krw",
        reason="한도",
        ref_ids=("REF-F-1",),
    )

    assert adjustment.split_date is None


def test_칸_자체는_계약에_있다():
    """어휘가 없는 것이 아니라 **안 채우는 것**이다. 둘을 섞지 않는다."""
    names = {f.name for f in fields(SuggestedAdjustment)}

    assert "split_date" in names
    assert "scenario_labels" in names, "같은 판(#175·되먹임 v0.2)에서 만든 짝이다"
