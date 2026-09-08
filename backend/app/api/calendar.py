"""공용 날짜축 — 여섯 탭이 **같은 12칸**을 본다.

★ **왜 공용인가.** 탭마다 날짜를 따로 만들면 as_of 선이 탭마다 다른 자리에
  섭니다. 데모에서 실제로 그랬습니다. 그래서 축을 한 군데서만 만듭니다.

★ **축은 달력일입니다. 개장일이 아닙니다.** 화면은 사람이 보는 것이라
  토·일이 빠지면 오히려 헷갈립니다. 대신 그날 장이 섰는지를 `market_open`
  으로 같이 내려보내, 화면이 흐리게 그립니다.

  (우리 모델 안쪽은 **영업일 축**으로 배웁니다 — 그건 그대로 두고, 밖으로
  나올 때만 달력일로 폅니다. `app/ml/service.py` 가 하던 일과 같습니다.)

소유: ML 파트.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.api.primitives import CalendarAxis, Day

#: as_of 기준으로 뒤로 며칠, 앞으로 며칠을 보이나.
BACK_DAYS = 8
FORWARD_DAYS = 3

_DOW = ("월", "화", "수", "목", "금", "토", "일")

#: 가락시장 휴장 규칙 (§5.8 실측). 일요일 + 신정 + 명절 당일~+2 + 8월 첫 토요일.
#: 여기서는 **일요일과 신정만** 본다 — 명절은 `ref_holiday` 를 봐야 하는데
#: 이 화면이 그 표에 붙기 전까지는 넘겨짚지 않는다. 넘겨짚으면 틀린 날을
#: 회색으로 칠하게 되고, 그건 값이 없는 것보다 나쁘다.
def _market_open(day: date) -> bool:
    if day.weekday() == 6:  # 일요일
        return False
    if (day.month, day.day) in ((1, 1), (1, 2)):
        return False
    return True


def _survey(day: date) -> bool:
    """가격 조사가 있는 날. 조사 축은 토요일이 빠진다 (경매 축과 다르다)."""
    return _market_open(day) and day.weekday() != 5


def build_axis(as_of: date) -> CalendarAxis:
    """as_of 를 가운데 두고 앞뒤로 편 날짜축."""
    start = as_of - timedelta(days=BACK_DAYS)
    days = [
        Day(
            date=(start + timedelta(days=i)).isoformat(),
            dow=_DOW[(start + timedelta(days=i)).weekday()],
            market_open=_market_open(start + timedelta(days=i)),
            survey=_survey(start + timedelta(days=i)),
        )
        for i in range(BACK_DAYS + FORWARD_DAYS + 1)
    ]
    return CalendarAxis(as_of=as_of.isoformat(), as_of_index=BACK_DAYS, days=days)
