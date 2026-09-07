"""마스터 테스트 공통 — **네트워크를 타지 않게 막는다.**

★ 여기 있는 fixture 하나가 지키는 것: `/master/ask` 경로가 **⑥(사용자 응답 생성)
  때문에 조용히 실 LLM 을 부르는 일**이 없게 한다.

  ①(의도 분류)은 `ask(request, service=...)` 로 갈아 끼울 수 있어 각 테스트가
  막아 왔는데, ⑥은 **API 를 통해 들어오면 갈아 끼울 자리가 없다** (`TestClient` 가
  라우터를 부르므로 인자를 넣을 수 없다). 그래서 모듈 경계에서 막는다.

  실 모델 채점은 `-m llm` 테스트가 서비스를 **직접** 부르므로 이 fixture 의 영향을
  받지 않는다.
"""

from __future__ import annotations

from datetime import date

import pytest
from 개장정본_격리 import (
    개장_정본_이름을_가져간_모듈들,
)

from app.master.inputs import MasterInputs, SourcedInput
from app.master.llm.answer_runtime import NarrativeService
from app.master.llm.runtime import LLMSettings

_OFFLINE = LLMSettings(
    enabled=False,  # 꺼 두면 프로바이더를 만들지도, 부르지도 않는다
    provider="disabled",
    model="",
    base_url="",
    timeout_seconds=1.0,
    max_retries=0,
    max_output_tokens=256,
    effort=None,
)


class _NeverCalled:
    def generate(self, system: str, user: str, schema: dict) -> str:
        raise AssertionError("테스트에서 실 LLM 을 불렀다 — fixture 가 뚫렸다")


@pytest.fixture(autouse=True)
def 응답_생성_LLM_을_끈다(monkeypatch: pytest.MonkeyPatch) -> None:
    """⑥을 꺼 둔다. **답은 그대로 나온다** — 숫자는 규칙이 만들기 때문이다.

    문장이 붙는지 보고 싶은 테스트는 `NarrativeService` 를 직접 만들어 쓴다
    (`test_answer.py`).
    """
    monkeypatch.setattr(
        "app.master.ask_service.get_narrative_service",
        lambda: NarrativeService(_OFFLINE, _NeverCalled()),
    )


_NOT_LOADED = MasterInputs(
    forecast=SourcedInput("forecast", None, "MISSING", "-", "테스트에서는 적재하지 않는다"),
    confirmed_orders=SourcedInput("confirmed_orders", None, "MISSING", "-", ""),
    policy_values=SourcedInput("policy_values", None, "MISSING", "-", ""),
)


@pytest.fixture(autouse=True)
def 입력_적재를_끈다(monkeypatch: pytest.MonkeyPatch) -> None:
    """마스터가 실어 주는 입력 3종(§3.2.5)을 **DB 대신 빈 값으로** 준다.

    ★ **공용 DB 를 건드리지 않기 위해서다.** `.env` 의 `DB_HOST` 가 팀 공용 서버라
      `run_procurement` 을 그냥 부르면 실제 조회가 나간다.

    ★ 빈 값이어도 기존 테스트는 그대로 돈다 — 셋이 없으면 매입이 `missing_data` 로
      답하는 것이 **원래 계약**이고, 그 경로를 테스트가 이미 검사하고 있다.
      실 적재는 `test_inputs.py` 가 따로 본다.

    🔴 **판매 경로는 `load_forecast` 를 따로 부른다** (M-1). 판매가 나르는 것은 예측
      하나뿐이라 셋을 모으지 않는다 — 그래서 `collect_inputs` 만 막으면 `run_sales` 가
      **조용히 실 DB 를 친다.** 두 자리를 같이 막는다.
    """
    monkeypatch.setattr("app.master.service.collect_inputs", lambda *a, **k: _NOT_LOADED)
    monkeypatch.setattr("app.master.service.load_forecast", lambda *a, **k: _NOT_LOADED.forecast)


class _공휴일이_없는_달력:
    """모든 날을 덮고 공휴일은 하나도 없는 달력.

    ★ **오늘까지의 동작과 정확히 같다** — 주말만 걸린다. 그래서 이 fixture 는 기존
      검사들의 답을 하나도 바꾸지 않는다.

    ★ **공휴일 축이 실제로 붙는 경로는 `test_holiday_calendar.py` 가 본다.** 거기서는
      가짜 달력을 직접 꽂아 공휴일과 *"달력에 없는 날"* 을 둘 다 재현한다.
    """

    def is_holiday(self, day: object) -> bool:
        return False


class _주말만_쉬는_시장:
    """토·일에 장이 안 서고 평일에는 다 서는 **개장 축** 가짜 (`MarketCalendar`).

    ⚠️ **실제 달력과 다르다** — 실 표에서는 토요일 대부분이 개장이다 (2026년 45일).
      여기서 토요일을 닫는 것은 **기존 검사들의 답을 안 바꾸려는 것**이고, 개장 축이
      실제로 붙는 경로는 `test_execution_calendar*.py` 가 가짜를 직접 꽂아 본다.

    ★ 이 fixture 로는 *"봉투가 실렸는가"* 만 재고, *"어느 날이 실렸는가"* 는 못 잰다.
    """

    def is_market_open(self, day: date) -> bool:
        return day.weekday() < 5


@pytest.fixture(autouse=True)
def 개장_정본_적재를_막는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """개장 정본(`master_day_openings`) 조회·적재를 **DB 대신 가짜로** 받는다.

    🔴 **`record_day_opening` 이 예외를 삼킨다** (이력이 없는 것보다 하루를 못 여는
       것이 나쁘므로). 그래서 안 막으면 검사가 **조용히 실 DB 를 치고도 초록**이다.

    🔴 **조회(`read_day_opening`)도 같이 막는다.** 이쪽은 예외를 삼키지도 않고 값을
       돌려주므로, 안 막으면 검사의 답이 **그날 실 DB 에 행이 있느냐로 갈린다** —
       `day_gate` 의 근사 분기가 정확히 그렇게 무너져 있었다.

    ★ 이름을 가져간 모듈까지 훑어 막는다 — `개장_정본_이름을_가져간_모듈들` 참조.

    ★ 정본 자체를 재는 검사는 `test_day_opening_repository.py` 가 가짜 커넥션을 직접
      꽂고, **격리가 실제로 섰는지는 `test_db_isolation.py` 가 잰다.**
    """
    가짜 = {
        "read_day_opening": lambda **kw: None,
        "record_day_opening": lambda **kw: True,
    }
    for 이름, 대체 in 가짜.items():
        for 모듈 in 개장_정본_이름을_가져간_모듈들(이름):
            monkeypatch.setattr(모듈, 이름, 대체)


@pytest.fixture(autouse=True)
def 개장_관문을_통과시킨다(monkeypatch: pytest.MonkeyPatch) -> None:
    """개장 관문을 **DB 없이 통과**시킨다.

    🔴 **`run_procurement` 이 첫 관문에서 `is_open` 을 묻는다** (2026-09-06 · 계약).
       등록소가 전역이라 다른 검사가 하루 넘김을 등록해 두면, 그 뒤 실행이 **실 DB 로
       개장 여부를 물으러 나간다.**

    ★ **관문 자체를 재는 검사는 `test_day_gate.py` 가 가짜를 직접 꽂는다.** 여기서는
      *"관문 때문에 다른 검사가 막히지 않는다"* 만 보장한다 — 공휴일 달력을 가짜로
      주는 것과 같은 이유다.
    """
    from app.master.day_gate import DayGate

    monkeypatch.setattr(
        "app.master.service.check_day_gate",
        lambda as_of, **kw: DayGate(
            as_of=as_of, gate="PASS", result="ALREADY_OPENED", last_opened_date=as_of
        ),
    )


@pytest.fixture(autouse=True)
def 공휴일_달력을_가짜로_준다(monkeypatch: pytest.MonkeyPatch) -> None:
    """문 앞의 공휴일 축과 봉투의 개장 축을 **DB 대신 가짜로** 준다.

    ★ **입력 적재를 끄는 것과 같은 이유다.** `.env` 의 `DB_HOST` 가 팀 공용 서버라
      `run_procurement` 을 그냥 부르면 `ml_calendar_days` 로 실제 조회가 나간다.

    ⚠️ **둘 다 꽂는다.** 축이 둘이라 한쪽만 막으면 나머지가 조용히 실 DB 를 친다.
    """
    monkeypatch.setattr("app.master.service.get_calendar", lambda: _공휴일이_없는_달력())
    monkeypatch.setattr("app.master.service.get_market_calendar", lambda: _주말만_쉬는_시장())
