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
        "item": "피마늘",
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

    assert commitment.item == "피마늘"
    assert [leg.item for leg in commitment.arrival_schedule] == ["피마늘"]


def test_품목_어휘를_값으로_막는다():
    """🔴 `orchestrator` 의 `ItemCode` 는 `str` 별칭이라 `"(승인분)"` 도 통과한다.

    실제로 `contracts_core.py:975` 가 그 값을 품목 자리에 넣고 있다. 여기서는 막는다.
    """
    with pytest.raises(CommitmentNotBuildable, match="4품목"):
        _build(item="(승인분)")

    with pytest.raises(CommitmentNotBuildable, match="4품목"):
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
            item="피마늘",
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


def test_품목_어휘가_매입_ItemName_과_같다():
    """★ `attempt_max` 와 같은 자리 — 같은 4품목이 두 곳에 선언돼 있다.

    매입 `ItemName` 은 Literal, 마스터 `ITEM_CODES` 는 frozenset. 어느 쪽이
    품목을 늘리면 **다른 쪽은 에러 없이 낡는다.** 갈리는 날 여기가 운다.
    """
    from typing import get_args

    from app.master.commitment import ITEM_CODES
    from app.purchase_agent.schemas import ItemName

    assert ITEM_CODES == frozenset(get_args(ItemName))


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
