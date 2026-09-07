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


def test_the_sentence_does_not_claim_a_cause_without_commitments() -> None:
    """🔴 **약정이 안 온 날**은 *"어제"* · *"승인"* · *"줄었다"* 를 쓰지 않는다.

    ⚠️ **금지어 목록을 줄이지 않았다** (`#312` 뒤에도). ``approved_commitments`` 는
      *"없으면 칸을 안 만든다"* 로 오는 값이라 **안 오는 날이 계속 있다.** 목록에서
      *"어제"* · *"승인"* 을 빼면 그 날을 못 잡는다 — 근거가 생긴 것은 **약정이 온
      날뿐**이고, 검사는 입력으로 갈린다.
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


# ── 승인 이력이 온 날 (`#312`) ────────────────────────────────────────────────
#
# 🟢 마스터가 `approved_commitments` 를 싣기 시작했다. 예정분 3,587kg 과 승인
#   3,587kg 이 같다는 것을 **우리가 눈으로 맞춰보던 자리**를 이제 코드가 잰다.
#
# 🔴 **대조가 맞을 때만 인과를 쓴다.** 어긋나면 종전 문장 그대로다 — 예정분에 승인분
#   아닌 점유가 섞였다는 뜻이라, 그때 "어제 승인분" 이라고 적으면 남의 물량을 우리
#   것이라고 말하게 된다.

#: 관통 Day2 실측 (`#310` 회신 · ``H1-THRU-20260105-BAECHU-1``). LIVE 의 예정분 3,587 과 맞는다.
COMMITMENT = {
    "approval_id": "H1-THRU-20260105-BAECHU-1",
    "item": "배추",
    "total_qty_kg": 3587.0,
    "arrival_schedule": [{"qty_kg": 3587.0, "arrival_date": "2026-01-07", "seq": 1}],
}


def test_a_matching_commitment_names_the_cause() -> None:
    """🟢 **이 판의 본문이다.** 대조가 맞으면 *"어제 승인분 N kg 이 D 에 옵니다"*.

    ``8,000 − 4,058.6 − 354.4 = 3,587.0`` 이고 약정 ``total_qty_kg`` 도 3,587.0 이다.
    """
    item = _warehouse_rationale(LIVE, AS_OF, [COMMITMENT], "배추")
    assert item is not None
    assert item["claim"] == "날짜별 입고 여유 4,059kg — 어제 승인분 3,587kg 이 2026-01-07 에 옵니다"


def test_the_named_quantity_is_the_commitment_not_our_arithmetic() -> None:
    """🔴 문장의 N 은 **약정이 말한 수량**이지 우리 뺄셈 결과가 아니다.

    둘이 같아야 여기까지 오지만, 적을 때는 **출처가 있는 쪽**을 적는다.
    """
    item = _warehouse_rationale(LIVE, AS_OF, [COMMITMENT], "배추")
    assert item is not None
    assert f"{COMMITMENT['total_qty_kg']:,.0f}kg" in item["claim"]


def test_a_mismatched_commitment_keeps_the_old_sentence() -> None:
    """🔴 **대조가 어긋나면 인과를 안 쓴다** — 예정분에 다른 점유가 섞인 날이다.

    승인은 3,587kg 인데 예정분이 4,000kg 이면, 413kg 은 우리가 모르는 무엇이다.
    """
    off = {**LIVE, "used_capacity_kg": 0.0}  # 예정분이 3,941.4 로 벌어진다
    item = _warehouse_rationale(off, AS_OF, [COMMITMENT], "배추")
    assert item is not None
    assert "승인" not in item["claim"], item["claim"]
    assert "이 예정)" in item["claim"]


def test_another_items_commitment_is_not_ours() -> None:
    """★ 배추 안의 근거에 무 승인을 적을 수 없다 — 수량이 우연히 맞아도 남의 것이다."""
    other = {**COMMITMENT, "item": "무"}
    item = _warehouse_rationale(LIVE, AS_OF, [other], "배추")
    assert item is not None
    assert "승인" not in item["claim"], item["claim"]


def test_several_arrival_dates_drop_the_cause() -> None:
    """⚠️ 도착일이 여러 날이면 *"언제"* 를 한 날로 못 적는다.

    수량 대조는 맞았으니 *"어제 승인분"* 까지는 사실인데, 날짜를 빼고 적으면 문장이
    *"온다"* 만 남아 **언제인지 모른다는 사실이 사라진다.** 반쯤 아는 것을 다 아는
    것처럼 적느니 종전 문장이 정확하다.
    """
    split = {
        **COMMITMENT,
        "arrival_schedule": [
            {"qty_kg": 1793.5, "arrival_date": "2026-01-07", "seq": 1},
            {"qty_kg": 1793.5, "arrival_date": "2026-01-13", "seq": 2},
        ],
    }
    item = _warehouse_rationale(LIVE, AS_OF, [split], "배추")
    assert item is not None
    assert "승인" not in item["claim"], item["claim"]


def test_two_commitments_of_the_same_item_are_summed() -> None:
    """같은 품목 약정이 둘이면 합이 예정분과 맞는지 본다 — 한 건만 보면 늘 어긋난다."""
    halves = [
        {**COMMITMENT, "approval_id": "A", "total_qty_kg": 1800.0,
         "arrival_schedule": [{"qty_kg": 1800.0, "arrival_date": "2026-01-07", "seq": 1}]},
        {**COMMITMENT, "approval_id": "B", "total_qty_kg": 1787.0,
         "arrival_schedule": [{"qty_kg": 1787.0, "arrival_date": "2026-01-07", "seq": 1}]},
    ]
    item = _warehouse_rationale(LIVE, AS_OF, halves, "배추")
    assert item is not None
    assert "어제 승인분 3,587kg 이 2026-01-07 에 옵니다" in item["claim"]


def test_shrinkage_words_are_never_allowed() -> None:
    """🔴 *"줄었"* · *"감소"* 는 **약정이 와도** 못 쓴다.

    전날 payload 를 안 들고 있어 비교 대상이 없다. 우리가 말할 수 있는 것은
    *"이만큼이 이 날 온다"* 이지 *"이만큼 줄었다"* 가 아니다.
    """
    for commitments in ([COMMITMENT], None):
        item = _warehouse_rationale(LIVE, AS_OF, commitments, "배추")
        assert item is not None
        text = item["claim"] + item["evidence_detail"]
        for forbidden in ("줄었", "감소"):
            assert forbidden not in text, f"비교 대상이 없는데 {forbidden} 를 썼다"


def test_the_cause_actually_reaches_the_rationale_list() -> None:
    """🔴 **`_rationale` 을 통과하는지 잰다** (규칙 8).

    위 검사들은 ``_warehouse_rationale`` 을 직접 부른다 — 그것만으로는 **승인 이력이
    거기까지 전달되는지**를 증명하지 못한다. ``_rationale`` 이 ``state`` 에서 약정을
    꺼내 넘기는 그 줄을 지우면 여기가 운다.
    """
    from datetime import date

    from app.purchase_agent.config import load_constraints
    from app.purchase_agent.nodes.package_scenarios import _rationale
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state("배추", date(2026, 8, 21))
    state["inventory"] = {**state["inventory"], **LIVE}  # type: ignore[typeddict-item]
    state["approved_commitments"] = [COMMITMENT]
    draft = {"daily_demand_kg": 717.0, "coverage_days": 5}

    items = _rationale(state, draft, load_constraints(), "AUC-2026-08-21")
    cap = next(row for row in items if row["ref_id"] == "CAP-2026-08-21")
    assert "어제 승인분 3,587kg" in cap["claim"], cap["claim"]
