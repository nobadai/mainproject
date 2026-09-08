"""Finance 내부에서 공유하는 작은 결정론 helper."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any


def decimal_value(value: Any) -> Decimal:
    """Finance 숫자 입력을 Decimal 로 정규화한다."""
    if isinstance(value, bool):
        raise TypeError("boolean values are not valid numeric inputs")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("float is not an accepted business numeric input")
    return Decimal(str(value))


def row_value(row: Any, name: str, index: int = 0) -> Any:
    """dict row 와 tuple row 를 같은 방식으로 읽는다."""
    if isinstance(row, Mapping):
        return row[name]
    return row[index]
