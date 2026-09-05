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

import pytest

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
    """
    monkeypatch.setattr("app.master.service.collect_inputs", lambda *a, **k: _NOT_LOADED)


class _공휴일이_없는_달력:
    """모든 날을 덮고 공휴일은 하나도 없는 달력.

    ★ **오늘까지의 동작과 정확히 같다** — 주말만 걸린다. 그래서 이 fixture 는 기존
      검사들의 답을 하나도 바꾸지 않는다.

    ★ **공휴일 축이 실제로 붙는 경로는 `test_holiday_calendar.py` 가 본다.** 거기서는
      가짜 달력을 직접 꽂아 공휴일과 *"달력에 없는 날"* 을 둘 다 재현한다.
    """

    def is_holiday(self, day: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def 공휴일_달력을_가짜로_준다(monkeypatch: pytest.MonkeyPatch) -> None:
    """문 앞의 공휴일 축을 **DB 대신 가짜로** 준다.

    ★ **입력 적재를 끄는 것과 같은 이유다.** `.env` 의 `DB_HOST` 가 팀 공용 서버라
      `run_procurement` 을 그냥 부르면 `ml_calendar_days` 로 실제 조회가 나간다.
    """
    monkeypatch.setattr("app.master.service.get_calendar", lambda: _공휴일이_없는_달력())
