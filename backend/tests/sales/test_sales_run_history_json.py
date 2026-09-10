"""판매 실행이력 payload 가 **JSON 으로 실릴 수 있는가** (2026-09-11).

🔴 **걷기 206일에서 123건이 여기서 터졌다** (`SIM-WALK-2026-FULL` 실측 ·
   2026-01-03 ~ 2026-09-05 · 무 41 · 배추 41 · 양파 41).

```text
app/sales/adapter.py:87   request_payload=asdict(request)
  → `AgentRequest.context.as_of` 는 **`date` 객체 그대로**다
  → app/sales/runs.py       Jsonb(...) 가 그것을 못 싣는다
  → TypeError: Object of type date is not JSON serializable
```

★★ 그리고 그 예외가 **`SL2_NO_CANDIDATE`(안을 못 만들었다)** 로 적혔다. 터진 것과
  안이 없는 것은 다르다 — 고치고 나니 진짜 사유가 드러났다
  (`PROPOSAL_QUANTITY_REQUIRED`).

★ 저장소 관례는 이미 `model_dump(mode="json")` 이다 (`app/logistics/service.py:65` ·
  `app/master/cycle_persistence.py:77`). 둘 다 날짜를 ISO 로 편다. 여기만
  `dataclasses.asdict` 라 안 펴졌다.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.sales.runs import _json_safe, _payload


def _싣는다(값: object) -> str:
    """`_payload` 가 만든 `Jsonb` 를 실제로 직렬화한다."""
    실린것 = _payload(값)
    return 실린것.dumps(실린것.obj)


def test_날짜가_ISO_로_펴진다() -> None:
    """🔴 **이것이 123건을 만든 값이다.**"""
    실린 = json.loads(_싣는다({"as_of": date(2026, 1, 3)}))

    assert 실린["as_of"] == "2026-01-03"


def test_시각도_펴진다() -> None:
    실린 = json.loads(_싣는다({"at": datetime(2026, 1, 3, 11, 0, 0, tzinfo=UTC)}))

    assert 실린["at"].startswith("2026-01-03T11:00")


def test_Decimal_과_UUID_도_펴진다() -> None:
    """★ 같은 자리에서 다음에 터질 둘이다 — `Decimal` 은 마스터가 `#175` 에서 만났다."""
    실린 = json.loads(
        _싣는다({"금액": Decimal("1234.56"), "id": UUID("00000000-0000-0000-0000-000000000001")})
    )

    assert 실린["금액"] == "1234.56"
    assert 실린["id"] == "00000000-0000-0000-0000-000000000001"


def test_모르는_것은_뭉개지_않고_터뜨린다() -> None:
    """🔴 **`default=str` 로 통째로 접지 않는다.**

    ★★ 접으면 앞으로 실리는 어떤 타입이든 조용히 문자열이 되고, 그 손실이 **이력에만**
      남아 아무도 안 아프다. 아는 셋만 펴고 나머지는 터뜨린다.
    """

    class 모르는것:
        pass

    with pytest.raises(TypeError) as 터진것:
        _싣는다({"x": 모르는것()})

    assert "모르는것" in unicodedata.normalize("NFC", str(터진것.value))


def test_아는_것만_편다는_말이_실제로_아는_셋이다() -> None:
    """⚠️ 위 검사들이 통과해도 `_json_safe` 가 **아무거나 받으면** 뜻이 없다."""
    with pytest.raises(TypeError):
        _json_safe(object())

    assert _json_safe(date(2026, 1, 3)) == "2026-01-03"
