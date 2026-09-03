"""⑦ 날짜별 창고 여유 컷 (#93 · §4-⑦).

⑥ ``cap_constrained_quantities`` 는 **재배분**한다 — 회차 사이로 물량을 옮기되 총합은
그대로다. 맞출 수 없으면 균등 분할을 그대로 내보냈고, **컷은 아무도 안 했다.**
여유 100kg 에 7,714kg 을 넣는 계획이 risks 고지만 달고 나갔다.

🔴 **이 파일의 급소는 일괄 안이다.** ⑥의 재배분도 고지도 timing 축 분할 진입
  경로에서만 돌아서(``materialize_split`` 의 ``if not chosen: return``), 회차를 안 나눈
  안은 **검사 자체를 안 탔다.** 컷을 분할 안에만 넣으면 "회차를 안 나눌수록 검사를
  안 받는" 구조가 된다 — 그래서 ⑦은 ``chosen``·``axis``·``entered`` 를 안 본다.

⚠️ mock 재고에는 ``cap_by_date`` 도 ``inbound_lead_days`` 도 없다. 물류 어댑터 경로에서만
  오는 값이라 전부 **합성 입력**으로 시험한다.
"""

from datetime import date

import pytest

from app.master.envelope import AgentRequest, ExecutionContext
from app.purchase_agent import ports
from app.purchase_agent.adapter import purchase_port
from app.purchase_agent.nodes.self_check import (
    arrival_capacity,
    cap_window,
    check_arrival_capacity,
)

AS_OF = date(2025, 12, 31)
ITEM = "배추"

#: #93 재현 세팅 — 재무 상한을 올려 **창고만 남긴다**. 그러지 않으면 현금이 먼저 컷해
#: 날짜 축이 한 번도 안 밟힌다.
HUGE_CASH = 10**12


def _payload(*, cap_by_date=None, lead=None, cash=HUGE_CASH) -> dict:
    inventory = dict(ports.get_inventory(ITEM, AS_OF))
    if lead is not None:
        inventory["inbound_lead_days"] = lead
    if cap_by_date is not None:
        inventory["cap_by_date"] = cap_by_date
    extras = ports.get_snapshot_extras(ITEM, AS_OF)
    return {
        "item": ITEM,
        "constraints": {
            "finance": {
                "base_projected_cash_min": ports.get_projected_cash_min(AS_OF, 30),
                "margin_defense_floor_rate": 0.267,
                "finance_cap_amount_krw": cash,
                "purchase_payment_days": 7,
                "critical_payment_dates": [],
            },
            "inventory": inventory,
        },
        "forecast": ports.get_forecast(ITEM, AS_OF),
        "confirmed_orders": ports.get_confirmed_orders(ITEM, AS_OF, days=14),
        "policy_values": {
            "contract_price_krw": extras["contract_price"],
            "item_mix_ratio": extras["item_mix_ratio"],
        },
    }


def _proposal(**over) -> dict:
    request = AgentRequest(
        context=ExecutionContext("REQ-93", AS_OF, "ML_COMPLETE", "v2.3"),
        agent="purchase",
        mode="GENERATE_SCENARIOS",
        payload=_payload(**over),
    )
    return purchase_port(request)[0].payload


#: 2026년 1월 전 날짜의 여유. 값을 바꿔가며 쓴다.
def _caps(kg: float) -> dict[str, float]:
    return {f"2026-01-{day:02d}": kg for day in range(1, 32)}


#: 단위 검사용 최소 안. ⑦은 ``split_plan`` 과 ``inventory`` 만 본다.
def _scenario(*rounds: tuple[int, str, int]) -> dict:
    return {
        "label": "기본",
        "split_plan": [
            {"seq": seq, "date": "2025-12-31", "qty_kg": qty, "expected_arrival_date": eta}
            for seq, eta, qty in rounds
        ],
    }


def _state(cap_by_date=None, *, lead: int | None = None, window: int | None = None) -> dict:
    """단위 검사용 최소 State. ``lead``·``window`` 를 주면 계산 구간을 만들 수 있다.

    ⚠️ 둘은 **물류가 같은 payload 에 함께** 싣는 값이라 여기서도 같은 dict 에 둔다 —
      한쪽만 주면 창을 못 만드는 경로(``cap_window`` → ``None``)를 시험하게 된다.
    """
    inventory: dict = {}
    if cap_by_date is not None:
        inventory["cap_by_date"] = cap_by_date
    if lead is not None:
        inventory["inbound_lead_days"] = lead
    if window is not None:
        inventory["cap_by_date_window_days"] = window
    return {"inventory": inventory, "date": "2025-12-31"}  # type: ignore[return-value]


# ── #93 재현: 컷이 실제로 난다 ────────────────────────────────────────────


def test_over_capacity_split_scenario_is_cut() -> None:
    """🔴 여유 100kg 에 7,714kg — **전에는 통과했다.**

    이슈 본문의 재현 그대로다: 살아남은 안 3 · 컷 0 이었다.
    """
    proposal = _proposal(cap_by_date=_caps(100), lead=2)
    labels = [s["label"] for s in proposal.get("scenarios", [])]
    assert labels == [], f"여유 100kg 인데 살아남은 안이 있다: {labels}"

    reasons = {r["label"]: r["reason"] for r in proposal["rejected_reasons"]}
    assert set(reasons) == {"보수", "기본", "공격"}
    for label, reason in reasons.items():
        assert "날짜별 창고 초과" in reason, f"{label}: {reason}"


def test_bulk_scenario_is_cut_too() -> None:
    """🔴 **이번 작업의 핵심** — 일괄(1회차) 안도 컷된다.

    보수 2,571kg · 기본 6,429kg 은 회차가 하나라 ⑥의 재배분 경로를 안 탔고,
    그래서 risks 줄조차 없이 나갔다. 분할 안만 컷하면 **회차를 안 나눌수록 검사를
    안 받는** 구조가 된다.
    """
    proposal = _proposal(cap_by_date=_caps(100), lead=2)
    reasons = {r["label"]: r["reason"] for r in proposal["rejected_reasons"]}

    for label in ("보수", "기본"):
        assert label in reasons, f"{label} 안(1회차)이 컷되지 않았다"
        assert "날짜별 창고 초과" in reasons[label]
        # 1회차뿐이므로 누적 = 그 회차 수량이다
        assert "1회차" in reasons[label], reasons[label]


def test_a_scenario_that_fits_survives() -> None:
    """넉넉하면 통과한다 — 검사가 무조건 컷하는 것이 아님을 잠근다."""
    proposal = _proposal(cap_by_date=_caps(10**9), lead=2)
    assert proposal["scenarios"], "여유가 충분한데 컷됐다"
    assert not [
        r for r in proposal["rejected_reasons"] if "날짜별 창고 초과" in r["reason"]
    ]


def test_total_passes_but_a_date_does_not() -> None:
    """🔴 **총량 축은 통과인데 날짜 축에서 걸린다** — 두 검사가 다른 것을 본다.

    창고 여유 총량(12,000 + 임차 3,600)에는 드는 안이, 하루치 여유에는 안 든다.
    총량만 보던 시절 이 안이 그대로 나갔다.
    """
    proposal = _proposal(cap_by_date=_caps(2_000), lead=2)
    reasons = {r["label"]: r["reason"] for r in proposal["rejected_reasons"]}
    assert reasons, "날짜 축에서 걸리는 안이 하나도 없으면 검사가 헛돈다"
    for label, reason in reasons.items():
        assert "날짜별 창고 초과" in reason, f"{label} 이 총량 축에서 먼저 걸렸다: {reason}"


# ── 누적으로 본다 ─────────────────────────────────────────────────────────


def test_earlier_rounds_still_occupy_the_warehouse() -> None:
    """앞 회차가 아직 창고에 있다.

    ``cap_by_date[d]`` 는 그날의 여유인데 물류는 **기존 일정만** 재생해 그 값을 낸다 —
    우리가 새로 넣을 회차는 거기 없다. 날짜마다 독립으로 보면 아래가 통과한다.
    """
    scenario = _scenario((1, "2026-01-02", 60), (2, "2026-01-08", 60))
    caps = {"2026-01-02": 100, "2026-01-08": 100}

    # 회차별로만 보면 60 ≤ 100 · 60 ≤ 100 → 통과. 누적이면 120 > 100 → 컷.
    violation = check_arrival_capacity(scenario, _state(caps))  # type: ignore[arg-type]
    assert violation is not None, "누적하지 않으면 이 계획이 통과한다"
    assert "2026-01-08" in violation
    assert "120" in violation


def test_the_first_round_alone_can_bust_it() -> None:
    """1회차에서 이미 넘으면 거기서 컷한다 — 뒤를 더 보지 않는다."""
    scenario = _scenario((1, "2026-01-02", 500), (2, "2026-01-08", 1))
    violation = check_arrival_capacity(scenario, _state({"2026-01-02": 100, "2026-01-08": 10**9}))  # type: ignore[arg-type]
    assert violation and "2026-01-02" in violation


# ── 4갈래: 앞 셋은 컷하지 않고 고지만 ──────────────────────────────────────


def test_no_cap_is_skipped_not_cut() -> None:
    """물류가 날짜별 여유를 안 줬다 — 검사를 안 한 것이지 위반이 아니다 (규칙 3)."""
    verdict = arrival_capacity(_scenario((1, "2026-01-02", 10**9)), _state())  # type: ignore[arg-type]
    assert verdict.violation is None
    assert verdict.skipped and "받지 못했다" in verdict.skipped


def test_no_lead_is_skipped_not_cut() -> None:
    """N4 미결이면 ⑥이 도착일을 ``None`` 으로 채운다 — 0으로 읽지 않는다."""
    scenario = {
        "label": "기본",
        "split_plan": [
            {"seq": 1, "date": "2025-12-31", "qty_kg": 10**9, "expected_arrival_date": None}
        ],
    }
    verdict = arrival_capacity(scenario, _state(_caps(100)))  # type: ignore[arg-type]
    assert verdict.violation is None
    assert verdict.skipped and "입고 소요일이 정해지지 않아" in verdict.skipped


def test_unknown_date_is_skipped_not_cut() -> None:
    """🔴 창 밖은 0이 아니라 "안 봤다"다.

    ``.get(d, 0)`` 으로 읽으면 조회 기간 밖 도착일이 **여유 0** 이 되어 통째로 컷된다.
    하나라도 모르면 아무 회차도 판정하지 않는다.
    """
    scenario = _scenario((1, "2026-01-02", 50), (2, "2026-01-08", 50))
    verdict = arrival_capacity(scenario, _state({"2026-01-02": 10}))  # type: ignore[arg-type]
    assert verdict.violation is None, "창 밖 회차를 여유 0으로 눌러 컷하면 안 된다"
    assert verdict.skipped and "2026-01-08" in verdict.skipped


def test_zero_capacity_inside_the_window_does_cut() -> None:
    """**있는 0**과 **없는 값**은 다르다 — 0은 판정 대상이다."""
    scenario = _scenario((1, "2026-01-02", 50))
    verdict = arrival_capacity(scenario, _state({"2026-01-02": 0}))  # type: ignore[arg-type]
    assert verdict.violation is not None, "여유 0은 확정된 값이라 미검사가 아니다"
    assert verdict.skipped is None


def test_skipped_notice_reaches_every_scenario() -> None:
    """미검사 고지가 **안마다** 나간다 — ⑥ 시절에는 timing 축 안에만 붙었다."""
    proposal = _proposal()  # cap_by_date 없음 = mock 경로
    assert proposal["scenarios"]
    for scenario in proposal["scenarios"]:
        assert [note for note in scenario["risks"] if "검사를 하지 않았다" in note], (
            f"{scenario['label']}: {scenario['risks']}"
        )


def test_a_cut_scenario_does_not_also_claim_it_was_unchecked() -> None:
    """컷과 미검사는 배타다 — 죽은 안에 "검사 안 했다"가 붙으면 둘 다 못 읽는다."""
    verdict = arrival_capacity(_scenario((1, "2026-01-02", 500)), _state({"2026-01-02": 100}))  # type: ignore[arg-type]
    assert verdict.violation is not None
    assert verdict.skipped is None


# ── 창 밖 vs 창 안 누락 (물류 규약 2026-09-03) ────────────────────────────
#
# 물류 규약은 셋을 다른 상태로 규정한다:
#   키 존재 + 값 0     계산 결과 입고 가능량이 0        → 판정 대상
#   창 안인데 키 누락   계산 누락 또는 미결              → **고쳐야 할 것**
#   창 밖             계산 대상이 아니다               → **정상**
#
# 전에는 뒤 둘을 한 문장으로 뭉쳐 냈다. 행동(컷 안 함)은 같아도 **읽는 사람이
# 고쳐야 할 것과 정상을 구분할 수 없었다.**

#: N4=2 · 창 18일 → 2026-01-02 ~ 2026-01-19 (물류 build_cap_window 와 같은 식)
LEAD = 2
WINDOW = 18


def test_the_window_matches_what_logistics_built() -> None:
    """🔴 물류 ``build_cap_window`` 와 **같은 창**을 만드는지.

    저쪽은 ``as_of + lead`` 부터 ``range(window_days)`` 라 **끝이 start + (n-1)** 이다.
    off-by-one 이 나면 경계 하루가 통째로 갈래를 바꾼다.
    """
    from datetime import timedelta

    from app.logistics.tools import build_cap_window

    state = _state({}, lead=LEAD, window=WINDOW)
    ours = cap_window(state)  # type: ignore[arg-type]
    assert ours == ("2026-01-02", "2026-01-19")

    class _Snap:
        inbound_lead_days = LEAD

    theirs = build_cap_window(_Snap(), date(2025, 12, 31))  # type: ignore[arg-type]
    assert theirs is not None
    assert len(theirs) == WINDOW
    assert (theirs[0].isoformat(), theirs[-1].isoformat()) == ours
    assert theirs[-1] == theirs[0] + timedelta(days=WINDOW - 1)


@pytest.mark.parametrize("lead", [2, 2.0], ids=["int", "float"])
def test_the_window_reads_the_shape_logistics_actually_sends(lead) -> None:
    """🔴 **실 payload 의 ``inbound_lead_days`` 는 ``2.0`` 이다** (실측 2026-09-03).

    물류가 숫자를 ``_num()`` 으로 싸서 보내므로 float 로 온다. ``isinstance(v, int)``
    로 보면 **실운영에서 창을 한 번도 못 만들고** 늘 "가르지 못했다"로 떨어진다 —
    검사가 있는데 안 도는 상태이고, 단위 검사가 int 만 쓰면 아무도 모른다.

    ``adapter._arrival_input_problems`` 도 float 를 통과시킨다 — 두 곳이 갈리면
    어댑터가 받은 값을 여기서 버린다.
    """
    assert cap_window(_state({}, lead=lead, window=WINDOW)) == (  # type: ignore[arg-type]
        "2026-01-02",
        "2026-01-19",
    )


def test_a_fractional_lead_does_not_build_a_window() -> None:
    """소수 리드타임은 일 단위가 아니다 — 반올림해 창을 지어내지 않는다 (규칙 3)."""
    assert cap_window(_state({}, lead=2.5, window=WINDOW)) is None  # type: ignore[arg-type]


def test_outside_the_window_is_not_a_gap() -> None:
    """창 밖 — **계산 대상이 아니다.** 빠진 값이 아니라 정상이다."""
    scenario = _scenario((1, "2026-01-02", 50), (2, "2026-01-25", 50))  # 01-25 는 창 밖
    verdict = arrival_capacity(
        scenario, _state({"2026-01-02": 10_000}, lead=LEAD, window=WINDOW)  # type: ignore[arg-type]
    )
    assert verdict.violation is None, "창 밖을 여유 0으로 읽어 컷하면 안 된다"
    assert verdict.skipped
    assert "계산 구간 밖이라 애초에 대상이 아니다" in verdict.skipped
    assert "2026-01-25" in verdict.skipped
    assert "2026-01-02~2026-01-19" in verdict.skipped, "구간을 밝혀야 읽는 사람이 확인한다"


def test_a_gap_inside_the_window_asks_for_a_fix() -> None:
    """창 안 누락 — **고쳐야 할 것.** 계산이 빠졌거나 아직 안 정해졌다."""
    scenario = _scenario((1, "2026-01-02", 50), (2, "2026-01-08", 50))  # 01-08 은 창 안
    verdict = arrival_capacity(
        scenario, _state({"2026-01-02": 10_000}, lead=LEAD, window=WINDOW)  # type: ignore[arg-type]
    )
    assert verdict.violation is None
    assert verdict.skipped
    assert "계산 구간 안인데 여유 값이 오지 않았다" in verdict.skipped
    assert "확인이 필요하다" in verdict.skipped
    assert "2026-01-08" in verdict.skipped


def test_the_two_gaps_do_not_read_the_same() -> None:
    """🔴 **이 작업의 급소** — 둘의 문면이 실제로 갈리는지.

    행동은 같다(둘 다 컷 안 함). 갈리는 것은 문장뿐이라, 문장이 같으면 이 작업은
    아무것도 안 한 것이 된다.
    """
    caps = {"2026-01-02": 10_000}
    outside = arrival_capacity(
        _scenario((1, "2026-01-02", 50), (2, "2026-01-25", 50)),
        _state(caps, lead=LEAD, window=WINDOW),  # type: ignore[arg-type]
    ).skipped
    inside = arrival_capacity(
        _scenario((1, "2026-01-02", 50), (2, "2026-01-08", 50)),
        _state(caps, lead=LEAD, window=WINDOW),  # type: ignore[arg-type]
    ).skipped

    assert outside and inside
    assert outside != inside, "창 밖과 창 안 누락이 같은 문장으로 나간다"
    # 한쪽에만 있어야 하는 말 — 뭉쳐 쓰면 양쪽에 다 들어간다
    assert "확인이 필요하다" in inside and "확인이 필요하다" not in outside
    assert "애초에 대상이 아니다" in outside and "애초에 대상이 아니다" not in inside


def test_both_kinds_at_once_name_both() -> None:
    """둘이 함께 오면 **둘 다 짚는다** — 급한 쪽(누락)이 먼저다."""
    scenario = _scenario(
        (1, "2026-01-02", 50), (2, "2026-01-08", 50), (3, "2026-01-25", 50)
    )
    verdict = arrival_capacity(
        scenario, _state({"2026-01-02": 10_000}, lead=LEAD, window=WINDOW)  # type: ignore[arg-type]
    )
    assert verdict.skipped
    assert "2026-01-08" in verdict.skipped and "2026-01-25" in verdict.skipped
    assert verdict.skipped.index("2026-01-08") < verdict.skipped.index("2026-01-25")


@pytest.mark.parametrize(
    ("lead", "window"),
    [(None, WINDOW), (LEAD, None), (None, None)],
)
def test_without_the_window_we_say_we_could_not_tell(lead, window) -> None:
    """창을 못 만들면 **가르지 못했다는 사실을 밝힌다** — 지어내지 않는다 (규칙 3).

    물류가 창 길이를 payload 에 싣기로 했으므로(2026-09-03), 없으면 그것이 확인할
    거리다. 조용히 옛 문장으로 돌아가면 아무도 안 본다.
    """
    verdict = arrival_capacity(
        _scenario((1, "2026-01-02", 50), (2, "2026-01-25", 50)),
        _state({"2026-01-02": 10_000}, lead=lead, window=window),  # type: ignore[arg-type]
    )
    assert verdict.violation is None
    assert verdict.skipped
    assert "가르지 못했다" in verdict.skipped
    assert "2026-01-25" in verdict.skipped


def test_a_zero_inside_the_window_is_still_judged() -> None:
    """창을 알아도 **있는 0** 은 판정 대상이다 — 갈래가 늘어도 그건 안 바뀐다."""
    verdict = arrival_capacity(
        _scenario((1, "2026-01-02", 50)),
        _state({"2026-01-02": 0}, lead=LEAD, window=WINDOW),  # type: ignore[arg-type]
    )
    assert verdict.violation is not None
    assert verdict.skipped is None


# ── 고지 문면 ─────────────────────────────────────────────────────────────


def test_notices_survive_the_output_wording_ban() -> None:
    """사람이 읽는 자리이므로 내부 용어가 없어야 한다 (#164 · #167)."""
    from tests.test_purchase_agent.test_output_wording import BANNED

    proposal = _proposal()
    lines = [note for s in proposal["scenarios"] for note in s["risks"]]
    assert lines
    for line in lines:
        for word in BANNED:
            assert word not in line, f"고지에 내부 용어 {word!r}: {line}"


@pytest.mark.parametrize("cap", [100, 2_000])
def test_cut_reason_survives_the_output_wording_ban(cap: int) -> None:
    """컷 사유도 사람이 읽는다 — ``rejected_reasons`` 는 화면 1행으로 간다."""
    from tests.test_purchase_agent.test_output_wording import BANNED

    proposal = _proposal(cap_by_date=_caps(cap), lead=2)
    reasons = [r["reason"] for r in proposal["rejected_reasons"]]
    assert reasons
    for reason in reasons:
        for word in BANNED:
            assert word not in reason, f"컷 사유에 내부 용어 {word!r}: {reason}"
