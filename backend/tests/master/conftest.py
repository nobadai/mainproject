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
