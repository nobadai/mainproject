"""매출액이 **전선에서 재무가 읽는 이름으로** 나가는가 (2026-09-11).

🔴 **판매가 내는 이름과 재무가 읽는 이름이 달랐다.**

```text
판매가 내던 이름   `sales_amount_krw`
재무가 읽는 이름   `reported_sales_amount_krw`   (REQUIRED_SALES_INPUT_FIELDS)
```

★★ **이름을 통일하면 안 된다.** 재무는 이 값을 **믿지 않고 다시 세서 맞대 본다.**

```text
reported_sales_amount_krw       판매가 **보고한** 금액
recalculated_sales_amount_krw   재무가 **다시 센** 금액
compare_reported_sales_amount(reported, recalculated)   허용 오차 **없음**
```

  두 항의 이름이 같아지면 **검사가 무슨 둘을 맞대는지 읽을 수 없다.**

★ 그렇다고 판매 안쪽 이름을 바꿀 수도 없다 — 판매는 **제안하는 것**이지 보고하는
  것이 아니고, 자기 코드에서 `reported_` 는 틀린 말이다.

🔴 **그래서 마스터가 바꾸지 않는다.** 마스터는 안을 그대로 나르고 이름의 주인은
   내는 쪽이다. 여기서 안 실으면 마스터가 **두 파트 사이의 번역기**가 되고,
   그 계층(`interop.py`)은 2026-08-29 에 지운 것이다.
"""

from __future__ import annotations

from app.finance.capabilities.sales import REQUIRED_SALES_INPUT_FIELDS
from app.sales.adapter import _proposal_payload
from app.sales.proposal import run_proposal
from tests.sales.test_sales_proposal import _request

_전선이름 = "reported_sales_amount_krw"
_안쪽이름 = "sales_amount_krw"


def _안하나(payload) -> dict:
    안들 = payload.get("scenarios") or []
    assert 안들, "이 검사의 전제가 깨졌다 — 안이 하나도 안 섰다"
    return 안들[0]


def test_봉투에_실릴_때_재무가_읽는_이름으로_나간다() -> None:
    """🔴 **이것이 재무 판정을 막던 자리다.**"""
    안 = _안하나(_proposal_payload(run_proposal(_request(business_mode="SPOT_SALES"))))

    assert _전선이름 in 안, f"전선에 '{_전선이름}' 이 없다 — 재무가 못 읽는다"
    assert 안[_전선이름] is not None


def test_판매_안쪽_이름은_안_바뀐다() -> None:
    """★ 판매는 **제안**하는 것이지 보고하는 것이 아니다."""
    reply = run_proposal(_request(business_mode="SPOT_SALES"))

    assert reply.scenarios[0].sales_amount_krw is not None

    안 = _안하나(reply.model_dump(mode="json"))
    assert _안쪽이름 in 안, "별칭 없는 덤프에서 본래 이름이 사라졌다"
    assert _전선이름 not in 안, "안쪽 덤프에까지 전선 이름이 샜다 — 한 사실에 두 이름이 된다"


def test_전선_이름이_재무가_요구하는_목록에_실제로_있다() -> None:
    """★★ **이름을 손으로 적어 맞추지 않는다.**

    재무가 그 목록을 바꾸면 이 검사가 먼저 빨개진다 — 걷기 113건이 조용히
    멈추는 것보다 낫다.
    """
    assert _전선이름 in REQUIRED_SALES_INPUT_FIELDS


def test_재무가_요구하는_다른_칸도_같은_이름으로_나간다() -> None:
    """⚠️ 금액 하나만 맞춰 놓고 나머지가 어긋나면 **한 걸음 더 가서 같은 자리에 선다.**"""
    안 = _안하나(_proposal_payload(run_proposal(_request(business_mode="SPOT_SALES"))))

    어긋난것 = [
        칸 for 칸 in REQUIRED_SALES_INPUT_FIELDS if 칸 not in 안 and 칸 != "source_ref"
    ]

    assert not 어긋난것, f"재무가 요구하는데 전선에 없는 칸: {어긋난것}"
