"""`llm_status` 네 값의 뜻 — **팀 공통 어휘라 한 곳에 고정한다.**

매입이 8/31 에 지적했다 — *"mix 가 None 이면 DISABLED 로 떨어지는데 그 경로가
둘이다. LLM 은 켜져 있었는데 DISABLED 로 나간다."*

★ **새로 정하는 규칙이 아니다.** 마스터·Critic 이 이미 이대로 쓴다. 다만 뜻이
  어디에도 안 적혀 있어 각 파트가 남의 코드를 읽고 유추해야 했다 — 이 검사가
  **두 파트가 같은 뜻으로 쓰는지**를 붙잡아 둔다.
"""

from __future__ import annotations

import pytest

from app.critic.llm import runtime as critic_runtime
from app.critic.llm.schemas import SanitizedLLMContext
from app.master.envelope import LLMStatus
from app.master.llm import runtime as master_runtime


def test_네_값뿐이다():
    assert set(LLMStatus.__args__) == {"SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"}


# ── 설정이 꺼짐 → DISABLED ───────────────────────────────────────────────


def _master(enabled: bool) -> master_runtime.IntentService:
    settings = master_runtime.LLMSettings(
        enabled=enabled,
        provider="ollama",
        model="gemma3:4b",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1.0,
        max_retries=0,
        max_output_tokens=256,
        effort=None,
    )
    return master_runtime.IntentService(settings, master_runtime.UnavailableProvider())


def test_마스터_설정이_꺼지면_DISABLED():
    assert _master(enabled=False).classify("배추 얼마나 사?").llm_status == "DISABLED"


def test_마스터_안_부르기로_하면_SKIPPED_TEMPLATE():
    """켜져 있는데 **이번엔 부를 조건이 아니었다** — 빈 발화문."""
    assert _master(enabled=True).classify("   ").llm_status == "SKIPPED_TEMPLATE"


def test_마스터_불렀는데_실패하면_FALLBACK():
    """답은 나간다 — 규칙이 만든 답이다. **실패지 오류가 아니다.**"""
    result = _master(enabled=True).classify("배추 얼마나 사?")

    assert result.llm_status == "FALLBACK"
    assert result.llm_fallback_used is True
    assert result.intent.action == "UNKNOWN"


# ── Critic 도 같은 뜻으로 쓴다 ───────────────────────────────────────────


def _critic(enabled: bool) -> critic_runtime.JudgeService:
    settings = critic_runtime.LLMSettings(
        enabled=enabled,
        provider="ollama",
        model="qwen2.5:7b",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1.0,
        max_retries=0,
    )
    return critic_runtime.JudgeService(settings, critic_runtime.UnavailableProvider())


def _ctx(rationale: str = "설명문") -> SanitizedLLMContext:
    return SanitizedLLMContext(
        domain="CRITIC", cycle="A", signals=[], facts=[], binding_constraints=[],
        rationale=rationale,
    )


def test_Critic_설정이_꺼지면_DISABLED():
    got = _critic(enabled=False).judge(_ctx(), runtime_ready=True, end_stage_reached=False)
    assert got.llm_status == "DISABLED"


def test_Critic_안_부르기로_하면_SKIPPED_TEMPLATE():
    """앞 계층이 FAIL 로 끊겼으면 L5 는 안 돈다 — **켜져 있어도 안 부른다.**"""
    got = _critic(enabled=True).judge(_ctx(), runtime_ready=True, end_stage_reached=True)
    assert got.llm_status == "SKIPPED_TEMPLATE"


def test_Critic_불렀는데_실패하면_FALLBACK():
    got = _critic(enabled=True).judge(_ctx(), runtime_ready=True, end_stage_reached=False)
    assert got.llm_status == "FALLBACK"


@pytest.mark.parametrize("enabled", [True, False])
def test_두_파트가_같은_값을_쓴다(enabled: bool):
    """🔴 어휘가 갈리면 부서마다 다른 뜻이 된다 — 그게 이 검사의 목적이다."""
    master = _master(enabled).classify("   ").llm_status
    critic = _critic(enabled).judge(_ctx(), runtime_ready=True, end_stage_reached=True).llm_status

    assert master == critic
