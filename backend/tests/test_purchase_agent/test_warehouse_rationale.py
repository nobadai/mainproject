"""⑥ 창고 근거 — **지금 이렇다만 말하고 왜 이렇게 됐나는 말하지 않는다.**

창고는 ③(총량 클립 · ``warehouse_cap_kg``)과 ⑦(도착일 컷 · ``arrival_capacity``)이
둘 다 읽는데 근거 문장에는 한 줄도 없었다. 관통 사흘 실측에서 그 공백이 드러났다:

    날짜    cap_by_date   화면 문장
    01-05    7,645.6      창고 언급 0건
    01-06    4,058.6      창고 언급 0건   ← 승인분 3,587 만큼 줄었는데 아무도 안 말한다
    01-07    4,058.6      창고 언급 0건

⚠️ mock 재고에는 ``cap_by_date`` 도 ``guaranteed_capacity_kg`` 도 ``used_capacity_kg`` 도
  **없다.** 물류 어댑터 경로에서만 오는 값이라 ``test_arrival_capacity.py`` 와 같이
  **합성 입력**으로 시험한다. 그래서 mock 산출물은 이 항목 없이 그대로다.

🔴 **값을 대조하는 검사가 아니라 계산을 재현하는 검사다** (규칙 8). 세 입력을 주고
  나온 문장의 숫자를 직접 단언한다 — 식을 바꾸면 여기가 운다.
"""

from app.purchase_agent.nodes.package_scenarios import _warehouse_rationale

AS_OF = "2026-01-06"

#: 관통 Day2 실측값 (``THRU-20260106-BAECHU-D2B``). 세 값이 실제로 이렇게 왔다.
LIVE = {
    "guaranteed_capacity_kg": 8000.0,
    "cap_by_date": {"2026-01-08": 4058.6, "2026-01-09": 4058.6},
    "used_capacity_kg": 354.4,
}


def test_the_live_numbers_become_a_sentence() -> None:
    """🔴 실측 셋이 그날 승인 수량과 맞아떨어진다 — 그 계산을 문장으로 낸다.

    ``8,000 − 4,058.6 − 354.4 = 3,587.0`` 이고 그날 승인이 3,587kg 이었다.

    ⚠️ **그 일치는 우리가 알아본 것이지 payload 가 말해준 것이 아니다.** 그래서 문장은
      *"예정"* 까지만 적고 *"어제 승인분"* 이라고 적지 않는다.
    """
    item = _warehouse_rationale(LIVE, AS_OF)
    assert item is not None
    assert item["claim"] == "날짜별 입고 여유 4,059kg (전량 8,000kg 중 3,587kg 이 예정)"
    assert item["ref_id"] == f"CAP-{AS_OF}"
    assert item["source"] == "재고"


def test_the_window_minimum_wins_not_the_first_day() -> None:
    """🔴 **창 안 최솟값을 쓴다.**

    실측(관통 사흘)에서는 18일이 전부 같은 값이라 첫날과 구분되지 않았다. 반출 예정이
    생기면 갈리는데, 그때 첫날을 쓰면 창 뒤쪽이 더 좁은 날을 **넓다고 말하게 된다.**
    """
    uneven = {**LIVE, "cap_by_date": {"2026-01-08": 6000.0, "2026-01-09": 4058.6}}
    item = _warehouse_rationale(uneven, AS_OF)
    assert item is not None
    assert "4,059kg" in item["claim"], item["claim"]
    assert "6,000kg" not in item["claim"], "첫날 값을 쓰면 뒤쪽 좁은 날을 넓다고 말한다"


def test_a_missing_field_drops_the_whole_item() -> None:
    """🔴 셋 중 하나라도 없으면 **항목 자체를 안 만든다** (규칙 3).

    0으로 채우면 *"전량이 비어 있다"* 또는 *"전량이 찼다"* 를 사실처럼 적게 된다.
    """
    for dropped in ("guaranteed_capacity_kg", "cap_by_date", "used_capacity_kg"):
        partial = {k: v for k, v in LIVE.items() if k != dropped}
        assert _warehouse_rationale(partial, AS_OF) is None, f"{dropped} 없이 문장이 나왔다"


def test_an_empty_cap_window_is_not_a_full_warehouse() -> None:
    """빈 ``cap_by_date`` 는 *"여유가 0"* 이 아니라 *"창을 못 받았다"* 다."""
    assert _warehouse_rationale({**LIVE, "cap_by_date": {}}, AS_OF) is None


def test_a_float_slip_does_not_erase_a_confirmed_zero() -> None:
    """🔴 **0 은 확정된 0 이다** (규칙 3) — 뺄셈 오차로 항목이 사라지면 안 된다.

    승인 전인 날은 예정분이 정확히 0인데 부동소수점이 음수 쪽으로 미끄러진다. 실측::

        2026-01-05 (THRU-20260105-BAECHU)
        8000.0 − 7645.6 − 354.4 = -3.410605131648481e-13

    이 오차 하나로 **그날 항목이 통째로 사라졌다.** 승인 전과 후를 나란히 보여주는 것이
    이 문장의 목적인데, 앞쪽이 없어지면 비교가 성립하지 않는다.
    """
    day1 = {
        "guaranteed_capacity_kg": 8000.0,
        "cap_by_date": {"2026-01-07": 7645.6},
        "used_capacity_kg": 354.4,
    }
    assert day1["guaranteed_capacity_kg"] - 7645.6 - day1["used_capacity_kg"] < 0, (
        "이 검사의 전제가 깨졌다 — 뺄셈이 더 이상 음수가 아니다"
    )
    item = _warehouse_rationale(day1, AS_OF)
    assert item is not None, "확정된 0 인데 항목이 사라졌다"
    assert item["claim"] == "날짜별 입고 여유 7,646kg (전량 8,000kg 중 0kg 이 예정)"


def test_a_negative_reservation_is_not_reported() -> None:
    """세 값의 출처가 갈라진 날 — **지어내지 않고 안 적는다.**

    ``guaranteed`` 가 ``cap + used`` 보다 작으면 뺄셈이 음수가 된다. 그 상태를 *"0kg 예정"*
    으로 적으면 어긋남이 사라지고, 음수를 그대로 적으면 읽는 사람이 우리를 의심한다.
    """
    broken = {**LIVE, "guaranteed_capacity_kg": 1000.0}
    assert _warehouse_rationale(broken, AS_OF) is None


def test_the_sentence_does_not_claim_a_cause() -> None:
    """🔴 *"어제"* · *"승인"* · *"줄었다"* 를 쓰지 않는다 — 근거가 없다 (``#310``).

    ``approved_commitments`` 도 ``in_transit`` 도 봉투에 안 오고, 전날 payload 도 안 들고
    있다. 셋 중 하나라도 문장에 들어가면 **형식만 맞고 내용이 우리 것이 아닌 주장**이
    된다.
    """
    item = _warehouse_rationale(LIVE, AS_OF)
    assert item is not None
    text = item["claim"] + item["evidence_detail"]
    for forbidden in ("어제", "승인", "줄었", "감소"):
        assert forbidden not in text, f"근거 없는 낱말이 문장에 있다: {forbidden}"


def test_the_item_actually_reaches_the_rationale_list() -> None:
    """🔴 **`_rationale` 을 통과하는지 잰다** (규칙 8).

    앞의 검사들은 ``_warehouse_rationale`` 을 직접 부른다. 그것만으로는 **그 함수가
    호출되는지**를 증명하지 못한다 — 실측으로 확인했다: ``_rationale`` 에서 호출 한 줄을
    지워도 `tests/test_purchase_agent` 1,191건이 **전부 통과했다** (2026-09-06 변이 ③).

    mock 재고에 세 키가 없어 mock 산출물에는 이 항목이 안 실리므로, **합성 재고를 얹어**
    한 번 태운다.
    """
    from datetime import date

    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.package_scenarios import _rationale
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state("배추", date(2026, 8, 21))
    state["inventory"] = {**state["inventory"], **LIVE}  # type: ignore[typeddict-item]
    draft = {"daily_demand_kg": 717.0, "coverage_days": 5}

    items = _rationale(state, draft, load_constraints(), "AUC-2026-08-21")
    refs = [row["ref_id"] for row in items]
    assert "CAP-2026-08-21" in refs, f"창고 근거가 rationale 에 안 실렸다 — {refs}"
