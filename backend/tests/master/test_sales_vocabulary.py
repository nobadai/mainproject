"""판매 사이클 어휘 — 봉투에 판매가 들어왔다는 것만 고정한다 (판매 2026-09-06 통보).

★ **어휘 확장 3건이 전부다.** 판매 Flow·어댑터·라우팅은 없다.
  그래서 이 파일은 *"판매 사이클이 돈다"* 를 시험하지 않는다. 시험하는 것은

  ```text
  ① 판매가 부를 수 있는 대상이 됐다        AgentName + _AGENT_MODES
  ② 판매 경계·판매 제안이 매입 것과 갈린다  PRE_SALES · GENERATE_SALES_PROPOSAL
  ③ 늘어난 어휘가 검사를 무르게 하지 않았다  등록 안 된 조합은 그대로 거부
  ```

🔴 **③ 이 이 파일의 이유다.** 닫힌 집합에 값을 넣는 변경은 *"안 걸리던 것이 걸리게"*
  가 아니라 **"걸리던 것이 안 걸리게"** 만드는 쪽으로 틀린다. 판매를 열면서
  `sales / PRE_PURCHASE` 까지 같이 열려도 정상 경로는 전부 초록이라 아무도 모른다.
"""

from __future__ import annotations

from typing import get_args

import pytest

from app.contracts.core import ContractViolation, SuggestedAdjustment
from app.master.envelope import (
    AgentName,
    Mode,
    agent_allowed_modes,
    agent_dept,
    validate_reply,
)
from tests.master.test_envelope import reply, req

# ── ① 판매가 부를 수 있는 대상이 됐다 ───────────────────────────────────────


def test_판매는_제안_생성을_받는다():
    """판매 사이클에서 시나리오를 만드는 쪽이 판매다 — 매입 사이클의 매입 자리."""
    assert req(agent="sales", mode="GENERATE_SALES_PROPOSAL").agent == "sales"


def test_판매도_상태_조회는_받는다():
    assert req(agent="sales", mode="STATUS_QUERY").mode == "STATUS_QUERY"


def test_모든_에이전트가_모드_표에_등록돼_있다():
    """🔴 빠지면 `ContractViolation` 이 아니라 `KeyError` 다.

    `AgentRequest.__post_init__` 이 `_AGENT_MODES[self.agent]` 로 읽으므로, 이름만
    늘리고 표를 안 늘리면 **계약 위반이 아니라 마스터가 터진다.** 사유 문장도 없다.
    """
    for agent in get_args(AgentName):
        assert agent_allowed_modes(agent), f"{agent} 가 모드 표에 없다"


def test_모드_표에_오타난_모드가_없다():
    """표는 `Mode` 어휘 안에서만 고른다 — 손으로 쓰는 문자열이라 오타가 조용히 산다."""
    known = set(get_args(Mode))
    for agent in get_args(AgentName):
        assert agent_allowed_modes(agent) <= known, agent


def test_아무도_안_받는_모드는_없다():
    """어휘만 늘고 받는 쪽이 없으면 **부를 수 없는 mode** 다 — 죽은 어휘다.

    ★ `AgentName` 과 `_AGENT_MODES` 중 **한쪽만 늘어난 날**을 잡는 자리이기도 하다.
      `Literal` 은 런타임에 강제되지 않아서, 이름만 빠져도 호출은 그대로 통한다.
    """
    reachable: set[str] = set()
    for agent in get_args(AgentName):
        reachable |= agent_allowed_modes(agent)

    assert set(get_args(Mode)) - reachable == set()


# ── ② 판매 경계·판매 제안이 매입 것과 갈린다 ────────────────────────────────


def test_판매_경계는_물류가_낸다():
    """`PRE_PURCHASE` 가 매입 경계이듯 `PRE_SALES` 가 판매 경계다."""
    assert "PRE_SALES" in agent_allowed_modes("inventory")


def test_판매_경계는_물류만_낸다():
    """재무·매입·판매에 열면 없는 책임을 만든다 — `SALES_VALIDATION` 과 같은 결."""
    for agent in ("finance", "purchase", "sales"):
        assert "PRE_SALES" not in agent_allowed_modes(agent), agent


def test_매입_시나리오_생성과_판매_제안_생성이_갈려_있다():
    """합치면 `(agent, mode, call_seq)` 로 둘을 구분할 수 없고 payload 를 보고 추측하게 된다."""
    assert "GENERATE_SCENARIOS" in agent_allowed_modes("purchase")
    assert "GENERATE_SCENARIOS" not in agent_allowed_modes("sales")
    assert "GENERATE_SALES_PROPOSAL" in agent_allowed_modes("sales")
    assert "GENERATE_SALES_PROPOSAL" not in agent_allowed_modes("purchase")


def test_판매_제안의_재무_검증은_판매가_받지_않는다():
    """제안자가 자기 제안을 검증하면 검증이 아니다 — `SALES_VALIDATION` 은 재무 것이다."""
    assert "SALES_VALIDATION" in agent_allowed_modes("finance")
    assert "SALES_VALIDATION" not in agent_allowed_modes("sales")


# ── ③ 늘어난 어휘가 검사를 무르게 하지 않았다 ───────────────────────────────


@pytest.mark.parametrize(
    "mode",
    ["PRE_PURCHASE", "PRE_SALES", "SCENARIO_VALIDATION", "SALES_VALIDATION", "GENERATE_SCENARIOS"],
)
def test_판매가_못_받는_모드는_거부된다(mode):
    """판매를 열면서 판매에게 **다 열지는 않았다.**"""
    with pytest.raises(ContractViolation, match=mode):
        req(agent="sales", mode=mode)


def test_다른_에이전트가_판매_제안_생성을_받을_수_없다():
    for agent in ("finance", "inventory", "purchase"):
        with pytest.raises(ContractViolation, match="GENERATE_SALES_PROPOSAL"):
            req(agent=agent, mode="GENERATE_SALES_PROPOSAL")


def test_거부_사유에_받은_값과_허용_집합이_둘_다_있다():
    """무엇이 왔고 무엇이 되는지 — 기존 mode 거부 문장과 같은 모양을 판매에서도 유지한다."""
    with pytest.raises(ContractViolation) as e:
        req(agent="sales", mode="PRE_PURCHASE")

    말 = str(e.value)
    assert "PRE_PURCHASE" in 말, "무엇이 왔는지가 없으면 고칠 값을 모른다"
    assert "GENERATE_SALES_PROPOSAL" in 말, "무엇이 되는지가 없다"


# ── ④ 판매는 조언자가 아니다 ────────────────────────────────────────────────


def test_판매는_부서_어휘로_넘어가지_않는다():
    """🔴 `Dept` 에 `"sales"` 가 있어도 여기서는 `None` 이다.

    `Dept` 의 sales 는 **매입 밴드에 조언을 보태는 쪽**이고, `AgentName` 의 sales 는
    **판매 사이클의 제안자**다. 글자가 같아서 넣기 쉬운데, 넣으면
    `band_is_formed`·`blocking_agents` 가 판매를 조언자로 세어 판매가 답하지 않은 날
    **매입 밴드가 성립하지 않은 것으로 읽힌다** — 없는 의존이 생긴다.
    """
    assert agent_dept("sales") is None
    assert agent_dept("finance") == "finance"


def test_판매는_축_조정을_제안할_수_없다():
    """제안자 ≠ 조언자. 매입과 같은 자리다."""
    adj = SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=1.0,
        unit="KRW",
        reason="r",
        ref_ids=("X",),
    )
    with pytest.raises(ContractViolation, match="제안자"):
        reply(agent="sales", mode="GENERATE_SALES_PROPOSAL", suggested_adjustments=(adj,))


# ── ⑤ 봉투 검증이 판매 회신에도 그대로 돈다 ─────────────────────────────────


def test_판매_회신도_봉투_검증을_통과한다():
    """어휘만 늘리고 검증이 판매에서 깨지면 확장이 아니라 고장이다."""
    request = req(agent="sales", mode="GENERATE_SALES_PROPOSAL")
    r = reply(agent="sales", mode="GENERATE_SALES_PROPOSAL", run_id="SAL-1")

    assert validate_reply(request, r) == ()


def test_판매_회신의_바인딩_불일치는_그대로_잡힌다():
    request = req(agent="sales", mode="GENERATE_SALES_PROPOSAL")
    r = reply(agent="sales", mode="STATUS_QUERY", run_id="SAL-1")

    assert "E-BIND-MODE" in {f.code for f in validate_reply(request, r)}
