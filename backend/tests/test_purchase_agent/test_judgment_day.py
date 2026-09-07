"""판정 기준일(D+14)이 **복사값을 안 밟는지** 잠근다 (ML 회신 2026-08-27).

장이 안 서는 날은 직전 개장일 값을 그대로 복사하고 ML 이 ``is_filled`` 로 표시한다.
🔴 **복사된 행은 예측 구간까지 복사된다** — 그날의 불확실성이 아니라 **전 장날의
불확실성**을 재게 된다. ``ci_width`` 가 그 구간에서 나오므로 ``situation`` 판정이
통째로 다른 날 것이 된다.

🔴 **"우리 payload 로는 못 잰다" 고 적었었다 (2026-09-04 정정).** ``ml/service`` 의
``DailyPoint`` 가 넷만 담는 것은 맞는데 **마스터는 그 모델을 안 거친다** — 뷰를 직접
읽어 ``daily`` 를 그대로 나르고, 그 안에 ``is_filled`` 가 있다. 없는 경로의 한계를 보고
*"안 온다"* 고 적었다 (#213 · #212).

**그래도 두 갈래는 그대로 둔다.** 나누는 이유가 바뀌었을 뿐이다::

    주기 조건    기본 스위트   빠르고 항상 돈다. 설정을 바꾸면 즉시 운다
    실제 복사    -m db        느리고 사내망이 필요하다. **진짜로 잰다**

앞은 *"주말을 밟지 않는 값인가"* 이고 뒤는 *"실제로 안 밟았나"* 다. 앞만 두면
공휴일을 놓치고, 뒤만 두면 DB 없는 날 아무 검사도 안 돈다.

🔴 **``-m db`` 둘은 기본 스위트에서 안 돈다** (``addopts = "-m 'not llm and not db'"``).
  *"뒤가 진짜로 잰다"* 고 해 놓고 그 진짜를 아무도 안 돌리면 **있으나 마나**가 된다 —
  이 파일이 막으려는 바로 그 모양이다 (현서님 지적 2026-09-03).

    언제   **새 ``base_dt`` 가 들어온 날.** 이 검사가 재는 것이 *"판정일 행이
           복사값인가"* 이고, 그 답은 **배치가 들어올 때만** 바뀐다. 배포 전이나
           주기(주 1회)로 두면 예측이 안 들어온 날 같은 행을 다시 세고, 들어온
           날은 놓친다.
    누가   🔴 **지금은 사람이 손으로 돌린다.** 자동이 아니다.

           DB_SCHEMA=haetdeul uv run pytest -q -m db

  ⚠️ **자동이 아니라는 사실을 감추지 않는다.** 배치 도착을 우리가 감지하지 못하므로
    (ML 이 알려 주는 것도 우리가 폴링하는 것도 없다), 지금 이 검사는 **누가 기억하는가**
    에 매달려 있다. 그 약점을 적어 두지 않으면 다음 사람이 *"CI 가 본다"* 로 읽는다.

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


def _swap_judgment_day(monkeypatch: pytest.MonkeyPatch, day: int) -> None:
    """**선언만 바꾼다.** 코드는 한 줄도 안 건드리고 판정이 따라 움직이는지 본다.

    ``load_constraints`` 를 이 모듈이 이름으로 들고 있으므로(``from … import``)
    원본이 아니라 **여기 붙은 이름**을 갈아 끼운다 — 원본을 패치하면 이미 바인딩된
    이름이 그대로라 안 먹는다.

    ``coverage_days`` 는 그대로 옮긴다. 지평 검사가 같은 dict 를 읽으므로 빠뜨리면
    *"주기 때문에 울었는지 dict 가 깨져서 울었는지"* 가 구분되지 않는다.
    """
    real = load_constraints()
    swapped = {**real, "situation": {**real["situation"], "ci_judgment_day": day}}
    monkeypatch.setattr(f"{__name__}.load_constraints", lambda: swapped)


@pytest.mark.parametrize(
    ("day", "check"),
    [
        (13, test_the_judgment_day_lands_on_the_same_weekday_as_the_base_date),
        (21, test_the_horizon_can_actually_reach_the_judgment_day),
    ],
    ids=["주기밖_13", "지평밖_21"],
)
def test_a_bad_declaration_makes_the_check_cry(
    monkeypatch: pytest.MonkeyPatch, day: int, check: object
) -> None:
    """🔴 **위 검사들이 실제로 무는지** — 선언을 흔들어 본다 (규칙 8).

    두 값이 서로 다른 것을 잡는다. 하나만 두면 나머지 조건이 죽어도 모른다::

        13   주기 밖              → 주기 검사가 운다 (지평 18 안이라 지평은 통과)
        21   주기 위이지만 지평 밖  → 지평 검사가 운다 (21 % 7 == 0 이라 주기는 통과)

    ⚠️ **전에는 이 검사가 ``bad % 7 != 0`` 이었다** (현서님 리뷰 2026-09-03).
      파이썬 산술을 단언해서 ``judgment_row`` 를 어떻게 깨뜨려도, ``constraints.yaml``
      을 어떻게 바꿔도 **절대 안 울었다.** 이름이 ``would_fail_this_check`` 인데
      **그 검사를 안 불렀다.**

      🔴 **규칙 8 을 피했다고 PR 본문에 적으면서 같은 자리에서 어겼다.** 값 비교를
        안 쓴다고 해 놓고 상수 대조를 넣었다 — 전제를 단언하려다 **전제 대신 상수를
        단언**한 것이고, 이 파일이 막으려던 바로 그 모양이다.
    """
    _swap_judgment_day(monkeypatch, day)
    with pytest.raises(AssertionError):
        check()  # type: ignore[operator]


def test_the_declared_value_passes_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """**양성 대조** — 지금 선언(14)으로는 둘 다 통과한다.

    위 검사만 두면 *"바꾸면 운다"* 는 알아도 **"안 바꾸면 안 운다"** 를 모른다.
    두 검사가 무엇을 넣어도 우는 상태여도 위가 초록이기 때문이다.

    ★ ``== 14`` 로 대조하지 않는다 — 선언을 읽어 그대로 다시 넣고, **판정이 서는지**만
      본다. 선언이 15 로 바뀌면 이 검사가 아니라 위 두 검사가 운다.
    """
    _swap_judgment_day(monkeypatch, _judgment_day())
    test_the_judgment_day_lands_on_the_same_weekday_as_the_base_date()
    test_the_horizon_can_actually_reach_the_judgment_day()


# ── 실제 복사 여부 (-m db) ────────────────────────────────────────────────


@pytest.mark.db
def test_a_copied_judgment_day_is_always_a_holiday() -> None:
    """🔴 **판정일이 복사값인 날은 반드시 공휴일이다** (2026-09-07 정정 · ``#384``).

    전에는 이 검사가 *"복사값이 아예 없다"* 를 단언했고, 이름도
    ``..._is_never_a_copied_row`` 였다. **21조합에서만 참이었다** — 504조합으로
    넓히니 27건이 복사값이고 전부 **``target_dt`` 가 공휴일**이었다.

    ★ **주기 가정이 틀린 게 아니다.** ``base_dt + 14`` 는 같은 요일이라 **주말**을
      안 밟는다 — 그건 위 기본 스위트 검사가 그대로 잠근다. 공휴일은 요일과 무관해서
      주기로 못 막을 뿐이다.

    🟢 그래서 잠그는 성질이 바뀐다::

        전   복사값이 없다                     🔴 사실이 아니다
        후   복사값이면 그날은 공휴일이다        🟢 사실이고, 깨지면 진짜 이상이다

    ⚠️ **공휴일이 아닌 복사값이 나오면 그때는 진짜 문제다** — 개장일인데 예측이 없어
      복사됐다는 뜻이고, 그건 ML 적재 쪽 사고다.

    ★ **선언을 읽어 조회 좌표로 쓴다** — ``ci_judgment_day`` 를 바꾸면 이 검사가
      그 offset 을 조회한다 (규칙 8).
    """
    day = _judgment_day()
    rows = db.fetch_all(
        "SELECT f.base_dt, f.item_nm, f.target_dt, f.is_filled, "
        "       c.holiday_nm, c.is_open "
        "FROM haetdeul.ml_price_forecasts f "
        "LEFT JOIN haetdeul.ml_calendar_days c ON c.dt = f.target_dt "
        "WHERE f.target_kind = %(kind)s AND f.offset_days = %(day)s "
        "ORDER BY f.base_dt, f.item_nm",
        {"kind": JUDGMENT_TARGET_KIND, "day": day},
    )
    assert rows, (
        f"offset_days={day} · target_kind={JUDGMENT_TARGET_KIND} 행이 0건이다 — "
        "조회가 빗나갔거나 예측이 안 들어왔다. 빈 결과를 통과로 읽지 않는다"
    )
    unexplained = [
        f"{r['base_dt']} {r['item_nm']}(→{r['target_dt']})"
        for r in rows
        if r["is_filled"] and not r["holiday_nm"]
    ]
    assert not unexplained, (
        f"판정일 D+{day} 이 복사값인데 공휴일이 아닌 행 {len(unexplained)}건: "
        f"{', '.join(unexplained[:5])} — 개장일인데 예측이 없어 복사된 것이라면 "
        f"ML 적재 쪽 사고다. {_WHY}"
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

    # 🔴 **주기 위에도 복사값이 있다 — 공휴일이다** (2026-09-07 · `#384`).
    #   전에는 여기서 `not dirty_on` 을 단언했는데 21조합에서만 참이었다. 잠그는 것은
    #   *"주기 위가 깨끗하다"* 가 아니라 **"주기 위가 주기 밖보다 훨씬 깨끗하다"** 로
    #   바꾼다 — 그게 판정일을 주기 위에 둔 실제 이유다.
    def _rate(group: dict) -> float:
        copied = sum(r["copied"] for r in group.values())
        total = sum(r["total"] for r in group.values())
        return copied / total if total else 0.0

    on_rate, off_rate = _rate(on_cycle), _rate(off_cycle)
    assert on_rate < off_rate / 2, (
        f"주기 위 복사율 {on_rate:.1%} 이 주기 밖 {off_rate:.1%} 의 절반 아래가 아니다 "
        "— 판정일을 주기 위에 둔 근거가 데이터에서 사라졌다"
    )

    assert any(r["copied"] for r in off_cycle.values()), (
        "주기 밖 offset 에도 복사값이 하나도 없다 — 그러면 판정일이 안전한 이유가 "
        "주기가 아니라 다른 것이고, 이 검사가 근거로 삼는 설명이 틀렸다"
    )


# ── 복사값인 날 고지가 나가는가 (기본 스위트) ──────────────────────────────


def _risks_with_filled_judgment_day(filled: bool) -> list[str]:
    """판정일 행의 ``is_filled`` 만 바꿔 ⑥까지 돌린 risks.

    **합성 입력이다.** mock 예측에는 이 칸이 아예 없어(규칙 3) 앵커만 돌려서는
    이 경로가 한 번도 안 선다 — 그런데 실 경로에서는 504조합 중 27건이 이 경로다.
    """
    import os

    os.environ["PURCHASE_LLM_ENABLED"] = "false"
    from datetime import date

    from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
    from app.purchase_agent.nodes.classify_situation import classify_situation
    from app.purchase_agent.nodes.draft_plan import draft_plan
    from app.purchase_agent.nodes.package_scenarios import package_scenarios
    from app.purchase_agent.nodes.split_plan import split_plan
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state("배추", date(2026, 8, 21))
    row = state["forecast"]["daily"][_judgment_day() - 1]
    state["forecast"]["daily"][_judgment_day() - 1] = {**row, "is_filled": filled}
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    state.update(split_plan(state))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    return [risk for s in state["scenarios_final"] for risk in s["risks"]]


def test_a_copied_judgment_day_is_named_in_risks() -> None:
    """판정일이 복사값이면 **그 사실이 화면으로 나간다** (``#384`` ㄴ).

    🔴 컷하지 않는다. 복사값이라고 틀린 값이 아니다 — 다만 그날 판정은 **그날이 아니라
    앞 장날의 예측 구간**으로 낸 것이라, 그 사실이 사람에게 보여야 한다.
    """
    named = [r for r in _risks_with_filled_judgment_day(True) if "상황 판정일" in r]
    assert named, "복사값인데 고지가 없다"
    assert "앞 장날" in named[0], named[0]


def test_a_normal_judgment_day_leaves_no_note() -> None:
    """복사값이 아닌 날에는 **안 붙는다.**

    매일 붙으면 신호가 죽는다 — 실 경로에서 이 경로는 504조합 중 27건(5.4%)이다.
    """
    assert not [r for r in _risks_with_filled_judgment_day(False) if "상황 판정일" in r]
