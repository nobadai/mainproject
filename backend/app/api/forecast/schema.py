"""가격 예측 탭이 받는 모양.

소유: ML 파트 (우리).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.primitives import CalendarAxis, Chart, Note, Source, Table


class ItemCard(BaseModel):
    """품목 하나의 내일 예측 — 대시보드 위쪽 카드에도 같은 값을 쓴다."""

    item: str = Field(description="배추 · 무 · 양파")
    grade: str = Field(description="예측한 등급. 경락가는 특등급")
    spec: str | None = Field(default=None, description="포장 규격. 경락가만 있다")
    target_date: str = Field(description="언제 값인가")
    predicted: int = Field(description="가운데 값. **이것만 보고 사면 안 된다**")
    lower: int = Field(description="구간 아래끝")
    upper: int = Field(description="구간 위끝. 최악을 잡을 때 이 값을 쓴다")
    unit: str = Field(default="원/kg", description="값의 단위")
    ci_width: float = Field(description="구간 폭 ÷ 가운데 값. 클수록 덜 확실하다")
    review: bool = Field(description="폭이 넓어 사람이 한 번 볼 것을 권하나")
    use_recommended: bool = Field(
        description="False 면 이 조합은 '어제 가격 그대로' 가 우리 모델보다 낫다"
    )


class ForecastTab(BaseModel):
    axis: CalendarAxis = Field(description="공용 날짜축")
    items: list[str] = Field(description="고를 수 있는 품목")
    selected: str = Field(description="지금 고른 품목")
    cards: list[ItemCard] = Field(description="세 품목 요약. 대시보드가 같이 쓴다")
    chart: Chart = Field(description="고른 품목의 실측 · 전일예측 · 내일예측")
    accuracy: Table = Field(description="최근 성적 — 우리 오차를 숨기지 않는다")
    quality: Table = Field(description="조합별로 써도 되는지")
    caveat: Note = Field(description="★ 이 값을 어디까지 믿어야 하나")
    source: Source = Field(description="예시값인지 실제 값인지")
