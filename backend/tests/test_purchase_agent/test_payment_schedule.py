"""회차별 지급 계획 (재무 확정 7필드 · 2026-08-27 회신 · 이슈 #61).

재무가 ``SCENARIO_VALIDATION``에서 회차별 Cashflow를 검증할 때 쓰는 값이다. 두 금액이
각각 다른 검증에 들어간다 — ``amount_krw``는 BASE, ``amount_max_krw``는 STRESS.

**이중 경로다.** mock 경로는 N5가 미결이라 키가 없고, 어댑터 경로만 값을 받아 싣는다.
재무 cap·N5 보류 고지와 같은 패턴이다.
"""

from datetime import date, timedelta

import pytest

from app.master.envelope import AgentRequest, ExecutionContext, validate_reply
from app.purchase_agent import ports
from app.purchase_agent.adapter import purchase_port
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.nodes.package_scenarios import build_payment_schedule
from app.purchase_agent.nodes.self_check import check_payment_schedule

SPLIT_DAY = date(2026, 8, 21)  # 공격안이 2회 분할되는 앵커
N5 = 7


def _payload(item: str, as_of: date, **over) -> dict:
    extras = ports.get_snapshot_extras(item, as_of)
    finance = {
        "base_projected_cash_min": ports.get_projected_cash_min(as_of, 30),
        "margin_defense_floor_rate": 0.267,
        "finance_cap_amount_krw": 20_000_000,
        "purchase_payment_days": N5,
        "critical_payment_dates": [],
    }
    finance.update(over.pop("finance", {}))
    return {
        "item": item,
        "constraints": {"finance": finance, "inventory": ports.get_inventory(item, as_of)},
        "forecast": ports.get_forecast(item, as_of),
        "confirmed_orders": ports.get_confirmed_orders(item, as_of, days=14),
        "policy_values": {
            "contract_price_krw": extras["contract_price"],
            "item_mix_ratio": extras["item_mix_ratio"],
        },
    }


def _run(item: str, as_of: date, **over):
    request = AgentRequest(
        context=ExecutionContext(f"REQ-{as_of}-{item}", as_of, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=_payload(item, as_of, **over),
    )
    reply, metadata = purchase_port(request)
    return request, reply, metadata


def _split_scenario(payload: dict) -> dict:
    return next(s for s in payload["scenarios"] if len(s["split_plan"]) > 1)


# ── 필드 계약 — 재무 확정 7개 ─────────────────────────────────────────────


def test_schedule_carries_exactly_the_seven_agreed_fields() -> None:
    """재무 확정분과 우리 제안 §3.2가 일치한 7필드. **``by_grade``는 없다.**

    등급별 수량·단가는 ``sourcing_plan``이 정본이고, 여기는 회차별 Cashflow 정보만
    있으면 충분하다 (회신 §2).
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    schedule = _split_scenario(reply.payload)["payment_schedule"]
    for row in schedule:
        assert set(row) == {
            "seq",
            "purchase_date",
            "payment_date",
            "qty_kg",
            "amount_krw",
            "amount_max_krw",
            "basis",
        }
        assert "by_grade" not in row


def test_two_amounts_feed_different_finance_checks() -> None:
    """``amount_krw``(BASE)와 ``amount_max_krw``(STRESS)는 **다른 값**이다.

    같으면 STRESS 검증이 BASE와 구분되지 않아 재무의 3단 판정
    (PASS / REVIEW_REQUIRED / FAIL)이 2단으로 무너진다.
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = _split_scenario(reply.payload)
    for row in scenario["payment_schedule"]:
        assert row["amount_max_krw"] == row["qty_kg"] * scenario["max_price"]
        assert row["amount_max_krw"] > row["amount_krw"], "상한가가 오늘 단가보다 높다"


# ── 항등식 5개 ────────────────────────────────────────────────────────────


def test_quantities_and_amounts_sum_to_the_scenario_totals() -> None:
    """항등식 1·2 — ``Σqty == total_qty_kg`` · ``Σamount == total_amount_krw``.

    금액 합이 어긋나면 재무의 BASE Cashflow가 **틀린 입력 위에서** 계산된다.
    회차마다 ``round()``하면 합이 밀리므로 마지막 회차가 잔량을 흡수한다.
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = _split_scenario(reply.payload)
    schedule = scenario["payment_schedule"]
    assert sum(r["qty_kg"] for r in schedule) == scenario["total_qty_kg"]
    assert sum(r["amount_krw"] for r in schedule) == scenario["total_amount_krw"]


def test_dates_track_split_plan_and_add_n5() -> None:
    """항등식 3·4 — ``purchase_date == split_plan[i].date`` · ``payment_date == +N5``.

    N5는 **calendar day**이고 영업일 보정을 하지 않는다 (재무 확정). 여기서 주말을 밀면
    재무 계산과 어긋난다.
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = _split_scenario(reply.payload)
    for row, item in zip(scenario["payment_schedule"], scenario["split_plan"], strict=True):
        assert row["seq"] == item["seq"]
        assert row["purchase_date"] == item["date"]
        due = date.fromisoformat(item["date"]) + timedelta(days=N5)
        assert row["payment_date"] == due.isoformat()


def test_bulk_scenarios_have_no_payment_schedule_key() -> None:
    """항등식 5 — **일괄 안에는 키 자체가 없다.**

    ``null``이 아니라 **부재**다. 지급일 하나는 ``split_plan``에서 바로 파생되므로 같은
    값을 두 벌 내보내지 않는다. 재무는 이 키의 유무로 회차별 검증 여부를 가른다.
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    for scenario in reply.payload["scenarios"]:
        if len(scenario["split_plan"]) == 1:
            assert "payment_schedule" not in scenario


# ── 이중 경로 — mock은 N5가 미결이다 ──────────────────────────────────────


def test_mock_path_omits_the_key_and_discloses_why() -> None:
    """mock 경로는 N5가 ``null``이라 **지급일을 계산할 수 없다** (규칙 3).

    0으로 채우면 "D+0 즉시지급"이 되어 운전자금이 과대 계상된다. 키를 만들지 않고
    그 사실을 ``deferred_checks``(risks)가 싣는다.
    """
    proposal = run_purchase_agent("배추", SPLIT_DAY)
    split = _split_scenario(proposal)
    assert len(split["split_plan"]) > 1, "분할 안인데도"
    assert "payment_schedule" not in split, "N5가 없으면 키를 만들지 않는다"
    assert [note for note in split["risks"] if "대금 지급 소요일" in note], "왜 없는지 남아야 한다"


def test_adapter_path_carries_the_key_on_the_same_day() -> None:
    """같은 날 같은 안인데 **N5를 받으면 실린다** — 경로만 다르다."""
    _, reply, _ = _run("배추", SPLIT_DAY)
    assert "payment_schedule" in _split_scenario(reply.payload)


# ── ⑦ 검사가 실제로 무는가 ────────────────────────────────────────────────


def _state(payment_days: int | None = N5) -> dict:
    return {"purchase_payment_days": payment_days}


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s["payment_schedule"].pop(), "회차 수 불일치"),
        (lambda s: s["payment_schedule"][0].__setitem__("qty_kg", 1), "수량 합"),
        (lambda s: s["payment_schedule"][0].__setitem__("amount_krw", 1), "금액 합"),
        (lambda s: s["payment_schedule"][0].__setitem__("purchase_date", "2026-01-01"), "어긋난다"),
        (lambda s: s["payment_schedule"][0].__setitem__("payment_date", "2026-01-01"), "지급일이"),
    ],
)
def test_self_check_catches_a_broken_schedule(mutate, expected: str) -> None:
    """항등식이 깨진 계획을 ⑦이 컷한다 — 재무가 틀린 입력 위에서 판정하면 안 된다."""
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = {**_split_scenario(reply.payload)}
    scenario["payment_schedule"] = [dict(r) for r in scenario["payment_schedule"]]
    mutate(scenario)
    reason = check_payment_schedule(scenario, _state())  # type: ignore[arg-type]
    assert reason and expected in reason


# ── Codex 교차검증 회귀 — 우회 경로 ───────────────────────────────────────


def test_deleting_the_schedule_does_not_bypass_the_check() -> None:
    """🔴 **키를 지우면 검사가 통째로 빠졌다** (Codex 교차검증 P1).

    ``schedule is None``에서 무조건 통과시켰는데, N5를 받은 분할안이라면 **있어야
    한다.** 없는 것도 위반이다 — 만들어야 할 것이 사라진 상태다.
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = _split_scenario(reply.payload)
    stripped = {k: v for k, v in scenario.items() if k != "payment_schedule"}
    reason = check_payment_schedule(stripped, _state())  # type: ignore[arg-type]
    assert reason and "없다" in reason


def test_schedule_without_n5_is_rejected() -> None:
    """N5가 미결인데 실려 있으면 컷한다 — **계산할 수 없는 값**이다.

    예전엔 ``payment_days``가 None이면 지급일 검사를 건너뛰어 ``2026-01-01`` 같은
    과거 지급일도 통과했다 (Codex 교차검증 P1).
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = _split_scenario(reply.payload)
    reason = check_payment_schedule(scenario, _state(None))  # type: ignore[arg-type]
    assert reason and "N5 미결" in reason


def test_stress_amount_is_verified() -> None:
    """🔴 **STRESS 금액을 검증하지 않았다** (Codex 교차검증 P1).

    재무가 ``amount_max_krw``를 그대로 STRESS Cashflow에 쓰므로, 틀리면
    REVIEW_REQUIRED/FAIL이어야 할 판정이 **PASS 쪽으로 왜곡된다.**
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = {**_split_scenario(reply.payload)}
    scenario["payment_schedule"] = [dict(r) for r in scenario["payment_schedule"]]
    scenario["payment_schedule"][0]["amount_max_krw"] = 0
    reason = check_payment_schedule(scenario, _state())  # type: ignore[arg-type]
    assert reason and "STRESS" in reason


def test_moving_money_between_rounds_is_caught() -> None:
    """🔴 **합계만 유지하면 회차 간 이동이 통과했다** (Codex 교차검증 P1).

    1회차에서 빼 2회차에 더하면 두 합계와 seq·date가 그대로라 최종 출력까지 갔다.
    그런데 ``payment_schedule[i].qty_kg != split_plan[i].qty_kg``이고, **BASE 현금유출이
    잘못된 날짜에 배치된다** — 재무가 다른 날 돈이 나간다고 계산한다.
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = {**_split_scenario(reply.payload)}
    schedule = [dict(r) for r in scenario["payment_schedule"]]
    schedule[0]["qty_kg"] += 1
    schedule[1]["qty_kg"] -= 1
    schedule[0]["amount_krw"] += 1
    schedule[1]["amount_krw"] -= 1
    scenario["payment_schedule"] = schedule

    # 합계는 그대로다 — 그래서 예전 검사를 빠져나갔다
    assert sum(r["qty_kg"] for r in schedule) == scenario["total_qty_kg"]
    assert sum(r["amount_krw"] for r in schedule) == scenario["total_amount_krw"]

    reason = check_payment_schedule(scenario, _state())  # type: ignore[arg-type]
    assert reason and "회차" in reason


def test_moving_quantity_is_caught_even_when_stress_is_kept_consistent() -> None:
    """수량 검사 **고유의** 방어를 확인한다.

    ⚠️ 앞 테스트만으로는 부족했다 — 수량을 옮기면 ``amount_max_krw``(= 수량 × 상한가)도
    어긋나 **STRESS 검사가 우연히 잡는다.** 수량 검사를 지워도 초록불이었다(변이로 확인).
    STRESS까지 새 수량에 맞춰 주면 수량 검사만 남는다.
    """
    _, reply, _ = _run("배추", SPLIT_DAY)
    scenario = {**_split_scenario(reply.payload)}
    schedule = [dict(r) for r in scenario["payment_schedule"]]
    max_price = scenario["max_price"]
    for index, delta in ((0, 1), (1, -1)):
        schedule[index]["qty_kg"] += delta
        schedule[index]["amount_krw"] += delta
        schedule[index]["amount_max_krw"] = schedule[index]["qty_kg"] * max_price
    scenario["payment_schedule"] = schedule

    # 합계·seq·date·STRESS 전부 정합이다 — 오직 회차별 수량만 분할과 어긋난다
    assert sum(r["qty_kg"] for r in schedule) == scenario["total_qty_kg"]
    assert sum(r["amount_krw"] for r in schedule) == scenario["total_amount_krw"]
    assert all(r["amount_max_krw"] == r["qty_kg"] * max_price for r in schedule)

    reason = check_payment_schedule(scenario, _state())  # type: ignore[arg-type]
    assert reason and "수량이 분할과 다르다" in reason


def test_multi_grade_amounts_are_reproducible_from_sourcing_plan() -> None:
    """🔴 **다등급 날 금액이 어떤 정수 kg 구성으로도 재현되지 않았다** (Codex 교차검증).

    전체 가중단가를 회차 수량에 곱했기 때문이다. 재무가 ``sourcing_plan``으로 검산하면
    어긋난다. 9/11 공격안이 다등급 + 분할이라 이 경로가 실제로 밟힌다.
    """
    _, reply, _ = _run("배추", date(2026, 9, 11))
    scenario = _split_scenario(reply.payload)
    assert len(scenario["sourcing_plan"]) > 1, "다등급 날이어야 의미가 있다"

    total = sum(x["qty_kg"] * x["grade_unit_price"] for x in scenario["sourcing_plan"])
    assert sum(r["amount_krw"] for r in scenario["payment_schedule"]) == total

    # 각 회차 금액이 등급 단가의 **정수 조합**으로 떨어진다
    prices = sorted({x["grade_unit_price"] for x in scenario["sourcing_plan"]})
    for row in scenario["payment_schedule"]:
        assert any(
            row["amount_krw"] == mid * prices[0] + (row["qty_kg"] - mid) * prices[1]
            for mid in range(row["qty_kg"] + 1)
        ), f"회차 {row['seq']} 금액 {row['amount_krw']:,}이 정수 kg 구성으로 안 나온다"


def test_negative_n5_produces_no_schedule() -> None:
    """음수 N5는 **지급일이 매입일보다 앞선다**는 모순이라 만들지 않는다.

    예전엔 만들고, ⑦도 같은 음수로 재계산해 정상 판정했다 (Codex 교차검증).
    """
    _, reply, _ = _run("배추", SPLIT_DAY, finance={"purchase_payment_days": -7})
    assert "payment_schedule" not in _split_scenario(reply.payload)


def test_schema_rejects_a_wrong_basis_and_boolean_numbers() -> None:
    """``basis``는 열거형이고 숫자 자리에 ``bool``이 못 들어간다 (Codex 교차검증).

    bool은 int의 서브클래스라 ``ge``/``gt``를 그냥 통과하고 ``True``가 ``1``이 된다 —
    인접한 ``SplitPlanItem``·``SourcingPlanItem``과 같은 방어다.
    """
    from pydantic import ValidationError

    from app.purchase_agent.schemas import PaymentScheduleItem

    valid = {
        "seq": 1,
        "purchase_date": "2026-08-21",
        "payment_date": "2026-08-28",
        "qty_kg": 100,
        "amount_krw": 165_000,
        "amount_max_krw": 180_000,
        "basis": "as_of_unit_price",
    }
    assert PaymentScheduleItem.model_validate(valid)

    with pytest.raises(ValidationError):
        PaymentScheduleItem.model_validate({**valid, "basis": "wrong"})
    with pytest.raises(ValidationError):
        PaymentScheduleItem.model_validate({**valid, "qty_kg": True})


def test_self_check_rejects_a_schedule_on_a_bulk_scenario() -> None:
    """일괄 안에 계획이 실리면 컷한다 — 실을 것이 없는데 실은 것이다."""
    _, reply, _ = _run("배추", SPLIT_DAY)
    split = _split_scenario(reply.payload)
    bulk = next(s for s in reply.payload["scenarios"] if len(s["split_plan"]) == 1)
    tampered = {**bulk, "payment_schedule": split["payment_schedule"]}
    reason = check_payment_schedule(tampered, _state())  # type: ignore[arg-type]
    assert reason and "일괄 안에" in reason


def test_self_check_is_silent_when_the_key_is_absent() -> None:
    """만들지 않은 경로는 검사 대상이 아니다 — 없는 것이 정상인 날이 있다."""
    proposal = run_purchase_agent("배추", SPLIT_DAY)
    assert check_payment_schedule(_split_scenario(proposal), _state(None)) is None  # type: ignore[arg-type]


def test_broken_schedule_reaches_rejected_reasons() -> None:
    """검사가 **그래프 안에서** 컷하는가 — 함수 단위 통과와 배선은 별개다.

    ⚠️ 이 테스트가 없으면 ``check_payment_schedule``을 검사 체인에서 통째로 빼도 전부
    초록불이다. 위 단위 테스트들이 함수를 직접 부르기 때문이다 — 변이로 확인해 드러났다.
    """
    from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
    from app.purchase_agent.nodes.classify_situation import classify_situation
    from app.purchase_agent.nodes.collect_context import collect_context
    from app.purchase_agent.nodes.draft_plan import draft_plan
    from app.purchase_agent.nodes.package_scenarios import package_scenarios
    from app.purchase_agent.nodes.self_check import self_check
    from app.purchase_agent.nodes.split_plan import split_plan
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state("배추", SPLIT_DAY)
    # 어댑터 경로를 흉내낸다 — N5를 실어야 계획이 만들어진다
    state["purchase_payment_days"] = N5  # type: ignore[typeddict-unknown-key]
    for node in (
        classify_situation,
        collect_context,
        draft_plan,
        split_plan,
        allocate_sourcing,
        package_scenarios,
    ):
        state.update(node(state))

    target = next(
        s for s in state["scenarios_final"] if s.get("payment_schedule")
    )
    target["payment_schedule"][0]["amount_krw"] += 1  # 금액 합을 깨뜨린다

    result = self_check(state)
    assert any("금액 합" in item["reason"] for item in result["rejected_reasons"])


# ── 생성 함수 단위 ────────────────────────────────────────────────────────


_SOURCING = [
    {"market": "가락", "grade": "중", "qty_kg": 600, "grade_unit_price": 1300},
    {"market": "가락", "grade": "상", "qty_kg": 400, "grade_unit_price": 1650},
]


def test_builder_returns_none_for_three_reasons() -> None:
    """``None``인 경우가 셋이고 **뜻이 다르다** — 전부 키를 만들지 않는다.

    N5 미결은 *"계산할 수 없다"*, 회차 1은 *"실을 것이 없다"*, 음수 N5는 *"지급일이
    매입일보다 앞선다"*는 모순이다.
    """
    rounds = [
        {"seq": 1, "date": "2026-08-21", "qty_kg": 500},
        {"seq": 2, "date": "2026-08-27", "qty_kg": 500},
    ]
    assert build_payment_schedule(rounds, _SOURCING, 1_800, None) is None  # N5 미결
    assert build_payment_schedule(rounds[:1], _SOURCING, 1_800, N5) is None  # 회차 1
    assert build_payment_schedule(rounds, _SOURCING, 1_800, -7) is None  # 음수 N5


def test_amounts_are_reproducible_from_the_grade_mix() -> None:
    """🔴 금액은 **등급 구성에서 재현 가능해야 한다** (Codex 교차검증).

    처음엔 전체 가중단가를 회차 수량에 곱했는데, 그러면 **어떤 정수 kg 등급 구성으로도
    나올 수 없는 금액**이 된다. 재무가 ``sourcing_plan``으로 검산하면 어긋난다.
    """
    rounds = [
        {"seq": 1, "date": "2026-08-21", "qty_kg": 500},
        {"seq": 2, "date": "2026-08-27", "qty_kg": 500},
    ]
    schedule = build_payment_schedule(rounds, _SOURCING, 1_800, N5)
    assert schedule is not None

    total = sum(line["qty_kg"] * line["grade_unit_price"] for line in _SOURCING)
    assert sum(r["amount_krw"] for r in schedule) == total

    # 각 회차 금액이 **정수 kg 조합**으로 설명된다: 중 300 + 상 200 = 720,000
    assert schedule[0]["amount_krw"] == 300 * 1300 + 200 * 1650
    assert schedule[1]["amount_krw"] == 300 * 1300 + 200 * 1650


def test_amounts_sum_exactly_with_indivisible_quantities() -> None:
    """나누어떨어지지 않아도 합은 1원도 어긋나지 않는다.

    ``Σ amount_krw == total_amount_krw``가 재무 BASE 검증의 전제다.
    """
    rounds = [
        {"seq": 1, "date": "2026-08-21", "qty_kg": 333},
        {"seq": 2, "date": "2026-08-24", "qty_kg": 333},
        {"seq": 3, "date": "2026-08-27", "qty_kg": 334},
    ]
    sourcing = [
        {"market": "가락", "grade": "중", "qty_kg": 667, "grade_unit_price": 1301},
        {"market": "가락", "grade": "상", "qty_kg": 333, "grade_unit_price": 1657},
    ]
    schedule = build_payment_schedule(rounds, sourcing, 1_800, N5)
    assert schedule is not None
    total = sum(line["qty_kg"] * line["grade_unit_price"] for line in sourcing)
    assert sum(r["amount_krw"] for r in schedule) == total


# ── 봉투 ──────────────────────────────────────────────────────────────────


def test_envelope_stays_clean_with_a_payment_schedule() -> None:
    """중첩 배열이 근거 요구를 늘리지 않는다.

    봉투는 배열을 **한 겹만** 파고들므로 ``scenarios[i].payment_schedule[j]``는 대상이
    아니다 — 더 깊은 중첩의 규칙은 도메인이 정한다 (M-1 §7.1). 실측으로 확인한다.
    """
    request, reply, metadata = _run("배추", SPLIT_DAY)
    assert "payment_schedule" in _split_scenario(reply.payload)
    assert not [e for e in reply.evidences if "payment_schedule" in e.claim]
    assert validate_reply(request, reply, metadata) == ()
