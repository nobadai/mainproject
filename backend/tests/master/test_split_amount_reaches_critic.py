"""회차 금액이 매입 payload 에서 `SplitLeg` 까지 **실제로 닿는가** (#295).

🔴 **이 파일이 재는 것은 판정 규칙이 아니라 통로다.**

`#265` 가 `check_triple_identity` 에 split 금액 변을 세웠고 그 판정은 이미
`tests/orchestrator/test_split_amount_identity.py` 가 잰다. 여기서 그것을 다시 재면
안 된다 - 같은 사실의 주인이 둘이 된다.

여기서 재는 것은 **그 함수까지 값이 닿는가** 다. 통로가 셋이다.

```text
① orchestrator/schemas.py   SplitLegIn.amount_krw
② master/critic_bridge.py   스칼라에 실행 품목 이름표를 붙여 나른다
③ critic/service.py         _to_scenario 가 SplitLeg 에 넘긴다
```

★ **`지적 0건` 으로는 통로를 증명할 수 없다.** 통과인지 안 돈 것인지 구분이 안 되고,
  실제로 이 저장소가 그 0건을 *"돌았고 통과"* 로 한 번 오독했다 (2026-09-04).
  그래서 여기서는 **어긋난 값을 넣고 지적이 나오는 것**을 본다.

⚠️ 시작점은 **매입 payload 모양** 이다 (`critic_bridge` 가 받는 dict). 중간에서
  시작하면 끊긴 칸을 건너뛰고도 초록이 된다.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.critic.service import _to_scenario
from app.master import critic_bridge as bridge
from app.master.plan import ExecutionPlan
from app.master.verifier import MasterVerifier, VerificationContext
from app.orchestrator.schemas import SplitLegIn
from tests.master.test_critic_bridge import CONSTRAINTS, EVIDENCES, _proposal, _scenario

AS_OF = date(2025, 12, 31)
ITEM = "배추"

#: `_scenario()` 기준값 - 1,000kg × 1,650원.
TOTAL_QTY_KG = 1000
TOTAL_AMOUNT_KRW = 1_650_000

#: 총액과 어긋난 회차 금액. **65 만원 차이**라 허용 오차(1원)로는 못 덮는다.
WRONG_AMOUNT_KRW = 1_000_000

#: 금액 항등식 위반이 붙어 나오는 자리. `fold()` 가 `CRITIC/{layer}/{check_id}` 로 적는다.
IDENTITY_FINDING = "CRITIC/L1_hard/check_triple_identity"


def _split(amount: int | None) -> list[dict]:
    """매입이 보내는 회차 한 줄. `amount_krw` 는 **스칼라**다 (#269)."""
    leg: dict = {"seq": 1, "date": "2026-01-01", "qty_kg": TOTAL_QTY_KG}
    if amount is not None:
        leg["amount_krw"] = amount
    return [leg]


def _proposal_with(amount: int | None) -> dict:
    return _proposal(_scenario(split_plan=_split(amount)))


def _plan() -> ExecutionPlan:
    return ExecutionPlan(request_id="REQ-T", as_of=AS_OF)


def _ctx() -> VerificationContext:
    return VerificationContext(as_of=AS_OF, item=ITEM, evidences=EVIDENCES)


def _verify(amount: int | None):
    return MasterVerifier()(_proposal_with(amount), CONSTRAINTS, {}, _plan(), _ctx())


def _legs_at_critic(amount: int | None):
    """매입 payload → 통로 셋 → `SplitLeg`. **세 파일을 다 지난 뒤의 값**이다."""
    request = bridge.build_request(
        as_of=AS_OF,
        item=ITEM,
        proposal=_proposal_with(amount),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    return _to_scenario(request.scenarios[0]).split_plan


# ---------------------------------------------------------------------------
# ① 어긋난 값 → 지적이 나온다 (핵심)
# ---------------------------------------------------------------------------


def test_어긋난_회차_금액이_Critic_지적으로_나온다():
    """🔴 **이 파일에서 가장 중요한 검사.**

    회차 금액 1,000,000원 vs 총액 1,650,000원. 매입 payload 에서 시작해 통로 셋을 다
    지나야 이 지적이 나온다 - 한 칸만 끊겨도 값이 `None` 으로 남고 금액 변은 통째로
    건너뛰어져 **조용히 초록**이 된다.

    ⚠️ 마스터 자신의 `L-IDENTITY-*` 는 이것을 못 잡는다 - 수량 변과 `Σ(수량×등급단가)`
      만 보고 **회차 금액은 안 본다.** 그래서 이 지적의 출처가 Critic 이어야 한다.
    """
    result = _verify(WRONG_AMOUNT_KRW)

    hits = [f for f in result.findings if f.startswith(IDENTITY_FINDING)]
    assert hits, result.findings
    assert any("금액 항등식 위반" in f for f in hits), hits
    # 값이 닿았다는 증거 - 지적 문면에 **매입이 보낸 숫자**가 그대로 있다.
    assert any(f"{WRONG_AMOUNT_KRW:,.0f}원" in f for f in hits), hits

    # 안 돈 것을 통과로 읽지 않는다 - Critic 이 실제로 돌았어야 한다.
    assert not any("항등식이 깨져" in s for s in result.skipped), result.skipped
    assert not any("Critic 계약에 맞지 않는다" in s for s in result.skipped), result.skipped


def test_어긋난_값이_SplitLeg_까지_그대로_닿는다():
    """지적의 앞단 - 값 자체가 계약 객체에 실려 있는가."""
    legs = _legs_at_critic(WRONG_AMOUNT_KRW)

    assert legs[0].amount_krw == {ITEM: float(WRONG_AMOUNT_KRW)}


# ---------------------------------------------------------------------------
# ② 맞는 값 → 지적이 없다 (거짓 양성이 아니다)
# ---------------------------------------------------------------------------


def test_맞는_회차_금액에는_지적이_없다():
    """반례가 없으면 ①은 *"항상 운다"* 와 구분이 안 된다."""
    result = _verify(TOTAL_AMOUNT_KRW)

    assert not any(f.startswith(IDENTITY_FINDING) for f in result.findings), result.findings
    assert _legs_at_critic(TOTAL_AMOUNT_KRW)[0].amount_krw == {ITEM: float(TOTAL_AMOUNT_KRW)}


# ---------------------------------------------------------------------------
# ③ 금액이 없으면 지금과 똑같다 (기존 경로 보존)
# ---------------------------------------------------------------------------


def test_금액을_안_실으면_None_으로_남고_아무것도_안_깨진다():
    """⚠️ 선택 필드다. `0` 이나 빈 매핑으로 채우면 **없는 것이 0 원이 되어** 금액 변이
    조용히 통과한다 - 없는 것과 0 원은 다르다."""
    legs = _legs_at_critic(None)

    assert legs[0].amount_krw is None
    # 수량·도착일은 전과 같다 - 이 PR 로 바뀌는 것이 없다.
    assert legs[0].qty_kg == {ITEM: float(TOTAL_QTY_KG)}
    assert legs[0].expected_arrival_date == date(2026, 1, 3)

    result = _verify(None)
    assert not any(f.startswith(IDENTITY_FINDING) for f in result.findings), result.findings
    assert not any("Critic 계약에 맞지 않는다" in s for s in result.skipped), result.skipped


# ---------------------------------------------------------------------------
# ④ 스칼라 → 매핑 변환이 **실행 품목**을 쓴다
# ---------------------------------------------------------------------------


def test_스칼라를_실행_품목으로_이름표_붙여_옮긴다():
    """매입은 회차 금액을 스칼라로 보내고 계약은 품목별 매핑을 요구한다
    (근거는 `contracts.core.SplitLeg.amount_krw`).

    ★ 창작이 아니다 - 실행 하나가 품목 하나라 **키가 하나뿐**이고 값은 매입이 보낸
      그대로다. 품목이 바뀌면 키도 따라 바뀌어야 한다.
    """
    legs = bridge._split_legs(
        {"split_plan": _split(WRONG_AMOUNT_KRW)}, "무", AS_OF, lead=2
    )

    assert legs[0]["amount_krw"] == {"무": float(WRONG_AMOUNT_KRW)}
    assert legs[0]["qty_kg"] == {"무": float(TOTAL_QTY_KG)}


def test_금액이_없으면_이름표도_안_붙인다():
    legs = bridge._split_legs({"split_plan": _split(None)}, ITEM, AS_OF, lead=2)

    assert legs[0]["amount_krw"] is None


# ---------------------------------------------------------------------------
# ⑤ SplitLegIn 이 extra="forbid" 인데 새 필드를 받는다
# ---------------------------------------------------------------------------


def test_SplitLegIn_이_금액_칸을_연다():
    """`extra="forbid"` 라 **칸이 없으면 422 로 막힌다** - 그때 마스터는 그것을
    `findings` 가 아니라 `skipped` 로 적고, 검증이 안 돈 채 하루가 지나간다."""
    leg = SplitLegIn(offset_days=1, qty_kg={ITEM: 1000.0}, amount_krw={ITEM: 1_650_000.0})

    assert leg.amount_krw == {ITEM: 1_650_000.0}
    assert SplitLegIn(offset_days=1, qty_kg={ITEM: 1000.0}).amount_krw is None


def test_모르는_칸은_여전히_막는다():
    """칸을 연 것이 문을 연 것은 아니다 - 오타 필드는 그대로 막혀야 한다."""
    with pytest.raises(ValidationError):
        SplitLegIn(offset_days=1, qty_kg={ITEM: 1000.0}, amount_kwr={ITEM: 1.0})
