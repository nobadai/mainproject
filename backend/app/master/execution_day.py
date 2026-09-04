"""
execution_day.py — **실행일은 평일만**. 토·일에는 안 돈다.

시뮬레이션은 하루씩 앞으로 걸어가며 매입 판단을 만든다. 그 걸음을 **평일에만**
딛는다.

```text
월 화 수 목 금 토 일 월
 ●  ●  ●  ●  ●  ○  ○  ●     ● 실행일 (판단한다)   ○ 쉬는 날 (안 돈다)
```

★ **왜 주말을 거르는가.** 토·일에는 시장이 안 서서 **ML 예측이 없다.** 실측
  (2026-09-04)으로 ML 예측 기준일 12개가 전부 평일이고 주말 기준일은 0건이다.
  주말 대상일 값은 전부 `is_filled=True` — 직전 개장일 값의 복사다. 없는 값을
  복사본으로 채워 판단하면, 그 판단은 시장을 본 것이 아니라 **금요일을 두 번 본
  것**이다.

🔴 **이 모듈로 경과일수를 세면 안 된다.**

  주말에 **판단을 안 할 뿐**, 주말이 사라지는 것은 아니다. 재고는 토·일에도
  늙고, 도착일·지급일도 달력일로 온다. 그래서 금요일 다음 실행일은 월요일이고
  **그 사이는 3일**이다 — 1일이 아니다.

  ```text
  실행일        평일만          ← 이 모듈이 답한다
  경과일수      달력일 그대로    ← 이 모듈은 답하지 않는다
  ```

  경과일수의 주인은 **`app/master/verifier.py` 의 `_day_gap`** 이다 (123행,
  docstring: *"`YYYY-MM-DD` 두 개의 일수 차이. calendar day 다 — 영업일 보정
  없음 (N5)."*). 같은 사실의 주인이 둘이 되면 언젠가 둘이 갈리고, 갈린 날
  아무도 어느 쪽이 맞는지 말해 주지 않는다. 여기서 날짜 차를 세는 코드를 쓰지
  않는 이유이고, `tests/master/test_execution_day.py` 가 이 파일의 **원문을
  읽어** 그것을 막는다.

⚠️ **한계 — 주말만 거른다. 공휴일은 못 거른다.**

  설·추석·대체공휴일에도 이 모듈은 평일이라고 답한다. 그날 시장이 쉬면 ML 예측이
  없는 것은 주말과 똑같은데, 여기서는 안 걸린다.

  **같은 한계가 이미 적혀 있다** — `app/purchase_agent/constraints.yaml:62`
  (*"⚠️ **주말만 피한다 — 공휴일은 못 피한다.**"*). 거기서 정한 이유가 여기서도
  그대로다: 공휴일 달력을 들이지 않기로 했고(같은 파일 253행), 그 결정을 이
  모듈이 뒤집지 않는다. 넓히려면 저 자리와 **같이** 넓혀야 한다.
"""

from __future__ import annotations

from datetime import date, timedelta

__all__ = ["is_execution_day", "next_execution_day"]

# `date.weekday()` 가 토요일에 주는 값. 이 값 미만이 평일이다 (월 0 … 금 4).
_SATURDAY = 5


def is_execution_day(day: date) -> bool:
    """이 날 판단을 도는가. 월~금이면 참, 토·일이면 거짓.

    ★ **날짜만 본다.** 공휴일은 모른다 (모듈 docstring 의 한계).
    """
    return day.weekday() < _SATURDAY


def next_execution_day(day: date) -> date:
    """`day` **다음**의 실행일. `day` 자신은 세지 않는다.

    ```text
    목 → 금   금 → 월   토 → 월   일 → 월
    ```

    ⚠️ 금요일에 물으면 월요일이 나오지만, 그 사이가 **1일이라는 뜻이 아니다.**
      달력으로는 3일이다. 이 함수는 *"다음에 언제 도는가"* 만 답하고
      *"며칠 지났는가"* 는 답하지 않는다 (모듈 docstring 의 🔴).
    """
    following = day + timedelta(days=1)
    while not is_execution_day(following):
        following = following + timedelta(days=1)
    return following
