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
    """
    assert "현금" in _purchase_cap_keys()
    assert "현금" in _rationale_sources(), (
        "RationaleSource 에서 '현금' 이 빠졌다 — 축 충돌이 풀렸으면 이 검사를 정리한다"
    )


# ── ③ 매입이 아직 안 옮겼다 (고정) ─────────────────────────────────────────


def test_매입은_아직_옛_한글_어휘를_쓴다():
    """⚠️ **고정한 것이 최종 상태가 아니다.**

    어휘는 정해졌고 매입이 옮기는 것은 매입 일정이다. 옮기면 이 검사가 빨간불이
    되어 알려 준다 — 그때 이 파일을 **§④ 쪽으로 뒤집는다.**

    🔴 **마스터가 매입 코드를 런타임에 읽지 않는다.** 읽으면 마스터가 부서 설정을
      배우는 것이 된다. 대조는 여기서만 한다 (`test_retry_cap_ownership` 과 같다).
    """
    assert _purchase_cap_keys() == ("창고", "현금", "신선도"), (
        "매입 caps 키가 달라졌다. 새 어휘로 옮기셨으면 이 검사를 "
        "test_매입이_새_어휘를_쓴다 로 뒤집는다"
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
