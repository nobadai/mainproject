"""계약 밖 품목은 문 앞에서 막는다 (2026-09-03).

🔴 **전에는 아무도 안 걸렀다.** 피마늘을 계약에서 뺀 뒤 실측하니 그 요청이
**Critic L0 까지 가서** `E-UNKNOWN-ITEM` 으로 죽었다.

```text
전   요청 → 매입 호출 → 안 생성 → 세 부서 판정 → Critic L0 에서 E-UNKNOWN-ITEM
후   요청 → 422
```

막히기는 했다. 다만 그때까지 매입을 부르고 안을 만들고 세 부서를 다 돈다.
**"막힌다" 와 "일찍 막힌다" 는 다르다.**

★ `app/ml/router.py:35` 가 이미 같은 일을 하고 있었다. 같은 일을 다른 방식으로
  하지 않는다.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.contracts.core import ITEMS
from app.master.schemas import ProcurementRunRequest


def _request(**kw):
    base = {"as_of": date(2025, 12, 31), "policy_version": "v1.0"}
    return ProcurementRunRequest(**{**base, **kw})


@pytest.mark.parametrize("item", ITEMS)
def test_계약_안_품목은_통과한다(item: str):
    assert _request(item=item).item == item


def test_계약_밖_품목은_막힌다():
    with pytest.raises(ValidationError) as caught:
        _request(item="피마늘")

    assert "피마늘" in str(caught.value)


def test_막을_때_가능한_목록을_말해_준다():
    """★ **못 한 것을 말할 때 무엇이 되는지도 말한다.**

    *"지원하지 않는 품목"* 만 내면 부르는 쪽이 다음에 무엇을 넣을지 모른다.
    """
    with pytest.raises(ValidationError) as caught:
        _request(item="없는품목")

    message = str(caught.value)
    for item in ITEMS:
        assert item in message, f"가능 목록에 {item} 이 없다"


def test_품목을_안_주는_것은_막지_않는다():
    """🔴 **`None` 과 계약 밖은 다르다** (§1.2-10).

    안 주면 매입이 `missing_data: ["item"]` 으로 그 사실을 낸다.
    여기서 막으면 *"품목 없이 도는"* 정상 경로가 죽는다.
    """
    assert _request().item is None
    assert _request(item=None).item is None


def test_ML_과_같은_목록을_쓴다():
    """★ 두 문이 다른 목록을 보면 한쪽만 고쳐지는 날이 온다."""
    from app.ml.schemas import ITEMS as ML_ITEMS

    assert set(ITEMS) <= set(ML_ITEMS)


def test_문_앞에서_막혀_에이전트를_안_부른다():
    """🔴 **이것이 이 판의 요점이다.**

    검증이 스키마에 있으므로 요청 객체가 아예 안 만들어진다.
    `run_procurement` 이 호출될 자리까지 못 간다 — 매입도, 세 부서도 안 돈다.
    """
    with pytest.raises(ValidationError):
        _request(item="피마늘")
