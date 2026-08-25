"""노드가 공유하는 입력 가드.

**조용히 넘어가지 않는다.** 0으로 나누거나 빈 목록에서 max를 취하면 파이썬이 알아서
터지긴 하지만, 그 메시지로는 어느 입력이 왜 잘못됐는지 알 수 없다. 여기서 먼저 막아
"무엇이 없어서 계산을 못 했는가"를 남긴다 — 미결값이 계산을 막아야 한다는 규칙 3과 같은 정신이다.
"""

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
