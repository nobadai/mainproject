"""되먹임 수신부 — 마스터가 2회차에 싣는 것을 **받는 데까지**.

마스터는 재호출 때 두 칸을 payload 에 싣는다 (``flow.py`` ``_purchase_input``)::

    payload["adjustments"]        [asdict(a) for a in suggested_adjustments]
    payload["feedback_context"]   attempt · reason · findings · verdicts · verdict_reasons

전에는 어댑터가 ``prior_feedback`` 하나만 읽고 **둘 다 버렸다.** 보내는 쪽에서는
안 보냈을 때와 구분되지 않는다 — 이 파일이 잠그는 것이 그 구멍이다.

🔴 **반영은 범위 밖이다.** ``target_value`` 가 *"이 값으로 바꿔라"* 인지 *"이 값을
  넘지 마라"* 인지가 미확정이라 반영 규칙을 만들 수 없다. 그래서 검사도 *"수량이
  바뀌었나"* 를 보지 않고 **"받았고, 받았다고 정직하게 적었나"** 를 본다.
"""

import json
from datetime import date

import pytest

from app.contracts.core import SuggestedAdjustment
from app.master.envelope import AgentRequest, ExecutionContext, validate_reply
from app.master.flow import ProcurementFlow
from app.master.ports import AgentRegistry
from app.master.runner import MasterRunner
from app.purchase_agent import ports
from app.purchase_agent.adapter import build_state, purchase_port

AS_OF = date(2026, 9, 11)  # stable 앵커 — 3안이 다 서는 날이라 risks 를 안별로 볼 수 있다
ITEM = "양파"

#: 부서가 내는 표준형. **dataclass 그대로** 둔다 — 전선용 dict 는 마스터가 만든다.
_ADJUSTMENT_OBJECT = SuggestedAdjustment(
    dept="inventory",
    axis="quantity",
    target_value=900.0,
    unit="kg",
    reason="기본 시나리오 2026-09-11 회차 — 수량을 900kg 로 조정 제안",
    ref_ids=("INV-CAP-0911",),
    scenario_labels=("보수", "기본"),
    split_date=date(2026, 9, 11),
)

FEEDBACK_CONTEXT = {
    "attempt": 2,
    "reason": "물류 conditional 1건",
    "findings": ["창고 여유가 회차 수량에 못 미친다"],
    "verdicts": {"inventory": "conditional"},
    "verdict_reasons": {"inventory": "2026-09-11 도착일에 여유가 부족하다"},
}


def _master_payload() -> dict:
    """🔴 **마스터가 실제로 보내는 payload 를 마스터에게 만들게 한다** (2026-09-03).

    전에는 이 파일이 dict 를 **손으로 조립**했다. 주석에는 *"마스터가 보내는 그대로를
    쓴다"* 고 적혀 있었는데, 손으로 적은 순간 그게 아니게 된다 — 그리고 실제로 어긋났다::

        2026-09-03  마스터가 `asdict` → `_wire` 로 바꿨다 (#175 · 커밋 8ac244e)
                      ref_ids · scenario_labels   튜플 → 목록
                      split_date                  date 객체 → ISO 문자열

      우리 검사는 **그대로 통과했다.** 우리 어댑터가 ``dict(item)`` 으로 모양을 안 가려서
      받기는 계속 됐지만, 픽스처가 이미 없는 모양을 잠그고 있었다. 마스터 쪽 계약이
      바뀐 것을 우리 쪽에서는 아무도 못 봤다.

    ★ **``_wire`` 만 부르지 않는다.** 그건 조정안 한 칸이고, 여기가 잠그려는 것은
      *"매입에게 가는 payload 전체"* 다 — ``prior_feedback`` 과 ``feedback_context`` 가
      어느 칸에 어떤 모양으로 실리는지까지 이 함수가 정한다.

    ⚠️ ``ProcurementFlow._purchase_input`` 은 비공개다. 그래도 부르는 편이 낫다 —
      이름이 바뀌면 **import 가 즉시 터져** 우리가 알게 된다. 손으로 조립하면 아무 일도
      일어나지 않고 조용히 어긋난다. 위 2026-09-03 이 그 실례다.
    """
    extras = ports.get_snapshot_extras(ITEM, AS_OF)
    runner = MasterRunner(
        ExecutionContext(f"REQ-{AS_OF.isoformat()}-{ITEM}", AS_OF, "ML_COMPLETE", "v2.3"),
        AgentRegistry(),  # `_purchase_input` 은 부서를 부르지 않는다 — 조립만 한다
    )
    flow = ProcurementFlow(
        runner,
        item=ITEM,
        forecast=ports.get_forecast(ITEM, AS_OF),
        confirmed_orders=ports.get_confirmed_orders(ITEM, AS_OF, days=14),
        policy_values={
            "contract_price_krw": extras["contract_price"],
            "item_mix_ratio": extras["item_mix_ratio"],
        },
    )
    flow.suggested_adjustments = [_ADJUSTMENT_OBJECT]
    return flow._purchase_input(
        {"finance": {}, "inventory": {}},  # 제약은 아래 `_payload` 가 실물로 덮는다
        FEEDBACK_CONTEXT,
    )


#: 마스터가 전선에 실은 조정안 한 건. **이 파일 어디에도 손으로 적은 dict 가 없다.**
ADJUSTMENT = _master_payload()["adjustments"][0]


def _payload(**over) -> dict:
    extras = ports.get_snapshot_extras(ITEM, AS_OF)
    payload = {
        "item": ITEM,
        "constraints": {
            "finance": {
                "base_projected_cash_min": ports.get_projected_cash_min(AS_OF, 30),
                "margin_defense_floor_rate": 0.267,
                "finance_cap_amount_krw": 9_000_000,
                "purchase_payment_days": 7,
                "critical_payment_dates": [],
            },
            "inventory": ports.get_inventory(ITEM, AS_OF),
        },
        "forecast": ports.get_forecast(ITEM, AS_OF),
        "confirmed_orders": ports.get_confirmed_orders(ITEM, AS_OF, days=14),
        "policy_values": {
            "contract_price_krw": extras["contract_price"],
            "item_mix_ratio": extras["item_mix_ratio"],
        },
    }
    payload.update(over)
    return payload


def _request(**over) -> AgentRequest:
    return AgentRequest(
        context=ExecutionContext(f"REQ-{AS_OF.isoformat()}-{ITEM}", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=_payload(**over),
    )


def _proposal(**over) -> dict:
    reply, _ = purchase_port(_request(**over))
    return reply.payload


# ── 픽스처가 실제 모양인가 ────────────────────────────────────────────────


def test_the_fixture_is_what_the_master_actually_sends() -> None:
    """🔴 **픽스처가 마스터 출력에서 나왔는지** 잠근다.

    손으로 적은 dict 로 되돌리면 이 검사가 운다 — ``_master_payload()`` 를 다시 부르므로
    픽스처와 실제 출력이 같은 함수에서 나온다.
    """
    assert ADJUSTMENT == _master_payload()["adjustments"][0]
    assert ADJUSTMENT["dept"] == _ADJUSTMENT_OBJECT.dept
    assert ADJUSTMENT["reason"] == _ADJUSTMENT_OBJECT.reason


def test_what_the_master_sends_survives_a_json_round_trip() -> None:
    """🔴 **전선에 실리는 모양이다** — 마스터가 자기 쪽에서 잠근 성질을 여기서도 본다.

    ``asdict`` 는 ``date`` 를 그대로 두고 튜플도 그대로 둔다. 그 dict 는
    ``json.dumps`` 에서 **죽거나**(*"Object of type date is not JSON serializable"*),
    한 번 왕복하면 **튜플이 목록으로 바뀐다.** 같은 칸이 경로에 따라 두 모양이 되면
    받는 쪽의 ``== [...]`` 비교가 **in-process 에서만** 통과한다 (#175 · 커밋 8ac244e).

    ★ 칸마다 세지 않고 **성질 하나로** 잠근다. 다음에 ``date`` 칸이 하나 더 생겨도
      같은 병에 안 걸린다 — 마스터 ``test_전선에_실은_것은_왕복해도_같다`` 와 같은 기준이다.

    ⚠️ 이 검사는 **마스터를 감시하려는 것이 아니다.** 우리 픽스처가 실제 전선 모양인지를
      본다 — 손으로 적은 값이 슬며시 들어오면 여기서 걸린다.
    """
    payload = _master_payload()
    for key in ("adjustments", "feedback_context", "prior_feedback"):
        value = payload.get(key)
        if value is None:
            continue
        assert json.loads(json.dumps(value)) == value, f"{key} 가 JSON 왕복에서 달라진다"


def test_the_wire_has_no_python_only_types() -> None:
    """``date`` 객체·튜플이 남아 있으면 전선에 못 싣는다 — 어긋난 칸을 이름으로 짚는다.

    위 왕복 검사가 성질을 보는 것과 달리 이건 **어디가 문제인지**를 말한다. 왕복만
    있으면 실패 메시지가 *"달라진다"* 뿐이라 어느 칸인지 찾아 들어가야 한다.
    """
    for key, value in ADJUSTMENT.items():
        assert not isinstance(value, tuple), f"{key} 가 튜플이다 — JSON 왕복에서 목록이 된다"
        assert not isinstance(value, date), f"{key} 가 date 객체다 — JSON 직렬화에서 죽는다"


# ── 받는다 ────────────────────────────────────────────────────────────────


def test_adjustments_land_in_state() -> None:
    """조정안이 오면 State 에 **그대로** 실린다.

    ★ 변이 감지 자리다 — ``build_state`` 의 수신 두 줄을 지우면 여기서 운다.
    """
    state = build_state(_request(adjustments=[ADJUSTMENT], feedback_context=FEEDBACK_CONTEXT))
    assert state["adjustments"] == [ADJUSTMENT]
    assert state["feedback_context"] == FEEDBACK_CONTEXT


def test_first_attempt_runs_with_empty_slots() -> None:
    """1회차는 두 칸이 안 온다 — **빈 채로 돌아야** 949건 회귀가 산다."""
    state = build_state(_request())
    assert state["adjustments"] == []
    assert state["feedback_context"] is None

    proposal = _proposal()
    assert proposal["scenarios"], "1회차가 조정안 없이도 안을 내야 한다"
    assert proposal["meta"]["received_adjustments"] == 0


def test_mock_path_does_not_carry_the_slots() -> None:
    """mock 경로(``build_initial_state``)는 두 칸을 **안 채운다**.

    재무 수신값 넷과 같은 선례다 — 어댑터를 안 거치는 949건이 그대로 도는 근거이고,
    ``.get()`` 이 None 을 돌려주면 노드가 종전 경로로 간다.
    """
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state(item=ITEM, as_of=AS_OF)
    assert state.get("adjustments") is None
    assert state.get("feedback_context") is None


# ── 섞이지 않는다 ─────────────────────────────────────────────────────────


def test_prior_feedback_and_adjustments_are_independent_slots() -> None:
    """🔴 두 칸이 **서로를 안 건드린다** (되먹임 계약 v0.2 §2).

    수명·모양·권위가 달라 나눈 것이므로, 한쪽만 와도 다른 쪽이 흔들리면 안 된다.
    v0.1 이 한 슬롯에 ``source`` 로 갈랐다가 타입이 값에 딸려 가 계약이 관례가 됐다.
    """
    user_only = build_state(_request(prior_feedback={"note": "이번 주는 보수적으로"}))
    assert user_only["feedback"] == {"note": "이번 주는 보수적으로"}
    assert user_only["adjustments"] == []

    dept_only = build_state(_request(adjustments=[ADJUSTMENT]))
    assert dept_only["feedback"] is None
    assert dept_only["adjustments"] == [ADJUSTMENT]

    both = build_state(
        _request(prior_feedback={"note": "이번 주는 보수적으로"}, adjustments=[ADJUSTMENT])
    )
    assert both["feedback"] == {"note": "이번 주는 보수적으로"}
    assert both["adjustments"] == [ADJUSTMENT]


def test_feedback_attempt_comes_from_the_refeed_slot() -> None:
    """🔴 **회차는 ``feedback_context["attempt"]`` 가 말한다** (#178 확정 2026-09-03).

    ``attempt`` 라는 이름이 두 슬롯에 있었다. 슬롯을 둘로 나누면서(계약 v0.2 §2)
    **안의 키 이름을 안 갈랐던** 탓이다. 마스터가 나중에 온 자기 쪽을 양보해
    ``condition_seq`` 로 바꿨다::

        prior_feedback["condition_seq"]   사람이 조건을 건 회차
        feedback_context["attempt"]       매입 재호출 회차   ← 이쪽이 ``attempt`` 를 가진다

    ``attempt`` 를 되먹임 쪽에 남긴 근거는 **우리 것**이다 — ``constraints.yaml`` 의
    ``feedback.attempt_max``(= ``MAX_PURCHASE_ATTEMPTS`` 인용)가 세는 것이 그쪽이라
    옮기면 더 헷갈린다.

    ⚠️ **실제 값을 기대한다.** 전에는 넷 다 ``== 0`` 이라, 죽은 슬롯을 읽어도 검사가
      통과했다 — 실측(12-31 피마늘 2회차)에서 부딪히고서야 드러났다.
    """
    proposal = _proposal(adjustments=[ADJUSTMENT], feedback_context=FEEDBACK_CONTEXT)
    assert FEEDBACK_CONTEXT["attempt"] == 2, "픽스처가 2회차여야 이 검사가 성립한다"
    assert proposal["meta"]["feedback_attempt"] == 2
    assert proposal["meta"]["received_adjustments"] == 1


def test_a_refeed_round_says_it_is_a_refeed() -> None:
    """🔴 **``is_refeed`` 도 같은 슬롯을 잘못 보고 있었다.**

    ``bool(prior_feedback)`` 만 보면 순수 되먹임 2회차가 ``False`` 로 나간다 —
    바로 옆 ``feedback_attempt`` 가 2 인데 재호출이 아니라는, **서로를 부정하는 meta**
    가 된다. 사람 조건이든 조언자 판정이든 *"다시 먹인 실행"* 인 것은 같다.
    """
    refeed_only = _proposal(feedback_context=FEEDBACK_CONTEXT)
    assert refeed_only["meta"]["is_refeed"] is True
    assert refeed_only["meta"]["feedback_attempt"] == 2

    condition_only = _proposal(prior_feedback={"condition_text": "예산을 낮춰서"})
    assert condition_only["meta"]["is_refeed"] is True, "사람 조건도 되먹임이다 (기존 동작)"
    assert condition_only["meta"]["feedback_attempt"] == 0, "회차는 되먹임 슬롯 소유다"


def test_the_first_round_is_not_a_refeed() -> None:
    """1회차는 둘 다 안 오므로 ``False`` · 0 이다 — 회귀."""
    proposal = _proposal()
    assert proposal["meta"]["is_refeed"] is False
    assert proposal["meta"]["feedback_attempt"] == 0


def test_condition_seq_is_not_read_as_attempt() -> None:
    """🔴 사용자 조건 슬롯의 회차를 ``feedback_attempt`` 로 싣지 않는다.

    개명 전에는 그 자리에 ``decision_seq`` 가 실렸다. **다른 개념을 싣던 것이 멈추는
    것**이라 의도한 방향이다 (#178). 여기서 다시 읽으면 두 개념이 한 칸으로 합쳐진다.
    """
    proposal = _proposal(prior_feedback={"condition_text": "예산을 낮춰서", "condition_seq": 3})
    assert proposal["meta"]["feedback_attempt"] == 0, "condition_seq 를 회차로 읽으면 안 된다"


# ── 받았다고 적는다 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("count", [1, 3])
def test_received_count_reaches_meta(count: int) -> None:
    """건수가 ⑦ meta 까지 간다. **건수만** 간다 — 반영 여부는 안 적는다."""
    proposal = _proposal(adjustments=[ADJUSTMENT] * count)
    assert proposal["meta"]["received_adjustments"] == count
    # 🔴 **기대를 뜻에 맞췄다** (2026-09-09 · E3-6). 전에는 *"반영 안 했으므로 이름도
    #   없어야 한다"* 였는데 반영이 붙어 칸이 생겼다. 잡으려던 사실은 그대로다 —
    #   **닿은 수와 쓴 수를 뭉치지 않는다.** 이 픽스처는 물류 ``quantity`` 축이라
    #   지금 선언(``amount`` 하나)에 없어 한 건도 안 쓰인다.
    assert proposal["meta"]["applied_adjustments"] == 0, "못 쓰는 조정안을 썼다고 적었다"
    assert proposal["meta"]["applied_adjustments"] <= proposal["meta"]["received_adjustments"]


def test_every_scenario_says_it_did_not_apply_them() -> None:
    """🔴 **받았는데 안 썼다**를 안마다 적는다.

    이 줄이 없으면 보내는 쪽은 자기 제안이 반영된 줄 안다 — 우리가 다른 파트에
    지적했던 "값을 실어 주고 안 쓰는" 자리와 같아진다.

    🔴 **기대를 뜻에 맞췄다** (2026-09-09 · E3-6). 전에는 *"조정안 2건을 받았으나"*
      라는 **한 문면**을 봤는데, 고지가 두 갈래로 갈렸다 — *"반영 규칙이 없다"*(우리
      사정)와 *"이 조정안은 못 쓴다"*(그쪽 사정). 이 픽스처의 조정안은 물류
      ``quantity`` 축이라 뒤쪽에 걸린다. **잡으려던 사실은 그대로다.**
    """
    proposal = _proposal(adjustments=[ADJUSTMENT, ADJUSTMENT])
    assert proposal["scenarios"], "이 앵커는 안이 서야 검사가 성립한다"
    for scenario in proposal["scenarios"]:
        notice = [r for r in scenario["risks"] if "조정안 2건" in r]
        assert notice, f"{scenario['label']} 안에 미반영 고지가 없다: {scenario['risks']}"
        assert "반영하지 않았다" in notice[0]
        assert " — " in notice[0], "왜 안 썼는지가 없다 — 사유 없는 고지는 물을 자리를 안 준다"


def test_declared_axes_are_names_the_contract_owns() -> None:
    """🔴 **축 이름의 주인은 우리가 아니다** (``contracts.core.AdjustAxis``).

    ``applicable_axis_units`` 에 계약에 없는 축을 적으면 그 줄은 **영원히 안 맞는다** —
    들어오는 조정안의 축이 그 이름일 수 없기 때문이다. 조용히 «반영할 수 있는 항목이
    아니다» 로만 나가고 아무도 안 운다.

    ⚠️ 규칙 8 로는 못 잡는다 — 선언을 바꿔도 판정이 «못 씀» 그대로다. 그래서 판정이
      아니라 **이름을 계약과 대조**한다 (``test_constraints_has_no_new_unread_declaration``
      이 참조를 세는 것과 같은 종류).
    """
    from typing import get_args

    from app.contracts.core import AdjustAxis
    from app.purchase_agent.config import load_constraints

    declared = {row["axis"] for row in load_constraints()["feedback"]["applicable_axis_units"]}
    assert declared, "반영할 수 있는 항목이 하나도 없으면 이 층 전체가 죽은 코드다"
    assert declared <= set(get_args(AdjustAxis)), (
        f"계약에 없는 축을 선언했다: {sorted(declared - set(get_args(AdjustAxis)))}"
    )


# ── 축·단위가 안 맞는 조정안 (E3-6 · 2026-09-09) ──────────────────────────
#
# 계약(`SuggestedAdjustment`)의 `unit` 은 자유 문자열이라 **축과 안 맞아도 봉투가 안
# 막는다.** 마스터 IO Contract 가 *"받는 쪽이 risks 로 걸러야 합니다"* 로 넘긴 자리다.
#
# 🔴 실측(2026-09-09 · `master_agent_runs`)에 `axis=amount` 인데 `unit=kg` ·
#   `target_value=900` 인 조정안이 6건 있다. 아래 픽스처는 **그 실측 모양 그대로**다.

#: 실측에서 본 «축과 단위가 어긋난» 조정안. 원으로 알고 환산하면 900 ÷ 단가 = 0kg 이다.
WRONG_UNIT = {
    "dept": "finance",
    "axis": "amount",
    "target_value": 900.0,
    "unit": "kg",
    "reason": "Verified Finance amount alternative.",
    "ref_ids": ["FIN-AGENT:WRONG-UNIT"],
    "scenario_labels": ["기본"],
    "split_date": None,
}

#: 실측에서 본 «쓸 수 있는» 재무 조정안.
FINANCE_AMOUNT = {**WRONG_UNIT, "target_value": 2_953_738.0, "unit": "krw"}


def test_an_amount_in_kilograms_is_not_taken_as_won() -> None:
    """🔴 **이 검사가 없으면 매입량이 조용히 0 이 된다.**

    ``axis=amount`` 를 원으로 알고 ``900 ÷ 단가`` 를 하면 ``0kg`` 이고, ③이 ``min()``
    으로 클립하므로 **그 안의 수량이 0** 이 된다. 예외도 경고도 없다.
    """
    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.draft_plan import split_adjustments

    usable, unusable = split_adjustments([WRONG_UNIT], load_constraints())

    assert usable == [], "축과 단위가 어긋난 조정안이 반영 대상에 들어갔다"
    assert len(unusable) == 1
    assert "kg" in unusable[0][1] and "krw" in unusable[0][1], "무엇이 어긋났는지가 없다"


def test_an_adjustment_without_a_target_scenario_is_not_applied() -> None:
    """어느 안인지 모르는 조정안은 **안 쓴다.**

    계약이 *"안 채운 것과 해당 없는 것을 여기서 가르지 않는다"* 라 빈 값이 두 뜻이다.
    모르는 채로 전 안을 조이면 **근거 없이 조이는 것**이다 (규칙 3).
    """
    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.draft_plan import split_adjustments

    usable, unusable = split_adjustments(
        [{**FINANCE_AMOUNT, "scenario_labels": []}], load_constraints()
    )
    assert usable == []
    assert "어느 안에" in unusable[0][1]


def test_a_matching_adjustment_survives_the_filter() -> None:
    """거르는 층이 **다 거르면** 거르는 게 아니라 막는 것이다 — 통과하는 길을 잠근다."""
    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.draft_plan import split_adjustments

    usable, unusable = split_adjustments([FINANCE_AMOUNT], load_constraints())
    assert usable == [FINANCE_AMOUNT]
    assert unusable == []


def test_the_declaration_decides_which_unit_is_right() -> None:
    """🔴 **선언을 바꾸면 판정이 따라 바뀌는가** (규칙 8).

    지금 선언(`amount`→`krw`)과 코드가 값이 같아, *"krw 가 통과한다"* 를 확인하는
    검사만으로는 코드가 그 값을 박아 뒀는지 갈리지 않는다. 선언을 실제로 뒤집어
    **통과·차단이 자리를 바꾸는지** 본다.
    """
    from copy import deepcopy

    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.draft_plan import split_adjustments

    flipped = deepcopy(load_constraints())
    flipped["feedback"]["applicable_axis_units"] = [{"axis": "amount", "unit": "kg"}]

    usable, _ = split_adjustments([WRONG_UNIT], flipped)
    assert usable == [WRONG_UNIT], "선언을 바꿨는데 판정이 안 따라왔다 — 코드가 단위를 박고 있다"

    usable, unusable = split_adjustments([FINANCE_AMOUNT], flipped)
    assert usable == [] and "krw" in unusable[0][1], "반대 방향도 따라와야 한다"


def test_unusable_notice_reaches_the_scenario_risks() -> None:
    """🔴 **배선 검사다.** 헬퍼만 갈라 놓고 ⑥이 안 부르면 화면은 그대로다."""
    proposal = _proposal(adjustments=[WRONG_UNIT])
    assert proposal["scenarios"], "이 앵커는 안이 서야 검사가 성립한다"
    for scenario in proposal["scenarios"]:
        notice = [r for r in scenario["risks"] if "조정안" in r]
        assert notice, f"{scenario['label']} 안에 고지가 없다"
        assert any("kg" in line and "krw" in line for line in notice), (
            f"어긋난 단위를 말하지 않는다: {notice}"
        )


def test_one_line_per_reason_not_per_adjustment() -> None:
    """같은 사유가 여러 건이면 **줄은 하나**다 — 사유가 정보이지 건수가 아니다."""
    proposal = _proposal(adjustments=[WRONG_UNIT, WRONG_UNIT, WRONG_UNIT])
    for scenario in proposal["scenarios"]:
        notice = [r for r in scenario["risks"] if "조정안" in r]
        assert len(notice) == 1, f"사유가 하나인데 줄이 여럿이다: {notice}"
        assert "3건" in notice[0], "묶었으면 건수는 남아야 한다"


# ── 반영 (E3-6 · 2026-09-09) ──────────────────────────────────────────────
#
# `target_value` 는 **넘지 말아야 할 값**이다 — 마스터 IO Contract §4.4 확정.
# 지시값이 아니라 상한이라 ③은 `min([raw_qty, *caps])` 에 칸 하나가 늘 뿐이다.


def test_the_adjustment_only_shrinks_the_scenarios_it_names() -> None:
    """🔴 **이 판의 본체다.** 조정안이 겨냥한 안만 줄고 나머지는 그대로여야 한다.

    재무가 상한 2,000만에 보수 1,500만 · 기본 2,100만 · 공격 2,800만을 봤으면
    **기본·공격만 재조정 대상**이다 (``scenario_labels`` 가 신설된 이유 그대로).
    전 안을 조이면 근거 없이 조이는 것이고, 아무 안도 안 조이면 반영이 아니다.
    """
    before = {s["label"]: s["total_qty_kg"] for s in _proposal()["scenarios"]}
    after = {
        s["label"]: s["total_qty_kg"]
        for s in _proposal(adjustments=[FINANCE_AMOUNT])["scenarios"]
    }

    assert before and set(before) == set(after), "안 구성이 바뀌면 이 비교가 성립하지 않는다"
    assert after["기본"] < before["기본"], "겨냥한 안이 안 줄었다 — 반영이 안 됐다"
    for label in set(before) - {"기본"}:
        assert after[label] == before[label], f"{label} 은 대상이 아닌데 줄었다"


def test_the_shrunk_scenario_still_balances() -> None:
    """줄인 뒤에도 **사중 일치**가 선다 (규칙 4). 수량만 줄이고 나머지를 안 맞추면 컷된다."""
    proposal = _proposal(adjustments=[FINANCE_AMOUNT])
    target = next(s for s in proposal["scenarios"] if s["label"] == "기본")

    assert target["total_qty_kg"] == pytest.approx(
        sum(item["qty_kg"] for item in target["sourcing_plan"])
    )
    assert target["total_qty_kg"] == pytest.approx(
        sum(item["qty_kg"] for item in target["split_plan"])
    )


def test_the_reason_for_the_smaller_number_is_on_the_scenario() -> None:
    """줄인 이유가 그 안에 남는다 — 숫자만 바뀌고 왜가 없으면 사람이 못 따라간다."""
    proposal = _proposal(adjustments=[FINANCE_AMOUNT])
    target = next(s for s in proposal["scenarios"] if s["label"] == "기본")

    shrunk = [r for r in target["risks"] if "조정안 제약으로" in r]
    assert shrunk, f"축소 사유가 없다: {target['risks']}"
    assert "kg으로 축소" in shrunk[0]


def test_a_scenario_does_not_say_applied_and_not_applied_at_once() -> None:
    """🔴 **화면이 두 말을 하지 않는다.**

    이 판을 붙이자마자 그 상태가 났다 — 같은 안에 *"조정안 제약으로 … 축소"* 와
    *"조정안 1건을 받았으나 … 반영하지 않았다"* 가 나란히 떴다. 한 사실을 두 곳이
    적으면 한쪽만 고치는 날 갈린다.
    """
    proposal = _proposal(adjustments=[FINANCE_AMOUNT])
    for scenario in proposal["scenarios"]:
        lines = [r for r in scenario["risks"] if "조정안" in r]
        applied = [r for r in lines if "축소" in r or "반영했으나" in r]
        refused = [r for r in lines if "반영하지 않았다" in r]
        assert not (applied and refused), f"{scenario['label']} 이 두 말을 한다: {lines}"


def test_an_adjustment_that_does_not_bind_says_so() -> None:
    """걸었는데 **안 물린** 경우도 말한다 — 아무 줄도 없으면 «무관» 으로 읽힌다."""
    loose = {**FINANCE_AMOUNT, "target_value": 900_000_000.0}
    proposal = _proposal(adjustments=[loose])
    target = next(s for s in proposal["scenarios"] if s["label"] == "기본")

    notice = [r for r in target["risks"] if "상한 아래라" in r]
    assert notice, f"걸었는데 안 물린 사실이 없다: {target['risks']}"
    assert not [r for r in target["risks"] if "조정안 제약으로" in r], "안 물렸는데 축소했다"


def test_applied_count_is_not_the_received_count() -> None:
    """🔴 **닿은 수와 쓴 수는 다른 사실이다.** 마스터가 기다리던 칸이다."""
    proposal = _proposal(adjustments=[FINANCE_AMOUNT, WRONG_UNIT])

    assert proposal["meta"]["received_adjustments"] == 2
    assert proposal["meta"]["applied_adjustments"] == 1, (
        "단위가 어긋난 것까지 «반영했다» 로 세면 마스터 대조가 거짓을 통과한다"
    )


def test_the_lowest_cap_wins_when_several_target_one_scenario() -> None:
    """상한이 여럿이면 **전부 지켜야 한다** — 그건 min 이다."""
    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.draft_plan import adjustment_cap_kg, split_adjustments

    low = {**FINANCE_AMOUNT, "target_value": 1_000_000.0}
    usable, _ = split_adjustments([FINANCE_AMOUNT, low], load_constraints())
    assert len(usable) == 2

    assert adjustment_cap_kg(usable, "기본", 1_000) == 1_000
    assert adjustment_cap_kg(usable, "보수", 1_000) is None, "대상이 아닌 안에 상한이 걸렸다"


def test_no_notice_when_nothing_arrived() -> None:
    """안 왔으면 아무 줄도 안 붙는다 — 없는 사실을 고지하지 않는다."""
    proposal = _proposal()
    for scenario in proposal["scenarios"]:
        assert not [r for r in scenario["risks"] if "조정안" in r]


def test_notice_survives_the_output_wording_ban() -> None:
    """고지 문구에 **내부 용어가 없다**.

    이 필드는 H1 화면과 Critic 이 읽는다 (#164). ``test_output_wording`` 의 금지어
    목록을 여기서 다시 적지 않는다 — 같은 낱말을 두 곳에 두면 한쪽만 바뀐다.
    대신 그 검사가 실제로 이 문장을 훑도록 **조정안이 온 실행**을 하나 만들어 둔다.
    """
    from tests.test_purchase_agent.test_output_wording import BANNED

    proposal = _proposal(adjustments=[ADJUSTMENT])
    notice = [r for scenario in proposal["scenarios"] for r in scenario["risks"] if "조정안" in r]
    assert notice
    for line in notice:
        for word in BANNED:
            assert word not in line, f"고지 문구에 내부 용어 {word!r}: {line}"


# ── 봉투가 여전히 깨끗하다 ────────────────────────────────────────────────


def test_envelope_stays_clean_with_adjustments() -> None:
    """조정안을 받은 회신도 봉투 검증을 통과한다.

    meta 에 칸이 하나 늘었으므로 근거 규칙(``_needs_evidence``)이 새 근거를 요구하는지
    확인한다 — 우리가 규칙을 베껴 쓰지 않고 봉투 자신의 검증기를 부른다.
    """
    request = _request(adjustments=[ADJUSTMENT], feedback_context=FEEDBACK_CONTEXT)
    reply, metadata = purchase_port(request)
    assert validate_reply(request, reply, metadata) == ()
