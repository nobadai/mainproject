"""화면 부품 — 여섯 탭이 함께 쓰는 응답 조각.

★ **왜 이런 게 따로 있나.**

이 아래(`app/api/*/query.py`)는 **부서마다 다른 사람이 채웁니다.** 그런데 화면은
한 사람이 만듭니다. 그래서 "어떤 모양으로 주고받나"를 여기 한 군데에 못박아
둡니다. 부서는 이 모양에 값을 담기만 하면 되고, 화면은 이 모양만 그릴 줄 알면
됩니다.

★ **여기 있는 것은 값의 모양이지 화면의 모양이 아닙니다.** 색·굵기·자리는
프론트가 정합니다. 백엔드가 `style` 이나 `className` 을 내려보내면 안 됩니다 —
그러면 화면을 고칠 때마다 백엔드를 고쳐야 합니다.

★ **숫자와 표시를 나눠 담습니다.** `value` 는 사람이 읽을 글자(`"-1,328"`)고
`raw` 는 계산에 쓸 수 있는 수입니다. 화면이 정렬·비교를 하려면 수가 필요한데,
자리 표시(만원·소수점)는 부서마다 다르므로 글자도 같이 받습니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 값의 성격. 색이 아니라 **뜻**이다 — 색은 프론트가 정한다.
Tone = Literal["neutral", "good", "warn", "bad", "info", "sim"]

#: 표 칸을 어느 쪽으로 붙이나.
Align = Literal["left", "right", "center"]


class Stat(BaseModel):
    """큰 숫자 한 칸. 화면 맨 위 요약 줄에 쓴다."""

    label: str = Field(description="무엇의 값인가")
    value: str = Field(description="사람이 읽을 값. 자릿점까지 넣어서 준다")
    unit: str | None = Field(default=None, description="원 · kg · 만원 같은 단위")
    detail: str | None = Field(default=None, description="아래 작게 붙는 한 줄")
    tone: Tone = "neutral"
    raw: float | None = Field(default=None, description="계산에 쓸 수. 없으면 None")


class Badge(BaseModel):
    """상단 알약. 상태를 한눈에 알리는 짧은 말."""

    text: str
    tone: Tone = "neutral"


class Note(BaseModel):
    """설명 상자. 표나 그래프 밑에 붙는 문장.

    ★ **공란과 0 을 구분해서 적는 자리이기도 합니다.** 값이 없는 이유를
    화면에 안 적으면 보는 사람이 0 으로 읽습니다.
    """

    text: str
    tone: Tone = "neutral"


class Column(BaseModel):
    """표의 머리 한 칸."""

    key: str = Field(description="행 딕셔너리에서 꺼낼 이름")
    label: str
    align: Align = "left"
    mono: bool = Field(default=False, description="수치라 폭 고정 글꼴로 볼 것인가")


class Table(BaseModel):
    """표 하나.

    행은 `{칸이름: 값}` 입니다. 값에 `None` 을 넣으면 화면이 **공란**으로
    그립니다 — 0 으로 그리지 않습니다.
    """

    columns: list[Column]
    rows: list[dict[str, str | float | int | None]] = Field(default_factory=list)
    note: Note | None = None
    empty_text: str = Field(
        default="값이 없습니다",
        description="행이 0개일 때 화면에 대신 적을 말. '없음'과 '아직 안 들어옴'은 다르다",
    )


class Series(BaseModel):
    """그래프 선 하나.

    `data` 의 길이는 같은 응답 안 날짜축(`CalendarAxis.days`) 길이와 같아야
    합니다. **값이 없는 날은 `None`** 입니다 — 0 이 아닙니다. 휴장일과
    '아직 안 들어온 날'이 0 으로 그려지면 거짓말이 됩니다.
    """

    name: str
    data: list[float | None]
    tone: Tone = "info"
    dashed: bool = Field(default=False, description="추정값이면 점선")
    width: float = 2.0
    opacity: float = 1.0
    end_dot: bool = Field(default=False, description="마지막 점에 동그라미를 찍나")


class Band(BaseModel):
    """예측 구간 — 위아래 두 선 사이를 칠한다."""

    name: str
    hi: list[float | None]
    lo: list[float | None]
    tone: Tone = "info"


class Marker(BaseModel):
    """그래프 위 한 점에 붙이는 표시. 지급·폐기처럼 그날 일어난 일."""

    index: int = Field(description="날짜축에서 몇 번째 칸인가")
    value: float
    label: str
    tone: Tone = "neutral"


class Chart(BaseModel):
    """날짜축 그래프 하나."""

    label: str = Field(description="읽어주는 도구가 쓸 이름")
    y_min: float
    y_max: float
    y_ticks: list[float]
    y_unit: str = Field(default="", description="눈금 뒤에 붙일 글자. 예 'M'")
    y_labels: list[str] = Field(
        default_factory=list,
        description=(
            "눈금 글자를 **직접** 줄 때 쓴다 (`y_ticks` 와 같은 길이). "
            "★ 값의 단위와 보이는 단위가 다르면 반드시 이걸 쓴다 — "
            "재고는 kg 로 그리는데 눈금은 톤으로 적어야 해서, 숫자 뒤에 "
            "‘t’ 를 붙이기만 하면 10,000kg 이 «10,000t» 이 된다."
        ),
    )
    series: list[Series] = Field(default_factory=list)
    bands: list[Band] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)
    note: Note | None = None
    shade_label: str = Field(
        default="휴장 · 경매 없음",
        description=(
            "회색으로 눕힌 칸이 무엇인지. 탭마다 뜻이 다르다 — 대시보드는 "
            "휴장일이고, 가격 예측은 **모델을 안 쓰는 게이트 구간**이다."
        ),
    )
    x_labels: list[str] = Field(
        default_factory=list,
        description=(
            "가로 눈금 글자. **공용 날짜축을 안 쓰는 그래프**만 채운다 "
            "(재무 12월 30칸 · 판매 수금 15칸). 비면 화면이 날짜축을 쓴다. "
            "칸이 많으면 빈 문자열로 걸러 적는다 — 다 적으면 글자가 겹친다."
        ),
    )


class Day(BaseModel):
    """날짜축 한 칸."""

    date: str = Field(description="YYYY-MM-DD")
    dow: str = Field(description="월 화 수 …")
    market_open: bool = Field(description="그날 경매가 서나")
    survey: bool = Field(description="그날 가격 조사가 있나")


class CalendarAxis(BaseModel):
    """여섯 탭이 **같은 날짜축**을 쓰게 만드는 값.

    ★ 탭마다 날짜축을 따로 만들면 같은 화면에서 그래프가 서로 어긋납니다.
      as_of 선이 탭마다 다른 자리에 서는 것을 실제로 데모에서 봤습니다.
    """

    as_of: str
    as_of_index: int = Field(description="days 에서 as_of 가 몇 번째인가")
    days: list[Day]


class Source(BaseModel):
    """이 값이 어디서 왔나.

    ★ **`filled` 가 이 화면에서 제일 중요한 칸입니다.** 아직 부서가
      `query.py` 를 안 채웠으면 `False` 이고, 화면이 「예시값」 딱지를 붙입니다.
      딱지가 없으면 보는 사람이 데모 숫자를 실적으로 읽습니다.
    """

    filled: bool = Field(description="부서가 실제 값으로 채웠나. False 면 예시값")
    owner: str = Field(description="이 값을 채울 파트")
    note: str | None = Field(default=None, description="어디서 읽어온 값인지 한 줄")


class Card(BaseModel):
    """카드 하나 — 제목 + 내용 한 덩어리.

    ★ **재고·물류와 판매 탭이 이걸로 만들어집니다.** 카드가 15개, 8개라
      칸마다 따로 이름을 붙이면 부서가 하나 더 넣을 때마다 화면을 고쳐야
      합니다. 그래서 목록으로 받습니다 — 부서가 카드를 더하면 화면에
      그대로 늡니다.

    ★ 안에 무엇이든 다 넣을 필요는 없습니다. 채운 것만 그립니다.
    """

    key: str = Field(description="화면이 자리를 기억하는 데 쓴다. 부서 안에서 겹치지 않게")
    title: str
    subtitle: str | None = None
    source_ref: str | None = Field(default=None, description="어느 표를 읽었나. 오른쪽 위에 작게")
    lead: Note | None = Field(default=None, description="표보다 먼저 읽을 안내")
    flow: list[str] = Field(default_factory=list, description="화살표로 잇는 단계")
    stats: list[Stat] = Field(default_factory=list)
    table: Table | None = None
    chart: Chart | None = None
    bullets: list[str] = Field(default_factory=list, description="원칙·주의처럼 줄글 목록")
    footer: str | None = None


class Pane(BaseModel):
    """탭 안의 작은 탭. 재고·물류가 넷으로 나뉜다."""

    key: str
    label: str
    stats: list[Stat] = Field(default_factory=list)
    cards: list[Card] = Field(default_factory=list)
