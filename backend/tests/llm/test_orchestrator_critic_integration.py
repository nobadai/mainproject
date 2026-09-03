"""오케 / Critic 응답에 LLM 상태 필드가 실리는지 + Ollama 장애 시 Core 생존 검증.

Finance / Logistics 통합테스트와 같은 방식이다 — Provider 를 실패시켜 놓고
결정론 결과가 그대로 남는지 본다. 네트워크에 나가지 않는다.
"""

from datetime import date

from app.critic.llm.judge import JudgeRunner
from app.critic.llm.runtime import JudgeService, get_llm_settings
from app.critic.llm.runtime import LLMSettings as CriticLLMSettings
from app.orchestrator.interpretation import build_orchestrator_context, enrich_orchestrator_response
from app.orchestrator.llm.runtime import LLMSettings as OrchestratorLLMSettings
from app.orchestrator.llm.runtime import SelectionService
from app.orchestrator.schemas import BandOut, ClipResultOut, ProcurementResponse

_LLM_FIELDS = {
    "interpretation",
    "llm_status",
    "llm_provider",
    "llm_model",
    "llm_attempts",
    "llm_fallback_used",
}


class FailingProvider:
    def generate(self, context, *, retry_guidance=None):
        del context, retry_guidance
        raise RuntimeError("ollama unavailable")


def _failing_selection_service() -> SelectionService:
    return SelectionService(
        OrchestratorLLMSettings(
            enabled=True,
            provider="ollama",
            model="gemma3:4b",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        FailingProvider(),
    )


def _failing_judge_service() -> JudgeService:
    return JudgeService(
        CriticLLMSettings(
            enabled=True,
            provider="ollama",
            model="gemma3:4b",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        FailingProvider(),
    )


def _clip(scenario_id: str, total_kg: float, *, clipped: bool) -> ClipResultOut:
    return ClipResultOut(
        scenario_id=scenario_id,
        clipped_qty_kg={"배추": total_kg},
        total_kg=total_kg,
        original_total_kg=total_kg,
        clip_ratio=1.0 if not clipped else 0.5,
        clipped=clipped,
        over_clipped=False,
        binding_constraints=["cap_total_kg"] if clipped else [],
        identity_problems=[],
        infeasible=False,
        clipped_amount_krw=total_kg * 1000,
    )


def _procurement_response() -> ProcurementResponse:
    return ProcurementResponse(
        as_of=date(2026, 8, 25),
        snapshot_id="SNAP-1",
        runtime_status="READY",
        band=BandOut(
            floor_kg={"배추": 1000.0},
            cap_kg={"배추": 8000.0},
            cap_total_kg=8000.0,
            cap_amount_krw=None,
            cap_by_date_kg={},
            contributors={"배추": "inventory"},
            not_ready=[],
            usable=True,
        ),
        deadlock=None,
        clip_results=[
            _clip("SCN-1", 5000.0, clipped=False),
            _clip("SCN-2", 4000.0, clipped=True),
        ],
        ranked_ids=["SCN-1", "SCN-2"],
        recommended_id="SCN-1",
        soft_warnings=["[finance] 지급 일정이 빠듯합니다."],
    )


# --- 오케스트레이터 -----------------------------------------------------------
def test_response_carries_llm_fields():
    assert _LLM_FIELDS <= set(ProcurementResponse.model_fields)


def test_default_response_is_disabled_not_crashing():
    """LLM 을 붙이기 전에도 응답은 유효하다 — 기본값 DISABLED."""
    response = _procurement_response()
    assert response.llm_status == "DISABLED"
    assert response.llm_attempts == 0
    assert response.interpretation.ranked_scenario_ids == []


def test_context_carries_no_quantities():
    """Context 에 수량·금액이 실리면 안 된다 — 라벨과 코드만 넘긴다."""
    context = build_orchestrator_context(_procurement_response())
    dumped = context.model_dump_json()
    assert "5000" not in dumped
    assert "4000" not in dumped
    assert [c.clip_magnitude for c in context.candidates] == ["FULL", "MAJOR_CLIP"]


def test_core_survives_when_ollama_is_down():
    """★ LLM 이 죽어도 밴드·클리핑·순위는 그대로다."""
    original = _procurement_response()
    enriched = enrich_orchestrator_response(original, _failing_selection_service())

    assert enriched.llm_status == "FALLBACK"
    assert enriched.llm_fallback_used is True
    assert enriched.llm_model == "gemma3:4b"
    # Core 결과 무손실
    assert enriched.ranked_ids == original.ranked_ids
    assert enriched.recommended_id == original.recommended_id
    assert enriched.band == original.band
    assert enriched.clip_results == original.clip_results


def test_disabled_llm_keeps_deterministic_ranking():
    service = SelectionService(
        OrchestratorLLMSettings(
            enabled=False,
            provider="ollama",
            model="gemma3:4b",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        FailingProvider(),
    )
    enriched = enrich_orchestrator_response(_procurement_response(), service)
    assert enriched.llm_status == "DISABLED"
    assert enriched.ranked_ids == ["SCN-1", "SCN-2"]


# --- Critic -------------------------------------------------------------------
def test_critic_verdict_carries_llm_fields():
    from app.critic.schemas import CriticVerdictOut

    assert _LLM_FIELDS <= set(CriticVerdictOut.model_fields)


def test_critic_judge_does_not_kill_decision_when_down():
    """★ Ollama 장애 → judge 는 PASS 를 돌려주고 상태만 FALLBACK 으로 드러낸다."""
    runner = JudgeRunner(_failing_judge_service(), cycle="A")
    ok, _ = runner({"binding_constraints": ["cap_total_kg"], "rationale": "상한에 걸렸습니다."})
    assert ok is True
    assert runner.ran is False
    assert runner.result is not None
    assert runner.result.llm_status == "FALLBACK"


# --- Critic API — LLM 장애 시 커버리지 정직성 ----------------------------------
def _evidence(claim: str, ref: str, value: float, unit: str = "kg") -> dict:
    return {
        "claim": claim,
        "ref_ids": [ref],
        "value": value,
        "unit": unit,
        "evidence_grade": "OFFICIAL",
    }


def _critic_request(rationale: str = "재고 상한에 걸려 물량을 줄였습니다.") -> dict:
    return {
        "as_of": "2026-08-25",
        "target_scenario_id": "SCN-1",
        "rationale": rationale,
        "replies": [
            {
                "dept": "sales",
                "item": "배추",
                "reasoning": "김치공장 물량이 필요합니다.",
                "checks": [
                    {
                        "check_id": "sales.floor",
                        "floor_kg": {"배추": 1000},
                        "reason": "최소 물량",
                        "evidences": [_evidence("최소물량", "SO-1", 1000)],
                    }
                ],
            },
            {
                "dept": "inventory",
                "reasoning": "창고 여유가 부족합니다.",
                "checks": [
                    {
                        "check_id": "inv.cap",
                        "cap_kg": {"배추": 8000},
                        "cap_total_kg": 8000,
                        "evidences": [_evidence("가용", "WH-1", 8000)],
                    }
                ],
            },
            {
                "dept": "finance",
                "reasoning": "지급 일정이 빠듯합니다.",
                "checks": [
                    {
                        "check_id": "fin.cap",
                        "cap_amount_krw": 20000000,
                        "evidences": [_evidence("한도", "FIN-1", 20000000, "KRW")],
                    }
                ],
            },
        ],
        "scenarios": [
            {
                "scenario_id": "SCN-1",
                "stance": "보수",
                "qty_kg": {"배추": 3000},
                "unit_price_krw_per_kg": {"배추": 1200},
            },
            {
                "scenario_id": "SCN-2",
                "stance": "공격",
                "qty_kg": {"배추": 7000},
                "unit_price_krw_per_kg": {"배추": 1200},
            },
        ],
    }


def _force_unreachable_ollama(monkeypatch) -> None:
    """LLM 을 **켠 채로 못 닿게** 만든다.

    🔴 `LLM_BASE_URL` 만 죽은 포트로 두면 안 막힌다 (매입 실측 2026-09-03).
      `.env` 에 `CRITIC_LLM_PROVIDER=gemini` 가 있고 Critic 런타임이 그것을 읽는데
      **Gemini 는 자체 엔드포인트를 써서 `LLM_BASE_URL` 을 무시**한다
      (`critic/llm/runtime.py:53` 이 그 사정을 이미 적어 뒀다).

      그래서 *"LLM 이 없음"* 을 시험하려는 검사가 **실제 구글 API 를 불렀다.**
      빠르게 성공하면 `SUCCESS` 라 빨간불, 느리거나 429 면 `FALLBACK` 이라 초록불 —
      **상시 실패가 아니라 플래키**였고 요금과 429 한도까지 같이 썼다.

    ⚠️ `LLM_ENABLED=false` 로 막으면 안 된다. 그러면 `DISABLED` 가 나오는데
      이 검사가 재는 것은 `FALLBACK` 이다 — **"안 불렀다" 와 "부르고 실패했다" 는
      다르다** (`envelope.LLMStatus`).

    🔴 **`delenv` 로는 안 지워진다** (매입 실측 2026-09-03 · 2차 지적).

      `get_llm_settings()` 가 **함수 안에서** `load_dotenv(_ENV_FILE)` 을 부르고,
      그 기본값이 `override=False` 다. *"이미 설정된 변수만 안 덮는다"* 는 뜻이라
      **지워서 없는 상태로 만들면 `.env` 값이 그대로 채워진다.**

      ```text
      delenv 후 실제 provider   gemini   ← 되살아난다 (실측)
      setenv 후 실제 provider   ollama
      ```

      **지우는 것이 아니라 덮어써야 한다.** 값이 환경에 있어야 `.env` 가 못 이긴다.

      ⚠️ 저장소가 이 함정을 이미 세 곳에 적어 뒀다
      (`test_per_agent_model_override.py:23` · `test_purchase_agent/conftest.py:34` ·
      `test_mix_llm.py:107`). 그것을 안 읽고 `delenv` 를 썼다.
    """
    monkeypatch.setenv("CRITIC_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")

    # 🔴 **여기서 확인하지 않으면 다시 조용히 새어 나간다.**
    #   `.env` 가 이기면 이 검사가 재려던 것(닿지 않는 ollama)이 아니라 **실제 구글
    #   API 호출**이 된다. 빠르면 SUCCESS 라 빨간불 · 느리거나 429 면 FALLBACK 이라
    #   초록불인 **플래키**가 되고, 요금과 429 한도까지 같이 쓴다.
    settings = get_llm_settings()
    assert settings.provider == "ollama", (
        f"Critic 이 실제로 쓰는 프로바이더가 {settings.provider!r} 다. "
        f".env 가 이겼다 — 이 검사는 네트워크로 나가면 안 된다"
    )
    assert settings.base_url == "http://127.0.0.1:1", f"닿지 않아야 한다: {settings.base_url}"


def test_critic_api_reports_zero_l5_coverage_when_llm_unavailable(monkeypatch):
    """★ 검사하지 못한 것을 검사했다고 말하지 않는다 (설계서 §8).

    LLM 이 죽으면 coverage L5 는 0 이어야 하고, skipped 가 이유를 드러내야 한다.
    그러면서도 Core 판정(PASS)과 나머지 레이어는 그대로 살아 있어야 한다.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.critic.router import router

    # 아무도 듣지 않는 포트 — 실제 Ollama 없이 장애 상황을 만든다.
    _force_unreachable_ollama(monkeypatch)
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post("/critic/procurement", json=_critic_request())

    assert response.status_code == 200
    body = response.json()

    # LLM 은 죽었다
    assert body["llm_status"] == "FALLBACK"
    assert body["llm_fallback_used"] is True
    # 🔴 **무엇을 불러서 죽었는지까지 본다.** 픽스처 단언과 층이 다르다 —
    #   저쪽은 설정을 보고 여기는 결과를 본다. `.env` 가 이겨 gemini 로 나갔는데
    #   마침 실패하면 FALLBACK 이 나오고, 그때 이 줄이 잡는다.
    assert body["llm_provider"] == "ollama", (
        f"닿지 않는 ollama 를 재려던 검사가 {body['llm_provider']!r} 를 불렀다"
    )
    # 커버리지는 정직하다
    assert tuple(body["coverage"]["L5"]) == (0, 6)
    assert any("L5" in entry for entry in body["skipped"])
    # L5 가 돌지 않았으므로 L5 발 CONCERN 은 없어야 한다
    assert "E-LOGIC" not in {concern["code"] for concern in body["concerns"]}
    # Core 는 살아 있다 — LLM 장애가 판정을 FAIL 로 만들지 않는다
    assert body["status"] == "PASS"
    assert body["scenario_id"] == "SCN-1"
    assert tuple(body["coverage"]["L1"])[1] > 0


def test_critic_api_skips_l5_when_no_rationale_submitted(monkeypatch):
    """검사할 결정 근거가 없으면 L5 는 돌지 않는다.

    ★ 부서 회신(reasoning)으로 대신하지 않는다 — 부서 문장은 클리핑 이전에 쓰이므로
      binding_constraints 누락을 판정하면 정상 실행마다 CONCERN 이 붙는다(실측 확인).
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.critic.router import router

    _force_unreachable_ollama(monkeypatch)

    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).post("/critic/procurement", json=_critic_request("")).json()

    assert body["llm_status"] == "SKIPPED_TEMPLATE"
    assert body["llm_attempts"] == 0  # 호출 자체가 없었다
    assert tuple(body["coverage"]["L5"]) == (0, 6)
    assert any("L5" in entry for entry in body["skipped"])
    assert "E-LOGIC" not in {c["code"] for c in body["concerns"]}
