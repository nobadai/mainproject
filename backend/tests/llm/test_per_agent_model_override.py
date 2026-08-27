"""에이전트별 LLM 모델 override 해석 규칙 검증.

`<AGENT>_LLM_*` → `LLM_*` → 기본값 순으로 읽는다.
★ Critic judge 를 생성 측(selector)과 다른 모델로 돌리는 것이 설계서 §6.4 의 요구다.
"""

from app.critic.llm.runtime import get_llm_settings as critic_settings
from app.orchestrator.llm.runtime import get_llm_settings as orchestrator_settings

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
    for module in ("app.orchestrator.llm.runtime", "app.critic.llm.runtime"):
        monkeypatch.setattr(f"{module}.load_dotenv", lambda *a, **k: False)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"ORCHESTRATOR_{key}", raising=False)
        monkeypatch.delenv(f"CRITIC_{key}", raising=False)


def test_shared_model_is_used_when_no_override(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    assert orchestrator_settings().model == "gemma3:4b"
    assert critic_settings().model == "gemma3:4b"


def test_each_agent_can_use_its_own_model(monkeypatch):
    """★ 핵심 — selector 와 judge 가 서로 다른 모델을 쓴다 (§6.4)."""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("ORCHESTRATOR_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("CRITIC_LLM_MODEL", "exaone3.5:7.8b")

    assert orchestrator_settings().model == "qwen2.5:7b"
    assert critic_settings().model == "exaone3.5:7.8b"


def test_override_does_not_leak_across_agents(monkeypatch):
    """오케 전용 설정이 Critic 을 건드리면 안 된다."""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("ORCHESTRATOR_LLM_MODEL", "qwen2.5:7b")

    assert orchestrator_settings().model == "qwen2.5:7b"
    assert critic_settings().model == "gemma3:4b"


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

    orchestrator = orchestrator_settings()
    assert orchestrator.base_url == "http://127.0.0.1:11434"
    assert orchestrator.timeout_seconds == 30


def test_llm_can_be_disabled_per_agent(monkeypatch):
    """Critic 만 LLM 을 끄고 오케는 켜 둘 수 있다."""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("CRITIC_LLM_ENABLED", "false")

    assert orchestrator_settings().enabled is True
    assert critic_settings().enabled is False
