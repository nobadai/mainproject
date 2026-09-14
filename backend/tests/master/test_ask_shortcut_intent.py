"""「마스터에게 묻기」 바로가기는 **분류 LLM 없이** 정해진 의도로 조회한다.

2026-09-14 실측 (dev@34a00f9) · 바로가기 「자금」 을 누르면 문장을 `/master/ask` 로 보냈고,
분류 LLM 이 429 로 죽자 규칙 대체도 UNKNOWN 이라 「알아듣지 못했습니다」 가 나왔다.
이제 버튼은 `MasterConsole.tsx` 의 `SHORTCUT` 에 적힌 `Intent` 를 `/ask/execute` 로 보낸다.

🔴 **실 DB 에 닿지 않는다.** 부서 포트는 대역이고 적재 함수는 가로챈다.
🔴 **실 LLM 을 부르지 않는다.** 분류 서비스는 부르면 터지는 대역으로 잠근다.

```text
① 프론트 상수에서 네 버튼의 intent 를 읽는다 (파싱 · 키 넷)
② 네 intent 가 백엔드 Intent 스키마와 분류 검증기(validate_intent)를 통과한다
③ 네 intent 는 분류기가 같은 문장에 낼 값과 같다 (STATUS_QUERY · agents=[버튼 부서])
④ execute 에 넣으면 분류를 안 부르고 조회가 돈다 · ⑥ 문장 LLM 이 죽어도 답이 나간다
```
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from app.master import AgentReply, AgentRequest, ExecutionMetadata, ask_service, wiring
from app.master.ask_schemas import AskExecuteRequest
from app.master.llm import answer_runtime
from app.master.llm import runtime as intent_runtime
from app.master.llm.runtime import LLMSettings, validate_intent
from app.master.llm.schemas import Intent

_BACKEND = Path(__file__).resolve().parents[2]
_CONSOLE = _BACKEND.parent / "frontend" / "src" / "components" / "console" / "MasterConsole.tsx"
_KEYS = {"purchase", "inventory", "finance", "sales"}

AS_OF = date(2026, 1, 26)


# ── ① 프론트 상수 읽기 ───────────────────────────────────────────────────


def _shortcuts() -> dict[str, dict]:
    """`const SHORTCUT … = { … };` 블록에서 버튼마다 `utterance` 와 `intent` 를 꺼낸다."""
    text = _CONSOLE.read_text(encoding="utf-8")
    blocks = re.findall(r"\nconst SHORTCUT\b.*?\n\};", text, flags=re.DOTALL)
    assert len(blocks) == 1, (
        f"MasterConsole.tsx 에서 `const SHORTCUT` 블록을 하나로 못 찾았다: {len(blocks)}"
    )
    entries = re.findall(
        r"\n  (\w+): \{\s*label: \"([^\"]*)\","
        r"\s*utterance: \"([^\"]*)\","
        r"\s*intent: (\{[^{}]*\}),\s*\},",
        blocks[0],
    )
    out: dict[str, dict] = {}
    for key, label, utterance, literal in entries:
        # TS 객체 리터럴 → JSON: 따옴표 없는 키만 감싼다 (값은 이미 JSON 모양이다).
        as_json = re.sub(r"([{,]\s*)(\w+):", r'\1"\2":', literal)
        out[key] = {"label": label, "utterance": utterance, "intent": json.loads(as_json)}
    return out


def test_프론트_바로가기는_네_버튼이_intent_를_든다():
    shortcuts = _shortcuts()
    assert set(shortcuts) == _KEYS, f"바로가기 키가 갈렸다: {sorted(shortcuts)}"


# ── ② · ③ 스키마 · 분류기와 같은 값 ─────────────────────────────────────


@pytest.mark.parametrize("key", sorted(_KEYS))
def test_바로가기_intent_는_스키마와_분류_검증을_통과하고_분류기_값과_같다(key):
    shortcut = _shortcuts()[key]
    raw = json.dumps(shortcut["intent"], ensure_ascii=False)

    intent = Intent.model_validate_json(raw)
    # 분류기가 LLM 출력에 거는 검증 그대로 — 조회할 수 없는 부서면 여기서 걸린다.
    checked = validate_intent(raw, shortcut["utterance"])

    assert checked == intent
    assert intent.action == "STATUS_QUERY"
    assert intent.agents == [key], f"{key} 버튼이 다른 부서를 부른다: {intent.agents}"
    assert intent.item is None
    # 🔴 확인 없이 도는 종류여야 버튼 한 번에 답이 나온다 (확인 게이트 판정과 같은 함수).
    assert intent_runtime._needs_confirmation(intent) is False


def test_매입_버튼은_매입안_생성이_아니다():
    """🔴 버튼 하나로 호출 예산 12회와 매입 LLM 을 태우는 길에 들어가지 않는다."""
    purchase = _shortcuts()["purchase"]
    assert purchase["intent"]["action"] != "PROCUREMENT_RUN"
    assert "사야" not in purchase["utterance"]
    assert purchase["label"] != "오늘 매입"


# ── ④ execute 는 분류 없이 조회를 돌린다 ────────────────────────────────


class _터지는_프로바이더:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        raise RuntimeError("429 quota — 이 검사에서는 LLM 이 죽어 있다")


def _port(seen: list[AgentRequest]):
    def port(request: AgentRequest):
        seen.append(request)
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status="ok",
            payload={"available_cash": 1},
        )
        meta = ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("status_tool",),
            tool_order=(1,),
        )
        return reply, meta

    return port


@pytest.fixture(autouse=True)
def clean_wiring():
    wiring.reset()
    yield
    wiring.reset()


@pytest.fixture
def 잠금(monkeypatch):
    """분류는 부르면 터지고, ⑥ 문장은 켜진 설정에 죽은 프로바이더를 준다."""
    monkeypatch.setattr(ask_service.persistence, "record_status", lambda **kw: None)

    def _분류_금지(*args, **kwargs):
        raise AssertionError("바로가기 실행이 분류 LLM 을 불렀다")

    monkeypatch.setattr(ask_service, "get_intent_service", _분류_금지)
    monkeypatch.setattr(intent_runtime.IntentService, "classify", _분류_금지)

    provider = _터지는_프로바이더()
    settings = LLMSettings(
        enabled=True,
        provider="gemini",
        model="locked-test-model",
        base_url="http://127.0.0.1:1",
        timeout_seconds=0.1,
        max_retries=1,
        max_output_tokens=256,
        effort=None,
    )
    # conftest 는 ⑥을 꺼 둔다. 여기서는 **켜진 채 죽은** 상태로 덮어 시연 날 429 를 흉내 낸다.
    monkeypatch.setattr(
        ask_service,
        "get_narrative_service",
        lambda: answer_runtime.NarrativeService(settings, provider),
    )
    return provider


@pytest.mark.parametrize("key", sorted(_KEYS))
def test_바로가기_intent_를_execute_에_넣으면_분류_없이_조회가_돈다(key, 잠금):
    seen: list[AgentRequest] = []
    wiring.register(key, _port(seen))
    intent = Intent.model_validate(_shortcuts()[key]["intent"])

    response = ask_service.execute(
        AskExecuteRequest(intent=intent, as_of=AS_OF, policy_version="v1.3")
    )

    assert response.outcome == "STATUS_ANSWERED"
    assert response.request_id  # 서버가 업무 키를 발급했다
    assert [(r.agent, r.mode) for r in seen] == [(key, "STATUS_QUERY")]
    # ①은 안 불렀다고 **정직하게** 말한다 — SUCCESS 로 꾸미지 않는다.
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    assert response.llm_fallback_used is False
    # ⑥이 죽어도 규칙이 만든 답은 나간다.
    assert response.answer is not None and response.answer.text
    assert response.answer.narrative is None
    assert response.answer.llm_status == "FALLBACK"
    # ⑥은 한 번 시도하고 곧바로 접는다 — 429 에 매달려 버튼이 멈추지 않는다.
    assert 잠금.calls == 1
