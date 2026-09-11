"""매출액이 **별칭으로 나갔다가 되읽히는가** — 2026-09-11.

`#575` 가 나가는 이름을 재무가 읽는 이름으로 바꿨다. **그런데 나가는 쪽만 고쳤다.**

```text
① app/sales/schemas.py    sales_amount_krw 에 serialization_alias
② adapter._proposal_payload 가 model_dump(by_alias=True)
   ★ 그 전선은 재무만이 아니라 **판매 → 마스터** 이기도 했다
③ 마스터가 그 별칭 형태 **그대로** 실행 이력에 적는다
④ 승인이 SalesScenario.model_validate 로 되읽는다
   → extra="forbid" → ValidationError → 확정이 못 선다
```

🔴 **실측된 피해** (2026-09-11 · 걷기).

```text
재검증   PASSED 7건   🟢
확정     sales 0행    🔴  ValidationError: reported_sales_amount_krw
                          Extra inputs are not permitted
```

★★ **`#575` 는 나가는 쪽만 쟀다.** 이 파일이 더하는 것은 **돌아오는 길**이다 —
  나가는 이름을 바꿀 때는 그 이름으로 다시 읽히는지를 같이 재야 한다.

🔴 **이 파일이 잡으려는 셋.**

```text
① by_alias=True 로 덤프한 것을 되읽으면 금액이 살아 있다   ← 그날 터진 자리
② by_alias=False 로 덤프한 것도 되읽힌다                   ← 옛 이름을 잃지 않는다
③ serialization_alias 가 붙은 칸이 **전부** 왕복한다        ← 다음 칸도 같은 병이다
```

★ ③ 은 **쓸어서 센다.** 손으로 나열하면 다음 사람이 칸을 하나 더 달 때 이 파일이
  조용히 통과한다. 🔴 **센 것이 0이면 실패**시킨다 — 쓸개가 고장 나면 빈 통과가 된다.

★ **DB 를 안 탄다.** 모델 하나를 세워 덤프하고 되읽는 것이 전부다.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from typing import Any

import pytest
from pydantic import AliasChoices, BaseModel

from app.sales import schemas as 판매스키마
from app.sales.schemas import SalesScenario

_전선이름 = "reported_sales_amount_krw"
_안쪽이름 = "sales_amount_krw"

#: 실측에 있던 값 그대로. 🔴 **소수점을 지운 값으로 바꾸지 않는다** — `Decimal` 왕복이
#:   문자열을 거치므로 그 자리가 검사 대상이다.
_금액 = "193125.00"


def _안() -> SalesScenario:
    """최소 모양의 판매안 하나. **금액이 실려 있다.**"""
    return SalesScenario(
        scenario_id="SALES-001-A-R1",
        scenario_type="BALANCED",
        objective="BALANCE",
        business_mode="SPOT_SALES",
        item="배추",
        quantity_kg=Decimal(2000),
        unit_price_krw=Decimal(1200),
        sales_amount_krw=Decimal(_금액),
        supply={},  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# ① 🔴 전선 이름으로 나간 것이 되읽힌다 — 그날 터진 자리
# ---------------------------------------------------------------------------


def test_전선_이름으로_덤프한_것이_되읽힌다() -> None:
    """🔴 **이것이 `sales` 를 0행으로 만든 자리다.**

    `sales_approval._confirmation_input` 이 실행 이력에 적힌 안을
    `SalesScenario.model_validate` 로 되읽는데, 그 안은 `by_alias=True` 로 나간
    모양이다. `validation_alias` 가 없으면 `extra="forbid"` 가 그 자리에서 막는다.
    """
    나간것 = _안().model_dump(mode="json", by_alias=True)

    assert _전선이름 in 나간것, "전선 이름으로 안 나갔다 — 이 검사의 전제가 깨졌다"

    되읽은것 = SalesScenario.model_validate(나간것)

    assert 되읽은것.sales_amount_krw == Decimal(_금액), (
        f"전선 이름으로 나간 금액이 되읽을 때 사라졌다: {되읽은것.sales_amount_krw!r}"
    )


def test_안쪽_이름으로_덤프한_것도_되읽힌다() -> None:
    """★ **옛 이름을 잃지 않는다.** 별칭을 다는 것이 본래 이름을 밀어내면 안 된다.

    🔴 `AliasChoices` 에서 본래 이름을 빼면 별칭 없는 덤프가 못 돌아온다 — 그 모양이
      저장소 안에 아직 많다 (`model_dump()` 기본이 `by_alias=False` 다).
    """
    나간것 = _안().model_dump(mode="json", by_alias=False)

    assert _안쪽이름 in 나간것, "안쪽 이름으로 안 나갔다 — 이 검사의 전제가 깨졌다"

    되읽은것 = SalesScenario.model_validate(나간것)

    assert 되읽은것.sales_amount_krw == Decimal(_금액)


def test_두_경로의_결과가_같다() -> None:
    """★★ **한 사실이므로 어느 길로 왕복해도 같은 안이 나와야 한다.**

    ⚠️ 금액만 비교하면 *"금액은 살았는데 다른 칸이 밀렸다"* 를 못 본다 — 모델 전체를
      맞댄다.
    """
    안 = _안()
    전선왕복 = SalesScenario.model_validate(안.model_dump(mode="json", by_alias=True))
    안쪽왕복 = SalesScenario.model_validate(안.model_dump(mode="json", by_alias=False))

    assert 전선왕복 == 안쪽왕복, "같은 안이 두 경로에서 다르게 돌아왔다"
    assert 전선왕복 == 안, "왕복이 원본과 다른 안을 냈다"


def test_모르는_칸은_여전히_막힌다() -> None:
    """🔴 **`extra="forbid"` 를 풀어서 고치지 않았다.**

    푸는 것으로 고치면 오타가 조용히 통과한다 — 그것은 다른 병을 들여오는 것이다.
    `validation_alias` 는 **그 이름을 아는 칸으로 만들 뿐** 금지를 풀지 않는다.
    """
    나간것 = _안().model_dump(mode="json", by_alias=True)
    나간것["reported_sales_amount_krw_oops"] = "1"

    with pytest.raises(Exception) as 터진것:
        SalesScenario.model_validate(나간것)

    assert "extra_forbidden" in str(터진것.value), f"모르는 칸이 조용히 통과했다: {터진것.value}"


# ---------------------------------------------------------------------------
# ② 🔴 쓸어서 센다 — 다음 칸도 같은 병이다
# ---------------------------------------------------------------------------


def _별칭_붙은_칸() -> list[tuple[str, str, str, Any]]:
    """`app/sales/schemas.py` 안에서 `serialization_alias` 가 붙은 칸 전부.

    ```text
    (모델 이름, 칸 이름, 전선 이름, validation_alias)
    ```

    ★ **손으로 나열하지 않는다.** 다음 사람이 칸을 하나 더 달면 그 칸이 여기 저절로
      잡히고, 왕복을 안 열었으면 아래 검사가 그 자리에서 빨개진다.
    """
    out: list[tuple[str, str, str, Any]] = []
    for 이름, 물건 in inspect.getmembers(판매스키마, inspect.isclass):
        if not issubclass(물건, BaseModel) or 물건.__module__ != 판매스키마.__name__:
            continue
        for 칸이름, 칸 in 물건.model_fields.items():
            if 칸.serialization_alias is not None:
                out.append((이름, 칸이름, 칸.serialization_alias, 칸.validation_alias))
    return out


def test_쓸개가_실제로_칸을_찾는다() -> None:
    """🔴 **먼저 이것부터.** 0건을 세면 아래 검사가 전부 공짜로 초록이 된다.

    `#320` 의 변이가 안 울었던 이유가 정확히 그 모양이었다 — 재는 줄은 있는데 재는
    대상이 없었다.
    """
    잡힌것 = _별칭_붙은_칸()

    assert 잡힌것, (
        "app/sales/schemas.py 에서 serialization_alias 가 붙은 칸을 하나도 못 찾았다 "
        "— 쓸개가 고장 났다"
    )
    assert (
        "SalesScenario",
        _안쪽이름,
        _전선이름,
    ) in [(모델, 칸, 전선) for 모델, 칸, 전선, _v in 잡힌것], (
        f"알고 있는 그 칸이 안 잡혔다: {잡힌것}"
    )


def test_전선_이름이_붙은_칸은_전부_그_이름으로_되읽힌다() -> None:
    """🔴 **하나라도 `validation_alias` 가 없으면 같은 병이다.**

    나가는 이름만 바꾸면 그 모양으로 저장된 것이 돌아올 길이 없다 — 그리고 그것은
    `extra="forbid"` 때문에 **조용히 틀리지 않고 터진다.** 터진 자리가 예외를 값으로
    바꾸는 곳(`confirm_approved_sale`)이면 화면에는 `BLOCKED` 하나만 남는다.

    ★ **본래 이름도 같이 요구한다.** 전선 이름만 받게 하면 별칭 없는 덤프가 못
      돌아오고, 그 모양이 저장소 안에 아직 많다.
    """
    샌것: list[str] = []
    for 모델, 칸, 전선, 받는이름 in _별칭_붙은_칸():
        받는것 = (
            set(받는이름.choices)
            if isinstance(받는이름, AliasChoices)
            else {받는이름}
            if 받는이름 is not None
            else set()
        )
        빠진것 = {칸, 전선} - 받는것
        if 빠진것:
            샌것.append(f"{모델}.{칸}(전선 '{전선}' · 못 받는 이름 {sorted(빠진것)})")

    assert not 샌것, "나가는 이름만 바꾸고 돌아오는 길을 안 연 칸이 있다: " + ", ".join(샌것)
