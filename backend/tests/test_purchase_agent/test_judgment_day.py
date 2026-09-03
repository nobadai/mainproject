"""판정 기준일(D+14)이 **복사값을 안 밟는지** 잠근다 (ML 회신 2026-08-27).

장이 안 서는 날은 직전 개장일 값을 그대로 복사하고 ML 이 ``is_filled`` 로 표시한다.
🔴 **복사된 행은 예측 구간까지 복사된다** — 그날의 불확실성이 아니라 **전 장날의
불확실성**을 재게 된다. ``ci_width`` 가 그 구간에서 나오므로 ``situation`` 판정이
통째로 다른 날 것이 된다.

⚠️ **우리 payload 로는 못 잰다.** ``ml/service`` 의 ``DailyPoint`` 가
``date/predicted/lower/upper`` 넷만 담아 ``is_filled`` 를 버리고, 마스터
``_FORECAST_ENVELOPE_KEYS`` 에도 없다. **그래서 두 갈래로 나눠 잰다**::

    주기 조건    기본 스위트   빠르고 항상 돈다. 설정을 바꾸면 즉시 운다
    실제 복사    -m db        느리고 사내망이 필요하다. **진짜로 잰다**

앞은 *"주말을 밟지 않는 값인가"* 이고 뒤는 *"실제로 안 밟았나"* 다. 앞만 두면
공휴일을 놓치고, 뒤만 두면 DB 없는 날 아무 검사도 안 돈다.

🔴 **값 비교를 쓰지 않는다** (규칙 8). ``ci_judgment_day == 14`` 로 잠그면 코드가
같은 상수를 들고 있어도 통과한다 — 아무것도 증명하지 못한다. 여기서는 **선언을 읽어
그 값으로 판정을 만들고**, 선언을 바꾸면 판정이 따라 바뀐다.
"""

import pytest

from app.purchase_agent import db
from app.purchase_agent.config import load_constraints

#: 한 주. 달력 상수이지 설정값이 아니라 여기 적는다 — ``constraints.yaml`` 에서 읽어 오면
#: 검사와 대상이 같은 선언을 보게 되고, 그것이 규칙 8 이 막는 자리다.
DAYS_IN_WEEK = 7

#: 우리 판정이 쓰는 계열. ``ci_width`` 는 AUC(경락) 하나만 본다 — RTL·WHSL 은 안 쓴다.
JUDGMENT_TARGET_KIND = "AUC"

_WHY = (
    "D+7·D+14 만 개장일이 보장된다. 다른 offset 은 복사값을 밟고, 복사된 행은 예측 "
    "구간까지 복사돼 그날의 불확실성이 아니라 전 장날의 불확실성을 재게 된다. "
    "(ML 회신 2026-08-27)"
)


def _judgment_day() -> int:
    return load_constraints()["situation"]["ci_judgment_day"]


# ── 주기 조건 (기본 스위트) ───────────────────────────────────────────────


def test_the_judgment_day_lands_on_the_same_weekday_as_the_base_date() -> None:
    """🔴 판정일은 **주(週)의 배수**여야 한다.

    ``base_dt`` 가 개장일이면 D+7·D+14 는 **같은 요일**이라 주말을 안 밟는다.
    실측이 그 주기를 그대로 보여준다 (앵커 7 × 3품목 = 21행)::

        offset:   1  2  3  4  5  6  7  8  9 10 11 12 13 14
        복사값:   6  6 12 12  6  3  0  3  6 12 12  6  3  0
                              ↑                        ↑

    ⚠️ **주말만 피한다 — 공휴일은 못 피한다.** 그건 아래 ``-m db`` 검사가 잰다.
    """
    day = _judgment_day()
    assert day % DAYS_IN_WEEK == 0, (
        f"ci_judgment_day={day} 는 {DAYS_IN_WEEK} 의 배수가 아니다 — {_WHY}"
    )


def test_a_non_multiple_would_fail_this_check() -> None:
    """🔴 **검사가 실제로 무는지** 본다.

    위 검사가 통과하는 것만으로는 *"14 라서 통과"* 인지 *"무엇이든 통과"* 인지
    구분되지 않는다. 주기를 벗어난 값이 실제로 걸리는지 여기서 확인한다.
    """
    for bad in (10, 11, 13, 18):
        assert bad % DAYS_IN_WEEK != 0, f"{bad} 이 주기 조건을 통과하면 검사가 헛돈다"
    for good in (7, 14):
        assert good % DAYS_IN_WEEK == 0


def test_the_horizon_can_actually_reach_the_judgment_day() -> None:
    """판정일이 지평 안에 있어야 한다 — 밖이면 ``judgment_row`` 가 멈춘다.

    주기 조건만 보면 21·28 도 통과하는데 지평(D+18)을 넘는다. 두 조건이 함께여야
    *"쓸 수 있는 값"* 이 된다.
    """
    day = _judgment_day()
    horizon = load_constraints()["coverage_days"]["max"]
    assert day <= horizon, (
        f"ci_judgment_day={day} 가 지평 {horizon}일을 넘는다 — 그 줄은 예측에 없다"
    )


# ── 실제 복사 여부 (-m db) ────────────────────────────────────────────────


@pytest.mark.db
def test_the_judgment_day_is_never_a_copied_row() -> None:
    """🔴 **진짜로 잰다** — 선언된 판정일에 실제로 복사값이 있는지 DB 에서 본다.

    ★ **선언을 읽어 조회 좌표로 쓴다.** ``ci_judgment_day`` 를 13 으로 바꾸면 이
      검사가 offset 13 을 조회해 복사값 3건을 찾아 운다 — 값 비교가 아니라 **판정이
      선언을 따라 움직인다** (규칙 8).

    ⚠️ 행이 0건이면 **통과가 아니라 실패**다. 조회가 빗나간 채 조용히 초록불이 되면
      이 검사는 있으나 마나다.
    """
    day = _judgment_day()
    rows = db.fetch_all(
        "SELECT base_dt, item_nm, target_dt, is_filled "
        "FROM haetdeul.ml_price_forecasts "
        "WHERE target_kind = %(kind)s AND offset_days = %(day)s "
        "ORDER BY base_dt, item_nm",
        {"kind": JUDGMENT_TARGET_KIND, "day": day},
    )
    assert rows, (
        f"offset_days={day} · target_kind={JUDGMENT_TARGET_KIND} 행이 0건이다 — "
        "조회가 빗나갔거나 예측이 안 들어왔다. 빈 결과를 통과로 읽지 않는다"
    )
    copied = [f"{r['base_dt']} {r['item_nm']}(→{r['target_dt']})" for r in rows if r["is_filled"]]
    assert not copied, (
        f"판정일 D+{day} 이 복사값인 행 {len(copied)}/{len(rows)}건: "
        f"{', '.join(copied[:5])} — {_WHY}"
    )


@pytest.mark.db
def test_the_weekly_cycle_is_what_makes_it_safe() -> None:
    """🔴 **왜 안전한지**를 잰다 — 주기가 아니면 복사값이 실제로 나온다.

    위 검사만 두면 *"우연히 깨끗한 데이터"* 와 *"주기라서 깨끗하다"* 가 구분되지 않는다.
    주기를 벗어난 offset 에서 복사값이 **실제로 나오는지** 확인해, 안전의 근거가
    데이터에 있음을 못 박는다.

    ⚠️ 특정 건수를 단언하지 않는다 — 배치가 쌓이면 숫자가 변한다. 잠그는 것은
      **"주기 밖에는 복사값이 존재한다"** 는 성질이다.
    """
    rows = db.fetch_all(
        "SELECT offset_days, "
        "       sum(CASE WHEN is_filled THEN 1 ELSE 0 END) AS copied, "
        "       count(*) AS total "
        "FROM haetdeul.ml_price_forecasts "
        "WHERE target_kind = %(kind)s GROUP BY 1 ORDER BY 1",
        {"kind": JUDGMENT_TARGET_KIND},
    )
    assert rows, "예측 행이 0건이다 — 조회가 빗나갔다"

    on_cycle = {r["offset_days"]: r for r in rows if r["offset_days"] % DAYS_IN_WEEK == 0}
    off_cycle = {r["offset_days"]: r for r in rows if r["offset_days"] % DAYS_IN_WEEK}
    assert on_cycle and off_cycle, "양쪽 offset 이 다 있어야 대조가 성립한다"

    dirty_on = {day: r["copied"] for day, r in on_cycle.items() if r["copied"]}
    assert not dirty_on, f"주기 위 offset 에 복사값이 있다: {dirty_on} — 주기 가정이 깨졌다"

    assert any(r["copied"] for r in off_cycle.values()), (
        "주기 밖 offset 에도 복사값이 하나도 없다 — 그러면 판정일이 안전한 이유가 "
        "주기가 아니라 다른 것이고, 이 검사가 근거로 삼는 설명이 틀렸다"
    )
