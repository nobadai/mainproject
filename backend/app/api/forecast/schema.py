"""가격 예측 탭이 받는 모양.

소유: **ML 파트 (우리)**.

★ **우리가 쓰던 화면(`localhost:3100`)에 있던 것을 그대로 옮겼습니다.**
  가격종류 셋 · 기준일 고르기 · 채점 상태 · 옛 기준 경고 · 18일 전체 ·
  실제값 겹치기 · 게이트 구간 · 리드타임별 표.

  숫자 하나만 크게 띄우면 **틀린 줄 모르고 씁니다.** 지나간 성적을 같이
  보이는 것이 이 화면의 핵심입니다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.primitives import CalendarAxis, Chart, Note, Source, Table


class KindOption(BaseModel):
    """가격 종류 하나. 셋이 서로 다른 단계의 값이라 섞으면 안 된다."""

    kind: str = Field(description="auc · whsl · rtl")
    label: str = Field(description="경락가 · 중도매가 · 소매가")
    role: str = Field(description="이 값이 누구에게 무엇인가")


class BaseDateOption(BaseModel):
    """예측을 만든 날 하나.

    ★ `scored` 는 «몇 건이 실제 가격과 맞춰졌나» 입니다. 기준일이 오래될수록
      대상일이 지나 늘어납니다. 0 이면 아직 결과가 안 나온 것입니다.
    """

    base_dt: str
    total: int = Field(description="그날 낸 예측 건수")
    scored: int = Field(description="그중 실제 가격과 맞춰진 건수")
    pre_fix: bool = Field(
        description=(
            "2026-08-28 이전에 만든 예측인가. **다른 날과 나란히 놓고 비교하면 "
            "안 된다** — 그때는 경락가에 포장 규격이 섞여 있었다"
        )
    )


class ItemCard(BaseModel):
    """품목 하나의 다음 값 — 대시보드 위쪽 카드에도 같은 값을 쓴다."""

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
    gated: bool = Field(
        default=False,
        description=(
            "이 값이 **모델이 아니라 어제값 그대로**인가. 리드타임 3 미만이면 "
            "모델을 안 쓴다 — 어제 가격이 이미 정답에 가까워 모델이 낄 자리가 없다"
        ),
    )


class ChartPoint(BaseModel):
    """그래프가 쓰는 **원시 수치** 한 점.

    ★ 표(`rows`)와 같은 줄인데 **글자가 아니라 수**다. 표는 사람이 읽으려고
      「600–855」처럼 글자로 만들어 두는데, 그래프는 좌표를 계산해야 하므로
      숫자가 필요하다. 화면이 글자를 다시 숫자로 되돌리게 하면 자릿점(`,`)과
      단위 때문에 조용히 틀린다.

    ★ **`null` 은 0 이 아니라 「없음」이다.** 채점 안 된 날은 `actual` 이 없다.
      그걸 0 으로 내리면 그래프가 «값이 폭락했다» 로 보인다.
    """

    lead: int = Field(description="리드타임. 0 이 기준일 그날")
    target_dt: str = Field(description="대상일 YYYY-MM-DD")
    pred: float | None = Field(description="예측 가운데 값")
    lo: float | None = Field(description="구간 아래끝")
    hi: float | None = Field(description="구간 위끝")
    actual: float | None = Field(description="실제값. 아직 안 지난 날이면 None")
    err_pct: float | None = Field(default=None, description="오차율 %")
    anchor: float | None = Field(description="출발점 (어제값·7일평균 섞음)")
    gated: bool = Field(default=False, description="모델을 안 쓰고 출발점을 그대로 낸 칸")


class ForecastTab(BaseModel):
    #  고르는 것
    kinds: list[KindOption] = Field(description="가격 종류 셋")
    selected_kind: str = Field(description="지금 고른 종류")
    items: list[str] = Field(description="고를 수 있는 품목")
    selected: str = Field(description="지금 고른 품목")
    base_dates: list[BaseDateOption] = Field(description="예측을 만든 날 목록 (최신순)")
    selected_base_dt: str = Field(description="지금 고른 기준일")
    base_dates_truncated: bool = Field(
        description="목록이 상한에 닿아 잘렸나. **잘린 것을 안 알리면 «지워졌나» 로 보인다**"
    )

    #  알림
    notice: Note | None = Field(
        default=None, description="옛 기준으로 만든 예측을 골랐을 때의 경고"
    )

    #  값
    cards: list[ItemCard] = Field(description="세 품목 요약. 대시보드가 같이 쓴다")
    axis: CalendarAxis = Field(
        description="기준일 + 대상일 18개. **회색 칸은 게이트 구간**이다"
    )
    chart: Chart = Field(description="18일 예측 · 구간 · 실제값")
    rows: Table = Field(description="리드타임별 한 줄씩 — 예측 · 구간 · 실제 · 오차")
    points: list[ChartPoint] = Field(
        default_factory=list,
        description="그래프가 쓰는 원시 수치. 표와 같은 줄인데 글자가 아니라 수다",
    )
    gate_lead: int = Field(description="이 리드타임 미만은 모델을 안 쓴다")
    quality_note: str | None = Field(default=None, description="이 조합의 판정 근거")

    #  믿을 만한가
    accuracy: Table = Field(description="봉인 개봉 실측 — 우리 오차를 숨기지 않는다")
    quality: Table = Field(description="조합별로 써도 되는지")
    caveat: Note = Field(description="★ 이 값을 어디까지 믿어야 하나")
    source: Source = Field(description="예시값인지 실제 값인지")
