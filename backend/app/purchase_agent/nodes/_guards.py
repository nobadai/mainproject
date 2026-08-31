"""노드가 공유하는 입력 가드.

**조용히 넘어가지 않는다.** 0으로 나누거나 빈 목록에서 max를 취하면 파이썬이 알아서
터지긴 하지만, 그 메시지로는 어느 입력이 왜 잘못됐는지 알 수 없다. 여기서 먼저 막아
"무엇이 없어서 계산을 못 했는가"를 남긴다 — 미결값이 계산을 막아야 한다는 규칙 3과 같은 정신이다.
"""

from decimal import Decimal
from math import isfinite
from typing import Any


def require_positive[Number: (int, float)](value: Number | None, name: str) -> Number:
    """0·음수·None을 거른다. 나눗셈의 분모와 단가처럼 0이면 뜻이 무너지는 값에 쓴다."""
    if value is None:
        raise ValueError(f"{name} is missing (None) — 계산을 진행할 수 없다")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return value


def require_non_empty(items: list, name: str) -> list:
    """빈 목록을 거른다. ``max()``·``[0]`` 앞에 둔다."""
    if not items:
        raise ValueError(f"{name} is empty — 계산을 진행할 수 없다")
    return items


def require_capacity_kg(value: Any, name: str) -> float:
    """수용량(kg). **확정된 0은 통과시키고, 수가 아닌 값은 여기서 세운다.**

    ``require_positive``와 나눠 두는 이유는 ``0``의 뜻이 반대이기 때문이다. 단가·분모의
    0은 값이 잘못 온 것이지만, 창고 여유 0kg·임차한도 0kg은 **확정된 0**이다
    (``rental_cap_kg``는 2026-08-27 물류 회신 §1로 0 확정 — 규칙 3).

    ⚠️ ``bool``을 먼저 막는다. ``True``가 1kg으로 통과하면 창고 상한이 1kg이 되어 **전
    안이 창고에 눌려 죽는데 에러가 없어 원인이 안 보인다** (``_positive_int``·
    ``schemas._reject_boolean``과 같은 이유).

    ⚠️ ``Decimal``도 받아 float으로 맞춘다. 물류는 float으로 보내지만(``adapter._num``)
    다른 출처가 Decimal이면 ``Decimal + float``이 더하는 자리에서 ``TypeError``로 죽고,
    **어느 키가 문제인지 남지 않는다**.

    ⚠️ 음수를 막는 것은 조용히 틀리기 때문이다. 상한이 음수면 수량이 음수로 클립되는데
    (실측 ``-500``) 그건 "살 수 없다"가 아니라 **말이 안 되는 수**다.

    수신 payload는 여기까지 오기 전에 ``adapter.validate_payload``가 같은 검사를 하고
    ``missing_data``로 사유를 낸다. 이 가드가 실제로 터지는 자리는 payload가 아닌
    경로(mock·직접 호출)이고, 거기서 잘못된 값은 조용히 흘리는 것보다 세우는 쪽이 맞다.

    타입 어긋남만 ``TypeError``다 — 나머지(무한대·음수)는 타입은 맞고 값이 틀린 것이라
    ``ValueError``로 나눈다.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"{name} must be a number in kg, got {value!r}")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if number < 0:
        raise ValueError(f"{name} must not be negative, got {value!r}")
    return number
