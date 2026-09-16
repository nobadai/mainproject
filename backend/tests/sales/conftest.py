"""판매 테스트 공통 — **네트워크를 타지 않게 막는다.**

🔴 **여기가 없는 동안 판매 테스트는 «우연히» 결정론적이었다** (2026-09-16 실측).

  전략 Planner 의 Gemini 요청이 스키마 때문에 **매번 400 으로 실패**했고
  (`$defs`·`$ref` 미해소), 그래서 어떤 테스트도 모델을 안 켰는데도 늘 규칙 템플릿이
  섰다. 그 400 을 고치자 `_generate_scenarios` 를 부르는 테스트들이 **실제로
  네트워크를 타기 시작했고**, 모델이 회차마다 다른 자세를 골라 단가가 흔들렸다 —
  실행마다 다른 테스트가 깨졌다.

  ```text
  고치기 전   plan_strategies → HTTP 400 → FALLBACK → 템플릿 (늘 같은 값)
  고친 뒤     plan_strategies → 실 Gemini → 자세가 회차마다 다름 → 단가가 흔들림
  ```

★ **두 겹으로 막는다.**

  ```text
  ① 설정을 끈다        SALES_LLM_ENABLED=false  → 부를 조건 자체를 없앤다
  ② 전선을 막는다      _gemini_structured 가 불리면 그 자리에서 실패한다
  ```

  ①만 두면 켜는 테스트가 실수로 네트워크를 탄다. ②가 그것을 **조용한 통과가 아니라
  빨간불**로 만든다.

★ **모델을 켜서 보는 테스트는 그대로 돈다.** 그 테스트들은 `_call_gemini` ·
  `_call_gemini_planner` 를 갈아 끼우므로 ②의 전선까지 내려오지 않는다.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def 판매_LLM_을_끈다(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본은 꺼 둔다. **켜는 테스트가 명시적으로 덮어쓴다.**

    ★ `monkeypatch.setenv` 라 테스트마다 되돌아간다 — 켠 테스트가 다음 테스트로
      새지 않는다.
    """
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


@pytest.fixture(autouse=True)
def 실_LLM_전선을_막는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 뚫리면 **그 자리에서 선다.** 조용히 네트워크를 타는 것보다 낫다."""

    def 부르면_안_된다(**_kwargs: object) -> object:
        raise AssertionError(
            "판매 테스트에서 실 LLM 을 불렀다 — fixture 가 뚫렸다. "
            "모델을 보는 테스트는 _call_gemini · _call_gemini_planner 를 갈아 끼운다"
        )

    monkeypatch.setattr("app.sales.llm.runtime._gemini_structured", 부르면_안_된다)
