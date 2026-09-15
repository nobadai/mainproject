"""에이전트별 LLM 모델 override 해석 규칙 검증.

`<AGENT>_LLM_*` → `LLM_*` → 기본값 순으로 읽는다.

★ **오케 selector 쪽 검사는 2026-09-08 에 걷어냈다.**
  `cycle_llm/runtime.py` 를 지웠고 `ORCHESTRATOR_LLM_*` 을 읽는 코드가 남지 않았다.
  남은 검사는 Critic 하나가 접두사 규칙을 그대로 지키는지를 본다.
"""

from app.master.critic.llm.runtime import get_llm_settings as critic_settings

_KEYS = (
    "LLM_ENABLED",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
)


def _clear(monkeypatch):
    """.env 내용과 무관하게 시작점을 고정한다.

    ★ 런타임이 매번 `load_dotenv(.env)` 를 부르므로, 지운 변수가 .env 값으로 되살아난다.
      이 테스트가 보려는 것은 **해석 순서**이지 .env 내용이 아니므로 로딩 자체를 끊는다.
    """
    monkeypatch.setattr("app.master.critic.llm.runtime.load_dotenv", lambda *a, **k: False)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"CRITIC_{key}", raising=False)


def test_shared_model_is_used_when_no_override(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    assert critic_settings().model == "gemma3:4b"


def test_agent_override_wins_over_shared(monkeypatch):
    """★ 핵심 — Critic judge 는 공통 모델과 다른 모델을 쓸 수 있다 (§6.4)."""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("CRITIC_LLM_MODEL", "exaone3.5:7.8b")

    assert critic_settings().model == "exaone3.5:7.8b"


def test_empty_override_falls_back_to_shared(monkeypatch):
    """빈 문자열은 '미설정'으로 본다 — .env 에 키만 남겨 둔 경우."""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("CRITIC_LLM_MODEL", "")
    assert critic_settings().model == "gemma3:4b"


def test_all_settings_are_overridable_per_agent(monkeypatch):
    """모델뿐 아니라 호스트·타임아웃·재시도도 에이전트별로 나눌 수 있다."""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("CRITIC_LLM_BASE_URL", "http://127.0.0.1:22222")
    monkeypatch.setenv("CRITIC_LLM_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("CRITIC_LLM_MAX_RETRIES", "0")

    critic = critic_settings()
    assert critic.base_url == "http://127.0.0.1:22222"
    assert critic.timeout_seconds == 60
    assert critic.max_retries == 0


def test_llm_can_be_disabled_per_agent(monkeypatch):
    """공통으로 켜 두고 Critic 만 끌 수 있다."""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("CRITIC_LLM_ENABLED", "false")

    assert critic_settings().enabled is False
