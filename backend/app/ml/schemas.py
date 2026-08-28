"""ML 가격 예측 계약.

purchase_agent 의 ``ports.get_forecast`` 가 받는 모양과 1:1 로 맞춘다.
IO명세 §1-① 의 반환 형태가 기준이다.

    {"generated_at": "2026-08-26T06:00:00+09:00", "item": "배추",
     "unit": "원/kg", "current_price": 610, "horizon_days": 18,
     "daily": [{"date": "2026-08-27", "predicted": 610,
                "lower": 395, "upper": 831}],
     "model_version": "ops_auc"}

## 계약에 없지만 함께 싣는 것

``is_filled`` · ``use_recommended`` 는 IO명세에 없다. 그래도 싣는 이유:

- **``is_filled``** — 토·일·공휴일은 경매가 없어 예측이 없다. 그 칸은 직전
  개장일 값을 끌어와 채운다. 채운 값과 진짜 예측이 구분되지 않으면 나중에
  이상한 판단이 나왔을 때 원인을 되짚을 수 없다. 18일 중 5~6일이 채운 값이다.
- **``use_recommended``** — 조합에 따라 우리 모델이 "어제 가격 그대로" 보다
  나쁘다. 그런 조합은 쓰면 손해다. 2026-08-27 기준 중도매가·양파가 그렇다.

포트가 무시해도 되지만, 없으면 조용히 틀린 값을 쓰게 된다.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TargetKind = Literal["AUC", "WHSL", "RTL"]
HORIZON_DAYS = 18

#: 예측 대상 품목. 마늘은 원자료 품질 문제로 아직 제외한다
#: (경락가 결측 11% · 피마늘/깐마늘 구분 미확정).
ITEMS = ("배추", "무", "양파")

#: 어떤 값을 예측한 것인지. 매입 파트가 "규격·등급을 명시해달라" 고 요청했다.
#: 경락가는 포장 규격마다 가격이 크게 다르다 — 배추 특등급 하루치에
#: 그물망 10kg(711원/kg)부터 1kg 소포장(11,224원/kg)까지 섞여 있었다.
SPEC = {
    "AUC": {
        "market": "서울가락",
        "grade": "특",
        "desc": {
            "배추": "그물망·파렛트 10kg",
            "무": "상자·파렛트 20kg (2018년 이전 18kg)",
            "양파": "그물망·파렛트 15kg",
        },
        "kg": {"배추": 10, "무": 20, "양파": 15},
    },
    "WHSL": {"market": "가락도매", "grade": "상품(04)", "desc": {}, "kg": {}},
    "RTL": {"market": "서울 소매", "grade": "상품(04)", "desc": {}, "kg": {}},
}


class DailyPoint(BaseModel):
    """하루치 예측. IO명세 §1-① 의 ``daily`` 원소."""

    model_config = ConfigDict(extra="forbid")

    date: date
    predicted: int = Field(gt=0)
    lower: int = Field(gt=0)
    upper: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "DailyPoint":
        if not self.lower <= self.predicted <= self.upper:
            raise ValueError(
                f"구간이 뒤집혔습니다: lower={self.lower} "
                f"predicted={self.predicted} upper={self.upper}"
            )
        return self


class Forecast(BaseModel):
    """한 품목·한 가격종류의 18일 예측."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    item: str
    target_kind: TargetKind
    unit: str
    current_price: int = Field(gt=0)
    horizon_days: int
    model_version: str
    generated_at: datetime
    daily: list[DailyPoint]

    # 계약 밖 — 값을 믿어도 되는지 판단할 근거
    market_name: str | None = None
    grade_name: str | None = None
    spec_desc: str | None = None
    use_recommended: bool | None = None
    quality_note: str | None = None
    filled_count: int = 0

    @model_validator(mode="after")
    def _calendar_axis(self) -> "Forecast":
        """``daily`` 는 **연속 달력일 D+1~D+18** 이어야 한다.

        우리 내부는 개장일 축으로 센다. 변환을 빠뜨리면 날짜가 띄엄띄엄해지고
        판정 기준일(D+14)이 조용히 밀린다. 여기서 막는다.
        """
        if len(self.daily) != self.horizon_days:
            raise ValueError(
                f"daily 가 {len(self.daily)}건입니다. {self.horizon_days}건이어야 합니다."
            )
        for offset, point in enumerate(self.daily, start=1):
            expected = self.as_of + __import__("datetime").timedelta(days=offset)
            if point.date != expected:
                raise ValueError(
                    f"daily[{offset - 1}] 날짜가 {point.date} 입니다. "
                    f"{expected} 여야 합니다 (연속 달력일)."
                )
        return self

    @model_validator(mode="after")
    def _no_lookahead(self) -> "Forecast":
        """예측을 만든 시각이 기준일보다 나중일 수 없다.

        백테스트 성적을 부풀리는 가장 흔한 실수다.
        """
        made = self.generated_at.date()
        if made > self.as_of:
            raise ValueError(f"generated_at({made})이 as_of({self.as_of})보다 나중입니다.")
        return self
