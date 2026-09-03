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

from app.master.envelope import AgentRequest, ExecutionContext, validate_reply
from app.master.flow import ProcurementFlow
from app.master.ports import AgentRegistry
from app.master.runner import MasterRunner
from app.orchestrator.contracts_core import SuggestedAdjustment
from app.purchase_agent import ports
from app.purchase_agent.adapter import build_state, purchase_port

AS_OF = date(2026, 9, 11)  # stable 앵커 — 3안이 다 서는 날이라 risks 를 안별로 볼 수 있다
ITEM = "피마늘"

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


def test_adjustments_do_not_move_feedback_attempt() -> None:
    """``feedback_attempt`` 는 ``feedback`` 소유다 — 조정안이 와도 안 움직인다.

    ⚠️ 그래서 ``feedback_context["attempt"]`` 가 2 여도 ``meta.feedback_attempt`` 는
      0 이다. **의도한 상태이고 미결이다** — 회차 번호를 어느 칸이 말하는지가
      아직 안 정해졌다. 여기서 조용히 바꾸면 그 미결이 사라진다.
    """
    proposal = _proposal(adjustments=[ADJUSTMENT], feedback_context=FEEDBACK_CONTEXT)
    assert proposal["meta"]["feedback_attempt"] == 0
    assert proposal["meta"]["is_refeed"] is False
    assert proposal["meta"]["received_adjustments"] == 1


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
