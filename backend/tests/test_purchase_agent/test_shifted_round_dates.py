"""`#300` 검사 — 회차일이 장이 안 서는 날이면 **다음 개장일로 민다** (`SHIFT`).

이 파일이 잠그는 것은 ⑥ ``package_scenarios`` 의 밀기이고, ⑦ ``market_open_days``(같은
달력을 보는 컷)는 `test_market_open_days.py` 가 잠근다. **둘을 한 파일에 두지 않는다** —
⑥이 밀면 ⑦은 아무것도 안 잡는 것이 정상이라, 같은 파일에 두면 «검사가 조용해진 것» 과
«검사가 죽은 것» 이 구분되지 않는다.

★ **이름이 `test_execution_calendar` 가 아닌 이유**는 `test_market_open_days.py` 머리말과
  같다 — `tests/master/` 에 같은 basename 이 있으면 pytest 가 **수집 단계에서** 터진다.

⚠️ **실 경로에는 지금 밀 대상이 0건이다** (2026년 실행 전수 · 회차 155건이 전부 1회차이고
  그 하나가 ``as_of``). 그래서 여기 입력은 **전부 합성**이다 — mock 만 돌려서는 이 가지가
  살아 있는지 알 수 없다는 `test_split.py` 머리말과 같은 이유다.
"""

from datetime import date, timedelta

from app.purchase_agent.nodes.package_scenarios import (
    arrival_dates,
    materialize_split,
    round_offsets,
    shifted_rounds_note,
    split_offsets,
)
from app.purchase_agent.nodes.self_check import market_open_days

#: 2026-01-05(월). 그 주 토·일이 01-10·01-11 이다.
AS_OF = "2026-01-05"
#: 마스터가 싣는 모양 그대로 — 목록과 지평이 **한 덩어리**다.
CALENDAR = {
    "non_execution_days": ["2026-01-10", "2026-01-11", "2026-01-17", "2026-01-18"],
    "horizon_end": "2026-02-05",
}
HALF = [{"ratio": 0.5}, {"ratio": 0.5}]


def _day(offset: int, *, as_of: str = AS_OF) -> str:
    return (date.fromisoformat(as_of) + timedelta(days=offset)).isoformat()


# ── 미는가 ──────────────────────────────────────────────────────────


def test_a_closed_round_day_moves_to_the_next_open_day() -> None:
    """D=12 · 2회차면 2회차가 01-11(일)이다. **다음 개장일 01-12(월)로 민다.**

    본문 실측표의 첫 줄이다 — *"as_of 월 · 공격 D=12 2회차 · offsets=[0,6] → seq2 01-11(일)"*.
    """
    assert split_offsets(12, 2) == [0, 6]
    assert _day(6) == "2026-01-11"
    assert round_offsets(AS_OF, 12, 2, CALENDAR) == [0, 7]
    assert _day(7) == "2026-01-12"


def test_an_open_saturday_is_not_moved() -> None:
    """🔴 **토요일이라고 밀지 않는다.** 목록에 있는 날만 민다.

    ★ 2026년 토요일 45일에 가락이 선다. 요일로 밀면 **살 수 있는 날에 못 산다고 계획**하는
      것이고, 마스터가 축을 ``is_execution_day`` 에서 ``is_open`` 으로 갈아탄 이유가 그것이다.
    """
    open_saturday = {"non_execution_days": ["2026-01-11"], "horizon_end": "2026-02-05"}
    # 01-10 은 토요일인데 목록에 없다 → 서는 날이다.
    assert round_offsets(AS_OF, 10, 2, open_saturday) == split_offsets(10, 2) == [0, 5]
    assert _day(5) == "2026-01-10"


def test_the_first_round_is_never_moved() -> None:
    """🔴 **1회차는 ``as_of`` 다 — 휴장일이어도 안 민다.**

    IO명세 §2 가 *"seq 1 의 date = as_of"* 로 못박았고 마스터가 약정을 그 등식으로 조립한다.
    ``as_of`` 가 휴장일이면 그날은 **살 수 없는 날**이지 미룰 날이 아니고, 그 판정은
    ⑦ ``market_open_days`` 가 컷으로 낸다.
    """
    sunday = "2026-01-11"
    calendar = {"non_execution_days": [sunday], "horizon_end": "2026-02-05"}
    assert round_offsets(sunday, 12, 2, calendar)[0] == 0


def test_shifting_keeps_the_rounds_in_order() -> None:
    """민 자리가 다음 회차와 겹치면 **그 회차도 민다.**

    겹치면 분할이 아니라 같은 매입을 두 줄로 적은 것이 된다 (``split_infeasible_reason``).
    """
    # 01-06(화)·01-07(수) 를 닫으면 offsets [0,1,2] 가 [0,3,4] 로 밀려야 한다.
    calendar = {
        "non_execution_days": ["2026-01-06", "2026-01-07"],
        "horizon_end": "2026-02-05",
    }
    moved = round_offsets(AS_OF, 3, 3, calendar)
    assert moved == [0, 3, 4]
    assert moved == sorted(set(moved))  # 순서가 지켜지고 겹치지 않는다


# ── 안 미는가 ───────────────────────────────────────────────────────


def test_no_calendar_means_no_shift() -> None:
    """달력이 없으면 **밀 근거가 없다.** 빈 목록을 «안 서는 날이 없다» 로 읽지 않는다."""
    for calendar in (None, {}, {"non_execution_days": [], "horizon_end": "2026-02-05"}):
        assert round_offsets(AS_OF, 12, 2, calendar) == split_offsets(12, 2)


def test_a_calendar_without_a_horizon_does_not_shift() -> None:
    """지평이 없으면 목록을 못 쓴다 — 마스터 모듈이 그 규약을 길게 적어 뒀다."""
    assert round_offsets(AS_OF, 12, 2, {"non_execution_days": ["2026-01-11"]}) == [0, 6]


def test_nothing_moves_when_one_round_would_leave_the_horizon() -> None:
    """🔴 **절반만 밀지 않는다.** 한 회차라도 지평 밖으로 나가면 **전부** 제자리다.

    지평 밖은 «안 선다» 가 아니라 «모른다» 다. 절반만 민 계획은 민 이유도 안 민 이유도
    설명할 수 없고, ⑦ 이 같은 태도로 «못 봤다» 를 적는다.
    """
    tight = {
        "non_execution_days": ["2026-01-11", "2026-01-12", "2026-01-13"],
        "horizon_end": "2026-01-12",  # 밀 자리(01-13)가 지평 밖이다
    }
    assert round_offsets(AS_OF, 12, 2, tight) == split_offsets(12, 2)


# ── 따라오는 것들 ───────────────────────────────────────────────────


def test_arrival_dates_follow_the_shifted_purchase_day() -> None:
    """도착일 = **밀린** 회차일 + N4. 도착일을 따로 밀지 않는다 (물류 축)."""
    assert arrival_dates(AS_OF, 12, 2, 2, CALENDAR) == ["2026-01-07", "2026-01-14"]
    # 밀기 전이면 01-13 이었다 — 회차일이 하루 밀린 만큼 도착일도 하루 밀린다.
    assert arrival_dates(AS_OF, 12, 2, 2) == ["2026-01-07", "2026-01-13"]


def test_materialize_split_publishes_the_shifted_dates() -> None:
    """안에 실려 나가는 ``split_plan`` 이 밀린 날짜다 — 계산과 출력이 갈리지 않는다."""
    rounds = materialize_split(
        AS_OF, 100, HALF, 12, lead_days=2, calendar=CALENDAR
    )
    assert [line["date"] for line in rounds] == ["2026-01-05", "2026-01-12"]
    assert [line["expected_arrival_date"] for line in rounds] == [
        "2026-01-07",
        "2026-01-14",
    ]


def test_the_seventh_node_finds_nothing_once_the_sixth_has_shifted() -> None:
    """★★ ⑥이 밀면 ⑦은 **아무것도 안 잡는다** — 그것이 이 판의 목적이다.

    ⚠️ 그렇다고 ⑦을 걷지 않는다. ⑥이 못 미는 경우가 셋 있고(달력 없음 · 지평 밖 ·
    ``as_of`` 자신이 휴장일), 그때 컷을 내는 것이 ⑦이다.
    """
    state = {"execution_calendar": CALENDAR}
    shifted = {"split_plan": materialize_split(AS_OF, 100, HALF, 12, calendar=CALENDAR)}
    assert market_open_days(shifted, state).violation is None
    # 안 밀었으면 같은 달력으로 ⑦이 컷을 낸다 — 검사가 살아 있다는 대조군이다.
    unshifted = {"split_plan": materialize_split(AS_OF, 100, HALF, 12)}
    assert "2026-01-11" in (market_open_days(unshifted, state).violation or "")


# ── 고지 ────────────────────────────────────────────────────────────


def test_the_shift_is_reported() -> None:
    """민 사실을 적는다. 안 밀었으면 ``None`` 이다."""
    assert shifted_rounds_note(AS_OF, 12, 2, None) is None
    note = shifted_rounds_note(AS_OF, 12, 2, CALENDAR)
    assert note is not None
    assert "2026-01-11" in note and "2026-01-12" in note


def test_a_shift_past_the_coverage_window_says_so() -> None:
    """⚠️ **커버가 늘면 판단이 바뀐 것이다** — 마스터 회신 §4.2 가 그렇게 청했다."""
    # D=6 · 2회차면 2회차가 offset 3(01-08)이다. 01-08~01-10 을 닫으면 01-11 도 닫혀
    # 01-12(offset 7)까지 밀리고, 그건 커버 6일 구간 밖이다.
    calendar = {
        "non_execution_days": ["2026-01-08", "2026-01-09", "2026-01-10", "2026-01-11"],
        "horizon_end": "2026-02-05",
    }
    assert round_offsets(AS_OF, 6, 2, calendar) == [0, 7]
    note = shifted_rounds_note(AS_OF, 6, 2, calendar)
    assert note is not None and "커버 6일" in note


# ── 배선이 실제로 물리는가 (규칙 8) ─────────────────────────────────


def test_the_calendar_actually_reaches_the_dates() -> None:
    """🔴 **달력을 빼면 날짜가 달라져야 한다.**

    ``materialize_split`` 이 ``calendar`` 를 받아 놓고 안 쓰면 위 검사들이 ``round_offsets``
    만 시험하고 **배선은 안 시험한 것**이 된다 — `#529` 변이 ④가 정확히 그렇게 0건 실패로
    지나간 자리다 (규칙 8).
    """
    with_calendar = materialize_split(AS_OF, 100, HALF, 12, calendar=CALENDAR)
    without = materialize_split(AS_OF, 100, HALF, 12)
    assert [r["date"] for r in with_calendar] != [r["date"] for r in without]


# ── ⑥ 관통 — 봉투에서 안까지 (규칙 8) ───────────────────────────────


def _rising_aggressive(calendar: dict | None) -> dict:
    """mock 상승 앵커의 공격안. **실 경로에서 회차가 둘인 유일한 자리다.**

    ⚠️ ``purchase_port`` 관통으로는 이 배선을 못 잡는다 — 실 봉투에서는 회차가 전부
    하나이고 1회차는 안 밀기 때문이다. 그래서 mock 상태에 달력을 직접 얹는다.
    """
    from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
    from app.purchase_agent.nodes.classify_situation import classify_situation
    from app.purchase_agent.nodes.draft_plan import draft_plan
    from app.purchase_agent.nodes.package_scenarios import package_scenarios
    from app.purchase_agent.nodes.split_plan import split_plan
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state("배추", date(2026, 8, 21))
    for node in (classify_situation, draft_plan, split_plan, allocate_sourcing):
        state.update(node(state))
    if calendar is not None:
        state["execution_calendar"] = calendar  # type: ignore[typeddict-unknown-key]
    result = package_scenarios(state)
    return next(s for s in result["scenarios_final"] if s["label"] == "공격")


#: 2026-08-21(금) 상승 앵커 공격안의 2회차가 08-27 이다. 그 날과 다음 날을 닫으면 08-29 로
#: 밀려야 한다 — 회차를 둘 닫는 이유는 «한 칸만 미는 것» 과 «다음 개장일까지 미는 것» 을
#: 가르기 위해서다.
_CLOSED_AROUND_LEG2 = {
    "non_execution_days": ["2026-08-27", "2026-08-28"],
    "horizon_end": "2026-09-21",
}


def test_the_envelope_reaches_the_published_dates() -> None:
    """🔴 봉투 달력이 **안의 `split_plan` 날짜까지** 간다 — 노드 안에서만 알고 끝나지 않는다."""
    assert [leg["date"] for leg in _rising_aggressive(None)["split_plan"]] == [
        "2026-08-21",
        "2026-08-27",
    ]
    assert [
        leg["date"] for leg in _rising_aggressive(_CLOSED_AROUND_LEG2)["split_plan"]
    ] == ["2026-08-21", "2026-08-29"]


def test_the_shift_notice_reaches_risks() -> None:
    """🔴 민 사실이 **안의 `risks` 까지** 간다.

    ★ `#529` 변이 ④가 *"검사는 있는데 아무것도 안 죽인다"* 였다면 여기는 *"밀기는 하는데
      아무도 모른다"* 다. 밀린 계획이 말없이 나가면 받는 쪽은 **우리가 낸 D 커버가 그대로**
      라고 읽는다.
    """
    risks = _rising_aggressive(_CLOSED_AROUND_LEG2)["risks"]
    note = next((r for r in risks if "장이 안 서는 날이라" in r), None)
    assert note is not None, risks
    assert "2026-08-27" in note and "2026-08-29" in note
    assert not any(
        "장이 안 서는 날이라" in risk for risk in _rising_aggressive(None)["risks"]
    )
