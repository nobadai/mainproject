"""🔴 **과거로 돌려도 미래를 안 본다** — 규칙 1 을 한 자리에서 잰다.

백테스트에서 look-ahead 가 새면 **에러가 안 난다.** 손익만 좋아지고 성적이 무효가
된다. 그래서 방어가 네 겹인데, 지금까지 각 겹이 자기 자리에서만 검사됐다::

    벽시계 금지     test_mocks.py            패키지에 date.today()/now() 없음
    예측 시점       adapter.validate_forecast  generated_at <= as_of (+ 마스터 한 겹)
    시세 관측일     quotes.py                as_of 이전 최신 거래일 하루
    문서 발행일     ports.py                 published_at <= as_of

★ **이 파일은 그 넷을 하나로 묶는다.** 한 문장으로: *"이 제안이 근거로 든 모든
  관측은 ``as_of`` 이전 것이다."* 발표에서 그대로 읽을 수 있는 문장이어야 한다.

🔴 **산출물의 미래 날짜는 재지 않는다 — 그건 위반이 아니다.**

    🟢 미래여야 맞는 것
       split_plan[].date          회차 매입일 = as_of + offset
       expected_arrival_date      도착일 = 매입일 + N4
       payment_due_date           지급일 = 매입일 + N5
       cap_by_date 키             도착일 창 18일
       forecast.daily[]           D+1 ~ D+18 예측

    🔴 위반인 것
       그 값을 만들 때 쓴 **입력**이 as_of 뒤의 관측일 때

⚠️ **다음 사람에게** — *"산출물에 미래 날짜가 있는데 왜 통과하나"* 로 이 검사를
  고치려 들지 마라. 산출물을 훑어 미래 날짜를 찾는 검사를 지으면 위 다섯이 전부
  걸린다. 재는 축은 **입력의 관측 시점**이고, 그 구분이 이 파일의 전부다.
  ``test_future_dated_outputs_are_not_a_violation`` 이 그 구분을 잠근다.
"""

from datetime import date

import pytest

from app.purchase_agent.graph import build_graph
from app.purchase_agent.state import build_initial_state

ANCHORS = (date(2025, 12, 31), date(2026, 8, 21), date(2026, 8, 28), date(2026, 9, 4))
ITEMS = ("배추", "무", "양파")

#: 관측 시점을 담고 있는 자리. **없는 축은 안 싣는다** — mock 시세에는 관측일 칸이
#: 없다(실 DB 경로에만 있다). 규칙 3 대로 없는 것을 만들어 내지 않고, 대신 아래
#: ``_observations`` 가 **하나도 못 찾으면 실패**하게 해서 공허한 통과를 막는다.
_OBSERVATION_FIELDS = (
    ("예측 배치", "forecast", None, "generated_at"),
    ("시세 관측일", "market_quotes", "list", "observed_date"),
    ("문서 발행일", "context_docs", "list", "published_at"),
)


def _leaks(state: dict, as_of: date) -> list[str]:
    """``as_of`` 뒤의 관측을 근거로 쓴 자리. 없으면 빈 목록이다.

    ★ **판정을 함수로 뺀 이유**: 전수 검사가 *"없다"* 를 재고, 합성 검사가 *"있으면
      잡힌다"* 를 잰다. 판정이 검사 안에 인라인이면 뒤엣것을 못 짓고, 그러면 앞엣것이
      **우연히 통과하는 것과 구분되지 않는다** (규칙 8 의 정신).
    """
    limit = as_of.isoformat()
    return [f"{label} 이 {stamp}" for label, stamp in _observations(state) if stamp > limit]


def _observations(state: dict) -> list[tuple[str, str]]:
    """이 판단이 근거로 삼은 관측 시점 목록 ``(이름, ISO 문자열)``.

    ⚠️ 값이 ``datetime`` 문자열이어도 앞 10글자가 날짜라 ISO 비교가 성립한다
    (``2026-09-04T06:00:00+09:00`` → ``2026-09-04``).
    """
    found: list[tuple[str, str]] = []
    for label, key, kind, field in _OBSERVATION_FIELDS:
        value = state.get(key)
        if value is None:
            continue
        rows = value if kind == "list" else [value]
        for row in rows:
            stamp = row.get(field) if isinstance(row, dict) else None
            if stamp:
                found.append((label, str(stamp)[:10]))
    return found


@pytest.fixture(scope="module")
def runs() -> dict[tuple[date, str], dict]:
    """앵커 × 품목 전수를 한 번만 돌린다."""
    graph = build_graph()
    return {
        (as_of, item): graph.invoke(build_initial_state(item, as_of))
        for as_of in ANCHORS
        for item in ITEMS
    }


def test_every_observation_predates_as_of(runs: dict) -> None:
    """🔴 **이 제안이 근거로 든 모든 관측은 ``as_of`` 이전 것이다.**

    네 겹을 하나로 묶은 문장이다. 축 하나가 뚫리면 여기서 운다 — 어느 축인지는
    실패 메시지가 이름으로 말한다.
    """
    seen = 0
    for (as_of, item), state in runs.items():
        seen += len(_observations(state))
        assert not _leaks(state, as_of), (
            f"{as_of} {item}: {_leaks(state, as_of)} — as_of 뒤의 관측을 근거로 썼다. "
            "백테스트에서 이 누수는 에러가 아니라 성적으로 나타난다 (규칙 1)"
        )
    assert seen, "관측 시점을 하나도 못 찾았다 — 조회가 빗나갔거나 필드 이름이 바뀌었다"


def test_the_agent_never_reads_the_wall_clock_at_runtime(runs: dict) -> None:
    """🟢 **정적 금지(``test_mocks``)의 짝** — 실제로 돌려도 오늘 날짜가 안 샌다.

    ``date.today()`` 를 안 부르는 것과 *"산출물이 오늘에 안 물든다"* 는 다른 사실이다.
    벽시계를 안 불러도 mock 파일에 오늘 날짜가 박혀 있으면 같은 일이 난다.
    """
    today = date.today().isoformat()  # noqa: DTZ011 — 검사가 "오늘이 안 샌다"를 재는 자리다
    for (as_of, item), state in runs.items():
        if as_of.isoformat() == today:
            continue  # 우연히 오늘이 앵커면 대조가 성립하지 않는다
        for label, stamp in _observations(state):
            assert stamp != today, f"{as_of} {item}: {label} 이 오늘({today})이다 — 벽시계가 샜다"


def test_future_dated_outputs_are_not_a_violation(runs: dict) -> None:
    """🔴 **산출물의 미래 날짜는 정상이다** — 그 구분을 잠근다.

    이 검사가 없으면 다음 사람이 *"산출물에 미래 날짜가 있네"* 를 결함으로 읽고
    위 검사를 산출물 스캔으로 바꾼다. 그러면 회차일·도착일이 전부 걸려 **정상 동작이
    빨간불**이 된다.

    ★ 여기서 단언하는 것은 *"미래 날짜가 실제로 있다"* 이다 — 있어야 정상이고,
      없어지면 그때가 오히려 이상하다.
    """
    future_seen = 0
    for (as_of, item), state in runs.items():
        for scenario in state["proposal"]["scenarios"]:
            for leg in scenario["split_plan"]:
                if leg["date"] > as_of.isoformat():
                    future_seen += 1
        for row in state["forecast"]["daily"]:
            if row["date"] > as_of.isoformat():
                future_seen += 1
    assert future_seen, (
        "산출물에 as_of 뒤 날짜가 하나도 없다 — 예측 지평이나 회차일이 사라졌다는 뜻이고, "
        "그건 이 검사가 지키려던 구분 자체가 무의미해졌다는 신호다"
    )


# ── 판정이 실재하는가 (합성 입력) ──────────────────────────────────────────


_AS_OF = date(2026, 9, 4)
_FUTURE = "2099-01-01"


@pytest.mark.parametrize(
    ("label", "state"),
    [
        ("예측 배치", {"forecast": {"generated_at": f"{_FUTURE}T06:00:00+09:00"}}),
        ("시세 관측일", {"market_quotes": [{"grade": "특", "observed_date": _FUTURE}]}),
        ("문서 발행일", {"context_docs": [{"doc_id": 1, "published_at": _FUTURE}]}),
    ],
)
def test_a_leaked_observation_is_caught(label: str, state: dict) -> None:
    """🔴 **각 축을 하나씩 뚫으면 잡힌다.**

    ⚠️ 이 검사가 없으면 위 전수 검사가 *"우연히 통과"* 와 구분되지 않는다. 실제로
    mock 경로에서 **잴 수 있는 축이 예측 하나뿐**이고 그 값은 포트가 ``as_of`` 로
    만들어 넣는다(``mocks/_load.py:85``) — 그러니 전수 검사만으로는 판정이 살아
    있는지 알 수 없다.

    🟡 시세 관측일·문서 발행일은 **실 경로에만 실린다** (mock 시세에는 관측일 칸이
      없고, 문서는 포트가 ``published_at <= as_of`` 로 이미 걸러 미래 문서가 안 온다).
      그래서 여기서 합성으로 잰다.
    """
    assert _leaks(state, _AS_OF), f"{label} 축이 뚫렸는데 안 잡힌다"


def test_a_past_observation_is_not_flagged() -> None:
    """어제 관측은 **정상이다** — 판정이 늘 참이면 위 검사가 공허하다."""
    state = {"forecast": {"generated_at": "2026-09-03T06:00:00+09:00"}}
    assert not _leaks(state, _AS_OF)


def test_the_same_day_observation_is_not_flagged() -> None:
    """**당일 관측은 통과한다** — 경계가 ``<=`` 다.

    ⚠️ ``<`` 로 바꾸면 그날 아침 배치(``as_of`` 06:00)가 전부 위반이 된다. 그건
    look-ahead 가 아니라 **정상 운영**이다.
    """
    state = {"forecast": {"generated_at": f"{_AS_OF.isoformat()}T06:00:00+09:00"}}
    assert not _leaks(state, _AS_OF)
