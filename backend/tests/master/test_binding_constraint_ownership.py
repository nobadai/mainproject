"""**안이 무엇 때문에 깎였는가** 의 어휘는 마스터 소유다. 인용이 갈리면 여기서 잡는다.

2026-09-03 · 매입 `P-4` 답.

> 답: **마스터 소유.** 공용 계약이고 Critic 도 검사해야 하니까요.
> 저희가 정하면 저희 화면 문구에 맞춘 값이 됩니다.
> 다만 표시 매핑은 저희가 드리겠습니다 — `WAREHOUSE 창고 · FINANCE 자금 · FRESHNESS 신선도`

`test_retry_cap_ownership.py` 와 같은 규율이다 — **마스터는 매입 코드를 런타임에
읽지 않는다.** 대조는 테스트가 한다.

🔴 **어휘를 정하다 축 충돌을 찾았다.**

```text
자원 축      clipped_by[].constraint     창고 · 현금 · 신선도
근거 출처 축  RationaleSource             예측 · 시세관측 · 재고 · 주문 · 현금 · 문서ID
```

**`"현금"` 이 두 어휘에 동시에 있다.** 같은 문자열인데 뜻이 다르다 — 표시 문구를
값으로 쓰면 축이 섞인다. 영문 상수로 가는 이유가 그것이다.

⚠️ **아직 매입이 안 옮겼다.** 이 파일은 그 사실을 **고정**한다 — 옮기면 빨간불이
되어 알려 준다 (`test_known_gaps` 규율). 고정한 것이 최종 상태가 아니므로 문장을
그렇게 쓴다.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import app.master
from app.contracts.core import BINDING_CONSTRAINT_LABELS, BindingConstraint

_BACKEND = Path(app.master.__file__).parent.parent.parent
_DRAFT_PLAN = _BACKEND / "app" / "purchase_agent" / "nodes" / "draft_plan.py"
_PURCHASE_SCHEMAS = _BACKEND / "app" / "purchase_agent" / "schemas.py"


def _purchase_cap_keys() -> tuple[str, ...]:
    """매입 `draft_plan` 이 `caps=` 로 넘기는 키. **없으면 터진다** — 조용히 안 넘긴다."""
    tree = ast.parse(_DRAFT_PLAN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "caps" and isinstance(kw.value, ast.Dict):
                return tuple(
                    k.value for k in kw.value.keys if isinstance(k, ast.Constant)
                )
    raise AssertionError(f"{_DRAFT_PLAN} 에서 caps= 를 못 찾았다 — 대조가 성립하지 않는다")


def _band_binding_axes() -> tuple[str, ...]:
    """`band` 가 `binding_constraints` 에 넣는 축 이름."""
    tree = ast.parse((_BACKEND / "app" / "orchestrator" / "band.py").read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("cap_")
        ):
            out.append(node.args[0].value)
    return tuple(dict.fromkeys(out))


def _rationale_sources() -> tuple[str, ...]:
    """매입 `RationaleSource` 어휘. 축 충돌을 보는 데 쓴다."""
    tree = ast.parse(_PURCHASE_SCHEMAS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RationaleSource" for t in node.targets)
            and isinstance(node.value, ast.Subscript)
        ):
            return tuple(
                e.value
                for e in ast.walk(node.value)
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    raise AssertionError("RationaleSource 를 못 찾았다")


# ── ① 어휘 자체 ────────────────────────────────────────────────────────────


def test_어휘와_표시_매핑이_짝이_맞는다():
    """🔴 **한쪽만 늘면 조용히 갈린다.**

    값을 더하고 문구를 안 더하면 화면에 영문 상수가 그대로 뜬다. 반대면 안 쓰는
    문구가 남아 *"이런 제약이 있나"* 로 읽힌다.
    """
    assert set(BINDING_CONSTRAINT_LABELS) == set(get_args(BindingConstraint)), (
        "어휘와 표시 매핑이 갈렸다 — 한쪽을 고칠 때 다른 쪽도 고친다"
    )


def test_표시_문구를_값으로_쓰지_않는다():
    """어휘는 영문 상수다. 한글이 값에 섞이면 **표시와 판정이 같은 축**이 된다."""
    for value in get_args(BindingConstraint):
        assert value.isascii() and value.isupper(), f"{value!r} 는 표시 문구처럼 보인다"


# ── ② 🔴 축 충돌 — 어휘를 가르는 이유 ───────────────────────────────────────


def test_현금이_두_어휘에_동시에_있다():
    """🔴 **이것이 영문 상수로 가는 근거다.**

    매입 `caps` 의 `"현금"` 과 `RationaleSource` 의 `"현금"` 은 **다른 축**이다.

    ```text
    자원 축      무엇이 안을 깎았나
    근거 출처 축  이 숫자가 어디서 왔나
    ```

    ⚠️ 이 검사는 **충돌이 사라지면 빨간불**이다. 그때는 축이 갈렸다는 뜻이므로
      이 문장을 지우고 §① 만 남기면 된다.

    ★ **전환 중에는 안 본다.** 매입이 `caps` 를 새 어휘로 옮기면 자원 축에서
      `"현금"` 이 사라져 충돌도 같이 없어진다 — 그때 이 검사가 죽는 것은
      **고장이 아니라 목적 달성**이다 (매입이 *"뒤집는 대상이 둘"* 이라고 짚었다).
    """
    if _purchase_cap_keys() != _OLD_KEYS:
        return  # 전환 완료 — test_전환이_끝나면_좁힌다 가 정리 시점을 알린다

    assert "현금" in _purchase_cap_keys()
    assert "현금" in _rationale_sources(), (
        "RationaleSource 에서 '현금' 이 빠졌다 — 축 충돌이 풀렸으면 이 검사를 정리한다"
    )


# ── ③ 매입이 아직 안 옮겼다 (고정) ─────────────────────────────────────────


#: 전환 중에 허용하는 두 상태. **둘 다 아니면 빨간불이다.**
_OLD_KEYS = ("창고", "현금", "신선도")
_NEW_KEYS = ("WAREHOUSE", "FINANCE", "FRESHNESS")


def test_매입_caps_키가_옛것이거나_새것이다():
    """🔴 **전환 창을 없앤다** (매입 지적 2026-09-03).

    처음에는 옛 한글만 허용해 *"매입이 옮기면 빨간불"* 로 뒀는데, 그러면 **어느
    순서로 가도 dev 가 빨간불이 되는 구간**이 생긴다.

    ```text
    매입이 먼저 바꾼다     → 이 검사가 옛 값을 단언해서 깨진다
    마스터가 먼저 뒤집는다  → 매입이 아직 안 바꿔서 깨진다
    ```

    **한 PR 에 둘 다 넣지 않는 한 창이 남는다.** 그런데 마스터 검사를 매입 PR 이
    고치면 소유 규율이 깨진다 (매입이 ⓑ 를 스스로 뺀 이유).

    ★ **그래서 전환 기간을 검사가 표현한다.** 둘 중 하나면 통과하고, 셋도 아닌
      값이 오면 여전히 잡는다. 전환이 끝나면 `_OLD_KEYS` 를 지워 다시 좁힌다.

    ⚠️ **"뒤집는 것은 마스터 몫" 이다** — 매입이 주어가 빠졌다고 짚어 줬다.
      이 파일이 `tests/master/` 에 있고 어휘 소유가 마스터다
      (`test_retry_cap_ownership` 선례와 같다).
    """
    keys = _purchase_cap_keys()

    assert keys in (_OLD_KEYS, _NEW_KEYS), (
        f"매입 caps 키가 {keys} 다. 옛 어휘도 새 어휘도 아니다 — "
        f"{_OLD_KEYS} 또는 {_NEW_KEYS} 여야 한다"
    )


def test_전환이_끝나면_좁힌다():
    """⚠️ **허용 목록이 둘인 것은 한시 상태다.** 좁힐 시점을 여기서 알린다.

    🔴 **실패가 아니라 `skip` 이다.** 실패로 두면 매입이 옮기는 순간 dev 가
      빨간불이 되고, 그것이 이 판이 없애려던 **전환 창**이다.

    ```text
    실패로 두면   매입 PR 머지 → dev 빨간불 → 마스터가 급히 고침
    skip 이면     매입 PR 머지 → skipped 에 사유가 남음 → 마스터가 정리
    ```

    ★ **`skipped` 로 사실을 남기는 것이 규율이다** (`04` 문서 §3.3).
      통과로 읽히면 안 되는 것을 통과시키지 않으면서 사실을 남긴다.
    """
    import pytest

    if _purchase_cap_keys() == _NEW_KEYS:
        pytest.skip(
            "🟢 매입이 새 어휘로 옮겼다 — 이제 마스터가 좁힌다. "
            "_OLD_KEYS 를 지우고 test_매입_caps_키가_옛것이거나_새것이다 를 "
            "새 어휘만 보게 하며, test_현금이_두_어휘에_동시에_있다 도 정리한다"
        )

    assert _purchase_cap_keys() == _OLD_KEYS, (
        f"매입 caps 키가 {_purchase_cap_keys()} 다 — 옛 어휘도 새 어휘도 아니다"
    )


def test_옛_키와_새_어휘가_일대일로_대응한다():
    """옮길 때 **빠지거나 남는 것이 없어야** 한다.

    지금 한글 셋과 새 어휘 셋의 **개수가 같고 표시 문구로 이어진다** — `"현금" → 자금`
    만 문구가 바뀐다 (재무 제약이 차입여력까지 포함해서 *"현금"* 은 좁다).
    """
    old = _purchase_cap_keys()
    assert len(old) == len(get_args(BindingConstraint))

    labels = set(BINDING_CONSTRAINT_LABELS.values())
    assert {"창고", "신선도"} <= labels, "그대로 가는 둘이 표시 매핑에 없다"
    assert "자금" in labels and "현금" not in labels, (
        "'현금' 을 표시 문구로 되돌리면 매입·판매가 정리한 뜻이 사라진다"
    )


# ── ④ 🔴 밴드 축은 다른 어휘다 (매입 지적) ─────────────────────────────────


def test_밴드_축_어휘를_흡수하지_않는다():
    """🔴 **"무엇이 안을 깎았나" 가 두 어휘로 다닌다 — 그리고 그것이 의도다.**

    매입이 짚었다.

    > `#203` 이 `band.py` 를 한 줄도 안 건드렸습니다. 그런데 `band.py` 의
    > `binding_constraints` 를 Critic 이 읽습니다.

    **사실이다. 다만 층이 다르다.**

    ```text
    BindingConstraint               자원     창고 · 자금 · 신선도
    ClipResult.binding_constraints  밴드 축   cap_total_kg · cap_amount_krw ·
                                             cap_by_date.{날짜}
    ```

    ⚠️ **셋이 안 겹친다.** `신선도` 는 밴드 축에 없고(매입 내부 제약),
      `cap_by_date.{날짜}` 는 날짜가 붙어 어휘를 못 닫는다.

    ★ **합치면 Critic 이 잃는다** — `cap_total_kg` 과 `cap_by_date.2026-01-05` 가
      같은 `WAREHOUSE` 가 되면 LLM 이 인과를 대조할 재료가 줄어든다
      (`critic/llm/runtime.py:74` 가 그것으로 판정한다).
    """
    band_axes = _band_binding_axes()

    assert band_axes, "band 가 축 이름을 안 만든다 — 대조가 성립하지 않는다"
    assert not (set(band_axes) & set(get_args(BindingConstraint))), (
        f"두 어휘가 겹친다: {sorted(set(band_axes) & set(get_args(BindingConstraint)))}"
    )


def test_밴드_축은_어휘를_닫을_수_없다():
    """⚠️ `cap_by_date.{날짜}` 는 **날짜가 붙는다** — `Literal` 로 못 막는다.

    이것이 두 어휘를 합칠 수 없는 기술적 이유다.
    """
    source = (_BACKEND / "app" / "orchestrator" / "band.py").read_text(encoding="utf-8")

    assert 'f"cap_by_date.{' in source, (
        "cap_by_date 축이 날짜를 안 붙인다 — 그러면 어휘를 닫을 수 있고 "
        "이 검사의 근거가 사라진다"
    )
