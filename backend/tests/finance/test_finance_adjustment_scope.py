"""재무 조정안이 **어느 안에 대한 것인지**를 공용 계약까지 나르는가.

★ 이 파일이 지키는 것은 조정안의 **적용 범위(scope)** 다.

    · 상류가 아는 라벨은 공용 `SuggestedAdjustment` 까지 살아서 간다
    · 모르면 빈 tuple 이다 — 라벨을 지어내지 않는다
    · 빈 tuple 은 *"특정하지 못했다"* 이지 *"모든 안에 적용"* 이 아니다
    · 회차 개념이 없는 재무 `amount` 의 `split_date=None` 은 정상이다

🔴 예전에는 변환부가 여섯 칸만 옮겨서, 상류가 라벨을 채워도 이 지점에서 조용히
   사라졌다. 받는 쪽은 "세 안 중 어디에 적용할 조정인지" 를 영영 알 수 없었다.
"""

from datetime import date

from app.contracts.core import SuggestedAdjustment
from app.finance.execution import _adjustment_from_dict


def _dict(**over):
    base = {
        "dept": "finance",
        "axis": "amount",
        "target_value": 800.0,
        "unit": "krw",
        "reason": "Verified Finance amount alternative.",
        "ref_ids": ["FIN-AGENT:req-1:1:S2:validate_amount_adjustment"],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Case 1 — 라벨 전달
# ---------------------------------------------------------------------------


def test_scenario_labels_survive_the_conversion():
    adjustment = _adjustment_from_dict(_dict(scenario_labels=("기본", "공격")))

    assert adjustment.scenario_labels == ("기본", "공격")


def test_scenario_labels_become_a_tuple_even_when_given_as_a_list():
    # payload 는 JSON 을 왕복하므로 list 로 돌아온다 — 계약은 tuple 이다.
    adjustment = _adjustment_from_dict(_dict(scenario_labels=["기본"]))

    assert adjustment.scenario_labels == ("기본",)


def test_a_single_label_is_not_split_into_characters():
    adjustment = _adjustment_from_dict(_dict(scenario_labels=["보수"]))

    assert adjustment.scenario_labels == ("보수",)


# ---------------------------------------------------------------------------
# Case 2 — 라벨 없음
# ---------------------------------------------------------------------------


def test_missing_scenario_labels_stay_empty():
    adjustment = _adjustment_from_dict(_dict())

    assert adjustment.scenario_labels == ()


def test_explicit_empty_scenario_labels_stay_empty():
    adjustment = _adjustment_from_dict(_dict(scenario_labels=[]))

    assert adjustment.scenario_labels == ()


def test_no_scenario_label_vocabulary_is_invented_when_absent():
    """빈 목록에 '보수·기본·공격' 을 채워 넣지 않는다.

    라벨 어휘는 매입의 계약이다. 재무가 그것을 복제해 채우면, 매입이 라벨을 바꾼
    날 조용히 어긋난다 — 게다가 그것은 **재무가 판정하지 않은 안**에 조정을
    붙이는 일이 된다.
    """
    adjustment = _adjustment_from_dict(_dict())

    assert adjustment.scenario_labels == ()
    for invented in ("보수", "기본", "공격", "CONSERVATIVE", "BALANCED", "AGGRESSIVE"):
        assert invented not in adjustment.scenario_labels


# ---------------------------------------------------------------------------
# Case 3 — split_date
# ---------------------------------------------------------------------------


def test_finance_amount_adjustment_has_no_split_date():
    adjustment = _adjustment_from_dict(_dict())

    # 재무 amount 축에는 회차 개념이 없다 — None 이 정상이고, 날짜를 지어내지 않는다.
    assert adjustment.split_date is None


def test_split_date_is_carried_when_upstream_actually_has_one():
    adjustment = _adjustment_from_dict(_dict(split_date=date(2026, 1, 5)))

    assert adjustment.split_date == date(2026, 1, 5)


# ---------------------------------------------------------------------------
# Case 4 — 기존 여섯 칸 회귀
# ---------------------------------------------------------------------------


def test_the_six_original_fields_are_unchanged():
    adjustment = _adjustment_from_dict(_dict(scenario_labels=["기본"]))

    assert adjustment.dept == "finance"
    assert adjustment.axis == "amount"
    assert adjustment.target_value == 800.0
    assert adjustment.unit == "krw"
    assert adjustment.reason == "Verified Finance amount alternative."
    assert adjustment.ref_ids == ("FIN-AGENT:req-1:1:S2:validate_amount_adjustment",)


def test_finance_still_owns_only_the_amount_axis():
    adjustment = _adjustment_from_dict(_dict(scenario_labels=["기본"]))

    # 라벨을 싣는다고 축이 늘어나지 않는다.
    assert isinstance(adjustment, SuggestedAdjustment)
    assert adjustment.axis == "amount"
