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

from datetime import date

import pytest

from app.master.envelope import AgentRequest, ExecutionContext, validate_reply
from app.purchase_agent import ports
from app.purchase_agent.adapter import build_state, purchase_port

AS_OF = date(2026, 9, 11)  # stable 앵커 — 3안이 다 서는 날이라 risks 를 안별로 볼 수 있다
ITEM = "피마늘"

#: 물류가 내는 표준형을 ``asdict`` 로 편 모양. **마스터가 보내는 그대로**를 쓴다 —
#: 우리가 다시 조립하면 실제 payload 와 어긋나도 검사가 통과한다.
ADJUSTMENT = {
    "dept": "inventory",
    "axis": "quantity",
    "target_value": 900.0,
    "unit": "kg",
    "reason": "기본 시나리오 2026-09-11 회차 — 수량을 900kg 로 조정 제안",
    "ref_ids": ("INV-CAP-0911",),
    "scenario_labels": (),
    "split_date": None,
}

FEEDBACK_CONTEXT = {
    "attempt": 2,
    "reason": "물류 conditional 1건",
    "findings": ["창고 여유가 회차 수량에 못 미친다"],
    "verdicts": {"inventory": "conditional"},
    "verdict_reasons": {"inventory": "2회차 도착일에 여유가 부족하다"},
}


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
    assert "applied_adjustments" not in proposal["meta"], "반영 안 했으므로 이름도 없어야 한다"


def test_every_scenario_says_it_did_not_apply_them() -> None:
    """🔴 **받았는데 안 썼다**를 안마다 적는다.

    이 줄이 없으면 보내는 쪽은 자기 제안이 반영된 줄 안다 — 우리가 다른 파트에
    지적했던 "값을 실어 주고 안 쓰는" 자리와 같아진다.
    """
    proposal = _proposal(adjustments=[ADJUSTMENT, ADJUSTMENT])
    assert proposal["scenarios"], "이 앵커는 안이 서야 검사가 성립한다"
    for scenario in proposal["scenarios"]:
        notice = [r for r in scenario["risks"] if "조정안 2건을 받았으나" in r]
        assert notice, f"{scenario['label']} 안에 미반영 고지가 없다: {scenario['risks']}"
        assert "반영하지 않았다" in notice[0]


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
