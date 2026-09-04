"""승인 → 확정 입고 약정 (H1). **품목을 잃지 않는다.**

🔴 **실측 2026-09-01 — 오케 경로가 품목을 없애고 있었다.**

```python
# orchestrator/cycle.py:311
ArrivalLeg(qty_kg=sum(leg.qty_kg.values()), ...)      # ← 품목이 여기서 사라진다
```

그래서 물류 H1 이 총 kg 으로만 계산했고, *"배추 출고가 양파 재고를 대신 소진한다"* 가
났다 (물류 질의 §1).

★ **오케를 부르지 않고 마스터가 만든다** (지시 2026-09-01 · ⓐ).
  `tests/master/test_no_orchestrator_runtime.py` 가 그 방향을 잠근다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.master.commitment import (
    ApprovedCommitment,
    ArrivalLeg,
    CommitmentNotBuildable,
    build_commitment,
)

AS_OF = date(2025, 12, 31)


def _scenario(**over):
    base = {
        "label": "보수",
        "total_qty_kg": 44.0,
        "total_amount_krw": 228800.0,
        "split_plan": [{"seq": 1, "date": "2025-12-31", "qty_kg": 44.0}],
    }
    base.update(over)
    return base


def _build(**over):
    kw = {
        "request_id": "REQ-1",
        "as_of": AS_OF,
        "item": "배추",
        "scenario": _scenario(),
        "inbound_lead_days": 2.0,
        "decision_seq": 1,
    }
    kw.update(over)
    return build_commitment(**kw)


# ---------------------------------------------------------------------------
# 품목 — 이 모듈이 존재하는 이유
# ---------------------------------------------------------------------------


def test_회차마다_품목이_붙는다():
    commitment = _build()

    assert commitment.item == "배추"
    assert [leg.item for leg in commitment.arrival_schedule] == ["배추"]


def test_품목_어휘를_값으로_막는다():
    """🔴 `orchestrator` 의 `ItemCode` 는 `str` 별칭이라 `"(승인분)"` 도 통과한다.

    실제로 `contracts_core.py:975` 가 그 값을 품목 자리에 넣고 있다. 여기서는 막는다.
    """
    with pytest.raises(CommitmentNotBuildable, match="계약 품목이 아니다"):
        _build(item="(승인분)")

    with pytest.raises(CommitmentNotBuildable, match="계약 품목이 아니다"):
        _build(item="딸기")


def test_품목이_없으면_지어내지_않는다():
    with pytest.raises(CommitmentNotBuildable, match="품목"):
        _build(item=None)


# ---------------------------------------------------------------------------
# 도착일 — 매입일 + N4
# ---------------------------------------------------------------------------


def test_도착일은_매입일에_리드타임을_더한다():
    """★ 안의 `date` 는 **매입 실행일**이지 도착일이 아니다 (매입 IO명세 §4)."""
    commitment = _build(inbound_lead_days=2.0)
    leg = commitment.arrival_schedule[0]

    assert leg.purchase_date == date(2025, 12, 31)
    assert leg.arrival_date == date(2026, 1, 2)
    assert commitment.first_arrival == date(2026, 1, 2)


def test_분할이_여러_회차면_회차마다_따로_계산한다():
    commitment = _build(
        scenario=_scenario(
            total_qty_kg=100.0,
            split_plan=[
                {"seq": 1, "date": "2025-12-31", "qty_kg": 60.0},
                {"seq": 2, "date": "2026-01-06", "qty_kg": 40.0},
            ],
        )
    )

    assert [leg.arrival_date for leg in commitment.arrival_schedule] == [
        date(2026, 1, 2),
        date(2026, 1, 8),
    ]
    assert commitment.first_arrival == date(2026, 1, 2)


def test_N4_가_없으면_일정을_만들지_않고_이유를_적는다():
    """🔴 0 으로 대체하면 *"오늘 승인분이 오늘 도착"* 이 된다 (§1.2-10 · §3.2.3)."""
    commitment = _build(inbound_lead_days=None)

    assert commitment.arrival_schedule == ()
    assert commitment.first_arrival is None
    assert any("N4" in note for note in commitment.notes), commitment.notes
    # ★ 약정 자체는 선다 — 승인한 사실이 일정 조립 실패로 지워지지 않는다.
    assert commitment.total_qty_kg == 44.0


def test_회차에_값이_없으면_반쪽_일정을_만들지_않는다():
    commitment = _build(scenario=_scenario(split_plan=[{"seq": 1, "date": "2025-12-31"}]))

    assert commitment.arrival_schedule == ()
    assert commitment.notes


# ---------------------------------------------------------------------------
# 항등 — 마스터가 맞춰 주지 않는다
# ---------------------------------------------------------------------------


def test_회차_합이_총량과_어긋나면_막는다():
    """★ 마스터가 둘 중 하나를 고쳐 맞추면 **고친 값이 근거가 된다.**"""
    with pytest.raises(CommitmentNotBuildable, match="어긋난다"):
        _build(
            scenario=_scenario(
                total_qty_kg=100.0,
                split_plan=[{"seq": 1, "date": "2025-12-31", "qty_kg": 44.0}],
            )
        )


def test_회차_품목이_다르면_막는다():
    with pytest.raises(CommitmentNotBuildable, match="회차 품목"):
        ApprovedCommitment(
            approval_id="H1-x-1",
            request_id="REQ-1",
            as_of=AS_OF,
            item="무",
            scenario_label="보수",
            total_qty_kg=44.0,
            total_amount_krw=1.0,
            arrival_schedule=(ArrivalLeg("배추", 44.0, date(2026, 1, 2), date(2025, 12, 31), 1),),
        )


def test_총량이나_총액이_없으면_막는다():
    with pytest.raises(CommitmentNotBuildable, match="총량"):
        _build(scenario=_scenario(total_qty_kg=None))
    with pytest.raises(CommitmentNotBuildable, match="총액"):
        _build(scenario=_scenario(total_amount_krw=None))


def test_bool_은_숫자가_아니다():
    """`True` 가 1.0 으로 새면 수량 1kg 짜리 약정이 조용히 선다."""
    with pytest.raises(CommitmentNotBuildable, match="총량"):
        _build(scenario=_scenario(total_qty_kg=True))


# ---------------------------------------------------------------------------
# 오케를 안 부른다
# ---------------------------------------------------------------------------


def test_오케를_import_하지_않는다():
    """★ ⓐ 의 요지 — 같은 변환을 오케에서 가져오지 않는다."""
    import ast
    from pathlib import Path

    import app.master.commitment as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not any(m.startswith("app.orchestrator") for m in modules), modules


# ---------------------------------------------------------------------------
# 자기 리뷰에서 나온 셋 (2026-09-01)
# ---------------------------------------------------------------------------


def test_소수_리드타임은_자르지_않고_일정을_안_만든다():
    """🔴 처음엔 `int(lead)` 로 잘랐다 — 2.9 가 조용히 2일이 됐다."""
    commitment = _build(inbound_lead_days=2.9)

    assert commitment.arrival_schedule == ()
    assert any("일수로 읽히지 않아" in note for note in commitment.notes), commitment.notes


def test_음수_리드타임은_과거_도착을_만들지_않는다():
    """🔴 -1 이 **매입일보다 과거 도착**을 만들었다 — 점유가 하루 이르게 계산된다."""
    commitment = _build(inbound_lead_days=-1)

    assert commitment.arrival_schedule == ()
    assert commitment.notes


def _purchase_item_names() -> frozenset[str]:
    from typing import get_args

    from app.purchase_agent.schemas import ItemName

    return frozenset(get_args(ItemName))


def test_계약_품목은_전부_매입이_받을_수_있다():
    """🔴 **이것이 안전 속성이다.** 마스터가 보낼 수 있는 품목을 매입이 못 받으면
    그 요청은 문 앞에서 죽는다.

    ⚠️ 반대 방향(매입이 더 넓은 것)은 사고가 아니다 — 마스터가 안 보내면 그만이다.
      그래서 같음이 아니라 **포함**으로 건다.
    """
    from app.master.commitment import ITEM_CODES

    assert ITEM_CODES <= _purchase_item_names()


def test_매입이_더_넓은_것은_전환이_아니라_결정이다():
    """🔴 **여기를 "끝내려" 하지 마라.** 차이가 남는 것이 결정이다.

    처음에 이 검사를 *"전환이 끝나면 같음으로 좁힌다"* 로 썼다. **틀렸다** —
    매입이 좁히지 않기로 했고 그것이 맞는 판단이다 (매입 회신 2026-09-03).

    ```text
    피마늘은 mock 에서 "규격 미확정 → 0안" 경로를 타는 유일한 품목이다
      constraints.yaml:284  피마늘: null   (배추 18kg · 양파 15kg 은 값이 있다)
      quotes.py:371         그 null 분기
      adapter.py:895        "12-31 피마늘이 그 모양" 을 payload 설계 근거로 인용
    ```

    `#57` 의 *"ML 3품목 + mock 4품목 시연용 유지"* 가 **한 쌍으로 선 결정**이라,
    mock 을 좁히면 그 후반부를 뒤집는다.

    ★ **대신 마스터가 문 앞에서 막는다** (`#223` ·
      `tests/master/test_item_gate_at_the_door.py`). 계약 밖 품목이 매입에
      도달하지 않으므로 매입이 넓어도 안전하다.

    ⚠️ 그래서 이 검사는 `skip` 으로 끝나지 않는다. **차이가 정확히 피마늘 하나인지**
      만 본다 — 다른 품목이 끼면 그건 결정이 아니라 사고다.
    """
    from app.master.commitment import ITEM_CODES

    extra = _purchase_item_names() - ITEM_CODES

    assert extra == {"피마늘"}, (
        f"매입과 계약의 차이가 피마늘 하나가 아니다: {sorted(extra)}. "
        f"넓은 것 자체는 사고가 아니지만 무엇이 넓은지는 알고 있어야 한다"
    )


# ---------------------------------------------------------------------------
# 매입이 도착일을 실어 주면 계산하지 않는다 (매입 회신 2026-09-01)
# ---------------------------------------------------------------------------


def test_매입이_실은_도착일을_그대로_쓴다():
    """★ 매입 값이 N4 계산과 **달라도** 매입 값이 이긴다 — 마스터는 재계산하지 않는다."""
    commitment = _build(
        scenario=_scenario(
            split_plan=[
                {
                    "seq": 1,
                    "date": "2025-12-31",
                    "qty_kg": 44.0,
                    "expected_arrival_date": "2026-01-05",
                }  # N4=2 계산이면 01-02
            ]
        ),
        inbound_lead_days=2.0,
    )

    assert commitment.arrival_schedule[0].arrival_date == date(2026, 1, 5)


def test_매입_도착일이_전부_있으면_N4_없이도_일정이_선다():
    """N4 미결인 날에도 매입이 값을 냈으면 그 사실을 버리지 않는다."""
    commitment = _build(
        scenario=_scenario(
            split_plan=[
                {
                    "seq": 1,
                    "date": "2025-12-31",
                    "qty_kg": 44.0,
                    "expected_arrival_date": "2026-01-02",
                }
            ]
        ),
        inbound_lead_days=None,
    )

    assert commitment.arrival_schedule[0].arrival_date == date(2026, 1, 2)
    assert commitment.notes == ()


def test_도착일이_null_이면_매입도_못_낸_것이라_계산하지_않는다():
    """★ null = "N4 미결로 매입도 못 냈다". 같은 N4 로 마스터가 대신 계산하면
    **두 곳 계산이 재현**된다 — N4 도 없으면 일정 없이 사유만 남는다."""
    commitment = _build(
        scenario=_scenario(
            split_plan=[
                {"seq": 1, "date": "2025-12-31", "qty_kg": 44.0, "expected_arrival_date": None}
            ]
        ),
        inbound_lead_days=None,
    )

    assert commitment.arrival_schedule == ()
    assert any("N4" in note for note in commitment.notes)


# ---------------------------------------------------------------------------
# 매입 #141 제보 셋 (2026-09-01) — 부분 공급 · 과거 도착일
# ---------------------------------------------------------------------------


def _two_legs(first_eta, second_eta):
    plan = [
        {"seq": 1, "date": "2025-12-31", "qty_kg": 60.0},
        {"seq": 2, "date": "2026-01-06", "qty_kg": 40.0},
    ]
    if first_eta is not ...:
        plan[0]["expected_arrival_date"] = first_eta
    if second_eta is not ...:
        plan[1]["expected_arrival_date"] = second_eta
    return _scenario(total_qty_kg=100.0, split_plan=plan)


def test_부분_공급은_출처를_섞지_않는다():
    """🔴 전에는 실린 회차=매입값 · 빈 회차=마스터 계산으로 섞인 일정이 나갔다."""
    commitment = _build(scenario=_two_legs("2026-01-05", ...), inbound_lead_days=2.0)

    assert commitment.arrival_schedule == ()
    assert any("섞어" in note for note in commitment.notes), commitment.notes


def test_부분_공급에_N4_까지_없으면_사유가_정확하다():
    """🔴 전에는 실린 1회차 값마저 "N4 가 없어" 라는 틀린 사유로 버려졌다 (매입 제보)."""
    commitment = _build(scenario=_two_legs("2026-01-05", None), inbound_lead_days=None)

    assert commitment.arrival_schedule == ()
    assert any("1회차만 실려" in note or "2회차 중 1회차" in note for note in commitment.notes), (
        commitment.notes
    )
    assert not any("N4" in note for note in commitment.notes), "사유가 원인을 잘못 가리킨다"


def test_받은_도착일이_매입일보다_앞서면_막는다():
    """마스터 계산은 lead<0 을 막으면서 수신 값은 안 보고 있었다 (매입 참고 ③)."""
    with pytest.raises(CommitmentNotBuildable, match="앞선다"):
        _build(
            scenario=_scenario(
                split_plan=[
                    {
                        "seq": 1,
                        "date": "2025-12-31",
                        "qty_kg": 44.0,
                        "expected_arrival_date": "2025-12-28",
                    }
                ]
            )
        )


# ---------------------------------------------------------------------------
# 회차 금액 (v0.4) — 총액의 **분해**이지 새 축이 아니다
#
# 매입이 회차별 금액을 보내면 마스터가 원장에 쓴다.
#
#     purchases.total_amount_krw     NOT NULL    회차마다 한 행
#     purchase_items.line_amount_krw NOT NULL    회차·품목마다 한 줄
#
# `Σ 회차금액 ≠ total_amount_krw` 여도 전에는 아무도 안 봤다.
# ★ 금액은 선택 필드다 — 매입이 아직 안 보내므로 오늘은 늘 비어 있고, 그때는 검사가
#   통째로 건너뛴다. 값이 오는 날부터 걸린다.
# ---------------------------------------------------------------------------


def _amount_legs(first_amount, second_amount, total_amount=228800.0):
    plan = [
        {"seq": 1, "date": "2025-12-31", "qty_kg": 60.0},
        {"seq": 2, "date": "2026-01-06", "qty_kg": 40.0},
    ]
    if first_amount is not ...:
        plan[0]["amount_krw"] = first_amount
    if second_amount is not ...:
        plan[1]["amount_krw"] = second_amount
    return _scenario(total_qty_kg=100.0, total_amount_krw=total_amount, split_plan=plan)


def test_회차_금액이_없으면_오늘과_같다():
    """이 변경이 기존 동작을 하나도 안 바꾼다는 것이 여기서 증명된다."""
    commitment = _build(scenario=_amount_legs(..., ...))

    assert len(commitment.arrival_schedule) == 2
    assert all(leg.amount_krw is None for leg in commitment.arrival_schedule)
    assert commitment.notes == ()


def test_전_회차에_금액이_있으면_그대로_싣는다():
    """마스터가 총액을 회차 수로 나눠 만들지 않는다 — 매입이 보낸 값을 옮긴다."""
    commitment = _build(scenario=_amount_legs(137280.0, 91520.0))

    assert [leg.amount_krw for leg in commitment.arrival_schedule] == [137280.0, 91520.0]
    assert commitment.notes == ()


def test_회차_금액_합이_총액과_어긋나면_못_만든다():
    """★ 수량과 같은 문구다 — **마스터가 둘 중 하나를 고쳐 맞추지 않는다.**"""
    with pytest.raises(CommitmentNotBuildable, match="회차 금액 합이 총액과 어긋난다"):
        _build(scenario=_amount_legs(137280.0, 90000.0))


def test_일부_회차만_금액이면_금액을_안_싣고_사유를_남긴다():
    """⚠️ 도착일 부분 공급은 **일정 전체를 버리지만** 금액은 **금액만 안 싣는다.**

    도착일이 없으면 회차가 성립하지 않지만 금액이 없어도 회차는 선다 — 오늘이
    정확히 그 상태다.
    """
    commitment = _build(scenario=_amount_legs(137280.0, ...))

    assert len(commitment.arrival_schedule) == 2, "일정까지 버리지 않는다"
    assert all(leg.amount_krw is None for leg in commitment.arrival_schedule)
    assert any("섞어 만들지 않는다" in note for note in commitment.notes), commitment.notes
    assert any("2회차 중 1회차" in note for note in commitment.notes), commitment.notes


def test_부분_공급이면_총액과_맞아떨어져도_안_싣는다():
    """실린 회차만 총액과 같아도 나머지 회차 금액을 **모르는** 것은 그대로다."""
    commitment = _build(scenario=_amount_legs(228800.0, ...))

    assert all(leg.amount_krw is None for leg in commitment.arrival_schedule)
    assert commitment.notes


def test_금액_허용오차는_수량과_같은_자리다():
    """같은 자리에서 다른 상수를 쓰면 왜 다른지를 아무도 모른다 — 1e-6 로 둔다."""
    ok = ApprovedCommitment(
        approval_id="H1-REQ-1-1",
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario_label="보수",
        total_qty_kg=100.0,
        total_amount_krw=228800.0,
        arrival_schedule=(
            ArrivalLeg("배추", 100.0, date(2026, 1, 2), AS_OF, 1, amount_krw=228800.0000001),
        ),
    )
    assert ok.arrival_schedule[0].amount_krw == pytest.approx(228800.0)

    with pytest.raises(CommitmentNotBuildable, match="회차 금액"):
        ApprovedCommitment(
            approval_id="H1-REQ-1-1",
            request_id="REQ-1",
            as_of=AS_OF,
            item="배추",
            scenario_label="보수",
            total_qty_kg=100.0,
            total_amount_krw=228800.0,
            arrival_schedule=(
                ArrivalLeg("배추", 100.0, date(2026, 1, 2), AS_OF, 1, amount_krw=228799.0),
            ),
        )
